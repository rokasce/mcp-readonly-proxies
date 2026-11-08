# syntax=docker/dockerfile:1.7
# docker build -t mcp-guard .
# A failing test fails the build: the runtime stage copies the test stage's marker.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM build AS test
RUN uv sync --frozen --no-editable
COPY guard.yaml ./
COPY tests ./tests
RUN uv run --frozen --no-sync pytest -q -p no:cacheprovider && touch /tmp/tests-passed

FROM python:3.12-slim
RUN useradd --system --uid 10001 --home-dir /app --no-create-home guard \
    && mkdir -p /app/data && chown guard /app/data
WORKDIR /app
COPY --from=test /tmp/tests-passed /tmp/tests-passed
COPY --from=build /app/.venv /app/.venv
COPY guard.yaml ./
# FASTMCP_HOME holds sign-ins when MCP_GUARD_STORAGE_URL isn't set; it's the only writable path.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 FASTMCP_HOME=/app/data
USER 10001
EXPOSE 3000
CMD ["mcp-guard", "--port", "3000"]
