# -*- coding: utf-8 -*-
"""Регресс подбора: один механизм на все случаи — есть готовый агент или нет.

05.10 владелец попросил прогнать всю цепочку подбора и сказал, в чём подозревает беду: «кажется, у
нас два механизма подбора, которые конфликтуют при вызове». Подозрение подтвердилось, и регресс на
проде показал три настоящих дефекта:

1. КРУГ. План для решения строился С ПОДСКАЗКАМИ от похожих агентов: агент «ADR» похож на задачу →
   его навык adr-writer получал прибавку → возглавлял план → агент «покрывает план» → запускаем ADR
   на разбор идеи сервиса. Подсказка подтверждала сама себя, и правка подбора этого не перебивала.
2. ДУБЛИКАТЫ. Покрытие требовало строгого вложения: агент аудита 1С умел два навыка из трёх шагов
   плана и объявлялся «делающим другое» — человеку предлагали собрать второго такого же агента.
3. ПЕРВЫЙ ≠ УМЕЮЩИЙ. Решение смотрело только на самого похожего агента, хотя закрыть задачу мог
   второй или третий по похожести.

Здесь это закреплено на игрушечном каталоге: правила видны, а не угадываются по живому стенду.
"""
from __future__ import annotations

from server import orchestrator

CATALOG = {
    "idea-scorer": {"title": "Оценка идеи"},
    "adr-writer": {"title": "ADR"},
    "audit-extract": {"title": "Извлечение"},
    "audit-graph": {"title": "Граф"},
    "audit-checks": {"title": "Проверки"},
}


def _plan(*skills, runner_up=None, scores=None):
    sc = scores or {}
    return {"ok": True, "steps": [{"skill": s, "title": s, "score": sc.get(s, 0.5)} for s in skills],
            "runner_up": runner_up or {}}


def test_агент_запускается_если_умеет_большую_часть_плана():
    """Дубликаты: строгое «умеет ВСЁ» заставляло собирать второго такого же агента."""
    plan = _plan("audit-extract", "audit-graph", "audit-checks")
    matches = [{"id": "audit.v11", "name": "Агент · audit", "score": 0.78,
                "skills": ["audit-extract", "audit-checks"]}]
    d = orchestrator.decide("проведи аудит данных 1С", catalog=CATALOG, entities=set(), slots=set(),
                            matches=matches, plan=plan)
    assert d.kind == "agent" and d.agent_id == "audit.v11", d.why
    assert d.gaps == ["audit-graph"], "не названо, чего агент из плана НЕ делает"
    assert d.alt_skills == ["audit-extract", "audit-graph", "audit-checks"], \
        "вторая возможность (собрать из навыков) потеряна — человеку не из чего выбирать"


def test_агент_не_умеющий_главного_не_исполнитель():
    """Половина плана без главного шага — это не исполнитель, а похожий по словам сосед."""
    plan = _plan("idea-scorer", "adr-writer")
    matches = [{"id": "adr.v1", "name": "ADR", "score": 0.85, "skills": ["adr-writer"]}]
    d = orchestrator.decide("проанализируй идею сервиса", catalog=CATALOG, entities=set(), slots=set(),
                            matches=matches, plan=plan)
    assert d.kind == "build", d.why
    assert d.alt_agent.get("id") == "adr.v1", "близкий агент не предложен как альтернатива"


def test_выбирают_не_самого_похожего_а_умеющего():
    """Первый по похожести умеет половину, третий — всё. Похожесть ранжирует, решает умение."""
    plan = _plan("audit-extract", "audit-checks")
    matches = [
        {"id": "manager.v3", "name": "Мой менеджер-агент", "score": 0.85, "skills": ["audit-extract"]},
        {"id": "other.v1", "name": "Другой", "score": 0.80, "skills": []},
        {"id": "audit.v11", "name": "Агент · audit", "score": 0.73,
         "skills": ["audit-extract", "audit-checks"]},
    ]
    d = orchestrator.decide("проведи аудит", catalog=CATALOG, entities=set(), slots=set(),
                            matches=matches, plan=plan)
    assert d.agent_id == "audit.v11", f"выбран самый похожий, а не умеющий: {d.agent_name} · {d.why}"
    assert d.facts.get("покрытие_агента") == 1.0


def test_приветствие_не_превращается_в_допрос():
    """«привет, как дела?» — это реплика. Вопрос «что нужно сделать?» в ответ выглядит грубо."""
    for t in ("привет", "Привет, как дела?", "спасибо!", "ок"):
        d = orchestrator.decide(t, catalog=CATALOG, entities=set(), slots=set(), matches=[])
        assert d.kind == "chat", f"«{t}» → {d.kind}: {d.why}"


def test_благодарность_с_задачей_остаётся_задачей():
    """«спасибо, а теперь сверь проводки…» — работа, а не болтовня: длину проверяем не зря."""
    t = "спасибо, а теперь сверь проводки с первичкой за квартал и выпиши расхождения"
    plan = _plan("audit-extract", "audit-checks")
    d = orchestrator.decide(t, catalog=CATALOG, entities=set(), slots=set(), matches=[], plan=plan)
    assert d.kind == "build", d.why


def test_решение_всегда_несёт_вторую_возможность():
    """Главное требование владельца: один механизм. Что бы ни решил оркестратор, человек видит и
    вторую дорогу — запустить готового или собрать из навыков."""
    plan = _plan("audit-extract", "audit-checks")
    agent = orchestrator.decide("аудит", catalog=CATALOG, entities=set(), slots=set(),
                                matches=[{"id": "a.v1", "name": "A", "score": 0.9,
                                          "skills": ["audit-extract", "audit-checks"]}], plan=plan)
    build = orchestrator.decide("аудит", catalog=CATALOG, entities=set(), slots=set(),
                                matches=[{"id": "b.v1", "name": "B", "score": 0.9,
                                          "skills": ["что-то-другое"]}], plan=plan)
    assert agent.kind == "agent" and agent.alt_skills, "у решения «агент» нет пути к сборке"
    assert build.kind == "build" and build.alt_agent, "у решения «сборка» нет пути к агенту"
    assert "alt_skills" in agent.as_dict() and "alt_agent" in build.as_dict(), \
        "вторая возможность не доезжает до интерфейса"
