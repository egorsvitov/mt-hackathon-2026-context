"""Export the route catalog to the dashboard network contract."""

from __future__ import annotations

import json
from pathlib import Path

from .catalog import Catalog
from .geo import distance
from .types import Variant


def _variant_rank(variant: Variant) -> tuple:
    source = {"observed": 0, "osm_hypothesis": 1}.get(variant.source, 2)
    return source, -variant.support, -variant.support_fraction, variant.variant_id


def _belongs_to_trip(variant: Variant, trip_occurrence_id: str | None) -> bool:
    """Return whether a variant was observed on the canonical full trip."""
    if not trip_occurrence_id:
        return False
    return any(len(item) == 4 and item[0] == trip_occurrence_id for item in variant.provenance)


def _pattern_variant_rank(variant: Variant, canonical_trip_id: str | None) -> tuple:
    # Use legs from one real passage before mixing independently popular variants.
    return (not _belongs_to_trip(variant, canonical_trip_id), *_variant_rank(variant))


def _coordinates(variant: Variant) -> list[list[float]]:
    coordinates = []
    for edge in variant.path.edges:
        for lon, lat in edge.shape:
            point = [round(lat, 6), round(lon, 6)]
            if not coordinates or point != coordinates[-1]:
                coordinates.append(point)
    return coordinates


def dashboard_network(catalog: Catalog) -> dict:
    """Materialize the legacy one-route-per-vehicle dashboard contract."""
    stop_by_id = {}
    for sequence in catalog.sequences:
        for stop in sequence.stops:
            stop_by_id.setdefault(stop.stop_id, stop)
    stop_keys = {stop_id: index for index, stop_id in enumerate(sorted(stop_by_id))}
    stops = []
    for stop_id, key in stop_keys.items():
        stop = stop_by_id[stop_id]
        name = next((v.address for v in stop.visits if v.address.strip()), "Остановка б/н")
        stops.append(
            {
                "stop_key": key,
                "lat": stop.coord[1],
                "lon": stop.coord[0],
                "name": name,
                "physical_stop_id": stop_id,
            }
        )

    pattern_by_id = {pattern.route_pattern_id: pattern for pattern in catalog.patterns}
    routes = []
    vehicle_ids = sorted({sequence.tr_id for sequence in catalog.sequences})
    for tr_id in vehicle_ids:
        sequences = sorted(
            (s for s in catalog.sequences if s.tr_id == tr_id),
            key=lambda s: (s.start_time, s.sequence_id),
        )
        ordered_stops = []
        seen_stops = set()
        segment_options = {}
        for sequence in sequences:
            for stop in sequence.stops:
                key = stop_keys[stop.stop_id]
                if key not in seen_stops:
                    ordered_stops.append(key)
                    seen_stops.add(key)
            for leg in sequence.legs:
                pair = (
                    sequence.route_pattern_id,
                    stop_keys[leg.start.stop_id],
                    stop_keys[leg.end.stop_id],
                )
                pattern = pattern_by_id.get(sequence.route_pattern_id)
                canonical_trip_id = pattern.canonical_trip_id if pattern else None
                variants = sorted(
                    catalog.variants.get(leg.segment_id, ()),
                    key=lambda variant: _pattern_variant_rank(variant, canonical_trip_id),
                )
                candidate = (variants[0] if variants else None, sequence, leg)
                current = segment_options.get(pair)
                if current is None or (
                    candidate[0] is not None
                    and (
                        current[0] is None
                        or _variant_rank(candidate[0]) < _variant_rank(current[0])
                    )
                ):
                    segment_options[pair] = candidate
        segments = []
        for (_, start_key, end_key), (variant, sequence, leg) in sorted(
            segment_options.items(), key=lambda item: tuple(str(value) for value in item[0])
        ):
            if variant is None:
                start, end = leg.start.coord, leg.end.coord
                path = [[start[1], start[0]], [end[1], end[0]]]
                source, support, quality, length_m, synthetic = (
                    "direct",
                    0,
                    "unknown",
                    distance(start, end),
                    True,
                )
            else:
                path = _coordinates(variant)
                source, support, length_m, synthetic = (
                    variant.source,
                    variant.support,
                    variant.path.length_m,
                    False,
                )
                quality = "confirmed" if variant.confirmed else "tentative"
            segments.append(
                {
                    "from": start_key,
                    "to": end_key,
                    "synthetic": synthetic,
                    "path": path,
                    "segment_id": leg.segment_id,
                    "route_pattern_id": sequence.route_pattern_id,
                    "source": source,
                    "quality": quality,
                    "support": support,
                    "length_m": round(length_m, 2),
                }
            )
        routes.append(
            {
                "route_id": f"R{tr_id}",
                "tr_id": tr_id,
                "name": f"Маршрут ТС {tr_id}",
                "speed_norm_kmh": None,
                "stops": ordered_stops,
                "segments": segments,
            }
        )
    return {"stops": stops, "routes": routes}


def write_dashboard_network(catalog: Catalog, path: str | Path) -> dict:
    payload = dashboard_network(catalog)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload
