# -*- coding: utf-8 -*-
"""Канва «Строю» показывает граф агента, а не выдуманную цепочку.

До этого открытие контракта строило линейный путь «источник → навыки → результат» из
`intake.skills`. Для агента ОДДС это неправда: у него шесть источников (движения, справочник
строк формы, правила ПБУ 23/2011, правила назначения платежа, остатки, раскладка), один навык и
выход в почту с подтверждением. Канва, рисующая процесс, которого нет, хуже пустой: по ней
нельзя понять, откуда агент берёт данные и куда уходит отчёт.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
ШАБЛОН = io.open(КОРЕНЬ / "webapp" / "src" / "template.html", encoding="utf-8").read()
ЛИНЗА = io.open(КОРЕНЬ / "webapp" / "src" / "components" / "GraphLens.dc.html", encoding="utf-8").read()
ТЕЛО_ОДДС = json.load(io.open(КОРЕНЬ / "demo" / "oddc" / "agent_body.json", encoding="utf-8"))


def test_канва_берёт_граф_агента():
    assert "loadAgentGraphToCanvas" in ШАБЛОН
    блок = ШАБЛОН[ШАБЛОН.index("  loadAgentGraphToCanvas(agentId, auditId) {"):][:2200]
    assert "/api/agents/" in блок, "граф агента не запрашивается"
    assert "if (!gn.length) return" in блок, "пустой граф затрёт цепочку контракта"
    assert "layoutFlow" in блок and "canvasNodes" in блок
    # Узел должен сам рассказывать, что он: сущность у источника, канал у выхода.
    assert "n.entity" in блок and "out.channel" in блок


def test_открытие_контракта_зовёт_граф_агента():
    i = ШАБЛОН.index("  openContractInCanvas(auditId) {")
    блок = ШАБЛОН[i:i + 4000]
    assert "loadAgentGraphToCanvas(ags[0].id" in блок, "контракт открывается без графа агента"


def test_граф_агента_оддс_полный():
    """Тело агента в репозитории — то, что ставится на прод: источники, навык и выход."""
    узлы = ТЕЛО_ОДДС["graph"]["nodes"]
    виды = [n["kind"] for n in узлы]
    assert виды.count("source") == 6, виды
    assert виды.count("skill") == 1 and виды.count("out") == 1
    источники = {n["entity"] for n in узлы if n["kind"] == "source"}
    assert источники == {"cash_movement", "oddc_line", "oddc_rule", "payment_purpose_rule",
                         "cash_balance", "oddc_report_line"}, источники
    навык = [n for n in узлы if n["kind"] == "skill"][0]
    assert навык["skill"] == "oddc-build" and навык["hitl"] is True
    выход = [n for n in узлы if n["kind"] == "out"][0]["out"]
    # Отправка наружу без подтверждения человеком — не демо, а инцидент.
    assert выход["run"] is False and выход["hitl"] is True
    assert выход["format"] == "pdf" and выход["channel"] in ("yandex", "email")
    связи = {(e["from"], e["to"]) for e in ТЕЛО_ОДДС["graph"]["edges"]}
    assert ("oddc-build", "out_mail") in связи
    assert all((n["id"], "oddc-build") in связи for n in узлы if n["kind"] == "source")


def test_на_канве_есть_вход_в_агентов_платформы():
    """Агент с графом на сервере был неоткрываем: выбор знал только демо-сценарии и черновики."""
    assert "{{ gl.serverAgents }}" in ЛИНЗА and "АГЕНТЫ ПЛАТФОРМЫ" in ЛИНЗА
    assert "{{ gl.hasServerAgents }}" in ЛИНЗА, "раздел должен скрываться, когда агентов нет"
    assert "serverAgents: (s.agentsList || [])" in ШАБЛОН, "список берётся из /api/agents"
    блок = ШАБЛОН[ШАБЛОН.index("  openAgentOnCanvas(a) {"):][:700]
    assert "loadAgentGraphToCanvas(a.id" in блок, "открывается не сохранённый граф агента"
    assert "canvasAgentId" in блок and "lens: 'build'" in блок


def test_заголовок_канвы_называет_открытого_агента():
    """Иначе на экране остаётся имя демо-сценария, а граф уже другой — это и читается как «агента нет»."""
    i = ШАБЛОН.index("canvasTitle: s.canvasAgentId")
    блок = ШАБЛОН[i:i + 320]
    assert "canvasAgentName" in блок
    # Выбор демо-сценария снимает открытого агента: заголовок не должен врать в обратную сторону.
    j = ШАБЛОН.index("scenarioTabs: Object.keys(this.allScenarios())")
    assert "canvasAgentId: null" in ШАБЛОН[j:j + 420]


def test_статус_агента_виден_в_выборе():
    """«Черновик» и «в проде» — разные вещи; человек должен видеть, что именно открывает."""
    i = ШАБЛОН.index("serverAgents: (s.agentsList || [])")
    блок = ШАБЛОН[i:i + 700]
    assert "в проде" in блок and "черновик" in блок
