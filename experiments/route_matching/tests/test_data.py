from pathlib import Path

from route_matching.data import build_sequences, from_traffic_row, read_schedule


def test_schedule_whitelist_and_equal_coordinate_collapse(tmp_path: Path):
    schedule = tmp_path / "schedule.csv"
    schedule.write_text(
        "tt_action_item_id,time_begin,time_fact_begin,tr_id,geom,building_address\n"
        '1,2026-01-06 10:00:00,2099-01-01 00:00:00,7,POINT (37.6 55.75),A\n'
        '2,2026-01-06 10:00:00,1900-01-01 00:00:00,7,POINT (37.6 55.75),A\n'
        '3,2026-01-06 10:05:00,2099-01-01 00:00:00,7,POINT (37.61 55.75),B\n')
    visits = read_schedule(schedule, "Europe/Moscow")
    sequences = build_sequences(visits)
    assert len(sequences) == 1
    assert len(sequences[0].stops) == 2
    assert [v.visit_id for v in sequences[0].stops[0].visits] == ["1", "2"]
    # Factual times were neither parsed nor retained.
    assert all(not hasattr(v, "time_fact_begin") for v in visits)


def test_equal_time_different_places_remain_ambiguous(tmp_path: Path):
    schedule = tmp_path / "schedule.csv"
    schedule.write_text(
        "tt_action_item_id,time_begin,tr_id,geom,building_address\n"
        '1,2026-01-06 10:00:00,7,POINT (37.6 55.75),A\n'
        '2,2026-01-06 10:00:00,7,POINT (37.61 55.75),B\n'
        '3,2026-01-06 10:05:00,7,POINT (37.62 55.75),C\n')
    sequences = build_sequences(read_schedule(schedule, "Europe/Moscow"))
    assert len(sequences) == 2
    assert all(s.ambiguous for s in sequences)
    assert {tuple(stop.coord for stop in s.stops[:2]) for s in sequences} == {
        ((37.6, 55.75), (37.61, 55.75)), ((37.61, 55.75), (37.6, 55.75))}


def test_ndtp_mapping_adapter_keeps_event_and_receive_times():
    row = {"tr_id": "7", "event_time": "2026-01-06 10:00:00", "receive_time":
           "2026-01-06 10:00:03", "lon": "37.6", "lat": "55.75", "speed": "12",
           "heading": "90", "location_valid": "true", "packet_id": "p"}
    event = from_traffic_row(row, "Europe/Moscow")
    assert event.available_time - event.event_time == 3
    assert event.valid
