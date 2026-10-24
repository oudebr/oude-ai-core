"""
Webhooks de pagamento (um por provedor, credenciais do LOJISTA da organização da URL):
  POST /webhooks/pagamentos/{provedor}/{org_id}/{segredo}
  POST /webhooks/infinitepay/{org_id}/{segredo}        (compatibilidade com a rota anterior)
Segurança: (1) segredo por organização/provedor na URL (comparação em tempo constante; mesma resposta 404 para
"org inexistente" e "segredo errado"); (2) o conteúdo NUNCA confirma pagamento: só a consulta servidor-a-servidor
ao provedor, com a credencial salva da organização (services/payments/confirmacao.py).
Resposta: 200 = processado/ignorado; 400 = falha temporária (o provedor tenta de novo).
"""
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_http, get_repo, get_settings_dep
from app.services.context import carregar_tenant
from app.services.payments.confirmacao import ErroTemporario, confirmar_pagamento
from app.services.payments.registry import provedor_de
from app.services.payments.webhook_auth import webhook_autorizado

router = APIRouter(tags=["webhooks"])
MAX_BYTES = 20_000


async def _processar(provedor_nome: str, org_id: UUID, segredo: str, request: Request, repo, http, settings) -> dict:
    corpo = await request.body()
    if len(corpo) > MAX_BYTES:
        raise HTTPException(413, "Corpo grande demais.")
    provedor = provedor_de(provedor_nome)
    t = await carregar_tenant(repo, settings, org_id)
    cred = t.credenciais.get(provedor_nome)
    if not webhook_autorizado(provedor, cred, segredo):
        raise HTTPException(404, "Não encontrado.")
    try:
        payload = json.loads(corpo)
    except ValueError as exc:
        raise HTTPException(400, "JSON inválido.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON inválido.")
    try:
        r = await confirmar_pagamento(repo, provedor, http, cred, org_id, payload)
    except ErroTemporario as exc:
        raise HTTPException(400, "Falha temporária ao confirmar o pagamento.") from exc
    return {"status": r.status}


@router.post("/webhooks/pagamentos/{provedor}/{org_id}/{segredo}")
async def webhook_pagamento(
    provedor: str, org_id: UUID, segredo: str, request: Request, repo=Depends(get_repo), http=Depends(get_http),
    settings=Depends(get_settings_dep),
) -> dict:
    return await _processar(provedor, org_id, segredo, request, repo, http, settings)


@router.post("/webhooks/infinitepay/{org_id}/{segredo}")
async def webhook_infinitepay(
    org_id: UUID, segredo: str, request: Request, repo=Depends(get_repo), http=Depends(get_http),
    settings=Depends(get_settings_dep),
) -> dict:
    return await _processar("infinitepay", org_id, segredo, request, repo, http, settings)
