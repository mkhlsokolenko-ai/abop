#!/bin/bash
# Бэкап прод-БД ABOP (спринт 3, №10). Данные пилота — в одной копии на одном диске, это
# исправляем. Проверка восстановления сделана 20.11: дамп 8 МБ, 48 таблиц, dp_records
# идентичны живой базе (finding 16831, doc1c 7742).
#
# ВАЖНО (найдено 20.11): база abop живёт в контейнере svod-analytics-pg — ЧУЖОМ по имени.
# Пересборка svod-стека уронит хранилище ABOP. Хозяину контейнеров: разнести или зафиксировать
# владение в ops-доке. Скрипт не зависит от имени: берёт контейнер, который слушает 5433.
#
# Крон (владелец):  0 3 * * * /opt/abop/scripts/backup_db.sh >> /opt/backups/backup.log 2>&1
#
# Имена переменных латиницей: bash не допускает кириллические идентификаторы (урок «svershik»,
# тот же класс — две системы письма в одном месте).
set -euo pipefail

PG_CONTAINER=$(docker ps --format "{{.Names}} {{.Ports}}" | grep "127.0.0.1:5433" | awk '{print $1}')
[ -n "$PG_CONTAINER" ] || { echo "нет контейнера на 5433"; exit 1; }

BACKUP_DIR=/opt/backups
mkdir -p "$BACKUP_DIR"
OUT_FILE="$BACKUP_DIR/abop-$(date +%F).sql.gz"

docker exec "$PG_CONTAINER" pg_dump -U postgres -d abop | gzip > "$OUT_FILE.tmp"
mv "$OUT_FILE.tmp" "$OUT_FILE"

# restore-проба: в scratch-базу, счёт главной таблицы данных — бэкап без пробы это надежда
docker exec "$PG_CONTAINER" psql -U postgres -c "DROP DATABASE IF EXISTS abop_restore_check" >/dev/null
docker exec "$PG_CONTAINER" psql -U postgres -c "CREATE DATABASE abop_restore_check" >/dev/null
zcat "$OUT_FILE" | docker exec -i "$PG_CONTAINER" psql -U postgres -d abop_restore_check >/dev/null 2>&1
ROWS=$(docker exec "$PG_CONTAINER" psql -U postgres -d abop_restore_check -t -c \
  "SELECT count(*) FROM dp_records WHERE entity='finding'" | tr -d " ")
docker exec "$PG_CONTAINER" psql -U postgres -c "DROP DATABASE abop_restore_check" >/dev/null
echo "$(date +%F_%T) $(basename "$OUT_FILE") $(du -h "$OUT_FILE" | cut -f1) · finding в копии: $ROWS"

# ротация: 14 дневных
ls -1t "$BACKUP_DIR"/abop-2*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
