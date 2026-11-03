#!/bin/sh
# Ежедневный бэкап БД (pg_dump -Fc) и фото акторов. Хранятся BACKUP_KEEP_DAYS дней.
# Восстановление: pg_restore --clean --if-exists -d "$PGDATABASE" /backups/db-<дата>.dump
# BACKUP_PING_URL (например, healthchecks.io): пинг после успеха, /fail — после ошибки.
set -u

ping() {
  [ -n "${BACKUP_PING_URL:-}" ] && wget -q -O /dev/null -T 10 "$1" || true
}

run_backup() {
  stamp=$(date +%Y-%m-%d_%H%M)
  tmp="/backups/.db-$stamp.dump"
  if pg_dump -Fc -f "$tmp" && pg_restore --list "$tmp" >/dev/null; then
    mv "$tmp" "/backups/db-$stamp.dump"
    tar -czf "/backups/media-$stamp.tar.gz" -C /media . 2>/dev/null || true
    find /backups -maxdepth 1 \( -name 'db-*.dump' -o -name 'media-*.tar.gz' \) \
      -mtime +"${BACKUP_KEEP_DAYS:-14}" -delete
    echo "$(date -Is) backup ok: db-$stamp.dump ($(du -h "/backups/db-$stamp.dump" | cut -f1))"
    ping "${BACKUP_PING_URL:-}"
  else
    rm -f "$tmp"
    echo "$(date -Is) backup FAILED" >&2
    ping "${BACKUP_PING_URL:-}/fail"
  fi
}

# Первый бэкап сразу после старта, затем каждый день в BACKUP_HOUR (время контейнера, UTC).
run_backup
while true; do
  now=$(date +%s)
  next=$(date -d "$(date +%Y-%m-%d) ${BACKUP_HOUR:-3}:00" +%s)
  [ "$next" -le "$now" ] && next=$((next + 86400))
  sleep $((next - now))
  run_backup
done
