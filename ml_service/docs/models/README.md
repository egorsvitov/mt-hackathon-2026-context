# Модели и качество

[К навигации ML](../../README.md)

CPU: CatBoost v2 seed42. GPU: сохранённый ансамбль №40 — 66% среднего трёх CatBoost v2 + 34% среднего трёх гибридов v2+TS2Vec, seeds 42/17/73. Модели residual: runtime добавляет текущую задержку `cur_dev_s`.

## Bundle

[model/](../../model/) содержит шесть residual CatBoost, три encoder, два run/dwell CatBoost и каталог. [Manifest](../../model/manifest.json) фиксирует порядок признаков, SHA256, нормализацию, seeds и веса ансамбля. Переобучения при запуске нет.

[Encoder](../../runtime/encoder.py) — минимальная inference-адаптация TS2Vec с [MIT-лицензией авторов](../../runtime/TS2VEC_LICENSE). Обучения SSL и loss functions здесь нет. Одиночный запрос дополняется до batch 128 для сохранения CUDA-геометрии; удаление этого блока может изменить прогноз. Для исходного хвоста batch 23 зафиксированы расхождения в [отчёте](../../VERIFICATION.md).

## Тесты

Из корня репозитория в отдельном окружении:

```sh
python -m venv .venv
.venv/bin/pip install -r ml_service/requirements.txt pytest httpx
.venv/bin/python -m pytest ml_service/tests -q
.venv/bin/pip install -r backend/requirements.txt
.venv/bin/python -m pytest backend/tests -q
```

ML и backend тестируются отдельными процессами: оба имеют пакет `app`. GPU fixture требует совместимого PyTorch и доступной CUDA; в CPU-окружении он пропускается. Один fixture не заменяет сравнение всего контрольного набора.

## Ограничения оценок

Зафиксированные MAE: seed42 — 62.965 с, ансамбль №40 — 63.228 с. По этой метрике ансамбль не лучше самостоятельного seed42.

Веса подбирались на официальном test, пересекающемся с train по траекториям. Это не независимая проверка обобщения. Датасетный `cur_dev_s` проверяет реализацию, а не качество онлайн-детектора. Replay и latency не являются submission score.

При сокращении runtime нужно сравнивать признаки, категории, missing-маски, окна и прогнозы обоих режимов до HTTP-округления. [VERIFICATION.md](../../VERIFICATION.md) — исторический отчёт, а не автоматически обновляемый статус main.
