"""Valhalla HTTP boundary and content-addressed, graph-specific request cache."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from .geo import decode_polyline, polyline_length
from .types import Coord, Edge, Event, MatchedPoint, RoadPath, Trace, digest


class GraphError(RuntimeError):
    pass


class RoadGraph(Protocol):
    version: str

    def routes(self, start: Coord, end: Coord) -> tuple[RoadPath, ...]: ...

    def route_through(self, stops: tuple[Coord, ...]) -> RoadPath: ...

    def trace(self, events: list[Event], timeout: float = 1.0) -> Trace: ...


class Valhalla:
    def __init__(self, url: str, version: str, cache_dir: Path | None = None):
        if not version or version in {"latest", "unknown"}:
            raise ValueError("A pinned graph version is required")
        self.url, self.version, self.cache_dir = url.rstrip("/"), version, cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def request(self, action: str, payload: dict, timeout: float = 30.0) -> dict:
        key = digest((self.version, action, payload))
        cache = self.cache_dir / (key + ".json") if self.cache_dir else None
        if cache and cache.exists():
            return json.loads(cache.read_text())
        request = urllib.request.Request(
            self.url + "/" + action,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except OSError:
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise GraphError(f"Valhalla {action}: HTTP {exc.code}{suffix}") from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise GraphError(f"Valhalla {action}: {exc}") from exc
        if "error" in result:
            raise GraphError(str(result["error"]))
        if cache:
            temp = cache.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            temp.write_text(json.dumps(result))
            temp.replace(cache)
        return result

    def health(self) -> dict:
        try:
            with urllib.request.urlopen(self.url + "/status", timeout=2) as response:
                result = json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise GraphError(f"Valhalla status: {exc}") from exc
        if "version" not in result:
            raise GraphError("Valhalla status response has no version")
        return result

    def _attributes(self, payload: dict, timeout: float) -> Trace:
        payload.update(
            costing="bus",
            units="kilometers",
            filters={
                "action": "include",
                "attributes": [
                    "shape",
                    "edge.id",
                    "edge.way_id",
                    "edge.length",
                    "edge.begin_shape_index",
                    "edge.end_shape_index",
                    "edge.begin_osm_node_id",
                    "edge.end_osm_node_id",
                    "matched.point",
                    "matched.type",
                    "matched.edge_index",
                    "matched.distance_along_edge",
                    "matched.distance_from_trace_point",
                    "matched.begin_route_discontinuity",
                    "matched.end_route_discontinuity",
                ],
            },
        )
        return parse_trace(self.request("trace_attributes", payload, timeout), self.version)

    def routes(self, start: Coord, end: Coord) -> tuple[RoadPath, ...]:
        response = self.request(
            "route",
            {
                "locations": [{"lon": p[0], "lat": p[1]} for p in (start, end)],
                "costing": "bus",
                "alternates": 2,
                "units": "kilometers",
            },
        )
        trips = [response] + response.get("alternates", [])
        paths = []
        for result in trips:
            trip = result.get("trip", result)
            legs = trip.get("legs", [])
            if len(legs) != 1:
                continue
            # edge_walk extracts attributes from the EXACT returned route, not a second snap.
            trace = self._attributes(
                {"encoded_polyline": legs[0]["shape"], "shape_match": "edge_walk"}, 30
            )
            if trace.path.edges and trace.path.signature not in [p.signature for p in paths]:
                paths.append(trace.path)
        return tuple(paths)

    def route_through(self, stops: tuple[Coord, ...]) -> RoadPath:
        """Return one connected path through an ordered stop sequence."""
        if len(stops) < 2:
            raise GraphError("A pattern route needs at least two stops")
        response = self.request(
            "route",
            {
                "locations": [{"lon": lon, "lat": lat, "type": "break"} for lon, lat in stops],
                "costing": "bus",
                "units": "kilometers",
            },
        )
        trip = response.get("trip", response)
        legs = trip.get("legs", [])
        if len(legs) != len(stops) - 1:
            raise GraphError("Valhalla pattern route returned an unexpected leg count")
        edges = []
        for leg in legs:
            trace = self._attributes(
                {"encoded_polyline": leg["shape"], "shape_match": "edge_walk"}, 30
            )
            if not trace.path.edges:
                raise GraphError("Valhalla pattern route contains an empty leg")
            edges.extend(trace.path.edges)
        return RoadPath(tuple(edges))

    def trace(self, events: list[Event], timeout: float = 1.0) -> Trace:
        if len(events) < 2 or not all(e.valid for e in events):
            raise GraphError("Trace needs at least two valid GPS observations")
        return self._attributes(
            {
                "shape": [{"lon": e.lon, "lat": e.lat, "time": int(e.event_time)} for e in events],
                "shape_match": "map_snap",
                # The standard service limit is 100 m; larger values fail before matching.
                "trace_options": {"gps_accuracy": 25, "search_radius": 100},
            },
            timeout,
        )


def parse_trace(raw: dict, version: str) -> Trace:
    try:
        shape = decode_polyline(raw["shape"])
        edges = []
        for row in raw["edges"]:
            coords = shape[row["begin_shape_index"] : row["end_shape_index"] + 1]
            if len(coords) < 2:
                # Preserve edge indices, including degenerate intersection edges.
                point = coords[0] if coords else shape[row["begin_shape_index"]]
                coords = (point, point)
            edges.append(
                Edge(
                    f"{version}:{row['id']}",
                    str(row.get("way_id", "")),
                    coords,
                    polyline_length(coords),
                    float(row.get("source_percent_along", 0)),
                    float(row.get("target_percent_along", 1)),
                    str(row["begin_osm_node_id"]) if "begin_osm_node_id" in row else None,
                    str(row["end_osm_node_id"]) if "end_osm_node_id" in row else None,
                )
            )
        points = []
        for row in raw.get("matched_points", []):
            valid = row.get("type") != "unmatched" and "edge_index" in row
            index = int(row["edge_index"]) if valid else None
            if index is not None and not 0 <= index < len(edges):
                raise ValueError("Matched point edge index is out of bounds")
            points.append(
                MatchedPoint(
                    (row["lon"], row["lat"]) if valid else None,
                    index,
                    row.get("distance_along_edge") if valid else None,
                    row.get("distance_from_trace_point") if valid else None,
                    bool(
                        row.get("begin_route_discontinuity") or row.get("end_route_discontinuity")
                    ),
                )
            )
        return Trace(RoadPath(tuple(edges)), tuple(points))
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise GraphError(f"Invalid trace_attributes response: {exc}") from exc
