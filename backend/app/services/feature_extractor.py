from typing import Any, Optional
from app.core.config import settings
from app.schemas.telemetry import MLFeaturesPayload, RawNDTPRecord


class FeatureExtractor:

    def __init__(self):
        # Хранилище последнего состояния ТС в памяти для расчета простоя и скоростей
        self.last_state: dict[str, dict[str, Any]] = {}

    def extract_features(
        self,
        record: RawNDTPRecord,
        planned_schedule: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[MLFeaturesPayload]:
        """Сопоставляет ТС с расписанием и собирает признаки строго ДО момента T."""
        t = record.timestamp
        tr_id = record.tr_id

        # 1. Поиск первой плановой остановки в окне (T + 10 мин, T + 15 мин]
        target_stop = self._find_target_stop(tr_id, t, planned_schedule)
        if not target_stop:
            return None

        # 2. Расчет времени простоя (dwell_time)
        prev = self.last_state.get(tr_id, {})
        dwell_time = 0.0
        if record.doors_open or record.speed < 1.0:
            dwell_time = prev.get("dwell_time_sec", 0.0) + max(
                0, t - prev.get("last_t", t)
            )

        # 3. Расчет текущего отклонения от графика
        current_delay_sec = float(target_stop.get("current_offset_sec", 0.0))

        # Обновляем состояние ТС
        self.last_state[tr_id] = {
            "last_t": t,
            "dwell_time_sec": dwell_time,
            "last_speed": record.speed,
        }

        return MLFeaturesPayload(
            tr_id=tr_id,
            t_timestamp=t,
            target_stop_id=target_stop["stop_id"],
            planned_arrival_time=target_stop["planned_time"],
            current_delay_sec=current_delay_sec,
            current_speed_kmh=record.speed,
            avg_speed_segment=record.speed,
            dwell_time_sec=dwell_time,
        )

    def _find_target_stop(
        self,
        tr_id: str,
        t: int,
        schedule: Optional[list[dict[str, Any]]],
    ) -> Optional[dict[str, Any]]:
        """Ищет первую плановую остановку в окне (T + WINDOW_MIN_SEC, T + WINDOW_MAX_SEC]."""
        if not schedule:
            # Дефолтный fallback, если расписание еще не подгружено
            return {
                "stop_id": "STOP_DEFAULT",
                "planned_time": t + 720,  # +12 минут
                "current_offset_sec": 45.0,
            }

        min_window = t + settings.WINDOW_MIN_SEC
        max_window = t + settings.WINDOW_MAX_SEC

        for stop in schedule:
            p_time = stop.get("planned_time", 0)
            if min_window < p_time <= max_window:
                return stop
        return None


feature_extractor = FeatureExtractor()