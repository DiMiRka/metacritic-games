#!/bin/sh
# Конфиг Alertmanager собирается при старте из переменных: токен бота не лежит в репозитории
set -eu

CONFIG=/tmp/alertmanager.yml

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  case "$TELEGRAM_CHAT_ID" in
    *[!0-9-]*) echo "TELEGRAM_CHAT_ID должен быть числом, например -1001234567890" >&2; exit 1 ;;
  esac
  umask 077
  printf '%s' "$TELEGRAM_BOT_TOKEN" > /tmp/telegram_token
  cat > "$CONFIG" <<EOF
route:
  receiver: telegram
  group_by: [alertname]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
receivers:
  - name: telegram
    telegram_configs:
      - bot_token_file: /tmp/telegram_token
        chat_id: ${TELEGRAM_CHAT_ID}
        parse_mode: HTML
        send_resolved: true
EOF
else
  echo "TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы: тревоги видны только в Alertmanager" >&2
  cat > "$CONFIG" <<EOF
route:
  receiver: nowhere
receivers:
  - name: nowhere
EOF
fi

exec /bin/alertmanager --config.file="$CONFIG" --storage.path=/alertmanager "$@"
