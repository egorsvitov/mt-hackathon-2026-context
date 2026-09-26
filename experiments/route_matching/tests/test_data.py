from pathlib import Path

from route_matching.data import build_sequences, discover_sequences, from_traffic_row, read_schedule
from route_matching.types import Config, Visit


def test_schedule_whitelist_and_equal_coordinate_collapse(tmp_path: Path):
    schedule = tmp_path / "schedule.csv"
    schedule.write_text(
        "tt_action_item_id,time_begin,time_fact_begin,tr_id,geom,building_address\n"
        "1,2026-01-06 10:00:00,2099-01-01 00:00:00,7,POINT (37.6 55.75),A\n"
        "2,2026-01-06 10:00:00,1900-01-01 00:00:00,7,POINT (37.6 55.75),A\n"
        "3,2026-01-06 10:05:00,2099-01-01 00:00:00,7,POINT (37.61 55.75),B\n"
    )
    visits = read_schedule(schedule, "Europe/Moscow")
    sequences = build_sequences(visits)
    assert len(sequences) == 1
    assert len(sequences[0].stops) == 2
    assert [v.visit_id for v in sequences[0].stops[0].visits] == ["1", "2"]
    # Factual times were neither parsed nor retained.
    assert all(not hasattr(v, "time_fact_begin") for v in visits)


def test_equal_time_different_places_are_ordered_once(tmp_path: Path):
    schedule = tmp_path / "schedule.csv"
    schedule.write_text(
        "tt_action_item_id,time_begin,tr_id,geom,building_address\n"
        "1,2026-01-06 10:00:00,7,POINT (37.6 55.75),A\n"
        "2,2026-01-06 10:00:00,7,POINT (37.61 55.75),B\n"
        "3,2026-01-06 10:05:00,7,POINT (37.62 55.75),C\n"
    )
    sequences = build_sequences(read_schedule(schedule, "Europe/Moscow"))
    assert len(sequences) == 1
    assert all(s.ambiguous for s in sequences)
    assert tuple(stop.coord for stop in sequences[0].stops) == (
        (37.6, 55.75),
        (37.61, 55.75),
        (37.62, 55.75),
    )


def test_trip_occurrences_and_patterns_are_explicit_and_shared():
    plan = tuple(
        Visit(f"{tr}-{index}", tr, time, str(index), coord)
        for tr in (7, 8)
        for index, (time, coord) in enumerate(
            (
                (1000, (37.60, 55.75)),
                (1100, (37.61, 55.75)),
                (1400, (37.61, 55.75)),
                (1500, (37.60, 55.75)),
            )
        )
    )
    sequences, patterns, assignments = discover_sequences(
        plan, Config(terminal_pause_s=180, trip_gap_s=1200)
    )
    assert len(sequences) == 4
    assert len(patterns) == 2
    assert len({s.route_pattern_id for s in sequences}) == 2
    assert len(assignments) == len(plan)
    assert all(s.trip_occurrence_id for s in sequences)


def test_physical_stop_clustering_and_pattern_ids_are_order_independent():
    plan = (
        Visit("1", 7, 1000, "raw-a", (37.600000, 55.750000)),
        Visit("2", 7, 1100, "raw-b", (37.610000, 55.750000)),
        Visit("3", 8, 1000, "other-a", (37.600010, 55.750000)),
        Visit("4", 8, 1100, "other-b", (37.610010, 55.750000)),
    )
    forward = discover_sequences(plan)
    reversed_input = discover_sequences(tuple(reversed(plan)))
    assert len(forward[1]) == 1
    assert forward[1] == reversed_input[1]
    assert {sequence.route_pattern_id for sequence in forward[0]} == {
        sequence.route_pattern_id for sequence in reversed_input[0]
    }


def test_ndtp_mapping_adapter_keeps_event_and_receive_times():
    row = {
        "tr_id": "7",
        "event_time": "2026-01-06 10:00:00",
        "receive_time": "2026-01-06 10:00:03",
        "lon": "37.6",
        "lat": "55.75",
        "speed": "12",
        "heading": "90",
        "location_valid": "true",
        "packet_id": "p",
    }
    event = from_traffic_row(row, "Europe/Moscow")
    assert event.available_time - event.event_time == 3
    assert event.valid
