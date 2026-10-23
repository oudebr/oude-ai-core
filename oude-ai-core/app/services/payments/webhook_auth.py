"""Autenticação do webhook (pura, testável): segredo por organização/provedor, comparação em tempo constante."""
import hmac
from typing import Any


def webhook_autorizado(provedor: Any, cred: dict[str, Any] | None, segredo_da_url: str) -> bool:
    """False para: provedor inexistente/sem webhook (Pix), integração não conectada ou segredo diferente."""
    if provedor is None or not provedor.usa_webhook or not cred:
        return False
    esperado = str(cred.get("webhook_secret", ""))
    return bool(esperado) and hmac.compare_digest(esperado, segredo_da_url)
