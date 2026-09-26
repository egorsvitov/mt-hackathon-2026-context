from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import (
    MAX_CONTIGUOUS_GAP_S,
    MAX_SPEED_KMH,
    PEER_MAX_AGE_S,
    PEER_RADII_M,
    WINDOW_SECONDS,
)

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


def _heading_difference(a: float, b: np.ndarray) -> np.ndarray:
    return np.abs((b - a + 180.0) % 360.0 - 180.0)


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
        speed = group["speed"].to_numpy(float)
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


def _window_features(
    history: VehicleHistory, t_ns: int, end: int, seconds: int
) -> dict[str, float]:
    start = int(np.searchsorted(history.times_ns, t_ns - seconds * 1_000_000_000, side="left"))
    idx = np.arange(start, end)
    valid_start = int(np.searchsorted(history.valid_indices, start, side="left"))
    valid_end = int(np.searchsorted(history.valid_indices, end, side="left"))
    valid_idx = history.valid_indices[valid_start:valid_end]
    speeds = history.speed[idx]
    finite_speed = speeds[np.isfinite(speeds)]
    prefix = f"w{seconds // 60}m_"
    out = {
        prefix + "packets": float(idx.size),
        prefix + "valid_gps_fraction": float(valid_idx.size / idx.size) if idx.size else np.nan,
        prefix + "speed_count": float(finite_speed.size),
        prefix + "speed_mean": float(np.mean(finite_speed)) if finite_speed.size else np.nan,
        prefix + "speed_median": float(np.median(finite_speed)) if finite_speed.size else np.nan,
        prefix + "speed_min": float(np.min(finite_speed)) if finite_speed.size else np.nan,
        prefix + "speed_max": float(np.max(finite_speed)) if finite_speed.size else np.nan,
        prefix + "speed_std": float(np.std(finite_speed)) if finite_speed.size else np.nan,
        prefix + "stopped_fraction": float(np.mean(finite_speed <= 1.0))
        if finite_speed.size
        else np.nan,
        prefix + "distance_m": np.nan,
        prefix + "target_approach_m": np.nan,
        prefix + "max_gap_s": np.nan,
    }
    if idx.size >= 2:
        out[prefix + "max_gap_s"] = float(np.max(np.diff(history.times_ns[idx])) / 1e9)
    if valid_idx.size >= 2:
        gaps = np.diff(history.times_ns[valid_idx]) / 1e9
        usable = gaps <= MAX_CONTIGUOUS_GAP_S
        distances = _haversine(
            history.lon[valid_idx[:-1]],
            history.lat[valid_idx[:-1]],
            history.lon[valid_idx[1:]],
            history.lat[valid_idx[1:]],
        )
        out[prefix + "distance_m"] = float(np.sum(distances[usable]))
    return out


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
        for seconds in WINDOW_SECONDS:
            if history:
                row.update(_window_features(history, t_ns, end, seconds))

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
                for seconds in WINDOW_SECONDS:
                    start = int(
                        np.searchsorted(history.times_ns, t_ns - seconds * 1e9, side="left")
                    )
                    valid_start = int(np.searchsorted(history.valid_indices, start, side="left"))
                    valid_end = int(np.searchsorted(history.valid_indices, end, side="left"))
                    valid = history.valid_indices[valid_start:valid_end]
                    if valid.size:
                        past_distance = float(
                            _haversine(
                                history.lon[valid[0]],
                                history.lat[valid[0]],
                                target.stop_lon,
                                target.stop_lat,
                            )
                        )
                        row[f"w{seconds // 60}m_target_approach_m"] = (
                            past_distance - row["distance_to_target_m"]
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

        # Fleet context from the last valid point of each other vehicle known by T.
        if last is not None:
            peer_lon, peer_lat, peer_speed, peer_heading, peer_age = [], [], [], [], []
            for peer_id, peer in histories.items():
                if peer_id == int(point.tr_id):
                    continue
                peer_end = int(np.searchsorted(peer.times_ns, t_ns, side="right"))
                peer_last = _last_valid(peer, peer_end) if peer_end else None
                if peer_last is None:
                    continue
                age = float((t_ns - peer.times_ns[peer_last]) / 1e9)
                if age <= PEER_MAX_AGE_S:
                    peer_lon.append(peer.lon[peer_last])
                    peer_lat.append(peer.lat[peer_last])
                    peer_speed.append(peer.speed[peer_last])
                    peer_heading.append(peer.heading[peer_last])
                    peer_age.append(age)
            if peer_lon:
                distances = _haversine(
                    row["last_lon"], row["last_lat"], np.array(peer_lon), np.array(peer_lat)
                )
                speeds, headings, ages = map(np.asarray, (peer_speed, peer_heading, peer_age))
                for radius in PEER_RADII_M:
                    for aligned in (False, True):
                        mask = distances <= radius
                        suffix = "_aligned" if aligned else ""
                        if aligned and np.isfinite(row["last_heading"]):
                            mask &= _heading_difference(row["last_heading"], headings) <= 45
                        elif aligned:
                            mask &= False
                        finite = mask & np.isfinite(speeds)
                        prefix = f"peers_{radius}m{suffix}_"
                        row[prefix + "count"] = float(np.sum(mask))
                        row[prefix + "speed_median"] = (
                            float(np.median(speeds[finite])) if finite.any() else np.nan
                        )
                        row[prefix + "speed_std"] = (
                            float(np.std(speeds[finite])) if finite.any() else np.nan
                        )
                        row[prefix + "age_max_s"] = (
                            float(np.max(ages[mask])) if mask.any() else np.nan
                        )
        rows.append(row)

    result = pd.DataFrame(rows)
    return result.sort_values("sample_id", kind="stable").reset_index(drop=True)


def feature_columns(
    frame: pd.DataFrame, groups: tuple[str, ...], include_vehicle: bool = False
) -> list[str]:
    base = [
        "cur_dev_s",
        "cur_dev_abs_s",
        "horizon_s",
        "time_sin",
        "time_cos",
        "hour",
        "last_speed",
        "last_heading",
        "has_history",
        "has_valid_gps",
        "packet_age_s",
        "gps_age_s",
    ]
    columns = list(base)
    if "dynamics" in groups:
        columns += [c for c in frame if c.startswith("w")]
    if "geography" in groups:
        columns += [
            c
            for c in (
                "last_lon",
                "last_lat",
                "target_lon",
                "target_lat",
                "distance_to_target_m",
                "previous_stop_lon",
                "previous_stop_lat",
                "target_leg_planned_s",
                "remaining_visits",
                "schedule_progress",
                "geo_lon_cell",
                "geo_lat_cell",
            )
            if c in frame
        ]
    if "peers" in groups:
        columns += [c for c in frame if c.startswith("peers_")]
    if include_vehicle:
        columns.append("tr_id")
    return list(dict.fromkeys(c for c in columns if c in frame.columns))
