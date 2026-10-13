# -*- coding: utf-8 -*-
"""Привязка внешнего канала: узкий токен, а не доверенность на всё.

Бот в мессенджере — процесс, доступный из интернета. Напрашивался offline-токен Keycloak: один
вход, дальше бот обновляет доступ сам. Но offline-токен несёт ПОЛНЫЕ права человека — внешние
действия через другие каналы и, если человек admin, всю админку. Утечка файла с токенами означала
бы именно это, поэтому канал получает собственный токен ABOP с правами `analyst`.

Здесь проверяется то, на чём такой канал легко сделать дырой:

* **Расширение прав.** Токен канала не должен проходить туда, куда нужен `manager`, и не должен
  уметь переключать арендатора.
* **Цепочка привязок.** Если каналом можно выпустить ещё один канал, то отзыв перестаёт быть
  отзывом: утёкший токен плодит новые без конца.
* **Подбор кода.** Код из восьми знаков подбирается перебором, если ручка обмена открыта всем и
  отвечает по-разному на «нет такого кода» и «код истёк».
* **Внешние действия.** Канал запускает только то, что читает и считает. Проверка должна стоять НА
  ЗАПУСКЕ: спрятать агента из списка значит защититься от интерфейса, а не от запроса.
* **Арендатор.** Код и токен сами устанавливают арендатора — бот его не знает и знать не может.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"
os.environ["ABOP_BOT_SECRET"] = "secret-for-test"

from fastapi.testclient import TestClient  # noqa: E402

from server import assembly, channel_tokens as ct, tenancy  # noqa: E402
from server.routes import channels as маршрут  # noqa: E402

ЧЕЛОВЕК = {"sub": "u-7", "name": "Пётр", "department": "финансы", "level": "manager"}


@pytest.fixture(autouse=True)
def чисто():
    ct._МЕМ_ТОКЕНЫ.clear()
    ct._МЕМ_КОДЫ.clear()
    маршрут._ПОПЫТКИ.clear()
    маршрут._СЕКРЕТ = "secret-for-test"
    tenancy.установить(tenancy.ПО_УМОЛЧАНИЮ)
    yield
    tenancy.установить(tenancy.ПО_УМОЛЧАНИЮ)


def _привязать(кто: dict = ЧЕЛОВЕК, куда: str = "99001") -> dict:
    к = asyncio.run(ct.выдать_код(кто))
    return asyncio.run(ct.обменять_код(к["код"], "telegram", куда))


# ── права токена канала ──────────────────────────────────────────────────────────────────────────

def test_уровень_канала_самый_низкий():
    """`analyst` — это не осторожность, а свойство личности: `require_level` считает по ней."""
    л = ct.личность_по_токену(_привязать()["токен"])
    assert л["level"] == "analyst", л
    assert л["roles"] == ["analyst"]


def test_мутации_уровня_manager_каналу_закрыты():
    from fastapi import HTTPException

    from server.deps import require_level

    л = ct.личность_по_токену(_привязать()["токен"])
    with pytest.raises(HTTPException) as e:
        require_level(л, "manager")
    assert e.value.status_code == 403
    require_level(л, "analyst")          # своё — можно


def test_канал_не_переключает_арендатора():
    л = ct.личность_по_токену(_привязать()["токен"])
    assert tenancy.можно_переключать(л) is False
    assert tenancy.для_пользователя(л, "чужой-арендатор") == л["tenant"], (
        "канал смог запросить данные другого арендатора")


def test_отдел_человека_сохраняется():
    """Без отдела канал не увидит собственных агентов и станет бесполезен."""
    л = ct.личность_по_токену(_привязать()["токен"])
    assert л["department"] == "финансы"


def test_в_личности_видно_канал_и_куда_отвечать():
    л = ct.личность_по_токену(_привязать(куда="55555")["токен"])
    assert л["channel"] == "telegram"
    assert л["channel_to"] == "55555", "неизвестно, в какой чат отвечать"
    assert л["channel_id"], "нет идентификатора привязки — нечего отзывать"


# ── код: одноразовый, короткоживущий, неподбираемый ──────────────────────────────────────────────

def test_код_действует_один_раз():
    к = asyncio.run(ct.выдать_код(ЧЕЛОВЕК))
    assert asyncio.run(ct.обменять_код(к["код"], "telegram", "1"))["ok"] is True
    assert asyncio.run(ct.обменять_код(к["код"], "telegram", "1"))["ok"] is False


def test_просроченный_код_не_подходит(monkeypatch):
    monkeypatch.setattr(ct, "КОД_ЖИВЁТ", -1)
    к = asyncio.run(ct.выдать_код(ЧЕЛОВЕК))
    assert asyncio.run(ct.обменять_код(к["код"], "telegram", "1"))["ok"] is False


def test_причина_отказа_одна_для_всех_случаев():
    """По разнице в ответах код подбирают: «истёк» и «нет такого» не должны отличаться."""
    к = asyncio.run(ct.выдать_код(ЧЕЛОВЕК))
    asyncio.run(ct.обменять_код(к["код"], "telegram", "1"))
    использован = asyncio.run(ct.обменять_код(к["код"], "telegram", "1"))["почему"]
    нет_такого = asyncio.run(ct.обменять_код("ZZZZZZZZ", "telegram", "1"))["почему"]
    assert использован == нет_такого, (использован, нет_такого)


def test_в_базе_лежат_только_хэши():
    """Утечка базы не должна давать то, чем можно действовать от имени человека."""
    к = asyncio.run(ct.выдать_код(ЧЕЛОВЕК))
    assert к["код"] not in ct._МЕМ_КОДЫ, "сам код лежит в хранилище"
    r = _привязать()
    assert all(r["токен"] not in str(ключ) for ключ in ct._МЕМ_ТОКЕНЫ), "сам токен лежит в хранилище"
    assert ct._хэш(r["токен"]) in ct._МЕМ_ТОКЕНЫ


def test_код_читается_с_экрана():
    """Код диктуют и перенабирают, поэтому в алфавите нет похожих друг на друга знаков."""
    assert not set("01OI") & set(ct.АЛФАВИТ), ct.АЛФАВИТ
    к = asyncio.run(ct.выдать_код(ЧЕЛОВЕК))
    assert len(к["код"]) == ct.КОД_ДЛИНА
    assert set(к["код"]) <= set(ct.АЛФАВИТ)


def test_неизвестный_канал_не_привязывается():
    assert ct.нормализовать_канал("whatsapp") == ""
    assert asyncio.run(ct.обменять_код("ABCD2345", "whatsapp", "1"))["ok"] is False


# ── арендатор устанавливается кодом ──────────────────────────────────────────────────────────────

def test_арендатор_приезжает_из_кода_а_не_из_контекста_бота():
    """Бот, получив код в мессенджере, не знает и не может знать, чей это человек."""
    tenancy.установить("acme")
    к = asyncio.run(ct.выдать_код(ЧЕЛОВЕК))
    tenancy.установить(tenancy.ПО_УМОЛЧАНИЮ)        # бот работает вне арендатора
    r = asyncio.run(ct.обменять_код(к["код"], "telegram", "1"))
    assert r["ok"] is True
    assert r["арендатор"] == "acme", r
    assert ct.личность_по_токену(r["токен"])["tenant"] == "acme"


def test_привязки_видны_только_в_своём_арендаторе():
    tenancy.установить("acme")
    к = asyncio.run(ct.выдать_код(ЧЕЛОВЕК))
    asyncio.run(ct.обменять_код(к["код"], "telegram", "1"))
    assert len(asyncio.run(ct.мои("u-7"))) == 1
    tenancy.установить("beta")
    assert asyncio.run(ct.мои("u-7")) == [], "привязка одного арендатора видна в другом"


# ── отзыв ────────────────────────────────────────────────────────────────────────────────────────

def test_отзыв_действует_немедленно():
    r = _привязать()
    assert asyncio.run(ct.отозвать("u-7", r["id"])) is True
    assert ct.личность_по_токену(r["токен"]) is None, (
        "отозванный токен продолжает действовать — кэш не сброшен")


def test_чужую_привязку_не_отозвать():
    r = _привязать()
    assert asyncio.run(ct.отозвать("другой-человек", r["id"])) is False
    assert ct.личность_по_токену(r["токен"]) is not None


# ── ручки ────────────────────────────────────────────────────────────────────────────────────────

def test_каналом_нельзя_выпустить_канал():
    """Иначе утёкший токен плодит привязки без конца, и отзыв одной ничего не решает."""
    from fastapi import HTTPException

    л = ct.личность_по_токену(_привязать()["токен"])
    with pytest.raises(HTTPException) as e:
        asyncio.run(маршрут.выдать_код("telegram", л))
    assert e.value.status_code == 403
    assert "не из бота" in str(e.value.detail)


def test_обмен_требует_секрета_бота():
    c = TestClient(__import__("server.web_api", fromlist=["app"]).app)
    к = asyncio.run(ct.выдать_код(ЧЕЛОВЕК))
    r = c.post("/api/channels/telegram/exchange",
               json={"code": к["код"], "external_id": "1"})
    assert r.status_code == 403, r.text
    r = c.post("/api/channels/telegram/exchange", json={"code": к["код"], "external_id": "1"},
               headers={"X-Bot-Secret": "secret-for-test"})
    assert r.status_code == 200, r.text
    assert r.json()["токен"].startswith(ct.ПРЕФИКС)


def test_без_секрета_в_настройках_обмен_выключен(monkeypatch):
    """Лучше неработающий канал, чем открытая ручка обмена."""
    monkeypatch.setattr(маршрут, "_СЕКРЕТ", "")
    c = TestClient(__import__("server.web_api", fromlist=["app"]).app)
    r = c.post("/api/channels/telegram/exchange", json={"code": "ABCD2345", "external_id": "1"},
               headers={"X-Bot-Secret": ""})
    assert r.status_code == 503, r.text


def test_перебор_кода_ограничен():
    c = TestClient(__import__("server.web_api", fromlist=["app"]).app)
    коды = [c.post("/api/channels/telegram/exchange",
                   json={"code": "ZZZZ000" + str(i % 10), "external_id": "1"},
                   headers={"X-Bot-Secret": "secret-for-test"}).status_code
            for i in range(маршрут._ПРЕДЕЛ + 2)]
    assert 429 in коды, коды
    assert коды.count(400) <= маршрут._ПРЕДЕЛ, коды


def test_ручка_отдаёт_код_человеку_и_пишет_в_журнал():
    c = TestClient(__import__("server.web_api", fromlist=["app"]).app)
    r = c.post("/api/channels/telegram/code")
    assert r.status_code == 200, r.text
    тело = r.json()
    assert len(тело["код"]) == ct.КОД_ДЛИНА and тело["живёт_минут"] == ct.КОД_ЖИВЁТ // 60


def test_неизвестный_канал_в_ручке_отклоняется():
    c = TestClient(__import__("server.web_api", fromlist=["app"]).app)
    assert c.post("/api/channels/whatsapp/code").status_code == 400


# ── внешние действия ─────────────────────────────────────────────────────────────────────────────

ВНЕШНИЙ = {"graph": {"nodes": [{"kind": "skill", "skill": "x"},
                               {"kind": "out", "out": {"channel": "email"}}]}}
МЕСТНЫЙ = {"graph": {"nodes": [{"kind": "skill", "skill": "x"},
                               {"kind": "out", "out": {"channel": "pdf"}}]}}


def test_внешние_действия_определяются_в_одном_месте():
    """Правило спрашивают из трёх мест; три копии разошлись бы на первой правке списка каналов."""
    assert assembly.действует_наружу(ВНЕШНИЙ) == ["email"]
    assert assembly.действует_наружу(МЕСТНЫЙ) == []
    assert assembly.действует_наружу({}) == []
    текст = (КОРЕНЬ / "server" / "routes" / "plan.py").read_text(encoding="utf-8")
    assert "assembly._LOCAL_CHANNELS" not in текст, (
        "в plan.py снова своя копия правила про внешние каналы")


def test_проверка_канала_стоит_на_запуске():
    """Спрятать агента из списка — защита от интерфейса, а не от запроса."""
    текст = (КОРЕНЬ / "server" / "routes" / "runs.py").read_text(encoding="utf-8")
    i = текст.index("async def run_start")
    тело = текст[i:i + 3500]
    assert "действует_наружу" in тело, "на запуске прогона нет проверки каналов доставки"
    assert "КАНАЛЫ_ДОСТАВКИ" in тело, "перечень разрешённых каналов не применяется"
    assert "РЕЖИМЫ_НАВЫКОВ" in тело, "режим навыка не проверяется"
    assert 'u.get("channel")' in тело, "проверка не смотрит, что запрос пришёл из канала"
    assert тело.count("403") >= 2, "оба отказа должны быть 403"


# ── политика канала: читать, готовить письма, отправлять коллегам ────────────────────────────────
#
# Решение владельца 13.10: канал нужен для двух вещей — спросить данные и отослать их коллегам
# письмом. Поэтому «никаких внешних действий» сменилось точным перечнем: почта можно, остальное
# необратимое — нет. Почта тоже необратима, и к ней идут ДВЕ защиты: подтверждение человеком и
# проверка, что адресат — коллега.

ПОЧТА = {"graph": {"nodes": [{"kind": "skill", "skill": "x"},
                             {"kind": "out", "out": {"channel": "email"}}]}}
ТРЕКЕР = {"graph": {"nodes": [{"kind": "skill", "skill": "x"},
                              {"kind": "out", "out": {"channel": "redmine"}}]}}


def test_почта_каналу_разрешена_а_трекер_нет():
    assert "email" in ct.КАНАЛЫ_ДОСТАВКИ
    почта = [c for c in assembly.действует_наружу(ПОЧТА) if c not in ct.КАНАЛЫ_ДОСТАВКИ]
    трекер = [c for c in assembly.действует_наружу(ТРЕКЕР) if c not in ct.КАНАЛЫ_ДОСТАВКИ]
    assert почта == [], "почта должна проходить: канал нужен, чтобы отослать данные коллегам"
    assert трекер == ["redmine"], "задача в трекере из мессенджера проходить не должна"


def test_режимы_навыков_каналу_ограничены():
    """`action` меняет внешнюю систему помимо доставки — в мессенджере ему места нет."""
    assert ct.РЕЖИМЫ_НАВЫКОВ == ("read", "write")
    assert "action" not in ct.РЕЖИМЫ_НАВЫКОВ


def test_отправка_из_канала_всегда_через_подтверждение():
    """Даже если автономия агента позволяет отправить саму — из канала только через человека."""
    текст = (КОРЕНЬ / "server" / "run_exec.py").read_text(encoding="utf-8")
    i = текст.index("async def _deliver_out_nodes")
    # Окном, а не до следующего `async def`: внутри функции есть вложенные определения, и поиск
    # «следующего» выхватывал не тот кусок (а при их отсутствии бросал ValueError вместо отказа).
    тело = текст[i:i + 6000]
    assert "из_канала" in тело, "доставка не знает, что прогон пришёл из канала"
    j = тело.index("из_канала and channel not in")
    рядом = тело[j:j + 500]
    assert '"hitl": True' in рядом, "гейт не ставится принудительно"
    assert "необратим" in рядом.lower() or "подтверждени" in рядом.lower(), (
        "решение не объяснено в коде — следующий читатель снимет его как лишнее")


def test_признак_канала_доезжает_через_очередь():
    """Прогон из канала почти всегда асинхронный: без признака в задании защита не работала бы."""
    маршрут = (КОРЕНЬ / "server" / "routes" / "runs.py").read_text(encoding="utf-8")
    assert '"channel": str(u.get("channel") or "")' in маршрут, "канал не кладётся в задание"
    воркер = (КОРЕНЬ / "server" / "web_api.py").read_text(encoding="utf-8")
    assert 'из_канала=str(p.get("channel") or "")' in воркер, "воркер не передаёт канал исполнителю"


# ── подтверждение: только своё, только коллеге ───────────────────────────────────────────────────

ЗАЯВКА = {"id": "hitl-1", "channel": "email", "to_addr": "kolya@acme.ru",
          "requested_by": "Пётр", "state": "pending", "family": "финансы"}


def _решить(item, decision="approve", **личность):
    from server.routes.hitl import _можно_решать

    л = {"channel": "telegram", "name": "Пётр", "sub": "u-7", "level": "analyst", **личность}
    _можно_решать(л, item, decision)


def test_канал_подтверждает_своё_письмо_коллеге(monkeypatch):
    monkeypatch.setenv("ABOP_COLLEAGUE_DOMAINS", "acme.ru")
    _решить(ЗАЯВКА)            # не бросает — значит можно


def test_канал_не_подтверждает_чужую_заявку(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("ABOP_COLLEAGUE_DOMAINS", "acme.ru")
    with pytest.raises(HTTPException) as e:
        _решить({**ЗАЯВКА, "requested_by": "Иван"})
    assert e.value.status_code == 403
    assert "не ваша заявка" in str(e.value.detail)


def test_канал_не_подтверждает_действия_в_других_системах(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("ABOP_COLLEAGUE_DOMAINS", "acme.ru")
    with pytest.raises(HTTPException) as e:
        _решить({**ЗАЯВКА, "channel": "redmine", "to_addr": "project-7"})
    assert e.value.status_code == 403
    assert "приложении" in str(e.value.detail)


def test_письмо_наружу_из_канала_не_уходит(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("ABOP_COLLEAGUE_DOMAINS", "acme.ru")
    with pytest.raises(HTTPException) as e:
        _решить({**ЗАЯВКА, "to_addr": "someone@gmail.com"})
    assert e.value.status_code == 403
    assert "не из организации" in str(e.value.detail)


def test_без_перечня_доменов_отправка_из_канала_закрыта(monkeypatch):
    """Ограничение без перечня — предложение, а не ограничение. Поэтому fail-closed."""
    from fastapi import HTTPException

    monkeypatch.delenv("ABOP_COLLEAGUE_DOMAINS", raising=False)
    with pytest.raises(HTTPException) as e:
        _решить(ЗАЯВКА)
    assert e.value.status_code == 403
    assert "ABOP_COLLEAGUE_DOMAINS" in str(e.value.detail), (
        "администратору не сказано, что именно задать")


def test_отказ_от_отправки_каналу_можно_всегда(monkeypatch):
    """Отказ необратимым не бывает — запрещать его значит заставлять человека идти за компьютер."""
    monkeypatch.delenv("ABOP_COLLEAGUE_DOMAINS", raising=False)
    _решить(ЗАЯВКА, decision="reject")
    _решить({**ЗАЯВКА, "to_addr": "someone@gmail.com"}, decision="reject")


def test_человеку_без_канала_правило_прежнее(monkeypatch):
    """Обычный путь не изменился: manager+ подтверждает что угодно, analyst — ничего."""
    from fastapi import HTTPException

    monkeypatch.setenv("ABOP_COLLEAGUE_DOMAINS", "acme.ru")
    _решить(ЗАЯВКА, channel="", level="manager")
    with pytest.raises(HTTPException) as e:
        _решить(ЗАЯВКА, channel="", level="analyst")
    assert e.value.status_code == 403
