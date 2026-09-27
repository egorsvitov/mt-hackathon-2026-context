# Дашборд: локальная подложка используется без скачивания; при её отсутствии скачиваем один раз.
FROM python:3.11-alpine AS builder

WORKDIR /app
COPY tools/fetch_basemap.py tools/fetch_basemap.py
COPY data /app/data
# Не скачиваем ни карту, ни инструменты, если PMTiles уже есть в build context.
RUN if [ ! -s /app/data/basemap/moscow.pmtiles ]; then \
      apk add --no-cache curl tar gzip && \
      python tools/fetch_basemap.py --out /app/data/basemap/moscow.pmtiles; \
    fi

FROM nginx:1.27-alpine

ENV BACKEND_URL=http://backend:8000 \
    DASHBOARD_API=""

COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --chmod=755 docker/40-dashboard-config.sh /docker-entrypoint.d/40-dashboard-config.sh
COPY index.html config.js /usr/share/nginx/html/
COPY css /usr/share/nginx/html/css
COPY js /usr/share/nginx/html/js
COPY vendor /usr/share/nginx/html/vendor
COPY --from=builder /app/data /usr/share/nginx/html/data

EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
