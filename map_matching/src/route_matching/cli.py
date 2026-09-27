from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .catalog import Catalog, build_catalog
from .dashboard_replay import write_matched_dashboard_replay
from .data import read_events, read_schedule
from .export import write_dashboard_network
from .graph import Valhalla
from .types import Config


def _data_dir(value: str | None) -> Path:
    raw = value or os.getenv("DATA_DIR")
    if not raw:
        raise ValueError("Set DATA_DIR or pass --data-dir")
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"Invalid DATA_DIR: {path}")
    return path


def _graph(args: argparse.Namespace) -> Valhalla:
    manifest = json.loads(args.graph_manifest.read_text(encoding="utf-8"))
    return Valhalla(args.valhalla_url, manifest["graph_version"], args.graph_cache)


def _add_graph_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--valhalla-url",
        default=os.getenv("VALHALLA_URL", "http://127.0.0.1:8002"),
    )
    parser.add_argument("--graph-manifest", type=Path, required=True)
    parser.add_argument("--graph-cache", type=Path, default=Path("artifacts/graph_cache"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="route-match")
    parser.add_argument("--timezone", default="Europe/Moscow")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build-catalog")
    build.add_argument("--data-dir")
    build.add_argument("--history-split", choices=("train", "test"), default="train")
    build.add_argument(
        "--mode", choices=("offline_history", "static_plan_graph"), default="offline_history"
    )
    build.add_argument("--plan-split", choices=("train", "test", "validate"), default="train")
    build.add_argument("--output", type=Path, required=True)
    _add_graph_options(build)

    network = commands.add_parser("export-dashboard-network")
    network.add_argument("--catalog", type=Path, required=True)
    network.add_argument("--output", type=Path, required=True)

    replay = commands.add_parser("export-dashboard-replay")
    replay.add_argument("--catalog", type=Path, required=True)
    replay.add_argument("--network", type=Path, required=True)
    replay.add_argument("--input", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)

    if args.command == "build-catalog":
        data = _data_dir(args.data_dir)
        split = args.history_split
        road_graph = _graph(args)
        road_graph.health()
        schedule = (
            data
            / args.plan_split
            / ("schedule_plan.csv" if args.plan_split == "validate" else "schedule.csv")
            if args.mode == "static_plan_graph"
            else data / split / "schedule.csv"
        )
        catalog = build_catalog(
            read_schedule(schedule, args.timezone),
            []
            if args.mode == "static_plan_graph"
            else read_events(data / split / "traffic.csv", args.timezone),
            cutoff=None,
            graph=road_graph,
            config=Config(),
            provenance={"plan_split": args.plan_split}
            if args.mode == "static_plan_graph"
            else {"history_split": split},
            mode=args.mode,
        )
        catalog.save(args.output)
        result = {"catalog": str(args.output), "version": catalog.version, **catalog.report}
    elif args.command == "export-dashboard-network":
        payload = write_dashboard_network(Catalog.load(args.catalog), args.output)
        result = {
            "stops": len(payload["stops"]),
            "routes": len(payload["routes"]),
            "segments": sum(len(route["segments"]) for route in payload["routes"]),
            "output": str(args.output),
        }
    else:
        result = write_matched_dashboard_replay(
            Catalog.load(args.catalog),
            args.network,
            args.input,
            args.output,
        )

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
