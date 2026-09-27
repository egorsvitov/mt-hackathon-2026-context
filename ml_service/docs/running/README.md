# Запуск и диагностика

[К навигации ML](../../README.md)

Команды выполняются из корня репозитория решения с `docker-compose.yml`. Самой модели train/test CSV не нужны, но backend требует внешний датасет через `DATA_DIR`. Для проверки задайте отдельный Compose project и свободные порты, чтобы не перезапустить рабочий стенд команды.

## CPU

```sh
docker compose up --build
```

По умолчанию `ML_MODE=catboost`: сохранённый CatBoost v2 seed42, без PyTorch/CUDA. Зависимости описаны в [requirements.txt](../../requirements.txt), сборка — в [Dockerfile](../../Dockerfile).

## GPU

```sh
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build
```

Override задаёт `ML_MODE=ensemble`. Нужны NVIDIA Container Toolkit и драйвер с поддержкой CUDA 13; образ использует torch 2.14.0/cu130. CPU-ансамбль не поддерживается. Наличие видеокарты не означает, что Docker имеет доступ к ней.

## Диагностика

ML работает внутри Docker-сети на порту 8001:

```sh
docker compose exec -T ml-service python -c 'import urllib.request; print(urllib.request.urlopen("http://localhost:8001/health").read().decode())'
docker compose logs --tail=100 ml-service backend
```

`/health` показывает запрошенный и активный режим, версию и причину деградации. Ошибка загрузки/inference encoder переключает сервис на seed42. Невозможность запуска GPU-контейнера из-за Docker runtime этим механизмом не обрабатывается. При отказе всего ML backend использует `fallback:persistence` — текущую задержку.

Для ML-сборки поддерживается аргумент `PIP_INDEX_URL`. Поддержку аргумента другими сервисами нужно проверять в их Dockerfile.

Успешный HTTP-ответ может оказаться fallback, поэтому проверяйте активный режим. Исторические ограничения GPU: [VERIFICATION.md](../../VERIFICATION.md).
