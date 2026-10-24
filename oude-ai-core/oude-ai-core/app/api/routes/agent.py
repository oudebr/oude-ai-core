"""
Rotas da tela "Meu Agente de IA" do CRM: /v1/tenants/{org_id}/agent/*

Mesma autenticação das demais rotas do lojista (Bearer assinado pelo backend do CRM; papel real lido de org_membros).
Respostas NUNCA incluem chaves nem credenciais. Tudo que o lojista envia é validado e saneado em
app/services/agente_config.py antes de ser gravado.
"""
import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.api.authz import autorizar
from app.api.deps import get_repo, get_settings_dep
from app.core.auth import Principal
from app.core.providers import INTEGRACOES
from app.services.agente_config import (
    DOUTRINA_PADRAO, IDENTIDADE_PADRAO, PERMISSOES_PADRAO, ConfigAgenteInvalida, calcular_status, com_padroes,
    validar_agente, validar_autonomia, validar_doutrina, validar_identidade,
)
from app.services.context import carregar_tenant
from app.services.llm.prompt import TONS

router = APIRouter(prefix="/v1/tenants/{org_id}/agent", tags=["agente"])

SECOES = ("agente", "identidade", "doutrina", "autonomia")


async def estado_do_agente(repo, settings, org_id: UUID) -> dict[str, Any]:
    cfg, ident, dout, perm, estado, tenant = await asyncio.gather(
        repo.get_config(org_id), repo.get_identidade(org_id), repo.get_doutrina(org_id), repo.get_autonomia(org_id),
        repo.estado_integracoes(org_id), carregar_tenant(repo, settings, org_id),
    )
    identidade = com_padroes(ident, IDENTIDADE_PADRAO)
    return {
        "status": calcular_status(cfg, tenant.tem("llm"), identidade, estado),
        "agente": {
            "ativo": bool((cfg or {}).get("ativo", False)),
            "nome_agente": (cfg or {}).get("nome_agente", "Assistente"),
            "tom": (cfg or {}).get("tom", "cordial"),
            "instrucoes": (cfg or {}).get("instrucoes", ""),
        },
        "identidade": identidade,
        "doutrina": com_padroes(dout, DOUTRINA_PADRAO),
        "autonomia": com_padroes(perm, PERMISSOES_PADRAO),
        "tons_disponiveis": list(TONS),
        "integracoes": {x: tenant.tem(x) for x in INTEGRACOES},
    }


@router.get("/config")
async def ler_config(
    org_id: UUID, p: Principal = Depends(autorizar("agente.ler")), repo=Depends(get_repo), settings=Depends(get_settings_dep)
) -> dict:
    return await estado_do_agente(repo, settings, org_id)


@router.get("/status")
async def ler_status(
    org_id: UUID, p: Principal = Depends(autorizar("agente.ler")), repo=Depends(get_repo), settings=Depends(get_settings_dep)
) -> dict:
    return (await estado_do_agente(repo, settings, org_id))["status"]


@router.post("/config")
async def salvar_config(
    org_id: UUID, corpo: dict[str, Any] = Body(...), p: Principal = Depends(autorizar("agente.escrever")),
    repo=Depends(get_repo), settings=Depends(get_settings_dep),
) -> dict:
    """Salva uma ou mais seções: {"agente": {...}, "identidade": {...}, "doutrina": {...}, "autonomia": {...}}.
    'ativo' NÃO passa por aqui (use /agente/ativar e /agente/pausar, que validam as integrações)."""
    desconhecidas = set(corpo) - set(SECOES)
    if desconhecidas:
        raise HTTPException(422, f"Seção(ões) desconhecida(s): {sorted(desconhecidas)}.")
    if not any(isinstance(corpo.get(s), dict) for s in SECOES):
        raise HTTPException(422, "Nada para salvar.")
    try:  # valida TUDO antes de gravar qualquer coisa
        pronto = {s: fn(corpo[s]) for s, fn in (("agente", validar_agente), ("identidade", validar_identidade),
                                                ("doutrina", validar_doutrina), ("autonomia", validar_autonomia))
                  if isinstance(corpo.get(s), dict)}
    except ConfigAgenteInvalida as exc:
        raise HTTPException(422, str(exc)) from exc
    gravadores = {"agente": repo.salvar_config, "identidade": repo.salvar_identidade,
                  "doutrina": repo.salvar_doutrina, "autonomia": repo.salvar_autonomia}
    for secao, campos in pronto.items():
        if campos:
            await gravadores[secao](org_id, campos, p.user_id)
            await repo.auditar(org_id, p.user_id, f"agente_{secao}", ",".join(sorted(campos)))
    return await estado_do_agente(repo, settings, org_id)


@router.get("/logs")
async def ler_logs(
    org_id: UUID, limite: int = Query(50, ge=1, le=200), p: Principal = Depends(autorizar("agente.logs")), repo=Depends(get_repo)
) -> dict:
    """Histórico: ferramentas/testes/erros do agente + alterações feitas no painel (auditoria), do mais novo ao mais antigo."""
    logs, audit = await asyncio.gather(repo.listar_logs(org_id, limite), repo.listar_auditoria(org_id, limite))
    itens = [{"origem": "agente", "tipo": x["tipo"], "titulo": x.get("ferramenta") or x["tipo"], "ok": x["ok"],
              "detalhe": x["detalhe"], "quando": x["created_at"]} for x in logs]
    itens += [{"origem": "painel", "tipo": "auditoria", "titulo": x["acao"], "ok": True, "detalhe": x["detalhe"],
               "quando": x["created_at"]} for x in audit]
    itens.sort(key=lambda i: str(i["quando"]), reverse=True)
    return {"itens": itens[:limite]}
