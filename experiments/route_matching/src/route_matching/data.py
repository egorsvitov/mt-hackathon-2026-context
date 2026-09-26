"""Whitelisted CSV and NDTP adapters. No factual arrival fields enter this module."""
from __future__ import annotations

from collections import defaultdict
import csv
from decimal import Decimal
from itertools import groupby, permutations
import math
from pathlib import Path
import re

from .geo import distance
from .types import Event, Leg, Sequence, StopGroup, Visit, digest, timestamp


def number(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def read_schedule(path: str | Path, timezone_name: str = "UTC") -> tuple[Visit, ...]:
    result = []
    with open(path, newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            coords = re.fullmatch(r"POINT\s*\(([-+\d.eE]+)\s+([-+\d.eE]+)\)", row["geom"])
            if not coords:
                raise ValueError(f"Invalid stop geometry: {row['geom']}")
            exact = tuple(str(Decimal(v).normalize()) for v in coords.groups())
            result.append(Visit(str(row["tt_action_item_id"]), int(row["tr_id"]),
                                timestamp(row["time_begin"], timezone_name), digest(exact),
                                tuple(map(float, exact)), row.get("building_address", "")))
    return tuple(sorted(set(result), key=lambda v: (v.tr_id, v.time, v.stop_id, v.visit_id)))


def read_events(path: str | Path, timezone_name: str = "UTC") -> list[Event]:
    result = []
    with open(path, newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            speed = number(row.get("speed"))
            heading = number(row.get("heading"))
            result.append(Event(
                int(row["tr_id"]), timestamp(row["event_time"], timezone_name),
                number(row.get("lon")), number(row.get("lat")),
                speed if speed is not None and 0 <= speed <= 150 else None,
                heading % 360 if heading is not None else None,
                row.get("location_valid", "").lower() == "true", row.get("packet_id", ""),
                timestamp(row["receive_time"], timezone_name) if row.get("receive_time") else None))
    return normalize_events(result)


def normalize_events(events) -> list[Event]:
    # Identical navigation events with different packet ids are also duplicates.
    unique = {e.key: e for e in sorted(events, key=lambda e: e.packet_id)}
    return sorted(unique.values(), key=lambda e: (e.event_time, e.tr_id, not e.valid,
                                                  e.lon or 0, e.lat or 0))


def from_traffic_row(row, timezone_name: str) -> Event:
    """Accept the existing parser dataclass or a mapping; timezone must be explicit."""
    def get(name, default=None):
        return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)
    if get("tr_id") is None:
        raise ValueError("Map unit_id to tr_id before matching")
    speed = number(get("speed"))
    valid = get("location_valid", False)
    if isinstance(valid, str):
        valid = valid.lower() == "true"
    return Event(int(get("tr_id")), timestamp(get("event_time"), timezone_name),
                 number(get("lon")), number(get("lat")),
                 speed if speed is not None and 0 <= speed <= 150 else None,
                 number(get("heading")), bool(valid), str(get("packet_id", "")),
                 timestamp(get("receive_time"), timezone_name) if get("receive_time") else None)


def real_vehicle_ids(plan: tuple[Visit, ...]) -> set[int]:
    # The released synthetic namespace is explicit, never inferred from labels or outcomes.
    return {v.tr_id for v in plan if not 9_000_000 <= v.tr_id < 10_000_000}


def build_sequences(plan: tuple[Visit, ...], max_sequences: int = 8) -> tuple[Sequence, ...]:
    """Beam expansion of tied times. Ranking is geometry-only, never visit ID order.

    Equal-coordinate visits collapse while preserving every visit alias. A finite
    beam bounds repeated tie groups; discarded orders remain marked ambiguous.
    """
    by_vehicle = defaultdict(list)
    for visit in plan:
        by_vehicle[visit.tr_id].append(visit)
    sequences = []
    for tr_id, visits in sorted(by_vehicle.items()):
        beams = [(0.0, ())]
        ambiguous = False
        for _, tied in groupby(sorted(visits, key=lambda v: v.time), key=lambda v: v.time):
            places = defaultdict(list)
            for visit in tied:
                places[visit.stop_id].append(visit)
            groups = [StopGroup(k, vs[0].coord, tuple(sorted(vs, key=lambda v: v.visit_id)))
                      for k, vs in sorted(places.items())]
            ambiguous |= len(groups) > 1
            if len(groups) > 6:
                raise ValueError("More than six simultaneous stops: supply a resolved schedule")
            expanded = []
            for cost, prefix in beams:
                for order in permutations(groups):
                    combined = list(prefix)
                    added = 0.0
                    for stop in order:
                        if combined and combined[-1].stop_id == stop.stop_id:
                            old = combined.pop()
                            combined.append(StopGroup(old.stop_id, old.coord, old.visits + stop.visits))
                        else:
                            if combined:
                                added += distance(combined[-1].coord, stop.coord)
                            combined.append(stop)
                    expanded.append((cost + added, tuple(combined)))
            # Different permutations can collapse to the same geometric sequence.
            unique = {}
            for cost, stops in expanded:
                key = tuple((s.stop_id, tuple(v.visit_id for v in s.visits)) for s in stops)
                if key not in unique or cost < unique[key][0]:
                    unique[key] = (cost, stops)
            beams = sorted(unique.values(), key=lambda b: (b[0], tuple(s.stop_id for s in b[1])))[:max_sequences]
        for _, stops in beams:
            legs = []
            for i, (start, end) in enumerate(zip(stops, stops[1:])):
                context = (stops[i - 1].stop_id if i else None, start.stop_id, end.stop_id,
                           stops[i + 2].stop_id if i + 2 < len(stops) else None)
                legs.append(Leg(i, digest(context), start, end))
            sequence_id = digest((tr_id, [(s.stop_id, [v.visit_id for v in s.visits]) for s in stops]))
            sequences.append(Sequence(sequence_id, tr_id, stops, tuple(legs), ambiguous))
    return tuple(sequences)


def read_points(path: str | Path, timezone_name: str = "UTC") -> list[dict]:
    with open(path, newline="", encoding="utf-8") as stream:
        return [dict(sample_id=r["sample_id"], tr_id=int(r["tr_id"]),
                     T=timestamp(r["T"], timezone_name), target_visit_id=r["target_stop_id"],
                     cur_dev_s=float(r["cur_dev_s"])) for r in csv.DictReader(stream)]
