import httpx
from app.core.config import settings
from app.schemas.incident import IncidentResponse
from app.schemas.telemetry import MLFeaturesPayload


class MLServiceClient:

    def __init__(self):
        self.base_url = settings.ML_SERVICE_URL
        self.timeout = settings.ML_REQUEST_TIMEOUT

    async def get_prediction(
        self, features: MLFeaturesPayload
    ) -> IncidentResponse:
        """Отправляет вектор признаков в контейнер ML и возвращает прогноз задержки."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(
                    self.base_url, json=features.model_dump()
                )
                response.raise_for_status()
                data = response.json()
                return IncidentResponse(**data)
            except (httpx.RequestError, httpx.HTTPStatusError):
                # Требование Критерия 5: сервис не падает при обрыве связи с ML
                horizon = (features.planned_arrival_time - features.t_timestamp) / 60.0
                return IncidentResponse(
                    tr_id=features.tr_id,
                    target_stop_id=features.target_stop_id,
                    target_stop_name="Неизвестно (Авторежим)",
                    predicted_delay_sec=features.current_delay_sec,
                    delay_probability=0.0,
                    risk_level="UNKNOWN",
                    predicted_cause="Связь с ML прервана: расчет по последнему отклонению",
                    forecast_horizon_min=round(horizon, 1),
                )


ml_client = MLServiceClient()