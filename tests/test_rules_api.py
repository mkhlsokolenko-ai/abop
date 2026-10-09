# -*- coding: utf-8 -*-
"""Правила правит методолог, а не разработчик — и это должно быть безопаснее файла, а не опаснее.

Шаг 3 плана «правила — данные». Набор правил приезжает с пакетом вертикали и до сих пор правился
только файлом в репозитории: новая проверка = релиз. Теперь набор — редактируемая сущность со
своим хранилищем (`check_rules`), и у этого ровно три следствия, за которыми тут и следим.

1. **Невалидный набор не сохраняется.** Опечатка в ссылке на справочник (`$категория` вместо
   `@категория`) однажды молча уронила полноту проверок с 74 % до 20 %: правила не сработали, а
   карточки выглядели исправными. Файл такую правку принимал. Стор обязан отказать.
2. **Черновик видно до сохранения.** Методолог должен увидеть срабатывание правила на выборке
   раньше, чем правило начнёт считать находки заказчику.
3. **«Вернуть версию пакета» возможно всегда.** Поставочный набор неизменяем — откат снимает
   правку, а не пытается восстановить файл по памяти.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"
os.environ["ABOP_RUN_WORKERS"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import rules_store, web_api  # noqa: E402
# ИМЕННО `cli.ape`, а не `ape`: сервер держит ядро этим именем, и `import ape` дал бы ВТОРОЙ
# экземпляр модуля. Подмена `data_query` в нём не действует на ручку — проверка тогда тихо
# считает по настоящему Data Plane машины и зеленеет там, где данных просто случайно хватило.
from cli import ape  # noqa: E402

CLIENT = TestClient(web_api.app)
ДАННЫЕ = json.loads((ROOT / "tests" / "fixtures" / "audit1c_dump.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def чистый_стор():
    """Каждый тест начинает с «правок нет»: стор без Postgres живёт в памяти процесса."""
    rules_store._MEM.clear()
    asyncio.get_event_loop_policy().new_event_loop()
    rules_store.ЭФФЕКТИВНЫЕ.обновить({})
    ape.set_rule_store(rules_store.ЭФФЕКТИВНЫЕ)
    yield
    rules_store._MEM.clear()
    rules_store.ЭФФЕКТИВНЫЕ.обновить({})


@pytest.fixture
def стенд():
    """Выгрузка 1С вместо живого Data Plane: прогон обязан считать по фикстуре, а не по машине."""
    было = ape.data_query

    def _выборка(entity, *a, **k):
        предел = k.get("limit") or (a[0] if a else None) or 100000
        return ДАННЫЕ.get(entity, [])[:предел]

    ape.data_query = _выборка
    assert len(_выборка("doc1c", limit=50)) == 50, "фикстура стенда пуста — проверять нечего"
    yield
    ape.data_query = было


def test_список_наборов_показывает_поставку_и_правку():
    д = CLIENT.get("/api/data/rules").json()
    по_id = {н["id"]: н for н in д["наборы"]}
    assert {"showcase-b", "audit1c-base", "audit1c-nsi"} <= set(по_id), по_id.keys()
    аудит = по_id["audit1c-base"]
    assert аудит["предмет"] == "doc1c" and аудит["правил"] == 8
    assert аудит["правка"] is False, "правок не было — набор обязан быть поставочным"
    assert аудит["валиден"] is True and аудит["ошибки"] == []
    assert д["предикатов"] >= 10


def test_библиотека_предикатов_пригодна_для_формы():
    """Редактор правила строит форму по объявленной схеме параметров, а не по памяти автора."""
    д = CLIENT.get("/api/data/predicates").json()
    по_имени = {п["имя"]: п for п in д["предикаты"]}
    assert "не_меньше" in по_имени and "дубль_по_ключу" in по_имени
    for имя, п in по_имени.items():
        assert п["параметры"], f"{имя}: без схемы параметров форму не построить"
        assert п["описание"], имя
    assert по_имени["дубль_по_ключу"]["по_набору"] is True, "агрегатный предикат обязан быть помечен"


def test_невалидный_набор_не_сохраняется():
    """Ровно та опечатка, что дала тихое падение полноты: ссылка `$` вместо `@`."""
    набор = copy.deepcopy(CLIENT.get("/api/data/rules/showcase-b").json()["набор"])
    набор["контекст"][1]["по"]["категория"] = "$категория"
    r = CLIENT.put("/api/data/rules/showcase-b", json={"набор": набор})
    assert r.status_code == 422, r.text
    assert "$категория" in r.json()["detail"]
    assert CLIENT.get("/api/data/rules/showcase-b").json()["правка"] is False, (
        "отказ в сохранении не должен оставлять следов правки")


def test_неизвестный_предикат_отклоняется():
    набор = copy.deepcopy(CLIENT.get("/api/data/rules/showcase-b").json()["набор"])
    набор["правила"].append({"код": "X", "предикат": "выдуманный", "параметры": {}})
    r = CLIENT.put("/api/data/rules/showcase-b", json={"набор": набор})
    assert r.status_code == 422 and "выдуманный" in r.json()["detail"]


def test_набор_без_предмета_отклоняется():
    r = CLIENT.put("/api/data/rules/showcase-b", json={"набор": {"правила": []}})
    assert r.status_code == 422 and "предмет" in r.json()["detail"]


def test_правка_сохраняется_и_её_применяет_прогон(стенд):
    """Главное обещание шага: правило, добавленное в интерфейсе, меняет находки без релиза."""
    было = len(ape.прогнать_проверки("audit1c-nsi"))
    набор = copy.deepcopy(CLIENT.get("/api/data/rules/audit1c-nsi").json()["набор"])
    набор["правила"].append({
        "код": "C9", "предикат": "поле_обязательно", "параметры": {"поля": ["ИНН"]},
        "причина": "у контрагента не заполнен ИНН", "серьёзность": "средняя",
        "чинится": "человеком"})
    r = CLIENT.put("/api/data/rules/audit1c-nsi", json={"набор": набор})
    assert r.status_code == 200, r.text
    assert r.json()["правка"] is True and r.json()["версия_правки"] == 1

    карточка = CLIENT.get("/api/data/rules/audit1c-nsi").json()
    assert "C9" in карточка["коды"]
    assert карточка["поставочный"] and "C9" not in [п["код"] for п in карточка["поставочный"]["правила"]], (
        "поставочный набор обязан остаться неизменным — иначе откатывать не к чему")

    стало = ape.прогнать_проверки("audit1c-nsi")
    assert len(стало) > было, "прогон считает по старому набору — правка не доехала до ядра"
    assert any(н["код"] == "C9" for н in стало)


def test_вернуть_версию_пакета(стенд):
    набор = copy.deepcopy(CLIENT.get("/api/data/rules/audit1c-nsi").json()["набор"])
    набор["правила"] = []
    assert CLIENT.put("/api/data/rules/audit1c-nsi", json={"набор": набор}).status_code == 200
    assert ape.прогнать_проверки("audit1c-nsi") == [], "правка не применилась — проверять нечего"

    r = CLIENT.post("/api/data/rules/audit1c-nsi/revert")
    assert r.status_code == 200 and r.json()["снято"] is True
    assert r.json()["правка"] is False and r.json()["правил"] == 1
    assert ape.прогнать_проверки("audit1c-nsi"), "после отката прогон обязан снова видеть правила"


def test_dryrun_считает_черновик_не_сохраняя(стенд):
    """Срабатывание видно до сохранения — иначе методолог правит правила вслепую."""
    набор = copy.deepcopy(CLIENT.get("/api/data/rules/audit1c-base").json()["набор"])
    набор["правила"] = [п for п in набор["правила"] if п["код"] == "B1"]
    r = CLIENT.post("/api/data/rules/audit1c-base/dryrun", json={"набор": набор, "лимит": 50})
    д = r.json()
    assert r.status_code == 200, r.text
    assert д["черновик"] is True and д["записей"] == 50 and д["предмет"] == "doc1c"
    assert д["по_правилу"] == {"B1": д["находок"]}, д["по_правилу"]
    assert CLIENT.get("/api/data/rules/audit1c-base").json()["правка"] is False, (
        "dry-run не имеет права сохранять набор"
    )


def test_dryrun_показывает_нулевое_срабатывание(стенд):
    """Нуль у правила — такой же результат, как сотня: его обязано быть видно, а не пусто."""
    набор = copy.deepcopy(CLIENT.get("/api/data/rules/audit1c-base").json()["набор"])
    набор["правила"] = [{"код": "НИКОГДА", "предикат": "не_меньше",
                         "параметры": {"поле": "СуммаДокумента", "минимум": 0},
                         "причина": "сумма меньше нуля", "серьёзность": "низкая"}]
    д = CLIENT.post("/api/data/rules/audit1c-base/dryrun", json={"набор": набор}).json()
    assert д["по_правилу"] == {"НИКОГДА": 0}
    assert д["находок"] == 0 and д["ошибки"] == []


def test_объявленная_находка_контекста_не_считается_поломкой(стенд):
    """Поймано на живом прогоне: `CATEGORY_UNMAPPED` красилось как «набор сломан».

    Связь, которая ничего не нашла, бывает двух разных вещей. Если набор объявил её в `если_нет`
    («категория поставщика не сопоставлена») — это находка ПО СУЩЕСТВУ, правило сработало как
    написано. Если не объявил — это поломка набора. Складывать их в одну графу значит либо
    пугать ложной тревогой, либо прятать настоящую.
    """
    набор = copy.deepcopy(CLIENT.get("/api/data/rules/showcase-b").json()["набор"])
    д = CLIENT.post("/api/data/rules/showcase-b/dryrun", json={"набор": набор, "лимит": 20}).json()
    assert "CATEGORY_UNMAPPED" in д["по_контексту"], д["по_контексту"]
    assert "CATEGORY_UNMAPPED" not in д["служебные"], (
        "объявленная находка попала в графу поломок — ложная тревога")
    assert д["служебные"] == {}, д["служебные"]


def test_dryrun_отделяет_сигналы_движка_от_находок(стенд):
    """`КОНТЕКСТ_НЕ_РАЗРЕШЁН` — не находка по предмету, а поломка набора: графа отдельная."""
    набор = {"id": "проба", "предмет": "doc1c",
             "контекст": [{"имя": "спр", "из": "нет_такой_сущности", "по": {"id": "$запись.id"}}],
             "правила": [{"код": "Z", "предикат": "не_меньше",
                          "параметры": {"поле": "СуммаДокумента", "минимум": "@спр.порог"}}]}
    д = CLIENT.post("/api/data/rules/audit1c-base/dryrun", json={"набор": набор, "лимит": 5}).json()
    assert д["по_правилу"] == {"Z": 0}
    assert д["служебные"].get("КОНТЕКСТ_НЕ_РАЗРЕШЁН") == 5, д["служебные"]


def test_правка_требует_прав_менеджера():
    """Правило считает находки заказчику — правка набора это мутация, а не настройка вида."""
    слой = (ROOT / "server" / "routes" / "rules.py").read_text(encoding="utf-8")
    for ручка in ("rule_save", "rule_revert"):
        блок = слой[слой.index("async def " + ручка):][:900]
        assert 'require_level(u, "manager")' in блок, f"{ручка} без проверки прав"
