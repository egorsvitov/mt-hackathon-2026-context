# Route-aware run/dwell CatBoost v2

Дата прогона: 27 сентября 2026.

## Результат

| Проверка | v2 MAE, с | v1 MAE, с | Persistence MAE, с |
|---|---:|---:|---:|
| Temporal CV, real-only training | 78,15 | 80,07 | 94,79 |
| Temporal CV, synthetic ablation | **74,08** | 80,07 | 94,79 |
| Предоставленный test | **64,21** | 72,49 | 93,36 |

Победитель — глобальные run/dwell модели плюс map-aware residual CatBoost. Отдельные
route-specific correctors не приняты: их CV MAE 78,53 с против 78,15 с у глобальных
компонентов. Segment peer context также не прошёл gate (78,39 с).

Финальная модель обучена на 4 434 строках train, включая синтетику, и 353 размеченных test
строках. Submission содержит 151 validate prediction.

## Данные и coverage

- Map matching: 1 124/4 434 train, 346/353 test и 149/151 validate точек. Почти все
  unmatched train-строки относятся к синтетическим ТС.
- Извлечено 3 741 run и 4 083 dwell события на 32 route patterns и 158 trip occurrences.
- 21,8% stop events классифицированы как pass-through с нулевым dwell.
- Идентичные train/test component events дедуплицируются по trip/segment-or-visit/time.

## Архитектура

Run и dwell обучаются отдельными глобальными CatBoost MAE-моделями. Маршрутный residual
expert создаётся только при 100 событиях и трёх рейсах, затем shrinkage-вес равен
`n/(n+100)`. В выбранном варианте experts отключены по результату temporal CV.

Component aggregator суммирует непройденную долю текущего run, будущие run и промежуточные
dwell до target. Полученные ETA/delay/run/dwell передаются в финальный residual CatBoost:

```text
prediction = cur_dev_s + CatBoost(target_delay_s - cur_dev_s)
```

Финальный bundle содержит 68 признаков, включая шесть категориальных map matching полей.

## Валидация и ограничения

Использованы folds 10:00, 14:00 и 18:00. Label допускается в train только после фактического
целевого события; один trip occurrence не делится между train и validation. Component event
попадает в обучение только после `available_at`.

Статическая геометрия объявлена заранее известной. Это осознанное допущение эксперимента.
Динамические duration/support из full-day catalog запрещены и проверяются schema assertion.

Test и train telemetry в раздаче существенно пересекаются. Поэтому 64,21 с — корректная
официальная train→test проверка, но не независимый будущий день; temporal CV остаётся основной
проверкой переносимости.

