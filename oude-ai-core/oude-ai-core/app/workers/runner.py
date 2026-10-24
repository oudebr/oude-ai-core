"""
Fan-out por tenant: roda um job para os lojistas com o agente ATIVO com concorrência limitada.
Falha de um lojista é isolada (não derruba os outros) e nada de um lojista vaza para outro.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx

from app.core.tenant import TenantContext
from app.db.repositories import TenantRepository
from app.services.context import carregar_tenant

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)
JobFn = Callable[[TenantRepository, httpx.AsyncClient, TenantContext], Awaitable[int]]


async def executar_para_tenant(
    nome: str, job: JobFn, repo: TenantRepository, http: httpx.AsyncClient, settings: Settings, org_id: UUID
) -> int:
    try:
        tenant = await carregar_tenant(repo, settings, org_id)
        return await job(repo, http, tenant)
    except Exception:  # noqa: BLE001 - isolamento por tenant
        log.exception("job=%s org=%s falhou", nome, org_id)
        return 0


async def executar_para_todos(
    nome: str, job: JobFn, repo: TenantRepository, http: httpx.AsyncClient, settings: Settings
) -> None:
    limite = asyncio.Semaphore(settings.max_concurrent_tenants)
    total = tenants = 0

    async def um(org: UUID) -> None:
        nonlocal total, tenants
        async with limite:
            total += await executar_para_tenant(nome, job, repo, http, settings, org)
            tenants += 1

    async with asyncio.TaskGroup() as grupo:
        async for org in repo.listar_orgs_ativas():  # agente pausado => não roda
            grupo.create_task(um(org))
    log.info("job=%s concluído: tenants=%d ações=%d", nome, tenants, total)
