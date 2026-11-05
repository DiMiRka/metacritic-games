# Игры Metacritic

Сервис раз в час забирает 20 игр с Metacritic, которые сегодня ещё не обрабатывались, сохраняет оценки по платформам, пишет нейросетью резюме отзывов критиков и игроков и подбирает похожие игры. Веб-интерфейс: каталог с поиском, фильтром по платформе и сортировкой, карточка игры, мониторинг воркера с кнопкой ручного запуска.

Стек: Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, PostgreSQL 16 + pgvector + pg_trgm, Jinja2 + HTMX, APScheduler, Claude или любой OpenAI-совместимый LLM (по умолчанию в примере Gemini), fastembed, Docker Compose, Caddy.

Сутки для обхода считаются по московскому времени (`TIMEZONE`), время в интерфейсе тоже московское.

## Локальный запуск

```bash
cp .env.example .env            # заполнить пароль базы и ключ LLM
docker compose up -d db
python -m venv .venv && .venv/Scripts/activate   # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload   # http://localhost:8000
python -m app.worker            # воркер по расписанию, в отдельном терминале
python -m app.crawler           # или один проход вручную
pytest
```

Весь стек целиком: `docker compose up -d --build`, сайт на https://localhost (сертификат самоподписанный).

## Деплой на VPS

Нужен сервер с Ubuntu 22.04/24.04, 2 vCPU и 4 ГБ памяти. Сервер должен стоять **за пределами России**: API Gemini и Claude не обслуживают российские IP, YouTube оттуда тоже работает плохо.

Домен не обязателен: адрес вида `203-0-113-10.sslip.io` сам указывает на IP `203.0.113.10`, и Caddy получает на него настоящий HTTPS-сертификат от Let's Encrypt.

Все команды ниже выполняются на сервере от root.

**1. Подключиться**

```bash
ssh root@IP_СЕРВЕРА
```

**2. Обновить систему и поставить Docker**

```bash
apt update && apt -y upgrade
curl -fsSL https://get.docker.com | sh
docker compose version
```

**3. Файл подкачки на 2 ГБ** (запас памяти для сборки образа и расшифровки речи)

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

**4. Файрвол:** открыты только SSH и веб

```bash
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
```

Postgres наружу не торчит: в `docker-compose.yml` его порт привязан к `127.0.0.1`.

**5. Забрать код**

```bash
git clone https://github.com/DiMiRka/metacritic-games.git /opt/metacritic-games
cd /opt/metacritic-games
```

**6. Настроить `.env`**

```bash
cp .env.example .env
openssl rand -hex 24      # скопировать вывод, это пароль базы
nano .env
```

В `.env` поменять:

| Переменная | Значение |
|---|---|
| `POSTGRES_PASSWORD` | сгенерированный пароль |
| `DOMAIN` | IP сервера через дефисы + `.sslip.io`, например `203-0-113-10.sslip.io` |
| `LLM_PROVIDER`, `SUMMARY_MODEL`, ключи | как в разделе про LLM в `.env.example` |

`DATABASE_URL` на сервере трогать не нужно: compose подставляет адрес базы сам.

**7. Запустить**

```bash
docker compose up -d --build
docker compose ps          # migrate: Exited (0), остальные Up
```

Первый проход стартует сразу после запуска воркера, дальше раз в час.

**8. Проверить**

```bash
curl -I https://ВАШ_ДОМЕН/health
docker compose logs -f worker
```

В браузере: `https://ВАШ_ДОМЕН/` (каталог) и `https://ВАШ_ДОМЕН/monitor` (мониторинг).

Если сертификат не выпустился, смотреть `docker compose logs caddy`. Обычно причина в закрытом порту 80 или в опечатке в `DOMAIN`.

**Обновление**

```bash
cd /opt/metacritic-games && git pull && docker compose up -d --build
```

**Полезное**

```bash
docker compose logs -f web worker      # логи
docker compose restart worker          # перезапустить воркер
docker compose exec db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup.sql   # бэкап базы
```
