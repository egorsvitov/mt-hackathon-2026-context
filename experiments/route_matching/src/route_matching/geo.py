"""Small-area metric geometry without native dependencies.

The released stops lie inside Moscow. An equirectangular projection around 55.75°N
has sub-metre error at route-segment scale and avoids a native GIS runtime.
"""
from __future__ import annotations

import math
from dataclasses import replace

from .types import Coord, Edge, RoadPath

EARTH_M = 6_371_008.8
REF_LAT_RAD = math.radians(55.75)
COS_REF = math.cos(REF_LAT_RAD)


def xy(coord: Coord) -> Coord:
    return math.radians(coord[0]) * EARTH_M * COS_REF, math.radians(coord[1]) * EARTH_M


def lonlat(coord: Coord) -> Coord:
    return math.degrees(coord[0] / (EARTH_M * COS_REF)), math.degrees(coord[1] / EARTH_M)


def distance(a: Coord, b: Coord) -> float:
    ax, ay = xy(a)
    bx, by = xy(b)
    return math.hypot(bx - ax, by - ay)


def line(shape: tuple[Coord, ...]) -> tuple[Coord, ...]:
    return shape


def polyline_length(shape: tuple[Coord, ...]) -> float:
    return sum(distance(a, b) for a, b in zip(shape, shape[1:]))


def angle_difference(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def project(shape: tuple[Coord, ...], coord: Coord) -> tuple[float, float, Coord, float]:
    """Return distance, offset, snapped lon/lat and directed heading."""
    if len(shape) < 2:
        raise ValueError("Polyline needs two points")
    px, py = xy(coord)
    best = None
    cursor = 0.0
    for a, b in zip(shape, shape[1:]):
        ax, ay = xy(a)
        bx, by = xy(b)
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        fraction = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2)) if length2 else 0.0
        sx, sy = ax + fraction * dx, ay + fraction * dy
        d = math.hypot(px - sx, py - sy)
        length = math.sqrt(length2)
        candidate = (d, cursor + fraction * length, lonlat((sx, sy)),
                     math.degrees(math.atan2(dx, dy)) % 360)
        if best is None or candidate[0] < best[0]:
            best = candidate
        cursor += length
    return best


def joinable(a: Edge, b: Edge, tolerance_m: float = 3.0) -> bool:
    if distance(a.shape[-1], b.shape[0]) > tolerance_m:
        return False
    if a.edge_id == b.edge_id:
        return abs(a.target_fraction - b.source_fraction) < 0.02
    # These paths meet at a shared planned stop. Valhalla validates each directed
    # path; exact endpoint continuity is therefore a stronger portable invariant
    # than OSM node ids, which trace_attributes does not expose on all builds.
    return True


def _point_at(shape: tuple[Coord, ...], wanted: float) -> Coord:
    cursor = 0.0
    for a, b in zip(shape, shape[1:]):
        length = distance(a, b)
        if cursor + length >= wanted or b == shape[-1]:
            f = max(0.0, min(1.0, (wanted - cursor) / length)) if length else 0.0
            ax, ay = xy(a)
            bx, by = xy(b)
            return lonlat((ax + f * (bx - ax), ay + f * (by - ay)))
        cursor += length
    return shape[-1]


def _slice(shape: tuple[Coord, ...], start: float, end: float) -> tuple[Coord, ...]:
    total = polyline_length(shape)
    start, end = max(0.0, start), min(total, end)
    output = [_point_at(shape, start)]
    cursor = 0.0
    for a, b in zip(shape, shape[1:]):
        cursor += distance(a, b)
        if start < cursor < end:
            output.append(b)
    output.append(_point_at(shape, end))
    return tuple(output)


def clip_path(path: RoadPath, start: float, end: float) -> RoadPath:
    result = []
    cursor = 0.0
    for edge in path.edges:
        lo, hi = max(0.0, start - cursor), min(edge.length_m, end - cursor)
        cursor += edge.length_m
        if hi <= lo or edge.length_m <= 0:
            continue
        span = edge.target_fraction - edge.source_fraction
        result.append(replace(edge, shape=_slice(edge.shape, lo, hi), length_m=hi - lo,
                              source_fraction=edge.source_fraction + span * lo / edge.length_m,
                              target_fraction=edge.source_fraction + span * hi / edge.length_m))
    return RoadPath(tuple(result))


def decode_polyline(encoded: str, precision: int = 6) -> tuple[Coord, ...]:
    cursor = lat = lon = 0
    output = []
    while cursor < len(encoded):
        values = []
        for _ in range(2):
            result = shift = 0
            while True:
                if cursor >= len(encoded):
                    raise ValueError("Truncated polyline")
                byte = ord(encoded[cursor]) - 63
                cursor += 1
                result |= (byte & 31) << shift
                shift += 5
                if byte < 32:
                    break
            values.append(~(result >> 1) if result & 1 else result >> 1)
        lat += values[0]
        lon += values[1]
        output.append((lon / 10**precision, lat / 10**precision))
    return tuple(output)


def edit_distance(a: tuple, b: tuple) -> float:
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (left != right)))
        previous = current
    return previous[-1] / max(len(a), len(b), 1)
