"""Alerta de estoque mínimo: WhatsApp ao responsável; sem WhatsApp, notificação no painel do CRM."""
import logging
from datetime import date

import httpx

from app.core.tenant import TenantContext
from app.db.repositories import TenantRepository
from app.services.whatsapp.client import WhatsAppIndisponivel, whatsapp_do_tenant

log = logging.getLogger(__name__)


async def alerta_estoque(repo: TenantRepository, http: httpx.AsyncClient, tenant: TenantContext) -> int:
    itens = await repo.estoque_baixo(tenant.org_id)
    if not itens:
        return 0
    wa = whatsapp_do_tenant(http, tenant)
    destino = str(tenant.cred("whatsapp").get("notify_to", "")) if wa else ""  # responsável definido pelo lojista
    enviados = 0
    for item in itens:
        chave = f"estoque:{item['id']}:{date.today().isoformat()}"  # no máx. 1 alerta por produto/dia
        if not await repo.reservar_execucao(tenant.org_id, chave):
            continue
        texto = (
            f"Estoque baixo: {item['nome']} tem {item['estoque']} un. "
            f"(mínimo {item['estoque_minimo']}). Hora de repor."
        )
        try:
            if wa and destino:
                await wa.enviar_texto(destino, texto)
            else:
                await repo.criar_notificacao(tenant.org_id, "estoque", "Estoque baixo", texto)
            enviados += 1
        except WhatsAppIndisponivel:
            await repo.liberar_execucao(tenant.org_id, chave)  # tenta de novo na próxima rodada
            try:  # e já deixa o aviso no painel, para o lojista não ficar sem saber
                await repo.criar_notificacao(tenant.org_id, "estoque", "Estoque baixo", texto)
            except Exception:  # noqa: BLE001
                log.exception("org=%s falha ao criar notificação de fallback", tenant.org_id)
    return enviados
