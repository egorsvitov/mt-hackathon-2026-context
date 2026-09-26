"""Thin adapters for a streaming backend and its ML payload."""

from __future__ import annotations

from .catalog import Catalog
from .data import from_traffic_row
from .matcher import Matcher
from .types import DEFAULT_CONFIG, Config, MatchState


class StreamingSpatialAdapter:
    """Own one matcher and expose failure-tolerant dashboard/ML views."""

    def __init__(
        self,
        catalog: Catalog,
        graph=None,
        config: Config = DEFAULT_CONFIG,
        timezone_name: str = "Europe/Moscow",
    ):
        self.catalog = catalog
        self.graph = graph
        self.config = config
        self.timezone_name = timezone_name
        self.matcher = Matcher(catalog, graph, config, live=True)
        self.last_error: str | None = None

    def close(self) -> None:
        self.matcher.close()

    def reset(self) -> None:
        self.matcher.close()
        self.matcher = Matcher(self.catalog, self.graph, self.config, live=True)
        self.last_error = None

    def update(self, record) -> MatchState:
        event = from_traffic_row(record, self.timezone_name)
        return self.matcher.update(event)

    def snapshot(
        self, tr_id: int, T: float, target_visit_id: str, cur_dev_s: float | None = None
    ) -> MatchState:
        try:
            state = self.matcher.snapshot(tr_id, T, target_visit_id, cur_dev_s)
            self.last_error = None
            return state
        except (KeyError, ValueError, RuntimeError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return MatchState(
                tr_id,
                T,
                self.catalog.version,
                self.catalog.graph_version,
                reason="matcher_unavailable",
            )

    def ml_features(
        self, tr_id: int, T: float, target_visit_id: str, cur_dev_s: float | None = None
    ) -> dict[str, float | str | None]:
        return self.snapshot(tr_id, T, target_visit_id, cur_dev_s).feature_row()

    @staticmethod
    def dashboard_fields(state: MatchState) -> dict:
        return {
            "route_id": f"R{state.tr_id}",
            "route_pattern_id": state.route_pattern_id,
            "matched_lat": state.matched_lat,
            "matched_lon": state.matched_lon,
            "current_segment_id": state.current_segment_id,
            "route_progress": state.route_progress,
            "position_quality": state.position_quality,
            "off_route": state.off_route,
        }
