import pytest

from route_matching.geo import decode_polyline, edit_distance, joinable
from route_matching.graph import GraphError, Valhalla, parse_trace
from route_matching.types import Edge, Event


def test_polyline_and_edit_distance():
    points = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    assert points[0] == pytest.approx((-120.2, 38.5))
    assert edit_distance((1, 2, 3), (1, 4, 3)) == pytest.approx(1 / 3)


def test_invalid_trace_edge_index_fails_closed():
    raw = {
        "shape": "_p~iF~ps|U_ulLnnqC",
        "edges": [{"id": 1, "way_id": 2, "begin_shape_index": 0, "end_shape_index": 1}],
        "matched_points": [{"type": "matched", "edge_index": 8, "lon": -120, "lat": 38}],
    }
    with pytest.raises(GraphError):
        parse_trace(raw, "g")


def test_only_shared_path_endpoints_are_connected():
    a = Edge("a", "a", ((37.6, 55.75), (37.61, 55.75)), 100, 0, 0.5)
    b = Edge("b", "b", ((37.6101, 55.75), (37.61, 55.76)), 100, 0.5, 1)
    assert not joinable(a, b)


def test_trace_options_stay_within_standard_valhalla_limits(monkeypatch):
    graph = Valhalla("http://valhalla.invalid", "g")
    captured = {}

    def attributes(payload, timeout):
        captured.update(payload)
        return "trace"

    monkeypatch.setattr(graph, "_attributes", attributes)
    events = [
        Event(1, 1.0, 37.60, 55.75, 10.0, 90.0, True, "a"),
        Event(1, 2.0, 37.61, 55.75, 10.0, 90.0, True, "b"),
    ]
    assert graph.trace(events) == "trace"
    assert captured["trace_options"]["search_radius"] <= 100
