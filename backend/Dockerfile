FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt
COPY map_matching /opt/map_matching
RUN pip install --no-cache-dir /opt/map_matching
COPY backend/app ./app
COPY ml_service/runtime /opt/ml_service/runtime
COPY ml_service/model/catalog.json ml_service/model/manifest.json /opt/ml_service/model/
ENV PYTHONPATH=/opt/ml_service ML_MODEL_DIR=/opt/ml_service/model

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3   CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
