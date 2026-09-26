from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .audit import dataset_audit
from .catalog import Catalog, build_catalog
from .dashboard_replay import write_matched_dashboard_replay
from .data import read_events, read_points, read_schedule
from .evaluation import compare_result_files
from .export import write_dashboard_network, write_geojson
from .graph import Valhalla
from .ml import build_spatial_features, catboost_ablation
from .replay import replay, write_results
from .types import Config, timestamp


def graph(args) -> Valhalla:
    manifest = json.loads(args.graph_manifest.read_text())
    return Valhalla(args.valhalla_url, manifest["graph_version"], args.graph_cache)


def common_graph(parser):
    parser.add_argument(
        "--valhalla-url", default=os.getenv("VALHALLA_URL", "http://127.0.0.1:8002")
    )
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
    build.add_argument(
        "--cutoff", help="Optional legacy history boundary; omitted for offline history"
    )
    build.add_argument("--exclude-trip-id", action="append", default=[])
    build.add_argument("--history-split", choices=("train", "test"), default="train")
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

    dashboard = sub.add_parser("export-dashboard-network")
    dashboard.add_argument("--catalog", type=Path, required=True)
    dashboard.add_argument("--schedule", type=Path)
    dashboard.add_argument("--output", type=Path, required=True)

    dashboard_replay = sub.add_parser("export-dashboard-replay")
    dashboard_replay.add_argument("--catalog", type=Path, required=True)
    dashboard_replay.add_argument("--network", type=Path, required=True)
    dashboard_replay.add_argument("--input", type=Path, required=True)
    dashboard_replay.add_argument("--output", type=Path, required=True)
    dashboard_replay.add_argument("--mode", choices=("hmm", "nearest"), default="hmm")

    spatial = sub.add_parser("export-spatial-features")
    spatial.add_argument("--data-dir")
    spatial.add_argument("--split", choices=("train", "test", "validate"), required=True)
    spatial.add_argument("--catalog", type=Path, required=True)
    spatial.add_argument("--output", type=Path, required=True)

    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--input", action="append", required=True, metavar="MODE=JSONL")
    evaluate.add_argument("--output", type=Path, required=True)

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
        split = args.history_split
        result = build_catalog(
            read_schedule(data / split / "schedule.csv", args.timezone),
            read_events(data / split / "traffic.csv", args.timezone),
            timestamp(args.cutoff, args.timezone) if args.cutoff else None,
            road_graph,
            Config(),
            excluded_trip_ids=set(args.exclude_trip_id),
            provenance={"history_split": split},
        )
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
        results, result = replay(
            read_events(events, args.timezone),
            read_points(points, args.timezone),
            catalog,
            graph(args),
            mode=args.mode,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_results(results, args.output)
        report = args.report or args.output.with_suffix(".report.json")
        report.write_text(json.dumps(result, indent=2) + "\n")
    elif args.command == "export-geojson":
        catalog = Catalog.load(args.catalog)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_geojson(catalog, args.output)
        result = {
            "features": sum(len(v) for v in catalog.variants.values()),
            "output": str(args.output),
        }
    elif args.command == "export-dashboard-network":
        if args.schedule and not args.schedule.exists():
            raise ValueError(f"Schedule does not exist: {args.schedule}")
        payload = write_dashboard_network(Catalog.load(args.catalog), args.output)
        result = {
            "stops": len(payload["stops"]),
            "routes": len(payload["routes"]),
            "segments": sum(len(route["segments"]) for route in payload["routes"]),
            "output": str(args.output),
        }
    elif args.command == "export-dashboard-replay":
        result = write_matched_dashboard_replay(
            Catalog.load(args.catalog), args.network, args.input, args.output, args.mode
        )
    elif args.command == "export-spatial-features":
        try:
            import pandas as pd
        except ImportError as exc:
            raise RuntimeError("Install the project with the 'ml' extra") from exc
        data = data_dir(args.data_dir)
        split = args.split
        point_columns = [
            "sample_id",
            "tr_id",
            "T",
            "target_stop_id",
            "target_time_begin",
            "cur_dev_s",
        ]
        traffic_columns = [
            "tr_id",
            "event_time",
            "receive_time",
            "lon",
            "lat",
            "speed",
            "heading",
            "location_valid",
            "packet_id",
        ]
        schedule_columns = ["tt_action_item_id", "tr_id", "time_begin", "geom", "building_address"]
        traffic = pd.read_csv(data / split / "traffic.csv", usecols=traffic_columns)
        if split == "validate":
            points = pd.read_csv(data / "validate/points.csv", usecols=point_columns)
            schedule = pd.read_csv(data / "validate/schedule_plan.csv", usecols=schedule_columns)
        else:
            points = pd.read_csv(data / f"labels/labels_{split}.csv", usecols=point_columns)
            schedule = pd.read_csv(data / split / "schedule.csv", usecols=schedule_columns)
        features = build_spatial_features(
            points, traffic, schedule, Catalog.load(args.catalog), args.timezone
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.suffix == ".parquet":
            features.to_parquet(args.output, index=False)
        else:
            features.to_json(args.output, orient="records", lines=True, force_ascii=False)
        result = {
            "rows": len(features),
            "columns": len(features.columns),
            "output": str(args.output),
        }
    elif args.command == "evaluate":
        inputs = {}
        for value in args.input:
            if "=" not in value:
                raise ValueError("--input must have MODE=JSONL form")
            name, path = value.split("=", 1)
            inputs[name] = Path(path)
        result = compare_result_files(inputs)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    else:
        result = catboost_ablation(args.feature_cache, args.spatial_dir, args.output)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
