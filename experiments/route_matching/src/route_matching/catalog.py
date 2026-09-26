"""Cutoff-safe road discovery, monotone stop alignment and observed-path medoids."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from statistics import median

from .data import build_sequences, normalize_events, real_vehicle_ids
from .geo import clip_path, distance, edit_distance, line, project
from .graph import GraphError, RoadGraph
from .types import (Config, Edge, Event, Leg, Passage, RoadPath, Sequence, StopGroup,
                    Variant, Visit, digest)


@dataclass(frozen=True)
class Catalog:
    version: str
    graph_version: str
    cutoff: float
    sequences: tuple[Sequence, ...]
    variants: dict[str, tuple[Variant, ...]]
    report: dict

    def save(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))

    @classmethod
    def load(cls, path: str | Path) -> Catalog:
        raw = json.loads(Path(path).read_text())

        def stop(s):
            return StopGroup(s["stop_id"], tuple(s["coord"]),
                             tuple(Visit(**{**v, "coord": tuple(v["coord"])}) for v in s["visits"]))

        def road(p):
            return RoadPath(tuple(Edge(**{**e, "shape": tuple(tuple(c) for c in e["shape"])})
                                  for e in p["edges"]))

        sequences = tuple(Sequence(s["sequence_id"], s["tr_id"],
                                   tuple(stop(p) for p in s["stops"]),
                                   tuple(Leg(l["index"], l["segment_id"], stop(l["start"]), stop(l["end"]))
                                         for l in s["legs"]), s["ambiguous"])
                          for s in raw["sequences"])
        variants = {key: tuple(Variant(**{**v, "path": road(v["path"]),
                                         "provenance": tuple(tuple(p) for p in v["provenance"])})
                               for v in vs) for key, vs in raw["variants"].items()}
        return cls(raw["version"], raw["graph_version"], raw["cutoff"], sequences,
                   variants, raw["report"])


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
            if (dt > config.gap_s or
                    distance(current[-1].coord, event.coord) > config.max_speed_kmh / 3.6 * dt + 50):
                if len(current) >= 2:
                    yield current
                current = []
        current.append(event)
        if len(current) >= 400:
            yield current
            current = current[-2:]
    if len(current) >= 2:
        yield current


def extract_passages(sequence: Sequence, events: list[Event], trace, config: Config) -> list[Passage]:
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
            if d <= config.stop_radius_m and distance(event.coord, stop.coord) <= config.stop_radius_m:
                cost = (d / config.stop_radius_m)**2 + ((event.event_time - stop.time) / 900)**2
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
        if any(p is None for p in offsets[begin:end + 1]):
            continue
        duration = events[end].event_time - events[begin].event_time
        length = offsets[end] - offsets[begin]
        if duration <= 0 or length / duration > config.max_speed_kmh / 3.6:
            continue
        path = clip_path(trace.path, offsets[begin], offsets[end])
        if not path.edges:
            continue
        residuals = [p.distance_m for p in trace.points[begin:end + 1] if p.distance_m is not None]
        quality = 1 / (1 + (median(residuals) if residuals else 100) / config.gps_sigma_m)
        available_at = max(event.available_time for event in events[begin:end + 1])
        passages.append(Passage(leg.segment_id, sequence.tr_id, leg.start.visits[-1].visit_id,
                                leg.end.visits[0].visit_id, events[begin].event_time,
                                events[end].event_time, available_at, path, quality))
    return passages


def consensus(passages: list[Passage], config: Config) -> tuple[Variant, ...]:
    unique = {}
    for passage in sorted(passages, key=lambda p: (-p.quality, p.path.signature)):
        unique.setdefault(passage.key, passage)
    clusters: list[list[Passage]] = []
    for passage in sorted(unique.values(), key=lambda p: (p.start_time, p.tr_id)):
        # Complete linkage avoids chains merging genuinely distinct detours.
        fitting = [c for c in clusters if all(edit_distance(passage.path.signature, p.path.signature)
                                            <= config.cluster_distance for p in c)]
        if fitting:
            fitting[0].append(passage)
        else:
            clusters.append([passage])
    result = []
    for cluster in clusters:
        medoid = min(cluster, key=lambda p: (sum(edit_distance(p.path.signature, q.path.signature)
                                                 for q in cluster), -p.quality, p.path.signature))
        result.append(Variant(digest((medoid.segment_id, medoid.path.signature)), medoid.path,
                              "observed", len(cluster), len(cluster) / len(unique),
                              tuple(sorted(p.key for p in cluster)),
                              max(p.available_at for p in cluster),
                              median(p.end_time - p.start_time for p in cluster)))
    return tuple(sorted(result, key=lambda v: (-v.support, v.variant_id)))


def build_catalog(plan: tuple[Visit, ...], history: list[Event], cutoff: float,
                  graph: RoadGraph, config: Config = Config()) -> Catalog:
    real = real_vehicle_ids(plan)
    plan = tuple(v for v in plan if v.tr_id in real)
    sequences = build_sequences(plan)
    # Filter FIRST: even future outliers and future deduplication cannot influence the catalog.
    allowed = normalize_events(e for e in history
                               if e.event_time < cutoff and e.available_time < cutoff
                               and e.tr_id in real)
    by_vehicle = defaultdict(list)
    for event in allowed:
        by_vehicle[event.tr_id].append(event)
    by_sequence = defaultdict(list)
    for sequence in sequences:
        by_sequence[sequence.tr_id].append(sequence)
    report = {"allowed_events": len(allowed), "trace_requests": 0, "trace_errors": 0,
              "route_errors": 0, "unknown_segments": 0, "passages": 0, "error_examples": []}
    all_passages = defaultdict(list)
    for tr_id, events in sorted(by_vehicle.items()):
        for chunk in fragments(events, config):
            # No planned anchors in/near the fragment: do not spend a graph call.
            if not any(chunk[0].event_time - 1800 <= v.time <= chunk[-1].event_time + 1800
                       for v in plan if v.tr_id == tr_id and v.time < cutoff):
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
                if passage.available_at >= cutoff:
                    raise AssertionError("Future trace entered catalog")
                all_passages[passage.segment_id].append(passage)
    variants = {key: consensus(ps, config) for key, ps in all_passages.items()}
    report["passages"] = sum(sum(v.support for v in vs) for vs in variants.values())
    route_cache = {}
    for sequence in sequences:
        for leg in sequence.legs:
            if leg.segment_id in variants:
                continue
            pair = leg.start.coord, leg.end.coord
            if pair not in route_cache:
                try:
                    route_cache[pair] = graph.routes(*pair)
                except GraphError as exc:
                    route_cache[pair] = ()
                    report["route_errors"] += 1
                    if len(report["error_examples"]) < 5:
                        report["error_examples"].append(str(exc))
            variants[leg.segment_id] = tuple(
                Variant(digest((leg.segment_id, path.signature)), path, "osm_hypothesis", 0, 0)
                for path in route_cache[pair] if path.edges)
    report["unknown_segments"] = sum(not v for v in variants.values())
    report["confirmed_segments"] = sum(any(v.confirmed for v in vs) for vs in variants.values())
    payload = {"graph": graph.version, "cutoff": cutoff, "config": asdict(config),
               "sequences": [asdict(s) for s in sequences],
               "variants": {k: [asdict(v) for v in vs] for k, vs in sorted(variants.items())}}
    return Catalog(digest(payload), graph.version, cutoff, sequences, variants, report)
