# Дашборд диспетчера: статика на nginx (карта и подложка — локальные, без внешних запросов).
# Перед сборкой: python tools/build_fixtures.py --data-dir <dataset> и python tools/fetch_basemap.py
FROM nginx:1.27-alpine

ENV BACKEND_URL=http://backend:8000 \
    DASHBOARD_API=""

COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --chmod=755 docker/40-dashboard-config.sh /docker-entrypoint.d/40-dashboard-config.sh
COPY index.html config.js /usr/share/nginx/html/
COPY css /usr/share/nginx/html/css
COPY js /usr/share/nginx/html/js
COPY vendor /usr/share/nginx/html/vendor
# replay.js и подложка карты data/basemap/moscow.pmtiles (python tools/fetch_basemap.py)
COPY data /usr/share/nginx/html/data

EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
