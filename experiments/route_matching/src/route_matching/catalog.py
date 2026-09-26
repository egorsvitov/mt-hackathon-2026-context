"""Cutoff-safe road discovery, monotone stop alignment and observed-path medoids."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from statistics import median

from .data import discover_sequences, normalize_events, real_vehicle_ids
from .geo import clip_path, distance, edit_distance, line, project
from .graph import GraphError, RoadGraph
from .types import (
    DEFAULT_CONFIG,
    Config,
    Edge,
    Event,
    Leg,
    Passage,
    RoadPath,
    RoutePattern,
    Sequence,
    StopGroup,
    Variant,
    Visit,
    VisitAssignment,
    digest,
)


@dataclass(frozen=True)
class Catalog:
    version: str
    graph_version: str
    cutoff: float | None
    sequences: tuple[Sequence, ...]
    variants: dict[str, tuple[Variant, ...]]
    report: dict
    schema_version: int = 2
    patterns: tuple[RoutePattern, ...] = ()
    assignments: tuple[VisitAssignment, ...] = ()
    provenance: dict | None = None

    def save(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))

    @classmethod
    def load(cls, path: str | Path) -> Catalog:
        raw = json.loads(Path(path).read_text())

        def stop(s):
            return StopGroup(
                s["stop_id"],
                tuple(s["coord"]),
                tuple(Visit(**{**v, "coord": tuple(v["coord"])}) for v in s["visits"]),
            )

        def road(p):
            return RoadPath(
                tuple(
                    Edge(**{**e, "shape": tuple(tuple(c) for c in e["shape"])}) for e in p["edges"]
                )
            )

        sequences = tuple(
            Sequence(
                s["sequence_id"],
                s["tr_id"],
                tuple(stop(p) for p in s["stops"]),
                tuple(
                    Leg(l["index"], l["segment_id"], stop(l["start"]), stop(l["end"]))
                    for l in s["legs"]
                ),
                s.get("ambiguous", False),
                s.get("route_pattern_id"),
                s.get("direction_id"),
                s.get("trip_occurrence_id"),
            )
            for s in raw["sequences"]
        )
        variants = {
            key: tuple(
                Variant(
                    **{
                        **v,
                        "path": road(v["path"]),
                        "provenance": tuple(tuple(p) for p in v["provenance"]),
                    }
                )
                for v in vs
            )
            for key, vs in raw["variants"].items()
        }
        patterns = tuple(
            RoutePattern(
                **{
                    **p,
                    "stop_ids": tuple(p["stop_ids"]),
                    "trip_occurrence_ids": tuple(p["trip_occurrence_ids"]),
                }
            )
            for p in raw.get("patterns", ())
        )
        assignments = tuple(VisitAssignment(**a) for a in raw.get("assignments", ()))
        return cls(
            raw["version"],
            raw["graph_version"],
            raw.get("cutoff"),
            sequences,
            variants,
            raw["report"],
            raw.get("schema_version", 1),
            patterns,
            assignments,
            raw.get("provenance") or {},
        )

    @property
    def assignment_by_visit(self) -> dict[str, VisitAssignment]:
        return {assignment.visit_id: assignment for assignment in self.assignments}

    @property
    def sequence_by_trip(self) -> dict[str, Sequence]:
        return {
            sequence.trip_occurrence_id: sequence
            for sequence in self.sequences
            if sequence.trip_occurrence_id
        }


def fragments(history: list[Event], config: Config):
    """Yield only continuous valid fragments; no interpolation across outages/jumps."""
    current = []
    for event in history:
        if not event.valid:
            continue
        if current:
            dt = event.event_time - current[-1].event_time
            if dt <= 0:
                continue
            if (
                dt > config.gap_s
                or distance(current[-1].coord, event.coord) > config.max_speed_kmh / 3.6 * dt + 50
            ):
                if len(current) >= 2:
                    yield current
                current = []
        current.append(event)
        if len(current) >= 400:
            yield current
            current = current[-2:]
    if len(current) >= 2:
        yield current


def extract_passages(
    sequence: Sequence, events: list[Event], trace, config: Config
) -> list[Passage]:
    if len(trace.points) != len(events) or not trace.path.edges:
        return []
    prefix = [0.0]
    for edge in trace.path.edges:
        prefix.append(prefix[-1] + edge.length_m)
    offsets = []
    for match in trace.points:
        if match.edge_index is None or match.coord is None or match.discontinuity:
            offsets.append(None)
        else:
            _, local, _, _ = project(line(trace.path.edges[match.edge_index].shape), match.coord)
            offsets.append(prefix[match.edge_index] + local)
    # Monotone dynamic programming, including an explicit skip state for missed stops.
    states = [(0.0, -1, ())]
    considered = []
    for si, stop in enumerate(sequence.stops):
        if stop.time < events[0].event_time - 1800 or stop.time > events[-1].event_time + 1800:
            continue
        considered.append(si)
        candidates = []
        for j, (event, match, offset) in enumerate(zip(events, trace.points, offsets)):
            if offset is None or match.coord is None or abs(event.event_time - stop.time) > 1800:
                continue
            d = distance(stop.coord, match.coord)
            # Both raw and snapped observations must support the stop anchor.
            if (
                d <= config.stop_radius_m
                and distance(event.coord, stop.coord) <= config.stop_radius_m
            ):
                cost = (d / config.stop_radius_m) ** 2 + ((event.event_time - stop.time) / 900) ** 2
                candidates.append((cost, j))
        candidates = sorted(candidates)[:12]
        next_states = [(cost + 4, j, anchors + ((si, None),)) for cost, j, anchors in states]
        for cost, previous, anchors in states:
            for local_cost, j in candidates:
                if j > previous and (previous < 0 or offsets[j] >= (offsets[previous] or 0)):
                    next_states.append((cost + local_cost, j, anchors + ((si, j),)))
        # Retain the best predecessor for each event index, rather than duplicates.
        best = {}
        for candidate in sorted(next_states, key=lambda s: s[0]):
            best.setdefault(candidate[1], candidate)
        states = sorted(best.values(), key=lambda s: s[0])[:64]
    if not considered:
        return []
    anchors = dict(min(states, key=lambda s: s[0])[2])
    passages = []
    for leg in sequence.legs:
        begin, end = anchors.get(leg.index), anchors.get(leg.index + 1)
        if begin is None or end is None or end <= begin or offsets[end] <= offsets[begin]:
            continue
        if any(p is None for p in offsets[begin : end + 1]):
            continue
        duration = events[end].event_time - events[begin].event_time
        length = offsets[end] - offsets[begin]
        if duration <= 0 or length / duration > config.max_speed_kmh / 3.6:
            continue
        path = _anchor_path(
            clip_path(trace.path, offsets[begin], offsets[end]), leg.start.coord, leg.end.coord
        )
        if not path.edges:
            continue
        residuals = [
            p.distance_m for p in trace.points[begin : end + 1] if p.distance_m is not None
        ]
        quality = 1 / (1 + (median(residuals) if residuals else 100) / config.gps_sigma_m)
        available_at = max(event.available_time for event in events[begin : end + 1])
        passages.append(
            Passage(
                leg.segment_id,
                sequence.tr_id,
                leg.start.visits[-1].visit_id,
                leg.end.visits[0].visit_id,
                events[begin].event_time,
                events[end].event_time,
                available_at,
                path,
                quality,
                sequence.trip_occurrence_id,
            )
        )
    return passages


def consensus(passages: list[Passage], config: Config) -> tuple[Variant, ...]:
    unique = {}
    for passage in sorted(passages, key=lambda p: (-p.quality, p.path.signature)):
        unique.setdefault(passage.key, passage)
    clusters: list[list[Passage]] = []
    for passage in sorted(unique.values(), key=lambda p: (p.start_time, p.tr_id)):
        # Complete linkage avoids chains merging genuinely distinct detours.
        fitting = [
            c
            for c in clusters
            if all(
                edit_distance(passage.path.signature, p.path.signature) <= config.cluster_distance
                for p in c
            )
        ]
        if fitting:
            fitting[0].append(passage)
        else:
            clusters.append([passage])
    result = []
    for cluster in clusters:
        medoid = min(
            cluster,
            key=lambda p: (
                sum(edit_distance(p.path.signature, q.path.signature) for q in cluster),
                -p.quality,
                p.path.signature,
            ),
        )
        result.append(
            Variant(
                digest((medoid.segment_id, medoid.path.signature)),
                medoid.path,
                "observed",
                len(cluster),
                len(cluster) / len(unique),
                tuple(sorted(p.key for p in cluster)),
                max(p.available_at for p in cluster),
                median(p.end_time - p.start_time for p in cluster),
            )
        )
    return tuple(sorted(result, key=lambda v: (-v.support, v.variant_id)))


def _anchor_path(path: RoadPath, start, end) -> RoadPath:
    """Keep Valhalla road-centre geometry intact.

    Physical stop coordinates are observations, not road vertices. Replacing a
    routed edge endpoint with a stop creates diagonal coloured chords across
    junctions, so stops stay separate and paths retain projected endpoints.
    """
    del start, end
    return path


def _path_offset(path: RoadPath, coord, minimum: float = 0.0) -> float | None:
    cursor = 0.0
    choices = []
    for edge in path.edges:
        residual, local, _, _ = project(line(edge.shape), coord)
        offset = cursor + local
        if offset + 5 >= minimum:
            choices.append((residual, offset))
        cursor += edge.length_m
    return min(choices)[1] if choices else None


def _split_pattern_path(sequence: Sequence, path: RoadPath) -> dict[str, RoadPath]:
    offsets = []
    minimum = 0.0
    for stop in sequence.stops:
        offset = _path_offset(path, stop.coord, minimum)
        if offset is None:
            return {}
        offsets.append(offset)
        minimum = offset
    result = {}
    for leg, start, end in zip(sequence.legs, offsets, offsets[1:]):
        if end <= start:
            continue
        result[leg.segment_id] = _anchor_path(
            clip_path(path, start, end), leg.start.coord, leg.end.coord
        )
    return result


def _osm_pattern_paths(
    sequence: Sequence, graph: RoadGraph, report: dict
) -> dict[str, tuple[RoadPath, ...]]:
    route_through = getattr(graph, "route_through", None)
    if route_through is not None:
        try:
            path = route_through(tuple(stop.coord for stop in sequence.stops))
            split = _split_pattern_path(sequence, path)
            if len(split) == len(sequence.legs):
                return {key: (value,) for key, value in split.items()}
        except GraphError as exc:
            report["route_errors"] += 1
            if len(report["error_examples"]) < 5:
                report["error_examples"].append(str(exc))
    result = {}
    for leg in sequence.legs:
        try:
            result[leg.segment_id] = tuple(
                _anchor_path(path, leg.start.coord, leg.end.coord)
                for path in graph.routes(leg.start.coord, leg.end.coord)
                if path.edges
            )
        except GraphError as exc:
            result[leg.segment_id] = ()
            report["route_errors"] += 1
            if len(report["error_examples"]) < 5:
                report["error_examples"].append(str(exc))
    return result


def build_catalog(
    plan: tuple[Visit, ...],
    history: list[Event],
    cutoff: float | None,
    graph: RoadGraph,
    config: Config = DEFAULT_CONFIG,
    *,
    excluded_trip_ids: set[str] | None = None,
    provenance: dict | None = None,
) -> Catalog:
    real = real_vehicle_ids(plan)
    plan = tuple(v for v in plan if v.tr_id in real)
    sequences, patterns, assignments = discover_sequences(plan, config)
    excluded_trip_ids = excluded_trip_ids or set()
    sequences = tuple(s for s in sequences if s.trip_occurrence_id not in excluded_trip_ids)
    allowed_visits = {v.visit_id for s in sequences for stop in s.stops for v in stop.visits}
    assignments = tuple(a for a in assignments if a.visit_id in allowed_visits)
    used_pattern_ids = {s.route_pattern_id for s in sequences}
    patterns = tuple(p for p in patterns if p.route_pattern_id in used_pattern_ids)
    # Filter FIRST: even future outliers and future deduplication cannot influence the catalog.
    allowed = normalize_events(
        e
        for e in history
        if e.tr_id in real
        and (cutoff is None or (e.event_time < cutoff and e.available_time < cutoff))
    )
    by_vehicle = defaultdict(list)
    for event in allowed:
        by_vehicle[event.tr_id].append(event)
    by_sequence = defaultdict(list)
    for sequence in sequences:
        by_sequence[sequence.tr_id].append(sequence)
    report = {
        "allowed_events": len(allowed),
        "trace_requests": 0,
        "trace_errors": 0,
        "route_errors": 0,
        "unknown_segments": 0,
        "passages": 0,
        "error_examples": [],
    }
    all_passages = defaultdict(list)
    for tr_id, events in sorted(by_vehicle.items()):
        events = sorted(
            events, key=lambda event: (event.event_time, event.available_time, event.packet_id)
        )
        for chunk in fragments(events, config):
            # No planned anchors in/near the fragment: do not spend a graph call.
            if not any(
                chunk[0].event_time - 1800 <= v.time <= chunk[-1].event_time + 1800
                for v in plan
                if v.tr_id == tr_id and (cutoff is None or v.time < cutoff)
            ):
                continue
            report["trace_requests"] += 1
            try:
                trace = graph.trace(chunk, timeout=30)
            except GraphError as exc:
                report["trace_errors"] += 1
                if len(report["error_examples"]) < 5:
                    report["error_examples"].append(str(exc))
                continue
            options = [extract_passages(s, chunk, trace, config) for s in by_sequence[tr_id]]
            # GPS disambiguates schedule tie order; equivalent expansions are not new support.
            best = max(options, key=lambda ps: (len(ps), sum(p.quality for p in ps)), default=[])
            for passage in best:
                if cutoff is not None and passage.available_at >= cutoff:
                    raise AssertionError("Future trace entered catalog")
                all_passages[passage.segment_id].append(passage)
    variants = {key: consensus(ps, config) for key, ps in all_passages.items()}
    passages_by_trip = defaultdict(list)
    for passages in all_passages.values():
        for passage in passages:
            if passage.trip_occurrence_id:
                passages_by_trip[passage.trip_occurrence_id].append(passage)
    geometric_medoids = {}
    updated_patterns = []
    for pattern in patterns:
        signatures = {}
        for trip_id in pattern.trip_occurrence_ids:
            passages = sorted(passages_by_trip.get(trip_id, ()), key=lambda p: p.start_time)
            if passages:
                signatures[trip_id] = tuple(edge for p in passages for edge in p.path.signature)
        if signatures:
            medoid = min(
                signatures,
                key=lambda trip_id: (
                    sum(edit_distance(signatures[trip_id], other) for other in signatures.values()),
                    -len(signatures[trip_id]),
                    trip_id,
                ),
            )
            geometric_medoids[pattern.route_pattern_id] = medoid
            updated_patterns.append(replace(pattern, canonical_trip_id=medoid))
            for passage in passages_by_trip[medoid]:
                choices = variants.get(passage.segment_id, ())
                variants[passage.segment_id] = tuple(
                    sorted(
                        choices,
                        key=lambda variant: (
                            passage.key not in variant.provenance,
                            -variant.support,
                            variant.variant_id,
                        ),
                    )
                )
        else:
            updated_patterns.append(pattern)
    patterns = tuple(updated_patterns)
    report["passages"] = sum(sum(v.support for v in vs) for vs in variants.values())
    canonical_by_pattern = {}
    for pattern in patterns:
        options = [s for s in sequences if s.route_pattern_id == pattern.route_pattern_id]
        canonical_by_pattern[pattern.route_pattern_id] = min(
            options,
            key=lambda s: (s.trip_occurrence_id != pattern.canonical_trip_id, s.sequence_id),
        )
    for sequence in canonical_by_pattern.values():
        missing = [leg for leg in sequence.legs if leg.segment_id not in variants]
        if not missing:
            continue
        routed = _osm_pattern_paths(sequence, graph, report)
        for leg in missing:
            variants[leg.segment_id] = tuple(
                Variant(digest((leg.segment_id, path.signature)), path, "osm_hypothesis", 0, 0)
                for path in routed.get(leg.segment_id, ())
                if path.edges
            )
    pair_cache = {}
    for sequence in sequences:
        for leg in sequence.legs:
            if leg.segment_id in variants:
                continue
            pair = leg.start.coord, leg.end.coord
            if pair not in pair_cache:
                try:
                    pair_cache[pair] = graph.routes(*pair)
                except GraphError as exc:
                    pair_cache[pair] = ()
                    report["route_errors"] += 1
                    if len(report["error_examples"]) < 5:
                        report["error_examples"].append(str(exc))
            variants[leg.segment_id] = tuple(
                Variant(
                    digest((leg.segment_id, path.signature)),
                    _anchor_path(path, leg.start.coord, leg.end.coord),
                    "osm_hypothesis",
                    0,
                    0,
                )
                for path in pair_cache[pair]
                if path.edges
            )
    report["unknown_segments"] = sum(not v for v in variants.values())
    report["confirmed_segments"] = sum(any(v.confirmed for v in vs) for vs in variants.values())
    provenance = {
        "policy": "offline_history",
        "vehicle_ids": sorted(real),
        "trip_occurrence_ids": sorted(s.trip_occurrence_id for s in sequences),
        "excluded_trip_occurrence_ids": sorted(excluded_trip_ids),
        "history_min_available_time": min((e.available_time for e in allowed), default=None),
        "history_max_available_time": max((e.available_time for e in allowed), default=None),
        **(provenance or {}),
    }
    report.update(
        patterns=len(patterns),
        trip_occurrences=len(sequences),
        assignments=len(assignments),
        pattern_geometric_medoids=geometric_medoids,
    )
    payload = {
        "schema_version": 2,
        "graph": graph.version,
        "cutoff": cutoff,
        "config": asdict(config),
        "provenance": provenance,
        "sequences": [asdict(s) for s in sequences],
        "variants": {k: [asdict(v) for v in vs] for k, vs in sorted(variants.items())},
    }
    return Catalog(
        digest(payload),
        graph.version,
        cutoff,
        sequences,
        variants,
        report,
        2,
        patterns,
        assignments,
        provenance,
    )
