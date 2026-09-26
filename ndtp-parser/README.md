# NDTP parser

Парсер принимает бинарные NDTP-пакеты по TCP, проверяет CRC и преобразует
`G6CellNav00` в строку формата `traffic.csv`.

Реализованы восстановление кадров из TCP-потока, разбор NPL/NPH/Nav00,
CRC-16/Modbus, несколько одновременных соединений и повторное подключение.

## Запуск

Из корня репозитория:

```bash
python3 ndtp-parser/fullReceiver.py
```

Сервер слушает `0.0.0.0:9201`. Эмулятор можно настроить так:

```bash
curl -s -X POST http://localhost:18080/api/config \
  -H 'Content-Type: application/json' \
  -d '{
    "targetHost": "host.docker.internal",
    "targetPort": 9201,
    "units": [
      {"unitId": 893159, "intervalMs": 5000, "autoGenerate": true, "cells": []},
      {"unitId": 913870, "intervalMs": 3000, "autoGenerate": true, "cells": []},
      {"unitId": 786201, "intervalMs": 7000, "autoGenerate": true, "cells": []}
    ]
  }'
```

## Структура

```text
fullReceiver.py   точка запуска и вывод полной строки traffic.csv
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

Для интеграции с backend следует передать `TrafficRow` через callback или
потокобезопасную очередь вместо печати JSON в `handle_frame()`.
