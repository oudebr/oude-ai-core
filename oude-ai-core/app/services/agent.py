"""
Agente conversacional de uma organização: usa a chave de IA do PRÓPRIO lojista, as instruções
personalizadas dele, o histórico e a ferramenta `consultar_catalogo` (chamada de função, protocolo
OpenAI-compatível: OpenAI, Groq e xAI). O org_id NUNCA vem do modelo: é fixado aqui pelo servidor.
"""
import json
from typing import Any

import httpx

from app.core.tenant import TenantContext
from app.services.llm.client import BASES, LLMIndisponivel
from app.services.llm.prompt import montar_system_prompt

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
    }
]


async def _executar_ferramenta(repo, tenant: TenantContext, nome: str, argumentos: str) -> str:
    if nome != "consultar_catalogo":
        return json.dumps({"erro": "ferramenta desconhecida"})
    try:
        termo = str(json.loads(argumentos or "{}").get("termo", ""))
    except (ValueError, AttributeError):
        return json.dumps({"erro": "argumentos inválidos"})
    itens = await repo.buscar_catalogo(tenant.org_id, termo)  # org do TENANT, não do modelo
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
    if not tenant.tem("llm"):
        raise LLMIndisponivel("Este lojista não conectou uma chave de IA.")
    cred = tenant.cred("llm")
    base = BASES.get(str(cred.get("provider", "")).lower())
    chave, modelo = cred.get("api_key"), cred.get("model")
    if not base or not chave or not modelo:
        raise LLMIndisponivel("Integração de IA incompleta (provider, model e api_key).")

    historico = await repo.historico(tenant.org_id, contato)
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": montar_system_prompt(config.get("nome_agente", ""), config.get("instrucoes", ""), config.get("tom", ""))},
        *[m for m in historico if m.get("role") in ("user", "assistant")],
        {"role": "user", "content": mensagem[:4000]},
    ]
    resposta = ""
    try:
        for _ in range(MAX_RODADAS_FERRAMENTA + 1):
            r = await http.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {chave}"},
                json={"model": modelo, "messages": msgs, "tools": FERRAMENTAS, "temperature": 0.5, "max_tokens": 600},
            )
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            chamadas = msg.get("tool_calls") or []
            if not chamadas:
                resposta = str(msg.get("content") or "").strip()
                break
            msgs.append({"role": "assistant", "content": msg.get("content"), "tool_calls": chamadas})
            for c in chamadas:
                saida = await _executar_ferramenta(repo, tenant, c["function"]["name"], c["function"].get("arguments", ""))
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": saida})
        else:
            resposta = "Desculpe, não consegui concluir a consulta agora. Um atendente vai te ajudar."
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as exc:
        raise LLMIndisponivel(f"Falha ao consultar o provedor de IA ({type(exc).__name__}).") from exc
    if not resposta:
        raise LLMIndisponivel("O provedor de IA devolveu resposta vazia.")
    await repo.adicionar_mensagem(tenant.org_id, contato, "user", mensagem[:4000])
    await repo.adicionar_mensagem(tenant.org_id, contato, "assistant", resposta)
    return resposta
