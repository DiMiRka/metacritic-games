FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py \
    && pip install . \
    && pip uninstall -y metacritic-games \
    && rm -rf app build *.egg-info

COPY . .

RUN useradd --create-home appuser \
    && mkdir -p /srv/data/models \
    && chown -R appuser /srv/data
USER appuser

EXPOSE 8000
# SSE мониторинга держит соединение открытым, без таймаута остановка ждала бы его вечно
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "5"]
