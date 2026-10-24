"""
Agente conversacional de uma organização: usa a chave de IA do PRÓPRIO lojista, as instruções
personalizadas dele, o histórico e a ferramenta `consultar_catalogo` (chamada de função, protocolo
OpenAI-compatível: OpenAI, Groq e xAI). O org_id NUNCA vem do modelo: é fixado aqui pelo servidor.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.tenant import TenantContext
from app.services.agente_config import (
    DOUTRINA_PADRAO, IDENTIDADE_PADRAO, PERMISSOES_PADRAO, PermissaoNegada, com_padroes, exigir, ferramentas_permitidas,
)
from app.services.llm.client import BASES, LLMIndisponivel
from app.services.llm.prompt import montar_system_prompt

log = logging.getLogger(__name__)
MAX_RODADAS_FERRAMENTA = 3

FERRAMENTAS = [
    {
        "type": "function",
        "function": {
            "name": "consultar_catalogo",
            "description": "Busca produtos reais do catálogo da loja por nome, categoria ou família olfativa. Retorna preço (R$), estoque, tamanho e notas.",
            "parameters": {
                "type": "object",
                "properties": {"termo": {"type": "string", "description": "nome, categoria ou família olfativa"}},
                "required": ["termo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "encaminhar_funcionario",
            "description": "Avisa um atendente humano da loja que este cliente precisa de ajuda (use quando não souber responder, quando o cliente pedir um atendente ou quiser algo que você não está autorizado a fazer).",
            "parameters": {
                "type": "object",
                "properties": {"motivo": {"type": "string", "description": "resumo curto do que o cliente precisa"}},
                "required": ["motivo"],
            },
        },
    },
]


async def registrar_log_seguro(repo, org_id, tipo: str, detalhe: str = "", ferramenta: str | None = None,
                               contato: str | None = None, ok: bool = True) -> None:
    """Log é diagnóstico: se falhar (tabela ausente, rede), a conversa do cliente NÃO pode cair por isso."""
    try:
        await repo.registrar_log(org_id, tipo, detalhe, ferramenta, contato, ok)
    except Exception as exc:  # noqa: BLE001
        log.warning("org=%s falha ao gravar agent_logs (%s)", org_id, type(exc).__name__)


@dataclass
class Resultado:
    """Resposta do agente + o que ele fez (para a aba "Testar agente" mostrar as ferramentas acionadas)."""
    resposta: str
    ferramentas: list[dict[str, Any]] = field(default_factory=list)


async def _executar_ferramenta(
    repo, tenant: TenantContext, nome: str, argumentos: str, permissoes: dict[str, Any], simular: bool = False,
    contato: str = "",
) -> tuple[str, dict[str, Any]]:
    """Devolve (texto para o modelo, registro para o painel). A permissão é conferida AQUI, no servidor, mesmo
    que o modelo peça uma ferramenta que não foi oferecida a ele."""
    reg: dict[str, Any] = {"ferramenta": nome, "ok": True, "simulado": False, "detalhe": ""}
    try:
        args = json.loads(argumentos or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        reg.update(ok=False, detalhe="argumentos inválidos")
        return json.dumps({"erro": "argumentos inválidos"}), reg
    try:
        if nome == "consultar_catalogo":
            exigir(permissoes, "consultar_estoque")
            return await _catalogo(repo, tenant, str(args.get("termo", "")), reg), reg
        if nome == "encaminhar_funcionario":
            exigir(permissoes, "encaminhar_funcionario")
            motivo = str(args.get("motivo", ""))[:300] or "Cliente pediu ajuda."
            if simular:  # modo teste: nada irreversível acontece
                reg.update(simulado=True, detalhe="Simulado no teste: nenhum aviso foi enviado.")
            else:
                await repo.criar_notificacao(tenant.org_id, "agente_transbordo", "Cliente precisa de atendimento humano",
                                             f"{motivo} (contato: {contato[:60]})")
                reg["detalhe"] = "Atendente avisado no painel."
            return json.dumps({"ok": True, "mensagem": "Um atendente foi avisado."}), reg
    except PermissaoNegada as exc:
        reg.update(ok=False, detalhe=str(exc))
        return json.dumps({"erro": "ação não autorizada pelo lojista"}), reg
    reg.update(ok=False, detalhe="ferramenta desconhecida")
    return json.dumps({"erro": "ferramenta desconhecida"}), reg


async def _catalogo(repo, tenant: TenantContext, termo: str, reg: dict[str, Any]) -> str:
    itens = await repo.buscar_catalogo(tenant.org_id, termo)  # org do TENANT, não do modelo
    reg["detalhe"] = f"{len(itens)} item(ns) para “{termo[:40]}”."
    return json.dumps(
        [
            {"nome": i["nome"], "categoria": i.get("categoria"),
             "preco_reais": float(i["preco_venda"]) if i.get("preco_venda") is not None else None,
             "estoque": i.get("estoque"),
             "tamanho": (f"{i['volume_ml']} ml" if i.get("volume_ml") else None),
             "familia_olfativa": i.get("familia_olfativa"),
             "notas": {"topo": i.get("nota_topo") or None, "coracao": i.get("nota_coracao") or None, "fundo": i.get("nota_fundo") or None}}
            for i in itens
        ],
        ensure_ascii=False,
    )


async def conversar(
    http: httpx.AsyncClient, repo, tenant: TenantContext, config: dict[str, Any], contato: str, mensagem: str
) -> str:
    """Atalho compatível: devolve só o texto, com a configuração padrão de identidade/doutrina/autonomia."""
    return (await conversar_detalhado(http, repo, tenant, config, contato, mensagem)).resposta


async def conversar_detalhado(
    http: httpx.AsyncClient, repo, tenant: TenantContext, config: dict[str, Any], contato: str, mensagem: str,
    *, identidade: dict[str, Any] | None = None, doutrina: dict[str, Any] | None = None,
    autonomia: dict[str, Any] | None = None, simular: bool = False,
) -> Resultado:
    perm = com_padroes(autonomia, PERMISSOES_PADRAO)
    try:
        exigir(perm, "responder_duvidas")
    except PermissaoNegada as exc:
        raise LLMIndisponivel("O lojista desativou as respostas automáticas do agente.") from exc
    ident, dout = com_padroes(identidade, IDENTIDADE_PADRAO), com_padroes(doutrina, DOUTRINA_PADRAO)
    if not tenant.tem("llm"):
        raise LLMIndisponivel("Este lojista não conectou uma chave de IA.")
    cred = tenant.cred("llm")
    base = BASES.get(str(cred.get("provider", "")).lower())
    chave, modelo = cred.get("api_key"), cred.get("model")
    if not base or not chave or not modelo:
        raise LLMIndisponivel("Integração de IA incompleta (provider, model e api_key).")

    historico = await repo.historico(tenant.org_id, contato)
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": montar_system_prompt(
            config.get("nome_agente", ""), config.get("instrucoes", ""), config.get("tom", ""),
            identidade=ident, doutrina=dout, autonomia=perm)},
        *[m for m in historico if m.get("role") in ("user", "assistant")],
        {"role": "user", "content": mensagem[:4000]},
    ]
    resposta = ""
    usadas: list[dict[str, Any]] = []
    oferecidas = ferramentas_permitidas(perm)
    ferramentas = [f for f in FERRAMENTAS if f["function"]["name"] in oferecidas]
    try:
        for _ in range(MAX_RODADAS_FERRAMENTA + 1):
            r = await http.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {chave}"},
                json={"model": modelo, "messages": msgs, **({"tools": ferramentas} if ferramentas else {}),
                      "temperature": 0.5, "max_tokens": 600},
            )
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            chamadas = msg.get("tool_calls") or []
            if not chamadas:
                resposta = str(msg.get("content") or "").strip()
                break
            msgs.append({"role": "assistant", "content": msg.get("content"), "tool_calls": chamadas})
            for c in chamadas:
                saida, reg = await _executar_ferramenta(
                    repo, tenant, c["function"]["name"], c["function"].get("arguments", ""), perm, simular, contato)
                usadas.append(reg)
                await registrar_log_seguro(repo, tenant.org_id, "teste" if simular else "ferramenta", reg["detalhe"],
                                           reg["ferramenta"], contato, reg["ok"])
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": saida})
        else:
            resposta = "Desculpe, não consegui concluir a consulta agora. Um atendente vai te ajudar."
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as exc:
        raise LLMIndisponivel(f"Falha ao consultar o provedor de IA ({type(exc).__name__}).") from exc
    if not resposta:
        raise LLMIndisponivel("O provedor de IA devolveu resposta vazia.")
    await repo.adicionar_mensagem(tenant.org_id, contato, "user", mensagem[:4000])
    await repo.adicionar_mensagem(tenant.org_id, contato, "assistant", resposta)
    return Resultado(resposta=resposta, ferramentas=usadas)
