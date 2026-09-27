# Route-aware run/dwell model v2

Вторая offline-версия предиктора задержки. Статическая маршрутная геометрия используется
как заранее известный справочник, а все telemetry snapshots и исторические component events
строятся причинно. Победившая конфигурация объединяет глобальные CatBoost-модели времени
движения/стоянки с map-aware residual CatBoost.

Подробности эксперимента и метрики находятся в [MODEL.md](MODEL.md).

## Запуск

Требуются Python 3.12, `uv`, локальный датасет и заранее построенный route catalog:

```bash
cd ml_models/02_route_components
uv sync
export DATA_DIR="$(cd ../../../data/dataset && pwd)"
export ROUTE_CATALOG_PATH="$(cd ../../map_matching/artifacts && pwd)/catalog-train.json"

uv run delay-v2 prepare
uv run delay-v2 train
uv run delay-v2 evaluate
uv run delay-v2 predict --output artifacts/prediction.csv
```
Чтобы исключить пересечение `test` и `validate`, финальную модель и component models можно
обучить только на `train`. `test` при этом используется лишь для отчётной оценки, но не входит
в fit финального bundle и не поставляет run/dwell events:

```bash
uv run delay-v2 \
  --artifacts-dir artifacts/modeling_train_only \
  train --final-train-only
```


Можно передать пути явно через `--data-dir` и `--catalog`. `prepare` занимает несколько минут:
HMM причинно проигрывает телеметрию до каждой прогнозной точки.

## Артефакты

```text
artifacts/
├── features/                  # point features и prepare report
├── events/                    # run/dwell events
└── modeling/
    ├── evaluation_model/      # train → test bundle
    ├── evaluation_components/
    ├── model/                 # train+test → validate bundle
    ├── component_models/
    ├── experiment_report.json
    ├── test_predictions.csv
    └── submission.csv
```

Крупные кэши и bundles локальны и исключены из Git; отчёт эксперимента хранится в репозитории.

## Causal policy

Разрешены route pattern, direction, segment IDs, полилинии, длины и порядок остановок.
Из полного каталога намеренно не передаются `historical_segment_time_s`,
`estimated_remaining_time_s` и `segment_support`. Во временном fold component event разрешён
только после `available_at < cutoff`; point features используют telemetry с `event_time <= T`.

Проверки:

```bash
uv run pytest -q
uv run ruff check .
```

