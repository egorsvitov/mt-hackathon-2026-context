from typing import Optional
from pydantic import BaseModel, Field


class RawNDTPRecord(BaseModel):
    """Сырая запись телеметрии после парсинга NDTP."""

    tr_id: str = Field(
        ..., description="Идентификатор ТС (vehicle_id / tr_id)"
    )
    timestamp: int = Field(
        ..., description="Время замера T в секундах от начала суток или epoch"
    )
    lat: float = Field(..., description="Широта")
    lon: float = Field(..., description="Долгота")
    speed: float = Field(..., description="Текущая скорость км/ч")
    doors_open: bool = Field(False, description="Статус открытия дверей")
    route_id: Optional[str] = Field(None, description="Идентификатор маршрута")


class MLFeaturesPayload(BaseModel):
    """Пакет признаков, отправляемый в ML-модуль под задачу прогноза."""

    tr_id: str
    t_timestamp: int
    target_stop_id: str
    planned_arrival_time: int
    current_delay_sec: float
    current_speed_kmh: float
    avg_speed_segment: float
    dwell_time_sec: float