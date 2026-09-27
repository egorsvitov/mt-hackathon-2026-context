import time
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.schemas.telemetry import MLFeaturesPayload


@dataclass
class MLResult:
    """Ответ модели: прогноз задержки, вероятность опоздания, версия и статус (model или fallback)."""
    prediction_delay_s: float
    late_probability: float | None
    model_version: str
    status: str
    latency_ms: float | None = None


class MLServiceClient:
    """Клиент ML-сервиса: отправляет признаки и получает прогноз задержки.

    Если сервис не отвечает, возвращаем текущее отклонение как прогноз со статусом fallback
    и не обращаемся к нему ML_RETRY_AFTER_SEC секунд.
    """

    FALLBACK_VERSION = "fallback:persistence"

    def __init__(self):
        self.base_url = settings.ML_SERVICE_URL
        self.timeout = settings.ML_REQUEST_TIMEOUT
        self.down_until = 0.0
        self.last_error: str | None = None
        self.model_version: str | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def status(self) -> str:
        """ok, если модель отвечает, иначе fallback."""
        return (
            "fallback"
            if time.monotonic() < self.down_until or self.model_version is None
            else "ok"
        )

    def fallback(self, features: MLFeaturesPayload) -> MLResult:
        """Прогноз без модели: текущее отклонение остаётся таким же."""
        return MLResult(
            features.current_delay_sec, None, self.FALLBACK_VERSION, "fallback"
        )

    async def predict(self, features: MLFeaturesPayload, model_input=None) -> MLResult:
        """Запрашивает прогноз у ML-сервиса, при любой ошибке отдаёт fallback."""
        if time.monotonic() < self.down_until:
            return self.fallback(features)
        if self._client is None:
            # короткий connect, чтобы упавший ML не тормозил приём телеметрии
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=0.5)
            )
        t0 = time.perf_counter()
        try:
            response = await self._client.post(
                self.base_url, json=model_input.model_dump() if model_input is not None else features.model_dump()
            )
            response.raise_for_status()
            data = response.json()
            pred = data.get(
                "prediction_delay_s",
                data.get("predicted_delay_sec", data.get("prediction")),
            )
            if pred is None:
                raise ValueError("в ответе ML нет prediction_delay_s")
            prob = data.get("late_probability", data.get("delay_probability"))
            self.model_version = str(data.get("model_version") or "ml-service")
            self.last_error = None
            return MLResult(
                float(pred),
                None if prob is None else float(prob),
                self.model_version,
                "model",
                (time.perf_counter() - t0) * 1000,
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            self.down_until = time.monotonic() + settings.ML_RETRY_AFTER_SEC
            self.last_error = f"{type(e).__name__}: {e}"
            return self.fallback(features)


ml_client = MLServiceClient()
