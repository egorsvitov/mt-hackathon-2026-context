#!/bin/sh
# DASHBOARD_API задаёт адрес backend через прокси nginx, пустое значение включает воспроизведение в браузере
set -e
if [ -n "$DASHBOARD_API" ]; then
  printf 'window.DASH_CONFIG = { api: "%s" };\n' "$DASHBOARD_API" > /usr/share/nginx/html/config.js
fi
