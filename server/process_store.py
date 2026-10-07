"""Карта процессов: применённые редакции, посадки агентов и журнал правок. Postgres + mem.

Раньше карта и назначения лежали в `app_config` (admin_store) двумя JSON-полями: правка уходила
целиком, последняя запись побеждала молча, а слой AS-IS/TO-BE был лишь фильтром по полю `layer` —
план не мог стать фактом. ADR-042/043/045/046 разводят это на три сущности с разными жизненными
циклами:

* **применённая редакция** (`process_map`) — дерево процессов на момент применения. AS-IS читается
  отсюда и только читается;
* **посадка** (`process_seats`) — агент на ОДНОЙ операции, ссылка на `audit_id` без версии (версия
  растёт сама при добавлении триггера). Снятие — `until`, не DELETE: история участка остаётся;
* **редакция-черновик** (`process_revisions` + append-only `process_changes`) — предложение
  изменения. TO-BE = применённое дерево с наложенным журналом; `apply` переносит это в факт.

Синхронизация правок (ADR-046). Журнал дописывается под `FOR UPDATE`, поэтому число реплик API не
влияет на целостность. У каждой правки считаются три множества: что она **пишет**, что ей **нужно**,
что она **рушит**. Конфликт — пересечение «пишу»/«пишу», «нужно»/«рушит» или «рушит»/«нужно»; всё
остальное сливается, и клиенту возвращаются пропущенные им правки, чтобы догнать без перезагрузки.
Удаление узла рушит и его поддерево — иначе правка внутрь удалённого подпроцесса повисла бы в пустоте.

Без DSN стор работает в памяти (`asyncio.Lock` вместо `FOR UPDATE`) — тесты и CI идут без Postgres,
как и у остальных сторов.
"""
from __future__ import annotations

import asyncio
import copy
import datetime as _dt
import json
import uuid

from . import observability as obs
from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS process_map (
    area        TEXT NOT NULL,
    revision    INT  NOT NULL,
    tree        JSONB NOT NULL,
    note        TEXT,
    applied_by  TEXT,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (area, revision)
);
CREATE TABLE IF NOT EXISTS process_seats (
    id          TEXT PRIMARY KEY,
    area        TEXT NOT NULL,
    op_id       TEXT NOT NULL,
    audit_id    TEXT NOT NULL,
    pin_version INT,
    created_by  TEXT,
    since       TIMESTAMPTZ NOT NULL DEFAULT now(),
    until       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_seats_area ON process_seats (area) WHERE until IS NULL;
CREATE INDEX IF NOT EXISTS idx_seats_op ON process_seats (op_id) WHERE until IS NULL;
CREATE INDEX IF NOT EXISTS idx_seats_agent ON process_seats (audit_id) WHERE until IS NULL;
CREATE TABLE IF NOT EXISTS process_revisions (
    id          TEXT PRIMARY KEY,
    area        TEXT NOT NULL,
    base        INT  NOT NULL,
    status      TEXT NOT NULL DEFAULT 'черновик',
    version     INT  NOT NULL DEFAULT 0,
    author      TEXT,
    approved_by TEXT,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rev_open ON process_revisions (area)
    WHERE status IN ('черновик', 'на согласовании');
CREATE TABLE IF NOT EXISTS process_changes (
    rev_id  TEXT NOT NULL REFERENCES process_revisions(id) ON DELETE CASCADE,
    seq     INT  NOT NULL,
    kind    TEXT NOT NULL,
    target  TEXT NOT NULL,
    payload JSONB NOT NULL,
    author  TEXT NOT NULL,
    at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rev_id, seq)
);
CREATE TABLE IF NOT EXISTS process_presence (
    area  TEXT NOT NULL,
    actor TEXT NOT NULL,
    at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (area, actor)
);
"""

КАРТА = "*"      # карта организации одна и читается всеми; по областям живут только ЧЕРНОВИКИ
ОТКРЫТЫЕ = ("черновик", "на согласовании")
ВИДЫ = ("add_node", "drop_node", "rename_node", "move_node", "add_seat", "drop_seat")
ВМЕСТИЛИЩА = ("org", "process", "subprocess")

_MEM: dict = {"map": {}, "seats": [], "rev": {}, "changes": {}, "presence": {}}
_ЗАМКИ: dict = {}
_ПОДПИСЧИКИ: dict = {}      # area -> set[asyncio.Queue]


class Отказ(Exception):
    """Правка невозможна по состоянию карты: цель не существует, не лист, уже занята."""


class НуженПодписант(Отказ):
    """Префлайт потребовал человека: редакция идёт на согласование, а не применяется молча."""

    def __init__(self, сообщение: str, *, причины: list[str]):
        super().__init__(сообщение)
        self.причины = причины


class Конфликт(Exception):
    """Кто-то изменил то же самое, пока вы правили. Несёт номер версии и чужие правки."""

    def __init__(self, сообщение: str, *, версия: int, соперники: list[dict]):
        super().__init__(сообщение)
        self.версия = версия
        self.соперники = соперники


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _замок(area: str) -> asyncio.Lock:
    if area not in _ЗАМКИ:
        _ЗАМКИ[area] = asyncio.Lock()
    return _ЗАМКИ[area]


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def _сброс_для_тестов() -> None:
    """Чистое состояние памяти: вызывается только из тестов."""
    _MEM.update({"map": {}, "seats": [], "rev": {}, "changes": {}, "presence": {}})
    _ЗАМКИ.clear()
    _ПОДПИСЧИКИ.clear()


# ── чистая логика: дерево, зоны, наложение ────────────────────────────────────────────────────────

def найти(узел: dict, nid: str) -> dict | None:
    if not isinstance(узел, dict):
        return None
    if узел.get("id") == nid:
        return узел
    for c in узел.get("children") or []:
        это = найти(c, nid)
        if это:
            return это
    return None


def родитель(узел: dict, nid: str) -> dict | None:
    for c in узел.get("children") or []:
        if c.get("id") == nid:
            return узел
        это = родитель(c, nid)
        if это:
            return это
    return None


def поддерево(узел: dict) -> set[str]:
    """Идентификаторы узла и всего, что под ним — зона влияния удаления."""
    из = {узел.get("id")}
    for c in узел.get("children") or []:
        из |= поддерево(c)
    return {i for i in из if i}


def листья(узел: dict) -> set[str]:
    дети = узел.get("children") or []
    if not дети:
        return {узел.get("id")}
    из: set[str] = set()
    for c in дети:
        из |= листья(c)
    return из


def area_of(дерево: dict, nid: str) -> str:
    """Область узла: объявляется на процессе, наследуется вниз. Корень и необъявленное — «*»
    (правит только admin): цепочку ценности нельзя переставлять за чужой отдел (ADR-045)."""
    def спуск(узел: dict, текущая: str) -> str | None:
        своя = str(узел.get("area") or "").strip() or текущая
        if узел.get("id") == nid:
            return своя
        for c in узел.get("children") or []:
            это = спуск(c, своя)
            if это:
                return это
        return None
    return спуск(дерево, "*") or "*"


def _ключ_посадки(op_id: str, audit_id: str) -> str:
    return "seat:" + op_id + ":" + audit_id


def зоны(правка: dict, дерево: dict) -> tuple[set, set, set]:
    """(пишет, нужно, рушит) — три множества, по которым считается конфликт (ADR-046)."""
    вид, цель = правка["kind"], правка["target"]
    груз = правка.get("payload") or {}
    if вид == "add_node":
        return {груз.get("id")}, {цель}, set()
    if вид == "drop_node":
        узел = найти(дерево, цель)
        рушит = поддерево(узел) if узел else {цель}
        # посадки внутри удаляемого поддерева тоже рушатся
        рушит |= {_ключ_посадки(o, "*") for o in рушит}
        return {цель}, {цель}, рушит
    if вид == "rename_node":
        return {цель}, {цель}, set()
    if вид == "move_node":
        отец = родитель(дерево, цель)
        return {цель, (отец or {}).get("id")}, {цель}, set()
    if вид in ("add_seat", "drop_seat"):
        ключ = _ключ_посадки(цель, str(груз.get("audit_id") or ""))
        рушит = {ключ} if вид == "drop_seat" else set()
        return {ключ}, {цель}, рушит
    return {цель}, {цель}, set()


def _рушит_посадку(рушит: set, ключ: str) -> bool:
    """Удаление узла рушит ЛЮБУЮ посадку внутри поддерева — там звёздочка вместо агента."""
    if ключ in рушит:
        return True
    части = ключ.split(":")
    return len(части) == 3 and _ключ_посадки(части[1], "*") in рушит


def конфликтует(моя: dict, чужая: dict, дерево: dict) -> bool:
    мп, мн, мр = зоны(моя, дерево)
    чп, чн, чр = зоны(чужая, дерево)
    if (мп & чп) - {None}:
        return True
    for ключ in мн:
        if _рушит_посадку(чр, ключ) or ключ in чр:
            return True
    for ключ in чн:
        if _рушит_посадку(мр, ключ) or ключ in мр:
            return True
    return False


def проверить(правка: dict, дерево: dict, посадки: list[dict]) -> str | None:
    """Причинная проверка: можно ли применить правку к ЭТОМУ состоянию. Возвращает причину отказа."""
    вид, цель = правка.get("kind"), str(правка.get("target") or "")
    груз = правка.get("payload") or {}
    if вид not in ВИДЫ:
        return f"неизвестный вид правки «{вид}»"
    узел = найти(дерево, цель)
    if вид == "add_node":
        if not узел:
            return f"родитель «{цель}» не найден — возможно, его удалили"
        if узел.get("kind") not in ВМЕСТИЛИЩА:
            return f"в «{узел.get('label') or цель}» нельзя вкладывать блоки"
        if not str(груз.get("label") or "").strip():
            return "у нового блока нет названия"
        return None
    if not узел:
        return f"«{цель}» не найден в карте — возможно, его удалили"
    if вид == "drop_node":
        return "корень карты удалить нельзя" if not родитель(дерево, цель) else None
    if вид == "rename_node":
        return None if str(груз.get("label") or "").strip() else "пустое название"
    if вид == "move_node":
        return None if родитель(дерево, цель) else "корень карты не двигается"
    audit_id = str(груз.get("audit_id") or "").strip()
    if not audit_id:
        return "не указан агент"
    if вид == "add_seat":
        if узел.get("children"):
            return (f"«{узел.get('label') or цель}» — не операция, а {узел.get('kind')}: "
                    "агент сажается на конечный шаг процесса")
        # Работу делает задача. Событие — триггер процесса, шлюз — условие, ожидание — таймер,
        # HITL — человек: агента туда сажать нельзя. Интерфейс это знал (кнопка «＋ агент» рисуется
        # только на задачах), а сервер пускал любой лист — и агент садился на событие.
        if узел.get("kind") != "task":
            чем = {"event": "событие — это триггер процесса", "gateway": "шлюз — это условие",
                   "wait": "ожидание — это таймер", "hitl": "здесь решает человек"}.get(
                       узел.get("kind"), "это не задача")
            return f"на «{узел.get('label') or цель}» агента не сажают: {чем}"
        уже = [s for s in посадки if s["op_id"] == цель and s["audit_id"] == audit_id
               and s.get("state") != "dropping"]
        return "этот агент уже на участке" if уже else None
    есть = [s for s in посадки if s["op_id"] == цель and s["audit_id"] == audit_id
            and s.get("state") != "dropping"]
    return None if есть else "на этом участке такого агента нет"


def наложить(правка: dict, дерево: dict, посадки: list[dict]) -> None:
    """Применить правку к рабочей копии состояния (на месте)."""
    вид, цель = правка["kind"], правка["target"]
    груз = правка.get("payload") or {}
    if вид == "add_node":
        отец = найти(дерево, цель)
        отец.setdefault("children", []).append({
            "id": груз["id"], "kind": груз.get("kind") or "task",
            "label": груз.get("label") or "Новый блок",
        })
    elif вид == "drop_node":
        отец = родитель(дерево, цель)
        if отец:
            отец["children"] = [c for c in отец.get("children") or [] if c.get("id") != цель]
    elif вид == "rename_node":
        найти(дерево, цель)["label"] = груз["label"]
    elif вид == "move_node":
        отец = родитель(дерево, цель)
        дети = отец.get("children") or []
        i = next((k for k, c in enumerate(дети) if c.get("id") == цель), -1)
        j = i + (1 if int(груз.get("dir") or 1) > 0 else -1)
        if 0 <= i < len(дети) and 0 <= j < len(дети):
            дети[i], дети[j] = дети[j], дети[i]
    elif вид == "add_seat":
        посадки.append({"op_id": цель, "audit_id": груз["audit_id"],
                        "pin_version": груз.get("pin_version"), "state": "planned",
                        "created_by": правка.get("author")})
    elif вид == "drop_seat":
        for s in list(посадки):
            if s["op_id"] == цель and s["audit_id"] == груз["audit_id"]:
                if s.get("state") == "planned":
                    посадки.remove(s)
                else:
                    s["state"] = "dropping"
                break


def наложение(дерево: dict, посадки: list[dict], журнал: list[dict]) -> tuple[dict, list[dict]]:
    """TO-BE: применённое дерево с наложенным журналом. Посадки на исчезнувших узлах отбрасываются."""
    д = copy.deepcopy(дерево)
    п = [dict(s) for s in посадки]
    for ch in журнал:
        try:
            наложить(ch, д, п)
        except Exception:  # noqa: BLE001 — журнал мог пережить изменение структуры; пропускаем битое
            obs.log_event("warn", "process.overlay_skip", kind=ch.get("kind"), target=ch.get("target"))
    живые = поддерево(д)
    return д, [s for s in п if s["op_id"] in живые]


def liveness(*, seats: list[dict], runs: list[dict], retired: set,
             window_days: int = 7, now: _dt.datetime | None = None) -> dict:
    """Подтверждение AS-IS прогонами (ADR-044). Чистая функция: вход — посадки, прогоны и агенты из
    Лимба, выход — состояние каждого участка и список расхождений.

    Признак выводится, а не хранится — по образцу `_dep_status_ru`, где статус развёртывания тоже
    считается из статуса и триггеров, а не лежит в базе. Два источника истины — декларация (посадка
    есть) и факт (прогоны были) — остаются разными полями: расхождение между ними показывается, а не
    сглаживается.
    """
    сейчас = now or _now()
    порог = сейчас - _dt.timedelta(days=max(1, int(window_days)))

    def _когда(r: dict):
        try:
            d = _dt.datetime.fromisoformat(str(r.get("created_at") or "").replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
        except Exception:  # noqa: BLE001 — прогон без даты в окно не попадает
            return None

    по_операциям: dict = {}
    for s in seats:
        по_операциям.setdefault(s["op_id"], {"runs": 0, "ok": 0, "last_at": None,
                                             "audit_ids": [], "state": "тихо", "silent_days": None})
        по_операциям[s["op_id"]]["audit_ids"].append(s["audit_id"])

    посажены = {s["audit_id"] for s in seats}
    вне: dict = {}
    for r in runs:
        когда = _когда(r)
        audit_id = str(r.get("agent_id") or "").split(".v")[0]
        ops = [o for o in (r.get("op_ids") or []) if o in по_операциям]
        for op in ops:
            зап = по_операциям[op]
            if когда and когда >= порог:
                зап["runs"] += 1
                зап["ok"] += 1 if r.get("verdict_ok") else 0
            if когда and (зап["last_at"] is None or когда > зап["last_at"]):
                зап["last_at"] = когда
        # агент работает, но ни на одной операции не сидит — карта отстала от жизни
        if audit_id and audit_id not in посажены and когда and когда >= порог:
            вне.setdefault(audit_id, {"audit_id": audit_id, "runs": 0, "last_at": None})
            вне[audit_id]["runs"] += 1
            если = вне[audit_id]["last_at"]
            if если is None or когда > если:
                вне[audit_id]["last_at"] = когда

    for op, зап in по_операциям.items():
        if any(a in retired for a in зап["audit_ids"]):
            зап["state"] = "осиротел"
        elif зап["runs"] > 0:
            зап["state"] = "подтверждён"
        else:
            зап["state"] = "тихо"
        if зап["last_at"] is not None:
            зап["silent_days"] = max(0, (сейчас - зап["last_at"]).days)
            зап["last_at"] = зап["last_at"].isoformat()
    for v in вне.values():
        v["last_at"] = v["last_at"].isoformat() if v["last_at"] else None

    return {"window_days": int(window_days), "ops": по_операциям,
            "outside": sorted(вне.values(), key=lambda x: x["audit_id"])}


# ── чтение ────────────────────────────────────────────────────────────────────────────────────────

async def map_of(area: str) -> dict:
    """Применённая редакция области: дерево, номер, кто и когда применил."""
    if not _has_pg():
        строки = _MEM["map"].get(area) or []
        п = строки[-1] if строки else None
        return dict(п) if п else {"area": area, "revision": 0, "tree": None,
                                  "applied_by": None, "applied_at": None}
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT area, revision, tree, note, applied_by, applied_at FROM process_map "
            "WHERE area = %s ORDER BY revision DESC LIMIT 1", (area,))
        r = await cur.fetchone()
    if not r:
        return {"area": area, "revision": 0, "tree": None, "applied_by": None, "applied_at": None}
    return {"area": r[0], "revision": r[1], "tree": r[2], "note": r[3], "applied_by": r[4],
            "applied_at": r[5].isoformat() if r[5] else None}


async def seats_of(area: str | None = None, *, include_closed: bool = False) -> list[dict]:
    """Посадки. `area=None` — все: карта организации одна, и видно её целиком (ADR-045).
    По умолчанию только действующие (`until IS NULL`)."""
    if not _has_pg():
        из = [dict(s) for s in _MEM["seats"]
              if (area is None or s["area"] == area)
              and (include_closed or s.get("until") is None)]
        return [dict(s, state="actual") for s in из]
    from .db import _conn
    async with _conn() as conn:
        где = "" if include_closed else " AND until IS NULL"
        поле = "area = %s" if area is not None else "%s::text IS NULL"
        cur = await conn.execute(
            "SELECT id, area, op_id, audit_id, pin_version, created_by, since, until "
            f"FROM process_seats WHERE {поле}{где} ORDER BY since", (area,))
        строки = await cur.fetchall()
    return [{"id": r[0], "area": r[1], "op_id": r[2], "audit_id": r[3], "pin_version": r[4],
             "created_by": r[5], "since": r[6].isoformat() if r[6] else None,
             "until": r[7].isoformat() if r[7] else None, "state": "actual"} for r in строки]


async def seats_by_agent(audit_id: str) -> list[dict]:
    """Действующие посадки агента — по ним прогон узнаёт, какие операции он закрывает (ADR-044)."""
    if not _has_pg():
        return [dict(s) for s in _MEM["seats"] if s["audit_id"] == audit_id and s.get("until") is None]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id, area, op_id, audit_id, pin_version FROM process_seats "
            "WHERE audit_id = %s AND until IS NULL", (audit_id,))
        строки = await cur.fetchall()
    return [{"id": r[0], "area": r[1], "op_id": r[2], "audit_id": r[3], "pin_version": r[4]}
            for r in строки]


async def draft_of(area: str) -> dict | None:
    """Открытая редакция области вместе с журналом правок."""
    if not _has_pg():
        ред = _MEM["rev"].get(area)
        if not ред or ред["status"] not in ОТКРЫТЫЕ:
            return None
        return dict(ред, changes=[dict(c) for c in _MEM["changes"].get(ред["id"], [])])
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id, area, base, status, version, author, note, created_at, updated_at "
            "FROM process_revisions WHERE area = %s AND status = ANY(%s) LIMIT 1",
            (area, list(ОТКРЫТЫЕ)))
        r = await cur.fetchone()
        if not r:
            return None
        cur = await conn.execute(
            "SELECT seq, kind, target, payload, author, at FROM process_changes "
            "WHERE rev_id = %s ORDER BY seq", (r[0],))
        ch = await cur.fetchall()
    return {"id": r[0], "area": r[1], "base": r[2], "status": r[3], "version": r[4],
            "author": r[5], "note": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
            "updated_at": r[8].isoformat() if r[8] else None,
            "changes": [{"seq": c[0], "kind": c[1], "target": c[2], "payload": c[3],
                         "author": c[4], "at": c[5].isoformat() if c[5] else None} for c in ch]}


async def changes_since(area: str, since: int) -> list[dict]:
    """Разница по номеру: клиент берёт только то, чего не видел (ADR-046, часть 4)."""
    черновик = await draft_of(area)
    if not черновик:
        return []
    return [c for c in черновик["changes"] if int(c["seq"]) > int(since)]


async def projection(area: str, *, layer: str = "as-is") -> dict:
    """Срез карты для отрисовки. `as-is` — факт (только чтение), `to-be` — факт с черновиком."""
    применено = await map_of(КАРТА)
    дерево = применено.get("tree")
    посадки = await seats_of()
    черновик = await draft_of(area) if layer == "to-be" else None
    if layer == "to-be" and дерево:
        дерево, посадки = наложение(дерево, посадки, (черновик or {}).get("changes") or [])
    # Правки отдаём вместе с подписью цели из ПРИМЕНЁННОГО дерева: удалённый блок в наложении уже
    # не найти, а в разборе редакции человек должен прочитать «блок «Скоринг» удалён», а не «o213».
    правки = []
    for c in (черновик or {}).get("changes") or []:
        узел = найти(применено.get("tree") or {}, c.get("target"))
        правки.append(dict(c, target_label=(узел or {}).get("label") or c.get("target")))
    return {"area": area, "layer": layer, "revision": применено.get("revision") or 0,
            "version": (черновик or {}).get("version") or 0, "tree": дерево, "seats": посадки,
            "draft": ({k: v for k, v in черновик.items() if k != "changes"} if черновик else None),
            "changes": правки,
            "applied_by": применено.get("applied_by"), "applied_at": применено.get("applied_at")}


# ── запись ────────────────────────────────────────────────────────────────────────────────────────

async def save_map(area: str, tree: dict, *, editor: str, note: str = "") -> dict:
    """Новая ПРИМЕНЁННАЯ редакция структуры: правка схемы версионируется сразу, с автором и
    причиной (ADR-042, часть 3). Черновик при этом не создаётся."""
    текущее = await map_of(area)
    номер = int(текущее.get("revision") or 0) + 1
    запись = {"area": area, "revision": номер, "tree": tree, "note": note,
              "applied_by": editor, "applied_at": _now().isoformat()}
    if not _has_pg():
        _MEM["map"].setdefault(area, []).append(запись)
    else:
        from .db import _conn
        async with _conn() as conn:
            await conn.execute(
                "INSERT INTO process_map (area, revision, tree, note, applied_by) "
                "VALUES (%s,%s,%s,%s,%s)", (area, номер, json.dumps(tree, ensure_ascii=False), note, editor))
    await _оповестить(area, {"type": "map", "revision": номер, "author": editor})
    return запись


async def _открыть_черновик(area: str, автор: str, conn=None) -> dict:
    применено = await map_of(КАРТА)
    rid = "rev-" + uuid.uuid4().hex[:10]
    ред = {"id": rid, "area": area, "base": int(применено.get("revision") or 0),
           "status": "черновик", "version": 0, "author": автор, "note": "",
           "created_at": _now().isoformat(), "updated_at": _now().isoformat()}
    if conn is None:
        _MEM["rev"][area] = ред
        _MEM["changes"][rid] = []
    else:
        await conn.execute(
            "INSERT INTO process_revisions (id, area, base, status, version, author) "
            "VALUES (%s,%s,%s,'черновик',0,%s)", (rid, area, ред["base"], автор))
    return ред


def _нормализовать(правки: list[dict], автор: str) -> list[dict]:
    """Привести правки к одному виду и выдать идентификаторы новым узлам: зона add_node считается
    по НОВОМУ id, иначе двое, наполняющих один подпроцесс, конфликтовали бы друг с другом."""
    из = []
    for p in правки or []:
        вид = str((p or {}).get("kind") or "").strip()
        цель = str((p or {}).get("target") or "").strip()
        груз = dict((p or {}).get("payload") or {})
        if not вид or not цель:
            raise Отказ("у правки нет вида или цели")
        if вид == "add_node" and not str(груз.get("id") or "").strip():
            груз["id"] = "n" + uuid.uuid4().hex[:10]
        из.append({"kind": вид, "target": цель, "payload": груз, "author": автор})
    if not из:
        raise Отказ("нечего применять")
    return из


async def append(area: str, *, base_version: int, changes: list[dict], author: str) -> dict:
    """Дописать правки в журнал черновика. Сливает с чужими, если зоны не пересеклись; иначе
    поднимает Конфликт с указанием, кто опередил. Возвращает новый номер и пропущенные правки."""
    правки = _нормализовать(changes, author)
    if not _has_pg():
        async with _замок(area):
            return await _append_mem(area, int(base_version or 0), правки, author)
    from .db import _conn
    async with _conn() as conn:
        async with conn.transaction():
            return await _append_pg(conn, area, int(base_version or 0), правки, author)


async def _подготовить(area: str, base: int, правки: list[dict], журнал: list[dict],
                       версия: int, статус: str = "черновик") -> list[dict]:
    """Общая часть: конфликты против пропущенного, затем причинная проверка по состоянию.

    Порядок важен. Сначала конфликты: если кто-то уже тронул ту же цель, человеку полезнее узнать
    «@petrova сняла этого агента», чем «агент уже посажен» — второе правда, но не объясняет, почему.
    """
    if статус == "на согласовании":
        raise Отказ("редакция отправлена на согласование — дописывать в неё нельзя, "
                    "иначе подписант подпишет не то, что прочёл. Отзовите её или дождитесь решения")
    применено = await map_of(КАРТА)
    дерево_факт = применено.get("tree")
    if not дерево_факт:
        raise Отказ("карта процессов ещё не создана")
    посадки_факт = await seats_of()
    пропущено = [c for c in журнал if int(c["seq"]) > base]

    for моя in правки:
        for чужая in пропущено:
            if конфликтует(моя, чужая, дерево_факт):
                raise Конфликт(
                    "эту часть карты уже изменили, пока вы правили",
                    версия=версия,
                    соперники=[{"target": чужая["target"], "kind": чужая["kind"],
                                "author": чужая["author"], "seq": чужая["seq"]}])

    дерево, посадки = наложение(дерево_факт, посадки_факт, журнал)
    for моя in правки:
        причина = проверить(моя, дерево, посадки)
        if причина:
            raise Отказ(причина)
        наложить(моя, дерево, посадки)
    return пропущено


async def _append_mem(area: str, base: int, правки: list[dict], author: str) -> dict:
    ред = _MEM["rev"].get(area)
    if not ред or ред["status"] not in ОТКРЫТЫЕ:
        ред = await _открыть_черновик(area, author)
    журнал = _MEM["changes"].setdefault(ред["id"], [])
    пропущено = await _подготовить(area, base, правки, журнал, ред["version"], ред.get("status", "черновик"))
    for p in правки:
        ред["version"] += 1
        журнал.append(dict(p, seq=ред["version"], at=_now().isoformat()))
    ред["updated_at"] = _now().isoformat()
    await _оповестить(area, {"type": "change", "version": ред["version"], "author": author})
    return {"revision_id": ред["id"], "version": ред["version"], "missed": пропущено,
            "applied": правки}


async def _append_pg(conn, area: str, base: int, правки: list[dict], author: str) -> dict:
    cur = await conn.execute(
        "SELECT id, version, status FROM process_revisions WHERE area = %s AND status = ANY(%s) "
        "FOR UPDATE", (area, list(ОТКРЫТЫЕ)))
    r = await cur.fetchone()
    if not r:
        ред = await _открыть_черновик(area, author, conn=conn)
        rid, версия, статус = ред["id"], 0, "черновик"
    else:
        rid, версия, статус = r[0], int(r[1]), r[2]
    cur = await conn.execute(
        "SELECT seq, kind, target, payload, author FROM process_changes WHERE rev_id = %s ORDER BY seq",
        (rid,))
    журнал = [{"seq": c[0], "kind": c[1], "target": c[2], "payload": c[3], "author": c[4]}
              for c in await cur.fetchall()]
    пропущено = await _подготовить(area, base, правки, журнал, версия, статус)
    for p in правки:
        версия += 1
        await conn.execute(
            "INSERT INTO process_changes (rev_id, seq, kind, target, payload, author) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (rid, версия, p["kind"], p["target"], json.dumps(p["payload"], ensure_ascii=False), author))
    await conn.execute(
        "UPDATE process_revisions SET version = %s, updated_at = now() WHERE id = %s", (версия, rid))
    await _оповестить(area, {"type": "change", "version": версия, "author": author})
    return {"revision_id": rid, "version": версия, "missed": пропущено, "applied": правки}


async def drop_draft(area: str, *, actor: str) -> bool:
    """Отбросить черновик целиком: применённая карта не менялась, значит терять нечего."""
    if not _has_pg():
        ред = _MEM["rev"].get(area)
        if not ред or ред["status"] not in ОТКРЫТЫЕ:
            return False
        _MEM["changes"].pop(ред["id"], None)
        _MEM["rev"].pop(area, None)
    else:
        from .db import _conn
        async with _conn() as conn:
            cur = await conn.execute(
                "UPDATE process_revisions SET status = 'отклонена', approved_by = %s, updated_at = now() "
                "WHERE area = %s AND status = ANY(%s) RETURNING id", (actor, area, list(ОТКРЫТЫЕ)))
            if not await cur.fetchone():
                return False
    await _оповестить(area, {"type": "dropped", "author": actor})
    return True


async def submit(area: str, *, base_version: int, actor: str) -> dict:
    """Отправить редакцию на согласование. С этого момента она не правится: подписант должен
    подписать ровно то, что прочёл (ADR-042, часть 2)."""
    черновик = await draft_of(area)
    if not черновик:
        raise Отказ("нет открытой редакции")
    if черновик["status"] != "черновик":
        raise Отказ("редакция уже на согласовании")
    if int(черновик["version"]) != int(base_version):
        raise Конфликт("в редакцию дописали правки — перечитайте её перед отправкой",
                       версия=int(черновик["version"]), соперники=[])
    if not _has_pg():
        _MEM["rev"][area]["status"] = "на согласовании"
        _MEM["rev"][area]["updated_at"] = _now().isoformat()
    else:
        from .db import _conn
        async with _conn() as conn:
            await conn.execute("UPDATE process_revisions SET status = 'на согласовании', "
                               "updated_at = now() WHERE id = %s", (черновик["id"],))
    await _оповестить(area, {"type": "submitted", "author": actor})
    return {"revision_id": черновик["id"], "area": area, "status": "на согласовании"}


async def apply(area: str, *, base_version: int, actor: str, preflight=None,
                needs_approval=None, approved: bool = False) -> dict:
    """Применить редакцию: одна транзакция на дерево и посадки, либо ничего.

    `base_version` обязателен и строгий: применять редакцию, в которую после вашего просмотра
    дописали правки, нельзя — вы применяли бы непрочитанное (ADR-046, следствия).
    """
    черновик = await draft_of(area)
    if not черновик:
        raise Отказ("нет открытой редакции")
    if int(черновик["version"]) != int(base_version):
        raise Конфликт("в редакцию дописали правки — перечитайте её перед применением",
                       версия=int(черновик["version"]),
                       соперники=[{"target": c["target"], "kind": c["kind"], "author": c["author"],
                                   "seq": c["seq"]}
                                  for c in черновик["changes"] if int(c["seq"]) > int(base_version)])
    применено = await map_of(КАРТА)
    дерево_факт, посадки_факт = применено.get("tree"), await seats_of()
    дерево, посадки = наложение(дерево_факт, посадки_факт, черновик["changes"])
    новые = [s for s in посадки if s.get("state") == "planned"]
    # Снимаются и те посадки, чья операция исчезла из дерева: наложение убирает их из проекции, но
    # живая строка осталась бы в базе — посадка на несуществующем узле, невидимая и бессмертная.
    живые_узлы = поддерево(дерево)
    снимаемые = [s for s in посадки if s.get("state") == "dropping"]
    снимаемые += [s for s in посадки_факт if s["op_id"] not in живые_узлы]
    if preflight is not None:
        причины = await preflight(новые)
        if причины:
            raise Отказ("префлайт не пройден: " + "; ".join(причины))
    # Когда префлайт требует человека (ADR-013/014), применить молча нельзя: редакция идёт на
    # согласование, и применяет её подписант, прочитав ровно то, что отправили.
    if needs_approval is not None and not approved:
        нужно = await needs_approval(новые)
        if нужно:
            raise НуженПодписант(
                "редакция требует согласования: " + "; ".join(нужно)
                + ". Отправьте её на согласование — применит подписант", причины=нужно)

    номер = int(применено.get("revision") or 0) + 1
    if not _has_pg():
        _MEM["map"].setdefault(КАРТА, []).append({
            "area": КАРТА, "revision": номер, "tree": дерево, "note": черновик.get("note"),
            "applied_by": actor, "applied_at": _now().isoformat()})
        for s in новые:
            _MEM["seats"].append({"id": "seat-" + uuid.uuid4().hex[:10],
                                  "area": area_of(дерево, s["op_id"]),
                                  "op_id": s["op_id"], "audit_id": s["audit_id"],
                                  "pin_version": s.get("pin_version"),
                                  "created_by": s.get("created_by") or actor,
                                  "since": _now().isoformat(), "until": None})
        for s in снимаемые:
            for живая in _MEM["seats"]:
                if (живая["op_id"] == s["op_id"]
                        and живая["audit_id"] == s["audit_id"] and живая.get("until") is None):
                    живая["until"] = _now().isoformat()
        _MEM["changes"].pop(черновик["id"], None)
        ред = _MEM["rev"].get(area)
        if ред:
            ред["status"] = "применена"
            _MEM["rev"].pop(area, None)
    else:
        from .db import _conn
        async with _conn() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO process_map (area, revision, tree, note, applied_by) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (КАРТА, номер, json.dumps(дерево, ensure_ascii=False), черновик.get("note"), actor))
                for s in новые:
                    await conn.execute(
                        "INSERT INTO process_seats (id, area, op_id, audit_id, pin_version, created_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        ("seat-" + uuid.uuid4().hex[:10], area_of(дерево, s["op_id"]), s["op_id"],
                         s["audit_id"], s.get("pin_version"), s.get("created_by") or actor))
                for s in снимаемые:
                    await conn.execute(
                        "UPDATE process_seats SET until = now() WHERE op_id = %s "
                        "AND audit_id = %s AND until IS NULL", (s["op_id"], s["audit_id"]))
                await conn.execute(
                    "UPDATE process_revisions SET status = 'применена', approved_by = %s, "
                    "updated_at = now() WHERE id = %s", (actor, черновик["id"]))
    await _оповестить(area, {"type": "applied", "revision": номер, "author": actor})
    return {"area": area, "revision": номер, "seats_added": len(новые),
            "seats_closed": len(снимаемые), "tree": дерево,
            # сами посадки, а не только счётчик: вызывающей стороне надо знать, каких агентов
            # разворачивать — «Принять план» это префлайт И деплой (ADR-042)
            "seats": [{"op_id": s["op_id"], "audit_id": s["audit_id"],
                       "pin_version": s.get("pin_version")} for s in новые],
            "closed": [{"op_id": s["op_id"], "audit_id": s["audit_id"]} for s in снимаемые]}


# ── присутствие ───────────────────────────────────────────────────────────────────────────────────

async def presence_touch(area: str, actor: str) -> None:
    """Heartbeat живого подключения. Таблица, а не память процесса: при репликах список верен."""
    if not _has_pg():
        _MEM["presence"][(area, actor)] = _now()
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO process_presence (area, actor, at) VALUES (%s,%s,now()) "
            "ON CONFLICT (area, actor) DO UPDATE SET at = now()", (area, actor))


async def presence_of(area: str, *, window_sec: int = 60) -> list[str]:
    порог = _now() - _dt.timedelta(seconds=window_sec)
    if not _has_pg():
        return sorted(a for (ar, a), ts in _MEM["presence"].items() if ar == area and ts > порог)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT actor FROM process_presence WHERE area = %s AND at > %s ORDER BY actor",
            (area, порог))
        return [r[0] for r in await cur.fetchall()]


# ── живое догоняние ───────────────────────────────────────────────────────────────────────────────

def subscribe(area: str) -> asyncio.Queue:
    очередь: asyncio.Queue = asyncio.Queue(maxsize=64)
    _ПОДПИСЧИКИ.setdefault(area, set()).add(очередь)
    return очередь


def unsubscribe(area: str, очередь: asyncio.Queue) -> None:
    (_ПОДПИСЧИКИ.get(area) or set()).discard(очередь)


def poke(area: str | None = None) -> None:
    """Толкнуть локальных подписчиков. Вызывается и из cachebus-обработчика (правка с другой реплики)."""
    области = [area] if area else list(_ПОДПИСЧИКИ.keys())
    for a in области:
        for очередь in list(_ПОДПИСЧИКИ.get(a) or ()):
            try:
                очередь.put_nowait({"type": "poke", "area": a})
            except asyncio.QueueFull:
                pass    # подписчик отстал — переподключится и возьмёт разницу по since


async def _оповестить(area: str, событие: dict) -> None:
    """Своим подписчикам — сразу, остальным репликам — через существующий cachebus (топик process)."""
    for очередь in list(_ПОДПИСЧИКИ.get(area) or ()):
        try:
            очередь.put_nowait(dict(событие, area=area))
        except asyncio.QueueFull:
            pass
    try:
        from . import cachebus
        await cachebus.notify("process")
    except Exception as ex:  # noqa: BLE001 — оповещение опционально, запись уже прошла
        obs.log_event("warn", "process.notify_fail", error=f"{type(ex).__name__}: {ex}")
