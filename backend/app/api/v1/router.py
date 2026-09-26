from fastapi import APIRouter
from app.api.v1.endpoints import dashboard, health, incidents, telemetry

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(
    telemetry.router, prefix="/stream", tags=["telemetry"]
)
api_router.include_router(
    incidents.router, prefix="/incidents", tags=["incidents"]
)
api_router.include_router(dashboard.router)
