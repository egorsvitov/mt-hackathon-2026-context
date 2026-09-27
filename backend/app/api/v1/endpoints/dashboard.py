"""API диспетчерского дашборда. Формат ответов описан в dashboard/CONTRACT.md."""

from fastapi import APIRouter, HTTPException, Query

from app.schemas.dashboard import (
    Config,
    Metrics,
    Network,
    Prediction,
    Schedule,
    Vehicle,
    Verified,
)
from app.services.pipeline import pipeline

router = APIRouter()


@router.get(
    "/network", response_model=Network, response_model_by_alias=True, tags=["dashboard"]
)
async def get_network():
    """Остановки и геометрия маршрутов. Дашборд загружает их один раз при старте."""
    return pipeline.network.network_payload()


@router.get("/schedule", response_model=Schedule, tags=["dashboard"])
async def get_schedule(tr_id: str = Query(..., description="ID ТС")):
    """Плановые посещения ТС. В time_fact прибытия, которые уже засёк детектор."""
    out = pipeline.schedule_out(tr_id)
    if out is None:
        raise HTTPException(404, f"Нет расписания для ТС {tr_id}")
    return out


@router.get("/vehicles", response_model=list[Vehicle], tags=["dashboard"])
async def get_vehicles():
    """Последнее положение каждого ТС и возраст данных."""
    return pipeline.vehicles_out()


@router.get("/predictions", response_model=list[Prediction], tags=["dashboard"])
async def get_predictions():
    """Последний прогноз по каждому ТС на линии: остановка через 10-15 минут, риск и причина."""
    return pipeline.predictions_out()


@router.get("/predictions/verified", response_model=list[Verified], tags=["dashboard"])
async def get_verified(
    limit: int = Query(80, ge=1, le=20000),
    all: bool = Query(
        False, description="true: все прогнозы, иначе только сетка 5 минут"
    ),
):
    """Прогнозы, сверенные с фактом прибытия, новые первыми."""
    return pipeline.verified_out(limit, all)


@router.get("/metrics", response_model=Metrics, tags=["dashboard"])
async def get_metrics():
    """Состояние потока, задержка обработки и точность прогнозов по факту."""
    return pipeline.metrics_out()


@router.get("/config", response_model=Config, tags=["dashboard"])
async def get_config():
    """Пороги уровня риска, горизонт прогноза, версия и статус модели."""
    return pipeline.config_out()


@router.post("/demo/start", tags=["demo"])
async def demo_start(
    speed: float = Query(1.0, ge=0, le=600),
    t: str | None = Query(None, description="Время старта, ЧЧ:ММ"),
):
    """Запускает воспроизведение исторического дня заново с указанного времени."""
    replay = pipeline.replay
    await replay.start(speed, t)
    return {
        "status": "running",
        "speed": replay.speed,
        "now": pipeline.metrics_out()["now"],
    }


@router.post("/demo/speed", tags=["demo"])
async def demo_speed(speed: float = Query(..., ge=0, le=600, description="0 ставит на паузу")):
    """Меняет скорость воспроизведения, состояние не сбрасывается."""
    if not pipeline.replay.active:
        raise HTTPException(409, "Воспроизведение не запущено")
    pipeline.replay.set_speed(speed)
    return {"speed": pipeline.replay.speed}


@router.post("/demo/stop", tags=["demo"])
async def demo_stop():
    """Останавливает воспроизведение. Часы замирают, дашборд показывает последнее состояние как устаревшее."""
    await pipeline.replay.stop()
    return {"status": "stopped"}


@router.post("/demo/link", tags=["demo"])
async def demo_link(
    down: bool = Query(
        ..., description="true имитирует обрыв потока, false восстанавливает"
    ),
):
    """Имитирует обрыв связи с источником телеметрии: часы идут, а точки не приходят."""
    pipeline.replay.set_link(down)
    return {"ingest_status": pipeline.ingest_status()}
