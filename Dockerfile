# Verging Memory CI test deployment for Basic Memory.
#
# The image serves the adapter in verging_adapter/, which runs Basic Memory
# itself in-process. Basic Memory is installed from this checkout's source
# rather than built as a wheel: its version metadata comes from git tags, which
# are not available inside the build context.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_COMPILE_BYTECODE=1

# Dependencies first so a source-only change reuses this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY verging_adapter/ ./verging_adapter/

ENV PYTHONPATH=/app/src:/app \
    LOGFIRE_IGNORE_NO_CONFIG=1 \
    VERGING_ADAPTER_DATA_DIR=/var/lib/verging-adapter

EXPOSE 8080

CMD ["python3", "-m", "verging_adapter"]
