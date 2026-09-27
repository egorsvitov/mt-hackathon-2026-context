# Рабочий контекст команды

Код решения находится в `../mt-hackathon-2026/`. Этот репозиторий содержит задание, справочные материалы и внутренние заметки; для запуска и разработки решения он не требуется.

| Путь | Содержимое |
|---|---|
| [task/assignment.md](task/assignment.md) | Полный текст задания в Markdown |
| [task/assignment.pdf](task/assignment.pdf) | Исходный PDF |
| [other_info/dataset/README.md](other_info/dataset/README.md) | Исходное описание датасета, метрики и submission |
| [other_info/dataset/docs/Emulator-and-Telematic-Packets-Specification.md](other_info/dataset/docs/Emulator-and-Telematic-Packets-Specification.md) | Спецификация NDTP и эмулятора |
| [planning/plan_gpt6_astra.md](planning/plan_gpt6_astra.md) | Ранее подготовленный анализ и план; выводы требуют проверки |
| [backend/](backend/) | Backend на FastAPI: приём телеметрии, детектор прибытий, признаки, прогноз через ML-сервис, API дашборда ([README](backend/README.md)) |
| [dashboard/](dashboard/) | Диспетчерский дашборд: карта MapLibre с офлайн-подложкой OSM, инциденты, проверка прогнозов ([README](dashboard/README.md), [контракт данных](dashboard/CONTRACT.md)) |
| [map_matching/](map_matching/) | Офлайн-каталог маршрутов, причинный HMM и экспорт matched-геометрии для backend и dashboard |
| [docker-compose.yml](docker-compose.yml) | NDTP-парсер + backend + CatBoost ML-сервис + дашборд одной командой: `DATA_DIR=<датасет> docker compose up --build` |
| [research/deep-research-report.md](research/deep-research-report.md) | Обзор литературы и рекомендации по ML-архитектуре |
| [planning/architecture_after_research.md](planning/architecture_after_research.md) | Разбор отчёта, повторная проверка данных и предложение архитектуры |
| [ml_models/01_catboost_baseline/](ml_models/01_catboost_baseline/) | Первый CatBoost baseline: код, обученная модель, submission, тесты и описание эксперимента |
| [ndtp-parser/](ndtp-parser/) | TCP-приёмник и парсер телеметрии NDTP в формат `traffic.csv` |
| [research/papers/Wai_Zhou_2020_Real_Time_Bus_Time_Predictions.pdf](research/papers/Wai_Zhou_2020_Real_Time_Bus_Time_Predictions.pdf) | Wai & Zhou (2020): production-архитектура XGBoost-прогнозов времени движения и стоянки |

## Быстрый старт (для жюри)

Для проверки работоспособности проекта выполните несколько простых шагов:

1. **Подготовка конфигурации**: Скопируйте файл конфигурации среды:
   ```bash
   cp .env.example .env
   ```
   *По умолчанию проект ожидает распакованный датасет в папке `./dataset` в корне проекта (либо укажите абсолютный путь в `.env` в переменной `DATA_DIR`).*

2. **Запуск проекта через Docker**: Запустите все сервисы одной командой:
   ```bash
   docker compose up --build -d
   ```
   **Для чего нужен Docker и эта команда?**
   Docker позволяет развернуть весь наш сложный стек (Backend, ML-сервис, NDTP-парсер, Frontend-дашборд) изолированно и надежно. Вам не нужно устанавливать Python, настраивать базы данных или зависимости — Docker сделает всё сам. 
   - Флаг `--build` гарантирует сборку актуальных образов из нашего исходного кода (включая скачивание offline-карты).
   - Флаг `-d` запускает контейнеры в фоновом режиме.

После успешного запуска сервисы будут доступны по следующим адресам:
- **Диспетчерский дашборд**: [http://localhost:18080](http://localhost:18080) (Карта, инциденты, визуализация)
- **Документация API (Backend, Swagger)**: [http://localhost:18000/docs](http://localhost:18000/docs)
- **NDTP-парсер (TCP)**: `localhost:19201`

*(Сервис машинного обучения ml-service работает исключительно во внутренней сети Docker).*

### Как использовать Swagger (API Документация)
Swagger UI ([http://localhost:18000/docs](http://localhost:18000/docs)) предоставляет интерактивный веб-интерфейс для нашего API бэкенда.
**Зачем он нужен?**
С помощью Swagger вы можете:
- Изучить все доступные REST API эндпоинты проекта.
- Посмотреть схемы данных (какие параметры принимает API и какие ответы возвращает).
- **Протестировать API прямо в браузере**: раскройте любой эндпоинт, нажмите кнопку `Try it out`, введите тестовые данные и нажмите `Execute`.

## Надежность и производительность (P0-3)

В рамках хакатона проведены замеры производительности и тестирование сценариев деградации:

| Метрика / Сценарий | Значение / Результат |
| :--- | :--- |
| **p50 Latency (инференс)** | 6.63 ms |
| **p95 Latency (инференс)** | 11.27 ms |
| **Throughput (нормальный режим)** | 1.15 сообщений/сек (69 пакетов/мин в режиме replay) |
| **Throughput (экстремальная нагрузка)** | ~1900 сообщений/сек (замер на 10 000 пакетах, конкурентность 200) |
| **Cold start (сборка + запуск)** | ~23.5 сек |
| **Работа fallback (отключение ML)** | Успешно (backend не падает, отдает `status=fallback` и `fallback:persistence`) |
| **Автовосстановление ML** | Успешно (при рестарте ml-service backend автоматически переключается на `status=model`) |

## Документация разработчика (Sphinx)

Проект содержит автоматически генерируемую документацию для исходного кода (Backend, ML-сервис, Парсер, алгоритмы Map Matching). 
**Зачем нужен Sphinx?** 
Sphinx собирает все наши docstrings (комментарии в коде) и превращает их в удобный HTML-сайт. Это позволяет жюри и новым разработчикам легко понять архитектуру, внутреннее устройство классов, функций и модулей без необходимости читать сырой код.

Чтобы собрать и посмотреть актуальную HTML-документацию, выполните следующие шаги:

1. Откройте терминал в папке `docs`:
   ```bash
   cd docs
   ```
2. Установите зависимости проекта (чтобы Sphinx смог успешно извлечь `docstrings` из модулей):
   ```bash
   pip install -r ../backend/requirements.txt
   pip install -r ../ml_service/requirements.txt
   pip install sphinx myst-parser sphinx_rtd_theme
   ```
3. Соберите HTML-документацию:
   ```bash
   sphinx-build -b html . _build/html
   ```

Готовая документация будет лежать в `docs/_build/html/index.html` — просто откройте этот файл в браузере. Мы расширили конфигурацию Sphinx, так что теперь он охватывает все ключевые модули решения!

## Локальные данные

Датасет и эмулятор вынесены за пределы обоих репозиториев:

```text
hackathon/
├── mt-hackathon-2026/
├── mt-hackathon-2026-context/
└── data/
    ├── dataset/    # train, test, validate, labels, sample_submission.csv,
    │               # Docker-образ, исходные README и docs
    └── archives/   # dataset.zip
```

Для работы с кодом достаточно клонировать основной репозиторий и скачать раздачу: инструкция и ссылка находятся в его README. Путь к данным задаётся через `DATA_DIR`; контекстный репозиторий в этом пути не участвует.

В `other_info/dataset/` оставлены только справочные документы организаторов. Их копии включены в локальную раздачу; относительные пути и команды внутри этих документов относятся к корню датасета. Исходные документы не изменялись.

Новые справочные материалы добавляем в `other_info/`, планы и решения команды — в `planning/`. Статьи можно будет складывать в `research/`, инструкции агентам — в `AGENTS.md`. Внутренний план не является официальным заданием; примеры с `dataset/` в нём относятся к прежнему расположению данных.
