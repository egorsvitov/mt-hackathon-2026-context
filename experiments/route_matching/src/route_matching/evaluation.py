"""Comparable diagnostics for nearest, Meili and route-constrained HMM outputs."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import median


def evaluate_result_file(path: str | Path) -> dict:
    rows = []
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            rows.append(json.loads(line))
    states = [row["state"] for row in rows]
    distances = sorted(
        s["route_distance_m"] for s in states if s.get("route_distance_m") is not None
    )
    by_vehicle = defaultdict(list)
    for row in rows:
        by_vehicle[str(row["tr_id"])].append(row)
    backwards = comparisons = 0
    for vehicle_rows in by_vehicle.values():
        previous = None
        previous_trip = None
        for row in sorted(vehicle_rows, key=lambda r: (r["T"], r["sample_id"])):
            state = row["state"]
            progress = state.get("route_progress")
            trip = state.get("trip_occurrence_id")
            if progress is not None and previous is not None and trip == previous_trip:
                comparisons += 1
                backwards += progress < previous - 0.01
            if progress is not None:
                previous, previous_trip = progress, trip
    count = len(rows)
    return {
        "rows": count,
        "matched_fraction": sum(s.get("mode") != "unmatched" for s in states) / count
        if count
        else None,
        "route_fraction": sum(s.get("mode") == "route" for s in states) / count if count else None,
        "fallback_fraction": sum(s.get("mode") == "road" for s in states) / count
        if count
        else None,
        "confirmed_fraction": sum(s.get("route_quality") == "confirmed" for s in states) / count
        if count
        else None,
        "distance_m_p50": median(distances) if distances else None,
        "distance_m_p95": distances[min(len(distances) - 1, int(0.95 * len(distances)))]
        if distances
        else None,
        "backtrack_fraction": backwards / comparisons if comparisons else None,
        "route_eta_coverage": sum(s.get("estimated_remaining_time_s") is not None for s in states)
        / count
        if count
        else None,
    }


def compare_result_files(inputs: dict[str, Path]) -> dict:
    return {name: evaluate_result_file(path) for name, path in sorted(inputs.items())}
