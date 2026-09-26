#!/bin/sh
# Режим дашборда по умолчанию: DASHBOARD_API="/api" — живой backend через прокси, пусто — воспроизведение.
set -e
if [ -n "$DASHBOARD_API" ]; then
  printf 'window.DASH_CONFIG = { api: "%s" };\n' "$DASHBOARD_API" > /usr/share/nginx/html/config.js
fi
