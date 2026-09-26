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
    # После ошибки ML-сервиса не обращаемся к нему столько секунд: работаем на fallback,
    # чтобы недоступный ML не тормозил обработку потока.
    ML_RETRY_AFTER_SEC: float = 15.0

    # Горизонт прогнозирования в секундах: (T + 10 мин, T + 15 мин]
    WINDOW_MIN_SEC: int = 600
    WINDOW_MAX_SEC: int = 900

    # Данные. Пути — относительно рабочей папки backend (в Docker задаются абсолютными).
    DATA_DIR: str = "../../dataset"
    # Чьё плановое расписание загружать: test/train (schedule.csv) или validate (schedule_plan.csv).
    # Факты прибытий (time_fact_begin) в онлайн-контур не загружаются.
    SCHEDULE_SPLIT: str = "test"
    # Справочник сети (остановки и геометрия маршрутов), экспортируется map_matching
    NETWORK_PATH: str = "../dashboard/data/network.json"
    # Наивные метки времени датасета — московское время.
    TZ_OFFSET_HOURS: int = 3

    # Онлайн-контур
    PREDICT_EVERY_SEC: int = 60  # как часто выпускать прогноз по каждому ТС (по времени событий)
    ARRIVAL_RADIUS_M: float = 40.0  # геозона остановки для детектора прибытий
    # Прибытие ищется от 7 мин раньше плана до 13 мин позже (на test: MAE детектора 22 с против факта)
    ARRIVAL_EARLY_SEC: int = 420
    ARRIVAL_LATE_SEC: int = 780
    INGEST_DOWN_AFTER_SEC: float = 15.0  # столько секунд без пакетов — поток считается оборванным

    # Воспроизведение исторического дня (CSV replay) — пока нет живого NDTP-потока
    REPLAY_AUTOSTART: bool = True
    REPLAY_SPLIT: str = "test"
    REPLAY_SPEED: float = 1.0
    REPLAY_START: str = "08:30"  # время дня, с которого начинается воспроизведение
    # Сверка прогнозов с фактом (только replay: факт раскрывается после того, как наступил)
    REPLAY_EVALUATE: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
