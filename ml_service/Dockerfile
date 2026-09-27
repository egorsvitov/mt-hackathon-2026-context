FROM python:3.13-slim AS cpu

WORKDIR /srv
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY ml_service/requirements.txt .
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt

COPY ml_service/app ./app
COPY ml_service/runtime ./runtime
ENV MODEL_DIR=/model
ENV ML_MODE=catboost PYTHONDONTWRITEBYTECODE=1
COPY ml_service/model /model
COPY map_matching /opt/map_matching
RUN pip install --no-cache-dir /opt/map_matching

EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]

FROM cpu AS gpu
RUN pip install --no-cache-dir torch==2.14.0 --index-url https://download.pytorch.org/whl/cu130
ENV ML_MODE=ensemble
