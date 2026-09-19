#!/usr/bin/env bash
# Ночная пауза Vast-инстансов (экономия GPU-биллинга; storage остаётся). Запуск по cron на server-1.
#   vast_schedule.sh stop   → останавливает инстансы (GPU не капает; диск/модель сохраняются)
#   vast_schedule.sh start  → запускает; ждёт running; переподхватывает НОВЫЙ порт Qwen → .env → рестарт ABOP
# Инстансы: ape-qwen30b (ABOP LLM) + redteam-judge-ab. Ключ в /root/.vast_api_key (chmod 600).
# ВНИМАНИЕ: red-team паузится по решению владельца 2026-09-19 (оба). Порт Qwen может смениться после start.
set -eu

ACTION="${1:-}"
KEY_FILE=/root/.vast_api_key
API=https://console.vast.ai/api/v1/instances
QWEN_ID=51396167          # ape-qwen30b — на него завязан ABOP LOCAL_LLM_BASE_URL
REDTEAM_ID=49036119       # redteam-judge-ab
ENV_FILE=/opt/abop/.env
LOG=/var/log/vast_schedule.log

[ -f "$KEY_FILE" ] || { echo "нет $KEY_FILE"; exit 1; }
KEY=$(cat "$KEY_FILE")
ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }
log() { echo "$(ts) $*" | tee -a "$LOG"; }

set_state() {  # $1=id $2=stopped|running
  curl -s --request PUT --url "$API/$1/" \
    --header "Authorization: Bearer $KEY" --header "Content-Type: application/json" \
    --data "{\"state\":\"$2\"}" >/dev/null && log "instance $1 -> $2"
}

case "$ACTION" in
  stop)
    set_state "$QWEN_ID" stopped
    set_state "$REDTEAM_ID" stopped
    log "=== пауза выполнена (GPU остановлены; ABOP на ночь уйдёт на RouteAI по каскаду) ==="
    ;;
  start)
    set_state "$QWEN_ID" running
    set_state "$REDTEAM_ID" running
    # ждём Qwen: running + отдаёт порт (до ~5 мин)
    NEWURL=""
    for i in $(seq 1 40); do
      sleep 15
      J=$(curl -s "$API/" --header "Authorization: Bearer $KEY")
      read -r ST IP PORT <<EOF
$(printf '%s' "$J" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for i in d.get('instances',[]):
    if i.get('id')==$QWEN_ID:
        p=(i.get('ports') or {}).get('8000/tcp') or [{}]
        print(i.get('actual_status'), i.get('public_ipaddr') or '', (p[0].get('HostPort') if p else '') or '')
")
EOF
      log "ожидание Qwen: status=$ST ip=$IP port=$PORT (try $i)"
      if [ "$ST" = "running" ] && [ -n "$IP" ] && [ -n "$PORT" ]; then
        NEWURL="http://$IP:$PORT/v1"; break
      fi
    done
    [ -n "$NEWURL" ] || { log "Qwen не поднялся за отведённое время — ABOP остаётся на RouteAI (каскад)"; exit 0; }
    # обновить LOCAL_LLM_BASE_URL, только если изменился
    CUR=$(grep -E "^LOCAL_LLM_BASE_URL=" "$ENV_FILE" | head -1 | cut -d= -f2- || true)
    if [ "$CUR" != "$NEWURL" ]; then
      cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
      if grep -q "^LOCAL_LLM_BASE_URL=" "$ENV_FILE"; then
        sed -i "s|^LOCAL_LLM_BASE_URL=.*|LOCAL_LLM_BASE_URL=$NEWURL|" "$ENV_FILE"
      else
        echo "LOCAL_LLM_BASE_URL=$NEWURL" >> "$ENV_FILE"
      fi
      log "LOCAL_LLM_BASE_URL: $CUR -> $NEWURL; рестарт abop-webapi"
      docker rm -f abop-webapi >/dev/null 2>&1 || true
      docker run -d --name abop-webapi --network host --restart unless-stopped \
        --env-file "$ENV_FILE" -v abop_ape:/root/.ape abop-webapi >/dev/null
    else
      log "порт Qwen не изменился ($NEWURL) — рестарт не нужен"
    fi
    # прогреть vLLM (модель грузится ~1-2 мин)
    for i in $(seq 1 8); do
      curl -sf -m 8 "$NEWURL/models" >/dev/null 2>&1 && { log "vLLM готов: $NEWURL"; break; }
      sleep 15
    done
    log "=== старт выполнен ==="
    ;;
  *)
    echo "usage: $0 stop|start"; exit 1;;
esac
