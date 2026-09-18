#!/usr/bin/env bash
# Канонический деплой ABOP Web API (server-1). ЖЁСТКО фиксирует зависимости, чтобы перенакат
# НЕ терял данные:
#   1) том abop_ape:/root/.ape — canonical store (doc1c/ref1c/…), рецепты, коннекторы, память.
#      БЕЗ этого тома данные Data Plane живут в эфемерной ФС контейнера и пропадают при redeploy.
#   2) --env-file .env — все интеграции/токены.
#   3) --restart unless-stopped — переживает рестарт демона.
# Рецепты/коннекторы/агенты/прогоны дублируются в Postgres (dp_recipes/dp_connectors/…),
# но ЭМИТИРОВАННЫЕ канонические записи (jsonl) — только в томе. Том обязателен.
set -euo pipefail
cd /opt/abop

IMG=abop-webapi
VOL=abop_ape                 # <-- НЕ МЕНЯТЬ имя: в нём вся история Data Plane
ENVF=/opt/abop/.env

echo "[deploy] build image $IMG"
docker build -q -f Dockerfile.webapi -t "$IMG" . >/dev/null

# том создаётся автоматически при первом run; проверим, что он есть/не пуст (диагностика)
if docker volume inspect "$VOL" >/dev/null 2>&1; then
  echo "[deploy] volume $VOL exists (canonical store сохранится)"
else
  echo "[deploy] volume $VOL will be created (первый запуск — Data Plane наполнить рецептами)"
fi

echo "[deploy] restart container"
docker rm -f "$IMG" >/dev/null 2>&1 || true
docker run -d --name "$IMG" --network host --restart unless-stopped \
  --env-file "$ENVF" -v "$VOL":/root/.ape "$IMG" >/dev/null

sleep 5
if curl -sf http://127.0.0.1:8091/api/health >/dev/null; then
  echo "[deploy] health-ok"
else
  echo "[deploy] ВНИМАНИЕ: health-check не прошёл" >&2
  exit 1
fi
