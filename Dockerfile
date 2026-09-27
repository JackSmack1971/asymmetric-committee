FROM python:3.12-slim
# git: the worker commits and pushes anchor manifests (orchestration/anchor_adapters.py)
RUN apt-get update && apt-get install -y --no-install-recommends git \n    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.5.*
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
