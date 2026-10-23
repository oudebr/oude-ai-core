"""Monta o TenantContext: lê as integrações do lojista no Supabase e decifra as chaves (vinculadas à org)."""
from __future__ import annotations

import logging
from uuid import UUID

from app.core.crypto import CredenciaisInvalidas, decifrar_credenciais
from app.core.tenant import TenantContext
from app.db.repositories import TenantRepository

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)


async def carregar_tenant(repo: TenantRepository, settings: Settings, org_id: UUID) -> TenantContext:
    chaves = settings.chaves_mestras()
    creds: dict[str, dict] = {}
    for linha in await repo.integracoes(org_id):
        try:
            creds[linha["provider"]] = decifrar_credenciais(linha["encrypted_credentials"], chaves, org_id)
        except CredenciaisInvalidas as exc:
            # Uma chave ruim de um lojista não pode derrubar os demais: ignora só essa integração.
            log.warning("org=%s provider=%s credencial ignorada: %s", org_id, linha["provider"], exc)
    return TenantContext(org_id=org_id, credenciais=creds)
