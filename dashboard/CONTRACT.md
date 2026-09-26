# Контракт данных дашборда

Документ фиксирует, в каком виде ML-модуль и backend отдают данные дашборду.
Формат взят из плана команды (`planning/plan_gpt6_astra.md`, разделы 6 и 8) и дополнен
полями, без которых дашборду не нарисовать карточку инцидента.

Контракт реализован в backend (`backend/app/api/v1/endpoints/`, схемы — `backend/app/schemas/dashboard.py`,
Swagger — `http://localhost:8000/docs`). Эталон ответов лежит в [`contract/examples/`](contract/examples/): его
генерирует `tools/build_fixtures.py`, отдаёт `tools/mock_backend.py`. Дашборд подключается к любому из них
параметром `index.html?api=<база>`: для backend база `http://host:8000/api/v1`, для mock — `http://host:8765`.

```text
ML-модуль ──(prediction_delay_s, late_probability)──► Backend ──HTTP JSON──► Дашборд
           ▲                                           │
           └──── PredictionRequest (признаки на T) ────┘
```

## Общие правила

* Время — строка ISO 8601 со смещением (`2026-01-06T08:48:00+03:00`) или число epoch-секунд.
  Дашборд принимает оба варианта и показывает время по Москве.
* Задержка — секунды со знаком: `+` опоздание, `−` опережение (как `target_delay_s`).
* Поля с `?` необязательны: если их нет, дашборд посчитает значение сам или скроет элемент.
* CORS: backend отдаёт `Access-Control-Allow-Origin: *`, либо дашборд открывается через
  nginx-прокси (`/api/` → backend, см. `nginx.conf`).

## Как подключается модель

**Офлайн и в режиме REPLAY** (без backend) — через тот же формат, что и сабмит:

1. `tools/build_fixtures.py` пишет `data/points_grid.csv` в формате `validate/points.csv`:
   `sample_id,tr_id,T,target_stop_id,target_time_begin,cur_dev_s`. Точки выпускаются каждую
   минуту для каждого ТС, цель — первая плановая остановка в окне `(T+10 мин, T+15 мин]`.
2. Модель строит признаки по телеметрии с `event_time ≤ T` и возвращает CSV
   `sample_id;prediction[;late_probability]`, как `sample_submission.csv`.
3. `python tools/build_fixtures.py --predictions preds.csv` пересобирает воспроизведение
   на прогнозах модели.

**Онлайн** backend раз в минуту по каждому ТС считает признаки на момент T и вызывает ML-сервис
`POST {ML_SERVICE_URL}` (по умолчанию `http://ml-service:8001/predict`).

Запрос — `MLFeaturesPayload` (`backend/app/schemas/telemetry.py`), все признаки только по данным ≤ T:

| Поле | Смысл |
|---|---|
| `tr_id`, `t_timestamp` | ТС и момент T (epoch) |
| `target_stop_id`, `planned_arrival_time`, `horizon_s` | цель в окне (T+10, T+15] мин, её плановое время, горизонт |
| `current_delay_sec`, `current_delay_known`, `current_delay_age_s`, `delay_trend_15m_s` | текущее отклонение по детектору прибытий backend (онлайн-аналог `cur_dev_s`), известно ли оно, его возраст и рост за 15 мин |
| `current_speed_kmh`, `avg_speed_segment` (5 мин), `speed_15m_kmh`, `speed_norm_kmh` | скорости и норма маршрута |
| `dwell_time_sec`, `gps_age_s` | наблюдаемый простой, возраст GPS |
| `lat`, `lon`, `heading`, `target_lat`, `target_lon`, `distance_to_target_m`, `required_speed_kmh`, `near_stop_m`, `remaining_visits`, `hour` | положение и прогресс до цели |

Ответ: `{"prediction_delay_s": float, "late_probability": float | null, "model_version": str}`.
Прежняя форма `IncidentResponse` (`predicted_delay_sec`, `delay_probability`) тоже принимается.

Если ML недоступен, backend не падает: прогноз = текущее отклонение, `status: "fallback"`,
`model_version: "fallback:persistence"`, в метриках `ml_status: "fallback"`. После ошибки ML не
опрашивается 15 с, чтобы не тормозить поток.

## Эндпоинты backend

Пути ниже — относительно базы API (в backend это `/api/v1`).

| Метод | Путь | Ответ | Частота опроса |
|---|---|---|---|
| GET | `/network` | `Network` — остановки и геометрия маршрутов | один раз при старте |
| GET | `/schedule?tr_id=` | `Schedule` — плановые посещения ТС и известные факты | раз в 30 с для выбранного ТС |
| GET | `/vehicles` | `Vehicle[]` | раз в 2 с |
| GET | `/predictions` | `Prediction[]` — последний прогноз по каждому ТС на линии | раз в 2 с |
| GET | `/incidents` | `Incident[]` — активные и закрытые за последние 15 мин | раз в 2 с |
| GET | `/metrics` | `Metrics` | раз в 2 с |
| GET | `/predictions/verified?limit=80`? | `Verified[]` — прогнозы, сверенные с фактом | раз в 10 с |
| GET | `/config`? | `{thresholds, model}` | один раз |
| GET | `/health/live`, `/health/ready` | статус | для Docker healthcheck |
| POST | `/stream/telemetry` | приём записи телеметрии (`RawNDTPRecord`) от NDTP-парсера | — |
| POST | `/demo/start?t=ЧЧ:ММ&speed=`, `/demo/speed?speed=`, `/demo/stop`, `/demo/link?down=` | управление воспроизведением CSV в backend (дашборд вызывает из шапки) | по действию |

## Объекты

### Network

Сеть разделяет три сущности:

- `Vehicle` — текущее состояние отдельного ТС (`tr_id`);
- `routes[]` — рейс/плановая траектория конкретного ТС, используемая для расписания и участков прогноза;
- `route_patterns[]` — общая направленная линия на карте. Один паттерн может содержать несколько `tr_id`.

Геометрия восстанавливается офлайн из плановых остановок, исторических GPS и
Valhalla/OSM. Dashboard рисует линию один раз на `route_pattern_id`, а её цвет
соответствует максимальному риску среди активных ТС на этом паттерне.

```json
{
  "stops": [{"stop_key": 282, "lat": 55.61, "lon": 37.50, "name": "Профсоюзная ул., д.154"}],
  "route_patterns": [{
    "route_pattern_id": "pattern-a",
    "direction_id": "terminal-a:terminal-b",
    "name": "Профсоюзная ул. → Метро",
    "tr_ids": [132430, 132431],
    "stops": [282, 313],
    "segments": [{"from": 282, "to": 313, "synthetic": false, "path": [[55.61, 37.50], [55.62, 37.51]]}]
  }],
  "routes": [{
    "route_id": "R132430", "tr_id": 132430, "name": "Рейс ТС 132430",
    "speed_norm_kmh": 15.1, "stops": [282, 313], "segments": []
  }]
}
```

`stop_key` — ключ физической остановки. `target_stop_id` — идентификатор
планового посещения (`tt_action_item_id`). Для старых `network.json` без
`route_patterns` backend и браузер строят совместимый паттерн из каждого рейса.
ТС без расписания не получают вымышленную линию: они показываются отдельными
нейтральными точками, если включён слой «ТС без расписания».

### Schedule

```json
{"tr_id": 132430, "visits": [
  {"visit_id": 53698549251, "stop_key": 282, "time_plan": "2026-01-06T02:33:00+03:00", "time_fact": "2026-01-06T02:33:05+03:00"}
]}
```

`time_fact` заполняется только для прибытий, которые уже произошли (детектор прибытий
backend). Будущие факты отдавать нельзя: это утечка.

### Vehicle

| Поле | Тип | Смысл |
|---|---|---|
| `tr_id`, `unit_id` | int | ТС и бортовой терминал |
| `route_id` | str \| null | `null` — ТС без расписания (серые точки на карте) |
| `event_time` | time | время последней валидной GPS-отметки |
| `lat`, `lon`, `speed`, `heading` | number | положение, км/ч, курс в градусах |
| `data_age_s`? | number | сейчас − `event_time`; если нет, считает дашборд |
| `status`? | `live` \| `stale` \| `offline` | ≤ 60 с / ≤ 300 с / больше |
| `source` | `ndtp` \| `replay` | откуда пришла отметка |
| `route_pattern_id`?, `position_quality`?, `off_route`? | spatial | состояние HMM; при недоступном matcher поля пусты |

### Prediction

Поля плана (`sample_id … data_age_s`) плюс поля для карточки:

| Поле | Тип | Смысл |
|---|---|---|
| `sample_id` | str | `"{tr_id}_{T}"`, как в `points.csv` |
| `tr_id`, `route_id` | | |
| `as_of` | time | момент прогноза `T`; признаки только по данным ≤ `T` |
| `generated_at` | time | когда прогноз реально посчитан (для замера latency) |
| `target_stop_id`, `target_stop_name` | | цель: первая плановая остановка в `(T+10, T+15]` мин |
| `target_time_begin` | time | плановое прибытие на цель |
| `horizon_s` | int | `target_time_begin − as_of`, всегда 600…900 |
| `prediction_delay_s` | number | **прогноз задержки, с** |
| `predicted_arrival`? | time | `target_time_begin + prediction_delay_s` |
| `late_probability`? | 0…1 | P(задержка > 120 с) |
| `cur_dev_s` | number \| null | текущее отклонение: в REPLAY — подсказка организаторов, в backend — по детектору прибытий |
| `cur_dev_source`? | `detector` \| `none` | откуда `cur_dev_s` в backend |
| `severity`? | `ok` \| `warning` \| `critical` \| `early` | если нет, дашборд считает по порогам |
| `status` | `model` \| `fallback` \| `stale` | `fallback` — ML недоступен, прогноз = `cur_dev_s` (карточка: «без ML»); `stale` — поток оборван, показано последнее состояние (карточка: «данные устарели») |
| `model_version` | str | |
| `data_age_s` | number | возраст последней GPS-отметки на момент `T` |
| `segment` | object | `{from_stop_id, from_stop_name, to_stop_id, to_stop_name}` — участок от последней пройденной остановки до цели |
| `reason`? | object \| null | `{code, title, detail}` — гипотеза о причине |
| `evidence`? | array | `[{code, label, value, norm, unit, flag}]` — признаки на `T`; `flag: true` — на нём основана гипотеза |
| `recommendation`? | str \| null | что сделать диспетчеру |

Пороги уровня риска (`/config`, по умолчанию): `critical ≥ +120 с` (как `late` у
организаторов), `warning ≥ +60 с`, `early ≤ −60 с`, остальное `ok`.

### Incident

Инцидент — эпизод риска по одному ТС, а не отдельная карточка на каждый прогноз.

* **Открытие:** прогноз `critical` открывает инцидент сразу, `warning` и `early` — только
  со второго прогноза подряд (антидребезг).
* **Закрытие:** после трёх прогнозов подряд без риска.
* `incident_id` не меняется, пока эпизод длится; поля текущего прогноза обновляются.

| Поле | Смысл |
|---|---|
| `incident_id`, `tr_id`, `route_id` | |
| `kind` | `late` \| `early` |
| `status` | `active` \| `resolved` |
| `first_detected_at`, `updated_at`, `closed_at` | время обнаружения, обновления, закрытия |
| `severity`, `peak_severity` | текущий и максимальный уровень за эпизод |
| `target_*`, `horizon_s`, `prediction_delay_s`, `predicted_arrival`, `late_probability`, `segment` | из текущего прогноза |
| `suspected_reason`, `evidence`, `recommendation` | из последнего прогноза с риском |
| `prediction_status`? | `status` текущего прогноза, чтобы пометить карточку «данные устарели» |
| `alert_prediction_delay_s`, `alert_target_stop_name`? | прогноз, по которому шла тревога |
| `outcome_delay_s`? | фактическая задержка на этой цели, когда она наступила (проверка прогноза) |

### Metrics

| Поле | Смысл |
|---|---|
| `now` | часы backend (в режиме воспроизведения — виртуальные) |
| `mode`, `source` | `live`/`replay`, `ndtp`/`replay` |
| `ingest_status` | `ok` \| `degraded` \| `down` — есть ли поток NDTP; при `down` дашборд показывает баннер деградации |
| `last_packet_at`, `packets_per_min`, `queue_lag_s`, `reconnects` | состояние потока |
| `inference_latency_ms_p50`, `inference_latency_ms_p95` | задержка ML |
| `vehicles_live` | ТС со свежими данными |
| `mae_live_s`, `mae_baseline_live_s`, `n_verified` | MAE прогнозов, у которых уже наступил факт; базовый — «прогноз = `cur_dev_s`» |
| `model_version`, `ml_status` | версия модели; `ok` или `fallback` (ML недоступен) |
| `arrival_detector_mae_s`? | ошибка детектора прибытий против факта (replay) |
| `replay_speed`? | скорость часов воспроизведения в backend; дашборд по ней экстраполирует время между опросами |
| `offline_eval`? | офлайн-оценка модели `{mae_model, mae_cur_dev, n, on}` |

## Правила причин и рекомендаций

`backend/app/services/incident_rules.py` — единый источник: им пользуются backend и генератор
REPLAY (через прокладку `dashboard/tools/incident_rules.py`). Модуль не зависит от pandas:

* `severity_of(prediction_s)` — уровень риска;
* `diagnose(features, prediction_s, severity)` — код причины и текст с цифрами;
* `evidence(features, code)` — список доказательств;
* `REASONS`, `EVIDENCE_SPEC`, `THRESHOLDS` — справочники.

| Код | Когда | Рекомендация |
|---|---|---|
| `NO_GPS` | GPS-отметке больше 3 мин | проверить связь с терминалом |
| `LONG_STOP` | стоит ≥ 150 с дальше 150 м от остановки | связаться с водителем |
| `LONG_DWELL` | стоит ≥ 120 с у остановки | уточнить причину стоянки |
| `SLOW` | скорость за 5 мин < 60% нормы маршрута | проверить обстановку, светофорный приоритет |
| `GROWING` | отклонение выросло ≥ 60 с за 15 мин | предупредить водителя, выровнять интервал |
| `CARRYOVER` | уже опаздывает ≥ 120 с | сократить стоянку на конечной |
| `TIGHT` | нужная до цели скорость > 130% нормы | учесть при корректировке расписания |
| `EARLY` | прогноз ≤ −60 с | выдерживать время на остановках |
| `MODEL` | ничего из перечисленного | наблюдать |

Причины — это гипотезы по наблюдаемым признакам. Данных о дверях, пробках и ДТП в
раздаче нет, поэтому утверждать их нельзя.
