from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки backend. Любое поле можно переопределить переменной окружения или в .env."""
    PROJECT_NAME: str = "Transport Delay Predictor Backend"
    API_V1_STR: str = "/api/v1"

    DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/transport_db"
    )

    ML_SERVICE_URL: str = "http://localhost:8001/predict"
    ML_REQUEST_TIMEOUT: float = 1.5
    # после ошибки ML не дёргаем его столько секунд и считаем без модели
    ML_RETRY_AFTER_SEC: float = 15.0

    # прогноз строим на остановку, до которой от 10 до 15 минут
    WINDOW_MIN_SEC: int = 600
    WINDOW_MAX_SEC: int = 900

    DATA_DIR: str = "../../dataset"
    SCHEDULE_SPLIT: str = "test"
    NETWORK_PATH: str = "../dashboard/data/network.json"
    # без каталога map matching выключен, работаем по сырому GPS
    ROUTE_CATALOG_PATH: str | None = "../map_matching/artifacts/catalog-train.json"
    # время в датасете московское, без указания зоны
    TZ_OFFSET_HOURS: int = 3

    PREDICT_EVERY_SEC: int = 60
    ARRIVAL_RADIUS_M: float = 40.0
    # прибытие ищем от 7 минут раньше плана до 13 минут позже
    ARRIVAL_EARLY_SEC: int = 420
    ARRIVAL_LATE_SEC: int = 780
    INGEST_DOWN_AFTER_SEC: float = 15.0
    # точки, которые опережают часы системы больше чем на 5 минут, выкидываем,
    # иначе все следующие точки этого ТС считаются опоздавшими и трек замирает
    MAX_FUTURE_SKEW_SEC: float = 300.0

    REPLAY_AUTOSTART: bool = True
    REPLAY_SPLIT: str = "test"
    REPLAY_SPEED: float = 1.0
    REPLAY_START: str = "08:30"
    REPLAY_EVALUATE: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
