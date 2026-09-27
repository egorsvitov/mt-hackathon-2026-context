Добро пожаловать в документацию проекта!
========================================

Это автогенерируемая документация для бэкенда и ML-сервиса нашего решения.

.. toctree::
   :maxdepth: 2
   :caption: Оглавление:

   modules
   app
   route_matching
   backend_client
   crc
   framing
   protocol
   receiver
   traffic_adapter

Инфраструктура
--------------
Решение состоит из:

* **Backend (FastAPI)** — пайплайн обработки телеметрии, расчет признаков.
* **ML Service** — сохранённый CatBoost v2 seed42; опционально GPU-ансамбль TS2Vec + CatBoost. Инструкции и ограничения: ``ml_service/README.md``.
* **Dashboard (Nginx + JS)** — визуализация на карте.
