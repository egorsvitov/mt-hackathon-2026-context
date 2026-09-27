# Backend · FastAPI

Сервис принимает телеметрию, хранит состояние ТС, оценивает текущее отклонение, считает
признаки на момент T и вызывает ML-сервис. Результат — прогнозы, инциденты и метрики через
API для дашборда. Swagger: `http://localhost:8000/docs`.

```text
телеметрия ──► pipeline.ingest ──► состояние ТС + детектор прибытий
(NDTP / CSV replay)              ├──► HMM map matching ──► matched-положение dashboard
                                 │ раз в минуту по времени событий, для каждого ТС
                                 ▼
                        признаки на T (только данные ≤ T) ──► CatBoost /predict
                                        │                      └─ недоступен → fallback
                                        ▼
                    риск, причина, рекомендация (incident_rules) ──► инциденты ──► API /api/v1/*
```

## Запуск

```bash
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements.txt -e ../map_matching
REPLAY_SPEED=10 .venv/bin/python -m uvicorn app.main:app --port 8000
```

Все сервисы вместе — `docker compose up --build` из корня репозитория (см. `docker-compose.yml`).

Перед запуском нужны `dashboard/data/network.json` и, для HMM, локальный
`map_matching/artifacts/catalog-train.json`. Команды построения описаны в
[map_matching/README.md](../map_matching/README.md). Без каталога backend продолжит
работу с raw GPS; статус виден в `/health/ready`.

## Настройки (переменные окружения / `.env`)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `DATA_DIR` | `../../dataset` | датасет (нужны `<split>/schedule.csv` и `traffic.csv`) |
| `SCHEDULE_SPLIT` | `test` | чей план загружать; `validate` → `schedule_plan.csv` |
| `NETWORK_PATH` | `../dashboard/data/network.json` | остановки и геометрия маршрутов |
| `ROUTE_CATALOG_PATH` | `../map_matching/artifacts/catalog-train.json` | каталог HMM; отсутствие не блокирует backend |
| `ML_SERVICE_URL` | `http://localhost:8001/predict` | ML-сервис |
| `REPLAY_AUTOSTART`, `REPLAY_SPEED`, `REPLAY_START` | `true`, `1`, `08:30` | воспроизведение дня из CSV, пока нет живого NDTP |
| `ARRIVAL_RADIUS_M`, `ARRIVAL_EARLY_SEC`, `ARRIVAL_LATE_SEC` | `40`, `420`, `780` | детектор прибытий |

## Эндпоинты (`/api/v1`)

| Путь | Что отдаёт |
|---|---|
| `GET /network`, `/schedule?tr_id=` | сеть маршрутов; плановые посещения и прибытия по детектору |
| `GET /vehicles`, `/predictions`, `/incidents` | положение ТС, прогнозы в окне T+10…15 мин, инциденты |
| `GET /metrics`, `/config`, `/predictions/verified` | поток, задержка обработки, точность по факту, пороги |
| `POST /stream/telemetry` | приём записи от NDTP-парсера (`RawNDTPRecord`) |
| `POST /demo/start`, `/demo/speed`, `/demo/stop`, `/demo/link` | управление воспроизведением и имитация обрыва потока |
| `GET /health`, `/health/live`, `/health/ready` | для Docker healthcheck |

Формат ответов и запрос к ML-сервису описаны в [dashboard/CONTRACT.md](../dashboard/CONTRACT.md).

## Честность данных

* **План расписания** грузится whitelist-колонками, `time_fact_begin` в онлайн-контур не попадает.
* **Текущее отклонение** оценивает детектор прибытий (`services/feature_extractor.py`): вход ТС в геозону 40 м ближайшей подходящей по времени плановой остановки. На test против факта медиана ошибки 6 с, MAE 22 с. В NDTP `cur_dev_s` не приходит, поэтому этот детектор и есть его онлайн-аналог.
* **Факт прибытия** в режиме replay используется только для сверки прогнозов (`Evaluator`) и раскрывается, когда наступил.
* **Без ML-сервиса** прогноз равен текущему отклонению (`status: fallback`), и это видно в API и на дашборде.

## Интеграция

* Backend отправляет версионированный контекст, 62 маршрутных признака и сырое
  окно телеметрии 45×7 в ML-сервис и публикует его прогнозы в
  `/predictions`, `/incidents` и `/metrics`.
* HMM обрабатывает тот же причинный поток телеметрии. `/vehicles` отдаёт matched
  координаты, активный `route_pattern_id` и качество позиции.
* При недоступном CatBoost используется persistence; при недоступном каталоге HMM
  остаются исходные GPS-координаты.
