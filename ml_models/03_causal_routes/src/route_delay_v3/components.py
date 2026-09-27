from __future__ import annotations

import numpy as np
import pandas as pd
from route_matching import Catalog

from .events import visible_events


def _median(frame: pd.DataFrame, key: str, value: str):
    if frame.empty:
        return {}, np.nan
    grouped = frame.groupby(key)[value].agg(["median", "count"])
    return grouped.to_dict("index"), float(frame[value].median())


def _estimate(groups, global_median: float, key: str, planned: float):
    value = groups.get(key)
    if value is None or not np.isfinite(global_median):
        return planned, False, 0
    count = int(value["count"])
    weight = count / (count + 20)
    return weight * float(value["median"]) + (1 - weight) * global_median, True, count


def add_components(
    frame: pd.DataFrame,
    runs: pd.DataFrame,
    dwells: pd.DataFrame,
    catalog: Catalog,
    excluded_trips: set[str] | None = None,
) -> pd.DataFrame:
    excluded_trips = excluded_trips or set()
    if excluded_trips:
        runs = runs[~runs.trip_occurrence_id.isin(excluded_trips)]
        dwells = dwells[~dwells.trip_occurrence_id.isin(excluded_trips)]
    sequences = {sequence.sequence_id: sequence for sequence in catalog.sequences}
    assignments = catalog.assignment_by_visit
    rows = []
    for point in frame.itertuples(index=False):
        now = point.T
        trip = getattr(point, "mm_trip_occurrence_id", "__unknown__")
        past_run = visible_events(runs, now, trip)
        past_dwell = visible_events(dwells, now, trip)
        run_stats, run_global = _median(past_run, "segment_id", "run_time_s")
        dwell_stats, dwell_global = _median(past_dwell, "stop_id", "dwell_time_s")
        sequence = sequences.get(getattr(point, "mm_sequence_id", ""))
        assignment = assignments.get(str(point.target_stop_id))
        target = assignment.stop_index if assignment else None
        try:
            current = int(point.mm_current_leg_index)
        except (ValueError, TypeError):
            current = None
        horizon = float(point.horizon_s)
        fallback = (
            sequence is None
            or current is None
            or target is None
            or current < 0
            or target <= current
            or target >= len(sequence.stops)
            or assignment.trip_occurrence_id != sequence.trip_occurrence_id
        )
        if fallback:
            rows.append(
                {
                    "sample_id": point.sample_id,
                    "component_eta_s": horizon,
                    "component_delay_s": float(point.cur_dev_s),
                    "component_run_s": horizon,
                    "component_dwell_s": 0.0,
                    "component_coverage": 0.0,
                    "component_support": 0.0,
                    "component_fallback": 1.0,
                    "component_run_events": float(len(past_run)),
                    "component_dwell_events": float(len(past_dwell)),
                    "component_history_age_s": np.nan,
                }
            )
            continue
        run_sum = dwell_sum = 0.0
        supported = support = 0
        legs = sequence.legs[current:target]
        for leg_offset, leg in enumerate(legs):
            planned = max(0.0, leg.end.time - leg.start.time)
            run, observed, n = _estimate(run_stats, run_global, leg.segment_id, planned)
            if leg_offset == 0:
                progress = getattr(point, "mm_segment_progress", np.nan)
                if pd.notna(progress):
                    run *= max(0.0, 1.0 - min(1.0, float(progress)))
            run_sum += run
            supported += int(observed)
            support += n
            if leg.index + 1 < target and observed:
                stop = sequence.stops[leg.index + 1]
                dwell, _, _ = _estimate(dwell_stats, dwell_global, stop.stop_id, 0.0)
                dwell_sum += dwell
        elapsed = getattr(point, "mm_current_dwell_elapsed_s", np.nan)
        if pd.notna(elapsed) and current < len(sequence.stops):
            stop_id = sequence.stops[current].stop_id
            conditional = past_dwell[
                (past_dwell.stop_id == stop_id) & (past_dwell.dwell_time_s >= elapsed)
            ]
            if len(conditional):
                dwell_sum += float(np.median(conditional.dwell_time_s - elapsed))
        eta = run_sum + dwell_sum
        if len(past_run):
            age = (now - past_run.available_at.max()).total_seconds()
        else:
            age = np.nan
        rows.append(
            {
                "sample_id": point.sample_id,
                "component_eta_s": eta,
                "component_delay_s": eta - horizon,
                "component_run_s": run_sum,
                "component_dwell_s": dwell_sum,
                "component_coverage": supported / len(legs),
                "component_support": float(support),
                "component_fallback": float(supported == 0),
                "component_run_events": float(len(past_run)),
                "component_dwell_events": float(len(past_dwell)),
                "component_history_age_s": age,
            }
        )
    return frame.merge(pd.DataFrame(rows), on="sample_id", how="left", validate="one_to_one")
