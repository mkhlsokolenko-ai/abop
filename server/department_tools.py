# -*- coding: utf-8 -*-
"""Тулы отделов: реестр как данные, отдел-скоуп и эфемерное исполнение (решение владельца
14.11.2026 — «эфемерные докер-тулы, вызываемые агентами, ограниченные отделами и не имеющие
общего доступа»; docs/OTDELY_AGENTOV.md).

Тул — запись сущности `тул` в Data Plane (данные вертикали, тот же паттерн, что `rank_weight`):

    {"id": "pdf-разбор", "название": "Разбор PDF", "отдел": "audit" | "общий",
     "тип": "docker", "образ": "registry:5000/abop/pdf-inspector:1.2",
     "команда": ["python", "inspect.py"], "сеть": false,
     "таймаут": 60, "память": "256m", "цпу": "0.5",
     "описание": "классифицирует PDF и вынимает текстовый слой", "автор": "методолог"}

Правило доступа одно и оно не обсуждается: тул виден агенту, если он ОБЩИЙ или принадлежит
ЕГО отделу. Тул чужого отдела не попадает в подсказку и не исполняется — конверт отдела
не расширяется изнутри: нет тулa — нет действия.

Эфемерность — суть решения: docker-тул поднимается НА ВРЕМЯ ВЫЗОВА и умирает после него
(`docker run --rm`). Аргументы уходят JSON-ом в stdin (не в argv/env: их видно в ps и
/proc), результат — JSON в stdout. Сеть по умолчанию выключена; `сеть: true` в реестре —
осознанное исключение, которое видно при разборе. Секретов тул не получает вовсе: всё, что
ему нужно, приходит аргументами вызова от навыка, а навык берёт данные из Data Plane.
"""
from __future__ import annotations

import json
import subprocess

from .config import settings

СУЩНОСТЬ = "тул"


def записи() -> list[dict]:
    """Весь реестр тулов установки. Пусто — тулов нет, и это нормально: вертикаль могла
    не объявлять ни одного; локальные инструменты ядра живут отдельно (AGENT_TOOLS)."""
    try:
        from . import dataplane_records as dr
        return [r for r in (dr.query(СУЩНОСТЬ, None, None, 200) or []) if isinstance(r, dict)]
    except Exception:  # noqa: BLE001 — нет сущности это нормально
        return []


def тулы_для(семья: str) -> list[dict]:
    """Тулы, видимые агенту семьи: общие + своего отдела. Чужие не проходят фильтр вовсе —
    они не «запрещены», они НЕ СУЩЕСТВУЮТ для этого агента."""
    return [r for r in записи()
            if str(r.get("отдел") or "общий") in ("общий", семья) and str(r.get("id") or "").strip()]


def описания_для_промпта(семья: str) -> str:
    """Блок описаний тулов отдела для подсказки модели — только свои и общие."""
    строки = []
    for r in тулы_для(семья):
        строки.append(f"  - {r.get('id')}: {str(r.get('описание') or r.get('название') or '')[:160]}")
    return "\n".join(строки)


def найти(имя: str, семья: str) -> dict | None:
    """Тул по имени С ПРОВЕРКОЙ ОТДЕЛА. None = для этого агента тулa нет, что бы там ни
    было в реестре: чужой тул и несуществующий тул для агента неразличимы — и это намеренно."""
    for r in тулы_для(семья):
        if str(r.get("id") or "") == str(имя or "").strip():
            return r
    return None


def вызвать_эфемерно(тул: dict, args: dict) -> dict:
    """Поднять тул на время вызова и убить после него.

    docker run --rm: контейнер не переживает ответа; аргументы — JSON в stdin; результат —
    JSON в stdout. Сеть выключена, память и цпу ограничены записью реестра, таймаут убивает
    зависший вызов. Отказ docker назван по-отдельности: это «среда не готова», а не «тул
    сломался» — путать их нельзя, чинят их разными людьми.
    """
    if bool(settings.docker_tools_disabled):
        raise RuntimeError("эфемерные docker-тулы выключены настройкой ABOP_DOCKER_TOOLS=0")
    cmd = ["docker", "run", "--rm", "--interactive",
           "--network", "none" if not тул.get("сеть") else "bridge",
           "--memory", str(тул.get("память") or "256m"),
           "--cpus", str(тул.get("цпу") or "0.5"),
           str(тул.get("образ") or "")]
    cmd += [str(c) for c in (тул.get("команда") or [])]
    таймаут = int(тул.get("таймаут") or 60)
    if not тул.get("образ"):
        raise RuntimeError("в записи тулa нет образа — реестр неполон")
    try:
        пр = subprocess.run(cmd, input=json.dumps(args or {}, ensure_ascii=False).encode("utf-8"),
                            capture_output=True, timeout=таймаут)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"тул не ответил за {таймаут} с — вызов убит вместе с контейнером")
    except FileNotFoundError:
        raise RuntimeError("docker недоступен в этой среде — тул подъять нельзя (это отказ среды, не тулa)")
    if пр.returncode != 0:
        хвост = (пр.stderr or b"").decode("utf-8", "replace").strip()[-400:]
        raise RuntimeError(f"тул завершился с кодом {пр.returncode}: {хвост or 'без текста ошибки'}")
    stdout = (пр.stdout or b"").decode("utf-8", "replace").strip()
    if not stdout:
        return {}
    try:
        out = json.loads(stdout)
        return out if isinstance(out, dict) else {"результат": out}
    except Exception:  # noqa: BLE001 — тул ответил не JSON: отдаём текст как есть, не выдумываем структуру
        return {"результат": stdout[:2000]}
