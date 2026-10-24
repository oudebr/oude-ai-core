from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_repo
from app.db.repositories import TenantRepository

router = APIRouter(tags=["saúde"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(repo: TenantRepository = Depends(get_repo)) -> dict[str, str]:
    try:
        await repo.ping()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, "Banco indisponível.") from exc
    return {"status": "ready"}
