FROM python:3.11-slim AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv pip install --system -r pyproject.toml && uv cache clean


FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

COPY . .

RUN cp data/schema.sql /app/schema.sql && mkdir -p data/logs config && \
    find /usr/local/lib/python3.11/site-packages -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

ENV PYTHONUNBUFFERED=1

EXPOSE 9123

CMD ["python", "web/app.py"]
