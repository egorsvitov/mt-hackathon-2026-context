from dataclasses import replace

from conftest import simple_catalog

from route_matching.data import normalize_events
from route_matching.geo import distance
from route_matching.matcher import Matcher
from route_matching.replay import replay
from route_matching.types import Edge, Event, MatchedPoint, RoadPath, Trace


def event(t, lon, valid=True):
    return Event(
        7,
        t,
        lon if valid else None,
        55.75 if valid else None,
        speed=30,
        heading=90,
        location_valid=valid,
        packet_id=str(t),
    )


def test_streaming_match_preserves_repeated_edge_occurrence_and_snapshot():
    catalog = simple_catalog(repeated=True)
    matcher = Matcher(catalog)
    matcher.update(event(1010, 37.601))
    matcher.update(event(1060, 37.609))
    early = matcher.snapshot(7, 1060, "3", 0)
    matcher.update(event(1110, 37.611))
    matcher.update(event(1160, 37.619))
    assert early == matcher.snapshot(7, 1060, "3", 0)
    late = matcher.snapshot(7, 1160, "3", 0)
    assert early.edge_id == late.edge_id == "g:same"
    assert early.segment_id != late.segment_id
    assert early.edge_occurrence != late.edge_occurrence
    assert late.remaining_visits == 1
    assert late.remaining_stops == late.remaining_segments == 1
    assert late.current_segment_id == late.segment_id
    assert late.target_segment_id == late.segment_id
    assert late.segment_progress == late.progress
    assert late.estimated_remaining_time_s is not None
    assert late.max_event_time <= late.T
    matcher.close()


def test_invalid_jump_does_not_replace_valid_position():
    matcher = Matcher(simple_catalog())
    first = matcher.update(event(1010, 37.601))
    invalid = matcher.update(event(1020, 0, False))
    jump = matcher.update(event(1030, 37.8))
    assert first.edge_id == invalid.edge_id == jump.edge_id
    assert invalid.position_quality == "stale"
    assert jump.reason == "gps_jump"
    matcher.close()


def test_duplicate_is_idempotent_and_offline_catalog_has_no_snapshot_cutoff():
    matcher = Matcher(simple_catalog())
    matcher.update(event(990, 37.6))
    assert matcher.snapshot(7, 999, "3").T == 999
    matcher.update(event(1010, 37.601))
    duplicate = matcher.update(event(1010, 37.602))
    assert duplicate.max_event_time == 1010
    assert matcher.metrics["duplicate_packets"] == 1
    matcher.close()


def test_replay_is_order_independent_and_causal():
    catalog = simple_catalog()
    events = [event(1100, 37.61), event(1010, 37.601), event(1060, 37.609)]
    points = [{"sample_id": "a", "tr_id": 7, "T": 1060, "target_visit_id": "3", "cur_dev_s": 0}]
    a, _ = replay(events, points, catalog)
    b, _ = replay(list(reversed(events)), points, catalog)
    assert a[0][1] == b[0][1]
    assert a[0][1].max_event_time == 1060


def test_replay_reveals_packets_at_receive_time():
    catalog = simple_catalog()
    events = [
        event(1010, 37.601),
        Event(7, 1050, 37.609, 55.75, 30, 90, True, "late", receive_time=1070),
    ]
    points = [
        {"sample_id": "before", "tr_id": 7, "T": 1060, "target_visit_id": "3", "cur_dev_s": 0},
        {"sample_id": "after", "tr_id": 7, "T": 1080, "target_visit_id": "3", "cur_dev_s": 0},
    ]
    results, _ = replay(events, points, catalog)
    assert results[0][1].max_event_time == 1010
    assert results[1][1].max_event_time == 1050


def test_future_duplicate_never_replaces_an_earlier_visible_packet():
    past = Event(7, 1010, 37.601, 55.75, 30, 90, True, "past", receive_time=1011)
    future = Event(7, 1010, 37.601, 55.75, 30, 90, True, "future", receive_time=2000)
    normalized = normalize_events([future, past])
    assert normalized == [past]
    results, _ = replay(
        normalized,
        [{"sample_id": "a", "tr_id": 7, "T": 1100, "target_visit_id": "3", "cur_dev_s": 0}],
        simple_catalog(),
    )
    assert results[0][1].max_event_time == 1010


def test_late_packet_rebuilds_only_future_state_and_published_snapshot_is_immutable():
    matcher = Matcher(simple_catalog())
    matcher.update(event(1010, 37.601))
    matcher.update(event(1060, 37.609))
    published = matcher.snapshot(7, 1060, "3", 0)
    matcher.update(Event(7, 1040, 37.606, 55.75, 30, 90, True, "late", receive_time=1070))
    assert matcher.snapshot(7, 1060, "3", 0) == published
    assert matcher.metrics["late_packets"] == 1
    matcher.close()


def test_large_gap_clears_off_route_and_reference_state():
    matcher = Matcher(simple_catalog())
    matcher.update(event(1010, 37.601))
    matcher._off[7] = True
    old_reference = matcher._reference[7]
    state = matcher.update(event(1300, 37.611))
    assert state.off_route is not True
    assert matcher._reference[7] is not old_reference
    matcher.close()


class DetourGraph:
    version = "g"

    def trace(self, events, timeout=1):
        shape = ((37.6, 55.76), (37.63, 55.76))
        edge = Edge("g:detour", "detour", shape, distance(*shape), begin_node="x", end_node="y")
        points = tuple(MatchedPoint(e.coord, 0, 0.5, 0) for e in events)
        return Trace(RoadPath((edge,)), points)

    def routes(self, start, end):
        return ()


def test_confirmed_route_requires_hysteresis_before_off_route():
    matcher = Matcher(simple_catalog(), DetourGraph())
    matcher.update(event(1010, 37.601))
    first = matcher.update(Event(7, 1040, 37.601, 55.76, 20, 90, True, "a"))
    second = matcher.update(Event(7, 1060, 37.602, 55.76, 20, 90, True, "b"))
    third = matcher.update(Event(7, 1080, 37.603, 55.76, 20, 90, True, "c"))
    assert first.off_route is not True
    assert second.off_route is not True
    assert third.off_route is True
    assert third.mode == "road"
    assert third.edge_id == "g:detour"
    matcher.close()


def test_pattern_geometry_is_indexed_once_across_trip_occurrences():
    catalog = simple_catalog()
    first = catalog.sequences[0]
    duplicate = replace(first, sequence_id="second", trip_occurrence_id="second")
    matcher = Matcher(replace(catalog, sequences=(first, duplicate)))
    assert sum(len(entries) for entries in matcher._entries.values()) == 2
    assert len(matcher._vehicle_sequences[7]) == 2
    matcher.close()


def test_nearest_mode_never_calls_full_road_fallback():
    class FailingGraph:
        version = "g"

        def trace(self, events, timeout=1):
            raise AssertionError("nearest must not call Meili")

    matcher = Matcher(simple_catalog(), FailingGraph(), mode="nearest")
    state = matcher.update(event(1010, 37.8))
    assert state.mode == "unmatched"
    matcher.close()
