from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .config import data_dir
from .model import ModelBundle, diagnostics, mae, predict
from .workflow import prepare, run_selection, validate_submission


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="delay")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("artifacts/features"))
    parser.add_argument("--artifacts-dir", type=Path, default=Path("artifacts/modeling"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare", help="Build causal feature tables")
    sub.add_parser("train", help="Select, train, evaluate and create submission")
    evaluate = sub.add_parser("evaluate", help="Evaluate a saved bundle on prepared split")
    evaluate.add_argument("--split", choices=("train", "test"), default="test")
    evaluate.add_argument("--model-dir", type=Path, default=Path("artifacts/modeling/model"))
    pred = sub.add_parser("predict", help="Predict a prepared split")
    pred.add_argument("--split", choices=("train", "test", "validate"), default="validate")
    pred.add_argument("--model-dir", type=Path, default=Path("artifacts/modeling/model"))
    pred.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "prepare":
        report = prepare(data_dir(args.data_dir), args.cache_dir)
    elif args.command == "train":
        # Keep workflow's template source consistent with an explicit CLI override.
        if args.data_dir:
            import os

            os.environ["DATA_DIR"] = str(args.data_dir.resolve())
        report = run_selection(args.cache_dir, args.artifacts_dir)
    elif args.command == "evaluate":
        frame = pd.read_parquet(args.cache_dir / f"{args.split}.parquet")
        predicted = predict(frame, ModelBundle.load(args.model_dir))
        report = diagnostics(frame, predicted)
        report["persistence_mae"] = mae(frame["target_delay_s"], frame["cur_dev_s"])
    else:
        frame = pd.read_parquet(args.cache_dir / f"{args.split}.parquet")
        predicted = predict(frame, ModelBundle.load(args.model_dir))
        result = pd.DataFrame({"sample_id": frame["sample_id"], "prediction": predicted})
        if args.split == "validate":
            template = pd.read_csv(data_dir(args.data_dir) / "sample_submission.csv", sep=";")
            result = template[["sample_id"]].merge(
                result, on="sample_id", how="left", validate="one_to_one"
            )
            validate_submission(result, template)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(args.output, sep=";", index=False)
        report = {"rows": len(result), "output": str(args.output)}
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
