"""
Sincronização com o fornecedor do lojista. A URL vem do lojista, então SEMPRE passa por
`validar_url_externa` (anti-SSRF). O contrato da API do fornecedor ainda precisa ser definido
(formato de catálogo/preços): este módulo é o ponto de extensão.
"""
from typing import Any

import httpx

from app.core.net import validar_url_externa
from app.core.tenant import TenantContext


async def buscar_catalogo(http: httpx.AsyncClient, tenant: TenantContext) -> list[dict[str, Any]]:
    cred = tenant.cred("supplier")
    url = await validar_url_externa(str(cred.get("catalog_url", "")))
    headers = {"Authorization": f"Bearer {cred['api_key']}"} if cred.get("api_key") else {}
    r = await http.get(url, headers=headers, follow_redirects=False)  # sem seguir redirect (anti-SSRF)
    r.raise_for_status()
    dados = r.json()
    return dados if isinstance(dados, list) else []
