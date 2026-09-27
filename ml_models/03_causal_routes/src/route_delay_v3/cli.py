from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from transport_delay.config import data_dir as resolve_data_dir

from .data import prepare
from .workflow import backtest, evaluate, predict


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="delay-v3")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("artifacts/features"))
    parser.add_argument("--artifacts-dir", type=Path, default=Path("artifacts/modeling"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("backtest")
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--split", choices=("test",), required=True)
    prediction = commands.add_parser("predict")
    prediction.add_argument("--split", choices=("validate",), required=True)
    prediction.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    data = resolve_data_dir(args.data_dir)
    raw = args.catalog or os.environ.get("ROUTE_CATALOG_PATH")
    if not raw:
        raise ValueError("Pass --catalog or set ROUTE_CATALOG_PATH")
    catalog = Path(raw).expanduser().resolve()
    if not catalog.is_file():
        raise ValueError(f"Catalog does not exist: {catalog}")
    if args.command == "prepare":
        result = prepare(data, catalog, args.cache_dir)
    elif args.command == "backtest":
        result = backtest(args.cache_dir, catalog, args.artifacts_dir)
    elif args.command == "evaluate":
        result = evaluate(args.cache_dir, catalog, args.artifacts_dir)
    else:
        result = predict(data, args.cache_dir, catalog, args.artifacts_dir, args.output)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
