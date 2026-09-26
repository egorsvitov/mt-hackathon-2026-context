from dataclasses import asdict

from route_matching.catalog import Catalog
from route_matching.geo import distance
from route_matching.types import Edge, Leg, RoadPath, Sequence, StopGroup, Variant, Visit, digest


def edge(name, start, end, begin=None, finish=None):
    return Edge(
        "g:" + name,
        name,
        (start, end),
        distance(start, end),
        begin_node=begin or name + "a",
        end_node=finish or name + "b",
    )


def simple_catalog(confirmed=True, repeated=False, cutoff=1000.0):
    coords = [(37.60, 55.75), (37.61, 55.75), (37.62, 55.75)]
    visits = [
        Visit(str(i + 1), 7, cutoff + i * 300, str(i), coord) for i, coord in enumerate(coords)
    ]
    stops = tuple(StopGroup(v.stop_id, v.coord, (v,)) for v in visits)
    legs = tuple(Leg(i, "s" + str(i), stops[i], stops[i + 1]) for i in range(2))
    sequence = Sequence("seq", 7, stops, legs)
    first = edge("same" if repeated else "one", coords[0], coords[1], "a", "b")
    second = edge("same" if repeated else "two", coords[1], coords[2], "b", "c")
    if repeated:
        first = Edge(**{**asdict(first), "source_fraction": 0.0, "target_fraction": 0.5})
        second = Edge(**{**asdict(second), "source_fraction": 0.5, "target_fraction": 1.0})
    variants = {}
    for leg, path in zip(legs, (RoadPath((first,)), RoadPath((second,)))):
        variants[leg.segment_id] = (
            Variant(
                "v" + str(leg.index),
                path,
                "observed" if confirmed else "osm_hypothesis",
                2 if confirmed else 0,
                1.0 if confirmed else 0,
            ),
        )
    payload = {
        "sequence": asdict(sequence),
        "variants": {k: asdict(v[0]) for k, v in variants.items()},
    }
    return Catalog(digest(payload), "g", cutoff, (sequence,), variants, {})
