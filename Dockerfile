# Два образа из одного файла: --target web и --target worker.
# Зависимости ставятся строго по uv.lock с проверкой хешей, в образ web не попадают
# fastembed, LLM-клиенты и прочие библиотеки воркера

FROM ghcr.io/astral-sh/uv:0.12.6 AS uv

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH
WORKDIR /srv

FROM base AS deps
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./

FROM deps AS deps-web
RUN uv export --frozen --no-dev --extra web --no-emit-project -o /tmp/requirements.txt \
    && uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --require-hashes --no-cache \
        -r /tmp/requirements.txt

FROM deps AS deps-worker
RUN uv export --frozen --no-dev --extra worker --no-emit-project -o /tmp/requirements.txt \
    && uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --require-hashes --no-cache \
        -r /tmp/requirements.txt

FROM base AS app
RUN useradd --create-home appuser \
    && mkdir -p /srv/data/models \
    && chown -R appuser /srv/data
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

FROM app AS web
COPY --from=deps-web /opt/venv /opt/venv
USER appuser
EXPOSE 8000
# SSE мониторинга держит соединение открытым, без таймаута остановка ждала бы его вечно
CMD ["uvicorn", "app.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "5"]

# Выгрузка бэкапов: rclone и age, шифрование открытым ключом перед отправкой
FROM rclone/rclone:1.68.2 AS offsite
RUN apk add --no-cache age
COPY scripts/offsite.sh /usr/local/bin/offsite.sh
ENTRYPOINT ["sh", "/usr/local/bin/offsite.sh"]

FROM app AS worker
COPY --from=deps-worker /opt/venv /opt/venv
USER appuser
CMD ["python", "-m", "app", "worker"]
