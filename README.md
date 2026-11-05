# Игры Metacritic

Сервис раз в час забирает 20 игр с Metacritic, которые сегодня ещё не обрабатывались, сохраняет оценки по платформам, пишет нейросетью резюме отзывов критиков и игроков и подбирает похожие игры. Веб-интерфейс: каталог с поиском, фильтром по платформе и сортировкой, карточка игры. Служебная часть (мониторинг воркера и кнопка ручного запуска) доступна только администраторам после входа.

Стек: Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, PostgreSQL 16 + pgvector + pg_trgm, Jinja2 + HTMX, APScheduler, Claude или любой OpenAI-совместимый LLM (по умолчанию в примере Gemini), fastembed, Docker Compose, Caddy.

Сутки для обхода считаются по московскому времени (`TIMEZONE`), время в интерфейсе тоже московское.

## Структура проекта

Код разложен по предметным областям: всё про одну часть системы лежит в одном пакете.

```
app/
  core/         настройки, подключение к базе, базовая модель, время, проверка внешних URL,
                роли базы с минимальными правами
  catalog/      игры: модели, запросы каталога, похожие игры, схемы JSON API
  scraper/      клиент Metacritic (лимит запросов, повторы) и разбор ответов
  enrichment/   LLM-резюме отзывов и эмбеддинги
  crawler/      обход: курсор дня, обработка игры, проход с блокировкой,
                ручной запуск через NOTIFY/LISTEN, модели обхода
  monitoring/   снимок состояния для /monitor
  auth/         пользователи, пароли, серверные сессии, команды для пользователей
  web/          FastAPI: routes/ (публичные, служебные, вход), middleware/ (CSRF,
                заголовки, лимиты), шаблоны и статика
  worker/       процесс воркера: расписание, ручные запуски, heartbeat
  cli.py        команды python -m app
tests/          повторяют структуру app/, фикстуры ответов Metacritic в tests/fixtures
migrations/     Alembic
```

Зависимости идут в одну сторону: `web` и `worker` → предметные пакеты → `core`. `core` ни от кого не зависит, `web` никто не импортирует. Правила записаны в `tests/test_architecture.py`, новый импорт против них уронит тесты.

Команды:

```bash
python -m app worker                       # воркер по расписанию
python -m app crawl                        # один проход прямо сейчас
python -m app users create-admin <login>   # администратор, пароль через getpass
python -m app users set-password <login>   # смена пароля, сессии пользователя завершаются
python -m app db-roles                     # роли app_web и app_worker (делает сервис migrate)
uvicorn app.web.app:app                    # сайт
```

## Локальный запуск

```bash
cp .env.example .env            # заполнить пароли базы и ключ LLM
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d db   # dev-файл открывает порт 5432
python -m venv .venv && .venv/Scripts/activate   # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
python -m app users create-admin admin   # пароль спросит getpass
uvicorn app.web.app:app --reload   # http://localhost:8000
python -m app worker             # воркер по расписанию, в отдельном терминале
python -m app crawl              # или один проход вручную
pytest
```

Весь стек целиком: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build`, сайт на https://localhost (сертификат самоподписанный). Тесты и локальный uvicorn ходят в базу владельцем схемы, контейнеры web и worker — своими ролями.

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

Postgres наружу не торчит: в `docker-compose.yml` у него нет опубликованных портов, он виден только во внутренней сети `backend`.

**5. Забрать код**

```bash
git clone https://github.com/DiMiRka/metacritic-games.git /opt/metacritic-games
cd /opt/metacritic-games
```

**6. Настроить `.env`**

```bash
cp .env.example .env
openssl rand -hex 24      # три раза: пароль владельца базы и паролей двух ролей
nano .env
```

В `.env` поменять:

| Переменная | Значение |
|---|---|
| `ENV` | `prod`: cookie только по HTTPS, `/docs` выключены, Caddy ставит HSTS |
| `POSTGRES_PASSWORD` | сгенерированный пароль |
| `APP_WEB_DB_PASSWORD`, `APP_WORKER_DB_PASSWORD` | ещё два сгенерированных пароля (только hex, они попадают в URL базы) |
| `DOMAIN` | IP сервера через дефисы + `.sslip.io`, например `203-0-113-10.sslip.io` |
| `LLM_PROVIDER`, `SUMMARY_MODEL`, ключи | как в разделе про LLM в `.env.example` |

`DATABASE_URL` на сервере трогать не нужно: compose подставляет адрес базы сам.

**7. Запустить**

```bash
docker compose up -d --build
docker compose ps          # migrate: Exited (0), остальные Up
```

Первый проход стартует сразу после запуска воркера, дальше раз в час.

**7а. Создать администратора**

```bash
docker compose run --rm web python -m app users create-admin admin
```

Пароль (не короче 12 символов) вводится интерактивно и нигде не сохраняется в открытом виде. Сменить пароль: `python -m app users set-password admin`, при этом все сессии пользователя завершаются.

**8. Проверить**

```bash
curl -I https://ВАШ_ДОМЕН/health
docker compose logs -f worker
```

В браузере: `https://ВАШ_ДОМЕН/` (каталог) и `https://ВАШ_ДОМЕН/monitor` (мониторинг, попросит войти).

Если сертификат не выпустился, смотреть `docker compose logs caddy`. Обычно причина в закрытом порту 80 или в опечатке в `DOMAIN`.

**Обновление**

```bash
cd /opt/metacritic-games && git pull && docker compose up -d --build
```

Сервис `migrate` при каждом запуске применяет миграции и затем `python -m app db-roles`. Команда идемпотентна: создаёт роли `app_web` и `app_worker` или обновляет их пароли из `.env` и заново выдаёт права. На уже работающем сервере после добавления паролей ролей в `.env` достаточно того же `docker compose up -d --build` или отдельно `docker compose run --rm migrate`.

**Полезное**

```bash
docker compose logs -f web worker      # логи
docker compose restart worker          # перезапустить воркер
docker compose exec db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup.sql   # бэкап базы
```

## Безопасность

**Вход и сессии.** Пароли хешируются argon2. Сессии хранятся на сервере (в базе только sha256 от токена), поэтому их можно отозвать: выход, смена пароля и деактивация пользователя действуют сразу. Cookie `HttpOnly`, `SameSite=Lax`, в проде `Secure` и префикс `__Host-`, срок жизни `SESSION_TTL_HOURS`. Ответ и время проверки одинаковые для неверного логина и неверного пароля. Неудачные попытки входа считаются по паре IP и логин в базе (`LOGIN_MAX_ATTEMPTS` за `LOGIN_WINDOW_MINUTES`). Поток мониторинга перепроверяет сессию каждые 30 секунд и закрывается, если её отозвали.

**CSRF.** Все `POST`, `PUT`, `PATCH`, `DELETE` принимаются, только если `Origin` (или `Referer`) совпадает с адресом сайта. Для запросов от вошедшего пользователя дополнительно нужен заголовок `X-CSRF-Token`: токен выводится из токена сессии, лежит в `<meta>` и отправляется из `fetch` и htmx. На `SameSite=Lax` как на единственную защиту не полагаемся.

**CORS не включён намеренно.** Без CORS-middleware браузер разрешает чужим сайтам читать ответы API только того же источника. Сервису не нужны запросы с других доменов, поэтому любое ослабление этой политики только расширило бы поверхность атаки.

**CSP и заголовки.** Политику ставит middleware приложения: скрипты и стили только с собственного адреса (htmx лежит в `app/web/static`, инлайнового кода нет), картинки ещё с `www.metacritic.com`, iframe только с `cdn.jwplayer.com`, `frame-ancestors 'none'`. Плюс `nosniff`, `Referrer-Policy`, `Permissions-Policy`. HSTS ставит Caddy и только при `ENV=prod`. Ссылки из внешних данных (обложки, видео, летсплеи) проходят `safe_url`: только `https` и хост из списка разрешённых, а шаблоны проверяют их ещё раз перед выводом.

**Ограничение частоты.** `/`, `/api/games` и `/login` ограничены по IP (`RATE_LIMIT_*`). Счётчики живут в памяти процесса web: при нескольких процессах uvicorn лимит фактически умножится на их число. Сейчас процесс один, это допустимо. Для нескольких процессов счётчик нужно вынести в Redis или в базу, как это уже сделано для попыток входа.

**Адрес клиента.** uvicorn запущен с `--forwarded-allow-ips "*"` и берёт IP из `X-Forwarded-For`. Это безопасно, потому что до web можно достучаться только из Caddy: web публикует порт только во внутреннюю сеть `frontend`, где кроме него есть один Caddy. Caddy же перезаписывает `X-Forwarded-For` реальным адресом клиента, так что подставить чужой IP снаружи нельзя.

**Роли в базе.** Владелец схемы используется только сервисом `migrate`. `app_worker` может читать и менять рабочие таблицы каталога и обхода и видит только `users.id` (чтобы записать, кто запустил проход). `app_web` только читает каталог, проходы и статусы, а пишет лишь в `users`, `sessions` и `login_attempts`. DDL ни у одной роли нет. Для таблиц будущих миграций заданы `ALTER DEFAULT PRIVILEGES`: воркеру рабочие права, сайту только чтение.

**Контейнеры и сети.** `backend` (db, web, worker) и `frontend` (web, caddy) внутренние, без выхода наружу. Выход в интернет есть только у воркера (Metacritic, LLM, YouTube) и Caddy (сертификаты). У web, worker и migrate файловая система только для чтения (кроме `/tmp` и тома с моделями), сброшены все capabilities и включён `no-new-privileges`. Caddy оставлена только `NET_BIND_SERVICE`. У всех сервисов есть лимиты памяти, у воркера с запасом под faster-whisper.

