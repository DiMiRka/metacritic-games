#!/bin/sh
# Выгрузка бэкапов во внешнее хранилище. Каждый дамп шифруется age открытыми ключами из
# BACKUP_AGE_RECIPIENTS, закрытый ключ на сервере не хранится. Без ключей ничего не выгружается
set -u

INTERVAL=${OFFSITE_INTERVAL:-3600}
REMOTE=${BACKUP_REMOTE:?задайте BACKUP_REMOTE}

log() {
  level=$1
  shift
  msg=$(printf '%s' "$*" | tr '"' "'")
  printf '{"ts":"%s","level":"%s","logger":"offsite","msg":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" "$level" "$msg"
}

recipients=/tmp/age-recipients
printf '%s\n' "${BACKUP_AGE_RECIPIENTS:-}" | tr ', ' '\n\n' | grep '^age1' > "$recipients"

upload_new() {
  if ! uploaded=$(rclone lsf "$REMOTE" --include 'metacritic-*.dump.age' 2>/tmp/lsf.log); then
    log ERROR "хранилище недоступно: $(tail -n 1 /tmp/lsf.log)"
    return 1
  fi
  for file in /backups/metacritic-*.dump; do
    [ -e "$file" ] || continue
    name="$(basename "$file").age"
    if printf '%s\n' "$uploaded" | grep -qxF "$name"; then
      continue
    fi
    if age -R "$recipients" -o "/tmp/$name" "$file" \
      && rclone copyto "/tmp/$name" "$REMOTE/$name" 2>/tmp/copy.log; then
      log INFO "выгружен $name"
    else
      log ERROR "не удалось выгрузить $name: $(tail -n 1 /tmp/copy.log 2>/dev/null)"
    fi
    rm -f "/tmp/$name"
  done
}

while true; do
  if [ -s "$recipients" ]; then
    upload_new
  else
    log ERROR "BACKUP_AGE_RECIPIENTS не задан: без шифрования копии не выгружаются"
  fi
  [ "${OFFSITE_ONCE:-}" = "1" ] && exit 0
  sleep "$INTERVAL"
done
