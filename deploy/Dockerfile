# syntax=docker/dockerfile:1.7
# Build from the repo root:  docker build -f deploy/Dockerfile -t mcp-ro-proxy .

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra postgres
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --extra postgres

FROM python:3.12-slim
RUN useradd --system --uid 10001 --home-dir /app --no-create-home mcpproxy \
    && mkdir -p /app/data && chown mcpproxy /app/data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY config ./config
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PORT=8080 PROXY_CONFIG=/app/config/gmail.yaml
USER mcpproxy
EXPOSE 8080
# Cloud Run sets PORT; PROXY_CONFIG picks the upstream config baked into the image.
CMD ["sh", "-c", "exec mcp-ro-proxy serve --config \"$PROXY_CONFIG\" --host 0.0.0.0 --port \"$PORT\""]
