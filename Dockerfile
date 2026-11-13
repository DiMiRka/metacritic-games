# Два образа из одного файла: --target web и --target worker.
# Зависимости ставятся строго по uv.lock с проверкой хешей, в образ web не попадают
# fastembed, LLM-клиенты и прочие библиотеки воркера

FROM ghcr.io/astral-sh/uv:0.12.6@sha256:88bc6eb1ccd4b82efd0e1b530caffabddf50dc2bf612e66c14ea25b8ee8a4d3d AS uv

FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH
# upgrade подтягивает исправления Debian, вышедшие после сборки базового образа
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
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
# rclone собирается из исходников: официальный образ может отставать по версии Go,
# а исправления crypto/x509 и crypto/tls важны для выгрузки по TLS. Версия Go обновляется
# вместе с образом golang (Dependabot), версия rclone — в RCLONE_VERSION
FROM golang:1.27.3-alpine3.24@sha256:fa3d596898d9f126af7c01d14ee7597bab3f39cccf392ef36b69fe77cd681f18 AS rclone-build
ARG RCLONE_VERSION=v1.76.0
RUN CGO_ENABLED=0 go install -trimpath \
        -ldflags "-s -w -X github.com/rclone/rclone/fs.Version=${RCLONE_VERSION}" \
        github.com/rclone/rclone@${RCLONE_VERSION}

FROM alpine:3.24.4@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf AS offsite
# upgrade подтягивает исправления Alpine, вышедшие после сборки базового образа
RUN apk upgrade --no-cache && apk add --no-cache age ca-certificates
COPY --from=rclone-build /go/bin/rclone /usr/local/bin/rclone
COPY scripts/offsite.sh /usr/local/bin/offsite.sh
# Без root: контейнеру нужно только читать /backups и писать во временный /tmp
ENV HOME=/tmp
USER nobody
ENTRYPOINT ["sh", "/usr/local/bin/offsite.sh"]

FROM app AS worker
COPY --from=deps-worker /opt/venv /opt/venv
USER appuser
CMD ["python", "-m", "app", "worker"]
