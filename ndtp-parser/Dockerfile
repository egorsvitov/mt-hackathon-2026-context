FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt
COPY . .

EXPOSE 9201
CMD ["python", "main.py"]
