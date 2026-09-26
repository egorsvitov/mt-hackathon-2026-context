import json
from dataclasses import replace

from conftest import simple_catalog

from route_matching.export import dashboard_network, write_dashboard_network
from route_matching.types import RoutePattern


def test_dashboard_export_keeps_legacy_contract_and_spatial_metadata(tmp_path):
    catalog = simple_catalog()
    payload = dashboard_network(catalog)
    assert len(payload["stops"]) == 3
    assert len(payload["routes"]) == 1
    route = payload["routes"][0]
    assert route["route_id"] == "R7"
    assert route["stops"] == [0, 1, 2]
    assert len(route["segments"]) == 2
    assert all(segment["path"][0] != segment["path"][-1] for segment in route["segments"])
    assert all("segment_id" in segment and "source" in segment for segment in route["segments"])
    output = tmp_path / "network.json"
    write_dashboard_network(catalog, output)
    assert json.loads(output.read_text()) == payload


def test_dashboard_export_uses_variants_from_one_canonical_trip():
    catalog = simple_catalog()
    sequence = replace(
        catalog.sequences[0],
        route_pattern_id="pattern",
        trip_occurrence_id="trip-canonical",
    )
    variants = {}
    for leg in sequence.legs:
        base = catalog.variants[leg.segment_id][0]
        mixed = replace(
            base,
            variant_id=f"mixed-{leg.index}",
            support=100,
            provenance=(("other-trip", 7, 1.0, 2.0),),
        )
        canonical = replace(
            base,
            variant_id=f"canonical-{leg.index}",
            support=1,
            provenance=(("trip-canonical", 7, 1.0, 2.0),),
        )
        variants[leg.segment_id] = (mixed, canonical)
    pattern = RoutePattern(
        "pattern",
        "direction",
        tuple(stop.stop_id for stop in sequence.stops),
        "trip-canonical",
        ("trip-canonical",),
    )
    catalog = replace(
        catalog, sequences=(sequence,), variants=variants, patterns=(pattern,)
    )

    route = dashboard_network(catalog)["routes"][0]
    assert [segment["support"] for segment in route["segments"]] == [1, 1]



def test_dashboard_replay_embeds_network_and_matched_coordinates(tmp_path):
    from route_matching.dashboard_replay import write_matched_dashboard_replay

    catalog = simple_catalog()
    network_path = tmp_path / "network.json"
    write_dashboard_network(catalog, network_path)
    replay = {
        "meta": {"t0": 1000},
        "stops": [],
        "routes": [],
        "telemetry": {
            "7": {
                "unit_id": 70,
                "t": [10, 60],
                "lat": [5575020, 5575020],
                "lon": [3760100, 3760900],
                "spd": [30, 30],
                "hdg": [90, 90],
            }
        },
    }
    source = tmp_path / "replay.js"
    source.write_text("window.REPLAY_DATA=" + json.dumps(replay) + ";\n")
    output = tmp_path / "matched.js"
    report = write_matched_dashboard_replay(catalog, network_path, source, output)
    result = json.loads(output.read_text().removeprefix("window.REPLAY_DATA=")[:-2])
    track = result["telemetry"]["7"]
    assert result["routes"]
    assert report["matched"] == 2
    assert track["raw_lat"] == [5575020, 5575020]
    assert track["mm_matched"] == [1, 1]
    assert track["lat"] != track["raw_lat"]
