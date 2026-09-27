# V3: causal route experiment

This experiment replays the released day in timestamp order. It uses all train
vehicles, including the synthetic vehicles, as organizer-provided training data.
The route plan and pinned road graph are treated as known before the day begins.
No factual arrival field from any schedule CSV is loaded by the feature pipeline.

## Reproduction

From this directory:

```bash
uv sync
export DATA_DIR=/absolute/path/to/data/dataset
export GRAPH_MANIFEST=/absolute/path/to/data/valhalla-graph/graph_manifest.json
uv run route-match build-catalog --mode static_plan_graph \
  --plan-split train --data-dir "$DATA_DIR" --graph-manifest "$GRAPH_MANIFEST" \
  --output artifacts/catalog-static.json
uv run delay-v3 --catalog artifacts/catalog-static.json prepare
uv run delay-v3 --catalog artifacts/catalog-static.json backtest
uv run delay-v3 --catalog artifacts/catalog-static.json evaluate --split test
uv run delay-v3 --catalog artifacts/catalog-static.json predict --split validate \
  --output artifacts/modeling/submission.csv
uv run pytest -q
```

Valhalla must serve the graph described by `GRAPH_MANIFEST`. The catalog build
uses planned stop coordinates and road routes, never trace or historical GPS.
Unknown road segments remain unknown and use planned-duration fallback.

The telemetry journal combines train/test/validate and drops exact duplicate
packets. Conflicting payloads for one packet key stop preparation. A packet
becomes visible at `max(event_time, receive_time)`, or at `event_time` when
receive time is absent. All point features use only packets visible by the
point's `T`. A published training label becomes visible 60 seconds after its
actual target arrival. Component events become visible only after the GPS packet
that confirms the boundary. Aggregates exclude the point's own trip.

The first two train windows, 10:00–14:00 and 14:00–18:00, select one of the
predeclared candidates. The 18:00–22:00 train window and labels_test are held
out from that choice. In each validation window, all its trip occurrences are
excluded from learning. The final validate replay reveals eligible train and
test labels over time; it never reads validate facts. In particular, although
the released test schedule contains facts for validate stops, its
`time_fact_begin` field is forbidden to this experiment.

Artifacts contain the prepared point/event tables, source hashes, frozen
candidate report, hourly model bundles, test predictions, validate audit
predictions, and a strict `sample_id;prediction` submission. The experiment
measures performance on this released day; it does not claim generalization to
new days or fleets.

## Frozen offline result

With the supplied day and pinned graph, the selected candidate is `map`: a
residual CatBoost with causal map-matched state, without run/dwell estimates.
Development MAE is 61.49 s across the 10:00–18:00 real-vehicle windows
(causal v1: 67.23 s). The untouched 18:00–22:00 window is 101.70 s, and
labels_test MAE is 74.43 s over 353 points (persistence: 93.36 s). The
synthetic 18:00–22:00 slice is reported separately at 101.96 s. These values
are descriptive, not a basis for further tuning or leaderboard selection.

Complete fold and ablation details live in
`artifacts/modeling/backtest_report.json`; coverage and data hashes live in
`artifacts/features/prepare_report.json` and `manifest.json`. Generated
artifacts are local and deliberately excluded from Git.
