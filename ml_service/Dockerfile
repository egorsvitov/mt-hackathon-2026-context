FROM python:3.12-slim

WORKDIR /srv
COPY ml_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ml_service/app ./app
ENV MODEL_DIR=/model
COPY ml_models/01_catboost_baseline/artifacts/modeling/model /model

EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]