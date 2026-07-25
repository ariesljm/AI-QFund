FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv pip install --system -r pyproject.toml && uv cache clean

COPY . .

COPY data/schema.sql schema.sql

RUN mkdir -p data/logs config

ENV PYTHONUNBUFFERED=1

EXPOSE 9123

CMD ["python", "web/app.py"]
