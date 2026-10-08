# -*- coding: utf-8 -*-
"""Блок «Немедленно» из docs/ISSLEDOVANIE_VEB_DESKTOP_2026-10-08.md §10.

Двенадцать правок одной темы: интерфейс перестаёт обещать то, чего не делает. Каждая закрывает
место, где кнопка рапортовала об успехе, число бралось из демо-константы или признак отсутствовал
вовсе. Тесты статические там, где речь о разметке, и живые там, где речь о форме ответа.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"
os.environ["ABOP_RUN_WORKERS"] = "0"

from server import rbac_map, web_api  # noqa: E402

ШАБЛОН = (КОРЕНЬ / "webapp" / "src" / "template.html").read_text(encoding="utf-8")
# Правила проверяем ПО КОДУ: честный комментарий, объясняющий, что убрано и почему, не должен
# ронять тест — приём тот же, что в tests/test_ops_lens_contract.py.
_СТРОКИ = ШАБЛОН.splitlines()
КОД = chr(10).join(x for x in _СТРОКИ if not x.lstrip().startswith("//"))
АДМИН = (КОРЕНЬ / "webapp" / "src" / "components" / "AdminScreens.dc.html").read_text(encoding="utf-8")
ЯДРО = (КОРЕНЬ / "desktop" / "ui" / "core" / "app.js").read_text(encoding="utf-8")
АГЕНТЫ = (КОРЕНЬ / "desktop" / "ui" / "modules" / "agents" / "panel.js").read_text(encoding="utf-8")
ПРОГОНЫ = (КОРЕНЬ / "desktop" / "ui" / "modules" / "runs" / "panel.js").read_text(encoding="utf-8")


@pytest.fixture()
def client():
    with TestClient(web_api.app) as c:
        yield c


# ── №1. Матрица прав выводится из гейтов, а не из прозы о них ────────────────────────────────────

def test_карта_прав_собрана_из_кода():
    """Главное: разрешения выводятся разбором require_level в исходнике, а не словарём рядом."""
    карта = rbac_map.собрать()
    assert карта["total"] > 100, "роуты не нашлись — разбор сломан"
    assert карта["total"] - карта["open"] > 20, "гейты не нашлись"
    assert карта["source"].startswith("ast:"), "источник не назван"


def test_шкала_накопительная():
    """admin проходит и те гейты, что объявлены для manager, — матрица обязана это показывать."""
    пары = [(lv, rbac_map.сводка(lv)["allowed_count"]) for lv in rbac_map.УРОВНИ]
    числа = [n for _, n in пары]
    assert числа == sorted(числа), "уровни не по возрастанию прав: " + str(пары)
    assert rbac_map.сводка("analyst")["allowed_count"] == 0, "аналитику разрешены мутации"
    assert rbac_map.сводка("admin")["denied_count"] == 0, "администратору что-то запрещено"


def test_разбор_устойчив():
    """Разбор идёт по AST и не зависит от порядка чтения: повтор даёт тот же результат."""
    rbac_map._сброс_для_тестов()
    до = rbac_map.собрать()["total"]
    rbac_map._сброс_для_тестов()
    assert rbac_map.собрать()["total"] == до, "разбор неустойчив"


def test_ложной_кнопки_применить_rbac_больше_нет():
    """Кнопка показывала «RBAC применён · запись в аудит ИБ», а ручки на запись не существует."""
    assert "saveRbac" not in АДМИН, "кнопка «Применить» осталась в разметке"
    assert "RBAC применён" not in КОД, "ложный тост об успехе остался"
    assert "rbacEditNote" in ШАБЛОН and "rbacEditNote" in АДМИН, "нет честной подписи «только чтение»"


def test_ответ_rbac_называет_нередактируемость(client):
    r = client.get("/api/admin/rbac")
    assert r.status_code == 200
    d = r.json()
    assert d.get("editable") is False, "ответ не говорит, что матрица не правится"
    assert "enforcement" in d and d["enforcement"]["routes_gated"] > 0, "нет фактических чисел"


# ── №2. Демо-подмены на экране «Безопасность» помечены ───────────────────────────────────────────

def test_подзаголовок_безопасности_не_врёт_про_живые_данные():
    assert "живые данные Keycloak/Redmine" not in ШАБЛОН, "обещание «живые данные» безусловно"
    assert "secSourceNote" in ШАБЛОН, "источник данных не называется"


def test_счётчик_нарушений_не_из_демо_константы():
    блок = КОД[КОД.index("const secViolations"):][:320]
    assert "REATTEST" not in блок, "бейдж нарушений снова считается от демо-данных"
    assert "adminAudit" in блок, "бейдж не опирается на журнал аудита"


# ── №3. Шина: «чисто» отличимо от «нет шины» ─────────────────────────────────────────────────────

def test_dlq_называет_драйвер(client):
    r = client.get("/api/bus/dlq")
    assert r.status_code == 200
    d = r.json()
    assert "bus" in d and "bus_active" in d, "ответ DLQ не называет драйвер"
    if not d["bus_active"]:
        assert d["bus_note"], "шина выключена, но об этом не сказано"


def test_хвост_топика_тоже_называет_драйвер(client):
    r = client.get("/api/bus/tail?topic=abop.dlq")
    assert r.status_code == 200
    assert "bus_active" in r.json()


# ── №6. Лейбл метрики — шаблон роута ─────────────────────────────────────────────────────────────

def test_метрика_не_копит_серии_на_каждый_идентификатор(client):
    """Сырой путь давал отдельную серию на каждый run_id в словаре без ограничения размера."""
    for rid in ("run-a-111", "run-b-222", "run-c-333"):
        client.get("/api/runs/" + rid)
    текст = client.get("/metrics").text
    assert "run-a-111" not in текст and "run-c-333" not in текст, "идентификатор попал в лейбл"


# ── №4, №5, №7. Десктоп ──────────────────────────────────────────────────────────────────────────

def test_видимость_модулей_по_шкале_abop():
    """Роли lecturer в ABOP нет: «Граф» был скрыт от manager и support молча."""
    ядро_код = chr(10).join(x for x in ЯДРО.splitlines() if not x.lstrip().startswith("//"))
    assert "lecturer" not in ядро_код, "видимость модулей снова завязана на роль чужого realm"
    assert "MODULE_MIN_LEVEL" in ЯДРО and "levelAtLeast" in ЯДРО, "нет шкалы уровней"
    блок = ядро_код[ядро_код.index("MODULE_MIN_LEVEL"):][:200]
    assert "graphlens" in блок and "manager" in блок, "«Граф» не открыт менеджеру"


def test_архив_можно_открыть_и_вернуть_агента():
    """Диалог архивации обещает «вернуть можно в любой момент» — теперь это выполнимо."""
    assert 'tabBtn("arch"' in АГЕНТЫ, "вкладки «Архив» нет"
    assert "restoreAgent" in АГЕНТЫ and "/restore/" in АГЕНТЫ, "возврат не вызывается"
    assert "archived=1" in АГЕНТЫ, "архив не запрашивается"


def test_санитайзер_один_на_приложение():
    """Их было два, и слабый пропускал onclick в одинарных кавычках."""
    assert "export function sanitizeHtml" in ЯДРО, "общего санитайзера нет"
    assert "sanitizeHtml" in ЯДРО[ЯДРО.index("export const ctx"):][:400], "не отдан панелям через ctx"
    assert "ctx.sanitizeHtml" in ПРОГОНЫ, "журнал прогонов чистит HTML своей копией"


# ── №8–№12. Веб: действия с последствиями ────────────────────────────────────────────────────────

def test_остановка_прогона_отменяет_задание():
    блок = КОД[КОД.index("confirmStop:"):][:600]
    assert "cancelRunJob" in блок, "кнопка снова только меняет локальный флаг"
    assert "аудит-след сохранён" not in блок, "обещание записи в аудит вернулось"


def test_провенанс_не_выбирается_регуляркой():
    """ADR-016: каждое число с cite. Цепочку нельзя подбирать по тексту события."""
    блок = КОД[КОД.index("hasLineage: !!e.lineage_id") - 400:][:700]
    assert "ИНН" not in блок, "цепочка снова выбирается регуляркой по тексту"
    assert "e.lineage_id" in блок, "ссылка не опирается на собственную цепочку события"


def test_подтверждение_hitl_без_демо_ветки():
    блок = КОД[КОД.index("approveHitl:"):][:900]
    assert "Демо-симуляция" not in блок, "«Разрешить» снова иногда ничего не разрешает"


def test_полоса_где_идёт_только_на_настоящем_прогоне():
    assert "hasRunTrace" in ШАБЛОН, "полоса не загейтована"
    блок = КОД[КОД.index("hasRunTrace:"):][:260]
    assert "isRealRun" in блок, "гейт не проверяет, что прогон настоящий"


def test_состояние_талисмана_существует():
    """У Эйпа нет состояния think — работающий экземпляр рисовался бездействующим."""
    assert "ape: 'think'" not in ШАБЛОН, "несуществующее состояние вернулось"
    assert "ape: 'thinking'" in ШАБЛОН


def test_цвет_отказа_модели_определён():
    assert "--danger-ink:" in ШАБЛОН, "переменная используется, но не определена"
    assert ШАБЛОН.count("--danger-ink:") >= 2, "нет значения для светлой темы"
