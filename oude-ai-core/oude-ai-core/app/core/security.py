"""Token estático operacional (legado). A autorização por organização está em app/core/auth.py."""
import hmac

from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings


async def exigir_token_interno(
    x_internal_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Só para rotas operacionais /v1/internal/* (sem dados de organização). Rotas de lojista usam core/auth."""
    if settings.internal_api_token is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Não encontrado.")
    esperado = settings.internal_api_token.get_secret_value()
    # compare_digest evita ataque de tempo.
    if not x_internal_token or not hmac.compare_digest(x_internal_token, esperado):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Não autorizado.")
