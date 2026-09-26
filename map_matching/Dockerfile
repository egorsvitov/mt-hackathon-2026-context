FROM python:3.12-slim
WORKDIR /app
COPY src ./src
ENV PYTHONPATH=/app/src
ENTRYPOINT ["python", "-m", "route_matching.cli"]
