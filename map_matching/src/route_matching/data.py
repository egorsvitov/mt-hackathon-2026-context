"""Whitelisted CSV and NDTP adapters. No factual arrival fields enter this module."""

from __future__ import annotations

import csv
import math
import re
from collections import defaultdict
from decimal import Decimal
from itertools import groupby, pairwise, permutations
from pathlib import Path

from .geo import angle_difference, distance, edit_distance, project
from .types import (
    DEFAULT_CONFIG,
    Config,
    Event,
    Leg,
    RoutePattern,
    Sequence,
    StopGroup,
    Visit,
    VisitAssignment,
    digest,
    timestamp,
)


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
            result.append(
                Visit(
                    str(row["tt_action_item_id"]),
                    int(row["tr_id"]),
                    timestamp(row["time_begin"], timezone_name),
                    digest(exact),
                    tuple(map(float, exact)),
                    row.get("building_address", ""),
                )
            )
    return tuple(sorted(set(result), key=lambda v: (v.tr_id, v.time, v.stop_id, v.visit_id)))


def read_events(path: str | Path, timezone_name: str = "UTC") -> list[Event]:
    result = []
    with open(path, newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            speed = number(row.get("speed"))
            heading = number(row.get("heading"))
            result.append(
                Event(
                    int(row["tr_id"]),
                    timestamp(row["event_time"], timezone_name),
                    number(row.get("lon")),
                    number(row.get("lat")),
                    speed if speed is not None and 0 <= speed <= 150 else None,
                    heading % 360 if heading is not None else None,
                    row.get("location_valid", "").lower() == "true",
                    row.get("packet_id", ""),
                    timestamp(row["receive_time"], timezone_name)
                    if row.get("receive_time")
                    else None,
                )
            )
    return normalize_events(result)


def normalize_events(events) -> list[Event]:
    """Deduplicate without allowing a later packet to replace an earlier visible one."""
    events = list(events)
    by_packet = {}
    for event in sorted(events, key=lambda e: (e.available_time, e.event_time, e.packet_id)):
        packet_key = (event.tr_id, event.packet_id) if event.packet_id else None
        if packet_key is not None and packet_key in by_packet:
            continue
        if packet_key is not None:
            by_packet[packet_key] = event
    candidates = list(by_packet.values())
    candidates.extend(event for event in events if not event.packet_id)
    by_navigation = {}
    for event in sorted(candidates, key=lambda e: (e.available_time, e.packet_id)):
        by_navigation.setdefault(event.key, event)
    return sorted(
        by_navigation.values(),
        key=lambda e: (
            e.available_time,
            e.event_time,
            e.tr_id,
            not e.valid,
            e.lon or 0,
            e.lat or 0,
        ),
    )


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
    event_time = get("event_time", get("timestamp"))
    if event_time is None:
        raise ValueError("Telemetry record has no event_time or timestamp")
    receive_time = get("receive_time")
    # Backend records are available when ingest() is called. Replay uses event-time
    # ordering, so missing receive_time is represented by the event time itself.
    receive_time = event_time if receive_time is None else receive_time
    return Event(
        int(get("tr_id")),
        timestamp(event_time, timezone_name),
        number(get("lon")),
        number(get("lat")),
        speed if speed is not None and 0 <= speed <= 150 else None,
        number(get("heading")),
        bool(valid),
        str(get("packet_id", "")),
        timestamp(receive_time, timezone_name),
    )


def real_vehicle_ids(plan: tuple[Visit, ...]) -> set[int]:
    # The released synthetic namespace is explicit, never inferred from labels or outcomes.
    return {v.tr_id for v in plan if not 9_000_000 <= v.tr_id < 10_000_000}


def _physical_visits(plan: tuple[Visit, ...], radius_m: float) -> tuple[Visit, ...]:
    """Assign stable physical-stop ids with deterministic three-metre clustering."""
    unique = sorted({visit.coord for visit in plan})
    representatives: list[tuple[float, float]] = []
    mapping = {}
    for coord in unique:
        matches = [
            (distance(coord, representative), i)
            for i, representative in enumerate(representatives)
            if distance(coord, representative) <= radius_m
        ]
        if matches:
            representative = representatives[min(matches)[1]]
        else:
            representative = coord
            representatives.append(coord)
        mapping[coord] = digest(("physical_stop", representative))
    return tuple(
        Visit(v.visit_id, v.tr_id, v.time, mapping[v.coord], v.coord, v.address) for v in plan
    )


def _ordered_stops(visits: list[Visit]) -> tuple[list[StopGroup], bool]:
    groups_by_time = []
    ambiguous = False
    for _, tied in groupby(
        sorted(visits, key=lambda v: (v.time, v.stop_id, v.visit_id)), key=lambda v: v.time
    ):
        places = defaultdict(list)
        for visit in tied:
            places[visit.stop_id].append(visit)
        groups = [
            StopGroup(key, rows[0].coord, tuple(sorted(rows, key=lambda v: v.visit_id)))
            for key, rows in sorted(places.items())
        ]
        ambiguous |= len(groups) > 1
        if len(groups) > 6:
            raise ValueError("More than six simultaneous stops: supply a resolved schedule")
        groups_by_time.append(groups)

    ordered: list[StopGroup] = []
    for index, groups in enumerate(groups_by_time):
        if len(groups) == 1:
            ordered.extend(groups)
            continue
        next_coords = (
            [g.coord for g in groups_by_time[index + 1]] if index + 1 < len(groups_by_time) else []
        )
        ranked = []
        for order in permutations(groups):
            cost = sum(distance(a.coord, b.coord) for a, b in pairwise(order))
            if ordered:
                cost += distance(ordered[-1].coord, order[0].coord)
            if next_coords:
                cost += min(distance(order[-1].coord, coord) for coord in next_coords)
            ranked.append((cost, tuple(stop.stop_id for stop in order), order))
        ordered.extend(min(ranked)[2])
    return ordered, ambiguous


def _heading(a, b) -> float:
    return project((a, b), b)[3]


def _split_trips(stops: list[StopGroup], config: Config) -> list[tuple[StopGroup, ...]]:
    if len(stops) < 2:
        return []
    boundaries: list[tuple[int, bool]] = []
    for index in range(1, len(stops)):
        previous, current = stops[index - 1], stops[index]
        gap = current.time - previous.time
        if gap >= config.trip_gap_s:
            boundaries.append((index, False))
            continue
        if previous.stop_id == current.stop_id and gap >= config.terminal_pause_s:
            boundaries.append((index, False))
    for index in range(1, len(stops) - 1):
        current = stops[index]
        incoming = _heading(stops[index - 1].coord, current.coord)
        outgoing = _heading(current.coord, stops[index + 1].coord)
        terminal_evidence = len(current.visits) > 1 or (
            current.time - stops[index - 1].time >= config.terminal_pause_s
        )
        if terminal_evidence and angle_difference(incoming, outgoing) >= 110:
            boundaries.append((index, True))

    result = []
    start = 0
    for boundary, shared in sorted(set(boundaries)):
        end = boundary + 1 if shared else boundary
        if end - start >= 2:
            result.append(tuple(stops[start:end]))
        start = boundary
    if len(stops) - start >= 2:
        result.append(tuple(stops[start:]))
    return result or [tuple(stops)]


def discover_sequences(
    plan: tuple[Visit, ...], config: Config = DEFAULT_CONFIG
) -> tuple[tuple[Sequence, ...], tuple[RoutePattern, ...], tuple[VisitAssignment, ...]]:
    """Discover directional trip occurrences and shared route patterns."""
    plan = _physical_visits(plan, config.physical_stop_radius_m)
    by_vehicle = defaultdict(list)
    for visit in plan:
        by_vehicle[visit.tr_id].append(visit)
    raw = []
    for tr_id, visits in sorted(by_vehicle.items()):
        ordered, _ = _ordered_stops(visits)
        for ordinal, stops in enumerate(_split_trips(ordered, config)):
            ambiguous = len({stop.time for stop in stops}) != len(stops)
            trip_id = digest(
                (
                    "trip",
                    tr_id,
                    ordinal,
                    stops[0].time,
                    stops[-1].time,
                    tuple(s.stop_id for s in stops),
                )
            )
            raw.append((trip_id, tr_id, stops, ambiguous))

    clusters: list[list[tuple]] = []
    for item in raw:
        stop_ids = tuple(stop.stop_id for stop in item[2])
        candidates = []
        for cluster in clusters:
            reference = tuple(stop.stop_id for stop in cluster[0][2])
            if edit_distance(stop_ids, reference) <= config.pattern_edit_distance:
                candidates.append(cluster)
        if candidates:
            candidates[0].append(item)
        else:
            clusters.append([item])

    sequences = []
    patterns = []
    assignments = []
    for cluster in clusters:
        canonical = min(
            cluster,
            key=lambda item: (
                sum(
                    edit_distance(
                        tuple(s.stop_id for s in item[2]), tuple(s.stop_id for s in other[2])
                    )
                    for other in cluster
                ),
                item[0],
            ),
        )
        canonical_ids = tuple(stop.stop_id for stop in canonical[2])
        pattern_id = digest(("route_pattern", canonical_ids))
        direction_id = digest(("direction", canonical_ids[:3], canonical_ids[-3:]))
        trip_ids = tuple(sorted(item[0] for item in cluster))
        patterns.append(
            RoutePattern(pattern_id, direction_id, canonical_ids, canonical[0], trip_ids)
        )
        for trip_id, tr_id, stops, ambiguous in cluster:
            legs = []
            for index, (start, end) in enumerate(pairwise(stops)):
                segment_id = digest(("segment", pattern_id, index, start.stop_id, end.stop_id))
                legs.append(Leg(index, segment_id, start, end))
            sequence_id = digest(("sequence", trip_id, pattern_id))
            sequences.append(
                Sequence(
                    sequence_id,
                    tr_id,
                    stops,
                    tuple(legs),
                    ambiguous,
                    pattern_id,
                    direction_id,
                    trip_id,
                )
            )
            for stop_index, stop in enumerate(stops):
                assignments.extend(
                    VisitAssignment(
                        v.visit_id, tr_id, trip_id, pattern_id, direction_id, stop_index
                    )
                    for v in stop.visits
                )
    unique_assignments = {}
    for assignment in sorted(
        assignments, key=lambda a: (a.tr_id, a.visit_id, a.stop_index == 0, a.trip_occurrence_id)
    ):
        unique_assignments.setdefault(assignment.visit_id, assignment)
    return (
        tuple(sorted(sequences, key=lambda s: (s.tr_id, s.start_time, s.sequence_id))),
        tuple(sorted(patterns, key=lambda p: p.route_pattern_id)),
        tuple(sorted(unique_assignments.values(), key=lambda a: (a.tr_id, a.visit_id))),
    )


