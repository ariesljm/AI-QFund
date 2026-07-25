FROM python:3.11-slim AS builder

WORKDIR /app

COPY pyproject.toml uv.lock ./

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN uv sync --frozen && uv cache clean

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv

COPY . .

RUN mkdir -p data/logs config

ENV PYTHONUNBUFFERED=1

EXPOSE 9123

CMD ["/app/.venv/bin/python", "web/app.py"]
