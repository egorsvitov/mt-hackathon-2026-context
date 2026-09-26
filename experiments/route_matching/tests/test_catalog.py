from conftest import edge

from route_matching.catalog import Catalog, build_catalog, consensus
from route_matching.data import discover_sequences
from route_matching.geo import joinable, polyline_length
from route_matching.types import Config, Edge, Event, Passage, RoadPath, Visit


class FakeGraph:
    version = "g"

    def __init__(self):
        self.traces = 0
        self.routes_called = 0

    def trace(self, events, timeout=1):
        self.traces += 1
        raise AssertionError("No historical trace is eligible")

    def routes(self, start, end):
        self.routes_called += 1
        return (RoadPath((edge(str(self.routes_called), start, end),)),)


class ThroughGraph(FakeGraph):
    def __init__(self):
        super().__init__()
        self.through_calls = 0

    def route_through(self, stops):
        self.through_calls += 1
        return RoadPath((Edge("g:through", "through", stops, polyline_length(stops)),))

    def routes(self, start, end):
        raise AssertionError("Pairwise fallback must not run for a valid pattern route")


def test_cutoff_is_applied_before_matching_and_synthetic_is_excluded():
    plan = (
        Visit("1", 7, 1100, "a", (37.6, 55.75)),
        Visit("2", 7, 1200, "b", (37.61, 55.75)),
        Visit("x", 9_000_000, 1100, "a", (37.6, 55.75)),
        Visit("y", 9_000_000, 1200, "b", (37.61, 55.75)),
    )
    history = [
        Event(7, 900, 37.6, 55.75),
        Event(7, 999, 37.601, 55.75),
        Event(7, 998, 37.602, 55.75, receive_time=1001),
        Event(7, 1001, 37.61, 55.75),
        Event(9_000_000, 900, 37.6, 55.75),
    ]
    graph = FakeGraph()
    catalog = build_catalog(plan, history, 1000, graph)
    assert catalog.report["allowed_events"] == 2
    assert graph.traces == 0
    assert {s.tr_id for s in catalog.sequences} == {7}
    assert all(v.source == "osm_hypothesis" for vs in catalog.variants.values() for v in vs)


def test_consensus_deduplicates_passages_and_selects_observed_medoid():
    straight = RoadPath((edge("a", (37.6, 55.75), (37.61, 55.75)),))
    detour = RoadPath(
        (
            edge("b", (37.6, 55.75), (37.605, 55.755)),
            edge("c", (37.605, 55.755), (37.61, 55.75)),
        )
    )
    p1 = Passage("s", 7, "1", "2", 100, 200, 201, straight, 0.9)
    duplicate = Passage("s", 7, "1", "2", 100, 200, 202, straight, 0.5)
    p2 = Passage("s", 8, "1", "2", 110, 220, 221, straight, 0.8)
    p3 = Passage("s", 9, "1", "2", 120, 240, 241, detour, 0.8)
    variants = consensus([p1, duplicate, p2, p3], Config(cluster_distance=0.2))
    assert variants[0].support == 2
    assert variants[0].confirmed
    assert variants[0].path.signature == straight.signature
    assert sum(v.support for v in variants) == 3


def test_catalog_round_trip(tmp_path):
    plan = (Visit("1", 7, 1100, "a", (37.6, 55.75)), Visit("2", 7, 1200, "b", (37.61, 55.75)))
    catalog = build_catalog(plan, [], 1000, FakeGraph())
    path = tmp_path / "catalog.json"
    catalog.save(path)
    loaded = Catalog.load(path)
    assert loaded.version == catalog.version
    assert loaded.variants.keys() == catalog.variants.keys()
    assert loaded.sequences[0].stops == catalog.sequences[0].stops
    assert loaded.schema_version == 2
    assert loaded.patterns[0].route_pattern_id == catalog.patterns[0].route_pattern_id


def test_leave_one_trip_out_provenance_and_connected_segment_anchors():
    plan = (
        Visit("1", 7, 1000, "a", (37.6, 55.75)),
        Visit("2", 7, 1100, "b", (37.61, 55.75)),
        Visit("3", 7, 1400, "b", (37.61, 55.75)),
        Visit("4", 7, 1500, "a", (37.6, 55.75)),
    )
    discovered, _, _ = discover_sequences(plan)
    excluded = discovered[0].trip_occurrence_id
    catalog = build_catalog(plan, [], None, FakeGraph(), excluded_trip_ids={excluded})
    assert excluded in catalog.provenance["excluded_trip_occurrence_ids"]
    assert excluded not in catalog.provenance["trip_occurrence_ids"]
    for sequence in catalog.sequences:
        chosen = [catalog.variants[leg.segment_id][0] for leg in sequence.legs]
        assert all(joinable(a.path.edges[-1], b.path.edges[0]) for a, b in pairwise(chosen))


def test_osm_pattern_is_routed_once_then_split_into_connected_segments():
    plan = (
        Visit("1", 7, 1000, "a", (37.60, 55.75)),
        Visit("2", 7, 1100, "b", (37.61, 55.75)),
        Visit("3", 7, 1200, "c", (37.62, 55.75)),
    )
    graph = ThroughGraph()
    catalog = build_catalog(plan, [], None, graph)
    assert graph.through_calls == 1
    sequence = catalog.sequences[0]
    chosen = [catalog.variants[leg.segment_id][0] for leg in sequence.legs]
    assert all(joinable(a.path.edges[-1], b.path.edges[0]) for a, b in pairwise(chosen))


from itertools import pairwise
