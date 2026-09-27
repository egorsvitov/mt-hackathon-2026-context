"""Меры диспетчера (what-if): дополнительный автобус на линии и сокращение стоянок.

**Дополнительный автобус.** Резерв берёт рейс опаздывающего ТС с остановки, к которой успевает
по графику, и идёт по его расписанию до конечной. Это настоящее ТС линии: своё ``tr_id``,
своё расписание, отметки GPS вдоль геометрии маршрута синхронно с часами системы. Отметки идут
через тот же ``pipeline.ingest``, что и поток NDTP: детектор прибытий, прогноз ML, карта и списки
видят резерв как обычный автобус. Эффект — сколько остановок впереди обслужено по графику,
а не с опозданием основного ТС.

**Сокращение стоянок.** Прогноз опоздания на остановках впереди «как есть» и «с мерой».
«Как есть»: до целевой остановки — от текущего отклонения к прогнозу модели, дальше опоздание
сохраняется; на конечной его частично гасит отстой (до обычного минимума). «С мерой» — каждая
стоянка короче на ``cut_s`` (не больше, чем позволяет обычная стоянка по данным), отстой на
конечной сокращается до ``LAYOVER_SHORT_S``. Раньше графика автобус не идёт.

Данные (test, 06.01): стоянка на остановке — медиана 14 с, p75 22 с, p90 35 с; пауза на
конечной — медиана 10 мин.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math

import numpy as np

from app.schemas.telemetry import RawNDTPRecord
from app.services.network import VehiclePlan, haversine_m, to_dt, tr_out

log = logging.getLogger(__name__)

TRIP_GAP_S = 300  # пауза в расписании длиннее — конец рейса, отстой на конечной
DWELL_TYPICAL_S = 22  # обычная стоянка на остановке (p75 по данным)
DWELL_MIN_S = 8  # короче не бывает: открыть и закрыть двери
LAYOVER_NORMAL_S = 300  # минимальный отстой на конечной в обычном режиме
LAYOVER_SHORT_S = 120  # отстой, сокращённый по указанию диспетчера
HORIZON_S = 2 * 3600  # насколько вперёд считаем остановки
GPS_STEP_S = 10  # шаг отметок резерва (как у реальных трекеров)
SIM_DWELL_S = 15  # стоянка резерва на остановке
RESERVE_VISIT_OFFSET = 7 * 10**11  # номера посещений резерва не пересекаются с плановыми
KEEP_AFTER_TRIP_S = 600  # резерв виден после конца рейса


def _visible(plan: VehiclePlan, k: int) -> bool:
    return 0 <= k < len(plan.plan)


class Reserve:
    """Дополнительный автобус: рейс базового ТС с посещения k0 до конца рейса k1."""

    def __init__(self, mid: int, tr: str, vtr: str, base: VehiclePlan, k0: int, k1: int,
                 segments: dict, created: float):
        self.id, self.tr, self.vtr = mid, tr, vtr
        self.k0, self.k1 = k0, k1
        self.created = created
        idx = np.arange(k0, k1 + 1)
        self.plan = VehiclePlan(
            tr_id=vtr, visit_id=base.visit_id[idx] + RESERVE_VISIT_OFFSET, plan=base.plan[idx].copy(),
            stop=base.stop[idx].copy(), lat=base.lat[idx].copy(), lon=base.lon[idx].copy())
        self.base_visit = {int(v) + RESERVE_VISIT_OFFSET: int(v) for v in base.visit_id[idx]}
        # Отрезки движения: (выезд, прибытие, путь [(lat, lon)], накопленная длина).
        self.legs = []
        for i in range(len(idx) - 1):
            a, b = int(self.plan.stop[i]), int(self.plan.stop[i + 1])
            path = segments.get((a, b)) or [(self.plan.lat[i], self.plan.lon[i]), (self.plan.lat[i + 1], self.plan.lon[i + 1])]
            path = [(float(p[0]), float(p[1])) for p in path]
            cum = [0.0]
            for p, q in zip(path[:-1], path[1:]):
                cum.append(cum[-1] + float(haversine_m(p[0], p[1], q[0], q[1])))
            t_arr = float(self.plan.plan[i + 1])
            t_dep = min(float(self.plan.plan[i]) + SIM_DWELL_S, t_arr - 1)
            self.legs.append((t_dep, t_arr, path, cum))
        # Резерв едет из парка и появляется на остановке выхода за полминуты до отправления:
        # если бы он «стоял» там заранее, детектор засчитал бы прибытие на минуты раньше плана.
        self.next_t = math.floor(max(created, float(self.plan.plan[0]) - 30) / GPS_STEP_S) * GPS_STEP_S
        self.end = float(self.plan.plan[-1]) + KEEP_AFTER_TRIP_S

    def position(self, t: float) -> tuple[float, float, float, float]:
        """(lat, lon, скорость км/ч, курс) резерва в момент t."""
        p = self.plan
        if t <= p.plan[0] or not self.legs:
            return float(p.lat[0]), float(p.lon[0]), 0.0, 0.0
        for t_dep, t_arr, path, cum in self.legs:
            if t < t_dep:  # стоит на остановке
                return path[0][0], path[0][1], 0.0, _bearing(path[0], path[-1])
            if t <= t_arr:
                total = cum[-1] or 1.0
                s = total * (t - t_dep) / max(1.0, t_arr - t_dep)
                j = max(0, min(len(cum) - 2, int(np.searchsorted(cum, s, side="right")) - 1))
                seg = (cum[j + 1] - cum[j]) or 1.0
                f = (s - cum[j]) / seg
                a, b = path[j], path[j + 1]
                speed = total / max(1.0, t_arr - t_dep) * 3.6
                return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, speed, _bearing(a, b)
        return float(p.lat[-1]), float(p.lon[-1]), 0.0, 0.0


def _bearing(a, b) -> float:
    k = math.cos(math.radians(a[0]))
    return math.degrees(math.atan2((b[1] - a[1]) * k, b[0] - a[0])) % 360


class WhatIf:
    def __init__(self, pipeline):
        self.p = pipeline
        self.seq = itertools.count(1)
        self.measures: dict[int, dict] = {}
        self.reserves: dict[int, Reserve] = {}
        self.task: asyncio.Task | None = None

    # ------------------------------------------------------------------ жизненный цикл

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def clear(self) -> None:
        """Сброс состояния (перезапуск воспроизведения): меры относятся к прежней линии времени."""
        for r in list(self.reserves.values()):
            self._drop_reserve(r)
        self.measures.clear()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — резерв не должен ронять backend
                log.exception("What-if: ошибка при движении резерва")
            await asyncio.sleep(1.0)

    async def tick(self) -> None:
        now = self.p.now()
        for r in list(self.reserves.values()):
            if now < r.created - 60:  # часы ушли назад (перемотка) — резерв из другой линии времени
                self.remove(r.id)
                continue
            if self.p.ingest_status() == "down":
                continue  # при обрыве связи резерв тоже «молчит»
            while r.next_t <= min(now, r.end):
                lat, lon, speed, heading = r.position(r.next_t)
                await self.p.ingest(RawNDTPRecord(
                    tr_id=r.vtr, timestamp=int(r.next_t), lat=lat, lon=lon, speed=round(speed, 1),
                    heading=round(heading), location_valid=True, source="whatif"))
                r.next_t += GPS_STEP_S
            if now > r.end:
                self.remove(r.id)

    # ------------------------------------------------------------------ состояние ТС

    def _position_idx(self, tr: str, plan: VehiclePlan, now: float) -> int:
        """Последнее пройденное посещение: по прогнозу backend, детектору прибытий или плану."""
        pred = self.p.predictions.get(tr)
        if pred:
            k = plan.pos.get(int(pred["segment"]["from_stop_id"]))
            if k is not None:
                return k
        tracker = self.p.features.trackers.get(tr)
        a = tracker.last_before(now) if tracker else None
        return a.idx if a else plan.last_planned(now - 300)

    def projection(self, tr: str, now: float, *, cut_s: float = 0, short_layover: bool = False,
                   until_k: int | None = None) -> dict | None:
        """Опоздание на остановках впереди: «как есть» и «с мерой»."""
        plan = self.p.network.plans.get(tr)
        if plan is None:
            return None
        pred = self.p.predictions.get(tr)
        last = self._position_idx(tr, plan, now)
        cur = float(pred["cur_dev_s"]) if pred and pred["cur_dev_s"] is not None else 0.0
        target = plan.pos.get(int(pred["target_stop_id"])) if pred else None
        fc = float(pred["prediction_delay_s"]) if pred else cur
        cut = max(0.0, min(float(cut_s), DWELL_TYPICAL_S - DWELL_MIN_S))
        rows = []
        d_prev = cur
        saved = 0.0  # сколько секунд мера уже вернула к этой остановке
        k = max(last + 1, 0)
        while _visible(plan, k) and (plan.plan[k] <= now + HORIZON_S or (until_k is not None and k <= until_k)):
            gap = float(plan.plan[k] - plan.plan[k - 1]) if k > 0 else 0.0
            layover = bool(gap >= TRIP_GAP_S)
            # «Как есть»: до цели — от текущего отклонения к прогнозу модели (он уже учитывает
            # обычный отстой), дальше опоздание сохраняется и частично гасится отстоем на конечной.
            if target is not None and k <= target:
                span = max(1.0, plan.plan[target] - now)
                d = cur + (fc - cur) * min(1.0, max(0.0, (plan.plan[k] - now) / span))
            elif layover:  # раньше графика в рейс не выходят
                d = max(0.0, d_prev - max(0.0, gap - LAYOVER_NORMAL_S))
            else:
                d = d_prev
            # «С мерой»: отстой короче обычного минимума на (LAYOVER_NORMAL_S − LAYOVER_SHORT_S)
            # и каждая стоянка короче на cut — пока опоздание не отыграно полностью.
            left = d - saved
            if layover and short_layover and left > 0:
                saved += min(left, LAYOVER_NORMAL_S - LAYOVER_SHORT_S)
            elif not layover and k > last + 1 and left > 0:
                saved += min(left, cut)
            s = max(0.0, d - saved) if d > 0 else d
            rows.append({"visit_id": int(plan.visit_id[k]), "stop_name": self.p.network.stop_name(plan.stop[k]),
                         "time_plan": float(plan.plan[k]), "delay_s": round(float(d), 1), "delay_measure_s": round(float(s), 1),
                         "trip_start": layover})
            d_prev = d
            k += 1
        return {"tr": tr, "last_idx": last, "target_idx": target, "cur_dev_s": cur, "forecast_s": fc, "rows": rows}

    # ------------------------------------------------------------------ дополнительный автобус

    def reserve_options(self, tr: str, ready_min: float) -> tuple[VehiclePlan, list[int]] | None:
        plan = self.p.network.plans.get(tr)
        if plan is None:
            return None
        now = self.p.now()
        last = self._position_idx(tr, plan, now)
        ready = now + ready_min * 60
        opts = [k for k in range(max(0, last + 1), len(plan.plan)) if plan.plan[k] >= ready][:40]
        return plan, opts

    def add_reserve(self, tr: str, ready_min: float, start_visit_id: int | None) -> dict:
        res = self.reserve_options(tr, ready_min)
        if res is None:
            raise KeyError(f"нет расписания ТС {tr}")
        plan, opts = res
        if not opts:
            raise ValueError("до конца расписания резерв не успевает ни на одну остановку")
        k0 = plan.pos.get(int(start_visit_id)) if start_visit_id is not None else None
        if k0 not in opts:
            k0 = opts[0]
        k1 = k0
        while k1 + 1 < len(plan.plan) and plan.plan[k1 + 1] - plan.plan[k1] < TRIP_GAP_S:
            k1 += 1
        mid = next(self.seq)
        vtr = f"7{mid:02d}{tr}"  # 701122048: «7», номер меры, линия — числом, как все tr_id
        net = self.p.network
        base_route = net.routes.get(tr) or {}
        segments = {}
        for s in base_route.get("segments", []):
            segments.setdefault((int(s["from"]), int(s["to"])), s["path"])
        now = self.p.now()
        r = Reserve(mid, tr, vtr, plan, k0, k1, segments, now)
        net.plans[vtr] = r.plan
        net.routes[vtr] = {**base_route, "tr_id": tr_out(vtr), "reserve_of": tr_out(tr),
                           "name": f"{base_route.get('name', f'Маршрут ТС {tr}')} · резерв",
                           "route_id": base_route.get("route_id", f"R{tr}")}
        self.reserves[mid] = r
        m = {"id": mid, "kind": "reserve", "tr_id": tr, "virtual_tr_id": vtr, "created_at": now,
             "ready_min": ready_min, "k0": k0, "k1": k1}
        self.measures[mid] = m
        log.info("What-if: резерв %s на линии ТС %s с посещения %d по %d", vtr, tr, k0, k1)
        return self._out(m, now)

    def _drop_reserve(self, r: Reserve) -> None:
        p, net = self.p, self.p.network
        for d in (net.plans, net.routes, p.features.tracks, p.features.trackers, p.predictions,
                  p.next_pred, p.spatial_states):
            d.pop(r.vtr, None)
        p.incidents.drop(r.vtr, p.now())
        self.reserves.pop(r.id, None)

    # ------------------------------------------------------------------ стоянки

    def add_dwell(self, tr: str, scope: str, cut_s: float, short_layover: bool) -> dict:
        if tr not in self.p.network.plans:
            raise KeyError(f"нет расписания ТС {tr}")
        mid = next(self.seq)
        m = {"id": mid, "kind": "dwell", "tr_id": tr, "scope": scope, "cut_s": cut_s,
             "short_layover": short_layover, "created_at": self.p.now()}
        self.measures[mid] = m
        return self._out(m, self.p.now())

    def line_vehicles(self, tr: str) -> list[str]:
        """ТС той же линии (тот же route_id): основной и его резервы."""
        net = self.p.network
        rid = (net.routes.get(tr) or {}).get("route_id")
        return [t for t, r in net.routes.items() if rid and r.get("route_id") == rid and t in net.plans] or [tr]

    # ------------------------------------------------------------------ ответы

    def remove(self, mid: int) -> bool:
        m = self.measures.pop(mid, None)
        r = self.reserves.get(mid)
        if r:
            self._drop_reserve(r)
        return m is not None

    def reserve_of(self, tr: str) -> str | None:
        for r in self.reserves.values():
            if r.vtr == tr:
                return r.tr
        return None

    @staticmethod
    def _late(rows, key) -> int:
        return sum(1 for x in rows if x[key] > 120)

    def _summary(self, pr: dict) -> dict:
        rows = pr["rows"]
        tgt = next((x for x in rows if pr["target_idx"] is not None
                    and x["visit_id"] == int(self.p.network.plans[pr["tr"]].visit_id[pr["target_idx"]])), None)
        back = next((x for x in rows if x["delay_s"] > 60 and x["delay_measure_s"] <= 60), None)
        return {"target": tgt, "back_on_schedule": back,
                "late_stops": self._late(rows, "delay_s"), "late_stops_measure": self._late(rows, "delay_measure_s")}

    def _out(self, m: dict, now: float) -> dict:
        tr = m["tr_id"]
        out = {k: v for k, v in m.items() if k not in ("k0", "k1")}
        out["tr_id"] = tr_out(tr)
        out["created_at"] = to_dt(m["created_at"])
        if m["kind"] == "reserve":
            r = self.reserves.get(m["id"])
            if r is None:
                return out
            plan = self.p.network.plans.get(tr)
            base = self.projection(tr, now, until_k=r.k1) if plan is not None else None
            covered = [x for x in (base["rows"] if base else []) if plan.pos.get(x["visit_id"], -1) >= r.k0
                       and plan.pos.get(x["visit_id"], 10**9) <= r.k1]
            late = self._late(covered, "delay_s")
            vt = self.p.predictions.get(r.vtr)
            start_name = self.p.network.stop_name(r.plan.stop[0])
            end_name = self.p.network.stop_name(r.plan.stop[-1])
            out.update({
                "virtual_tr_id": tr_out(r.vtr),
                "start": {"stop_name": start_name, "time": to_dt(r.plan.plan[0]),
                          "lat": float(r.plan.lat[0]), "lon": float(r.plan.lon[0])},
                "end": {"stop_name": end_name, "time": to_dt(r.plan.plan[-1])},
                "stops": int(len(r.plan.plan)),
                "state": "к точке выхода" if now < r.plan.plan[0] else "на линии" if now <= r.plan.plan[-1] else "рейс завершён",
                "base_delay_s": round(covered[0]["delay_s"], 1) if covered else None,
                "late_stops_without": late, "late_stops_with": 0,
                "reserve_forecast_s": vt["prediction_delay_s"] if vt else None,
                "text": (f"Резерв идёт по графику с «{start_name}» в {to_dt(r.plan.plan[0]):%H:%M} до «{end_name}» "
                         f"({len(r.plan.plan)} ост.). " +
                         (f"Остановок с опозданием > 2 мин: {late} → 0." if late else
                          "Основной ТС успевает к этому рейсу сам — резерв добавляет рейс на линии.")),
            })
            return out
        trs = self.line_vehicles(tr) if m["scope"] == "route" else [tr]
        vehicles = []
        for t in trs:
            pr = self.projection(t, now, cut_s=m["cut_s"], short_layover=m["short_layover"])
            if pr is None:
                continue
            sm = self._summary(pr)
            vehicles.append({"tr_id": tr_out(t), "rows": [{**x, "time_plan": to_dt(x["time_plan"])} for x in pr["rows"]],
                             **{k: v for k, v in sm.items() if k in ("late_stops", "late_stops_measure")},
                             "target": sm["target"] and {**sm["target"], "time_plan": to_dt(sm["target"]["time_plan"])},
                             "back_on_schedule": sm["back_on_schedule"] and {**sm["back_on_schedule"],
                                                                              "time_plan": to_dt(sm["back_on_schedule"]["time_plan"])}})
        main = next((v for v in vehicles if str(v["tr_id"]) == str(tr_out(tr))), vehicles[0] if vehicles else None)
        text = ""
        if main and main["target"]:
            tg = main["target"]
            text = f"к «{tg['stop_name']}» {_d(tg['delay_s'])} → {_d(tg['delay_measure_s'])}"
            if main["back_on_schedule"]:
                b = main["back_on_schedule"]
                text += f"; в графике с «{b['stop_name']}» ({b['time_plan']:%H:%M})"
            text += f"; остановок с опозданием > 2 мин: {main['late_stops']} → {main['late_stops_measure']}"
        elif main:
            text = f"остановок с опозданием > 2 мин: {main['late_stops']} → {main['late_stops_measure']}"
        out.update({"vehicles": vehicles, "text": text})
        return out

    def list_out(self) -> list[dict]:
        now = self.p.now()
        return [self._out(m, now) for m in self.measures.values()]


def _d(x: float) -> str:
    s = "+" if x >= 0 else "−"
    x = abs(round(x))
    return f"{s}{x // 60}:{x % 60:02d}"
