"""Causal route discovery and streaming map matching."""

from .catalog import Catalog, build_catalog
from .matcher import Matcher
from .types import Config, Event, MatchState

__all__ = [
    "Catalog",
    "Config",
    "Event",
    "MatchState",
    "Matcher",
    "StreamingSpatialAdapter",
    "build_catalog",
]
from .adapters import StreamingSpatialAdapter
