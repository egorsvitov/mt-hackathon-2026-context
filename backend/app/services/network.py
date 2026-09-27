"""Сеть маршрутов и плановое расписание для онлайн-контура.

Сеть берётся из NETWORK_PATH. Из расписания читаются только плановые колонки, факт прибытия
backend оценивает сам по GPS.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.config import settings

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=settings.TZ_OFFSET_HOURS))
PLAN_COLUMNS = ["tt_action_item_id", "time_begin", "tr_id", "geom", "building_address"]


def naive_to_epoch(values) -> np.ndarray:
    """Переводит московское время из датасета (без зоны) в Unix-время в секундах."""
    d = pd.to_datetime(pd.Series(values))
    naive = ((d - pd.Timestamp("1970-01-01")) / pd.Timedelta(seconds=1)).to_numpy(
        dtype=float
    )
    return naive - settings.TZ_OFFSET_HOURS * 3600


def to_dt(epoch: float | None) -> datetime | None:
    """Unix-время в datetime с московской зоной, None и NaN превращает в None."""
    if epoch is None or (isinstance(epoch, float) and math.isnan(epoch)):
        return None
    return datetime.fromtimestamp(epoch, TZ)


def haversine_m(lat1, lon1, lat2, lon2):
    """Расстояние по поверхности Земли в метрах, работает и с массивами numpy."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371000.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def tr_out(tr: str):
    """tr_id для ответа API: числом, если он состоит из цифр, как в датасете и сети дашборда."""
    return int(tr) if str(tr).isdigit() else tr


@dataclass
class VehiclePlan:
    """Плановые посещения остановок одним ТС по возрастанию времени."""

    tr_id: str
    visit_id: np.ndarray
    plan: np.ndarray
    stop: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    pos: dict = field(default_factory=dict)

    def __post_init__(self):
        self.pos = {int(v): k for k, v in enumerate(self.visit_id)}

    def target(self, t: float) -> int:
        """Индекс первого посещения, плановое время которого через 10-15 минут от t, или -1."""
        j = int(np.searchsorted(self.plan, t + settings.WINDOW_MIN_SEC, side="right"))
        if j < len(self.plan) and self.plan[j] <= t + settings.WINDOW_MAX_SEC:
            return j
        return -1

    def last_planned(self, t: float) -> int:
        """Индекс последнего посещения, плановое время которого уже наступило к моменту t."""
        return int(np.searchsorted(self.plan, t, side="right")) - 1


class NetworkStore:
    """Сеть маршрутов и плановое расписание, которые backend держит в памяти."""
    def __init__(self):
        self.stops: dict[int, dict] = {}
        self.routes: dict[str, dict] = {}
        self.plans: dict[str, VehiclePlan] = {}
        self.loaded = False
        self.error: str | None = None

    def load(self) -> None:
        """Загружает сеть и расписание, ошибку запоминает, чтобы сервис всё равно поднялся."""
        try:
            self._load_network()
            self._load_plan()
            self.loaded = True
            log.info(
                "Сеть: %d остановок, %d маршрутов; план: %d ТС",
                len(self.stops),
                len(self.routes),
                len(self.plans),
            )
        except Exception as e:  # поднимаемся и без данных, причину покажет /health/ready
            self.error = f"{type(e).__name__}: {e}"
            log.exception("Не удалось загрузить справочные данные")

    def _load_network(self) -> None:
        """Читает остановки и геометрию маршрутов из network.json, если он есть."""
        path = Path(settings.NETWORK_PATH)
        if not path.exists():
            log.warning(
                "Нет %s — маршруты будут прямыми отрезками между остановками", path
            )
            return
        net = json.loads(path.read_text(encoding="utf-8"))
        self.stops = {int(s["stop_key"]): s for s in net["stops"]}
        self.routes = {str(r["tr_id"]): r for r in net["routes"]}

    def _load_plan(self) -> None:
        """Читает плановое расписание и привязывает его остановки к остановкам сети.

        Для ТС без маршрута в сети строится маршрут прямыми отрезками между остановками.
        """
        base_dir = Path(settings.DATA_DIR)
        split = base_dir / settings.SCHEDULE_SPLIT
        
        candidates = [
            split / "schedule_plan.csv",
            split / "schedule.csv",
            base_dir / "schedule_plan.csv",
            base_dir / "schedule.csv",
        ]
        
        path = None
        for cand in candidates:
            if cand.exists():
                path = cand
                break
                
        if not path:
            log.warning("Файл расписания (schedule.csv) не найден в %s. План не загружен.", base_dir)
            return
            
        # только плановые колонки: факт прибытия онлайн-контур не должен видеть
        df = pd.read_csv(path, usecols=PLAN_COLUMNS)
        df["t_plan"] = naive_to_epoch(df["time_begin"])

        # остановку из плана считаем той же, что в сети, если до неё не больше 3 м
        net_keys = np.array(list(self.stops), dtype=int)
        net_lat = np.array([self.stops[k]["lat"] for k in net_keys])
        net_lon = np.array([self.stops[k]["lon"] for k in net_keys])
        next_key = max(self.stops, default=-1) + 1
        keys = {}
        for geom, addr in (
            df[["geom", "building_address"]]
            .drop_duplicates("geom")
            .itertuples(index=False)
        ):
            lon, lat = map(float, geom.replace("POINT (", "").replace(")", "").split())
            k = None
            if len(net_keys):
                d = haversine_m(lat, lon, net_lat, net_lon)
                if d.min() <= 3.0:
                    k = int(net_keys[int(d.argmin())])
            if k is None:
                k = next_key
                next_key += 1
                self.stops[k] = {
                    "stop_key": k,
                    "lat": lat,
                    "lon": lon,
                    "name": addr
                    if isinstance(addr, str) and addr.strip()
                    else "Остановка б/н",
                }
            keys[geom] = k
        df["stop"] = df["geom"].map(keys)

        for tr, g in df.groupby("tr_id"):
            g = g.sort_values(["t_plan", "tt_action_item_id"], kind="stable")
            stop = g["stop"].to_numpy(int)
            tr = str(tr)
            self.plans[tr] = VehiclePlan(
                tr_id=tr,
                visit_id=g["tt_action_item_id"].to_numpy(np.int64),
                plan=g["t_plan"].to_numpy(float),
                stop=stop,
                lat=np.array([self.stops[s]["lat"] for s in stop]),
                lon=np.array([self.stops[s]["lon"] for s in stop]),
            )
            if tr not in self.routes:
                self.routes[tr] = self._straight_route(tr, stop)

    def _straight_route(self, tr: str, stop: np.ndarray) -> dict:
        """Маршрут прямыми отрезками по порядку остановок, когда нет реальной геометрии."""
        segs, seen = [], set()
        for a, b in zip(stop[:-1], stop[1:]):
            if a == b or (a, b) in seen:
                continue
            seen.add((a, b))
            A, B = self.stops[int(a)], self.stops[int(b)]
            segs.append(
                {
                    "from": int(a),
                    "to": int(b),
                    "synthetic": True,
                    "path": [[A["lat"], A["lon"]], [B["lat"], B["lon"]]],
                }
            )
        uniq = sorted({int(s) for s in stop})
        return {
            "route_id": f"R{tr}",
            "tr_id": tr_out(tr),
            "name": f"Маршрут ТС {tr}",
            "speed_norm_kmh": None,
            "stops": uniq,
            "segments": segs,
        }

    def stop_name(self, key: int) -> str:
        """Название остановки по ключу или прочерк."""
        s = self.stops.get(int(key))
        return s["name"] if s else "—"

    def route_id(self, tr: str) -> str | None:
        """Идентификатор маршрута ТС или None."""
        return self.routes[tr]["route_id"] if tr in self.routes else None

    def speed_norm(self, tr: str) -> float | None:
        """Обычная скорость на маршруте ТС в км/ч, если она известна."""
        r = self.routes.get(tr)
        return r.get("speed_norm_kmh") if r else None

    def network_payload(self) -> dict:
        """Сеть в виде ответа для /network."""
        return {
            "stops": list(self.stops.values()),
            "routes": list(self.routes.values()),
        }
