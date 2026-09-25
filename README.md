# Рабочий контекст команды

Код решения находится в `../mt-hackathon-2026/`. Этот репозиторий содержит задание, справочные материалы и внутренние заметки; для запуска и разработки решения он не требуется.

| Путь | Содержимое |
|---|---|
| [task/assignment.md](task/assignment.md) | Полный текст задания в Markdown |
| [task/assignment.pdf](task/assignment.pdf) | Исходный PDF |
| [other_info/dataset/README.md](other_info/dataset/README.md) | Исходное описание датасета, метрики и submission |
| [other_info/dataset/docs/Emulator-and-Telematic-Packets-Specification.md](other_info/dataset/docs/Emulator-and-Telematic-Packets-Specification.md) | Спецификация NDTP и эмулятора |
| [planning/plan_gpt6_astra.md](planning/plan_gpt6_astra.md) | Ранее подготовленный анализ и план; выводы требуют проверки |
| [research/deep-research-report.md](research/deep-research-report.md) | Обзор литературы и рекомендации по ML-архитектуре |
| [planning/architecture_after_research.md](planning/architecture_after_research.md) | Разбор отчёта, повторная проверка данных и предложение архитектуры |
| [ml_models/01_catboost_baseline/](ml_models/01_catboost_baseline/) | Первый CatBoost baseline: код, обученная модель, submission, тесты и описание эксперимента |
| [research/papers/Wai_Zhou_2020_Real_Time_Bus_Time_Predictions.pdf](research/papers/Wai_Zhou_2020_Real_Time_Bus_Time_Predictions.pdf) | Wai & Zhou (2020): production-архитектура XGBoost-прогнозов времени движения и стоянки |

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
