import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.api.websocket.dashboard_ws import ws_router
from app.core.config import settings
from app.services.pipeline import pipeline
from app.services.replay import ReplayFeeder

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Справочные данные: сеть маршрутов и плановое расписание.
    pipeline.network.load()
    pipeline.load_spatial()
    pipeline.replay = ReplayFeeder(pipeline)
    if settings.REPLAY_AUTOSTART and pipeline.network.loaded:
        try:
            pipeline.load_facts(settings.REPLAY_SPLIT)
            await pipeline.replay.start(settings.REPLAY_SPEED, settings.REPLAY_START)
        except Exception:
            log.exception("Replay не запущен — backend работает без потока")
    yield
    await pipeline.replay.stop()
    pipeline.close()


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url="/docs",
    description="Backend-сервис для приема телеметрии NDTP, расчета признаков и прогнозирования задержек",
    lifespan=lifespan,
)

# Разрешаем CORS для дашборда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_STR)
app.include_router(ws_router, prefix=settings.API_V1_STR)


@app.get("/")
async def root():
    return {
        "status": "online",
        "service": settings.PROJECT_NAME,
        "docs": "/docs",
    }
