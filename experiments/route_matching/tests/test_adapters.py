from conftest import simple_catalog

from route_matching.adapters import StreamingSpatialAdapter


def test_streaming_adapter_returns_ml_and_dashboard_contracts():
    adapter = StreamingSpatialAdapter(simple_catalog(), timezone_name="UTC")
    adapter.update(
        {
            "tr_id": 7,
            "event_time": 1010,
            "receive_time": 1010,
            "lon": 37.601,
            "lat": 55.75,
            "speed": 30,
            "heading": 90,
            "location_valid": True,
            "packet_id": "a",
        }
    )
    state = adapter.snapshot(7, 1010, "3", 0)
    features = adapter.ml_features(7, 1010, "3", 0)
    dashboard = adapter.dashboard_fields(state)
    assert features["mm_matched"] == 1
    assert dashboard["route_id"] == "R7"
    assert dashboard["matched_lon"] is not None
    adapter.close()
