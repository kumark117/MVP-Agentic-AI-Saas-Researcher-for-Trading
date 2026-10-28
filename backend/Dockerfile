# One image for api, worker and mcp (different commands in docker-compose.yml).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN useradd --create-home --uid 10001 alloyiq
WORKDIR /app

# Dependencies first for layer caching; the project itself is installed editable,
# so the dev bind mount of ./backend/src takes effect without a rebuild.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

USER alloyiq
EXPOSE 8000
CMD ["uvicorn", "alloyiq.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
