# syntax=docker/dockerfile:1.7
# One image definition, two targets: `api` and `worker`. Both share the same
# code and dependencies and differ only in the process they start.
#
#   docker build --target api -t relay-api .
#   docker build --target worker -t relay-worker .

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, from the lockfile, so this layer is cached across code changes.
COPY pyproject.toml uv.lock ./
COPY packages/relay_core/pyproject.toml packages/relay_core/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/worker/pyproject.toml services/worker/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace

COPY packages packages
COPY services services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" PORT=8080
WORKDIR /app
# Run as an unprivileged user; the image contains no build tools and no secrets.
RUN useradd --system --uid 10001 --no-create-home relay
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
USER 10001
EXPOSE 8080

FROM runtime AS api
CMD ["sh", "-c", "exec uvicorn relay_api.main:app_factory --factory --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*'"]

FROM runtime AS worker
CMD ["sh", "-c", "exec uvicorn relay_worker.main:app_factory --factory --host 0.0.0.0 --port ${PORT}"]
