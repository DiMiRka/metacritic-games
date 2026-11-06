#!/bin/sh
# Конфиг Alertmanager собирается при старте из переменных: токен бота и адрес пинга
# не лежат в репозитории. Оба получателя необязательны
set -eu

CONFIG=/tmp/alertmanager.yml
umask 077

receiver=nowhere
receivers='  - name: nowhere
'

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  case "$TELEGRAM_CHAT_ID" in
    *[!0-9-]*) echo "TELEGRAM_CHAT_ID должен быть числом, например -1001234567890" >&2; exit 1 ;;
  esac
  printf '%s' "$TELEGRAM_BOT_TOKEN" > /tmp/telegram_token
  receiver=telegram
  receivers="$receivers  - name: telegram
    telegram_configs:
      - bot_token_file: /tmp/telegram_token
        chat_id: ${TELEGRAM_CHAT_ID}
        parse_mode: HTML
        send_resolved: true
"
else
  echo "TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы: тревоги видны только в Alertmanager" >&2
fi

# Сторожевой сигнал: Watchdog горит всегда, и Alertmanager каждую минуту сообщает об этом
# внешнему сервису. Пропали сообщения — значит, лёг сервер, Prometheus или Alertmanager,
# и тревогу поднимает уже внешний сервис
deadman=nowhere
if [ -n "${DEADMAN_PING_URL:-}" ]; then
  case "$DEADMAN_PING_URL" in
    https://*) ;;
    *) echo "DEADMAN_PING_URL должен начинаться с https://" >&2; exit 1 ;;
  esac
  printf '%s' "$DEADMAN_PING_URL" > /tmp/deadman_url
  deadman=deadman
  receivers="$receivers  - name: deadman
    webhook_configs:
      - url_file: /tmp/deadman_url
        send_resolved: false
"
else
  echo "DEADMAN_PING_URL не задан: падение всего сервера останется незамеченным" >&2
fi

cat > "$CONFIG" <<EOF
route:
  receiver: ${receiver}
  group_by: [alertname]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: ['alertname = "Watchdog"']
      receiver: ${deadman}
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 1m
receivers:
${receivers}
EOF

exec /bin/alertmanager --config.file="$CONFIG" --storage.path=/alertmanager "$@"
