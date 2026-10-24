"""
oude-ai-core — "funcionário autônomo" do OÚDE CRM (multitenant por org_id).

Rodar local:  uvicorn main:app --reload --port 8000
Produção:     uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2   (RUN_SCHEDULER=true em UMA réplica)
"""
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.routes import agent, health, tenants, webhooks
from app.config import get_settings
from app.core.auth import CacheJti
from app.core.ratelimit import LimitadorPorOrg
from app.core.logging import configurar_logs
from app.db.repositories import TenantRepository
from app.db.supabase import criar_cliente
from app.workers.scheduler import criar_scheduler

log = logging.getLogger("oude-ai-core")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configurar_logs(settings.log_level)
    cliente = await criar_cliente(settings)
    http = httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        follow_redirects=False,  # anti-SSRF: URLs de lojistas nunca seguem redirecionamento
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )
    app.state.settings = settings
    app.state.repo = TenantRepository(cliente)
    app.state.http = http
    app.state.cache_jti = CacheJti()
    app.state.limitador = LimitadorPorOrg(settings.chat_requests_per_minute)

    scheduler = None
    if settings.run_scheduler:
        scheduler = criar_scheduler(app.state.repo, http, settings)
        scheduler.start()
        log.info("scheduler iniciado: %s", [j.id for j in scheduler.get_jobs()])
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        await http.aclose()


app = FastAPI(title="OÚDE AI Core", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(health.router)
app.include_router(tenants.router)
app.include_router(agent.router)
app.include_router(webhooks.router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000)
