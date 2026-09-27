"""Состояние ТС и признаки для модели на момент прогноза T.

Всё считается только по отметкам, пришедшим не позже T. Текущего отклонения от графика
в NDTP нет, его оценивает детектор прибытий по GPS.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from app.core.config import settings
from app.schemas.telemetry import MLFeaturesPayload, RawNDTPRecord
from app.services.network import NetworkStore, VehiclePlan, haversine_m

HISTORY_SEC = 1200
MOVING_KMH = 3.0


class VehicleTrack:
    """Последние минуты GPS-трека одного ТС."""
    def __init__(self, unit_id: int | None = None):
        self.unit_id = unit_id
        self.points: deque = deque()  # (t, lat, lon, speed, heading)
        self.last_event_t: float | None = None  # считаем и пакеты без координат

    def add(self, t: float, lat, lon, speed, heading, valid: bool) -> bool:
        """Добавляет точку, возвращает False для точки без координат, дубля или опоздавшей."""
        self.last_event_t = (
            t if self.last_event_t is None else max(self.last_event_t, t)
        )
        if not valid or lat is None or lon is None:
            return False
        if self.points and t <= self.points[-1][0]:
            return False  # дубль или опоздавший пакет
        self.points.append((t, lat, lon, float(speed or 0.0), float(heading or 0.0)))
        while self.points and self.points[0][0] < t - HISTORY_SEC:
            self.points.popleft()
        return True

    def last(self):
        """Последняя точка трека или None."""
        return self.points[-1] if self.points else None

    def mean_speed(self, t: float, window: float) -> float | None:
        """Средняя скорость за окно window секунд до момента t."""
        v = [p[3] for p in self.points if t - window < p[0] <= t]
        return sum(v) / len(v) if v else None

    def stationary(self, t: float) -> float:
        """Сколько секунд ТС стоит на месте к моменту t, пропуск связи стоянкой не считается."""
        pts = [p for p in self.points if p[0] <= t]
        if not pts or pts[-1][3] >= MOVING_KMH:
            return 0.0
        first = pts[-1][0]
        for p in reversed(pts):
            if p[3] >= MOVING_KMH:
                break
            first = p[0]
        return pts[-1][0] - first


@dataclass
class Arrival:
    """Прибытие на плановое посещение: индекс, время и отклонение от плана в секундах."""
    idx: int
    t: float
    dev: float


class ArrivalTracker:
    """Детектор прибытий по GPS.

    Идём по плановым посещениям ТС и засчитываем прибытие, когда автобус въезжает в зону
    остановки, а время укладывается в окно от 7 минут раньше плана до 13 минут позже.
    Из нескольких подходящих посещений берём ближайшее по времени. На test ошибка против
    факта около 22 секунд.
    """

    LOOKAHEAD = 6  # смотрим на несколько посещений вперёд: остановки бывают пропущены

    def __init__(self, plan: VehiclePlan):
        self.plan = plan
        self.k: int | None = None
        self.arrivals: list[Arrival] = []
        self.inside: int | None = None

    def update(self, t: float, lat: float, lon: float) -> Arrival | None:
        """Обрабатывает новую точку, возвращает прибытие, если оно случилось."""
        p = self.plan
        n = len(p.plan)
        early, late = settings.ARRIVAL_EARLY_SEC, settings.ARRIVAL_LATE_SEC
        if self.k is None:  # первая точка, возможно посреди дня
            self.k = int(np.searchsorted(p.plan, t - late))
        while self.k < n and p.plan[self.k] < t - late:
            self.k += 1
        end = min(n, self.k + self.LOOKAHEAD)
        if self.k >= end:
            return None
        d = haversine_m(lat, lon, p.lat[self.k : end], p.lon[self.k : end])
        cands = [
            j
            for j in range(self.k, end)
            if d[j - self.k] <= settings.ARRIVAL_RADIUS_M
            and -early <= t - p.plan[j] <= late
        ]
        if not cands:
            if (
                self.inside is not None
                and haversine_m(lat, lon, p.lat[self.inside], p.lon[self.inside])
                > settings.ARRIVAL_RADIUS_M
            ):
                self.inside = None
            return None
        j = min(cands, key=lambda c: abs(t - p.plan[c]))
        # на конечной одна остановка идёт дважды подряд: пока ТС стоит в зоне,
        # второе прибытие засчитываем не раньше чем за минуту до плана
        if (
            j > 0
            and p.stop[j] == p.stop[j - 1]
            and self.inside == j - 1
            and t < p.plan[j] - 60
        ):
            return None
        a = Arrival(j, t, t - p.plan[j])
        self.arrivals.append(a)
        self.k = j + 1
        self.inside = j
        return a

    def last_before(self, t: float) -> Arrival | None:
        """Последнее прибытие не позже момента t."""
        for a in reversed(self.arrivals):
            if a.t <= t:
                return a
        return None


class FeatureExtractor:
    """Держит треки и детекторы прибытий по всем ТС и собирает признаки для модели."""
    def __init__(self, network: NetworkStore):
        self.network = network
        self.tracks: dict[str, VehicleTrack] = {}
        self.trackers: dict[str, ArrivalTracker] = {}

    def reset(self) -> None:
        """Забывает все треки и прибытия."""
        self.tracks.clear()
        self.trackers.clear()

    def ingest(self, rec: RawNDTPRecord) -> Arrival | None:
        """Добавляет точку телеметрии и возвращает прибытие, если детектор его засёк."""
        tr = rec.tr_id
        track = self.tracks.get(tr)
        if track is None:
            track = self.tracks[tr] = VehicleTrack(rec.unit_id)
        ok = track.add(
            rec.timestamp, rec.lat, rec.lon, rec.speed, rec.heading, rec.location_valid
        )
        plan = self.network.plans.get(tr)
        if not ok or plan is None:
            return None
        tracker = self.trackers.get(tr)
        if tracker is None:
            tracker = self.trackers[tr] = ArrivalTracker(plan)
        return tracker.update(rec.timestamp, rec.lat, rec.lon)

    def extract_features(
        self, tr: str, t: float
    ) -> tuple[MLFeaturesPayload, dict] | None:
        """Признаки на момент T и индексы посещений для ответа API.

        Возвращает None, если у ТС нет остановки в окне 10-15 минут.
        """
        plan = self.network.plans.get(tr)
        track = self.tracks.get(tr)
        if plan is None or track is None:
            return None
        j = plan.target(t)
        if j < 0:
            return None
        last = track.last()
        tracker = self.trackers.get(tr)
        arr = tracker.last_before(t) if tracker else None
        arr15 = tracker.last_before(t - 900) if tracker else None
        cur_dev = arr.dev if arr else None
        lat = lon = speed = heading = None
        gps_age = dist = near = None
        if last:
            _, lat, lon, speed, heading = last
            gps_age = t - last[0]
            dist = float(haversine_m(lat, lon, plan.lat[j], plan.lon[j]))
            rs = self.network.routes.get(tr, {}).get("stops") or []
            if rs:
                st = [self.network.stops[k] for k in rs if k in self.network.stops]
                near = float(
                    np.min(
                        haversine_m(
                            lat,
                            lon,
                            np.array([s["lat"] for s in st]),
                            np.array([s["lon"] for s in st]),
                        )
                    )
                )
        horizon = float(plan.plan[j] - t)
        v5 = track.mean_speed(t, 300)
        hour = ((t + settings.TZ_OFFSET_HOURS * 3600) % 86400) / 3600
        from_idx = arr.idx if arr else max(plan.last_planned(t), 0)
        # Начало участка для карты. Пока прибытий нет, берём остановку, план которой прошёл
        # больше 5 минут назад, иначе опаздывающий автобус выглядит проехавшим её.
        # Признаки модели по-прежнему считаются от from_idx.
        seg_from = arr.idx if arr else max(plan.last_planned(t - 300), 0)
        minute = (t + settings.TZ_OFFSET_HOURS * 3600) % 86400 / 60
        cur = float(cur_dev) if cur_dev is not None else 0.0
        prev = None
        if from_idx > 0 and from_idx <= j:
            prev = (plan.lat[from_idx - 1], plan.lon[from_idx - 1])
        prev_lon = prev_lat = None
        leg = prog = None
        if prev is not None:
            prev_lon, prev_lat = prev
            leg = float(plan.plan[j] - plan.plan[from_idx - 1])
            if last and dist is not None:
                leg_len = haversine_m(prev_lat, prev_lon, plan.lat[j], plan.lon[j])
                if leg_len > 1:
                    prog = float(haversine_m(prev_lat, prev_lon, lat, lon) / leg_len)
        f = MLFeaturesPayload(
            tr_id=tr,
            t_timestamp=int(t),
            target_stop_id=str(int(plan.visit_id[j])),
            planned_arrival_time=int(plan.plan[j]),
            current_delay_sec=cur,
            current_speed_kmh=float(speed or 0.0),
            avg_speed_segment=float(v5 or 0.0),
            dwell_time_sec=track.stationary(t),
            horizon_s=horizon,
            current_delay_known=cur_dev is not None,
            current_delay_age_s=(t - arr.t) if arr else None,
            delay_trend_15m_s=(cur_dev - (arr15.dev if arr15 else 0.0))
            if cur_dev is not None
            else None,
            speed_15m_kmh=track.mean_speed(t, 900),
            speed_norm_kmh=self.network.speed_norm(tr),
            gps_age_s=gps_age,
            lat=lat,
            lon=lon,
            heading=heading,
            target_lat=float(plan.lat[j]),
            target_lon=float(plan.lon[j]),
            distance_to_target_m=dist,
            required_speed_kmh=(dist / max(horizon, 60.0) * 3.6)
            if dist is not None
            else None,
            near_stop_m=near,
            remaining_visits=j - from_idx,
            hour=hour,
            cur_dev_abs_s=abs(cur),
            time_sin=math.sin(2 * math.pi * minute / 1440),
            time_cos=math.cos(2 * math.pi * minute / 1440),
            has_history=bool(track.points),
            has_valid_gps=last is not None,
            packet_age_s=(t - track.last_event_t)
            if track.last_event_t is not None
            else None,
            geo_lon_cell=math.floor(plan.lon[j] * 200) / 200
            if plan.lon[j] is not None
            else None,
            geo_lat_cell=math.floor(plan.lat[j] * 200) / 200
            if plan.lat[j] is not None
            else None,
            previous_stop_lon=prev_lon,
            previous_stop_lat=prev_lat,
            target_leg_planned_s=leg,
            schedule_progress=prog,
            last_speed=speed,
            last_lon=lon,
            last_lat=lat,
            cur_dev_s=cur,
            last_heading=heading,
        )
        return f, {"target_idx": j, "from_idx": seg_from}


def rule_features(f: MLFeaturesPayload) -> dict:
    """Признаки в тех именах, которые ждут правила причин в incident_rules."""
    return {
        "cur_dev_s": f.current_delay_sec if f.current_delay_known else None,
        "dev_trend_15m_s": f.delay_trend_15m_s,
        "speed_5m_kmh": f.avg_speed_segment if f.gps_age_s is not None else None,
        "speed_norm_kmh": f.speed_norm_kmh,
        "stationary_s": f.dwell_time_sec,
        "near_stop_m": f.near_stop_m,
        "gps_age_s": f.gps_age_s,
        "required_speed_kmh": f.required_speed_kmh,
    }


def finite(x):
    """Число, если оно конечное, иначе None."""
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else x
