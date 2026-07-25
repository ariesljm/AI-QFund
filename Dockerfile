FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./

RUN uv pip install --system numpy pandas

RUN uv pip install --system lightgbm curl-cffi tls-client

RUN uv pip install --system aiohttp fastapi uvicorn jinja2 openai requests

RUN uv cache clean

COPY . .

RUN mkdir -p data/logs config

ENV PYTHONUNBUFFERED=1

EXPOSE 9123

CMD ["python", "web/app.py"]
