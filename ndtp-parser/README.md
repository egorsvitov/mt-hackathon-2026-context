# NDTP parser

Парсер принимает бинарные NDTP-пакеты по TCP, проверяет CRC и преобразует
`G6CellNav00` в строку формата `traffic.csv`.

Реализованы восстановление кадров из TCP-потока, разбор NPL/NPH/Nav00,
CRC-16/Modbus, несколько одновременных соединений и повторное подключение.

## Запуск

Из корня репозитория:

```bash
python3 ndtp-parser/main.py
```

Сервер слушает `0.0.0.0:9201`; в `docker compose` он опубликован на порту **19201**.

Эмулятор организаторов проще запустить в той же сети compose (API эмулятора — порт 18080):

```bash
docker load -i <датасет>/ndtp-telemetry-emulator.tar
docker compose --profile emulator up -d
curl -s -X POST http://localhost:18080/api/config \
  -H 'Content-Type: application/json' \
  -d '{
    "targetHost": "ndtp-parser",
    "targetPort": 9201,
    "units": [
      {"unitId": 893159, "intervalMs": 5000, "autoGenerate": true, "cells": []},
      {"unitId": 913870, "intervalMs": 3000, "autoGenerate": true, "cells": []},
      {"unitId": 786201, "intervalMs": 7000, "autoGenerate": true, "cells": []}
    ]
  }'
```

Если эмулятор запущен отдельно (`docker run -p 18080:18080 --add-host=host.docker.internal:host-gateway …`),
в конфиге укажите `"targetHost": "host.docker.internal"` и `"targetPort": 19201`.

Эмулятор шлёт случайные точки с текущей датой. Пока backend воспроизводит исторический день из CSV,
такие отметки отбрасываются как «из будущего» — иначе ТС с теми же `unit_id` (в примере выше это
ТС 122048, 122613, 122658) застыли бы на карте.

## Структура

```text
main.py           точка запуска и отправка телеметрии в backend
backend_client.py HTTP-клиент backend
receiver.py       TCP-сервер и соединения устройств
framing.py        TCP-поток → целые NDTP-кадры
protocol.py       NPL, NPH и G6CellNav00 → Telemetry
crc.py            проверка CRC-16/Modbus
schemas.py        структуры данных
traffic_adapter.py  Telemetry → TrafficRow
unit_mapping.json соответствие unit_id → tr_id
```

`tr_id` не передаётся в NDTP. Он берётся из `unit_mapping.json`. Если устройства
нет в словаре, парсер сообщает об этом и выводит доступные поля телеметрии.

Адрес backend задаётся переменной `BACKEND_URL`. По умолчанию используется
`http://localhost:8000/api/v1/stream/telemetry`.
