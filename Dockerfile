# Образ TaskFlow Server.
#
# Многоэтапная сборка: сначала ставим зависимости в отдельный слой, затем
# копируем код. Так при изменении кода пересобирается только код, а пакеты
# берутся из кэша Docker.

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Системные пакеты нужны и для сборки, и для работы:
#   build-essential — cffi/uvloop собираются из исходников на «голом» slim
#   ffmpeg, libsndfile1 — используются PyAV и faster-whisper
#   curl — health-check контейнера
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ffmpeg \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*


FROM base AS builder

COPY pyproject.toml ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.32" \
    "pydantic>=2.9" \
    "pydantic-settings>=2.6" \
    "email-validator>=2.2" \

        "sqlalchemy[asyncio]>=2.0.36" \
        "alembic>=1.14" \
        "python-multipart>=0.0.17" \
        "PyJWT>=2.10" \
        "argon2-cffi>=23.1" \
        "httpx>=0.28" \
        "aiosqlite>=0.20" \
        "orjson>=3.10" \
        "structlog>=24.4" \
        "python-slugify>=8.0" \
        "numpy>=1.26" \
        "av>=11,<16" \
        "faster-whisper>=1.1.0"


FROM base AS runtime

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY --chown=taskflow:taskflow . .

# Каталог данных вынесен наружу: туда кладутся база, файлы и модели.
RUN mkdir -p /app/data/storage /app/data/models /app/data/backups /app/data/web \
    && useradd --create-home --uid 10001 taskflow \
    && chown -R taskflow:taskflow /app

USER taskflow

ENV TASKFLOW_HOST=0.0.0.0 \
    TASKFLOW_PORT=8080 \
    DATABASE_URL=sqlite+aiosqlite:///./data/taskflow.db \
    STORAGE_DIR=./data/storage \
    MODELS_DIR=./data/models \
    BACKUP_DIR=./data/backups

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/v1/meta/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
