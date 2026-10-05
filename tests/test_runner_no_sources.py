# -*- coding: utf-8 -*-
"""Навык без источников: работает по знаниям и говорит об этом — а не выдаёт воду.

05.10 владелец разобрал отчёт по оценке идеи и назвал дыру: «модель опирается на внутренние
документы, которые должны передаваться приложением к оцениваемой идее либо ссылкой на S3/вики; если
таких ссылок нет и данные не найдены, модель в вакууме не может грамотно оценить идею и не использует
свой внутренний корпус знаний для формирования предложений, пусть и со ссылкой, что это основано на
её знаниях».

Так и было: промпт в ЛЮБОМ случае требовал «только из ДАННЫХ и норм, ничего не выдумывай» и ссылку
на id записи с суммой. Навыку без данных (оценка идеи, адвокат дьявола, ревью спецификации) запрещали
пользоваться собственными знаниями и не давали ничего взамен — оставалась вода.

Правила теперь такие: нет источников → работаем по приложенному материалу, общие знания разрешены
ЯВНО с пометкой, выдумывать id/суммы/цитаты нельзя, и навык обязан назвать, чего не хватило. Отчёт
об этом сообщает в строке источников, а не молчит.
"""
from __future__ import annotations

import asyncio
import json

from server import runner


def _agent(skill):
    return {"id": "ag-src", "name": "Разбор", "family": "product", "graph": {
        "nodes": [{"id": "n1", "kind": "skill", "skill": skill}], "edges": []}}


def _run(skill, chat_fn, *, data=None, sources=None, user_context="оцени идею сервиса"):
    contract = {"audit_id": "t-src", "autonomy_level": "A1", "criticality": "T3", "metrics": {}}
    return asyncio.run(runner.run_live(
        _agent(skill), contract, lambda sid: {"mode": "read", "egress": "internal", "cite": False},
        data_query=lambda e, **kw: (data or {}).get(e, []),
        skill_sources=lambda sid: (sources or []),
        load_body=lambda sid: "методика " + sid,
        chat_fn=chat_fn,
        skill_schemas={skill: {"instruction": "разбери", "template_id": skill}},
        user_context=user_context))


def _grab(box):
    async def chat_fn(messages=None, **kw):
        box.append(messages[-1]["content"])
        return {"text": json.dumps({"находки": [{"наблюдение": "вывод"}], "итог": "готово"},
                                   ensure_ascii=False),
                "model": "local/test", "input_tokens": 5, "output_tokens": 5}
    return chat_fn


def test_без_источников_знания_разрешены_явно():
    box: list = []
    _run("idea-scorer", _grab(box))
    p = box[0]
    assert "ПОЛЬЗУЙСЯ СВОИМИ ЗНАНИЯМИ" in p, "навыку по-прежнему нечем работать в вакууме"
    assert "(по общим знаниям модели)" in p, "не требуется помечать, что это знания, а не факт"
    assert "не придумывай id записей" in p, "запрет на выдумку id и сумм снят вместе с запретом знаний"
    assert "какие документы или ссылки" in p, "навык не просит приложить источник"


def test_с_данными_правила_прежние():
    """Там, где данные есть, ничего не меняется: выводы только из них."""
    box: list = []
    _run("idea-scorer", _grab(box), data={"doc1c": [{"id": "d1", "тип": "Реализация"}]},
         sources=[{"entity": "doc1c"}])
    p = box[0]
    assert "ПОЛЬЗУЙСЯ СВОИМИ ЗНАНИЯМИ" not in p, "в режиме данных знания модели разрешать нельзя"
    assert "ничего не выдумывай" in p


def test_режим_виден_в_результате_навыка():
    res = _run("idea-scorer", _grab([]))
    outs = [f for f in (res.get("findings") or []) if isinstance(f, dict)]
    assert outs and outs[0].get("sources_mode") == "knowledge", "режим не доехал до результата"
    assert outs[0].get("material") is True, "приложенный материал не отмечен"


def test_прогон_предупреждает_один_раз_за_всех():
    res = _run("idea-scorer", _grab([]))
    warn = [b for b in (res.get("board") or []) if b.get("kind") == "warning"
            and "не предоставлено" in str(b.get("text") or "")]
    assert warn, "в прогоне нет предупреждения об отсутствии источников"
    assert "idea-scorer" in warn[0]["text"], "не названо, чьи выводы стоят на знаниях"
    assert (res.get("run_metrics") or {}).get("knowledge_mode") == ["idea-scorer"]


def test_строка_источников_не_молчит():
    """Документ без строки источника выглядит так же убедительно, как собранный по выгрузке из 1С."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "server" / "web_api.py").read_text(encoding="utf-8")
    i = src.index("def _sources_html")
    body = src[i:i + 2200]
    assert "knowledge_mode" in body, "строка источников не знает про работу без данных"
    assert "не предоставлены" in body, "отчёт молчит о том, что данных не было"
    assert "приложите" in body.lower(), "не сказано, что именно приложить"
