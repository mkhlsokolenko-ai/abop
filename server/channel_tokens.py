# -*- coding: utf-8 -*-
"""Привязка внешнего канала (Telegram) к человеку: узкий токен ABOP, а не токен Keycloak.

**Главное решение и почему оно такое.** Бот в мессенджере — процесс, доступный из интернета, и
хранит он не пароль, а то, чем ходит в ABOP. Напрашивался offline-токен Keycloak: человек один раз
входит, бот обновляет доступ сам. Но offline-токен несёт ПОЛНЫЕ права человека — включая внешние
действия через другие каналы и, если человек admin, всю админку. Утечка файла с токенами
означала бы ровно это.

Поэтому канал получает собственный токен ABOP, у которого прав меньше по построению:

* **уровень снижен до `analyst`** — мутации, требующие `manager` и выше, канал не пройдёт, и это не
  проверка в одном месте, а свойство самой личности (`require_level` считает по ней);
* **переключение арендатора невозможно** — `tenancy.можно_переключать` пускает только admin/support;
* **отдел человека сохраняется** — иначе он не увидит собственных агентов, и канал станет бесполезен;
* **отзыв мгновенный и местный** — одна строка в своей базе, без обращения к Keycloak.

**Привязка без браузера.** Человек, уже вошедший в десктоп или веб, просит код; код живёт
`КОД_ЖИВЁТ` минут и действует ОДИН раз. Он отправляет код боту, бот меняет код на токен. Поэтому в
мессенджере не вводят пароль — там вводят одноразовый код, и это разные по цене ошибки.

**Что хранится.** Только хэши: `sha256` кода и токена. Утечка базы не даёт действовать от имени
человека — она даёт список того, что надо отозвать.

`channel_tokens{id, channel, external_id, token_hash, sub, name, department, tenant, …}`
`channel_codes{code_hash, channel, sub, …, expires_at, used}`
"""
from __future__ import annotations

import hashlib
import secrets
import time

from . import assembly, levels, tenancy
from .config import settings

# Канал — только из этого списка: строка приходит извне, а по ней ищется привязка.
КАНАЛЫ = ("telegram",)
КОД_ЖИВЁТ = 15 * 60          # с; одноразовый код на привязку
КОД_ДЛИНА = 8                # знаков, из алфавита без похожих друг на друга
АЛФАВИТ = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # без 0/O и 1/I — код читают с экрана
# Уровень доступа канала. Намеренно самый низкий: канал запускает агентов и читает свои прогоны,
# а подтверждать внешние действия и править настройки через мессенджер нельзя.
УРОВЕНЬ = "analyst"
ПРЕФИКС = "abopch_"          # по нему `deps.личность` отличает токен канала от JWT

# ── Что каналу позволено (решение владельца 13.10) ─────────────────────────────────────────────
# Канал нужен для двух вещей: СПРОСИТЬ данные и ОТОСЛАТЬ их коллегам письмом. Поэтому разрешены
# чтение и подготовка (навыки `read` и `write`, то есть в том числе составление письма) и один
# канал доставки — почта. Всё остальное необратимое (задача в трекере, запись в систему учёта,
# публикация) из мессенджера недоступно: там нет ни карточки действия, ни вида на последствия.
#
# Почта тоже необратима, и поэтому к ней идут ДВЕ защиты, а не одна:
#   1) отправка всегда через подтверждение человеком — канал не отправляет сам, даже если
#      автономия агента это позволяет (см. `run_exec._deliver_out_nodes`);
#   2) получатель обязан быть коллегой — перечень доменов задаёт `ABOP_COLLEAGUE_DOMAINS`.
#      Не задан ⇒ отправка из канала запрещена: ограничение без перечня — это предложение, а не
#      ограничение, и молча считать всех коллегами нельзя.
КАНАЛЫ_ДОСТАВКИ = ("email",)
РЕЖИМЫ_НАВЫКОВ = ("read", "write")


def вне_канала(действия: list[dict]) -> list[str]:
    """Действия агента, которые из мессенджера запускать нельзя, каждое с причиной.

    **Здесь ДВА вопроса, и свести их в один — ошибка, которую я уже сделал.** Лестница
    ответственности (`levels`) отвечает «какого уровня действие и кто решает». Политика канала
    отвечает «что вообще можно начинать из переписки», и она УЖЕ лестницы:

    * по лестнице задача в трекере — У1, действие внутри контура, подтверждения не требует;
    * но решение владельца 13.10 сузило канал до «спросить данные и отослать их коллегам
      письмом», и задача в трекере из мессенджера не заводится.

    Если проверять только лестницу, бот начнёт писать в Redmine и вики — не потому, что так
    решили, а потому что уровень позволил. Поэтому проверяются оба условия:

    1. класс действия — `наружу` и `необратимое` каналу закрыты (сюда же попадают навыки режима
       `action` и незнакомые каналы: у них класс fail-closed);
    2. канал доставки — только почта и местные (файл, PDF, чат).
    """
    плохие: list[str] = []
    for д in действия or []:
        что = str(д.get("что") or д.get("канал") or "?")
        класс = str(д.get("класс") or "")
        канал = str(д.get("канал") or "")
        if класс not in (levels.ВНУТРИ, levels.ПО_АДРЕСАТУ):
            плохие.append(f"{что} — {класс}, за пределами контура")
        elif (канал and not канал.startswith("навык:")
                and канал not in КАНАЛЫ_ДОСТАВКИ
                and канал not in assembly._LOCAL_CHANNELS):
            плохие.append(f"{что} — из переписки доступна только почта")
    return плохие


def домены_коллег() -> tuple[str, ...]:
    """Домены организации. Пусто ⇒ отправка из канала закрыта.

    Сам перечень живёт в `levels`: контур компании — свойство организации, а не особенность
    мессенджера, и по нему же считается уровень действия (письмо коллеге — У1, клиенту — У2).
    Здесь оставлено имя, потому что на него ссылаются маршрут подтверждения, бот и тесты.
    """
    return levels.домены_коллег()


def адрес_коллеги(адрес: str) -> tuple[bool, str]:
    """Можно ли из канала писать на этот адрес. Возвращает (можно, причина отказа).

    Сопоставляет домен `levels.свой` — один раз на продукт. Прежде здесь лежала своя копия
    сравнения, и она уже расходилась с лестницей на поддоменах: один и тот же адрес считался
    внешним в одном месте и внутренним в другом. Текст отказа остаётся канальным: человеку в
    переписке нужен не термин уровня, а что делать дальше.
    """
    домены = домены_коллег()
    if not домены:
        return False, ("перечень доменов коллег не задан (ABOP_COLLEAGUE_DOMAINS) — "
                       "отправка из канала закрыта, пока администратор его не задаст")
    а = str(адрес or "").strip().lower()
    if "@" not in а:
        return False, f"не похоже на адрес: «{адрес}»"
    if levels.свой(а):
        return True, ""
    return False, (f"адресат «{а}» не из организации (разрешены: {', '.join(домены)}) — "
                   "письмо наружу отправляйте из приложения")

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_tokens (
    id           TEXT PRIMARY KEY,
    channel      TEXT NOT NULL,
    external_id  TEXT NOT NULL DEFAULT '',   -- chat_id в мессенджере: куда можно отвечать
    token_hash   TEXT NOT NULL,
    sub          TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    department   TEXT NOT NULL DEFAULT '*',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked      BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_chtok_hash ON channel_tokens(token_hash);
CREATE TABLE IF NOT EXISTS channel_codes (
    code_hash   TEXT PRIMARY KEY,
    channel     TEXT NOT NULL,
    sub         TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    department  TEXT NOT NULL DEFAULT '*',
    expires_at  TIMESTAMPTZ NOT NULL,
    used        BOOLEAN NOT NULL DEFAULT FALSE
);
"""
SCHEMA += tenancy.миграция_ключа("channel_tokens", "id")
SCHEMA += tenancy.миграция_ключа("channel_codes", "code_hash")

_МЕМ_ТОКЕНЫ: dict[str, dict] = {}
_МЕМ_КОДЫ: dict[str, dict] = {}


def _есть_pg() -> bool:
    return bool(settings.pg_dsn)


def _хэш(значение: str) -> str:
    return hashlib.sha256(str(значение or "").encode("utf-8")).hexdigest()


def нормализовать_канал(значение: str) -> str:
    """Канал из внешнего ввода. Неизвестный — пустая строка, а не исключение: по нему ищут."""
    к = str(значение or "").strip().lower()
    return к if к in КАНАЛЫ else ""


async def init() -> None:
    if not _есть_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


# ── одноразовый код ─────────────────────────────────────────────────────────────────────────────

async def выдать_код(u: dict, канал: str = "telegram") -> dict:
    """Код на привязку. Хранится хэш: в базе нет того, что можно отправить боту."""
    канал = нормализовать_канал(канал) or "telegram"
    код = "".join(secrets.choice(АЛФАВИТ) for _ in range(КОД_ДЛИНА))
    запись = {
        "code_hash": _хэш(код), "channel": канал,
        "sub": str(u.get("sub") or u.get("name") or "dev"),
        "name": str(u.get("name") or ""),
        "department": str(u.get("department") or "*"),
        "expires_at": time.time() + КОД_ЖИВЁТ, "used": False,
        "tenant": tenancy.текущий(),
    }
    if _есть_pg():
        from .db import _conn
        async with _conn() as conn:
            await conn.execute(
                "INSERT INTO channel_codes(code_hash,channel,sub,name,department,expires_at,used,tenant) "
                "VALUES(%s,%s,%s,%s,%s,to_timestamp(%s),FALSE,%s) ON CONFLICT (code_hash,tenant) DO NOTHING",
                (запись["code_hash"], канал, запись["sub"], запись["name"], запись["department"],
                 запись["expires_at"], запись["tenant"]))
    else:
        # Ключ — ТОЛЬКО хэш, без арендатора в нём: обмен кода сквозной по построению, и
        # запомнить код «внутри арендатора» значило бы, что в памяти он не найдётся, а в Postgres
        # найдётся. Расхождение фолбэка с базой — это ошибка, которая всплывает только на проде.
        _МЕМ_КОДЫ[запись["code_hash"]] = запись
    return {"код": код, "канал": канал, "живёт_минут": КОД_ЖИВЁТ // 60}


async def обменять_код(код: str, канал: str, external_id: str) -> dict:
    """Код → токен канала. Код гасится, даже если им воспользовались «почти одновременно».

    Возвращает `{ok, токен, кто}` либо `{ok: False, почему}`. Причина отказа общая и для
    просроченного, и для использованного, и для несуществующего кода: по разнице в ответах код
    подбирают.
    """
    канал = нормализовать_канал(канал)
    ч = _хэш(код)
    отказ = {"ok": False, "почему": "код не подходит, истёк или уже использован"}
    if not канал or not str(external_id or "").strip():
        return отказ
    if _есть_pg():
        from .db import _conn
        async with _conn() as conn:
            # Гасим и читаем ОДНИМ запросом: иначе два сообщения боту подряд обменяли бы один код
            # дважды и дали два токена на одну привязку.
            #
            # Запрос СКВОЗНОЙ по арендаторам, и это необходимо: бот, получив код в мессенджере, не
            # знает и не может знать, к какому арендатору относится человек, — арендатор как раз и
            # возвращается этой выборкой. Хэш кода уникален (восемь знаков из тридцати двух плюс
            # срок жизни пятнадцать минут и однократность), поэтому «найти чужой код» значит его
            # угадать. Объявлено в `tests/test_tenancy.py: СКВОЗНЫЕ`.
            cur = await conn.execute(
                "UPDATE channel_codes SET used=TRUE WHERE code_hash=%s AND channel=%s "
                "AND used=FALSE AND expires_at > now() RETURNING sub,name,department,tenant",
                (ч, канал))
            строка = await cur.fetchone()
        if not строка:
            return отказ
        sub, имя, отдел, арендатор = строка[0], строка[1] or "", строка[2] or "*", строка[3]
    else:
        з = _МЕМ_КОДЫ.get(ч)
        if not з or з.get("used") or з["channel"] != канал or з["expires_at"] < time.time():
            return отказ
        з["used"] = True
        sub, имя, отдел, арендатор = з["sub"], з["name"], з["department"], з["tenant"]

    токен = ПРЕФИКС + secrets.token_urlsafe(32)
    ид = secrets.token_hex(8)
    if _есть_pg():
        from .db import _conn
        async with _conn() as conn:
            await conn.execute(
                "INSERT INTO channel_tokens(id,channel,external_id,token_hash,sub,name,department,tenant) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (ид, канал, str(external_id), _хэш(токен), sub, имя, отдел, арендатор))
    else:
        _МЕМ_ТОКЕНЫ[_хэш(токен)] = {
            "id": ид, "channel": канал, "external_id": str(external_id), "sub": sub,
            "name": имя, "department": отдел, "tenant": арендатор, "revoked": False,
            "created_at": time.time(), "last_used_at": None}
    return {"ok": True, "токен": токен, "id": ид, "кто": имя or sub, "канал": канал,
            "арендатор": арендатор}


# ── проверка токена ─────────────────────────────────────────────────────────────────────────────

def личность_по_токену(токен: str) -> dict | None:
    """Токен канала → личность для `deps`. Синхронно: разбор токена идёт в разборе запроса.

    Возвращает `None`, если токен не наш, отозван или неизвестен. Права урезаны здесь, а не в
    маршрутах: урезание в маршрутах пришлось бы повторять в каждом новом, и однажды забылось бы.
    """
    if not str(токен or "").startswith(ПРЕФИКС):
        return None
    запись = _МЕМ_ТОКЕНЫ.get(_хэш(токен))
    if запись is None and _есть_pg():
        запись = _из_pg(_хэш(токен))
    if not запись or запись.get("revoked"):
        return None
    return {
        "sub": запись["sub"], "name": запись.get("name") or запись["sub"],
        "roles": [УРОВЕНЬ], "level": УРОВЕНЬ,
        "department": запись.get("department") or "*",
        "dev": False,
        "tenant": запись.get("tenant") or tenancy.ПО_УМОЛЧАНИЮ,
        # По этим двум полям маршрут решает, можно ли действие в канале, а доставка — куда отвечать.
        "channel": запись.get("channel") or "",
        "channel_to": запись.get("external_id") or "",
        "channel_id": запись.get("id") or "",
    }


def _из_pg(хэш: str) -> dict | None:
    """Синхронное чтение привязки: разбор запроса в FastAPI синхронный, а токен нужен сразу.

    Отдельное короткое соединение, а не общий пул: пул асинхронный, и тащить его сюда значило бы
    сделать разбор запроса асинхронным ради одной выборки.
    """
    try:
        import psycopg
    except ImportError:
        return None
    try:
        with psycopg.connect(settings.pg_dsn, connect_timeout=3) as conn:
            cur = conn.execute(
                "SELECT id,channel,external_id,sub,name,department,tenant,revoked "
                "FROM channel_tokens WHERE token_hash=%s", (хэш,))
            r = cur.fetchone()
    except Exception:            # noqa: BLE001 — база недоступна: канал не пускаем, но не падаем
        return None
    if not r:
        return None
    з = {"id": r[0], "channel": r[1], "external_id": r[2], "sub": r[3], "name": r[4],
         "department": r[5], "tenant": r[6], "revoked": bool(r[7])}
    if not з["revoked"]:
        _МЕМ_ТОКЕНЫ[хэш] = з          # кэш на процесс: привязок мало, запросов много
    return з


async def отметить_обращение(ид: str) -> None:
    """Когда каналом пользовались последний раз. По этому видно забытые привязки."""
    if not _есть_pg():
        for з in _МЕМ_ТОКЕНЫ.values():
            if з.get("id") == ид:
                з["last_used_at"] = time.time()
        return
    from .db import _conn
    async with _conn() as conn:
        # Фильтр по арендатору — ТЕКСТОМ запроса, а не склейкой: склеенный сторож не видит, и
        # пропущенный фильтр становится утечкой, подписанной стилем кода.
        await conn.execute("UPDATE channel_tokens SET last_used_at=now() "
                           "WHERE id=%s AND tenant=%s", (ид, tenancy.текущий()))


# ── список и отзыв ──────────────────────────────────────────────────────────────────────────────

async def мои(sub: str) -> list[dict]:
    """Привязки человека: что показать в кабинете и что можно отозвать."""
    if not _есть_pg():
        return [{"id": з["id"], "канал": з["channel"], "куда": з["external_id"],
                 "создана": з.get("created_at"), "последнее": з.get("last_used_at")}
                for з in _МЕМ_ТОКЕНЫ.values()
                if з["sub"] == sub and not з.get("revoked")
                and з.get("tenant") == tenancy.текущий()]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,channel,external_id,created_at,last_used_at FROM channel_tokens "
            "WHERE sub=%s AND revoked=FALSE AND tenant=%s ORDER BY created_at DESC",
            (sub, tenancy.текущий()))
        строки = await cur.fetchall()
    return [{"id": r[0], "канал": r[1], "куда": r[2],
             "создана": r[3].isoformat() if r[3] else None,
             "последнее": r[4].isoformat() if r[4] else None} for r in строки]


async def отозвать(sub: str, ид: str) -> bool:
    """Отзыв привязки. Только своей: чужую не отзывают даже admin — для этого есть удаление."""
    if not _есть_pg():
        for хэш, з in list(_МЕМ_ТОКЕНЫ.items()):
            if з.get("id") == ид and з["sub"] == sub and з.get("tenant") == tenancy.текущий():
                з["revoked"] = True
                _МЕМ_ТОКЕНЫ.pop(хэш, None)      # из кэша тоже: иначе отзыв не действует до рестарта
                return True
        return False
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "UPDATE channel_tokens SET revoked=TRUE "
            "WHERE id=%s AND sub=%s AND revoked=FALSE AND tenant=%s RETURNING token_hash",
            (ид, sub, tenancy.текущий()))
        строка = await cur.fetchone()
    if строка:
        _МЕМ_ТОКЕНЫ.pop(строка[0], None)
        return True
    return False
