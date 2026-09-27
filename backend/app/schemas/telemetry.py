from pydantic import BaseModel, Field


class RawNDTPRecord(BaseModel):
    """Одна точка телеметрии после разбора NDTP или из воспроизведения CSV."""

    tr_id: str = Field(..., description="Идентификатор ТС (vehicle_id / tr_id)")
    timestamp: int = Field(
        ..., description="Время замера (event_time), Unix epoch, секунды"
    )
    lat: float | None = Field(None, description="Широта, пусто при невалидных координатах")
    lon: float | None = Field(None, description="Долгота")
    speed: float | None = Field(None, description="Текущая скорость км/ч")
    heading: float | None = Field(None, description="Курс, градусы")
    location_valid: bool = Field(
        True, description="Флаг достоверности координат (extraDopBit7)"
    )
    unit_id: int | None = Field(None, description="ID бортового терминала")
    doors_open: bool = Field(False, description="Статус открытия дверей")
    route_id: str | None = Field(None, description="Идентификатор маршрута")
    source: str = Field("ndtp", description="ndtp | replay")


class MLFeaturesPayload(BaseModel):
    """Признаки для ML-сервиса на момент прогноза. Все посчитаны по данным не позже t_timestamp."""

    tr_id: str
    t_timestamp: int = Field(..., description="Момент прогноза T, epoch")
    target_stop_id: str = Field(
        ..., description="Плановое посещение: первое в окне (T+10, T+15] мин"
    )
    planned_arrival_time: int = Field(
        ..., description="Плановое прибытие на цель, epoch"
    )
    current_delay_sec: float = Field(
        ..., description="Текущее отклонение по детектору прибытий (0, если неизвестно)"
    )
    current_speed_kmh: float
    avg_speed_segment: float = Field(..., description="Средняя скорость за 5 мин")
    dwell_time_sec: float = Field(..., description="Наблюдаемый непрерывный простой")

    horizon_s: float = 0.0
    current_delay_known: bool = Field(
        False, description="Было ли подтверждённое прибытие"
    )
    current_delay_age_s: float | None = None
    delay_trend_15m_s: float | None = None
    speed_15m_kmh: float | None = None
    speed_norm_kmh: float | None = None
    gps_age_s: float | None = None
    lat: float | None = None
    lon: float | None = None
    heading: float | None = None
    target_lat: float | None = None
    target_lon: float | None = None
    distance_to_target_m: float | None = None
    required_speed_kmh: float | None = None
    near_stop_m: float | None = None
    remaining_visits: int | None = None
    hour: float | None = None

    # ниже полный набор из 24 признаков модели, см. manifest.json в ml_models
    cur_dev_s: float | None = Field(
        None, description="Алиас current_delay_sec в именах модели"
    )
    last_heading: float | None = Field(
        None, description="Алиас heading в именах модели"
    )
    last_speed: float | None = None
    last_lon: float | None = None
    last_lat: float | None = None
    cur_dev_abs_s: float | None = None
    time_sin: float | None = None
    time_cos: float | None = None
    has_history: bool | None = None
    has_valid_gps: bool | None = None
    packet_age_s: float | None = None
    geo_lon_cell: float | None = None
    geo_lat_cell: float | None = None
    previous_stop_lon: float | None = None
    previous_stop_lat: float | None = None
    target_leg_planned_s: float | None = None
    schedule_progress: float | None = None
