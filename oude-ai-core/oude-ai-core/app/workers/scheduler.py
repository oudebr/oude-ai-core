"""Agenda os jobs. `coalesce` + `max_instances=1` impedem acúmulo se uma rodada demorar."""
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings
from app.db.repositories import TenantRepository
from app.workers.jobs.engajamento import engajamento
from app.workers.jobs.estoque import alerta_estoque
from app.workers.jobs.fornecedor import sincronizar_fornecedor
from app.workers.runner import executar_para_todos

# nome -> (função do job, gatilho). Horários em America/Sao_Paulo.
JOBS = {
    "estoque": (alerta_estoque, IntervalTrigger(minutes=30)),
    "engajamento": (engajamento, CronTrigger(hour=9, minute=0, timezone="America/Sao_Paulo")),
    "fornecedor": (sincronizar_fornecedor, CronTrigger(hour="*/6", minute=15, timezone="America/Sao_Paulo")),
}


def criar_scheduler(repo: TenantRepository, http: httpx.AsyncClient, settings: Settings) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="America/Sao_Paulo")
    for nome, (fn, gatilho) in JOBS.items():
        sched.add_job(
            executar_para_todos, gatilho, args=[nome, fn, repo, http, settings],
            id=nome, max_instances=1, coalesce=True, misfire_grace_time=300, replace_existing=True,
        )
    return sched
