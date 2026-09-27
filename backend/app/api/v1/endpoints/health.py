from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.services.ml_client import ml_client
from app.services.pipeline import pipeline

router = APIRouter()


@router.get("/health")
async def health_check():
    """Короткий ответ, что backend работает."""
    return {"status": "ok", "service": "backend"}


@router.get("/health/live")
async def health_live():
    """Процесс жив."""
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready():
    """Справочные данные загружены и можно работать. Если ML недоступен, прогнозы идут без модели."""
    net = pipeline.network
    body = {
        "status": "ready" if net.loaded else "not_ready",
        "network_error": net.error,
        "routes": len(net.routes),
        "vehicles_with_plan": len(net.plans),
        "mode": pipeline.mode,
        "ingest_status": pipeline.ingest_status(),
        "ml_status": ml_client.status,
        "map_matching_status": "ready" if pipeline.spatial is not None else "disabled",
        "map_matching_error": pipeline.spatial_error,
    }
    return JSONResponse(body, status_code=200 if net.loaded else 503)
