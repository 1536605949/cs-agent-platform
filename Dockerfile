# syntax=docker/dockerfile:1

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data
COPY scripts ./scripts

# Run as an unprivileged user; /app/data must stay writable for SQLite.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

ENV DATABASE_URL=sqlite:///./data/app.db \
    APP_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
