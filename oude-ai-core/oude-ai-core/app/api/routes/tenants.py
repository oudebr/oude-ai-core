"""
Rotas do lojista, chamadas pelo BACKEND do OÚDE CRM com `Authorization: Bearer <token assinado>`.
Cada rota exige um escopo; o papel real do usuário é conferido no banco (core/auth.py).
Respostas NUNCA incluem chaves/credenciais.
"""
import asyncio
import logging
from datetime import datetime, timezone
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
from app.services.agent import conversar_detalhado, registrar_log_seguro
from app.services.agente_config import (
    DOUTRINA_PADRAO, IDENTIDADE_PADRAO, bloqueios_de_ativacao,
)
from app.services.context import carregar_tenant
from app.services.integracoes import SCHEMAS, ConfigInvalida, validar_credenciais
from app.services.llm.client import BASES, LLMIndisponivel
from app.services.llm.prompt import TONS, sanear_instrucoes
from app.services.agente_config import PERMISSOES_PADRAO, com_padroes
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
        bloqueios = bloqueios_de_ativacao(t.tem("llm"), await repo.estado_integracoes(org_id))
        if bloqueios:
            raise HTTPException(409, " ".join(bloqueios))
    campos = {"ativo": ativo}
    if not ativo:
        campos["pausado_em"] = datetime.now(timezone.utc).isoformat()
    await repo.salvar_config(org_id, campos, p.user_id)
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


def _publico(provider: str, c: dict[str, Any]) -> dict[str, Any]:
    """Campos NÃO secretos de uma integração (lista branca), para a tela mostrar o que está conectado."""
    if provider == "llm":
        chave = str(c.get("api_key", ""))
        return {"provider": c.get("provider"), "model": c.get("model"), "chave_final": chave[-4:] if len(chave) >= 12 else ""}
    if provider == "whatsapp":
        return {"provider": c.get("provider")}
    if provider == "supplier":
        from urllib.parse import urlparse
        return {"host": urlparse(str(c.get("catalog_url", ""))).hostname}
    if provider == "infinitepay":
        return {"handle": c.get("handle")}
    if provider == "pix":
        return {"nome_recebedor": c.get("nome_recebedor"), "cidade": c.get("cidade")}
    return {}


@router.get("/integracoes/status")
async def status_detalhado(
    org_id: UUID, p: Principal = Depends(autorizar("integracoes.ler")), repo=Depends(get_repo), settings=Depends(get_settings_dep)
) -> dict:
    """Por integração: configurada?, último teste, dados públicos. Nunca devolve chaves."""
    t = await carregar_tenant(repo, settings, org_id)
    estado = {e["provider"]: e for e in await repo.estado_integracoes(org_id)}
    saida = {}
    for x in INTEGRACOES:
        e = estado.get(x, {})
        saida[x] = {
            "configurada": t.tem(x),
            "status": e.get("status", "configurado") if t.tem(x) else "nao_configurado",
            "ultimo_teste_em": e.get("ultimo_teste_em"), "ultimo_teste_ok": e.get("ultimo_teste_ok"),
            "detalhe": e.get("ultimo_detalhe", ""),
            "publico": _publico(x, t.cred(x)) if t.tem(x) else {},
        }
    return saida


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
    await repo.registrar_estado_integracao(org_id, provider, "configurado")  # credencial nova: teste anterior perde validade
    await repo.auditar(org_id, p.user_id, "integracao_salva", provider)
    return {"ok": True, "provider": provider}  # eco mínimo: nada de credencial


@router.delete("/integracoes/{provider}")
async def remover_integracao(
    org_id: UUID, provider: str, p: Principal = Depends(autorizar("integracoes.escrever")), repo=Depends(get_repo)
) -> dict:
    _provider_ou_404(provider)
    await repo.remover_integracao(org_id, provider)
    await repo.remover_estado_integracao(org_id, provider)
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
        ok, detalhe = False, f"Falha de conexão ({type(exc).__name__})."
    # ok=None => sem endpoint de teste confirmado: guarda o aviso, mas NÃO marca como conectado nem como erro
    await repo.registrar_estado_integracao(
        org_id, provider, "configurado" if ok is None else ("conectado" if ok else "erro"), ok, detalhe)
    await registrar_log_seguro(repo, org_id, "integracao", detalhe, provider, None, ok is not False)
    await repo.auditar(org_id, p.user_id, "integracao_testada", provider)
    return {"ok": ok, "detalhe": detalhe}


# ------------------------------------------------------------------ chat
class ChatIn(BaseModel):
    mensagem: str = Field(min_length=1, max_length=4000)
    contato: str | None = Field(default=None, max_length=80)
    teste: bool = False  # aba "Testar agente": funciona antes de ativar e NUNCA executa ação irreversível


@router.post("/chat")
async def chat(
    org_id: UUID, corpo: ChatIn, request: Request, p: Principal = Depends(autorizar("chat")),
    repo=Depends(get_repo), http=Depends(get_http), settings=Depends(get_settings_dep),
) -> dict:
    cfg = await repo.get_config(org_id) or {}
    if not corpo.teste and not cfg.get("ativo"):
        raise HTTPException(409, "O agente está pausado.")
    if not request.app.state.limitador.permitir(str(org_id)):
        raise HTTPException(429, "Limite de mensagens por minuto atingido.")
    t = await carregar_tenant(repo, settings, org_id)
    # o contato de teste é sempre do próprio usuário: não mistura com o histórico de clientes reais
    contato = f"teste:{p.user_id}:{(corpo.contato or 'sessao')[:30]}" if corpo.teste else (corpo.contato or f"painel:{p.user_id}")
    ident, dout, perm = await asyncio.gather(repo.get_identidade(org_id), repo.get_doutrina(org_id), repo.get_autonomia(org_id))
    try:
        r = await conversar_detalhado(http, repo, t, cfg, contato, corpo.mensagem, identidade=ident, doutrina=dout,
                                      autonomia=perm, simular=corpo.teste)
    except LLMIndisponivel as exc:
        await registrar_log_seguro(repo, org_id, "erro", str(exc)[:300], None, contato, False)
        raise HTTPException(502, str(exc)) from exc
    await registrar_log_seguro(repo, org_id, "teste" if corpo.teste else "chat", f"{len(r.ferramentas)} ferramenta(s) usada(s).",
                               None, contato, True)
    return {"resposta": r.resposta, "ferramentas": r.ferramentas, "teste": corpo.teste}


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
