"""One causal implementation for CSV replay and online event-by-event processing."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from statistics import median
from time import perf_counter

from .catalog import Catalog
from .graph import RoadGraph
from .matcher import Matcher
from .types import DEFAULT_CONFIG, Config, Event, MatchState


def replay(
    events: list[Event],
    prediction_points: list[dict],
    catalog: Catalog,
    graph: RoadGraph | None = None,
    config: Config = DEFAULT_CONFIG,
    mode: str = "hmm",
) -> tuple[list[tuple[dict, MatchState]], dict]:
    """Reveal only packets available by T. Input ordering cannot change the result."""
    points = sorted(prediction_points, key=lambda p: (p["T"], p["sample_id"]))
    relevant = {int(p["tr_id"]) for p in points}
    warmup_start = points[0]["T"] - config.window_s if points else float("-inf")
    stream = sorted(
        (e for e in events if e.tr_id in relevant and e.event_time >= warmup_start),
        key=lambda e: (e.available_time, e.event_time, e.tr_id, e.packet_id),
    )
    matcher = Matcher(catalog, graph, config, mode)
    output, latencies = [], []
    cursor = 0
    started = perf_counter()
    try:
        for point in points:
            while cursor < len(stream) and stream[cursor].available_time <= point["T"]:
                tick = perf_counter()
                matcher.update(stream[cursor])
                latencies.append((perf_counter() - tick) * 1000)
                cursor += 1
            state = matcher.snapshot(
                int(point["tr_id"]),
                float(point["T"]),
                str(point["target_visit_id"]),
                point.get("cur_dev_s"),
            )
            output.append((point, state))
        elapsed = perf_counter() - started
        ordered = sorted(latencies)
        percentile = lambda q: (
            ordered[min(len(ordered) - 1, int(q * len(ordered)))] if ordered else None
        )
        report = {
            "mode": mode,
            "events": cursor,
            "points": len(output),
            "seconds": elapsed,
            "events_per_second": cursor / elapsed if elapsed else None,
            "latency_ms_p50": median(latencies) if latencies else None,
            "latency_ms_p95": percentile(0.95),
            "matched_fraction": (
                sum(s.mode != "unmatched" for _, s in output) / len(output) if output else None
            ),
            "route_fraction": (
                sum(s.mode == "route" for _, s in output) / len(output) if output else None
            ),
            "confirmed_fraction": (
                sum(s.route_quality == "confirmed" for _, s in output) / len(output)
                if output
                else None
            ),
            "fallback_fraction": (
                sum(s.mode == "road" for _, s in output) / len(output) if output else None
            ),
            "matcher_metrics": dict(matcher.metrics),
        }
        progress = [state.route_progress for _, state in output if state.route_progress is not None]
        report["route_progress_rows"] = len(progress)
        report["mean_route_distance_m"] = (
            sum(state.route_distance_m for _, state in output if state.route_distance_m is not None)
            / sum(state.route_distance_m is not None for _, state in output)
            if any(state.route_distance_m is not None for _, state in output)
            else None
        )
        return output, report
    finally:
        matcher.close()


def write_results(results: list[tuple[dict, MatchState]], path: str | Path):
    with Path(path).open("w", encoding="utf-8") as stream:
        stream.writelines(
            json.dumps(
                {
                    "sample_id": point["sample_id"],
                    "tr_id": point["tr_id"],
                    "T": point["T"],
                    "target_visit_id": point["target_visit_id"],
                    "state": asdict(state),
                    "features": state.feature_row(),
                },
                allow_nan=False,
            )
            + "\n"
            for point, state in results
        )


def read_feature_rows(path: str | Path) -> list[dict]:
    rows = []
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            raw = json.loads(line)
            rows.append({"sample_id": raw["sample_id"], **raw["features"]})
    return rows
