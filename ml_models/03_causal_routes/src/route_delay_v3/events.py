from __future__ import annotations

from itertools import pairwise

import numpy as np
import pandas as pd
from route_matching import Catalog
from route_matching.geo import distance, project

TIMEZONE = "Europe/Moscow"


def _local(epoch: float) -> pd.Timestamp:
    return pd.Timestamp(epoch, unit="s", tz="UTC").tz_convert(TIMEZONE).tz_localize(None)


def _visit(group: pd.DataFrame, stop, after: pd.Timestamp):
    planned = _local(stop.time)
    window = group[
        (group.event_time >= max(planned - pd.Timedelta(minutes=30), after))
        & (group.event_time <= planned + pd.Timedelta(minutes=30))
    ]
    if window.empty:
        return None
    entered = None
    stopped = None
    moving = 0
    last_time = None
    max_gap = 0.0
    seen_available = None
    for row in window.itertuples(index=False):
        if not row.location_valid or pd.isna(row.lon) or pd.isna(row.lat):
            continue
        if last_time is not None:
            gap = (row.event_time - last_time).total_seconds()
            if entered is not None:
                max_gap = max(max_gap, gap)
                if gap > 45:
                    return None
        last_time = row.event_time
        d = distance((float(row.lon), float(row.lat)), stop.coord)
        speed = float(row.speed) if pd.notna(row.speed) else np.inf
        if entered is None:
            if d <= 40:
                entered = row
                stopped = row if speed <= 3 else None
                seen_available = row.available_at
            continue
        seen_available = max(seen_available, row.available_at)
        if stopped is None and d <= 40 and speed <= 3:
            stopped = row
        if speed > 3 and d <= 60:
            moving += 1
        else:
            moving = 0
        if d >= 60 or moving >= 2:
            if row.event_time == entered.event_time:
                continue
            return {
                "arrival": entered.event_time,
                "departure": row.event_time if stopped is not None else entered.event_time,
                "available_at": seen_available,
                "pass_through": stopped is None,
                "max_gap_s": max_gap,
                "source": "gps_exit" if d >= 60 else "two_moving_packets",
            }
    return None


def _backtracked(passage: pd.DataFrame, path) -> bool:
    """Reject clear reverse motion, ignoring GPS fixes far from the planned path."""
    offsets = []
    for row in passage.itertuples(index=False):
        if pd.isna(row.lon) or pd.isna(row.lat):
            continue
        cursor = 0.0
        candidates = []
        for edge in path.edges:
            if len(edge.shape) >= 2:
                residual, offset, _, _ = project(edge.shape, (float(row.lon), float(row.lat)))
                candidates.append((residual, cursor + offset))
            cursor += edge.length_m
        if candidates:
            residual, offset = min(candidates)
            if residual <= 40:
                offsets.append(offset)
    return any(later < earlier - 75 for earlier, later in pairwise(offsets))


def build_events(traffic: pd.DataFrame, catalog: Catalog) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_vehicle = {
        int(vehicle): group.sort_values("event_time", kind="stable").reset_index(drop=True)
        for vehicle, group in traffic.groupby("tr_id")
    }
    run_rows = []
    dwell_rows = []
    for sequence in catalog.sequences:
        if sequence.ambiguous:
            continue
        group = by_vehicle.get(sequence.tr_id)
        if group is None or group.empty:
            continue
        visits = []
        after = pd.Timestamp.min
        for index, stop in enumerate(sequence.stops):
            found = _visit(group, stop, after)
            if found is None:
                visits.append(None)
                continue
            after = found["departure"] + pd.Timedelta(microseconds=1)
            found["stop_index"] = index
            visits.append(found)
            dwell_rows.append(
                {
                    "trip_occurrence_id": sequence.trip_occurrence_id,
                    "route_pattern_id": sequence.route_pattern_id,
                    "stop_id": stop.stop_id,
                    "visit_id": stop.visits[0].visit_id,
                    "stop_index": index,
                    "event_time": found["arrival"],
                    "available_at": found["available_at"],
                    "dwell_time_s": max(
                        0.0, (found["departure"] - found["arrival"]).total_seconds()
                    ),
                    "pass_through": found["pass_through"],
                    "max_gap_s": found["max_gap_s"],
                    "boundary_source": found["source"],
                }
            )
        for leg, start, end in zip(sequence.legs, visits, visits[1:]):
            if start is None or end is None or end["arrival"] <= start["departure"]:
                continue
            passage = group[
                (group.event_time >= start["departure"])
                & (group.event_time <= end["arrival"])
                & group.location_valid
            ]
            gaps = passage.event_time.diff().dt.total_seconds().dropna()
            max_gap = float(gaps.max()) if len(gaps) else np.inf
            duration = (end["arrival"] - start["departure"]).total_seconds()
            if max_gap > 45 or duration > 3600:
                continue
            segment = catalog.variants.get(leg.segment_id, ())
            if segment and _backtracked(passage, segment[0].path):
                continue
            passage_available = passage.available_at.max()
            run_rows.append(
                {
                    "trip_occurrence_id": sequence.trip_occurrence_id,
                    "route_pattern_id": sequence.route_pattern_id,
                    "segment_id": leg.segment_id,
                    "stop_id": leg.end.stop_id,
                    "event_time": start["departure"],
                    "available_at": max(
                        start["available_at"], end["available_at"], passage_available
                    ),
                    "run_time_s": duration,
                    "planned_interval_s": max(0.0, leg.end.time - leg.start.time),
                    "segment_length_m": segment[0].path.length_m if segment else np.nan,
                    "max_gap_s": max(max_gap, start["max_gap_s"], end["max_gap_s"]),
                    "boundary_source": end["source"],
                }
            )
    run = pd.DataFrame(run_rows)
    dwell = pd.DataFrame(dwell_rows)
    if not run.empty:
        run = (
            run.drop_duplicates(["trip_occurrence_id", "segment_id", "event_time"])
            .sort_values("available_at", kind="stable")
            .reset_index(drop=True)
        )
    if not dwell.empty:
        dwell = (
            dwell.drop_duplicates(["trip_occurrence_id", "visit_id", "event_time"])
            .sort_values("available_at", kind="stable")
            .reset_index(drop=True)
        )
    return run, dwell


def visible_events(events: pd.DataFrame, now: pd.Timestamp, trip_id: str | None):
    if events.empty:
        return events
    visible = events[events.available_at < now]
    if trip_id and trip_id != "__unknown__":
        visible = visible[visible.trip_occurrence_id != trip_id]
    return visible
