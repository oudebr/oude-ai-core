"""Dependência FastAPI de autorização: Authorization: Bearer <token assinado pelo backend do CRM>."""
from uuid import UUID

from fastapi import Header, HTTPException, Request

from app.core.auth import NaoAutenticado, Principal, SemPermissao, autorizar_requisicao


def autorizar(escopo: str):
    async def dep(request: Request, org_id: UUID, authorization: str | None = Header(default=None)) -> Principal:
        token = authorization[7:].strip() if authorization and authorization[:7].lower() == "bearer " else None
        s = request.app.state
        try:
            return await autorizar_requisicao(
                s.repo, token, org_id, escopo, request.method, s.settings.segredos_crm(), s.cache_jti,
                s.settings.token_ttl_max_seconds,
            )
        except NaoAutenticado as exc:
            raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc
        except SemPermissao as exc:
            raise HTTPException(403, str(exc)) from exc

    return dep
