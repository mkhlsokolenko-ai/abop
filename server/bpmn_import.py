# -*- coding: utf-8 -*-
"""Импорт BPMN 2.0 в карту процессов ABOP (п.20 исследования 08.10).

**Зачем.** Карта процессов — главное отличие ABOP, но начинать её с чистого листа согласится не
каждый: у организаций, которым продукт адресован, процессы уже нарисованы — в Camunda, Signavio,
Bizagi, ARIS. BPMN 2.0 XML выгружают все. Импорт превращает «нарисуйте мне вашу организацию
заново» в «покажите, что у вас есть, и давайте посадим агентов».

**Что переносим и почему именно это.** BPMN богаче нашей модели, и переносить его целиком
бессмысленно: ABOP отвечает не «как исполнять процесс», а «кто из агентов на каком шаге работает
и правда ли работает». Поэтому берём ровно скелет:

* `process` → подпроцесс карты, `subProcess` → вложенный подпроцесс;
* `task` и все его виды (`userTask`, `serviceTask`, `scriptTask`…) → операция, на которую можно
  посадить агента. `userTask` особый: это шаг человека, и он становится `hitl`;
* `startEvent` / `endEvent` / промежуточные события → `event`;
* `exclusiveGateway` и прочие шлюзы → `gateway`;
* `sequenceFlow` → ребро; `conditionExpression` на нём делает ребро условным (`cond`);
* `timerEventDefinition` → `wait`.

Исполнительная часть BPMN — выражения, слушатели, формы, пулы и дорожки — сознательно
отбрасывается. Пулы и дорожки это про то, КТО исполняет; у нас на этот вопрос отвечает посадка
агента, и держать два ответа рядом значит обречь их разойтись.

**Чистая функция.** Разбор не ходит в базу и ничего не применяет: отдаёт дерево и перечень того,
что отброшено. Применяет это обычная редакция карты — импорт не должен быть вторым путём записи
мимо согласования.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

BPMN = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"

# Локальное имя элемента BPMN → вид узла карты. Всё, чего здесь нет, в карту не попадает и
# перечисляется в отчёте: молча терять элементы чужой схемы нельзя.
ВИДЫ = {
    "task": "task", "serviceTask": "task", "scriptTask": "task", "businessRuleTask": "task",
    "sendTask": "task", "receiveTask": "task", "manualTask": "task", "callActivity": "task",
    "userTask": "hitl",
    "startEvent": "event", "endEvent": "event",
    "intermediateCatchEvent": "event", "intermediateThrowEvent": "event",
    "boundaryEvent": "event",
    "exclusiveGateway": "gateway", "inclusiveGateway": "gateway",
    "parallelGateway": "gateway", "eventBasedGateway": "gateway",
    "complexGateway": "gateway",
}
КОНТЕЙНЕРЫ = ("process", "subProcess", "transaction", "adHocSubProcess")


class НеBPMN(ValueError):
    """Файл не похож на BPMN 2.0 — лучше сказать это сразу, чем импортировать пустоту."""


def _локальное(тег: str) -> str:
    return тег.rsplit("}", 1)[-1]


def _ид(эл, запас: str) -> str:
    """Идентификатор узла: берём из BPMN, но приводим к безопасному для карты виду."""
    сырой = (эл.get("id") or "").strip() or запас
    чистый = re.sub(r"[^A-Za-z0-9_.:-]", "_", сырой)
    return чистый[:120] or запас


def _имя(эл, вид: str, номер: int) -> str:
    имя = (эл.get("name") or "").strip()
    if имя:
        return re.sub(r"\s+", " ", имя)[:200]
    # Безымянные элементы в BPMN — норма (шлюзы, события). Подпись всё равно нужна человеку.
    подпись = {"gateway": "развилка", "event": "событие", "hitl": "шаг человека",
               "wait": "ожидание"}.get(вид, "операция")
    return f"{подпись} {номер}"


def _таймер(эл) -> bool:
    return any(_локальное(x.tag) == "timerEventDefinition" for x in эл)


def разобрать(xml: str | bytes, *, org_label: str = "Организация") -> dict:
    """BPMN 2.0 XML → {tree, stats, skipped}. Ничего не записывает."""
    try:
        корень = ET.fromstring(xml)
    except ET.ParseError as ex:
        raise НеBPMN(f"не удалось прочитать XML: {ex}") from ex
    if _локальное(корень.tag) != "definitions":
        raise НеBPMN("корневой элемент не <definitions>: это не BPMN 2.0")

    процессы = [x for x in корень if _локальное(x.tag) in КОНТЕЙНЕРЫ]
    if not процессы:
        raise НеBPMN("в файле нет ни одного <process>")

    пропущено: dict = {}
    счётчик = {"n": 0}

    def разобрать_контейнер(эл, вид_контейнера: str) -> dict:
        счётчик["n"] += 1
        узел = {"id": _ид(эл, f"c{счётчик['n']}"), "kind": вид_контейнера,
                "label": _имя(эл, "process", счётчик["n"]), "children": [], "edges": []}
        рёбра_по_ид: dict = {}
        for ребёнок in эл:
            имя_тега = _локальное(ребёнок.tag)
            if имя_тега in КОНТЕЙНЕРЫ:
                узел["children"].append(разобрать_контейнер(ребёнок, "subprocess"))
                continue
            if имя_тега == "sequenceFlow":
                рёбра_по_ид[ребёнок.get("id") or ""] = ребёнок
                continue
            вид = ВИДЫ.get(имя_тега)
            if вид is None:
                if имя_тега not in ("extensionElements", "documentation", "ioSpecification",
                                    "laneSet", "incoming", "outgoing"):
                    пропущено[имя_тега] = пропущено.get(имя_тега, 0) + 1
                continue
            счётчик["n"] += 1
            if вид == "event" and _таймер(ребёнок):
                вид = "wait"
            узел["children"].append({"id": _ид(ребёнок, f"n{счётчик['n']}"), "kind": вид,
                                     "label": _имя(ребёнок, вид, счётчик["n"])})
        свои = {c["id"] for c in узел["children"]}
        for поток in рёбра_по_ид.values():
            _чист = lambda v: re.sub(r"[^A-Za-z0-9_.:-]", "_", v or "")[:120]  # noqa: E731
            откуда, куда = _чист(поток.get("sourceRef")), _чист(поток.get("targetRef"))
            if откуда in свои and куда in свои:
                тип = "cond" if any(_локальное(x.tag) == "conditionExpression"
                                    for x in поток) else "seq"
                узел["edges"].append({"from": откуда, "to": куда, "kind": тип})
            else:
                # Поток через границу контейнера: в нашей модели связи живут внутри уровня
                # (ADR-047), сквозная связь смешала бы масштабы. Теряем осознанно и говорим об этом.
                ключ = "sequenceFlow-через-границу"
                пропущено[ключ] = пропущено.get(ключ, 0) + 1
        return узел

    дерево = {"id": "org", "kind": "org", "label": org_label,
              "children": [разобрать_контейнер(p, "process") for p in процессы], "edges": []}

    def посчитать(узел: dict, счёт: dict) -> dict:
        счёт[узел.get("kind")] = счёт.get(узел.get("kind"), 0) + 1
        for c in (узел.get("children") or []):
            посчитать(c, счёт)
        return счёт

    статистика = посчитать(дерево, {})
    статистика["edges"] = sum(len(x.get("edges") or []) for x in _все(дерево))
    return {"tree": дерево, "stats": статистика,
            "skipped": [{"element": k, "count": v} for k, v in sorted(пропущено.items())]}


def _все(узел: dict) -> list:
    out = [узел]
    for c in (узел.get("children") or []):
        out += _все(c)
    return out


def правки_импорта(дерево: dict, *, под: str = "org") -> list[dict]:
    """Дерево импорта → пачка правок редакции.

    Импорт не пишет в карту напрямую: он собирает те же `add_node`/`add_edge`, что рисует рука.
    Иначе появился бы второй путь записи мимо согласования и мимо проверки прав на область —
    а редакция как подписываемый документ (ADR-042) и есть то, чем карта отличается от схемы
    в рисовалке.
    """
    правки: list[dict] = []

    def обойти(узел: dict, родитель: str) -> None:
        for ребёнок in (узел.get("children") or []):
            правки.append({"kind": "add_node", "target": родитель,
                           "payload": {"id": ребёнок["id"], "kind": ребёнок["kind"],
                                       "label": ребёнок["label"]}})
            обойти(ребёнок, ребёнок["id"])
        for e in (узел.get("edges") or []):
            правки.append({"kind": "add_edge", "target": e["from"],
                           "payload": {"to": e["to"], "kind": e.get("kind") or "seq"}})

    обойти(дерево, под)
    return правки
