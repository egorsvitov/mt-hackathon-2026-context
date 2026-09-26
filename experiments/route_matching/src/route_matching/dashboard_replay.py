"""Patch the dashboard's static replay with matched positions and catalog geometry."""

from __future__ import annotations

import json
from pathlib import Path

from .catalog import Catalog
from .geo import distance
from .matcher import Matcher
from .types import Event

_PREFIX = "window.REPLAY_DATA="


def _read_replay(path: Path) -> dict:
    text = path.read_text(encoding="utf-8").strip()
    if not text.startswith(_PREFIX) or not text.endswith(";"):
        raise ValueError("Expected replay.js in window.REPLAY_DATA={...}; format")
    return json.loads(text[len(_PREFIX) : -1])


def write_matched_dashboard_replay(
    catalog: Catalog,
    network_path: str | Path,
    input_path: str | Path,
    output_path: str | Path,
    mode: str = "hmm",
) -> dict:
    """Replace embedded routes and snap causal replay positions to the route catalog."""
    if mode not in {"nearest", "hmm"}:
        raise ValueError("Dashboard replay supports nearest or hmm mode")
    data = _read_replay(Path(input_path))
    network = json.loads(Path(network_path).read_text(encoding="utf-8"))
    data["stops"] = [
        [stop["stop_key"], stop["lat"], stop["lon"], stop.get("name", "")]
        for stop in network["stops"]
    ]
    data["routes"] = network["routes"]

    t0 = float(data["meta"]["t0"])
    records = []
    for tr_text, track in data["telemetry"].items():
        tr_id = int(tr_text)
        count = len(track["t"])
        if not all(len(track[name]) == count for name in ("lat", "lon", "spd", "hdg")):
            raise ValueError(f"Telemetry arrays have different lengths for tr_id={tr_id}")
        track["raw_lat"] = list(track["lat"])
        track["raw_lon"] = list(track["lon"])
        track["mm_matched"] = [0] * count
        track["mm_route_pattern_id"] = [None] * count
        for index in range(count):
            records.append((t0 + float(track["t"][index]), tr_id, index, track))

    matcher = Matcher(catalog, graph=None, mode=mode)
    matched = 0
    route_residuals = []
    try:
        for event_time, tr_id, index, track in sorted(records):
            raw_lon = float(track["raw_lon"][index]) / 1e5
            raw_lat = float(track["raw_lat"][index]) / 1e5
            state = matcher.update(
                Event(
                    tr_id,
                    event_time,
                    raw_lon,
                    raw_lat,
                    float(track["spd"][index]),
                    float(track["hdg"][index]),
                    True,
                    f"dashboard:{tr_id}:{index}",
                )
            )
            fresh_route_match = (
                state.mode == "route"
                and state.max_event_time == event_time
                and state.matched_lon is not None
                and state.matched_lat is not None
            )
            if not fresh_route_match:
                continue
            track["lon"][index] = round(state.matched_lon * 1e5)
            track["lat"][index] = round(state.matched_lat * 1e5)
            track["mm_matched"][index] = 1
            track["mm_route_pattern_id"][index] = state.route_pattern_id
            matched += 1
            route_residuals.append(
                distance((raw_lon, raw_lat), (state.matched_lon, state.matched_lat))
            )
    finally:
        matcher.close()

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        _PREFIX + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    route_residuals.sort()
    percentile = lambda q: (
        route_residuals[min(len(route_residuals) - 1, int(q * len(route_residuals)))]
        if route_residuals
        else None
    )
    return {
        "events": len(records),
        "matched": matched,
        "matched_fraction": matched / len(records) if records else None,
        "residual_m_p50": percentile(0.5),
        "residual_m_p95": percentile(0.95),
        "routes": len(network["routes"]),
        "output": str(output),
    }
