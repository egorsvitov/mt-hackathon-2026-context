from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .audit import dataset_audit
from .catalog import Catalog, build_catalog
from .data import read_events, read_points, read_schedule
from .export import write_geojson
from .graph import Valhalla
from .ml import catboost_ablation
from .replay import replay, write_results
from .types import Config, timestamp


def graph(args) -> Valhalla:
    manifest = json.loads(args.graph_manifest.read_text())
    return Valhalla(args.valhalla_url, manifest["graph_version"], args.graph_cache)


def common_graph(parser):
    parser.add_argument("--valhalla-url", default=os.getenv("VALHALLA_URL", "http://127.0.0.1:8002"))
    parser.add_argument("--graph-manifest", type=Path, required=True)
    parser.add_argument("--graph-cache", type=Path, default=Path("artifacts/graph_cache"))


def data_dir(value: str | None) -> Path:
    raw = value or os.getenv("DATA_DIR")
    if not raw:
        raise ValueError("Set DATA_DIR or pass --data-dir")
    path = Path(raw).expanduser().resolve()
    if not (path / "train/traffic.csv").exists():
        raise ValueError(f"Invalid DATA_DIR: {path}")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="route-match")
    parser.add_argument("--timezone", default="Europe/Moscow")
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--data-dir")

    build = sub.add_parser("build-catalog")
    build.add_argument("--data-dir")
    build.add_argument("--cutoff", default="2026-01-06 02:05:00")
    build.add_argument("--output", type=Path, required=True)
    common_graph(build)

    run = sub.add_parser("replay")
    run.add_argument("--data-dir")
    run.add_argument("--split", choices=("train", "test", "validate"), required=True)
    run.add_argument("--catalog", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--report", type=Path)
    run.add_argument("--mode", choices=("hmm", "nearest", "meili"), default="hmm")
    common_graph(run)

    export = sub.add_parser("export-geojson")
    export.add_argument("--catalog", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)

    ml = sub.add_parser("catboost-experiment")
    ml.add_argument("--feature-cache", type=Path, required=True)
    ml.add_argument("--spatial-dir", type=Path, required=True)
    ml.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "audit":
        result = dataset_audit(data_dir(args.data_dir), args.timezone)
    elif args.command == "build-catalog":
        data = data_dir(args.data_dir)
        road_graph = graph(args)
        road_graph.health()
        result = build_catalog(read_schedule(data / "train/schedule.csv", args.timezone),
                               read_events(data / "train/traffic.csv", args.timezone),
                               timestamp(args.cutoff, args.timezone), road_graph, Config())
        result.save(args.output)
        result = {"catalog": str(args.output), "version": result.version, **result.report}
    elif args.command == "replay":
        data = data_dir(args.data_dir)
        catalog = Catalog.load(args.catalog)
        definitions = {
            "train": (data / "train/traffic.csv", data / "labels/labels_train.csv"),
            "test": (data / "test/traffic.csv", data / "labels/labels_test.csv"),
            "validate": (data / "validate/traffic.csv", data / "validate/points.csv"),
        }
        events, points = definitions[args.split]
        results, result = replay(read_events(events, args.timezone), read_points(points, args.timezone),
                                 catalog, graph(args), mode=args.mode)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_results(results, args.output)
        report = args.report or args.output.with_suffix(".report.json")
        report.write_text(json.dumps(result, indent=2) + "\n")
    elif args.command == "export-geojson":
        catalog = Catalog.load(args.catalog)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_geojson(catalog, args.output)
        result = {"features": sum(len(v) for v in catalog.variants.values()), "output": str(args.output)}
    else:
        result = catboost_ablation(args.feature_cache, args.spatial_dir, args.output)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
