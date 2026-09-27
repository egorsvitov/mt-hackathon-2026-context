from __future__ import annotations

import pandas as pd
import pytest
from route_matching.catalog import build_catalog
from route_matching.types import Edge, Event, RoadPath, StopGroup, Visit, timestamp

from route_delay_v3.data import read_traffic_union
from route_delay_v3.events import _visit, visible_events

TIMEZONE = "Europe/Moscow"


class SimpleGraph:
    version = "test-graph"

    def routes(self, start, end):
        return (RoadPath((Edge("edge", "way", (start, end), 100.0),)),)

    def route_through(self, stops):
        return RoadPath((Edge("edge", "way", stops, 100.0),))

    def trace(self, *args, **kwargs):
        raise AssertionError("Static catalog must not inspect telemetry")


def _stop(when="2026-01-06 12:00:00"):
    visit = Visit("visit", 1, timestamp(when, TIMEZONE), "stop", (37.6, 55.7))
    return StopGroup("stop", (37.6, 55.7), (visit,))


def test_static_catalog_ignores_history_and_shares_patterns():
    a = timestamp("2026-01-06 12:00:00", TIMEZONE)
    plan = (
        Visit("1", 100, a, "a", (37.6, 55.7)),
        Visit("2", 100, a + 60, "b", (37.601, 55.7)),
        Visit("3", 9_000_000, a, "a", (37.6, 55.7)),
        Visit("4", 9_000_000, a + 60, "b", (37.601, 55.7)),
    )
    graph = SimpleGraph()
    first = build_catalog(plan, [], None, graph, mode="static_plan_graph")
    future = Event(100, a + 3600, 37.6, 55.7, 20, 0, True, "future")
    second = build_catalog(plan, [future], None, graph, mode="static_plan_graph")
    assert first.version == second.version
    assert len(first.sequences) == 2
    assert len({s.route_pattern_id for s in first.sequences}) == 1
    assert all(v.median_duration_s is None for vs in first.variants.values() for v in vs)
    assert all(v.support == 0 for vs in first.variants.values() for v in vs)


def _packets(rows):
    frame = pd.DataFrame(rows, columns=["event_time", "lon", "speed", "available_at"])
    frame["event_time"] = pd.to_datetime(frame.event_time)
    frame["available_at"] = pd.to_datetime(frame.available_at)
    frame["lat"] = 55.7
    frame["location_valid"] = True
    return frame


def test_detector_confirms_dwell_pass_through_and_gap():
    stop = _stop()
    dwell = _packets(
        [
            ("2026-01-06 12:00:00", 37.6, 0, "2026-01-06 12:00:01"),
            ("2026-01-06 12:00:10", 37.6, 0, "2026-01-06 12:00:11"),
            ("2026-01-06 12:00:20", 37.601, 15, "2026-01-06 12:00:25"),
        ]
    )
    found = _visit(dwell, stop, pd.Timestamp.min)
    assert not found["pass_through"]
    assert found["departure"] == pd.Timestamp("2026-01-06 12:00:20")
    assert found["available_at"] == pd.Timestamp("2026-01-06 12:00:25")
    passing = dwell.copy()
    passing["speed"] = 15
    assert _visit(passing, stop, pd.Timestamp.min)["pass_through"]
    gap = dwell.copy()
    gap.loc[2, "event_time"] += pd.Timedelta(seconds=60)
    assert _visit(gap, stop, pd.Timestamp.min) is None


def test_unavailable_events_are_excluded():
    events = pd.DataFrame(
        {
            "available_at": pd.to_datetime(["2026-01-06 10:00", "2026-01-06 10:05"]),
            "trip_occurrence_id": ["other", "mine"],
        }
    )
    assert visible_events(events, pd.Timestamp("2026-01-06 10:04"), "mine").shape[0] == 1
    assert visible_events(events, pd.Timestamp("2026-01-06 10:06"), "mine").shape[0] == 1


def test_duplicate_telemetry_conflict_is_rejected(tmp_path):
    columns = [
        "packet_id",
        "tr_id",
        "event_time",
        "receive_time",
        "location_valid",
        "lon",
        "lat",
        "speed",
        "heading",
    ]
    first = ["1", 100, "2026-01-06 10:00:00", "2026-01-06 10:00:01", True, 37.6, 55.7, 10, 0]
    second = first.copy()
    second[5] = 37.7
    for split, row in [("train", first), ("test", second), ("validate", first)]:
        directory = tmp_path / split
        directory.mkdir()
        pd.DataFrame([row], columns=columns).to_csv(directory / "traffic.csv", index=False)
    with pytest.raises(ValueError, match="Conflicting telemetry"):
        read_traffic_union(tmp_path)


def test_future_packet_does_not_change_snapshot():
    from route_delay_v3.data import build_point_features

    a = timestamp("2026-01-06 10:00:00", TIMEZONE)
    plan = (
        Visit("1", 100, a, "a", (37.6, 55.7)),
        Visit("2", 100, a + 720, "b", (37.601, 55.7)),
    )
    catalog = build_catalog(plan, [], None, SimpleGraph(), mode="static_plan_graph")
    points = pd.DataFrame(
        [
            {
                "sample_id": "one",
                "tr_id": 100,
                "T": pd.Timestamp("2026-01-06 10:00:00"),
                "target_stop_id": 2,
                "target_time_begin": pd.Timestamp("2026-01-06 10:12:00"),
                "cur_dev_s": 0.0,
            }
        ]
    )
    schedule = pd.DataFrame(
        [
            {
                "tt_action_item_id": 1,
                "time_begin": pd.Timestamp("2026-01-06 10:00:00"),
                "tr_id": 100,
                "stop_lon": 37.6,
                "stop_lat": 55.7,
            },
            {
                "tt_action_item_id": 2,
                "time_begin": pd.Timestamp("2026-01-06 10:12:00"),
                "tr_id": 100,
                "stop_lon": 37.601,
                "stop_lat": 55.7,
            },
        ]
    )
    columns = [
        "packet_id",
        "tr_id",
        "event_time",
        "available_at",
        "location_valid",
        "lon",
        "lat",
        "speed",
        "heading",
    ]
    empty = pd.DataFrame(columns=columns)
    future = pd.DataFrame(
        [
            [
                "future",
                100,
                pd.Timestamp("2026-01-06 10:01:00"),
                pd.Timestamp("2026-01-06 10:02:00"),
                True,
                37.6,
                55.7,
                10,
                0,
            ]
        ],
        columns=columns,
    )
    earlier = build_point_features(points, empty, schedule, catalog)
    later = build_point_features(points, future, schedule, catalog)
    pd.testing.assert_frame_equal(earlier, later)


def test_label_gate_uses_actual_arrival_plus_delay():
    from route_delay_v3.workflow import eligible_training_rows

    frame = pd.DataFrame(
        {
            "label_available_at": pd.to_datetime(
                ["2026-01-06 10:59:59", "2026-01-06 11:00:00", "2026-01-06 10:30:00"]
            ),
            "mm_trip_occurrence_id": ["ok", "future", "blocked"],
        }
    )
    visible = eligible_training_rows(frame, pd.Timestamp("2026-01-06 11:00:00"), {"blocked"})
    assert visible.mm_trip_occurrence_id.tolist() == ["ok"]


def test_duplicate_packets_count_once(tmp_path):
    from route_delay_v3.data import read_traffic_union

    columns = [
        "packet_id",
        "tr_id",
        "event_time",
        "receive_time",
        "location_valid",
        "lon",
        "lat",
        "speed",
        "heading",
    ]
    row = ["1", 100, "2026-01-06 10:00:00", "2026-01-06 10:00:01", True, 37.6, 55.7, 10, 0]
    for split in ("train", "test", "validate"):
        directory = tmp_path / split
        directory.mkdir()
        pd.DataFrame([row], columns=columns).to_csv(directory / "traffic.csv", index=False)
    traffic, report = read_traffic_union(tmp_path)
    assert len(traffic) == 1
    assert report["duplicates_removed"] == 2


def test_unknown_route_uses_planned_fallback():
    from route_matching import Catalog

    from route_delay_v3.components import add_components

    frame = pd.DataFrame(
        [
            {
                "sample_id": "one",
                "T": pd.Timestamp("2026-01-06 10:00"),
                "mm_trip_occurrence_id": "__unknown__",
                "mm_sequence_id": "__unknown__",
                "target_stop_id": 123,
                "mm_current_leg_index": float("nan"),
                "horizon_s": 720.0,
                "cur_dev_s": 25.0,
            }
        ]
    )
    catalog = Catalog("static", "graph", None, (), {}, {})
    out = add_components(frame, pd.DataFrame(), pd.DataFrame(), catalog)
    assert out.component_eta_s.iloc[0] == 720.0
    assert out.component_delay_s.iloc[0] == 25.0
    assert out.component_fallback.iloc[0] == 1.0


def test_component_eta_uses_remaining_run_and_current_dwell():
    from route_delay_v3.components import add_components

    a = timestamp("2026-01-06 09:00:00", TIMEZONE)
    plan = (
        Visit("1", 100, a, "a", (37.6, 55.7)),
        Visit("2", 100, a + 720, "b", (37.601, 55.7)),
        Visit("3", 9_000_000, a, "a", (37.6, 55.7)),
        Visit("4", 9_000_000, a + 720, "b", (37.601, 55.7)),
    )
    catalog = build_catalog(plan, [], None, SimpleGraph(), mode="static_plan_graph")
    sequence = next(s for s in catalog.sequences if s.tr_id == 100)
    other = next(s for s in catalog.sequences if s.tr_id == 9_000_000)
    now = pd.Timestamp("2026-01-06 10:00:00")
    frame = pd.DataFrame(
        [
            {
                "sample_id": "one",
                "T": now,
                "target_stop_id": 2,
                "mm_sequence_id": sequence.sequence_id,
                "mm_trip_occurrence_id": sequence.trip_occurrence_id,
                "mm_current_leg_index": 0,
                "mm_segment_progress": 0.5,
                "mm_current_dwell_elapsed_s": 10.0,
                "horizon_s": 720.0,
                "cur_dev_s": 0.0,
            }
        ]
    )
    runs = pd.DataFrame(
        [
            {
                "trip_occurrence_id": other.trip_occurrence_id,
                "segment_id": sequence.legs[0].segment_id,
                "available_at": pd.Timestamp("2026-01-06 09:30:00"),
                "run_time_s": 100.0,
            }
        ]
    )
    dwells = pd.DataFrame(
        [
            {
                "trip_occurrence_id": other.trip_occurrence_id,
                "stop_id": sequence.stops[0].stop_id,
                "available_at": pd.Timestamp("2026-01-06 09:30:00"),
                "dwell_time_s": 30.0,
            }
        ]
    )
    out = add_components(frame, runs, dwells, catalog)
    assert out.component_run_s.iloc[0] == pytest.approx(50.0)
    assert out.component_dwell_s.iloc[0] == pytest.approx(20.0)
    assert out.component_eta_s.iloc[0] == pytest.approx(70.0)


def test_detector_waits_for_late_intermediate_packet():
    stop = _stop()
    packets = _packets(
        [
            ("2026-01-06 12:00:00", 37.6, 0, "2026-01-06 12:00:01"),
            ("2026-01-06 12:00:10", 37.6, 0, "2026-01-06 12:01:00"),
            ("2026-01-06 12:00:20", 37.601, 15, "2026-01-06 12:00:25"),
        ]
    )
    found = _visit(packets, stop, pd.Timestamp.min)
    assert found["available_at"] == pd.Timestamp("2026-01-06 12:01:00")


def test_clear_reverse_progress_is_rejected():
    from route_delay_v3.events import _backtracked

    path = RoadPath((Edge("road", "way", ((37.6, 55.7), (37.604, 55.7)), 250.0),))
    passage = pd.DataFrame(
        {
            "lon": [37.6, 37.603, 37.6005],
            "lat": [55.7, 55.7, 55.7],
        }
    )
    assert _backtracked(passage, path)


def test_public_schedule_ignores_factual_arrival(tmp_path):
    from route_delay_v3.data import read_public_schedule

    record = {
        "tt_action_item_id": 1,
        "time_begin": "2026-01-06 12:00:00",
        "tr_id": 100,
        "geom": "POINT (37.6 55.7)",
        "building_address": "A",
        "time_fact_begin": "2026-01-06 12:01:00",
    }
    for split, name in (
        ("train", "schedule.csv"),
        ("test", "schedule.csv"),
        ("validate", "schedule_plan.csv"),
    ):
        folder = tmp_path / split
        folder.mkdir()
        pd.DataFrame([record]).to_csv(folder / name, index=False)
    before, _ = read_public_schedule(tmp_path)
    path = tmp_path / "test" / "schedule.csv"
    record["time_fact_begin"] = "2026-01-06 20:00:00"
    pd.DataFrame([record]).to_csv(path, index=False)
    after, _ = read_public_schedule(tmp_path)
    pd.testing.assert_frame_equal(before, after)
    assert "time_fact_begin" not in after
