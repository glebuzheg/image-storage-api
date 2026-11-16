FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv
WORKDIR /srv
ENV UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-cache

COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
USER 10001:10001

CMD ["sh", "-c", "/srv/.venv/bin/alembic upgrade head && exec /srv/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"]
