# CatBoost baseline v1

Первый воспроизводимый ML-эксперимент для прогноза задержки на целевой остановке через
10–15 минут. В этой папке находятся код, тесты, зафиксированные зависимости, обученная модель,
полный отчёт эксперимента и полученный submission. Описание постановки и результатов — в
[MODEL.md](MODEL.md).

## Запуск

Требуются Python 3.12 и [uv](https://docs.astral.sh/uv/). Из этой папки:

```bash
uv sync
export DATA_DIR="$(cd ../../../data/dataset && pwd)"
uv run delay prepare
uv run delay train
```

`prepare` строит leakage-safe признаки для train/test/validate и сохраняет локальный кэш в
`artifacts/features/`. Этот кэш игнорируется Git. `train` повторяет подбор конфигурации,
переобучает победителя, оценивает его на test и обновляет артефакты в `artifacts/modeling/`.

Сохранённый результат уже находится здесь:

```text
artifacts/modeling/
├── experiment_report.json
├── model/
│   ├── manifest.json
│   └── model_0.cbm
├── submission.csv
└── submission_model_candidate.csv
```

Повторная оценка и инференс:

```bash
uv run delay evaluate --split test
uv run delay predict --split validate --output artifacts/prediction.csv
```

Проверки:

```bash
uv run pytest -q
uv run ruff check .
```

Код feature engine не загружает `time_fact_begin`; для точки T используются только события с
`event_time <= T`. Если кандидат не превосходит `cur_dev_s` и на временной проверке, и на test,
workflow автоматически формирует рекомендуемый submission из persistence baseline.
