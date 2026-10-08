# -*- coding: utf-8 -*-
"""AS-IS подтверждается прогонами, а не декларацией (ADR-044).

Карта утверждала, что на операции сидит агент. Проверить это было нечем: `runs` знает `agent_id`,
`run_jobs` знает `agent_id`, фаер триггера знает `agent_id` — операцию не знала ни одна таблица. AS-IS
поэтому был рисунком, который невозможно опровергнуть: агента сняли месяц назад, а карта продолжала
показывать его на месте.

Теперь прогон запоминает, какие операции он закрывал, а признак подтверждения считается, а не
хранится — по образцу `_dep_status_ru`, где статус развёртывания тоже выводится, а не лежит в базе.

Отдельно считается расхождение «агент работает по триггерам, но не посажен ни на одну операцию» —
детектор того, что процесс уехал, а карта осталась.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ.setdefault("ABOP_RUN_WORKERS", "0")
os.environ["ABOP_DEV_AUTH"] = "1"

from server import process_store as ps  # noqa: E402

СЕЙЧАС = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.timezone.utc)


def _прогон(audit_id, дней_назад, ops, ok=True):
    когда = (СЕЙЧАС - dt.timedelta(days=дней_назад)).isoformat()
    return {"agent_id": audit_id + ".v1", "created_at": когда, "verdict_ok": ok, "op_ids": ops}


def _посадка(op_id, audit_id):
    return {"op_id": op_id, "audit_id": audit_id, "state": "actual"}


def _считать(*, посадки, прогоны, в_лимбе=(), окно=7):
    return ps.liveness(seats=посадки, runs=прогоны, retired=set(в_лимбе),
                       window_days=окно, now=СЕЙЧАС)


def test_подтверждён_когда_по_операции_есть_прогоны():
    """Карта совпадает с жизнью: по участку в окне были прогоны."""
    итог = _считать(посадки=[_посадка("o213", "audit-1c")],
                    прогоны=[_прогон("audit-1c", 1, ["o213"]), _прогон("audit-1c", 3, ["o213"])])
    о = итог["ops"]["o213"]
    assert о["state"] == "подтверждён" and о["runs"] == 2 and о["ok"] == 2


def test_тихо_когда_посадка_есть_а_прогонов_нет():
    """Агент посажен и не работает — это надо видеть, а не додумывать."""
    итог = _считать(посадки=[_посадка("o213", "audit-1c")],
                    прогоны=[_прогон("audit-1c", 23, ["o213"])])
    о = итог["ops"]["o213"]
    assert о["state"] == "тихо" and о["silent_days"] == 23


def test_тихо_без_единого_прогона():
    итог = _считать(посадки=[_посадка("o213", "audit-1c")], прогоны=[])
    assert итог["ops"]["o213"]["state"] == "тихо" and итог["ops"]["o213"]["silent_days"] is None


def test_осиротел_когда_агент_выведен_в_лимб():
    """Автоматически снимать посадку нельзя: шаг тихо исчез бы с карты, и процесс выглядел бы целым."""
    итог = _считать(посадки=[_посадка("o213", "audit-1c")],
                    прогоны=[_прогон("audit-1c", 1, ["o213"])], в_лимбе=["audit-1c"])
    assert итог["ops"]["o213"]["state"] == "осиротел"


def test_прогон_вне_окна_не_подтверждает():
    итог = _считать(посадки=[_посадка("o213", "audit-1c")],
                    прогоны=[_прогон("audit-1c", 30, ["o213"])], окно=7)
    assert итог["ops"]["o213"]["state"] == "тихо" and итог["ops"]["o213"]["runs"] == 0


def test_замечания_считаются_отдельно_от_числа_прогонов():
    """«Работает» и «работает хорошо» — разные вопросы: подтверждение это первый, вердикт второй."""
    итог = _считать(посадки=[_посадка("o213", "audit-1c")],
                    прогоны=[_прогон("audit-1c", 1, ["o213"]), _прогон("audit-1c", 2, ["o213"], ok=False)])
    о = итог["ops"]["o213"]
    assert о["state"] == "подтверждён" and о["runs"] == 2 and о["ok"] == 1


def test_агент_вне_карты_попадает_в_расхождения():
    """Прогоны есть, посадок нет — процесс уехал, а карта осталась."""
    итог = _считать(посадки=[_посадка("o213", "audit-1c")],
                    прогоны=[_прогон("audit-1c", 1, ["o213"]), _прогон("mailtasks", 2, [])])
    вне = итог["outside"]
    assert [x["audit_id"] for x in вне] == ["mailtasks"] and вне[0]["runs"] == 1


def test_посаженный_агент_не_считается_вне_карты():
    """Ручной запуск посаженного агента — это отладка, а не расхождение."""
    итог = _считать(посадки=[_посадка("o213", "audit-1c")],
                    прогоны=[_прогон("audit-1c", 1, [])])
    assert итог["outside"] == []


def test_группа_подтверждается_одним_прогоном():
    """Один прогон закрывает все операции своей группы: в нём и лежит их список."""
    итог = _считать(посадки=[_посадка("o212", "audit-1c"), _посадка("o213", "audit-1c")],
                    прогоны=[_прогон("audit-1c", 1, ["o212", "o213"])])
    assert {o: итог["ops"][o]["state"] for o in ("o212", "o213")} == {
        "o212": "подтверждён", "o213": "подтверждён"}


def test_операции_прогона_берутся_из_посадок_агента():
    """`triggers.fire` знает агента, а операции — у его посадок (ADR-044)."""
    async def сценарий():
        await ps._сброс_для_тестов()
        await ps.save_map(ps.КАРТА, {
            "id": "org", "kind": "org", "label": "О", "children": [
                {"id": "p2", "kind": "process", "label": "Учёт", "area": "finance", "children": [
                    {"id": "s21", "kind": "subprocess", "label": "Аудит", "children": [
                        {"id": "o212", "kind": "task", "label": "Связи"},
                        {"id": "o213", "kind": "task", "label": "Проверки"},
                    ]}]}]}, editor="admin")
        r = await ps.append("finance", base_version=0, author="@pm", changes=[
            {"kind": "add_seat", "target": "o212", "payload": {"audit_id": "audit-1c"}},
            {"kind": "add_seat", "target": "o213", "payload": {"audit_id": "audit-1c"}}])
        await ps.apply("finance", base_version=r["version"], actor="@pm")
        from server import web_api
        # _seat_ops переехал вместе с картой процессов в server/routes/process.py (п.23).
        from server.routes import process as маршруты_карты
        return await маршруты_карты._seat_ops({"id": "audit-1c.v2",
                                               "contract_audit_id": "audit-1c"})
    assert sorted(asyncio.run(сценарий())) == ["o212", "o213"]


def test_сводка_прогонов_несёт_операции():
    """Сводка журнала нарочно лёгкая, без payload — и операции до подтверждения не доезжали.
    Поймано на стенде: участок с двумя свежими прогонами показывал «тихо — прогонов не было»."""
    async def сценарий():
        from server import run_store
        await run_store.init()
        await run_store.save({"id": "пробный", "agent_id": "audit-1c.v1", "verdict": {"ok": True},
                              "op_ids": ["o212", "o213"]})
        строки = await run_store.list_runs(limit=10)
        return [r for r in строки if r["id"] == "пробный"][0]
    сводка = asyncio.run(сценарий())
    assert сводка["op_ids"] == ["o212", "o213"], "сводка прогона потеряла операции участка"


def test_вне_карты_свежие_сверху():
    """Список «вне карты» читают, чтобы понять, что отстало от жизни последним, — порядок по
    времени последнего прогона, а не по идентификатору агента."""
    сейчас = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.timezone.utc)
    прогоны = [
        {"agent_id": "zzz-старый.v1", "created_at": (сейчас - dt.timedelta(days=5)).isoformat(),
         "verdict_ok": True, "started_by": "trigger"},
        {"agent_id": "aaa-свежий.v1", "created_at": (сейчас - dt.timedelta(hours=2)).isoformat(),
         "verdict_ok": True, "started_by": "trigger"},
    ]
    итог = ps.liveness(seats=[], runs=прогоны, retired=set(), window_days=7, now=сейчас)
    assert [x["audit_id"] for x in итог["outside"]] == ["aaa-свежий", "zzz-старый"]
