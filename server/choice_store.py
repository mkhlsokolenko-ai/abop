"""Исход подбора: что человек принял, что отменил, что поправил руками.

Подбор у нас объясним и воспроизводим, но он ничему не учится: один и тот же человек десятый раз
подряд меняет предложенный навык на свой, а на одиннадцатый получает то же предложение. Данные для
обучения уже есть — он нажимает «собрать и запустить», «не запускать», правит шаг карандашом, — но
они нигде не сходились.

Здесь они сходятся. Храним не «оценку», а ФАКТ решения: задача, что предложили, что человек взял,
чем кончилось. По этому журналу считается небольшая прибавка к подбору — ровно такая, чтобы решать
ничью в пользу того, что человек уже выбирал, и НЕ такая, чтобы перебить подбор по существу. Иначе
система быстро замкнётся на первых удачных выборах и перестанет видеть остальной каталог.

Прибавка персональная: это предпочтения конкретного человека, а не «правильный ответ» для всех.

plan_choices{id, user_sub, task, offered JSONB, taken JSONB, outcome, created_at}
outcome: accepted | edited | cancelled
"""
from __future__ import annotations

import json
import time

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_choices (
    id          BIGSERIAL PRIMARY KEY,
    user_sub    TEXT NOT NULL DEFAULT '',
    task        TEXT NOT NULL DEFAULT '',
    offered     JSONB NOT NULL DEFAULT '[]'::jsonb,
    taken       JSONB NOT NULL DEFAULT '[]'::jsonb,
    outcome     TEXT NOT NULL DEFAULT 'accepted',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS plan_choices_user_idx ON plan_choices (user_sub, created_at DESC);
"""

_MEM: list[dict] = []
# Сколько последних решений учитываем. Дальше — уже не «как человек работает сейчас», а археология.
WINDOW = 200
# Потолок прибавки за прошлый выбор. Держится НИЖЕ порога уверенного подбора (0.30) намеренно:
# предпочтение решает ничью, но не назначает исполнителя вместо подбора по существу.
PREFER_CAP = 0.12
# Отмена весит больше принятия: «не то» — более сильный сигнал, чем «подошло», потому что принимают
# часто и по инерции, а отменяют осознанно.
W_ACCEPTED, W_EDITED, W_CANCELLED = 1.0, 1.3, -1.6


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        for chunk in SCHEMA.split(";"):
            stmt = chunk.strip()
            if stmt:
                await conn.execute(stmt)


async def record(user_sub: str, task: str, offered: list, taken: list, outcome: str) -> None:
    """Записать исход. Сбой записи не должен мешать работе: это наблюдение, а не часть прогона."""
    row = {"user_sub": str(user_sub or ""), "task": str(task or "")[:600],
           "offered": [str(x) for x in (offered or [])], "taken": [str(x) for x in (taken or [])],
           "outcome": str(outcome or "accepted"), "at": time.time()}
    if not _has_pg():
        _MEM.append(row)
        del _MEM[:-WINDOW]
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO plan_choices (user_sub, task, offered, taken, outcome) VALUES (%s,%s,%s,%s,%s)",
            (row["user_sub"], row["task"], json.dumps(row["offered"], ensure_ascii=False),
             json.dumps(row["taken"], ensure_ascii=False), row["outcome"]))


async def history(user_sub: str, limit: int = WINDOW) -> list[dict]:
    if not _has_pg():
        return [r for r in _MEM if r["user_sub"] == str(user_sub or "")][-limit:]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT task, offered, taken, outcome FROM plan_choices WHERE user_sub=%s "
            "ORDER BY id DESC LIMIT %s", (str(user_sub or ""), int(limit)))
        rows = await cur.fetchall()
    out = []
    for task, offered, taken, outcome in rows:
        out.append({"task": task, "offered": list(offered or []), "taken": list(taken or []),
                    "outcome": outcome})
    return out


def _score(rows: list[dict]) -> dict[str, float]:
    """Вес навыка по прошлым решениям: взятое в плюс, отменённое в минус, свежее весомее старого."""
    acc: dict[str, float] = {}
    n = len(rows)
    for i, r in enumerate(rows):
        # Свежесть: последнее решение весит вдвое больше, чем решение на краю окна. Строки идут от
        # старых к новым, поэтому растёт именно хвост списка.
        fresh = 0.5 + 0.5 * ((i + 1) / n if n else 1)
        w = {"accepted": W_ACCEPTED, "edited": W_EDITED, "cancelled": W_CANCELLED}.get(r.get("outcome"), 0.0)
        for sid in (r.get("taken") or []):
            acc[sid] = acc.get(sid, 0.0) + w * fresh
        if r.get("outcome") == "cancelled":
            continue
        # Навык предложили, а человек его НЕ взял — это тоже решение, но слабее отмены всей карточки.
        for sid in set(r.get("offered") or []) - set(r.get("taken") or []):
            acc[sid] = acc.get(sid, 0.0) - 0.4 * fresh
    return acc


async def prefer(user_sub: str, limit: int = WINDOW) -> dict[str, float]:
    """Прибавка к подбору по прошлым решениям этого человека: {навык: 0..PREFER_CAP}.

    Нормируем по максимуму, а не по сумме: один часто выбираемый навык не должен задавить остальные
    до нуля — иначе подбор замкнётся на первой же привычке.
    """
    rows = list(reversed(await history(user_sub, limit)))
    if not rows:
        return {}
    raw = _score(rows)
    top = max((v for v in raw.values() if v > 0), default=0.0)
    if top <= 0:
        return {}
    return {sid: round(PREFER_CAP * (v / top), 4) for sid, v in raw.items() if v > 0}
