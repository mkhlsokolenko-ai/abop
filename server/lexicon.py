"""Словарь лексем агента (2026-09-28): «первичная засевка слов» из описаний его навыков.

Каждый новый агент приносит свои слова: заголовок и описание каждого навыка, инструкция шаблона
извлечения, тело SKILL.md, роль, семья, каналы доставки. Из них собирается взвешенный словарь основ
(`lexicon`), по которому детерминированный матчер сопоставляет этапы запроса с агентами — без
эмбеддингов и без модели. Словарь пересчитывается при сохранении агента и лежит в БД, поэтому
подстановка обновляется сама: появился агент — появились его слова.

Веса: чем ближе слово к «визитке» агента, тем выше (имя/роль/семья → 3, заголовок и описание навыка → 2,
инструкция шаблона → 2, тело навыка → 1, канал доставки → 2).
"""
from __future__ import annotations

import hashlib
import json

from . import nlu
from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_lexicon (
    agent_id   TEXT PRIMARY KEY,
    terms      JSONB NOT NULL DEFAULT '{}'::jsonb,
    doc_hash   TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    manual     JSONB NOT NULL DEFAULT '{}'::jsonb
);
ALTER TABLE agent_lexicon ADD COLUMN IF NOT EXISTS manual JSONB NOT NULL DEFAULT '{}'::jsonb;
"""
MANUAL_WEIGHT = 4.0     # слово, добавленное человеком, весит больше засеянного из навыка
MANUAL_BAN = -1.0       # «минус-слово»: агент не должен подбираться по нему

_MEM: dict[str, dict] = {}
_BODY_CHARS = 1500     # тела навыков режем: нужен словарь, а не полный текст
_MAX_TERMS = 400


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


def build(agent: dict, *, skills: dict | None = None, body_of=None, templates: dict | None = None) -> dict:
    """Словарь {лексема: вес} агента. `skills` — карточки навыков {sid: (title, desc, ...)},
    `body_of(sid)` — тело SKILL.md, `templates` — шаблоны извлечения {sid: {...}}."""
    weighted: dict[str, float] = {}

    def add(text: str, w: float) -> None:
        for t in nlu.tokens(text or ""):
            if len(t) < 2:
                continue
            weighted[t] = weighted.get(t, 0.0) + w

    add(" ".join(str(agent.get(k) or "") for k in ("name", "role", "family")), 3.0)
    nodes = (agent.get("graph") or {}).get("nodes") or []
    for n in nodes:
        sid = n.get("skill")
        if not sid:
            ch = (n.get("out") or {}).get("channel")
            if ch:
                add(str(ch), 2.0)
            continue
        add(sid.replace("-", " "), 2.0)
        card = (skills or {}).get(sid)
        if card:
            add(" ".join(str(x) for x in (card if isinstance(card, (list, tuple)) else [card])[:2]), 2.0)
        tpl = (templates or {}).get(sid) or {}
        add(str(tpl.get("name") or ""), 2.0)
        add(str(tpl.get("instruction") or "")[:_BODY_CHARS], 2.0)
        if body_of:
            try:
                add(str(body_of(sid) or "")[:_BODY_CHARS], 1.0)
            except Exception:  # noqa: BLE001 — тело навыка опционально
                pass
    if len(weighted) > _MAX_TERMS:
        weighted = dict(sorted(weighted.items(), key=lambda kv: -kv[1])[:_MAX_TERMS])
    return weighted


def doc_hash(agent: dict) -> str:
    """Отпечаток «визитки» агента — чтобы не пересчитывать словарь без изменений."""
    nodes = [(n.get("skill") or "", ((n.get("out") or {}).get("channel") or "")) for n in ((agent.get("graph") or {}).get("nodes") or [])]
    raw = json.dumps([agent.get("name"), agent.get("role"), agent.get("family"), sorted(nodes)], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


async def get(agent_id: str) -> dict:
    """Итоговый словарь: засеянный из навыков + ручные правки оператора (минус-слова убираются)."""
    seeded, manual = await parts(agent_id)
    return merge(seeded, manual)


async def parts(agent_id: str) -> tuple[dict, dict]:
    """(из навыков, ручные) — раздельно, чтобы UI показывал происхождение слова."""
    if not _has_pg():
        row = _MEM.get(agent_id) or {}
        return dict(row.get("terms") or {}), dict(row.get("manual") or {})
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT terms, manual FROM agent_lexicon WHERE agent_id=%s", (agent_id,))
        r = await cur.fetchone()
    return (dict(r[0] or {}), dict(r[1] or {})) if r else ({}, {})


def merge(seeded: dict, manual: dict) -> dict:
    """Ручные слова перекрывают засеянные; вес MANUAL_BAN убирает слово из подбора совсем."""
    out = dict(seeded or {})
    for t, w in (manual or {}).items():
        if float(w) <= 0:
            out.pop(t, None)
        else:
            out[t] = float(w)
    return out


async def set_manual(agent_id: str, manual: dict) -> dict:
    """Сохранить ручные термины (UI). Возвращает итоговый словарь."""
    if not _has_pg():
        row = _MEM.setdefault(agent_id, {"terms": {}, "manual": {}})
        row["manual"] = dict(manual or {})
        return merge(row.get("terms") or {}, row["manual"])
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO agent_lexicon (agent_id, terms, manual, updated_at) VALUES (%s,'{}'::jsonb,%s,now()) "
            "ON CONFLICT (agent_id) DO UPDATE SET manual=EXCLUDED.manual, updated_at=now()",
            (agent_id, json.dumps(manual or {}, ensure_ascii=False)))
    seeded, man = await parts(agent_id)
    return merge(seeded, man)


async def all_terms() -> dict[str, dict]:
    """{agent_id: {лексема: вес}} — весь словарь подстановки (навыки + ручные правки оператора)."""
    if not _has_pg():
        return {k: merge(v.get("terms") or {}, v.get("manual") or {}) for k, v in _MEM.items()}
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT agent_id, terms, manual FROM agent_lexicon")
        rows = await cur.fetchall()
    return {r[0]: merge(dict(r[1] or {}), dict(r[2] or {})) for r in rows}


async def save(agent_id: str, terms: dict, dh: str = "") -> None:
    if not _has_pg():
        row = _MEM.setdefault(agent_id, {"terms": {}, "manual": {}})
        row.update({"terms": terms, "doc_hash": dh})
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO agent_lexicon (agent_id, terms, doc_hash, updated_at) VALUES (%s,%s,%s,now()) "
            "ON CONFLICT (agent_id) DO UPDATE SET terms=EXCLUDED.terms, doc_hash=EXCLUDED.doc_hash, updated_at=now()",
            (agent_id, json.dumps(terms, ensure_ascii=False), dh))


async def refresh(agent: dict, **kw) -> dict:
    """Пересчитать словарь агента, если его визитка изменилась. Возвращает словарь."""
    aid = agent.get("id") or ""
    if not aid:
        return {}
    dh = doc_hash(agent)
    if not _has_pg():
        cur = _MEM.get(aid)
        if cur and cur.get("doc_hash") == dh:
            return dict(cur.get("terms") or {})
    else:
        from .db import _conn
        async with _conn() as conn:
            c = await conn.execute("SELECT doc_hash, terms FROM agent_lexicon WHERE agent_id=%s", (aid,))
            r = await c.fetchone()
        if r and r[0] == dh:
            return dict(r[1] or {})
    terms = build(agent, **kw)
    await save(aid, terms, dh)
    return terms


def score(stage_terms: set[str], lex: dict) -> float:
    """Совпадение этапа со словарём агента: сумма весов найденных лексем, нормированная на размер этапа.
    0 — ничего общего, ~1 и выше — уверенное совпадение по нескольким словам визитки."""
    if not stage_terms or not lex:
        return 0.0
    hit = sum(min(3.0, float(lex.get(t) or 0.0)) for t in stage_terms)
    return hit / (len(stage_terms) ** 0.5 * 3.0)
