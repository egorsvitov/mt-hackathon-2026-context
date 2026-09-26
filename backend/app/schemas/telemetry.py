from typing import Optional
from pydantic import BaseModel, Field


class RawNDTPRecord(BaseModel):
    """Сырая запись телеметрии после парсинга NDTP (ячейка G6CellNav00) или из CSV replay."""

    tr_id: str = Field(
        ..., description="Идентификатор ТС (vehicle_id / tr_id)"
    )
    timestamp: int = Field(
        ..., description="Время замера (event_time), Unix epoch, секунды"
    )
    lat: Optional[float] = Field(None, description="Широта; нет — координаты невалидны")
    lon: Optional[float] = Field(None, description="Долгота")
    speed: Optional[float] = Field(None, description="Текущая скорость км/ч")
    heading: Optional[float] = Field(None, description="Курс, градусы")
    location_valid: bool = Field(True, description="Флаг достоверности координат (extraDopBit7)")
    unit_id: Optional[int] = Field(None, description="ID бортового терминала")
    doors_open: bool = Field(False, description="Статус открытия дверей")
    route_id: Optional[str] = Field(None, description="Идентификатор маршрута")
    source: str = Field("ndtp", description="ndtp | replay")


class MLFeaturesPayload(BaseModel):
    """Пакет признаков, отправляемый в ML-модуль под задачу прогноза.

    Все признаки посчитаны только по данным с event_time <= t_timestamp.
    """

    tr_id: str
    t_timestamp: int = Field(..., description="Момент прогноза T, epoch")
    target_stop_id: str = Field(..., description="Плановое посещение: первое в окне (T+10, T+15] мин")
    planned_arrival_time: int = Field(..., description="Плановое прибытие на цель, epoch")
    current_delay_sec: float = Field(..., description="Текущее отклонение по детектору прибытий (0, если неизвестно)")
    current_speed_kmh: float
    avg_speed_segment: float = Field(..., description="Средняя скорость за 5 мин")
    dwell_time_sec: float = Field(..., description="Наблюдаемый непрерывный простой")

    horizon_s: float = 0.0
    current_delay_known: bool = Field(False, description="Было ли подтверждённое прибытие")
    current_delay_age_s: Optional[float] = None
    delay_trend_15m_s: Optional[float] = None
    speed_15m_kmh: Optional[float] = None
    speed_norm_kmh: Optional[float] = None
    gps_age_s: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    heading: Optional[float] = None
    target_lat: Optional[float] = None
    target_lon: Optional[float] = None
    distance_to_target_m: Optional[float] = None
    required_speed_kmh: Optional[float] = None
    near_stop_m: Optional[float] = None
    remaining_visits: Optional[int] = None
    hour: Optional[float] = None
