from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .config import catalog_path, data_dir
from .workflow import evaluate, predict_validate, prepare, run_training


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="delay-v2")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("artifacts/features"))
    parser.add_argument("--events-dir", type=Path, default=Path("artifacts/events"))
    parser.add_argument("--artifacts-dir", type=Path, default=Path("artifacts/modeling"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    training = commands.add_parser("train")
    training.add_argument(
        "--final-train-only",
        action="store_true",
        help="fit the validate submission model and component models without test data",
    )
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument(
        "--model-dir", type=Path, default=Path("artifacts/modeling/evaluation_model")
    )
    prediction = commands.add_parser("predict")
    prediction.add_argument("--model-dir", type=Path, default=Path("artifacts/modeling/model"))
    prediction.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    data = data_dir(args.data_dir)
    catalog = catalog_path(args.catalog)
    os.environ["DATA_DIR"] = str(data)
    if args.command == "prepare":
        result = prepare(data, catalog, args.cache_dir, args.events_dir)
    elif args.command == "train":
        result = run_training(
            data,
            catalog,
            args.cache_dir,
            args.events_dir,
            args.artifacts_dir,
            final_train_only=args.final_train_only,
        )
    elif args.command == "evaluate":
        result = evaluate(args.cache_dir, args.model_dir)
    else:
        result = predict_validate(data, args.cache_dir, args.model_dir, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

