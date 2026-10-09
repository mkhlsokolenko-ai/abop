# -*- coding: utf-8 -*-
"""Кейс «карточка для чужой витрины»: проверки считает код, а метрика не льстит себе.

Этот стенд опаснее обычного демо: в нём легко получить красивую цифру. Если генератор данных и
движок проверок применяют одни и те же правила, выходит стопроцентная полнота — и демонстрация
начинает обещать то, чего на живом фиде не будет никогда. Первая версия стенда именно это и
показала: 44 из 44, ноль пропусков, ноль ложных.

Поэтому в данных намеренно живут три вещи, и тесты сторожат каждую:

1. **визуальная модерация** — отказы, которых в фиде не видно; агент их не предсказывает и в
   заслугу себе не пишет;
2. **непубличные правила витрины** — она разворачивает по чек-листу, которого нет в требованиях;
   это пропуски агента, и они показывают, что просить у витрины;
3. **нестрогая модерация** — иногда нарушение пропускают, и у агента появляется ложная тревога.

Поэтому полнота и точность обязаны быть меньше ста процентов. Тест на это и стоит.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ / "cli"))

import ape  # noqa: E402


@pytest.fixture(scope="module")
def стенд():
    """Генерируем данные тем же скриптом, что и на стенде, и подсовываем их ядру."""
    каталог = tempfile.mkdtemp(prefix="omninet-")
    r = subprocess.run([sys.executable, str(КОРЕНЬ / "ops" / "seed_omninet.py"), "--out", каталог],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]
    данные = {
        "feed_card": json.loads((pathlib.Path(каталог) / "feed_cards.json").read_text(encoding="utf-8")),
        "showcase_rule": json.loads((pathlib.Path(каталог) / "showcase_rules.json").read_text(encoding="utf-8")),
        "category_map": json.loads((pathlib.Path(каталог) / "category_map.json").read_text(encoding="utf-8")),
        "showcase_reject": json.loads((pathlib.Path(каталог) / "showcase_rejects.json").read_text(encoding="utf-8")),
    }
    было = ape.data_query
    ape.data_query = lambda entity, *a, **k: данные.get(entity, [])
    yield данные
    ape.data_query = было


def test_данные_воспроизводимы(стенд):
    """Повторный запуск генератора даёт те же цифры — иначе демо нельзя показывать дважды."""
    каталог = tempfile.mkdtemp(prefix="omninet-2-")
    subprocess.run([sys.executable, str(КОРЕНЬ / "ops" / "seed_omninet.py"), "--out", каталог],
                   capture_output=True, text=True, check=True)
    снова = json.loads((pathlib.Path(каталог) / "feed_cards.json").read_text(encoding="utf-8"))
    assert снова == стенд["feed_card"], "генератор недетерминирован: демо будет разным"


def test_нарушения_считает_код_с_доказательством(стенд):
    находки = ape.omninet_check_cards()
    assert находки, "на стенде нет ни одного нарушения — проверять нечего"
    for f in находки:
        assert f["доказательство"], f"находка без доказательства: {f}"
        assert f["чинится"] in ("из данных", "человеком"), f
        assert f["код"] in {"CATEGORY_UNMAPPED", "ATTR_MISSING", "UNIT_MISMATCH",
                            "MARKING_REQUIRED", "IMAGES_FEW", "TITLE_TOO_LONG",
                            "TITLE_FORBIDDEN"}, f


def test_метрика_не_обещает_стопроцентной_точности(стенд):
    """Главный тест файла: стенд не вправе показывать идеальные числа."""
    с = ape.omninet_scorecard()
    assert 0 < с["полнота_%"] < 100, f"полнота {с['полнота_%']}% — стенд льстит себе"
    assert 0 < с["точность_%"] < 100, f"точность {с['точность_%']}% — стенд льстит себе"
    assert с["пропущено_отказов"] > 0, "нет пропусков: генератор и движок применяют одни правила"
    assert с["ложных_тревог"] > 0, "нет ложных тревог: витрина в данных строже, чем бывает живая"


def test_пропуски_разобраны_по_причинам(стенд):
    """Пропуск с повторяющейся причиной — это запрос к витрине, а не дефект модели."""
    с = ape.omninet_scorecard()
    assert с["пропущено_по_кодам"], "промахи не разобраны по кодам — сводка бесполезна"
    самая_частая = max(с["пропущено_по_кодам"].values())
    assert самая_частая >= 3, "в данных нет систематической причины пропусков — кейс выхолощен"


def test_визуальная_модерация_не_записана_в_вину(стенд):
    """Отказы, невыводимые из фида, считаются отдельно и не портят полноту."""
    с = ape.omninet_scorecard()
    assert с["вне_данных_фида"] > 0, "в данных нет непредсказуемых отказов — стенд слишком удобен"
    отклонено, вне = с["отклонено_витриной"], с["вне_данных_фида"]
    знаменатель = round(100 * с["поймано_отказов"] / (отклонено - вне))
    assert с["полнота_%"] == знаменатель, "полнота считается по предсказуемым отказам, а не по всем"
    assert "вне данных фида" in с["оговорка"]


def test_прирост_ftr_подан_как_оценка_сверху(стенд):
    с = ape.omninet_scorecard()
    assert с["ftr_если_чинить_до_отправки_%"] > с["ftr_сейчас_%"], "кейс не показывает выгоды"
    assert с["ftr_если_чинить_до_отправки_%"] < 100, "обещать стопроцентную приёмку нельзя"


def test_пустой_стенд_говорит_что_он_пуст():
    """Незасеянный стенд обязан сказать «нет данных», а не показать ноль как результат."""
    было = ape.data_query
    ape.data_query = lambda *a, **k: []
    try:
        assert "нет_данных" in ape.omninet_scorecard()
    finally:
        ape.data_query = было


def test_демо_пакет_на_месте():
    """`/api/demo/prepare` ждёт пару контракт + тело агента: без них «Запустить» ничего не делает."""
    пакет = КОРЕНЬ / "demo" / "omninet"
    контракт = json.loads((пакет / "contract_omninet.json").read_text(encoding="utf-8"))
    тело = json.loads((пакет / "agent_body.json").read_text(encoding="utf-8"))
    assert контракт["capability_request"]["audit_id"] == тело["contract_audit_id"]
    навыки = {s["id"] for s in контракт["capability_request"]["skills"]}
    узлы = {n["skill"] for n in тело["graph"]["nodes"] if n.get("skill")}
    assert узлы == навыки, "граф агента и контракт разошлись по составу навыков"
    assert навыки <= set(ape.SKILLS), "в контракте навык, которого нет в каталоге"
    наружу = [n for n in тело["graph"]["nodes"] if n.get("kind") == "out"]
    assert наружу and наружу[0]["out"]["hitl"] is True, "отправка наружу без подтверждения человека"


def test_контракт_проходит_приёмку():
    """Contract Ingress проверяет провенанс на границе: ссылка ev:<10hex> обязана совпадать с
    первыми знаками пакета свидетельств. Пакет, который не принимается, превращает кнопку
    «Собрать и запустить» в аккуратный 422 — проверено на проде."""
    sys.path.insert(0, str(КОРЕНЬ))
    from server import ingress

    пакет = json.loads((КОРЕНЬ / "demo" / "omninet" / "contract_omninet.json")
                       .read_text(encoding="utf-8"))
    r = ingress.validate(пакет)
    assert r.accepted, f"контракт кейса не проходит приёмку: {r.errors}"


def test_рецепты_покрывают_все_сущности_кейса():
    рецепты = sorted((КОРЕНЬ / "demo" / "omninet" / "recipes").glob("*.json"))
    сущности = {json.loads(p.read_text(encoding="utf-8"))["entity"] for p in рецепты}
    assert сущности == {"feed_card", "showcase_rule", "category_map", "showcase_reject"}
    for p in рецепты:
        r = json.loads(p.read_text(encoding="utf-8"))
        assert r["source"]["kind"] == "json" and r["source"]["path"], p.name
        assert ape.data_schema(r["entity"])["known"], f"{r['entity']} нет в реестре схем"
