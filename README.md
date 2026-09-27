# Предиктор задержек наземного транспорта

Решение команды для хакатона Московского транспорта 2026. Система принимает телеметрию NDTP,
каждую минуту прогнозирует отклонение от расписания на остановке через 10-15 минут, находит
причину и показывает диспетчеру инциденты на карте.

## Запуск

Нужны Docker с Compose v2 и распакованный датасет организаторов.

```bash
cp .env.example .env        # в DATA_DIR укажите путь к датасету, по умолчанию ./dataset
docker compose up --build -d
```

| Сервис | Адрес |
|---|---|
| Дашборд диспетчера | http://localhost:18090 |
| API backend, Swagger | http://localhost:18000/docs |
| Метрики потока и точности (JSON) | http://localhost:18000/api/v1/metrics |
| Приём NDTP (TCP) | localhost:19201 |

ML-сервис доступен только внутри сети Docker. После старта backend воспроизводит день
из `test/traffic.csv` с 08:30, воспроизведением управляет меню «Демо» на дашборде.

При сборке дашборд скачивает подложку карты (около 60 МБ). Если файл уже есть, положите его
в `dashboard/data/basemap/moscow.pmtiles`, тогда скачивания не будет. Если PyPI недоступен,
зеркало передаётся так: `docker compose build --build-arg PIP_INDEX_URL=<url>`.

### Эмулятор организаторов

```bash
docker load -i <датасет>/ndtp-telemetry-emulator.tar
docker compose --profile emulator up -d        # API эмулятора: http://localhost:18080
```

Настройка эмулятора описана в [ndtp-parser/README.md](ndtp-parser/README.md). Эмулятор шлёт
случайные точки с текущей датой, поэтому пока идёт воспроизведение исторического дня, backend
отбрасывает их как точки из будущего (счётчик `rejected_future` в метриках).

### GPU

По умолчанию прогноз делает CatBoost v2. Ансамбль CatBoost и TS2Vec требует NVIDIA GPU
и NVIDIA Container Toolkit:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Подробности в [ml_service/README.md](ml_service/README.md).

## Как устроено

```text
NDTP (TCP) ─► ndtp-parser ─► backend ─► ml-service
CSV replay ─────────────────►   │
                                 └─► REST API ─► дашборд (nginx)
```

| Папка | Что внутри |
|---|---|
| [backend/](backend/README.md) | FastAPI: приём телеметрии, детектор прибытий, признаки, вызов модели, инциденты, меры диспетчера |
| [ml_service/](ml_service/README.md) | сервис инференса, сохранённые веса моделей |
| [ndtp-parser/](ndtp-parser/README.md) | TCP-сервер NDTP: разбор кадров, CRC, отправка в backend |
| [map_matching/](map_matching/README.md) | каталог маршрутов и привязка GPS к маршруту (HMM) |
| [dashboard/](dashboard/README.md) | дашборд: карта MapLibre с офлайн-подложкой OSM, инциденты, аналитика, журнал; [контракт API](dashboard/CONTRACT.md) |
| [tests/reliability/](tests/reliability/README.md) | тесты отказов и нагрузки на поднятом стенде |
| [docs/](docs/) | Sphinx |
| [task/](task/assignment.md), [other_info/](other_info/dataset/README.md) | задание и документы организаторов |

Если ML-сервис недоступен, backend не падает: прогноз равен текущему отклонению
(`status: fallback`), а когда сервис вернётся, backend снова переключится на модель.

Каталога map matching (`map_matching/artifacts/catalog-train.json`) нет в репозитории.
Без него `/api/v1/health/ready` показывает `map_matching_status: disabled`, а дашборд рисует
ТС по сырым координатам GPS. На прогноз это не влияет, у ML свой каталог.

## Надёжность

Замеры `tests/reliability` на чистом клоне:

| Сценарий | Результат |
|---|---|
| холодный старт | все сервисы готовы за 18 с |
| отказ ML-сервиса | через 0,7 с прогнозы без модели, после запуска модель вернулась за 15 с |
| нагрузка | 622 отметки/с (x115 к реальному потоку), 0 ошибок |
| 8 часов данных, 52 ТС | память +7 МБ/мин, утечек нет |
| мусор в API, отметки из будущего, эмулятор поверх воспроизведения | backend работает, ТС не замирают |

Известные ограничения парсера NDTP: пока backend недоступен, отметки теряются (буфера нет);
после кадра с неверной длиной в заголовке теряются и следующие верные кадры.

## Тесты

Юнит-тесты лежат в `backend/tests` и `ml_service/tests` (pytest). Тесты надёжности
запускаются на поднятом стенде и идут около 25 минут:

```bash
python tests/reliability/run_tests.py
```

## Документация кода

```bash
pip install -r backend/requirements.txt -r ndtp-parser/requirements.txt ./map_matching
pip install sphinx myst-parser sphinx_rtd_theme
sphinx-build -b html docs docs/_build/html
```
