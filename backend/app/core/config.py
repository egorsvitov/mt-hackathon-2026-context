from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Transport Delay Predictor Backend"
    API_V1_STR: str = "/api/v1"

    # PostgreSQL
    DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/transport_db"
    )

    # ML Inference Service (в docker-compose хостом будет имя контейнера ml-service)
    ML_SERVICE_URL: str = "http://localhost:8001/predict"
    ML_REQUEST_TIMEOUT: float = 1.5

    # Горизонт прогнозирования в секундах: (T + 10 мин, T + 15 мин]
    WINDOW_MIN_SEC: int = 600
    WINDOW_MAX_SEC: int = 900

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()