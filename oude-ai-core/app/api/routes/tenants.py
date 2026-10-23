"""
Rotas do lojista, chamadas pelo BACKEND do OÚDE CRM com `Authorization: Bearer <token assinado>`.
Cada rota exige um escopo; o papel real do usuário é conferido no banco (core/auth.py).
Respostas NUNCA incluem chaves/credenciais.
"""
import logging
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.authz import autorizar
from app.api.deps import get_http, get_repo, get_settings_dep
from app.core.auth import Principal
from app.core.crypto import cifrar_credenciais
from app.core.net import UrlNaoPermitida, validar_url_externa
from app.core.providers import INTEGRACOES, PAGAMENTOS
from app.db.repositories import TenantRepository
from app.services.agent import conversar
from app.services.context import carregar_tenant
from app.services.integracoes import SCHEMAS, ConfigInvalida, validar_credenciais
from app.services.llm.client import BASES, LLMIndisponivel
from app.services.llm.prompt import TONS, sanear_instrucoes
from app.services.payments.base import PagamentoIndisponivel
from app.services.payments.confirmacao import confirmar_manual
from app.services.payments.registry import provedor_de
from app.services.whatsapp.client import testar_conexao
from app.services.pedidos import PedidoInvalido, validar_itens
from app.workers.runner import executar_para_tenant
from app.workers.scheduler import JOBS

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/tenants/{org_id}", tags=["tenants"])


# ------------------------------------------------------------------ agente (instruções, ligar/pausar)
@router.get("/agente")
async def ler_agente(
    org_id: UUID, p: Principal = Depends(autorizar("agente.ler")), repo=Depends(get_repo), settings=Depends(get_settings_dep)
) -> dict:
    cfg = await repo.get_config(org_id) or {}
    t = await carregar_tenant(repo, settings, org_id)
    return {
        "ativo": bool(cfg.get("ativo", False)),
        "nome_agente": cfg.get("nome_agente", "Assistente"),
        "instrucoes": cfg.get("instrucoes", ""),
        "tom": cfg.get("tom", "cordial"),
        "tons_disponiveis": list(TONS),
        "integracoes": {x: t.tem(x) for x in INTEGRACOES},
    }


@router.put("/agente")
async def salvar_agente(
    org_id: UUID, corpo: dict[str, Any] = Body(...), p: Principal = Depends(autorizar("agente.escrever")),
    repo=Depends(get_repo),
) -> dict:
    campos: dict[str, Any] = {}
    if "instrucoes" in corpo:
        if not isinstance(corpo["instrucoes"], str):
            raise HTTPException(422, "instrucoes deve ser texto.")
        campos["instrucoes"] = sanear_instrucoes(corpo["instrucoes"])
    if "nome_agente" in corpo:
        n = corpo["nome_agente"]
        if not isinstance(n, str) or not 1 <= len(n.strip()) <= 60:
            raise HTTPException(422, "nome_agente deve ter de 1 a 60 caracteres.")
        campos["nome_agente"] = n.strip()
    if "tom" in corpo:
        if corpo["tom"] not in TONS:
            raise HTTPException(422, f"tom deve ser um de {list(TONS)}.")
        campos["tom"] = corpo["tom"]
    if not campos:
        raise HTTPException(422, "Nada para salvar.")
    await repo.salvar_config(org_id, campos, p.user_id)  # 'ativo' NÃO passa por aqui (rotas ativar/pausar)
    await repo.auditar(org_id, p.user_id, "agente_config", ",".join(sorted(campos)))
    return {"ok": True}


async def _definir_ativo(org_id: UUID, p: Principal, repo, settings, ativo: bool) -> dict:
    if ativo:
        t = await carregar_tenant(repo, settings, org_id)
        if not t.tem("llm"):
            raise HTTPException(409, "Conecte a chave de IA antes de ativar o agente.")
    await repo.salvar_config(org_id, {"ativo": ativo}, p.user_id)
    await repo.auditar(org_id, p.user_id, "agente_ativado" if ativo else "agente_pausado")
    return {"ativo": ativo}


@router.post("/agente/ativar")
async def ativar(org_id: UUID, p: Principal = Depends(autorizar("agente.escrever")), repo=Depends(get_repo), settings=Depends(get_settings_dep)) -> dict:
    return await _definir_ativo(org_id, p, repo, settings, True)


@router.post("/agente/pausar")
async def pausar(org_id: UUID, p: Principal = Depends(autorizar("agente.escrever")), repo=Depends(get_repo), settings=Depends(get_settings_dep)) -> dict:
    return await _definir_ativo(org_id, p, repo, settings, False)


# ------------------------------------------------------------------ integrações
@router.get("/integracoes/schema")
async def schema_integracoes(org_id: UUID, p: Principal = Depends(autorizar("integracoes.ler"))) -> dict:
    """Campos de cada integração, para a tela do lojista montar o formulário (sem nenhum segredo)."""
    return SCHEMAS


@router.get("/integracoes")
async def status_integracoes(
    org_id: UUID, p: Principal = Depends(autorizar("integracoes.ler")), repo=Depends(get_repo), settings=Depends(get_settings_dep)
) -> dict:
    t = await carregar_tenant(repo, settings, org_id)
    return {x: t.tem(x) for x in INTEGRACOES}  # nunca devolve as chaves


def _provider_ou_404(provider: str) -> str:
    if provider not in INTEGRACOES:
        raise HTTPException(404, "Integração desconhecida.")
    return provider


@router.put("/integracoes/{provider}")
async def salvar_integracao(
    org_id: UUID, provider: str, corpo: dict[str, Any] = Body(...),
    p: Principal = Depends(autorizar("integracoes.escrever")), repo=Depends(get_repo), settings=Depends(get_settings_dep),
) -> dict:
    _provider_ou_404(provider)
    existente = (await carregar_tenant(repo, settings, org_id)).credenciais.get(provider)
    try:
        limpo = validar_credenciais(provider, corpo, existente)
    except ConfigInvalida as exc:
        raise HTTPException(422, str(exc)) from exc
    url_do_lojista = limpo.get("catalog_url") if provider == "supplier" else limpo.get("base_url")  # URLs do lojista: anti-SSRF
    if url_do_lojista:
        try:
            await validar_url_externa(url_do_lojista)
        except UrlNaoPermitida as exc:
            raise HTTPException(422, str(exc)) from exc
    await repo.salvar_integracao(org_id, provider, cifrar_credenciais(limpo, settings.chaves_mestras(), org_id))
    await repo.auditar(org_id, p.user_id, "integracao_salva", provider)
    return {"ok": True, "provider": provider}  # eco mínimo: nada de credencial


@router.delete("/integracoes/{provider}")
async def remover_integracao(
    org_id: UUID, provider: str, p: Principal = Depends(autorizar("integracoes.escrever")), repo=Depends(get_repo)
) -> dict:
    _provider_ou_404(provider)
    await repo.remover_integracao(org_id, provider)
    await repo.auditar(org_id, p.user_id, "integracao_removida", provider)
    return {"ok": True}


@router.post("/integracoes/{provider}/testar")
async def testar_integracao(
    org_id: UUID, provider: str, p: Principal = Depends(autorizar("integracoes.testar")),
    repo=Depends(get_repo), http=Depends(get_http), settings=Depends(get_settings_dep),
) -> dict:
    """Teste de conexão com as credenciais SALVAS da organização. Real só onde há endpoint confirmado:
    IA (GET /models), WhatsApp Meta/Evolution. Z-API, pagamentos e fornecedor: sem teste confirmado."""
    _provider_ou_404(provider)
    t = await carregar_tenant(repo, settings, org_id)
    if not t.tem(provider):
        raise HTTPException(409, "Integração não configurada.")
    c = t.cred(provider)
    try:
        if provider == "llm":
            r = await http.get(f"{BASES[c['provider']]}/models", headers={"Authorization": f"Bearer {c['api_key']}"})
            ok, detalhe = r.status_code == 200, f"Provedor respondeu HTTP {r.status_code}."
        elif provider == "whatsapp":
            ok, detalhe = await testar_conexao(http, c)
        elif provider == "supplier":
            await validar_url_externa(c["catalog_url"])
            ok, detalhe = None, "URL válida. Teste de conexão indisponível: contrato da API do fornecedor ainda não definido."
        else:
            ok, detalhe = None, "Sem endpoint de teste confirmado na documentação deste meio de pagamento."
    except (httpx.HTTPError, UrlNaoPermitida) as exc:
        return {"ok": False, "detalhe": f"Falha de conexão ({type(exc).__name__})."}
    await repo.auditar(org_id, p.user_id, "integracao_testada", provider)
    return {"ok": ok, "detalhe": detalhe}


# ------------------------------------------------------------------ chat
class ChatIn(BaseModel):
    mensagem: str = Field(min_length=1, max_length=4000)
    contato: str | None = Field(default=None, max_length=80)


@router.post("/chat")
async def chat(
    org_id: UUID, corpo: ChatIn, request: Request, p: Principal = Depends(autorizar("chat")),
    repo=Depends(get_repo), http=Depends(get_http), settings=Depends(get_settings_dep),
) -> dict:
    cfg = await repo.get_config(org_id)
    if not cfg or not cfg.get("ativo"):
        raise HTTPException(409, "O agente está pausado.")
    if not request.app.state.limitador.permitir(str(org_id)):
        raise HTTPException(429, "Limite de mensagens por minuto atingido.")
    t = await carregar_tenant(repo, settings, org_id)
    contato = corpo.contato or f"painel:{p.user_id}"
    try:
        return {"resposta": await conversar(http, repo, t, cfg, contato, corpo.mensagem)}
    except LLMIndisponivel as exc:
        raise HTTPException(502, str(exc)) from exc


# ------------------------------------------------------------------ jobs
@router.post("/jobs/{job}")
async def rodar_job(
    org_id: UUID, job: str, p: Principal = Depends(autorizar("jobs.rodar")), repo: TenantRepository = Depends(get_repo),
    http=Depends(get_http), settings=Depends(get_settings_dep),
) -> dict:
    if job not in JOBS:
        raise HTTPException(404, "Job desconhecido.")
    acoes = await executar_para_tenant(job, JOBS[job][0], repo, http, settings, org_id)
    await repo.auditar(org_id, p.user_id, "job_manual", job)
    return {"job": job, "acoes": acoes}


# ------------------------------------------------------------------ pedidos + pagamento (vários provedores)
@router.post("/pedidos")
async def criar_pedido(
    org_id: UUID, corpo: dict[str, Any] = Body(...), p: Principal = Depends(autorizar("pedidos.escrever")),
    repo=Depends(get_repo), http=Depends(get_http), settings=Depends(get_settings_dep),
) -> dict:
    try:
        itens, total = validar_itens(corpo.get("itens"))
    except PedidoInvalido as exc:
        raise HTTPException(422, str(exc)) from exc
    contato = str(corpo.get("contato", "balcao"))[:80]
    t = await carregar_tenant(repo, settings, org_id)
    conectados = [x for x in PAGAMENTOS if t.tem(x)]       # SÓ os meios de pagamento DESTA organização
    nome = corpo.get("provedor") or (conectados[0] if len(conectados) == 1 else None)
    if nome not in PAGAMENTOS:
        raise HTTPException(422, f"Informe 'provedor' (um de {conectados or 'nenhum conectado'}).")
    if not t.tem(nome):
        raise HTTPException(409, f"Conecte {nome} antes de criar pedidos com esse meio de pagamento.")
    provedor = provedor_de(nome)
    cred = t.cred(nome)
    if provedor.usa_webhook and not settings.public_base_url.startswith("https://"):
        raise HTTPException(503, "PUBLIC_BASE_URL (https) não configurada no serviço.")
    pedido = await repo.criar_pedido(org_id, contato, itens, total, nome)
    pid = str(pedido["id"])
    webhook = (f"{settings.public_base_url.rstrip('/')}/webhooks/pagamentos/{nome}/{org_id}/{cred['webhook_secret']}"
               if provedor.usa_webhook else "")
    try:
        cobranca = await provedor.criar_cobranca(http, cred, pid, itens, total, webhook)
    except PagamentoIndisponivel as exc:
        raise HTTPException(502, f"Pedido {pid} criado, mas a cobrança falhou: {exc}") from exc
    await repo.salvar_checkout(org_id, pid, cobranca.bruto, cobranca.url)
    await repo.auditar(org_id, p.user_id, "pedido_criado", f"{pid} via {nome}")
    return {"pedido_id": pid, "provedor": nome, "total_centavos": total, "checkout_url": cobranca.url,
            "pix_copia_e_cola": cobranca.copia_e_cola, "aviso": cobranca.aviso}


@router.get("/pedidos/{pedido_id}")
async def ver_pedido(org_id: UUID, pedido_id: UUID, p: Principal = Depends(autorizar("pedidos.ler")), repo=Depends(get_repo)) -> dict:
    ped = await repo.get_pedido(org_id, str(pedido_id))  # filtra por org: de outra org => 404
    if not ped:
        raise HTTPException(404, "Pedido não encontrado.")
    return {k: ped.get(k) for k in ("id", "provedor", "status", "total_centavos", "itens", "checkout_url", "created_at", "paid_at")}


@router.post("/pedidos/{pedido_id}/confirmar-manual")
async def confirmar_pedido_manual(
    org_id: UUID, pedido_id: UUID, p: Principal = Depends(autorizar("pedidos.escrever")), repo=Depends(get_repo)
) -> dict:
    """Só para Pix estático (sem aviso automático do banco). Exige dono/admin e fica na auditoria."""
    r = await confirmar_manual(repo, org_id, p.user_id, str(pedido_id))
    if r.status == "ignorado":
        raise HTTPException(404 if "inexistente" in r.detalhe else 409, r.detalhe)
    return {"status": r.status, "detalhe": r.detalhe}
