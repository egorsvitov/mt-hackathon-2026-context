# Map matching

Рабочий пространственный слой для дашборда и последующей интеграции с ML.

Он строит офлайн-каталог направленных маршрутов по плановым остановкам,
исторической телеметрии и локальному графу Valhalla. В потоке причинный HMM
выбирает маршрутный паттерн и положение на его геометрии. Если подтверждённый
маршрут потерян, HMM может кратковременно запросить Valhalla Meili как fallback.

В пакет входят:

- Catalog и команда build-catalog для построения статического каталога;
- Matcher.update() и Matcher.snapshot() для обработки телеметрии;
- StreamingSpatialAdapter для backend и ML;
- экспорт network.json для дашборда;
- преобразование статического replay в replay с matched-координатами.

Исследовательские абляции CatBoost, сравнение nearest/meili/HMM,
диагностический GeoJSON и тестовые сценарии остаются в ветке route-matching.

## Подготовка Valhalla

Нужен OSM PBF, покрывающий маршруты. Скрипт фиксирует checksum PBF, Docker image
и версию графа в manifest:

    cd map_matching
    python scripts/prepare_graph.py       --pbf /absolute/path/to/central_federal_district-latest.osm.pbf       --osm-dir /absolute/path/to/valhalla-graph       --source-url https://download.openstreetmap.fr/extracts/russia/central_federal_district-latest.osm.pbf

    docker compose up -d valhalla
    curl -fsS http://127.0.0.1:8002/status

## Построение данных дашборда

    cd map_matching
    uv sync
    export DATA_DIR=/absolute/path/to/dataset
    export GRAPH=/absolute/path/to/valhalla-graph/graph_manifest.json

    uv run route-match build-catalog       --data-dir "$DATA_DIR"       --graph-manifest "$GRAPH"       --history-split train       --output artifacts/catalog-train.json

    uv run route-match export-dashboard-network       --catalog artifacts/catalog-train.json       --output ../dashboard/data/network.json

    uv run route-match export-dashboard-replay       --catalog artifacts/catalog-train.json       --network ../dashboard/data/network.json       --input ../dashboard/data/replay.js       --output ../dashboard/data/replay.js

Каталог, HTTP-кэш Valhalla, PBF и tiles являются локальными артефактами и в Git
не добавляются. В репозитории хранится готовая сеть и replay, нужные для
демонстрации дашборда.

## Потоковый интерфейс

    from route_matching import Catalog, StreamingSpatialAdapter

    catalog = Catalog.load("artifacts/catalog-train.json")
    matcher = StreamingSpatialAdapter(catalog, graph=valhalla)

    matcher.update(telemetry_record)
    state = matcher.snapshot(tr_id, T, target_visit_id, cur_dev_s)
    dashboard_fields = matcher.dashboard_fields(state)
    ml_features = state.feature_row()

update обрабатывает только доступные пакеты, а опубликованный snapshot не
пересчитывается будущими событиями. При недоступном сопоставлении адаптер
возвращает состояние с missing-полями, поэтому backend и ML могут продолжить
работу.
