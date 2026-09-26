# Route matching experiment

Причинное восстановление дорожных трасс между плановыми остановками и потоковая привязка
телеметрии. Модуль не загружает `time_fact_begin`: каталог имеет фиксированный cutoff, а snapshot
на `T` использует только события с `event_time <= T`.

## Что реализовано

- точные физические места остановок, алиасы посещений и альтернативы одинакового времени;
- холодный старт через локальный Valhalla с `costing=bus` и до двух альтернатив;
- Meili `trace_attributes` исторических проходов, последовательное выравнивание остановок;
- кластеризация последовательностей рёбер и medoid реального прохода;
- инкрементальный HMM/Viterbi с расстоянием, курсом, скоростью, beam и повторными рёбрами;
- ограниченный по времени Meili fallback, off-route hysteresis и immutable snapshots;
- CSV replay, NDTP `TrafficRow`-адаптер, ML-признаки с missing masks и GeoJSON;
- сравнение `nearest`, полного `meili` и маршрутного `hmm`; CatBoost-абляция.

## Установка и проверки

```bash
cd mt-hackathon-2026-context/experiments/route_matching
python3.12 -m venv .venv
.venv/bin/pip install -e '.[ml]' pytest ruff
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Можно собрать CLI без локального Python:

```bash
docker build -t route-matching:local .
```

## Valhalla и воспроизводимость графа

Нужен PBF, который покрывает bbox остановок `37.148758…37.865917`,
`55.518080…55.984216`. Для текущего набора достаточно московского extract.

```bash
python scripts/prepare_graph.py \
  --pbf /absolute/path/to/moscow.osm.pbf \
  --osm-dir /absolute/path/to/valhalla-graph \
  --source-url https://download.openstreetmap.fr/extracts/russia/central_federal_district/moscow-latest.osm.pbf
docker compose up -d valhalla
curl -fsS http://127.0.0.1:8002/status
```

Скрипт фиксирует digest Docker-образа, SHA-256 PBF, параметры сборки и `graph_version`.
Артефакты графа и PBF находятся вне Git.

## Основной прогон

```bash
export DATA_DIR=/absolute/path/to/dataset
GRAPH=/absolute/path/to/valhalla-graph/graph_manifest.json

route-match audit
route-match build-catalog --graph-manifest "$GRAPH" \
  --cutoff '2026-01-06 02:05:00' --output artifacts/catalog-0205.json
route-match replay --graph-manifest "$GRAPH" --catalog artifacts/catalog-0205.json \
  --split test --mode hmm --output artifacts/test.jsonl
route-match export-geojson --catalog artifacts/catalog-0205.json \
  --output artifacts/catalog.geojson
```

Для сравнений повторить replay с `--mode nearest` и `--mode meili`. Отчёт рядом с JSONL содержит
покрытие, fallback и p50/p95 времени обработки. Каталоги для дополнительных срезов строятся тем
же вызовом с cutoff `2026-01-06 12:00:00` и `2026-01-06 18:00:00`; сравнивать их можно только с
последующими точками.

Для ML-абляции сначала получить `train.jsonl` и `test.jsonl` в одной папке, затем:

```bash
route-match catboost-experiment \
  --feature-cache ../../ml_models/01_catboost_baseline/artifacts/features \
  --spatial-dir artifacts/spatial --output artifacts/catboost_report.json
```

## Ограничения

Valhalla `edge.id` стабилен только внутри конкретной сборки графа, поэтому каталог невозможно
загрузить с другим `graph_version`. `confidence_margin` — диагностическая разница лог-оценок,
не вероятность. `off_route` известен только после подтверждённого историей маршрута; для OSM-
гипотезы возвращается `null`.
