"""Сборка данных для дашборда в режиме REPLAY (воспроизведение дня из датасета).

Что делает скрипт:

1. Восстанавливает «маршрутную сеть» по расписанию и GPS: у каждого реального ТС
   свой набор остановок, поэтому маршрут = нитка ТС; геометрия участков между
   остановками берётся из GPS-трека, а не прямыми отрезками.
2. Выпускает прогнозные точки так же, как будет делать backend онлайн: каждые
   ``--grid-step`` секунд для каждого ТС берётся первая плановая остановка в окне
   ``(T+10 мин, T+15 мин]``. Точки сохраняются в ``points_grid.csv`` в формате
   ``validate/points.csv``.
3. Получает прогноз задержки:
   * по умолчанию — встроенной демо-моделью (sklearn, обучение на ``labels_train``);
   * либо из ``--predictions preds.csv`` в формате сабмита ``sample_id;prediction``
     (опционально колонка ``late_probability``) — так подключается модель команды.
4. Считает уровень риска, гипотезу причины и рекомендацию (``incident_rules.py``),
   собирает инциденты и пишет ``data/replay.js`` для дашборда и примеры
   ответов backend в ``contract/examples/``.

Все признаки считаются только по данным с ``event_time <= T``. Фактическое время
прибытия (``time_fact_begin``) используется лишь как исход для проверки прогноза
и показывается на дашборде только после того, как это время наступило.

Запуск::

    python dashboard/tools/build_fixtures.py --data-dir ../dataset
    python dashboard/tools/build_fixtures.py --data-dir ../dataset --predictions preds.csv
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from incident_rules import (  # noqa: E402
    EVIDENCE_SPEC,
    REASONS,
    SEVERITY_ORDER,
    THRESHOLDS,
    diagnose,
    evidence,
    severity_of,
)

MSK_OFFSET_S = 3 * 3600  # метки времени в датасете — наивные, московское время
H_MIN, H_MAX = 600, 900  # окно прогноза (T+10 мин, T+15 мин]
FALLBACK_GPS_AGE_S = 900  # старше — прогноз модели не выпускаем, fallback = cur_dev_s
ALERT_KINDS = {"warning": "late", "critical": "late", "early": "early"}
INCIDENT_COOLDOWN = 3  # столько прогнозов подряд без риска закрывают инцидент

MODEL_FEATURES = [
    "cur_dev_s", "horizon_s", "hour_sin", "hour_cos", "speed_5m_kmh", "speed_15m_kmh",
    "speed_ratio", "stationary_s", "gps_age_s", "dist_to_target_m", "required_speed_kmh",
    "n_stops_to_target", "dev_trend_15m_s", "near_stop_m",
]


# ----------------------------------------------------------------------------- утилиты

def naive_sec(values) -> np.ndarray:
    """Наивные метки (МСК) -> секунды так, как в ``sample_id`` (время как будто UTC)."""
    d = pd.to_datetime(pd.Series(values))
    return ((d - pd.Timestamp("1970-01-01")) / pd.Timedelta(seconds=1)).to_numpy(dtype=float)


def to_epoch(naive_s: float) -> int:
    """Секунды «наивного» времени -> настоящий Unix epoch (МСК = UTC+3)."""
    return int(round(naive_s - MSK_OFFSET_S))


def iso_msk(epoch_s: float | None) -> str | None:
    if epoch_s is None or (isinstance(epoch_s, float) and math.isnan(epoch_s)):
        return None
    ts = pd.Timestamp(int(epoch_s) + MSK_OFFSET_S, unit="s")
    return ts.strftime("%Y-%m-%dT%H:%M:%S") + "+03:00"


def haversine_m(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def clean(x, nd=1):
    """Число для JSON: NaN -> None, округление."""
    if x is None:
        return None
    x = float(x)
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd) if nd else int(round(x))


# ----------------------------------------------------------------------------- данные

def load_traffic(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=["tr_id", "unit_id", "event_time", "location_valid",
                                    "lon", "lat", "speed", "heading"])
    df["t"] = naive_sec(df["event_time"])
    valid = df["location_valid"].astype(str).str.lower().eq("true")
    valid &= df["lat"].between(55.0, 56.6) & df["lon"].between(36.3, 38.6)
    valid &= df["speed"].fillna(0).between(0, 120)
    df = df[valid].copy()
    df["speed"] = df["speed"].fillna(0.0)
    df["heading"] = df["heading"].fillna(0.0)
    df = df.sort_values(["tr_id", "t"]).drop_duplicates(["tr_id", "t"])
    return df


class Track:
    """GPS-трек одного ТС с быстрыми запросами «состояние на момент T»."""

    def __init__(self, g: pd.DataFrame):
        self.unit_id = int(g["unit_id"].iloc[0]) if len(g) else None
        self.t = g["t"].to_numpy(float)
        self.lat = g["lat"].to_numpy(float)
        self.lon = g["lon"].to_numpy(float)
        self.spd = g["speed"].to_numpy(float)
        self.hdg = g["heading"].to_numpy(float)
        self.cs = np.concatenate([[0.0], np.cumsum(self.spd)])
        idx = np.where(self.spd >= 3, np.arange(len(self.t)), -1)
        self.last_move = np.maximum.accumulate(idx) if len(idx) else idx

    def at(self, T: np.ndarray) -> dict:
        n = len(self.t)
        T = np.asarray(T, float)
        if n == 0:
            nan = np.full(len(T), np.nan)
            return {"lat": nan, "lon": nan, "speed_5m": nan, "speed_15m": nan,
                    "gps_age": nan, "stationary": np.zeros(len(T))}
        end = np.searchsorted(self.t, T, side="right")
        has = end > 0
        i = np.clip(end - 1, 0, n - 1)

        def mean_speed(win):
            st = np.searchsorted(self.t, T - win, side="right")
            cnt = end - st
            return np.where(cnt > 0, (self.cs[end] - self.cs[st]) / np.maximum(cnt, 1), np.nan)

        lm = self.last_move[i]
        first_still = np.clip(lm + 1, 0, n - 1)
        # Наблюдаемая длительность простоя: разрыв связи простоем не считается.
        stationary = np.where(has & (lm != i), self.t[i] - self.t[first_still], 0.0)
        return {
            "lat": np.where(has, self.lat[i], np.nan),
            "lon": np.where(has, self.lon[i], np.nan),
            "speed_5m": mean_speed(300),
            "speed_15m": mean_speed(900),
            "gps_age": np.where(has, T - self.t[i], np.nan),
            "stationary": stationary,
        }


class Plan:
    """Плановые посещения остановок одним ТС, отсортированные по плановому времени."""

    def __init__(self, g: pd.DataFrame):
        g = g.sort_values(["time_begin", "time_fact_begin"], kind="stable")
        self.visit_id = g["tt_action_item_id"].to_numpy(np.int64)
        self.plan = naive_sec(g["time_begin"])
        self.fact = naive_sec(g["time_fact_begin"]) if "time_fact_begin" in g else np.full(len(g), np.nan)
        self.manual = g["manual_fill"].astype(str).str.lower().eq("true").to_numpy()
        self.stop = g["stop"].to_numpy(int)
        self.dev = self.fact - self.plan
        self.pos = {int(v): k for k, v in enumerate(self.visit_id)}

    def last_passed(self, T):
        return np.searchsorted(self.plan, T, side="right") - 1

    def cur_dev(self, T):
        """Как ``cur_dev_s`` организаторов: отклонение на последней остановке с планом <= T."""
        k = self.last_passed(T)
        d = np.where(k >= 0, self.dev[np.clip(k, 0, None)], 0.0)
        return np.nan_to_num(d, nan=0.0)

    def target(self, T):
        """Индекс первой плановой остановки в окне (T+10, T+15] или -1."""
        j = np.searchsorted(self.plan, T + H_MIN, side="right")
        jj = np.clip(j, 0, len(self.plan) - 1)
        ok = (j < len(self.plan)) & (self.plan[jj] <= T + H_MAX)
        return np.where(ok, jj, -1)


def parse_point(geom: str):
    lon, lat = geom.replace("POINT (", "").replace(")", "").split()
    return float(lat), float(lon)


class Split:
    """Одна часть датасета (train/test): треки, расписание, остановки."""

    def __init__(self, data_dir: Path, name: str):
        self.name = name
        self.traffic = load_traffic(data_dir / name / "traffic.csv")
        self.tracks = {int(tr): Track(g) for tr, g in self.traffic.groupby("tr_id")}
        sch = pd.read_csv(data_dir / name / "schedule.csv")
        keys = sch["geom"].drop_duplicates().tolist()
        self.stop_index = {k: i for i, k in enumerate(keys)}
        pts = np.array([parse_point(k) for k in keys])
        self.stop_lat, self.stop_lon = pts[:, 0], pts[:, 1]
        addr = sch.drop_duplicates("geom").set_index("geom")["building_address"]
        self.stop_addr = [a if isinstance(a, str) and a.strip() else None for a in addr.reindex(keys)]
        sch["stop"] = sch["geom"].map(self.stop_index)
        self.schedule = sch
        self.plans = {int(tr): Plan(g) for tr, g in sch.groupby("tr_id")}
        self.route_stops = {tr: np.unique(p.stop) for tr, p in self.plans.items()}
        self.speed_norm = {tr: self._speed_norm(tr) for tr in self.plans}

    def _speed_norm(self, tr) -> float:
        """Норма маршрута: медиана средней 5-минутной скорости на линии (км/ч)."""
        tr_track = self.tracks.get(tr)
        p = self.plans[tr]
        if tr_track is None or len(tr_track.t) == 0:
            return float("nan")
        T = np.arange(p.plan.min(), p.plan.max(), 60.0)
        s = tr_track.at(T)
        v = s["speed_5m"][s["gps_age"] <= 60]
        return float(np.nanmedian(v)) if np.isfinite(v).any() else float("nan")

    def features(self, tr: int, T: np.ndarray, target_idx: np.ndarray, cur_dev=None) -> pd.DataFrame:
        T = np.asarray(T, float)
        p = self.plans[tr]
        tr_track = self.tracks.get(tr) or Track(self.traffic.iloc[0:0])
        s = tr_track.at(T)
        cd = p.cur_dev(T) if cur_dev is None else np.asarray(cur_dev, float)
        cd15 = p.cur_dev(T - 900)
        k = p.last_passed(T)
        ts = p.stop[target_idx]
        dist = haversine_m(s["lat"], s["lon"], self.stop_lat[ts], self.stop_lon[ts])
        horizon = p.plan[target_idx] - T
        rs = self.route_stops[tr]
        with np.errstate(invalid="ignore"):
            near = haversine_m(s["lat"][:, None], s["lon"][:, None],
                               self.stop_lat[rs][None, :], self.stop_lon[rs][None, :]).min(axis=1)
        norm = self.speed_norm[tr]
        hour = (T % 86400) / 3600.0
        return pd.DataFrame({
            "cur_dev_s": cd,
            "horizon_s": horizon,
            "hour_sin": np.sin(2 * np.pi * hour / 24),
            "hour_cos": np.cos(2 * np.pi * hour / 24),
            "speed_5m_kmh": s["speed_5m"],
            "speed_15m_kmh": s["speed_15m"],
            "speed_norm_kmh": norm,
            "speed_ratio": s["speed_5m"] / norm if norm and np.isfinite(norm) else np.nan,
            "stationary_s": s["stationary"],
            "gps_age_s": s["gps_age"],
            "dist_to_target_m": dist,
            "required_speed_kmh": dist / np.maximum(horizon, 60) * 3.6,
            "n_stops_to_target": target_idx - k,
            "dev_trend_15m_s": cd - cd15,
            "near_stop_m": near,
            "lat": s["lat"],
            "lon": s["lon"],
            "last_idx": k,
        })

    def stop_name(self, i: int) -> str:
        if self.stop_addr[i]:
            return self.stop_addr[i]
        d = haversine_m(self.stop_lat[i], self.stop_lon[i], self.stop_lat, self.stop_lon)
        named = [j for j in np.argsort(d) if self.stop_addr[j] and d[j] < 2000]
        if not named:
            return "Остановка б/н"
        j = named[0]
        if d[j] < 250:
            return f"у {self.stop_addr[j]}"
        dist = f"{d[j]:.0f} м" if d[j] < 1000 else f"{d[j] / 1000:.1f} км".replace(".", ",")
        return f"б/н, {dist} от {self.stop_addr[j]}"


def label_features(split: Split, labels: pd.DataFrame) -> pd.DataFrame:
    """Признаки для размеченных точек (train/test labels)."""
    parts = []
    labels = labels.copy()
    labels["Tn"] = naive_sec(labels["T"])
    for tr, g in labels.groupby("tr_id"):
        tr = int(tr)
        if tr not in split.plans:
            continue
        p = split.plans[tr]
        idx = np.array([p.pos.get(int(v), -1) for v in g["target_stop_id"]])
        ok = idx >= 0
        g = g[ok]
        f = split.features(tr, g["Tn"].to_numpy(), idx[ok], cur_dev=g["cur_dev_s"].to_numpy())
        f.index = g.index
        parts.append(pd.concat([g, f.drop(columns=["cur_dev_s"])], axis=1))
    return pd.concat(parts)


# ----------------------------------------------------------------------------- демо-модель

class DemoModel:
    """Лёгкая модель для прототипа: остаток к cur_dev_s + вероятность опоздания > 120 с.

    Это не модель для сабмита — только чтобы дашборд показывал правдоподобные
    прогнозы, пока не подключена модель команды (``--predictions``).
    """

    version = "demo-hgb-0.1"

    def fit(self, df: pd.DataFrame):
        from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

        X = df[MODEL_FEATURES]
        y = df["target_delay_s"].to_numpy(float)
        self.reg = HistGradientBoostingRegressor(loss="absolute_error", max_iter=300, learning_rate=0.05,
                                                 max_depth=4, random_state=42)
        self.reg.fit(X, y - df["cur_dev_s"].to_numpy(float))
        self.clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_depth=3, random_state=42)
        self.clf.fit(X, (y > THRESHOLDS["critical"]).astype(int))
        return self

    def predict(self, df: pd.DataFrame):
        X = df[MODEL_FEATURES]
        pred = df["cur_dev_s"].to_numpy(float) + self.reg.predict(X)
        prob = self.clf.predict_proba(X)[:, 1]
        return pred, prob


# ----------------------------------------------------------------------------- геометрия

def decimate(path: list[tuple[float, float]], min_step_m=25.0):
    if len(path) <= 2:
        return path
    out = [path[0]]
    for p in path[1:-1]:
        if haversine_m(out[-1][0], out[-1][1], p[0], p[1]) >= min_step_m:
            out.append(p)
    out.append(path[-1])
    return out


def build_segments(split: Split, tr: int) -> list[dict]:
    """Геометрия участков «остановка -> следующая остановка» по GPS между фактами прибытия."""
    p = split.plans[tr]
    tr_track = split.tracks.get(tr)
    cand: dict[tuple[int, int], list] = {}
    for k in range(len(p.plan) - 1):
        a, b = int(p.stop[k]), int(p.stop[k + 1])
        if a == b:
            continue
        cand.setdefault((a, b), [])
        if tr_track is None or not (np.isfinite(p.fact[k]) and np.isfinite(p.fact[k + 1])):
            continue
        t0, t1 = p.fact[k], p.fact[k + 1]
        if t1 <= t0 or t1 - t0 > 1800:
            continue
        i0, i1 = np.searchsorted(tr_track.t, [t0, t1])
        if i1 - i0 < 2:
            continue
        pts = list(zip(tr_track.lat[i0:i1], tr_track.lon[i0:i1]))
        path = [(split.stop_lat[a], split.stop_lon[a])] + pts + [(split.stop_lat[b], split.stop_lon[b])]
        lat = np.array([q[0] for q in path])
        lon = np.array([q[1] for q in path])
        length = haversine_m(lat[:-1], lon[:-1], lat[1:], lon[1:]).sum()
        straight = haversine_m(lat[0], lon[0], lat[-1], lon[-1]) + 1.0
        if length / straight > 2.5 and length - straight > 300:
            continue  # петля/отстой — не годится как типичная геометрия
        cand[(a, b)].append((bool(p.manual[k] or p.manual[k + 1]), length, path))
    segs = []
    for (a, b), items in cand.items():
        auto = [c for c in items if not c[0]] or items
        if auto:
            auto.sort(key=lambda c: c[1])
            path = auto[len(auto) // 2][2]
            synthetic = False
        else:
            path = [(split.stop_lat[a], split.stop_lon[a]), (split.stop_lat[b], split.stop_lon[b])]
            synthetic = True
        path = decimate(path)
        segs.append({"from": a, "to": b, "synthetic": synthetic,
                     "path": [[round(q[0], 5), round(q[1], 5)] for q in path]})
    return segs


def short_name(name: str) -> str:
    """Короткое имя конечной для названия маршрута: улица без номера дома."""
    if " от " in name:
        name = name.split(" от ", 1)[1]
    return name.removeprefix("у ").split(", д.")[0]


# ----------------------------------------------------------------------------- сборка

def build(args):
    t_start = time.time()
    data_dir = Path(args.data_dir).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    contract_dir = Path(args.contract_out).resolve()
    contract_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/6] Загрузка {args.split} из {data_dir}")
    split = Split(data_dir, args.split)
    labels_path = data_dir / "labels" / f"labels_{args.split}.csv"
    labels = pd.read_csv(labels_path) if labels_path.exists() else None

    # --- прогнозные точки на сетке, как у онлайн-backend
    print("[2/6] Прогнозные точки на сетке", args.grid_step, "с")
    rows = []
    for tr, p in split.plans.items():
        T0 = math.floor((p.plan.min() - H_MAX) / args.grid_step) * args.grid_step
        T = np.arange(T0, p.plan.max() - H_MIN + 1, args.grid_step, dtype=float)
        j = p.target(T)
        T, j = T[j >= 0], j[j >= 0]
        if len(T) == 0:
            continue
        f = split.features(tr, T, j)
        f["tr_id"] = tr
        f["Tn"] = T
        f["target_idx"] = j
        rows.append(f)
    grid = pd.concat(rows, ignore_index=True)
    grid["sample_id"] = grid["tr_id"].astype(str) + "_" + grid["Tn"].astype(np.int64).astype(str)
    grid["target_stop_id"] = [int(split.plans[tr].visit_id[j]) for tr, j in zip(grid.tr_id, grid.target_idx)]
    grid["target_plan"] = [split.plans[tr].plan[j] for tr, j in zip(grid.tr_id, grid.target_idx)]
    grid["outcome_s"] = [split.plans[tr].dev[j] for tr, j in zip(grid.tr_id, grid.target_idx)]
    grid["outcome_at"] = [split.plans[tr].fact[j] for tr, j in zip(grid.tr_id, grid.target_idx)]
    fmt = lambda s: pd.to_datetime(s, unit="s").dt.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    points = pd.DataFrame({
        "sample_id": grid["sample_id"], "tr_id": grid["tr_id"], "T": fmt(grid["Tn"]),
        "target_stop_id": grid["target_stop_id"], "target_time_begin": fmt(grid["target_plan"]),
        "cur_dev_s": grid["cur_dev_s"],
    })
    points.to_csv(out_dir / "points_grid.csv", index=False)
    print(f"      {len(grid)} точек, {grid.tr_id.nunique()} ТС -> {out_dir / 'points_grid.csv'}")

    # --- модель
    print("[3/6] Прогноз")
    model_info = {}
    if args.predictions:
        ext = pd.read_csv(args.predictions, sep=None, engine="python")
        ext = ext.set_index("sample_id")
        grid["pred"] = grid["sample_id"].map(ext["prediction"])
        grid["late_prob"] = grid["sample_id"].map(ext["late_probability"]) if "late_probability" in ext else np.nan
        missing = int(grid["pred"].isna().sum())
        model_info["version"] = f"external:{Path(args.predictions).name}"
        if missing:
            print(f"      ВНИМАНИЕ: нет прогноза для {missing} точек — для них fallback cur_dev_s")
        latency_ms = None
    else:
        train = Split(data_dir, args.train_split)
        lt = label_features(train, pd.read_csv(data_dir / "labels" / f"labels_{args.train_split}.csv"))
        model = DemoModel().fit(lt)
        t0 = time.perf_counter()
        grid["pred"], grid["late_prob"] = model.predict(grid)
        batch_ms = (time.perf_counter() - t0) * 1000
        one = grid.iloc[[len(grid) // 2]]
        t0 = time.perf_counter()
        for _ in range(50):
            model.predict(one)
        latency_ms = (time.perf_counter() - t0) * 1000 / 50
        model_info["version"] = DemoModel.version
        model_info["batch_ms_per_1000"] = round(batch_ms / len(grid) * 1000, 2)
        if labels is not None:
            lf = label_features(split, labels)
            pred_l, _ = model.predict(lf)
            y = lf["target_delay_s"].to_numpy(float)
            model_info.update({
                "mae_model": round(float(np.mean(np.abs(y - pred_l))), 2),
                "mae_cur_dev": round(float(np.mean(np.abs(y - lf["cur_dev_s"]))), 2),
                "mae_zero": round(float(np.mean(np.abs(y))), 2),
                "n_eval": int(len(y)),
                "eval_on": f"labels_{args.split}",
            })
            print(f"      MAE на labels_{args.split}: модель {model_info['mae_model']} с, "
                  f"cur_dev {model_info['mae_cur_dev']} с, ноль {model_info['mae_zero']} с")
    model_info["latency_ms_single"] = round(latency_ms, 2) if latency_ms else None

    # --- статус и fallback
    stale = grid["gps_age_s"].isna() | (grid["gps_age_s"] > FALLBACK_GPS_AGE_S) | grid["pred"].isna()
    grid["status"] = np.where(stale, "fallback", "model")
    grid.loc[stale, "pred"] = grid.loc[stale, "cur_dev_s"]
    grid.loc[stale, "late_prob"] = np.nan
    grid["severity"] = [severity_of(v) for v in grid["pred"]]
    feats = grid[[s["feature"] for s in EVIDENCE_SPEC] + ["speed_norm_kmh", "near_stop_m"]].to_dict("records")
    diag = [diagnose(f, p, s) for f, p, s in zip(feats, grid["pred"], grid["severity"])]
    grid["reason"] = [d[0] for d in diag]
    grid["detail"] = [d[1] for d in diag]
    grid["labelled"] = grid["sample_id"].isin(set(labels["sample_id"])) if labels is not None else False
    grid = grid.sort_values(["tr_id", "Tn"]).reset_index(drop=True)
    # Участок начинается с последней остановки, до которой ТС реально доехало к T (факт <= T),
    # а не с последней по плану: опаздывающий автобус до «плановой» остановки ещё не доехал.
    reached = {tr: np.fmax.accumulate(np.nan_to_num(p.fact, nan=-np.inf)) for tr, p in split.plans.items()}
    grid["from_idx"] = [max(0, int(np.searchsorted(reached[tr], T, side="right")) - 1)
                        for tr, T in zip(grid["tr_id"], grid["Tn"])]

    # --- инциденты: эпизоды риска по ТС
    print("[4/6] Инциденты")
    incidents = []
    for tr, g in grid.groupby("tr_id", sort=False):
        cur = None
        calm = 0
        prev_t = None
        prev_kind = None
        for idx, t, sev in zip(g.index, g["Tn"], g["severity"]):
            kind = ALERT_KINDS.get(sev)
            if cur and prev_t is not None and t - prev_t > 5 * args.grid_step:
                cur["closed_at"] = prev_t + args.grid_step
                cur = None
            if kind:
                if cur and cur["kind"] != kind:
                    cur["closed_at"] = t
                    cur = None
                # Антидребезг: «красный» открывает инцидент сразу, остальное — со второго прогноза подряд.
                if cur is None and (sev == "critical" or prev_kind == kind):
                    cur = {"tr_id": int(tr), "kind": kind, "opened_at": t, "closed_at": None, "preds": []}
                    incidents.append(cur)
                if cur is not None:
                    cur["preds"].append(int(idx))
                calm = 0
            elif cur:
                calm += 1
                cur["preds"].append(int(idx))
                if calm >= INCIDENT_COOLDOWN:
                    cur["closed_at"] = t
                    cur = None
            prev_t = t
            prev_kind = kind
        if cur:
            cur["closed_at"] = prev_t + args.grid_step
    for inc in incidents:
        inc["incident_id"] = f"INC-{inc['tr_id']}-{to_epoch(inc['opened_at'])}"
    print(f"      {len(incidents)} инцидентов")

    if args.start == "auto":
        # Старт воспроизведения — незадолго до момента с наибольшим числом активных опозданий.
        best_t, best_n = None, -1
        for t in np.arange(grid["Tn"].min() + 3600, grid["Tn"].max() - 3600, 300):
            n = sum(i["kind"] == "late" and i["opened_at"] <= t < i["closed_at"] for i in incidents)
            if n > best_n:
                best_t, best_n = t, n
        start_naive = best_t - 600
        print(f"      старт воспроизведения: {pd.Timestamp(start_naive, unit='s')} ({best_n} активных опозданий)")
    else:
        start_naive = naive_sec([args.start])[0]

    # --- сеть маршрутов
    print("[5/6] Маршрутная сеть и телеметрия")
    used_stops = sorted({int(s) for p in split.plans.values() for s in p.stop})
    stop_names = {i: split.stop_name(i) for i in used_stops}
    routes = []
    for tr, p in split.plans.items():
        rs = split.route_stops[tr]
        lat, lon = split.stop_lat[rs], split.stop_lon[rs]
        d = haversine_m(lat[:, None], lon[:, None], lat[None, :], lon[None, :])
        a, b = np.unravel_index(np.argmax(d), d.shape)
        name = f"{short_name(stop_names[int(rs[a])])} — {short_name(stop_names[int(rs[b])])}"
        routes.append({
            "route_id": f"R{tr}", "tr_id": tr, "name": name,
            "speed_norm_kmh": clean(split.speed_norm[tr]),
            "stops": [int(s) for s in rs],
            "segments": build_segments(split, tr),
        })
    routes.sort(key=lambda r: r["tr_id"])

    # --- телеметрия (все ТС, в т.ч. без расписания)
    t0_epoch = to_epoch(split.traffic["t"].min())
    telemetry = {}
    for tr, tk in split.tracks.items():
        if len(tk.t) < 20:
            continue
        keep, last = [], -1e18
        for i, t in enumerate(tk.t):
            if t - last >= args.telemetry_step:
                keep.append(i)
                last = t
        keep = np.array(keep)
        telemetry[str(tr)] = {
            "unit_id": tk.unit_id,
            "t": (tk.t[keep] - MSK_OFFSET_S - t0_epoch).astype(int).tolist(),
            "lat": np.round(tk.lat[keep] * 1e5).astype(int).tolist(),
            "lon": np.round(tk.lon[keep] * 1e5).astype(int).tolist(),
            "spd": np.round(tk.spd[keep]).astype(int).tolist(),
            "hdg": np.round(tk.hdg[keep]).astype(int).tolist(),
        }

    visits = {}
    for tr, p in split.plans.items():
        visits[str(tr)] = {
            "id": [int(v) for v in p.visit_id],
            "stop": [int(s) for s in p.stop],
            "plan": [to_epoch(v) for v in p.plan],
            "fact": [to_epoch(v) if np.isfinite(v) else None for v in p.fact],
        }

    # --- прогнозы (колоночно, чтобы файл был компактным)
    pred_cols = ["as_of", "tr_id", "target_stop_id", "target_time_begin", "from_stop_id", "prediction_delay_s",
                 "late_probability", "cur_dev_s", "status", "outcome_delay_s", "outcome_at", "speed_5m_kmh",
                 "speed_norm_kmh", "stationary_s", "near_stop_m", "gps_age_s", "dev_trend_15m_s",
                 "required_speed_kmh", "reason", "detail", "labelled"]
    pred_rows = []
    for r in grid.itertuples():
        p = split.plans[r.tr_id]
        pred_rows.append([
            to_epoch(r.Tn), int(r.tr_id), int(r.target_stop_id), to_epoch(r.target_plan),
            int(p.visit_id[r.from_idx]), clean(r.pred, 0), clean(r.late_prob, 2), clean(r.cur_dev_s, 0),
            r.status, clean(r.outcome_s, 0), to_epoch(r.outcome_at) if np.isfinite(r.outcome_at) else None,
            clean(r.speed_5m_kmh), clean(r.speed_norm_kmh), clean(r.stationary_s, 0), clean(r.near_stop_m, 0),
            clean(r.gps_age_s, 0), clean(r.dev_trend_15m_s, 0), clean(r.required_speed_kmh),
            r.reason if isinstance(r.reason, str) else None, r.detail if isinstance(r.detail, str) else None,
            bool(r.labelled),
        ])

    meta = {
        "generated_at": iso_msk(time.time()),
        "split": args.split,
        "t0": t0_epoch,
        "day_start": to_epoch(grid["Tn"].min()),
        "day_end": to_epoch(grid["Tn"].max() + H_MAX),
        "default_start": to_epoch(start_naive),
        "grid_step_s": args.grid_step,
        "horizon_s": [H_MIN, H_MAX],
        "thresholds": THRESHOLDS,
        "severity_order": SEVERITY_ORDER,
        "evidence_spec": EVIDENCE_SPEC,
        "reasons": REASONS,
        "model": model_info,
        "timezone": "Europe/Moscow",
    }
    replay = {
        "meta": meta,
        "stops": [[int(i), round(float(split.stop_lat[i]), 6), round(float(split.stop_lon[i]), 6), stop_names[i]]
                  for i in used_stops],
        "routes": routes,
        "visits": visits,
        "telemetry": telemetry,
        "predictions": {"columns": pred_cols, "rows": pred_rows},
        "incidents": [{"incident_id": i["incident_id"], "tr_id": i["tr_id"], "kind": i["kind"],
                       "opened_at": to_epoch(i["opened_at"]),
                       "closed_at": to_epoch(i["closed_at"]) if i["closed_at"] is not None else None,
                       "preds": i["preds"]} for i in incidents],
    }
    # Справочник сети для backend: остановки и геометрия маршрутов (без фактов расписания).
    network = {"stops": [{"stop_key": s[0], "lat": s[1], "lon": s[2], "name": s[3]} for s in replay["stops"]],
               "routes": routes}
    (out_dir / "network.json").write_text(json.dumps(network, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    text = json.dumps(replay, ensure_ascii=False, separators=(",", ":"))
    (out_dir / "replay.js").write_text("window.REPLAY_DATA=" + text + ";\n", encoding="utf-8")
    print(f"      replay.js: {len(text) / 1e6:.1f} МБ")

    # --- примеры ответов backend по контракту
    print("[6/6] Примеры контракта ->", contract_dir)
    example = start_naive + 900 if args.example_time == "auto" else naive_sec([args.example_time])[0]
    write_contract_examples(contract_dir, replay, to_epoch(example))
    print(f"Готово за {time.time() - t_start:.0f} с")


# ----------------------------------------------------------------------------- контракт

def prediction_obj(replay: dict, i: int) -> dict:
    """Строка колоночного формата -> объект Prediction по контракту."""
    cols = replay["predictions"]["columns"]
    r = dict(zip(cols, replay["predictions"]["rows"][i]))
    feats = {s["feature"]: r.get(s["feature"]) for s in EVIDENCE_SPEC}
    feats["speed_norm_kmh"] = r["speed_norm_kmh"]
    sev = severity_of(r["prediction_delay_s"])
    code = r["reason"]  # причина уже определена при выпуске прогноза (diagnose)
    stops = {s[0]: s[3] for s in replay["stops"]}
    v = replay["visits"][str(r["tr_id"])]
    pos = {vid: k for k, vid in enumerate(v["id"])}
    return {
        "sample_id": f"{r['tr_id']}_{r['as_of'] + MSK_OFFSET_S}",
        "tr_id": r["tr_id"],
        "route_id": f"R{r['tr_id']}",
        "as_of": iso_msk(r["as_of"]),
        "generated_at": iso_msk(r["as_of"]),
        "target_stop_id": r["target_stop_id"],
        "target_stop_name": stops[v["stop"][pos[r["target_stop_id"]]]],
        "target_time_begin": iso_msk(r["target_time_begin"]),
        "horizon_s": r["target_time_begin"] - r["as_of"],
        "prediction_delay_s": r["prediction_delay_s"],
        "predicted_arrival": iso_msk(r["target_time_begin"] + r["prediction_delay_s"]),
        "late_probability": r["late_probability"],
        "cur_dev_s": r["cur_dev_s"],
        "severity": sev,
        "status": r["status"],
        "model_version": replay["meta"]["model"].get("version"),
        "data_age_s": r["gps_age_s"],
        "segment": {
            "from_stop_id": r["from_stop_id"],
            "from_stop_name": stops[v["stop"][pos[r["from_stop_id"]]]],
            "to_stop_id": r["target_stop_id"],
            "to_stop_name": stops[v["stop"][pos[r["target_stop_id"]]]],
        },
        "reason": {"code": code, "title": REASONS[code]["title"], "detail": r["detail"]} if code else None,
        "evidence": evidence(feats, code),
        "recommendation": REASONS[code]["recommendation"] if code else None,
    }


def snapshot(replay: dict, now: int) -> dict:
    """Состояние системы на момент ``now`` (epoch) — то, что backend отдаёт по API."""
    meta = replay["meta"]
    cols = replay["predictions"]["columns"]
    rows = replay["predictions"]["rows"]
    c = {k: i for i, k in enumerate(cols)}
    latest: dict[int, int] = {}
    for i, r in enumerate(rows):
        if r[c["as_of"]] <= now and r[c["as_of"]] > now - 3 * meta["grid_step_s"]:
            if r[c["tr_id"]] not in latest or rows[latest[r[c["tr_id"]]]][c["as_of"]] < r[c["as_of"]]:
                latest[r[c["tr_id"]]] = i
    predictions = [prediction_obj(replay, i) for i in latest.values()]
    sev_by_tr = {p["tr_id"]: p["severity"] for p in predictions}
    route_of = {r["tr_id"]: r["route_id"] for r in replay["routes"]}

    vehicles = []
    packets = 0
    for tr, tel in replay["telemetry"].items():
        t = np.array(tel["t"]) + meta["t0"]
        k = int(np.searchsorted(t, now, side="right")) - 1
        packets += k + 1 - int(np.searchsorted(t, now - 60, side="right"))
        if k < 0 or now - t[k] > 3600:
            continue
        age = int(now - t[k])
        vehicles.append({
            "tr_id": int(tr), "unit_id": tel["unit_id"], "route_id": route_of.get(int(tr)),
            "event_time": iso_msk(int(t[k])), "lat": tel["lat"][k] / 1e5, "lon": tel["lon"][k] / 1e5,
            "speed": tel["spd"][k], "heading": tel["hdg"][k], "location_valid": True,
            "data_age_s": age, "status": "live" if age <= 60 else ("stale" if age <= 300 else "offline"),
            "severity": sev_by_tr.get(int(tr), "unknown"), "source": "replay",
        })

    incidents = []
    for inc in replay["incidents"]:
        if inc["opened_at"] > now or (inc["closed_at"] is not None and inc["closed_at"] <= now - 900):
            continue
        seen = [i for i in inc["preds"] if rows[i][c["as_of"]] <= now]
        if not seen:
            continue
        cur = prediction_obj(replay, seen[-1])
        alert = [i for i in seen if severity_of(rows[i][c["prediction_delay_s"]]) in ALERT_KINDS]
        peak = max((severity_of(rows[i][c["prediction_delay_s"]]) for i in alert),
                   key=lambda s: SEVERITY_ORDER[s], default=cur["severity"])
        active = inc["closed_at"] is None or inc["closed_at"] > now
        head = prediction_obj(replay, alert[-1]) if alert else cur
        outcome_at = rows[seen[-1]][c["outcome_at"]]
        incidents.append({
            "incident_id": inc["incident_id"], "tr_id": inc["tr_id"], "route_id": f"R{inc['tr_id']}",
            "kind": inc["kind"], "status": "active" if active else "resolved",
            "first_detected_at": iso_msk(inc["opened_at"]), "updated_at": cur["as_of"],
            "closed_at": iso_msk(inc["closed_at"]) if not active else None,
            "severity": cur["severity"] if active else "ok", "peak_severity": peak,
            "target_stop_id": cur["target_stop_id"], "target_stop_name": cur["target_stop_name"],
            "target_time_begin": cur["target_time_begin"], "horizon_s": cur["horizon_s"],
            "prediction_delay_s": cur["prediction_delay_s"], "predicted_arrival": cur["predicted_arrival"],
            "late_probability": cur["late_probability"], "segment": cur["segment"],
            "suspected_reason": head["reason"], "evidence": head["evidence"],
            "recommendation": head["recommendation"],
            "outcome_delay_s": rows[seen[-1]][c["outcome_delay_s"]] if outcome_at and outcome_at <= now else None,
        })

    done = [r for r in rows if r[c["outcome_at"]] is not None and r[c["outcome_at"]] <= now and r[c["as_of"]] <= now]
    done = [r for r in done if r[c["outcome_delay_s"]] is not None]
    err = [abs(r[c["prediction_delay_s"]] - r[c["outcome_delay_s"]]) for r in done]
    base = [abs(r[c["cur_dev_s"]] - r[c["outcome_delay_s"]]) for r in done]
    mm = meta["model"]
    metrics = {
        "now": iso_msk(now), "mode": "replay", "source": "replay",
        "model_version": meta["model"].get("version"),
        "vehicles_live": sum(v["status"] == "live" for v in vehicles),
        "packets_per_min": packets, "last_packet_at": max((v["event_time"] for v in vehicles), default=None),
        "inference_latency_ms_p50": meta["model"].get("latency_ms_single"),
        "inference_latency_ms_p95": meta["model"].get("latency_ms_single"),
        "queue_lag_s": 0, "reconnects": 0,
        "mae_live_s": round(float(np.mean(err)), 1) if err else None,
        "mae_baseline_live_s": round(float(np.mean(base)), 1) if base else None,
        "n_verified": len(err), "n_verified_grid5": sum(r[c["as_of"]] % 300 == 0 for r in done),
        "offline_eval": {"mae_model": mm["mae_model"], "mae_cur_dev": mm["mae_cur_dev"], "n": mm["n_eval"],
                         "on": mm["eval_on"]} if mm.get("mae_model") is not None else None,
    }
    return {"vehicles": vehicles, "predictions": predictions, "incidents": incidents, "metrics": metrics}


def write_contract_examples(contract_dir: Path, replay: dict, now: int):
    snap = snapshot(replay, now)
    network = {
        "stops": [{"stop_key": s[0], "lat": s[1], "lon": s[2], "name": s[3]} for s in replay["stops"][:5]],
        "routes": [{**{k: v for k, v in r.items() if k != "segments"}, "segments": r["segments"][:2]}
                   for r in replay["routes"][:1]],
        "_comment": "Пример укорочен: полный ответ содержит все остановки и участки",
    }
    tr = str(replay["routes"][0]["tr_id"])
    v = replay["visits"][tr]
    schedule = {"tr_id": int(tr), "visits": [
        {"visit_id": v["id"][k], "stop_key": v["stop"][k], "time_plan": iso_msk(v["plan"][k]),
         "time_fact": iso_msk(v["fact"][k]) if v["fact"][k] and v["fact"][k] <= now else None}
        for k in range(min(6, len(v["id"])))]}
    # В примеры — самое показательное: активные инциденты и прогнозы с риском.
    preds = sorted(snap["predictions"], key=lambda p: -SEVERITY_ORDER[p["severity"]])
    incs = sorted(snap["incidents"], key=lambda i: (i["status"] != "active", -SEVERITY_ORDER[i["severity"]]))
    vehs = sorted(snap["vehicles"], key=lambda v: -SEVERITY_ORDER[v["severity"]])
    files = {"network.json": network, "schedule.json": schedule, "vehicles.json": vehs[:3],
             "predictions.json": preds[:2], "incidents.json": incs[:2], "metrics.json": snap["metrics"]}
    for name, obj in files.items():
        (contract_dir / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", str(here.parents[2] / "dataset")),
                    help="папка датасета (train/test/validate/labels); по умолчанию $DATA_DIR")
    ap.add_argument("--split", default="test", help="какой день воспроизводить (нужен факт в schedule.csv)")
    ap.add_argument("--train-split", default="train", help="на чём учить демо-модель")
    ap.add_argument("--predictions", help="CSV sample_id;prediction[;late_probability] для points_grid.csv")
    ap.add_argument("--grid-step", type=int, default=60, help="шаг выпуска прогнозов, с")
    ap.add_argument("--telemetry-step", type=float, default=10, help="прореживание телеметрии, с")
    ap.add_argument("--start", default="auto",
                    help="момент старта воспроизведения, 'YYYY-MM-DD HH:MM:SS' или auto (самый насыщенный)")
    ap.add_argument("--example-time", default="auto", help="момент для примеров контракта (auto = старт + 15 мин)")
    ap.add_argument("--out", default=str(here.parent / "data"))
    ap.add_argument("--contract-out", default=str(here.parent / "contract" / "examples"))
    build(ap.parse_args())


if __name__ == "__main__":
    main()
