from pydantic import BaseModel, Field


class IncidentResponse(BaseModel):
    """Карточка прогноза задержки ТС."""

    tr_id: str
    target_stop_id: str
    target_stop_name: str = "Остановка"
    predicted_delay_sec: float = Field(
        ..., description="Ожидаемое опоздание в секундах"
    )
    delay_probability: float = Field(..., description="Вероятность задержки")
    risk_level: str = Field(
        ..., description="Цветовая индикация: GREEN / YELLOW / RED"
    )
    predicted_cause: str = Field(
        ..., description="Выявленный паттерн/причина сбоя"
    )
    forecast_horizon_min: float = Field(
        ..., description="Горизонт предупреждения в минутах"
    )