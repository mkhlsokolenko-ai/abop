#!/usr/bin/env bash
# Ночная пауза Vast-инстансов (экономия GPU-биллинга; storage остаётся). Запуск по cron на server-1.
#   vast_schedule.sh stop   → останавливает инстансы (GPU не капает; диск/модель сохраняются)
#   vast_schedule.sh start  → запускает; ждёт running; переподхватывает НОВЫЙ порт Qwen → .env → рестарт ABOP
# Инстанс: ape-qwen30b (ABOP LLM). Ключ в /root/.vast_api_key (chmod 600).
# Порт Qwen может смениться после start — скрипт переподхватывает его сам.
# 09.11.2026: red-team-инстанс уничтожен (см. ниже), из расписания убран.
set -eu

ACTION="${1:-}"
KEY_FILE=/root/.vast_api_key
API=https://console.vast.ai/api/v1/instances       # листинг (GET) — v1
API_SET=https://console.vast.ai/api/v0/instances   # смена состояния (PUT state) — v0 (v1 отдаёт Not found)
QWEN_ID=59895017          # ape-qwen30b-int4-48-v5 (A6000, Virginia) — на него завязан ABOP LOCAL_LLM_BASE_URL
# История id: 51396167 (L40S, выведен 05.10) → 54301569 (A6000, потерян 08.11: Vast отдал
# GPU остановленного инстанса другому арендатору) → 59715501 (A6000, Virginia, уничтожен
# 09.11) → 59892319 (Delaware) и 59894025 (Virginia) — оба сняты в тот же день, не поднялись
# → 59895017 (A6000, Virginia, $0.457/ч, cuda 13.3, driver 610.57.04).
#
# РАБОЧИЙ РЕЦЕПТ создания (09.11.2026), два урока, оба стоили денег и времени.
#
# 1) НЕ передавать --quantization. Репозиторий cyankiwi/Qwen3-30B-A3B-Instruct-2507-AWQ-4bit
#    объявляет метод compressed-tensors, а прежний рецепт передавал awq_marlin. vLLM падает
#    на валидации конфига МГНОВЕННО: ValidationError «Quantization method specified in the
#    model config (compressed-tensors) does not match the quantization method specified in
#    the `quantization` argument». Без флага vLLM читает метод из конфигурации модели сам.
#
# 2) Вывод vllm НЕ перенаправлять в файл: было `> /var/log/vllm.log 2>&1`, и журнал Vast
#    обрывался на строке запуска — ошибка выше была не видна ВООБЩЕ, а инстанс при этом
#    числился running. Снаружи это выглядело как «молча не качает веса», и ушло два
#    пересоздания на ложную версию про драйвер. Правильно `2>&1 | tee -a /var/log/vllm.log`:
#    тогда ошибка видна в журнале через API request_logs.
#
# onstart:
#   set -eux
#   nvidia-smi || true
#   vllm serve cyankiwi/Qwen3-30B-A3B-Instruct-2507-AWQ-4bit --host 0.0.0.0 --port 8000 #     --served-model-name qwen3-30b-a3b --max-model-len 32768 --gpu-memory-utilization 0.92 #     2>&1 | tee -a /var/log/vllm.log
# image vllm/vllm-openai:latest, runtype ssh, disk 60, env: -p 8000:8000, CONTAINER_RUNTIME=runc
# Машину брать с cuda_max_good >= 13.0: образ latest поднимает требования к драйверу.
# ID ОБЯЗАН совпадать с /opt/redteam/ops/vast_sched.py: там он для планировщика, здесь для
# ночной паузы. Разошлись — и скрипт будет дёргать пустоту, молча (так и было с 05.10).
#
# redteam-инстанс из расписания УБРАН. Повод: REDTEAM_ID здесь стоял 49036119, а этого
# инстанса в аккаунте не было с 06.10 — скрипт месяцами дёргал пустоту, ровно как
# предупреждает абзац выше. Настоящий redteam-prod-v2 (54441202, 110 ГБ) уничтожен
# 09.11.2026 по решению владельца: оба инстанса стояли остановленными и капали только
# за диск, $42.58/мес за простой. Главный вывод на будущее: ОСТАНОВЛЕННЫЙ инстанс Vast
# продолжает брать деньги за диск, и ночная пауза этого не лечит.
ENV_FILE=/opt/abop/.env
LOG=/var/log/vast_schedule.log

[ -f "$KEY_FILE" ] || { echo "нет $KEY_FILE"; exit 1; }
KEY=$(cat "$KEY_FILE")
ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }
log() { echo "$(ts) $*" | tee -a "$LOG"; }

# Дата активации: до неё расписание НЕ срабатывает (файл /root/.vast_start_date, YYYY-MM-DD по МСК).
# Позволяет держать cron установленным, но включить его с нужного дня. Нет файла → активно всегда.
ACT_FILE=/root/.vast_start_date
if [ -f "$ACT_FILE" ]; then
  ACT=$(cat "$ACT_FILE" | tr -d '[:space:]')
  TODAY=$(TZ=Europe/Moscow date "+%Y-%m-%d")
  if [ "$TODAY" \< "$ACT" ]; then
    log "расписание отложено до $ACT (сегодня $TODAY) — действие '$ACTION' пропущено"
    exit 0
  fi
fi

set_state() {  # $1=id $2=stopped|running
  R=$(curl -s --request PUT --url "$API_SET/$1/" \
    --header "Authorization: Bearer $KEY" --header "Content-Type: application/json" \
    --data "{\"state\":\"$2\"}")
  log "instance $1 -> $2 : $R"
}

case "$ACTION" in
  stop)
    set_state "$QWEN_ID" stopped
    log "=== пауза выполнена (GPU остановлен; ABOP на ночь уйдёт на RouteAI по каскаду) ==="
    ;;
  start)
    set_state "$QWEN_ID" running
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
