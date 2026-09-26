# Route matching 0.2

Общий пространственный слой для CatBoost и диспетчерского дашборда. Он восстанавливает
направленные паттерны маршрутов по плановым остановкам и историческим GPS, а затем причинно
привязывает поток телеметрии к выбранному паттерну.

Реализация сочетает три идеи:

- Zhou et al. 2019: уплотнение редкой последовательности остановок историческими GPS;
- HMM/Newson–Krumm и Valhalla Meili: вероятностная привязка к связной дорожной геометрии;
- Wai–Zhou 2020: текущий сегмент, доля его прохождения и историческое время сегментов как
  входы модели времени прибытия.

## Контракты

`Catalog` содержит физические остановки, `RoutePattern`, `TripOccurrence` (`Sequence`),
`VisitAssignment`, варианты дорожной геометрии и provenance. Каталог строится офлайн и может
использоваться для прогноза на более раннем условном `T`: это статическая историческая карта, а
не состояние потока.

`Matcher.update(event)` принимает пакет телеметрии. `Matcher.snapshot(tr_id, T,
target_visit_id, cur_dev_s)` возвращает неизменяемый `MatchState`. Snapshot использует только
пакеты, доступные к `T`; поздний пакет перестраивает лишь ещё не опубликованное окно.

`build_spatial_features(points, telemetry, schedule_plan, route_catalog)` формирует одну строку
на `sample_id`. Числовые поля имеют префикс `mm_` и missing masks, строковые ID передаются
CatBoost как категориальные признаки. Функция не читает labels и игнорирует фактические поля
расписания.

## Установка

```bash
cd mt-hackathon-2026-context/experiments/route_matching
python3.12 -m venv .venv
.venv/bin/pip install -e '.[ml]' pytest ruff
.venv/bin/pytest -q
.venv/bin/ruff check .
```

## Граф Valhalla

Нужен PBF, покрывающий bbox остановок `37.148758…37.865917`,
`55.518080…55.984216`.

```bash
python scripts/prepare_graph.py \
  --pbf /absolute/path/to/central_federal_district-latest.osm.pbf \
  --osm-dir /absolute/path/to/valhalla-graph \
  --source-url https://download.openstreetmap.fr/extracts/russia/central_federal_district-latest.osm.pbf
docker compose up -d valhalla
curl -fsS http://127.0.0.1:8002/status
```

Manifest фиксирует SHA-256 PBF, digest Docker-образа, параметры сборки и `graph_version`.
PBF, tiles, HTTP cache и replay-артефакты не добавляются в Git.

## Построение и replay

```bash
export DATA_DIR=/absolute/path/to/dataset
GRAPH=/absolute/path/to/valhalla-graph/graph_manifest.json

route-match audit
route-match build-catalog \
  --graph-manifest "$GRAPH" \
  --history-split train \
  --output artifacts/catalog-train.json

for MODE in nearest meili hmm; do
  route-match replay \
    --graph-manifest "$GRAPH" \
    --catalog artifacts/catalog-train.json \
    --split test --mode "$MODE" \
    --output "artifacts/test-$MODE.jsonl"
done

route-match evaluate \
  --input nearest=artifacts/test-nearest.jsonl \
  --input meili=artifacts/test-meili.jsonl \
  --input hmm=artifacts/test-hmm.jsonl \
  --output artifacts/matcher-comparison.json
```

Для leave-one-trip-out передать один или несколько `--exclude-trip-id`. Исключения и фактически
использованные рейсы записываются в provenance каталога.

## ML и dashboard

```bash
route-match export-spatial-features \
  --data-dir "$DATA_DIR" --split test \
  --catalog artifacts/catalog-train.json \
  --output artifacts/spatial/test.parquet

route-match export-dashboard-network \
  --catalog artifacts/catalog-train.json \
  --schedule "$DATA_DIR/validate/schedule_plan.csv" \
  --output /path/to/dashboard/data/network.json
```

Dashboard export сохраняет старые обязательные поля `stops`, `routes` и `segments`, но добавляет
к сегментам `segment_id`, `route_pattern_id`, source, quality, support и длину. Координаты путей
записываются как `[lat, lon]`.

Для CatBoost-абляции replay-результаты `train.jsonl` и `test.jsonl` кладутся в одну директорию:

```bash
route-match catboost-experiment \
  --feature-cache ../../ml_models/01_catboost_baseline/artifacts/features \
  --spatial-dir artifacts/spatial \
  --output artifacts/catboost-report.json
```

Отчёт сравнивает текущий baseline, признаки положения, признаки времени Wai–Zhou,
категориальные pattern/segment ID и детерминированный route-ETA. Пространственный кандидат
рекомендуется только при улучшении временной проверки более чем на одну секунду.

## Ограничения

- Раздельные модели времени движения и стоянки из Wai–Zhou не строятся: раздача не содержит
  надёжных пар прибытие/отправление и 500 событий на локальную модель.
- `confidence_margin` является разницей HMM log-score, а не вероятностью.
- OSM `edge_id` допустимо сравнивать только внутри одной `graph_version`.
- Без локального Valhalla можно тестировать причинность, контракты и HMM на фикстурах, но нельзя
  получить реальное покрытие маршрутов или итоговую CatBoost-абляцию.
