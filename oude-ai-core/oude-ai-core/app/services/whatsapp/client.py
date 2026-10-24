"""
WhatsApp do LOJISTA (as credenciais são dele, cifradas em org_integrations). Interface única, três adaptadores:

  meta_cloud : API oficial da Meta       POST https://graph.facebook.com/v20.0/{phone_number_id}/messages  (Bearer)
  evolution  : Evolution API v2          POST {base_url}/message/sendText/{instance}    header `apikey`, body {number, text}
  zapi       : Z-API                     POST https://api.z-api.io/instances/{id}/token/{token}/send-text
                                          header `Client-Token` (token de segurança da conta), body {phone, message}

Evolution/Z-API: formato conferido na documentação oficial em 2026-10-22. Meta: formato do esqueleto anterior,
NÃO reconferido nesta rodada. A `base_url` da Evolution é do lojista (instância própria) => passa por validação
anti-SSRF a cada envio. Sem redirecionamentos. Meta exige template aprovado fora da janela de 24 h.
"""
import re
from typing import Any, Protocol

import httpx

from app.core.net import UrlNaoPermitida, validar_url_externa
from app.core.tenant import TenantContext

PROVEDORES_WHATSAPP = ("meta_cloud", "evolution", "zapi")


class WhatsAppIndisponivel(Exception):
    pass


class WhatsAppClient(Protocol):
    async def enviar_texto(self, para: str, texto: str) -> None: ...


def _fone(para: str) -> str:
    fone = "".join(c for c in para if c.isdigit())
    if len(fone) < 10:
        raise WhatsAppIndisponivel("Telefone inválido.")
    return fone


async def _post(http: httpx.AsyncClient, url: str, headers: dict[str, str], corpo: dict[str, Any]) -> None:
    try:
        r = await http.post(url, headers=headers, json=corpo)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise WhatsAppIndisponivel(f"Falha no envio ({type(exc).__name__}).") from exc


class MetaCloudWhatsApp:
    def __init__(self, http: httpx.AsyncClient, phone_number_id: str, token: str) -> None:
        self._http, self._pid, self._token = http, phone_number_id, token

    async def enviar_texto(self, para: str, texto: str) -> None:
        await _post(
            self._http, f"https://graph.facebook.com/v20.0/{self._pid}/messages",
            {"Authorization": f"Bearer {self._token}"},
            {"messaging_product": "whatsapp", "to": _fone(para), "type": "text", "text": {"body": texto[:3500]}},
        )


class EvolutionWhatsApp:
    def __init__(self, http: httpx.AsyncClient, base_url: str, instance: str, apikey: str) -> None:
        self._http, self._base, self._instance, self._key = http, base_url.rstrip("/"), instance, apikey

    async def enviar_texto(self, para: str, texto: str) -> None:
        try:
            await validar_url_externa(self._base)   # a URL é do lojista: revalida a cada envio
        except UrlNaoPermitida as exc:
            raise WhatsAppIndisponivel(f"Endereço da Evolution API não permitido: {exc}") from exc
        await _post(
            self._http, f"{self._base}/message/sendText/{self._instance}", {"apikey": self._key},
            {"number": _fone(para), "text": texto[:3500]},
        )


class ZApiWhatsApp:
    def __init__(self, http: httpx.AsyncClient, instance_id: str, token: str, client_token: str | None) -> None:
        self._http, self._id, self._token, self._client_token = http, instance_id, token, client_token

    async def enviar_texto(self, para: str, texto: str) -> None:
        headers = {"Client-Token": self._client_token} if self._client_token else {}
        await _post(
            self._http, f"https://api.z-api.io/instances/{self._id}/token/{self._token}/send-text", headers,
            {"phone": _fone(para), "message": texto[:3500]},
        )


def whatsapp_do_tenant(http: httpx.AsyncClient, tenant: TenantContext) -> WhatsAppClient | None:
    """None = lojista sem WhatsApp conectado/válido (use o fallback: notificação no painel).
    Usa SEMPRE as credenciais da organização do contexto."""
    if not tenant.tem("whatsapp"):
        return None
    c = tenant.cred("whatsapp")
    prov = str(c.get("provider", "meta_cloud"))
    if prov == "meta_cloud" and c.get("phone_number_id") and c.get("access_token"):
        return MetaCloudWhatsApp(http, str(c["phone_number_id"]), str(c["access_token"]))
    if prov == "evolution" and c.get("base_url") and c.get("instance") and c.get("apikey"):
        return EvolutionWhatsApp(http, str(c["base_url"]), str(c["instance"]), str(c["apikey"]))
    if prov == "zapi" and c.get("instance_id") and c.get("token"):
        return ZApiWhatsApp(http, str(c["instance_id"]), str(c["token"]), c.get("client_token") or None)
    return None


async def testar_conexao(http: httpx.AsyncClient, cred: dict[str, Any]) -> tuple[bool | None, str]:
    """(ok, detalhe). ok=None => não existe teste com endpoint confirmado na documentação.
    Meta: GET no nó do número (Graph API). Evolution: GET /instance/connectionState/{instance} (apikey) — endpoint
    visto em espelho da documentação; só o status HTTP é usado. Z-API: endpoint de status não confirmado."""
    prov = str(cred.get("provider", "meta_cloud"))
    try:
        if prov == "meta_cloud":
            r = await http.get(
                f"https://graph.facebook.com/v20.0/{cred['phone_number_id']}",
                params={"fields": "display_phone_number"}, headers={"Authorization": f"Bearer {cred['access_token']}"},
            )
        elif prov == "evolution":
            base = await validar_url_externa(str(cred["base_url"]).rstrip("/"))
            r = await http.get(f"{base}/instance/connectionState/{cred['instance']}", headers={"apikey": str(cred["apikey"])})
        else:
            return None, "Teste de conexão da Z-API indisponível: endpoint de status não confirmado na documentação consultada."
    except (httpx.HTTPError, UrlNaoPermitida) as exc:
        return False, f"Falha de conexão ({type(exc).__name__})."
    return r.status_code == 200, f"Provedor respondeu HTTP {r.status_code}."


ID_INSTANCIA = re.compile(r"^[\w.-]{1,80}$")
