"""Bounded streaming Viterbi. Published snapshots never undergo future smoothing."""
from __future__ import annotations

from collections import defaultdict, deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, replace
import math
from threading import BoundedSemaphore

from .catalog import Catalog
from .geo import angle_difference, distance, joinable, line, project, xy
from .graph import GraphError, RoadGraph
from .types import Config, Event, MatchState, Sequence, Variant


@dataclass(frozen=True)
class Candidate:
    sequence: Sequence
    leg_index: int
    variant: Variant
    edge_index: int
    offset_m: float
    path_m: float
    coord: tuple[float, float]
    distance_m: float
    heading: float
    score: float = 0.0
    # Per-hypothesis travel history; switching the winner does not create fake speeds.
    trail: tuple[tuple[float, float], ...] = ()

    @property
    def key(self):
        return (self.sequence.sequence_id, self.leg_index, self.variant.variant_id,
                self.edge_index)


@dataclass(frozen=True)
class Frame:
    time: float
    gps_time: float | None
    beam: tuple[Candidate, ...]
    state: MatchState


class Matcher:
    def __init__(self, catalog: Catalog, graph: RoadGraph | None = None,
                 config: Config = Config(), mode: str = "hmm", live: bool = False):
        if graph and graph.version != catalog.graph_version:
            raise ValueError("Graph/catalog version mismatch")
        if mode not in {"hmm", "nearest", "meili"}:
            raise ValueError("Unknown matcher mode")
        self.catalog, self.graph, self.config, self.mode, self.live = catalog, graph, config, mode, live
        self._frames = defaultdict(deque)
        self._events = defaultdict(deque)
        self._last = {}
        self._gps = {}
        self._beam = {}
        self._published = OrderedDict()
        self._off = {}
        self._outside = defaultdict(list)
        self._inside = defaultdict(list)
        self._reference = {}
        self._entries = defaultdict(list)
        geometries = defaultdict(list)
        self._grid = defaultdict(lambda: defaultdict(set))
        self._sequences = {s.sequence_id: s for s in catalog.sequences}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="meili")
        self._slots = BoundedSemaphore(2)
        self.metrics = defaultdict(int)
        for sequence in catalog.sequences:
            for leg in sequence.legs:
                for variant in catalog.variants.get(leg.segment_id, ()):
                    cursor = 0.0
                    for index, edge in enumerate(variant.path.edges):
                        if edge.length_m > 0:
                            entry_index = len(self._entries[sequence.tr_id])
                            self._entries[sequence.tr_id].append((sequence, leg.index, variant, index, cursor))
                            geometry = line(edge.shape)
                            geometries[sequence.tr_id].append(geometry)
                            points = [xy(p) for p in geometry]
                            radius = self.config.radius_m
                            cell = self.config.radius_m
                            x0 = math.floor((min(p[0] for p in points) - radius) / cell)
                            x1 = math.floor((max(p[0] for p in points) + radius) / cell)
                            y0 = math.floor((min(p[1] for p in points) - radius) / cell)
                            y1 = math.floor((max(p[1] for p in points) + radius) / cell)
                            for gx in range(x0, x1 + 1):
                                for gy in range(y0, y1 + 1):
                                    self._grid[sequence.tr_id][gx, gy].add(entry_index)
                        cursor += edge.length_m
        self._geometries = geometries
        self._transition_cache = {}

    def close(self):
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _empty(self, tr_id, T, reason=None):
        return MatchState(tr_id, T, self.catalog.version, self.catalog.graph_version, reason=reason)

    def _candidates(self, event: Event) -> list[Candidate]:
        if event.tr_id not in self._geometries:
            return []
        x, y = xy(event.coord)
        cell = self.config.radius_m
        indices = self._grid[event.tr_id].get((math.floor(x / cell), math.floor(y / cell)), ())
        result = []
        for index in indices:
            sequence, leg, variant, edge, cursor = self._entries[event.tr_id][index]
            d, offset, coord, heading = project(self._geometries[event.tr_id][index], event.coord)
            if d > self.config.radius_m:
                continue
            score = -0.5 * (d / self.config.gps_sigma_m)**2
            if event.speed is not None and event.speed >= self.config.heading_min_kmh and event.heading is not None:
                score -= 0.5 * (angle_difference(event.heading, heading) / self.config.heading_sigma_deg)**2
            result.append(Candidate(sequence, leg, variant, edge, offset, cursor + offset,
                                    coord, d, heading, score))
        return result

    def _between(self, a: Candidate, b: Candidate) -> float | None:
        if a.sequence.sequence_id != b.sequence.sequence_id:
            return None
        if a.leg_index == b.leg_index:
            return b.path_m - a.path_m if a.variant.variant_id == b.variant.variant_id else None
        if not a.leg_index < b.leg_index <= a.leg_index + 32:
            return None
        key = (a.sequence.sequence_id, a.leg_index, a.variant.variant_id,
               b.leg_index, b.variant.variant_id)
        if key not in self._transition_cache:
            frontier = [(a.variant.path.edges[-1], 0.0)]
            for leg in a.sequence.legs[a.leg_index + 1:b.leg_index]:
                next_frontier = {}
                for variant in self.catalog.variants.get(leg.segment_id, ()):
                    for edge, length in frontier:
                        if joinable(edge, variant.path.edges[0]):
                            last = variant.path.edges[-1]
                            k = last.edge_id, last.target_fraction
                            value = length + variant.path.length_m
                            if k not in next_frontier or value < next_frontier[k][1]:
                                next_frontier[k] = last, value
                frontier = list(next_frontier.values())
                if not frontier:
                    break
            lengths = [length for edge, length in frontier if joinable(edge, b.variant.path.edges[0])]
            self._transition_cache[key] = min(lengths) if lengths else None
            if len(self._transition_cache) > 50000:
                self._transition_cache.clear()
                self._transition_cache[key] = min(lengths) if lengths else None
        middle = self._transition_cache[key]
        return (a.variant.path.length_m - a.path_m + middle + b.path_m) if middle is not None else None

    def _extend(self, candidates, previous, event, last_gps):
        output = []
        dt = event.event_time - last_gps.event_time if last_gps else 0
        measured = distance(event.coord, last_gps.coord) if last_gps else 0
        for candidate in candidates:
            if not previous or self.mode == "nearest":
                leg = candidate.sequence.legs[candidate.leg_index]
                expected = leg.start.time + (leg.end.time - leg.start.time) * (
                    candidate.path_m / max(candidate.variant.path.length_m, 1))
                # A schedule prior disambiguates repeated laps; bounded so GPS can override it.
                prior = min(8.0, 0.5 * ((event.event_time - expected) / self.config.schedule_sigma_s)**2)
                output.append(replace(candidate, score=candidate.score - prior,
                                      trail=((event.event_time, 0.0),)))
                continue
            best = None
            for old in previous:
                delta = self._between(old, candidate)
                if (delta is None or delta < -self.config.backtrack_m or dt <= 0 or
                        delta > self.config.max_speed_kmh / 3.6 * dt + self.config.backtrack_m):
                    continue
                penalty = abs(max(0, delta) - measured) / self.config.transition_beta_m
                penalty += max(0, -delta) / self.config.backtrack_m
                score = old.score + candidate.score - penalty
                if best is None or score > best.score:
                    trail = old.trail + ((event.event_time, old.trail[-1][1] + max(0, delta)),)
                    # Retain the boundary observation for an honest time-weighted speed.
                    while len(trail) > 2 and trail[1][0] < event.event_time - self.config.window_s:
                        trail = trail[1:]
                    best = replace(candidate, score=score, trail=trail)
            if best is not None:
                output.append(best)
        output.sort(key=lambda c: (-c.score, c.key))
        if not output:
            return ()
        top = output[0].score
        return tuple(replace(c, score=c.score - top) for c in output[:self.config.beam])

    def _road(self, event: Event) -> MatchState | None:
        if not self.graph or not self._slots.acquire(blocking=False):
            self.metrics["fallback_unavailable"] += 1
            return None
        events = list(self._events[event.tr_id])
        if len(events) < 2:
            self._slots.release()
            return None
        self.metrics["fallback_requests"] += 1
        future = self._pool.submit(self.graph.trace, events, self.config.fallback_timeout_s)
        future.add_done_callback(lambda _: self._slots.release())
        try:
            trace = future.result(timeout=self.config.fallback_timeout_s)
            if len(trace.points) != len(events):
                raise GraphError("Trace result/input cardinality mismatch")
            point = trace.points[-1]
            if point.coord is None or point.edge_index is None or point.discontinuity:
                return None
            edge = trace.path.edges[point.edge_index]
            residual = distance(event.coord, point.coord)
            if residual > self.config.radius_m:
                return None
            _, offset, _, _ = project(line(edge.shape), point.coord)
            return replace(self._empty(event.tr_id, event.event_time), mode="road",
                           edge_id=edge.edge_id, edge_occurrence=f"meili:{point.edge_index}",
                           edge_offset_m=offset, lon=point.coord[0], lat=point.coord[1],
                           position_quality="medium", max_event_time=event.event_time, gps_age_s=0)
        except (GraphError, TimeoutError, OSError) as exc:
            self.metrics["fallback_errors"] += 1
            self.last_graph_error = str(exc)
            return None

    def _route_state(self, event: Event, beam: tuple[Candidate, ...], chosen=None):
        c = chosen or beam[0]
        leg = c.sequence.legs[c.leg_index]
        # Identical road positions on schedule aliases are route ambiguity, not GPS ambiguity.
        competitors = [b for b in beam if b.key != c.key]
        margin = c.score - max((b.score for b in competitors), default=c.score - 10)
        speeds = {}
        for seconds in (60, 180, 300):
            points = [p for p in c.trail if p[0] >= event.event_time - seconds]
            speeds[seconds] = ((points[-1][1] - points[0][1]) / (points[-1][0] - points[0][0])
                               if len(points) > 1 and points[-1][0] > points[0][0] else None)
        recent_speed = None
        if len(c.trail) > 1:
            a, b = c.trail[-2:]
            recent_speed = (b[1] - a[1]) / (b[0] - a[0]) if b[0] > a[0] else None
        return replace(self._empty(event.tr_id, event.event_time), mode="route",
                       edge_id=c.variant.path.edges[c.edge_index].edge_id,
                       edge_occurrence=(f"{c.sequence.sequence_id}:{c.leg_index}:"
                                        f"{c.variant.variant_id}:{c.edge_index}"),
                       edge_offset_m=c.offset_m, lon=c.coord[0], lat=c.coord[1],
                       sequence_id=c.sequence.sequence_id, visit_id=leg.end.visits[0].visit_id,
                       visit_index=c.leg_index + 1, segment_id=leg.segment_id,
                       variant_id=c.variant.variant_id, progress=c.path_m / c.variant.path.length_m,
                       speed_mps=recent_speed, speed_1m_mps=speeds[60], speed_3m_mps=speeds[180],
                       speed_5m_mps=speeds[300], route_distance_m=c.distance_m,
                       position_quality="high" if c.distance_m <= 25 and margin >= 2 else "medium",
                       route_quality="confirmed" if c.variant.confirmed else "tentative",
                       route_source=c.variant.source, confidence_margin=margin,
                       max_event_time=event.event_time, gps_age_s=0)

    def update(self, event: Event) -> MatchState:
        if self.live and (event.receive_time is None or event.receive_time < event.event_time):
            raise ValueError("Live events require receive_time >= event_time")
        tr = event.tr_id
        if tr in self._last and event.event_time <= self._last[tr]:
            self.metrics["duplicate_or_late"] += 1
            return self._frames[tr][-1].state
        self._last[tr] = event.event_time
        old_frame = self._frames[tr][-1] if self._frames[tr] else None
        previous = self._beam.get(tr, ())
        last_gps = self._gps.get(tr)
        state = self._empty(tr, event.event_time)
        gps_time = last_gps.event_time if last_gps else None
        reason = None
        gap = last_gps is None or event.event_time - last_gps.event_time > self.config.gap_s
        jump = (event.valid and last_gps is not None and not gap and
                distance(event.coord, last_gps.coord) > self.config.max_speed_kmh / 3.6 *
                (event.event_time - last_gps.event_time) + 50)
        if gap:
            previous = ()
            self._events[tr].clear()
            self._outside[tr].clear()
            self._inside[tr].clear()
        if event.valid and not jump:
            self._events[tr].append(event)
            while self._events[tr] and self._events[tr][0].event_time < event.event_time - self.config.window_s:
                self._events[tr].popleft()
            candidates = self._candidates(event)
            beam = self._extend(candidates, previous, event, last_gps)
            reference = self._reference.get(tr)
            road = None
            if not beam or self.mode == "meili":
                road = self._road(event)
                state = road or state
                reason = "no_route_candidates" if not candidates else "impossible_transition"
            else:
                state = self._route_state(event, beam)
                if beam[0].variant.confirmed:
                    self._reference[tr] = beam[0]
                    reference = beam[0]
            if reference is not None:
                # Distance to the confirmed reference occurrence and its immediate successor.
                refs = list(reference.variant.path.edges)
                next_index = reference.leg_index + 1
                if next_index < len(reference.sequence.legs):
                    key = reference.sequence.legs[next_index].segment_id
                    for variant in self.catalog.variants.get(key, ()):
                        if variant.confirmed:
                            refs.extend(variant.path.edges)
                residual = min(project(line(e.shape), event.coord)[0] for e in refs)
                if residual > self.config.offroute_m:
                    self._inside[tr].clear()
                    self._outside[tr].append(event.event_time)
                    if road is None:
                        road = self._road(event)
                    outside = self._outside[tr]
                    if (len(outside) >= self.config.hysteresis_count and
                            outside[-1] - outside[0] >= self.config.hysteresis_s and road and
                            road.edge_id not in {e.edge_id for e in refs}):
                        self._off[tr] = True
                        state = road
                elif beam and beam[0].variant.confirmed:
                    self._outside[tr].clear()
                    self._inside[tr].append(event.event_time)
                    inside = self._inside[tr]
                    if len(inside) >= self.config.hysteresis_count and inside[-1] - inside[0] >= self.config.hysteresis_s:
                        self._off[tr] = False
                else:
                    self._outside[tr].clear()
                    self._inside[tr].clear()
                state = replace(state, off_route=self._off.get(tr), route_distance_m=residual)
                if state.mode == "road":
                    state = replace(state, route_quality="confirmed", route_source="observed")
            # Following a break, road evidence can reinitialize route hypotheses on the next event.
            self._beam[tr] = beam if self.mode != "meili" else ()
            self._gps[tr] = event
            gps_time = event.event_time
        else:
            reason = "gps_jump" if jump else "invalid_gps"
            self._outside[tr].clear()
            self._inside[tr].clear()
            self._beam[tr] = previous
        if state.mode == "unmatched" and old_frame and old_frame.state.edge_id:
            state = replace(old_frame.state, T=event.event_time, position_quality="stale",
                            reason=reason, speed_mps=None, speed_1m_mps=None,
                            speed_3m_mps=None, speed_5m_mps=None)
        age = event.event_time - state.max_event_time if state.max_event_time is not None else None
        state = replace(state, gps_age_s=age, reason=reason or state.reason)
        frame = Frame(event.event_time, gps_time, self._beam.get(tr, ()), state)
        self._frames[tr].append(frame)
        while len(self._frames[tr]) > 2 and self._frames[tr][1].time < event.event_time - self.config.window_s:
            self._frames[tr].popleft()
        self.metrics["events"] += 1
        return state

    def snapshot(self, tr_id: int, T: float, target_visit_id: str, cur_dev_s=None) -> MatchState:
        if T < self.catalog.cutoff:
            raise ValueError("Prediction predates catalog")
        key = tr_id, T, str(target_visit_id), cur_dev_s
        if key in self._published:
            return self._published[key]
        frames = self._frames.get(tr_id, ())
        if frames and T < frames[0].time and self._last[tr_id] - T > self.config.window_s:
            raise ValueError("Snapshot expired; replay the prefix with a fresh matcher")
        available = [f for f in frames if f.time <= T]
        if not available:
            result = self._empty(tr_id, T, "no_history")
        else:
            frame = available[-1]
            result = replace(frame.state, T=T,
                             gps_age_s=T - frame.state.max_event_time if frame.state.max_event_time else None)
            if self.live:
                # Live event ingestion is arrival ordered; prevent a caller from backdating a prediction.
                received = [e.receive_time for e in self._events[tr_id] if e.event_time <= T]
                if any(t is not None and t > T for t in received):
                    raise ValueError("Live snapshot precedes receipt of a used event")
            options = []
            if frame.state.mode == "route" and frame.state.position_quality != "stale":
                for c in frame.beam:
                    targets = [i for i, s in enumerate(c.sequence.stops)
                               if any(v.visit_id == str(target_visit_id) for v in s.visits)]
                    for target in targets:
                        if target <= c.leg_index:
                            continue
                        score = c.score
                        if cur_dev_s is not None:
                            leg = c.sequence.legs[c.leg_index]
                            planned = leg.start.time + (leg.end.time - leg.start.time) * c.path_m / c.variant.path.length_m
                            score -= min(8.0, 0.5 * ((T - cur_dev_s - planned) / self.config.schedule_sigma_s)**2)
                        options.append((score, c, target))
            if options:
                _, c, target = max(options, key=lambda o: o[0])
                fake_event = Event(tr_id, frame.time, result.lon, result.lat)
                result = replace(self._route_state(fake_event, frame.beam, c), T=T,
                                 gps_age_s=T - frame.time, off_route=frame.state.off_route)
                remaining = c.variant.path.length_m - c.path_m
                last_edge = c.variant.path.edges[-1]
                for leg in c.sequence.legs[c.leg_index + 1:target]:
                    viable = [v for v in self.catalog.variants.get(leg.segment_id, ())
                              if joinable(last_edge, v.path.edges[0])]
                    if not viable:
                        remaining = None
                        break
                    variant = viable[0]
                    remaining += variant.path.length_m
                    last_edge = variant.path.edges[-1]
                visits = [v.visit_id for s in c.sequence.stops[c.leg_index + 1:target + 1] for v in s.visits]
                result = replace(result, remaining_distance_m=remaining,
                                 remaining_visits=visits.index(str(target_visit_id)) + 1)
            if result.gps_age_s is not None and result.gps_age_s > self.config.gap_s:
                result = replace(result, position_quality="stale", remaining_distance_m=None,
                                 remaining_visits=None, progress=None, speed_mps=None,
                                 speed_1m_mps=None, speed_3m_mps=None, speed_5m_mps=None)
        self._published[key] = result
        if len(self._published) > 4096:
            self._published.popitem(last=False)
        return result
