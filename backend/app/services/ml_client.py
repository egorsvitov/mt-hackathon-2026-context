import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.core.config import settings
from app.schemas.telemetry import MLFeaturesPayload


@dataclass
class MLResult:
    prediction_delay_s: float
    late_probability: Optional[float]
    model_version: str
    status: str  # model | fallback
    latency_ms: Optional[float] = None


class MLServiceClient:
    """Клиент ML-сервиса: POST признаков -> прогноз задержки.

    Ответ ML-сервиса: ``{"prediction_delay_s": float, "late_probability": float?, "model_version": str}``.
    Для совместимости принимается и прежняя форма ``IncidentResponse``
    (``predicted_delay_sec``, ``delay_probability``).

    Требование критерия 5: при недоступности ML сервис не падает, а выдаёт fallback
    «прогноз = текущее отклонение» с явной пометкой ``status = fallback``. После ошибки ML
    не опрашивается ``ML_RETRY_AFTER_SEC`` секунд, чтобы не тормозить поток телеметрии.
    """

    FALLBACK_VERSION = "fallback:persistence"

    def __init__(self):
        self.base_url = settings.ML_SERVICE_URL
        self.timeout = settings.ML_REQUEST_TIMEOUT
        self.down_until = 0.0
        self.last_error: Optional[str] = None
        self.model_version: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def status(self) -> str:
        return "fallback" if time.monotonic() < self.down_until or self.model_version is None else "ok"

    def fallback(self, features: MLFeaturesPayload) -> MLResult:
        return MLResult(features.current_delay_sec, None, self.FALLBACK_VERSION, "fallback")

    async def predict(self, features: MLFeaturesPayload) -> MLResult:
        if time.monotonic() < self.down_until:
            return self.fallback(features)
        if self._client is None:
            # connect — коротко: недоступный ML не должен задерживать поток телеметрии
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=0.5))
        t0 = time.perf_counter()
        try:
            response = await self._client.post(self.base_url, json=features.model_dump())
            response.raise_for_status()
            data = response.json()
            pred = data.get("prediction_delay_s", data.get("predicted_delay_sec", data.get("prediction")))
            if pred is None:
                raise ValueError("в ответе ML нет prediction_delay_s")
            prob = data.get("late_probability", data.get("delay_probability"))
            self.model_version = str(data.get("model_version") or "ml-service")
            self.last_error = None
            return MLResult(float(pred), None if prob is None else float(prob), self.model_version, "model",
                            (time.perf_counter() - t0) * 1000)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            self.down_until = time.monotonic() + settings.ML_RETRY_AFTER_SEC
            self.last_error = f"{type(e).__name__}: {e}"
            return self.fallback(features)


ml_client = MLServiceClient()
