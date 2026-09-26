"""Онлайн-контур: телеметрия -> состояние ТС -> признаки на T -> ML -> риск, причина -> инциденты.

Один владелец состояния (этот модуль), всё в памяти процесса. Источники телеметрии —
``POST /stream/telemetry`` (NDTP-парсер) и CSV replay (``replay.py``); оба вызывают ``ingest``.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import math
import time
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

from app.api.websocket.dashboard_ws import manager
from app.core.config import settings
from app.schemas.telemetry import RawNDTPRecord
from app.services.feature_extractor import Arrival, FeatureExtractor, finite, rule_features
from app.services.incident_rules import REASONS, THRESHOLDS, diagnose, evidence, severity_of
from app.services.incidents import IncidentTracker
from app.services.ml_client import ml_client
from app.services.network import NetworkStore, naive_to_epoch, to_dt, tr_out
from route_matching import Catalog, MatchState, StreamingSpatialAdapter

log = logging.getLogger(__name__)
OFFSET = settings.TZ_OFFSET_HOURS * 3600


def _r(x, nd=1):
    x = finite(x)
    return None if x is None else round(float(x), nd)


class Evaluator:
    """Сверка прогнозов с фактом (только replay). Факт раскрывается, когда его время наступило."""

    def __init__(self, facts: dict[int, float]):
        self.facts = facts
        self.pending: list = []
        self.seq = itertools.count()
        self.verified: deque = deque(maxlen=20000)  # все сверенные прогнозы (для аналитики и журнала)
        self.sum_err = self.sum_base = 0.0
        self.n = self.n5 = 0
        self.det_err = 0.0
        self.det_n = 0

    def register(self, p: dict) -> None:
        fact = self.facts.get(p["target_stop_id"])
        if fact is None:
            return
        heapq.heappush(self.pending, (max(fact, p["as_of"]), next(self.seq), p, fact - p["target_time_begin"]))

    def advance(self, now: float, incidents: IncidentTracker) -> None:
        while self.pending and self.pending[0][0] <= now:
            _, _, p, outcome = heapq.heappop(self.pending)
            self.n += 1
            self.sum_err += abs(p["prediction_delay_s"] - outcome)
            self.sum_base += abs((p["cur_dev_s"] or 0.0) - outcome)
            incidents.set_outcome(p["tr_id"], p["target_stop_id"], outcome)
            grid5 = int(p["as_of"]) % 300 == 0  # 5-минутная сетка, как у организаторов
            self.n5 += grid5
            self.verified.append({**p, "outcome_delay_s": outcome, "grid5": grid5})

    def on_arrival(self, visit_id: int, a: Arrival) -> None:
        fact = self.facts.get(visit_id)
        if fact is not None:
            self.det_err += abs(a.t - fact)
            self.det_n += 1


class Pipeline:
    def __init__(self):
        self.network = NetworkStore()
        self.features = FeatureExtractor(self.network)
        self.incidents = IncidentTracker()
        self.evaluator: Evaluator | None = None
        self.replay = None  # ReplayFeeder, если идёт воспроизведение
        self.spatial: StreamingSpatialAdapter | None = None
        self.spatial_states: dict[str, MatchState] = {}
        self.spatial_error: str | None = None
        self.reset()

    def reset(self) -> None:
        self.features.reset()
        self.incidents.reset()
        if self.spatial is not None:
            self.spatial.reset()
        self.spatial_states.clear()
        self.predictions: dict[str, dict] = {}
        self.next_pred: dict[str, float] = {}
        self.event_times: deque = deque()
        self.latencies: deque = deque(maxlen=500)
        self.last_packet_wall: float | None = None
        self.last_event_t: float | None = None
        self.reconnects = 0
        if self.evaluator:
            self.evaluator = Evaluator(self.evaluator.facts)

    def load_spatial(self) -> None:
        path_value = settings.ROUTE_CATALOG_PATH
        if not path_value:
            log.info("Map matching отключён: ROUTE_CATALOG_PATH не задан")
            return
        path = Path(path_value)
        if not path.is_file():
            self.spatial_error = f"catalog_not_found: {path}"
            log.warning("Map matching отключён: нет каталога %s", path)
            return
        try:
            self.spatial = StreamingSpatialAdapter(Catalog.load(path), graph=None)
            self.spatial_error = None
            log.info("Map matching: каталог %s загружен", self.spatial.catalog.version)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.spatial = None
            self.spatial_error = f"{type(exc).__name__}: {exc}"
            log.exception("Map matching отключён: каталог не загрузился")

    def close(self) -> None:
        if self.spatial is not None:
            self.spatial.close()
            self.spatial = None

    def load_facts(self, split: str) -> None:
        """Факты прибытий — только для сверки в replay, в признаки не попадают."""
        path = Path(settings.DATA_DIR) / split / "schedule.csv"
        if not settings.REPLAY_EVALUATE or not path.exists():
            return
        df = pd.read_csv(path, usecols=["tt_action_item_id", "time_fact_begin"]).dropna()
        facts = dict(zip(df["tt_action_item_id"].astype(np.int64).tolist(), naive_to_epoch(df["time_fact_begin"]).tolist()))
        self.evaluator = Evaluator(facts)

    # ------------------------------------------------------------------ часы и статус

    @property
    def mode(self) -> str:
        return "replay" if self.replay and self.replay.active else "live"

    def now(self) -> float:
        return self.replay.clock() if self.mode == "replay" else time.time()

    def ingest_status(self) -> str:
        if self.mode == "replay":
            return "down" if self.replay.link_down else "ok"
        if self.last_packet_wall is None or time.time() - self.last_packet_wall > settings.INGEST_DOWN_AFTER_SEC:
            return "down"
        return "ok"

    # ------------------------------------------------------------------ обработка потока

    async def ingest(self, rec: RawNDTPRecord) -> None:
        t = float(rec.timestamp)
        self.last_packet_wall = time.time()
        self.last_event_t = t if self.last_event_t is None else max(self.last_event_t, t)
        self.event_times.append(t)
        while self.event_times and self.event_times[0] < self.last_event_t - 60:
            self.event_times.popleft()

        arrival = self.features.ingest(rec)
        tr = rec.tr_id
        if self.spatial is not None:
            try:
                self.spatial_states[tr] = self.spatial.update(rec)
                self.spatial_error = self.spatial.last_error
            except (KeyError, ValueError, RuntimeError) as exc:
                self.spatial_error = f"{type(exc).__name__}: {exc}"
                log.warning("Map matching update failed for tr_id=%s: %s", tr, exc)
        plan = self.network.plans.get(tr)
        if arrival and self.evaluator and plan is not None:
            self.evaluator.on_arrival(int(plan.visit_id[arrival.idx]), arrival)
        if plan is None:
            return
        step = settings.PREDICT_EVERY_SEC
        T = math.floor(t / step) * step
        if T >= self.next_pred.get(tr, -math.inf):
            self.next_pred[tr] = T + step
            await self.predict(tr, T)

    async def predict(self, tr: str, T: float) -> None:
        t0 = time.perf_counter()
        res = self.features.extract_features(tr, T)
        if res is None:  # цели в окне нет — ТС не на линии
            self.predictions.pop(tr, None)
            self.incidents.drop(tr, T)
            return
        f, ctx = res
        ml = await ml_client.predict(f)
        pred = float(ml.prediction_delay_s)
        sev = severity_of(pred)
        rf = rule_features(f)
        code, detail = diagnose(rf, pred, sev)
        plan = self.network.plans[tr]
        i, j = ctx["from_idx"], ctx["target_idx"]
        name = self.network.stop_name
        elapsed = time.perf_counter() - t0
        p = {
            "sample_id": f"{tr}_{int(T + OFFSET)}", "tr_id": tr, "route_id": self.network.route_id(tr),
            "as_of": T, "generated_at": T + elapsed,
            "target_stop_id": int(plan.visit_id[j]), "target_stop_name": name(plan.stop[j]),
            "target_time_begin": float(plan.plan[j]), "horizon_s": float(plan.plan[j] - T),
            "prediction_delay_s": round(pred, 1), "predicted_arrival": float(plan.plan[j] + pred),
            "late_probability": _r(ml.late_probability, 3),
            "cur_dev_s": _r(rf["cur_dev_s"], 0), "cur_dev_source": "detector" if f.current_delay_known else "none",
            "severity": sev, "status": ml.status, "model_version": ml.model_version, "data_age_s": _r(f.gps_age_s, 0),
            "segment": {"from_stop_id": int(plan.visit_id[i]), "from_stop_name": name(plan.stop[i]),
                        "to_stop_id": int(plan.visit_id[j]), "to_stop_name": name(plan.stop[j])},
            "reason": {"code": code, "title": REASONS[code]["title"], "detail": detail} if code else None,
            "evidence": [{**e, "value": _r(e["value"]), "norm": _r(e["norm"])} for e in evidence(rf, code)],
            "recommendation": REASONS[code]["recommendation"] if code else None,
        }
        self.predictions[tr] = p
        self.incidents.update(tr, p)
        if self.evaluator:
            self.evaluator.register(p)
        self.latencies.append((time.perf_counter() - t0) * 1000)
        if manager.active_connections:
            await manager.broadcast({"type": "prediction", "prediction": self._pred_json(p)})

    # ------------------------------------------------------------------ ответы API

    def _visible_predictions(self, now: float) -> list[dict]:
        horizon = 3 * settings.PREDICT_EVERY_SEC
        down = self.ingest_status() == "down"
        out = []
        for tr, p in list(self.predictions.items()):
            if p["as_of"] <= now - horizon and not down:
                self.predictions.pop(tr, None)
                self.incidents.drop(tr, now)
                continue
            if down:
                p = {**p, "status": "stale", "data_age_s": (p["data_age_s"] or 0) + max(0.0, now - p["as_of"])}
            out.append(p)
        return out

    @staticmethod
    def _pred_out(p: dict) -> dict:
        return {**p, "tr_id": tr_out(p["tr_id"]), "as_of": to_dt(p["as_of"]), "generated_at": to_dt(p["generated_at"]),
                "target_time_begin": to_dt(p["target_time_begin"]), "predicted_arrival": to_dt(p["predicted_arrival"])}

    def _pred_json(self, p: dict) -> dict:
        from app.schemas.dashboard import Prediction
        return Prediction(**self._pred_out(p)).model_dump(mode="json")

    def predictions_out(self) -> list[dict]:
        return [self._pred_out(p) for p in self._visible_predictions(self.now())]

    def incidents_out(self) -> list[dict]:
        now = self.now()
        self._visible_predictions(now)
        stale = self.ingest_status() == "down"
        out = []
        for ep in self.incidents.visible(now):
            cur, alert = ep.current, ep.alert
            active = ep.closed_at is None
            out.append({
                "incident_id": ep.incident_id, "tr_id": tr_out(ep.tr_id), "route_id": cur["route_id"], "kind": ep.kind,
                "status": "active" if active else "resolved",
                "first_detected_at": to_dt(ep.opened_at), "updated_at": to_dt(cur["as_of"]), "closed_at": to_dt(ep.closed_at),
                "severity": cur["severity"] if active else "ok", "peak_severity": ep.peak,
                "target_stop_id": cur["target_stop_id"], "target_stop_name": cur["target_stop_name"],
                "target_time_begin": to_dt(cur["target_time_begin"]), "horizon_s": cur["horizon_s"],
                "prediction_delay_s": cur["prediction_delay_s"], "predicted_arrival": to_dt(cur["predicted_arrival"]),
                "late_probability": cur["late_probability"], "segment": cur["segment"],
                "prediction_status": "stale" if stale and active else cur["status"],
                "suspected_reason": alert["reason"], "evidence": alert["evidence"], "recommendation": alert["recommendation"],
                "alert_prediction_delay_s": alert["prediction_delay_s"], "alert_target_stop_name": alert["target_stop_name"],
                "alert_target_time_begin": to_dt(alert["target_time_begin"]), "outcome_delay_s": ep.outcome_delay_s,
            })
        return out

    def vehicles_out(self) -> list[dict]:
        now = self.now()
        out = []
        for tr, track in self.features.tracks.items():
            last = track.last()
            if not last:
                continue
            age = max(0.0, now - last[0])
            if age > 3600:
                continue
            spatial = self.spatial_states.get(tr)
            matched = spatial is not None and spatial.max_event_time == last[0]
            lat = (
                spatial.matched_lat
                if matched and spatial.matched_lat is not None
                else spatial.lat if matched else None
            )
            lon = (
                spatial.matched_lon
                if matched and spatial.matched_lon is not None
                else spatial.lon if matched else None
            )
            out.append({
                "tr_id": tr_out(tr), "unit_id": track.unit_id, "route_id": self.network.route_id(tr),
                "event_time": to_dt(last[0]), "lat": lat if lat is not None else last[1],
                "lon": lon if lon is not None else last[2], "speed": last[3], "heading": last[4],
                "location_valid": True, "data_age_s": round(age, 1),
                "status": "live" if age <= 60 else "stale" if age <= 300 else "offline",
                "source": "replay" if self.mode == "replay" else "ndtp",
                "route_pattern_id": spatial.route_pattern_id if matched else None,
                "position_quality": spatial.position_quality if matched else "raw",
                "off_route": spatial.off_route if matched else None,
            })
        return out

    def schedule_out(self, tr: str) -> dict | None:
        plan = self.network.plans.get(tr)
        if plan is None:
            return None
        now = self.now()
        tracker = self.features.trackers.get(tr)
        facts = {a.idx: a.t for a in tracker.arrivals if a.t <= now} if tracker else {}
        return {"tr_id": tr_out(tr), "visits": [
            {"visit_id": int(plan.visit_id[k]), "stop_key": int(plan.stop[k]), "time_plan": to_dt(plan.plan[k]),
             "time_fact": to_dt(facts.get(k))}
            for k in range(len(plan.plan))]}

    def verified_out(self, limit: int, all_: bool = False) -> list[dict]:
        """Сверенные с фактом прогнозы, новые сверху; по умолчанию — только 5-минутная сетка."""
        if not self.evaluator:
            return []
        rows = [p for p in self.evaluator.verified if all_ or p["grid5"]][-limit:][::-1]
        return [{"sample_id": p["sample_id"], "as_of": to_dt(p["as_of"]), "tr_id": tr_out(p["tr_id"]),
                 "route_id": p["route_id"], "target_time_begin": to_dt(p["target_time_begin"]),
                 "target_stop_name": p["target_stop_name"], "prediction_delay_s": p["prediction_delay_s"],
                 "outcome_delay_s": p["outcome_delay_s"], "cur_dev_s": p["cur_dev_s"],
                 "late_probability": p["late_probability"], "status": p["status"], "severity": p["severity"],
                 "reason_title": p["reason"]["title"] if p["reason"] else None} for p in rows]

    def metrics_out(self) -> dict:
        now = self.now()
        vehicles = self.vehicles_out()
        lat = sorted(self.latencies)
        ev = self.evaluator
        lag = 0.0
        if self.mode == "replay" and self.last_event_t is not None and not self.replay.link_down:
            lag = max(0.0, now - self.last_event_t)
        return {
            "now": to_dt(now), "mode": self.mode, "source": "replay" if self.mode == "replay" else "ndtp",
            "ingest_status": self.ingest_status(),
            "model_version": ml_client.model_version or ml_client.FALLBACK_VERSION, "ml_status": ml_client.status,
            "vehicles_live": sum(v["status"] == "live" for v in vehicles),
            "packets_per_min": 0 if self.ingest_status() == "down" else sum(1 for t in self.event_times if t > now - 60),
            "last_packet_at": to_dt(self.last_event_t),
            "inference_latency_ms_p50": round(lat[len(lat) // 2], 2) if lat else None,
            "inference_latency_ms_p95": round(lat[int(len(lat) * 0.95)], 2) if lat else None,
            "queue_lag_s": round(lag, 1),
            "reconnects": self.reconnects,
            "mae_live_s": round(ev.sum_err / ev.n, 1) if ev and ev.n else None,
            "mae_baseline_live_s": round(ev.sum_base / ev.n, 1) if ev and ev.n else None,
            "n_verified": ev.n if ev else 0, "n_verified_grid5": ev.n5 if ev else 0,
            "arrival_detector_mae_s": round(ev.det_err / ev.det_n, 1) if ev and ev.det_n else None,
            "offline_eval": None,
            "replay_speed": self.replay.speed if self.mode == "replay" else None,
        }

    def config_out(self) -> dict:
        return {"thresholds": THRESHOLDS, "horizon_s": [settings.WINDOW_MIN_SEC, settings.WINDOW_MAX_SEC],
                "predict_every_s": settings.PREDICT_EVERY_SEC,
                "model": {"version": ml_client.model_version or ml_client.FALLBACK_VERSION, "status": ml_client.status,
                          "ml_service_url": settings.ML_SERVICE_URL, "last_error": ml_client.last_error},
                "replay": self._replay_info()}

    def _replay_info(self) -> dict | None:
        r = self.replay
        if r is None or r.t is None:
            return None
        start, end = r.day_bounds
        return {"active": r.active, "speed": r.speed, "day_start": to_dt(start), "day_end": to_dt(end),
                "link_down": r.link_down}


pipeline = Pipeline()
