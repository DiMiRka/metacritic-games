#!/bin/bash
# Ежедневный бэкап базы с проверкой восстановления. Состояние в таблице backup_runs:
# по ней решается, пора ли делать копию, и по ней же Prometheus следит за свежестью
set -uo pipefail

DIR=/backups
HOUR=${BACKUP_HOUR:-3}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}
CHECK_DB=restore_check
CHECK_EVERY=300
# Без NOTICE от сервера: в выводе контейнера остаются только JSON-строки
export PGOPTIONS="-c client_min_messages=warning"

log() {
  local level=$1
  shift
  local msg=${*//\"/\'}
  printf '{"ts":"%s","level":"%s","logger":"backup","msg":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" "$level" "$msg"
}

sql() {
  psql -X -v ON_ERROR_STOP=1 -tAq "$@"
}

verify() {
  local file=$1 restored current games
  dropdb --if-exists "$CHECK_DB" && createdb "$CHECK_DB" || return 1
  if ! pg_restore --no-owner --exit-on-error -d "$CHECK_DB" "$file" 2>/tmp/restore.log; then
    log ERROR "восстановление не удалось: $(tail -n 1 /tmp/restore.log)"
    dropdb --if-exists "$CHECK_DB"
    return 1
  fi
  restored=$(sql -d "$CHECK_DB" -c "SELECT version_num FROM alembic_version")
  games=$(sql -d "$CHECK_DB" -c "SELECT count(*) FROM games")
  dropdb --if-exists "$CHECK_DB"
  current=$(sql -c "SELECT version_num FROM alembic_version")
  if [ -z "$restored" ] || [ "$restored" != "$current" ]; then
    log ERROR "версия схемы в копии '$restored', в базе '$current'"
    return 1
  fi
  log INFO "копия восстановлена в отдельную базу: схема $restored, игр $games"
}

run_backup() {
  local id file name size
  if ! id=$(sql -c "INSERT INTO backup_runs (status) VALUES ('running') RETURNING id"); then
    log ERROR "база недоступна, бэкап отложен"
    return 1
  fi
  name="metacritic-$(date +%Y%m%d-%H%M%S).dump"
  file="$DIR/$name"
  if pg_dump -Fc -f "$file.part" 2>/tmp/dump.log && mv "$file.part" "$file" && verify "$file"; then
    size=$(stat -c %s "$file")
    sql -c "UPDATE backup_runs SET status = 'success', finished_at = now(), file = '$name',
            size_bytes = $size, restore_checked = true WHERE id = $id"
    log INFO "бэкап $name готов, $size байт"
  else
    rm -f "$file.part"
    sql -c "UPDATE backup_runs SET status = 'failed', finished_at = now(),
            error = 'подробности в логах контейнера backup' WHERE id = $id"
    log ERROR "бэкап не удался: $(tail -n 1 /tmp/dump.log)"
  fi
  find "$DIR" -name 'metacritic-*.dump' -mtime +"$KEEP_DAYS" -delete
}

due() {
  # Пора, если успешной копии не было больше 23 часов и наступил час бэкапа,
  # либо её нет больше 25 часов: первый запуск или пропущенный час
  local hours
  hours=$(sql -c "SELECT coalesce(extract(epoch FROM now() - max(started_at)) / 3600, 1e6)
                  FROM backup_runs WHERE status = 'success'") || return 1
  awk -v h="$hours" -v now="$(date +%-H)" -v at="$HOUR" \
    'BEGIN { exit !((h > 23 && now == at) || h > 25) }'
}

log INFO "бэкап каждый день в $HOUR:00 ($(date +%Z)), хранение $KEEP_DAYS дней"
while true; do
  if due; then
    run_backup
  fi
  sleep "$CHECK_EVERY"
done
