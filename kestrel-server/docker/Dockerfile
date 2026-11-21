# docker/Dockerfile
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# Keep --workers 1. The app runs a single in-process MQTT subscriber and holds
# in-memory state (runtime settings cache, notifier cooldown/rate limits).
# Multiple workers would double-ingest MQTT and desync that state.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
