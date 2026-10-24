"""
Integrações do LOJISTA (cada uma gravada cifrada em org_integrations). Aqui: formulário (SCHEMAS, para a tela do CRM
desenhar os campos) e validação (sem pydantic: explícita e testável). Campos desconhecidos são descartados.
"""
import re
import secrets
from typing import Any
from urllib.parse import urlparse

from app.core.providers import INTEGRACOES


class ConfigInvalida(ValueError):
    pass


def _campo(nome: str, rotulo: str, secreto: bool = False, obrigatorio: bool = True, opcoes: list[str] | None = None) -> dict[str, Any]:
    return {"nome": nome, "rotulo": rotulo, "secreto": secreto, "obrigatorio": obrigatorio, "opcoes": opcoes}


# O que a tela do lojista deve pedir. `secreto` => campo de senha; nunca devolvido em nenhuma resposta.
SCHEMAS: dict[str, dict[str, Any]] = {
    "llm": {"titulo": "Chave de IA (OpenAI, Groq, xAI)", "campos": [
        _campo("provider", "Provedor", opcoes=["openai", "groq", "xai"]), _campo("model", "Modelo"),
        _campo("api_key", "Chave da API", secreto=True)]},
    "whatsapp": {"titulo": "WhatsApp", "campos": [
        _campo("provider", "Provedor", opcoes=["meta_cloud", "evolution", "zapi"]),
        _campo("notify_to", "WhatsApp do responsável (com DDD)"),
        _campo("phone_number_id", "Meta: ID do número", obrigatorio=False),
        _campo("access_token", "Meta: token de acesso", secreto=True, obrigatorio=False),
        _campo("base_url", "Evolution: URL do servidor (https)", obrigatorio=False),
        _campo("instance", "Evolution: nome da instância", obrigatorio=False),
        _campo("apikey", "Evolution: apikey", secreto=True, obrigatorio=False),
        _campo("instance_id", "Z-API: ID da instância", obrigatorio=False),
        _campo("token", "Z-API: token da instância", secreto=True, obrigatorio=False),
        _campo("client_token", "Z-API: token de segurança da conta", secreto=True, obrigatorio=False)]},
    "supplier": {"titulo": "Fornecedor (sincronização ainda não disponível)", "campos": [
        _campo("catalog_url", "URL do catálogo (https)"), _campo("api_key", "Chave da API", secreto=True, obrigatorio=False)]},
    "infinitepay": {"titulo": "InfinitePay", "campos": [_campo("handle", "InfiniteTag (sem $)")]},
    "mercadopago": {"titulo": "Mercado Pago", "campos": [_campo("access_token", "Access token", secreto=True)]},
    "pix": {"titulo": "Pix (confirmação manual)", "campos": [
        _campo("chave_pix", "Chave Pix"), _campo("nome_recebedor", "Nome do recebedor (até 25)"), _campo("cidade", "Cidade (até 15)")]},
}
assert set(SCHEMAS) == set(INTEGRACOES)  # toda integração tem formulário


def _txt(d: dict[str, Any], campo: str, minimo: int = 1, maximo: int = 400) -> str:
    v = d.get(campo)
    if not isinstance(v, str) or not (minimo <= len(v.strip()) <= maximo):
        raise ConfigInvalida(f"Campo '{campo}' ausente ou inválido.")
    return v.strip()


def _id_seguro(d: dict[str, Any], campo: str, maximo: int = 80) -> str:
    v = _txt(d, campo, 1, maximo)
    if not re.fullmatch(r"[\w.-]+", v):  # vira parte de caminho de URL: só caracteres seguros
        raise ConfigInvalida(f"Campo '{campo}' contém caracteres inválidos.")
    return v


def _url_https(d: dict[str, Any], campo: str) -> str:
    url = _txt(d, campo, 10, 500)
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ConfigInvalida(f"{campo} deve ser https e sem usuário/senha.")
    return url


def _segredo_webhook(existente: dict[str, Any] | None) -> str:
    # gerado aqui e preservado em re-salvamentos (senão o webhook já enviado ao provedor deixaria de funcionar)
    return (existente or {}).get("webhook_secret") or secrets.token_urlsafe(32)


def validar_credenciais(provider: str, d: dict[str, Any], existente: dict[str, Any] | None = None) -> dict[str, Any]:
    """Devolve o dicionário LIMPO a ser cifrado."""
    if provider == "llm":
        prov = _txt(d, "provider", 1, 20).lower()
        if prov not in ("openai", "groq", "xai"):
            raise ConfigInvalida("provider de IA deve ser openai, groq ou xai.")
        return {"provider": prov, "model": _txt(d, "model", 1, 80), "api_key": _txt(d, "api_key", 8, 300)}

    if provider == "whatsapp":
        prov = str(d.get("provider", "meta_cloud")).lower()
        fone = re.sub(r"\D", "", _txt(d, "notify_to", 10, 30))
        if len(fone) < 10:
            raise ConfigInvalida("notify_to deve ser um telefone com DDD.")
        base = {"provider": prov, "notify_to": fone}
        if prov == "meta_cloud":
            pid = _txt(d, "phone_number_id", 5, 30)
            if not pid.isdigit():
                raise ConfigInvalida("phone_number_id deve ser numérico.")
            return {**base, "phone_number_id": pid, "access_token": _txt(d, "access_token", 10, 600)}
        if prov == "evolution":
            return {**base, "base_url": _url_https(d, "base_url").rstrip("/"), "instance": _id_seguro(d, "instance"),
                    "apikey": _txt(d, "apikey", 4, 300)}
        if prov == "zapi":
            saida = {**base, "instance_id": _id_seguro(d, "instance_id"), "token": _id_seguro(d, "token", 200)}
            if d.get("client_token"):
                saida["client_token"] = _txt(d, "client_token", 4, 200)
            return saida
        raise ConfigInvalida("provider de WhatsApp deve ser meta_cloud, evolution ou zapi.")

    if provider == "supplier":
        saida: dict[str, Any] = {"catalog_url": _url_https(d, "catalog_url")}
        if d.get("api_key"):
            saida["api_key"] = _txt(d, "api_key", 4, 400)
        return saida

    if provider == "infinitepay":
        handle = _txt(d, "handle", 2, 60).lstrip("$")
        if not re.fullmatch(r"[\w.-]+", handle):
            raise ConfigInvalida("handle (InfiniteTag) inválido.")
        return {"handle": handle, "webhook_secret": _segredo_webhook(existente)}

    if provider == "mercadopago":
        return {"access_token": _txt(d, "access_token", 10, 400), "webhook_secret": _segredo_webhook(existente)}

    if provider == "pix":
        chave = _txt(d, "chave_pix", 3, 77)
        if not re.fullmatch(r"[\w@.+\-]+", chave):
            raise ConfigInvalida("chave_pix contém caracteres inválidos.")
        return {"chave_pix": chave, "nome_recebedor": _txt(d, "nome_recebedor", 1, 25), "cidade": _txt(d, "cidade", 1, 15)}

    raise ConfigInvalida("Integração desconhecida.")
