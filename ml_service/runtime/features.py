from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

MAX_SPEED_KMH = 150.0

EARTH_RADIUS_M = 6_371_000.0
META_COLUMNS = ["sample_id", "tr_id", "T", "target_stop_id", "target_time_begin", "cur_dev_s"]


@dataclass(frozen=True)
class VehicleHistory:
    times_ns: np.ndarray
    lon: np.ndarray
    lat: np.ndarray
    speed: np.ndarray
    heading: np.ndarray
    valid_gps: np.ndarray
    valid_indices: np.ndarray


def _haversine(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(np.radians, (lon1, lat1, lon2, lat2))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _histories(telemetry: pd.DataFrame) -> dict[int, VehicleHistory]:
    result = {}
    for tr_id, group in telemetry.groupby("tr_id", sort=False):
        duplicate_key = [c for c in ("tr_id", "event_time", "packet_id") if c in group]
        group = group.drop_duplicates(duplicate_key, keep="first").sort_values(
            "event_time", kind="stable"
        )
        gps = (
            group["location_valid"].to_numpy(dtype=bool)
            & group["lon"].between(30, 45).to_numpy()
            & group["lat"].between(50, 60).to_numpy()
        )
        speed = group["speed"].to_numpy(float, copy=True)
        speed[(speed < 0) | (speed > MAX_SPEED_KMH)] = np.nan
        result[int(tr_id)] = VehicleHistory(
            group["event_time"].astype("int64").to_numpy(),
            group["lon"].to_numpy(float),
            group["lat"].to_numpy(float),
            speed,
            group["heading"].to_numpy(float),
            gps,
            np.flatnonzero(gps),
        )
    return result


def _schedule_lookup(schedule: pd.DataFrame):
    target = schedule.set_index("tt_action_item_id", drop=False)
    by_vehicle = {}
    target_position = {}
    for tr_id, group in schedule.groupby("tr_id", sort=False):
        g = group.sort_values(["time_begin", "tt_action_item_id"], kind="stable").reset_index(
            drop=True
        )
        by_vehicle[int(tr_id)] = g
        target_position.update({int(v): i for i, v in enumerate(g["tt_action_item_id"])})
    return target, by_vehicle, target_position


def _last_valid(history: VehicleHistory, end: int) -> int | None:
    position = int(np.searchsorted(history.valid_indices, end, side="left")) - 1
    return int(history.valid_indices[position]) if position >= 0 else None


def build_features(
    points: pd.DataFrame,
    telemetry: pd.DataFrame,
    schedule_plan: pd.DataFrame,
) -> pd.DataFrame:
    """Build one causal feature row per point without reading factual schedule fields."""
    required = set(META_COLUMNS)
    if missing := required - set(points.columns):
        raise ValueError(f"points missing columns: {sorted(missing)}")
    horizons = (points["target_time_begin"] - points["T"]).dt.total_seconds()
    if not ((horizons > 600) & (horizons <= 900)).all():
        bad = points.loc[~((horizons > 600) & (horizons <= 900)), "sample_id"].tolist()[:5]
        raise ValueError(f"Forecast horizon outside (600, 900] seconds: {bad}")

    histories = _histories(telemetry)
    target_rows, schedules, target_positions = _schedule_lookup(schedule_plan)
    rows: list[dict[str, object]] = []

    for point in points.sort_values(["T", "sample_id"], kind="stable").itertuples(index=False):
        t_ns = pd.Timestamp(point.T).value
        row = {name: getattr(point, name) for name in META_COLUMNS}
        row["horizon_s"] = float((point.target_time_begin - point.T).total_seconds())
        minute = point.T.hour * 60 + point.T.minute + point.T.second / 60
        row["time_sin"] = math.sin(2 * math.pi * minute / 1440)
        row["time_cos"] = math.cos(2 * math.pi * minute / 1440)
        row["hour"] = float(point.T.hour)
        row["cur_dev_abs_s"] = abs(float(point.cur_dev_s))

        history = histories.get(int(point.tr_id))
        end = int(np.searchsorted(history.times_ns, t_ns, side="right")) if history else 0
        row["has_history"] = float(bool(end))
        row["packet_age_s"] = float((t_ns - history.times_ns[end - 1]) / 1e9) if end else np.nan
        last = _last_valid(history, end) if end else None
        row["has_valid_gps"] = float(last is not None)
        row["gps_age_s"] = (
            float((t_ns - history.times_ns[last]) / 1e9) if last is not None else np.nan
        )
        for name in ("last_lon", "last_lat", "last_speed", "last_heading"):
            row[name] = np.nan
        if last is not None:
            row.update(
                last_lon=float(history.lon[last]),
                last_lat=float(history.lat[last]),
                last_speed=float(history.speed[last]),
                last_heading=float(history.heading[last]),
            )

        target = (
            target_rows.loc[int(point.target_stop_id)]
            if int(point.target_stop_id) in target_rows.index
            else None
        )
        row.update(
            target_lon=np.nan,
            target_lat=np.nan,
            distance_to_target_m=np.nan,
            previous_stop_lon=np.nan,
            previous_stop_lat=np.nan,
            target_leg_planned_s=np.nan,
            remaining_visits=np.nan,
            schedule_progress=np.nan,
            geo_lon_cell=np.nan,
            geo_lat_cell=np.nan,
        )
        if target is not None:
            row["target_lon"], row["target_lat"] = float(target.stop_lon), float(target.stop_lat)
            row["geo_lon_cell"] = math.floor(float(target.stop_lon) * 200) / 200
            row["geo_lat_cell"] = math.floor(float(target.stop_lat) * 200) / 200
            if last is not None:
                row["distance_to_target_m"] = float(
                    _haversine(
                        history.lon[last], history.lat[last], target.stop_lon, target.stop_lat
                    )
                )
            vehicle_schedule = schedules.get(int(point.tr_id))
            pos = target_positions.get(int(point.target_stop_id))
            if vehicle_schedule is not None and pos is not None and pos > 0:
                prev = vehicle_schedule.iloc[pos - 1]
                row["previous_stop_lon"], row["previous_stop_lat"] = (
                    float(prev.stop_lon),
                    float(prev.stop_lat),
                )
                row["target_leg_planned_s"] = float(
                    (target.time_begin - prev.time_begin).total_seconds()
                )
                planned_now = point.T - pd.to_timedelta(float(point.cur_dev_s), unit="s")
                current_pos = int(
                    np.searchsorted(
                        vehicle_schedule["time_begin"].astype("int64").to_numpy(),
                        planned_now.value,
                        side="right",
                    )
                    - 1
                )
                row["remaining_visits"] = float(max(pos - current_pos, 0))
                if last is not None:
                    leg = float(
                        _haversine(prev.stop_lon, prev.stop_lat, target.stop_lon, target.stop_lat)
                    )
                    if leg > 1:
                        from_prev = float(
                            _haversine(
                                prev.stop_lon, prev.stop_lat, history.lon[last], history.lat[last]
                            )
                        )
                        row["schedule_progress"] = from_prev / leg

        rows.append(row)

    result = pd.DataFrame(rows)
    return result.sort_values("sample_id", kind="stable").reset_index(drop=True)
