"""Diagnostic GeoJSON for catalog review; it is not a dashboard dependency."""
from __future__ import annotations

import json
from pathlib import Path

from .catalog import Catalog


def write_geojson(catalog: Catalog, path: str | Path):
    features = []
    for sequence in catalog.sequences:
        for leg in sequence.legs:
            for rank, variant in enumerate(catalog.variants.get(leg.segment_id, ())):
                coords = []
                for edge in variant.path.edges:
                    coords.extend(edge.shape if not coords else edge.shape[1:])
                if len(coords) < 2:
                    continue
                features.append({"type": "Feature", "geometry": {"type": "LineString",
                                                                   "coordinates": coords},
                                 "properties": {
                                     "sequence_id": sequence.sequence_id, "tr_id": sequence.tr_id,
                                     "leg_index": leg.index, "segment_id": leg.segment_id,
                                     "variant_id": variant.variant_id, "rank": rank,
                                     "source": variant.source, "support": variant.support,
                                     "support_fraction": variant.support_fraction,
                                     "confirmed": variant.confirmed,
                                     "length_m": variant.path.length_m}})
    Path(path).write_text(json.dumps({"type": "FeatureCollection", "features": features},
                                     ensure_ascii=False, allow_nan=False))
