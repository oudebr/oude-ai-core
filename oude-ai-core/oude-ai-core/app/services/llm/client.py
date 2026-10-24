"""
IA com a chave do PRÓPRIO lojista (BYOK). OpenAI, Groq e xAI/Grok falam o mesmo protocolo
(/chat/completions), então um cliente httpx cobre os três. A chave só existe em memória nesta chamada.
"""
from typing import Any

import httpx

from app.core.tenant import TenantContext
from app.services.llm.perfumaria_prompt import SYSTEM_PROMPT_PERFUMARIA

BASES = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "xai": "https://api.x.ai/v1",
}


class LLMIndisponivel(Exception):
    pass


async def responder(
    http: httpx.AsyncClient,
    tenant: TenantContext,
    mensagem: str,
    historico: list[dict[str, str]] | None = None,
    extra_sistema: str = "",
) -> str:
    if not tenant.tem("llm"):
        raise LLMIndisponivel("Este lojista não conectou uma chave de IA.")
    cred = tenant.cred("llm")
    base = BASES.get(str(cred.get("provider", "")).lower())
    chave, modelo = cred.get("api_key"), cred.get("model")
    if not base or not chave or not modelo:
        raise LLMIndisponivel("Integração de IA incompleta (provider, model e api_key).")
    msgs: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT_PERFUMARIA + extra_sistema}]
    msgs += [m for m in (historico or []) if m.get("role") in ("user", "assistant")][-12:]
    msgs.append({"role": "user", "content": mensagem[:4000]})
    try:
        r = await http.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {chave}"},
            json={"model": modelo, "messages": msgs, "temperature": 0.5, "max_tokens": 600},
        )
        r.raise_for_status()
        dados: dict[str, Any] = r.json()
        return str(dados["choices"][0]["message"]["content"]).strip()
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        # Não propaga o erro cru (poderia conter detalhes da conta do lojista).
        raise LLMIndisponivel(f"Falha ao consultar o provedor de IA ({type(exc).__name__}).") from exc
