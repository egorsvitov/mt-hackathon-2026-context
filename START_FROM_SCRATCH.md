# Запуск main с нуля в отдельной папке

## Что требуется

Git, Docker Engine с Compose v2 и доступ к образам/пакетам. Для обычного запуска GPU и установленный на ноутбуке Python не нужны. Веса CatBoost и ML-каталог уже находятся в Git; экспериментальные папки и окружения обучения не нужны.

Датасет и большая подложка карты — внешние данные, не входящие в Git. Использование их существующих локальных копий не нарушает чистоту сборки кода.

## 1. Клонирование и данные

```bash
cd ~/projects/hackathons/mt-2026/test
git clone --branch main --single-branch git@github.com:egorsvitov/mt-hackathon-2026-context.git mt-hackathon-2026
cd mt-hackathon-2026
```

Если папка уже существует, не клонируйте поверх неё. Для SSH нужен доступ к GitHub; альтернативный URL: `https://github.com/egorsvitov/mt-hackathon-2026-context.git`.

В корне клона создайте `.env` (не коммитится):

```dotenv
DATA_DIR=/home/pavel/projects/hackathons/mt-2026/real/dataset
DASHBOARD_PORT=38090
BACKEND_PORT=38000
NDTP_PORT=39201
REPLAY_AUTOSTART=true
REPLAY_SPEED=1
REPLAY_START=08:30
```

Замените `DATA_DIR` на свой абсолютный путь. Для replay требуются `test/traffic.csv` и `test/schedule.csv` либо `test/schedule_plan.csv`. Данные монтируются read-only. Уникальные порты и имя проекта ниже позволяют не затрагивать уже работающий стенд.

## 2. Подложка без повторного скачивания

До сборки скопируйте существующую карту:

```bash
mkdir -p dashboard/data/basemap
cp /home/pavel/projects/hackathons/mt-2026/real/mt-hackathon-2026-context/dashboard/data/basemap/moscow.pmtiles dashboard/data/basemap/moscow.pmtiles
```

Исходная копия остаётся на месте. Docker использует файл из build context и пропускает загрузку. Если локальной карты нет, сборка попробует скачать её. Для проверенной копии размер — 60 362 205 байт, SHA256 — `3207f874e257a3bf258fcf1b5ba84c36df0221e95c48988e4d7bf6fbe0d43cde`.

## 3. Сборка и запуск

```bash
docker compose -p mt-clean-main build ml-service backend dashboard ndtp-parser
docker compose -p mt-clean-main up -d --no-build ml-service backend dashboard ndtp-parser
docker compose -p mt-clean-main ps
```

Если PyPI недоступен, используйте зеркало (передаётся всем Python-образам):

```bash
docker compose -p mt-clean-main build --no-cache --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple ml-service backend dashboard ndtp-parser
```

`--no-cache` проверяет сборку без готовых слоёв зависимостей, но не очищает чужие образы и контейнеры. Загрузка зависимостей может занять несколько минут. Если нет доступа к Docker socket, проверьте членство в группе `docker`; после добавления нужен новый сеанс или `newgrp docker`.

Откройте дашборд: <http://localhost:38090>; Swagger: <http://localhost:38000/docs>. NDTP TCP слушает порт 39201. ML доступен только внутри Docker-сети.

## 4. Проверки

```bash
curl -fsS http://localhost:38000/api/v1/health/ready
curl -fsS http://localhost:38000/api/v1/predictions
curl -fsS http://localhost:38090/api/v1/metrics
docker compose -p mt-clean-main exec -T ml-service python -c 'import urllib.request; print(urllib.request.urlopen("http://localhost:8001/health").read().decode())'
curl -sS -D - -H 'Range: bytes=0-7' http://localhost:38090/data/basemap/moscow.pmtiles
```

Ожидаются readiness `ready`, `ml_status: ok`, непустые прогнозы с `status: model` и `model_version: catboost:v2-seed42`. Для карты ожидаются HTTP 206, `Content-Range` и заголовок `PMTiles`. В браузере проверьте карту, маршруты, карточки транспорта и обновление времени replay. Сразу после старта история и прогнозы могут ещё прогреваться.

Диагностика:

```bash
docker compose -p mt-clean-main logs --tail=100 backend ml-service ndtp-parser dashboard
```

В Git нет `/route-data/catalog-train.json` для отдельного matcher дашборда. Поэтому `map_matching_status: disabled` с `catalog_not_found` возможен и не означает отказ ML: ML использует собственный сохранённый каталог. Дашборд в таком случае отображает сырой GPS. Подготовка необязательного каталога описана в [map_matching/README.md](map_matching/README.md); не подменяйте им каталог обучения ML.

Метрики replay относятся к онлайн-детектору задержки. Они не подтверждают скор submission и не являются независимой оценкой качества: исходные train/test пересекаются. Подробности проверки сохранённых моделей: [ml_service/VERIFICATION.md](ml_service/VERIFICATION.md).

## 5. GPU — отдельный режим

Стандартный запуск выше использует CatBoost без PyTorch/CUDA. Для ансамбля нужны NVIDIA Container Toolkit и драйвер, совместимый с CUDA 13:

```bash
docker compose -p mt-clean-main -f docker-compose.yml -f docker-compose.gpu.yml up -d --build ml-service backend dashboard ndtp-parser
```

Это заменяет ML-контейнер именно проекта `mt-clean-main`. Не выполняйте команду без нужного GPU runtime: обычного наличия видеокарты недостаточно. На данном ноутбуке GPU Docker-запуск пока не подтверждён. При ошибке encoder сервис может перейти на CatBoost; проверяйте активный режим и причину в `/health`, а не только факт успешного HTTP-ответа. Ограничения точного воспроизведения GPU изложены в `VERIFICATION.md`.

## 6. Остановка только этого стенда

```bash
docker compose -p mt-clean-main down
```

Не используйте глобальную очистку Docker. Исходный датасет и локальная карта сохраняются.

## Найденные расхождения чистого запуска

Проверка начата с GitHub main `1a97598`, исправления находятся в локальной ветке `fix/clean-main-build`:

- NDTP Dockerfile игнорировал `PIP_INDEX_URL`; сборка без кэша через зеркало падала. Добавлена поддержка аргумента, аналогично backend и ML.
- В README оставалось неверное утверждение о необходимости соседнего репозитория и неверный порт дашборда. Исправлены.

Проверка не переносит исследовательские окружения и не запускает переобучение. Исправления перенесены поверх обновлённого main `a2032b7`; новые изменения команды сохранены.

### Результаты проверки на ноутбуке

- Все четыре образа успешно собраны с `--no-cache` через указанное зеркало; после сборки контейнеры пересозданы.
- Backend, ML и dashboard healthy; NDTP-парсер запущен и принимает TCP-кадры.
- Replay загрузил 13 маршрутов и 13 планов транспорта; прогнозы приходят от `catboost:v2-seed42`. В ML-образе отсутствует модуль `torch`.
- В headless Chrome приложение готово, `basemapStatus=ok`, стиль и все тайлы загружены. Проверка только HTTP 206 сама по себе ещё не доказывает отрисовку.
- В одноразовых контейнерах на собранных образах: ML — 9 passed / 1 skipped (GPU); backend — 3 passed. Pytest устанавливался только в тестовые контейнеры, не в production-образы.
- При остановке только тестового ML-сервиса backend продолжил отвечать с `fallback:persistence`; после включения ML вернулся на CatBoost.
- NDTP handshake и навигационный кадр разобраны парсером. Кадр с текущей датой ноутбука отвергнут backend как будущий относительно исторического replay — ожидаемая защита.
- В обычном replay после старта измеренный `inference_latency_ms_p95` около 27 мс. Это метрика backend, не нагрузочный benchmark всего HTTP-запроса и не оценка submission.

Ограничения: необязательный dashboard matcher не подготовлен; GPU Docker-режим не проверен; независимое качество моделей и полный нагрузочный прогон этой проверкой не подтверждаются. Тестовый проект оставлен работающим на порту 38090, исходные сервисы команды не перезапускались.
