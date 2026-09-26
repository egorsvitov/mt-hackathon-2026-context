from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from route_matching import Catalog
from transport_delay.features import _haversine

from .config import MAX_EVENT_GAP_S, STOP_INNER_M, STOP_OUTER_M, STOPPED_KMH, TIMEZONE


@dataclass(frozen=True)
class StopEvent:
    visit_id: str
    stop_id: str
    stop_index: int
    arrival: pd.Timestamp
    departure: pd.Timestamp
    pass_through: bool
    distance_m: float
    max_gap_s: float


def _naive_local(epoch: float) -> pd.Timestamp:
    return pd.Timestamp(epoch, unit="s", tz="UTC").tz_convert(TIMEZONE).tz_localize(None)


def _event_for_stop(group: pd.DataFrame, stop, stop_index: int) -> StopEvent | None:
    planned = _naive_local(stop.time)
    window = group[
        (group["event_time"] >= planned - pd.Timedelta(minutes=30))
        & (group["event_time"] <= planned + pd.Timedelta(minutes=30))
        & group["location_valid"]
        & group["lon"].notna()
        & group["lat"].notna()
    ].copy()
    if window.empty:
        return None
    distance = _haversine(
        window["lon"].to_numpy(float),
        window["lat"].to_numpy(float),
        stop.coord[0],
        stop.coord[1],
    )
    inside = np.flatnonzero(distance <= STOP_OUTER_M)
    if not len(inside):
        return None
    nearest_local = int(inside[np.argmin(distance[inside])])
    times = window["event_time"].reset_index(drop=True)
    speeds = window["speed"].reset_index(drop=True)
    inner_stopped = np.flatnonzero(
        (distance <= STOP_INNER_M)
        & speeds.fillna(np.inf).to_numpy(float).__le__(STOPPED_KMH)
    )
    pass_through = not len(inner_stopped)
    if pass_through:
        arrival_i = departure_i = nearest_local
    else:
        closest = int(inner_stopped[np.argmin(np.abs(inner_stopped - nearest_local))])
        run = [closest]
        i = closest - 1
        while i >= 0 and distance[i] <= STOP_OUTER_M and speeds.iloc[i] <= STOPPED_KMH:
            run.insert(0, i)
            i -= 1
        i = closest + 1
        while i < len(window) and distance[i] <= STOP_OUTER_M and speeds.iloc[i] <= STOPPED_KMH:
            run.append(i)
            i += 1
        arrival_i = run[0]
        departure_i = min(run[-1] + 1, len(window) - 1)
    lo, hi = max(0, arrival_i - 1), min(len(times), departure_i + 2)
    gaps = times.iloc[lo:hi].diff().dt.total_seconds().dropna()
    max_gap = float(gaps.max()) if len(gaps) else 0.0
    if max_gap > MAX_EVENT_GAP_S:
        return None
    visit_id = stop.visits[0].visit_id
    return StopEvent(
        visit_id,
        stop.stop_id,
        stop_index,
        times.iloc[arrival_i],
        times.iloc[departure_i],
        pass_through,
        float(distance[nearest_local]),
        max_gap,
    )


def build_component_events(
    telemetry: pd.DataFrame, catalog: Catalog, split: str
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Create supervised component events; future points define labels, never features."""
    by_vehicle = {int(k): v.reset_index(drop=True) for k, v in telemetry.groupby("tr_id")}
    dwell_rows, run_rows = [], []
    seen_visits = set()
    for sequence in catalog.sequences:
        group = by_vehicle.get(sequence.tr_id)
        if group is None or sequence.trip_occurrence_id is None:
            continue
        detected: list[StopEvent | None] = []
        previous_arrival = None
        for index, stop in enumerate(sequence.stops):
            event = _event_for_stop(group, stop, index)
            if event and previous_arrival is not None and event.arrival <= previous_arrival:
                event = None
            if event:
                previous_arrival = event.arrival
                key = (split, event.visit_id)
                if key not in seen_visits:
                    seen_visits.add(key)
                    dwell_rows.append(
                        {
                            "split": split,
                            "event_id": f"{split}:dwell:{event.visit_id}",
                            "tr_id": sequence.tr_id,
                            "trip_occurrence_id": sequence.trip_occurrence_id,
                            "route_pattern_id": sequence.route_pattern_id or "__unknown__",
                            "direction_id": sequence.direction_id or "__unknown__",
                            "stop_id": event.stop_id,
                            "visit_id": event.visit_id,
                            "stop_index": event.stop_index,
                            "event_time": event.arrival,
                            "available_at": event.departure,
                            "hour": event.arrival.hour + event.arrival.minute / 60,
                            "distance_m": event.distance_m,
                            "max_gap_s": event.max_gap_s,
                            "pass_through": float(event.pass_through),
                            "dwell_time_s": max(
                                0.0, (event.departure - event.arrival).total_seconds()
                            ),
                        }
                    )
            detected.append(event)
        for leg, start, end in zip(sequence.legs, detected, detected[1:]):
            if start is None or end is None or end.arrival < start.departure:
                continue
            duration = (end.arrival - start.departure).total_seconds()
            if duration <= 0 or duration > 3600:
                continue
            variants = catalog.variants.get(leg.segment_id, ())
            length = variants[0].path.length_m if variants else np.nan
            planned = max(0.0, leg.end.time - leg.start.time)
            before = group[
                (group["event_time"] <= start.departure)
                & (group["event_time"] > start.departure - pd.Timedelta(minutes=5))
            ]["speed"]
            run_rows.append(
                {
                    "split": split,
                    "event_id": f"{split}:run:{sequence.trip_occurrence_id}:{leg.index}",
                    "tr_id": sequence.tr_id,
                    "trip_occurrence_id": sequence.trip_occurrence_id,
                    "route_pattern_id": sequence.route_pattern_id or "__unknown__",
                    "direction_id": sequence.direction_id or "__unknown__",
                    "segment_id": leg.segment_id,
                    "event_time": start.departure,
                    "available_at": end.arrival,
                    "hour": start.departure.hour + start.departure.minute / 60,
                    "segment_length_m": length,
                    "planned_interval_s": planned,
                    "recent_speed_kmh": float(before.median()) if before.notna().any() else np.nan,
                    "endpoint_distance_m": max(start.distance_m, end.distance_m),
                    "max_gap_s": max(start.max_gap_s, end.max_gap_s),
                    "run_time_s": duration,
                }
            )
    run = pd.DataFrame(run_rows).drop_duplicates("event_id")
    dwell = pd.DataFrame(dwell_rows).drop_duplicates("event_id")
    report = {
        "split": split,
        "run_events": len(run),
        "dwell_events": len(dwell),
        "routes_with_run": int(run["route_pattern_id"].nunique()) if len(run) else 0,
        "routes_with_dwell": int(dwell["route_pattern_id"].nunique()) if len(dwell) else 0,
        "trips_with_run": int(run["trip_occurrence_id"].nunique()) if len(run) else 0,
        "pass_through_fraction": float(dwell["pass_through"].mean()) if len(dwell) else None,
    }
    return run, dwell, report

