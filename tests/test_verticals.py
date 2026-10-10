# -*- coding: utf-8 -*-
"""Вертикаль — единица поставки, а не папка по соглашению.

Шаг 4 плана «правила — данные». Раньше «поставить вертикаль» значило: принять контракт, сохранить
агента, залить рецепты, не забыть наборы правил — четыре операции руками, и ни одна не проверяла
остальные. Теперь это пакет `verticals/<id>/` с манифестом и одна установка.

Проверяется здесь не «ручка отвечает 200», а четыре обещания формата.

1. **Манифест не врёт о содержимом.** Люди читают пакет по манифесту, а ставят по файлам. Если
   они разошлись, заказчик получит не то, что прочитал, — и узнает об этом на прогоне.
2. **Пакет не привозит того, чего не несёт.** Навыки и адаптеры должны быть в системе заранее;
   пакет НЕ несёт чужой Python. Нет навыка — отказ установки, а не «установлено, но не работает».
3. **Установка не затирает работу заказчика.** Набор правил, который правил методолог, остаётся
   как есть, и это сказано в отчёте. Снять правку можно только явным запросом.
4. **Отчёт называет каждое действие.** Молча установка не делает ничего.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import pathlib
import sys

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"
os.environ["ABOP_RUN_WORKERS"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import rules_store, web_api  # noqa: E402
from server.routes import verticals as домен  # noqa: E402
from cli import ape  # noqa: E402

CLIENT = TestClient(web_api.app)
ПАКЕТЫ = КОРЕНЬ / "verticals"


@pytest.fixture(autouse=True)
def чистый_стор():
    rules_store._MEM.clear()
    rules_store.ЭФФЕКТИВНЫЕ.обновить({})
    ape.set_rule_store(rules_store.ЭФФЕКТИВНЫЕ)
    yield
    rules_store._MEM.clear()
    rules_store.ЭФФЕКТИВНЫЕ.обновить({})


# ── формат ────────────────────────────────────────────────────────────────────────────────────

def test_все_поставочные_пакеты_годны():
    """Поставка обязана быть годной к установке: иначе это не поставка, а черновик в репозитории."""
    д = CLIENT.get("/api/verticals").json()
    пакеты = {в["id"]: в for в in д["вертикали"]}
    assert {"omninet", "audit1c"} <= set(пакеты), пакеты.keys()
    плохие = {и: (в["ошибки"], в["чего_нет"]) for и, в in пакеты.items() if not в["годен"]}
    assert not плохие, плохие


def test_манифест_сверяется_с_содержимым_в_обе_стороны():
    """Ни забытый в манифесте файл, ни обещанный и отсутствующий не должны проходить."""
    путь = ПАКЕТЫ / "omninet"
    манифест = json.loads((путь / "vertical.json").read_text(encoding="utf-8"))

    забыли = copy.deepcopy(манифест)
    забыли["содержит"]["правила"] = []
    ошибки = домен._сверить(путь, забыли)
    assert any("showcase-b" in о and "не объявлен" in о for о in ошибки), ошибки

    обещали = copy.deepcopy(манифест)
    обещали["содержит"]["рецепты"] = обещали["содержит"]["рецепты"] + ["которого_нет"]
    ошибки = домен._сверить(путь, обещали)
    assert any("которого_нет" in о and "файла нет" in о for о in ошибки), ошибки


def test_id_манифеста_совпадает_с_папкой():
    путь = ПАКЕТЫ / "audit1c"
    манифест = json.loads((путь / "vertical.json").read_text(encoding="utf-8"))
    манифест["id"] = "чужой"
    assert any("не совпадает с папкой" in о for о in домен._сверить(путь, манифест))


def test_битый_набор_правил_не_даёт_пакету_быть_годным():
    """Набор с неразрешимой ссылкой — не «почти работает»: правила по нему молча не сработают."""
    путь = ПАКЕТЫ / "omninet"
    манифест = json.loads((путь / "vertical.json").read_text(encoding="utf-8"))
    было = домен._наборы
    домен._наборы = lambda p: {"showcase-b": {
        "id": "showcase-b", "предмет": "feed_card",
        "контекст": [{"имя": "правила", "из": "showcase_rule", "по": {"к": "$категория"}}],
        "правила": []}}
    try:
        ошибки = домен._сверить(путь, манифест)
    finally:
        домен._наборы = было
    assert any("$категория" in о for о in ошибки), ошибки


def test_требования_проверяются_по_факту():
    """«Требует навык X» — проверяемое утверждение: пакет навыки с собой не привозит."""
    for vid in ("omninet", "audit1c"):
        манифест = json.loads((ПАКЕТЫ / vid / "vertical.json").read_text(encoding="utf-8"))
        assert домен._чего_нет(манифест) == {"навыки": [], "адаптеры": [], "сущности": []}, vid
    нет = домен._чего_нет({"требует": {"навыки": ["выдуманный-навык"],
                                       "адаптеры": ["выдуманный"],
                                       "сущности": ["выдуманная"]}})
    assert нет == {"навыки": ["выдуманный-навык"], "адаптеры": ["выдуманный"],
                   "сущности": ["выдуманная"]}


def test_манифест_называет_метрику():
    """Пилот продаётся как измерение (docs/MONETIZATION.md) — пакет обязан сказать, чем мерить."""
    for vid in ("omninet", "audit1c"):
        м = json.loads((ПАКЕТЫ / vid / "vertical.json").read_text(encoding="utf-8")).get("метрика")
        assert м and м.get("как") and м.get("почему"), vid


# ── установка ─────────────────────────────────────────────────────────────────────────────────

def test_установка_отчитывается_за_каждое_действие():
    r = CLIENT.post("/api/verticals/omninet/install")
    assert r.status_code == 200, r.text
    д = r.json()
    по_виду = {}
    for строка in д["отчёт"]:
        по_виду.setdefault(строка["что"], []).append(строка)
        assert строка["итог"] in ("поставлен", "обновлён", "пропущен"), строка
    assert set(по_виду) == {"контракт", "правила", "рецепт", "агент"}, по_виду.keys()
    assert len(по_виду["рецепт"]) == 4 and len(по_виду["правила"]) == 1
    assert д["агент"] and д["audit_id"] == "omninet-showcase-2026"
    assert д["установлена"] is True


def test_повторная_установка_идемпотентна():
    первая = CLIENT.post("/api/verticals/omninet/install").json()
    вторая = CLIENT.post("/api/verticals/omninet/install").json()
    assert вторая["агент"] == первая["агент"], "повторная установка наплодила версий агента"
    контракт = [с for с in вторая["отчёт"] if с["что"] == "контракт"][0]
    assert контракт["итог"] == "пропущен" and "уже принят" in контракт["почему"]


def test_установка_не_затирает_правку_методолога():
    """Главное обещание: заказчик правил правило — переустановка пакета его не отменяет."""
    CLIENT.post("/api/verticals/omninet/install")
    набор = copy.deepcopy(CLIENT.get("/api/data/rules/showcase-b").json()["набор"])
    набор["правила"] = [п for п in набор["правила"] if п["код"] != "IMAGES_FEW"]
    assert CLIENT.put("/api/data/rules/showcase-b", json={"набор": набор}).status_code == 200

    д = CLIENT.post("/api/verticals/omninet/install").json()
    строка = [с for с in д["отчёт"] if с["что"] == "правила"][0]
    assert строка["итог"] == "пропущен" and "правка" in строка["почему"], строка
    действует = CLIENT.get("/api/data/rules/showcase-b").json()
    assert действует["правка"] is True and "IMAGES_FEW" not in действует["коды"], (
        "установка снесла правку методолога")


def test_правку_можно_снять_только_явным_запросом():
    CLIENT.post("/api/verticals/omninet/install")
    набор = copy.deepcopy(CLIENT.get("/api/data/rules/showcase-b").json()["набор"])
    набор["правила"] = []
    CLIENT.put("/api/data/rules/showcase-b", json={"набор": набор})

    д = CLIENT.post("/api/verticals/omninet/install",
                    json={"перезаписать_правки": True}).json()
    строка = [с for с in д["отчёт"] if с["что"] == "правила"][0]
    assert строка["итог"] == "обновлён" and "правка снята" in строка["почему"], строка
    действует = CLIENT.get("/api/data/rules/showcase-b").json()
    assert действует["правка"] is False and "IMAGES_FEW" in действует["коды"]


def test_установка_требует_прав_менеджера():
    слой = (КОРЕНЬ / "server" / "routes" / "verticals.py").read_text(encoding="utf-8")
    блок = слой[слой.index("async def vertical_install("):][:600]
    assert 'require_level(u, "manager")' in блок


def test_чужой_путь_не_открывается():
    """`vid` — имя папки, а не путь: подниматься по дереву нельзя."""
    for плохой in ("..", "../server", ".hidden"):
        r = CLIENT.get("/api/verticals/" + плохой)
        assert r.status_code in (404, 422), (плохой, r.status_code)


def test_нет_пакета_честный_404():
    r = CLIENT.get("/api/verticals/которой-нет")
    assert r.status_code == 404 and "нет пакета" in r.json()["detail"]


def test_прогон_после_установки_считает_по_набору_пакета():
    """Смысл установки: после неё прогон работает — а не «файлы разложены»."""
    CLIENT.post("/api/verticals/audit1c/install")
    данные = json.loads((КОРЕНЬ / "tests" / "fixtures" / "audit1c_dump.json")
                        .read_text(encoding="utf-8"))
    было = ape.data_query
    ape.data_query = lambda entity, *a, **k: данные.get(entity, [])
    try:
        находки = ape.прогнать_проверки("audit1c-base")
    finally:
        ape.data_query = было
    assert находки, "после установки набор пакета не применяется"
    assert {н["код"] for н in находки} <= {"A1", "B1", "B2", "B3", "B4", "B5", "D1", "D2"}


def test_пакеты_лежат_в_своём_каталоге_а_не_в_демо():
    """Вертикаль — поставка, демо-сценарий — показ. Путать их значит возить демо заказчику."""
    assert домен.КАТАЛОГ.name == "verticals"
    assert not list((КОРЕНЬ / "demo").glob("*/rules/*.json")), (
        "наборы правил остались в demo/ — поставка должна жить в verticals/")
    assert (КОРЕНЬ / "Dockerfile.webapi").read_text(encoding="utf-8").count("COPY verticals") == 1, (
        "образ не везёт пакеты вертикалей — на проде не будет ни правил, ни норм")


# ── интерфейс и CLI ───────────────────────────────────────────────────────────────────────────

ШАБЛОН = (КОРЕНЬ / "webapp" / "src" / "template.html").read_text(encoding="utf-8")
АДМИНКА = (КОРЕНЬ / "webapp" / "src" / "components" / "AdminScreens.dc.html").read_text(
    encoding="utf-8")


def test_вкладка_вертикалей_есть_в_админке():
    assert "'Вертикали': []" in ШАБЛОН, "вкладка не заведена в SETTINGS"
    assert "isVerticalsTab: s.settingsTab === 'Вертикали'" in ШАБЛОН
    assert "{{ adm.isVerticalsTab }}" in АДМИНКА, "нет раздела в вёрстке админки"
    assert "{{ adm.vtRows }}" in АДМИНКА


def test_интерфейс_показывает_отчёт_установки():
    """Установка меняет систему заказчика — он обязан видеть, что именно изменилось."""
    assert "{{ adm.vtReport }}" in АДМИНКА and "что сделала установка" in АДМИНКА
    блок = ШАБЛОН[ШАБЛОН.index("  installVertical(id, overwrite) {"):][:1800]
    assert "отчёт" in блок and "_canMutate()" in блок
    assert "vtErr" in блок, "отказ установки не показывается"


def test_перезапись_правок_предлагается_только_при_правке():
    """Кнопка «снести правки» не должна стоять там, где сносить нечего."""
    i = АДМИНКА.index("Поставить с перезаписью правок")
    окно = АДМИНКА[max(0, i - 600):i]
    assert "v.hasEdits" in окно, окно[-200:]
    assert "hasEdits: (в.наборы || []).some(н => н.правка_в_системе)" in ШАБЛОН


def test_чего_нет_в_системе_сказано_словами():
    assert "Пакет это не привозит" in АДМИНКА, (
        "не сказано, что навыки и адаптеры ставит вендор, а не пакет")
    assert "v.hasMissing" in АДМИНКА


def test_cli_ставит_через_ту_же_ручку():
    """Второго пути установки быть не должно: он разойдётся с проверками сервера."""
    исходник = (КОРЕНЬ / "cli" / "ape.py").read_text(encoding="utf-8")
    блок = исходник[исходник.index("def cmd_verticals(a):"):]
    блок = блок[:блок.index("def cmd_usage(a):")]
    assert "/api/verticals" in блок and "/install" in блок
    assert "перезаписать_правки" in блок
    for запрет in ("contract_store", "rules_store", "dataplane_store"):
        assert запрет not in блок, f"CLI ставит в обход сервера: {запрет}"
