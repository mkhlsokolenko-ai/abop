# -*- coding: utf-8 -*-
"""Арендаторы: сосед не виден, и это проверяется, а не обещается.

Шаг 5 плана «правила — данные» и условие лицензионной модели: «компания собирает своих агентов»
значит, что на платформе живут несколько компаний. До этого тенанта не было ни у записей, ни у
агентов, ни у прогонов, ни у карты — то есть вторую компанию продать было нельзя.

Файл делится на две части, и вторая важнее первой.

* **Поведение**: записи, агенты, прогоны, правила, рецепты одного арендатора не видны другому.
  Чужое отвечает «нет такого», а не 403: существование чужого агента само по себе сведение о
  другой компании.
* **Сторож раскладки**: статическая проверка, что ни один стор не ходит в таблицу с арендатором
  без фильтра по нему. Поведенческий тест проверяет то, о чём я подумал; сторож — то, о чём
  забыл. Пропущенный фильтр это не баг интерфейса, а утечка данных заказчика.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import re
import sys

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"
os.environ["ABOP_RUN_WORKERS"] = "0"

import pytest  # noqa: E402

from server import agent_store, contract_store, dataplane_records, dataplane_store  # noqa: E402
from server import deps, rules_store, run_store, tenancy  # noqa: E402


def прогнать(корутина):
    return asyncio.new_event_loop().run_until_complete(корутина)


@pytest.fixture(autouse=True)
def чисто():
    dataplane_records._сброс_для_тестов()
    for стор in (agent_store, contract_store, run_store, rules_store):
        стор._MEM.clear()
    dataplane_store._MEM_RECIPES.clear()
    dataplane_store._MEM_CONNECTORS.clear()
    yield


# ── откуда берётся арендатор ──────────────────────────────────────────────────────────────────

def test_арендатор_из_токена_а_не_выбор_пользователя():
    assert deps._identity({"sub": "u", "tenant": "acme",
                           "realm_access": {"roles": ["manager"]}})["tenant"] == "acme"
    assert deps._identity({"sub": "u", "org": "beta",
                           "realm_access": {"roles": ["analyst"]}})["tenant"] == "beta"
    assert deps._identity({"sub": "u", "realm_access": {"roles": ["analyst"]}})["tenant"] == "default"


def test_кривой_идентификатор_не_попадает_в_общий_котёл():
    """Мусор в заявке не должен молча стать «общим» арендатором с чужими данными."""
    for мусор in ("../etc/passwd", "ACME Inc", "a" * 80, "-начинается-с-дефиса", "tenant;drop"):
        assert tenancy.нормализовать(мусор) == tenancy.ПО_УМОЛЧАНИЮ, мусор
    assert tenancy.нормализовать("acme-2") == "acme-2"
    assert tenancy.нормализовать("ACME") == "acme", "регистр не должен делать двух арендаторов"


def test_переключаться_может_только_вендор():
    вендор = {"level": "support", "tenant": "default"}
    клиент = {"level": "manager", "tenant": "acme"}
    assert tenancy.для_пользователя(вендор, "acme") == "acme"
    assert tenancy.для_пользователя(клиент, "beta") == "acme", (
        "менеджер заказчика сменил арендатора заголовком — это дыра")
    assert tenancy.можно_переключать(клиент) is False


# ── изоляция данных ───────────────────────────────────────────────────────────────────────────

def test_канонические_записи_не_видны_соседу():
    """Главное: данные заказчика. Их утечка — не неудобство, а инцидент."""
    with tenancy.с_арендатором("acme"):
        dataplane_records.append("doc1c", [{"id": "Д-1", "сумма": 100,
                                            "provenance": {"fetched_at": 1}}])
        assert len(dataplane_records.query("doc1c")) == 1
    with tenancy.с_арендатором("beta"):
        assert dataplane_records.query("doc1c") == []
        assert dataplane_records.get("doc1c", "Д-1") is None
        assert dataplane_records.count("doc1c") == 0
        assert dataplane_records.entities() == [], (
            "сосед видит ПЕРЕЧЕНЬ сущностей — по нему читается, чем занимается другая компания")
    with tenancy.с_арендатором("acme"):
        assert dataplane_records.count("doc1c") == 1, "своё не должно пропасть"


def test_перечитать_начисто_не_трогает_соседа():
    """`reset=True` — «перечитать сущность» — должно чистить только свою."""
    for кто in ("acme", "beta"):
        with tenancy.с_арендатором(кто):
            dataplane_records.append("doc1c", [{"id": кто, "provenance": {"fetched_at": 1}}])
    with tenancy.с_арендатором("acme"):
        dataplane_records.append("doc1c", [{"id": "новая", "provenance": {"fetched_at": 2}}],
                                 reset=True)
    with tenancy.с_арендатором("beta"):
        assert [r["id"] for r in dataplane_records.query("doc1c")] == ["beta"]


def test_агент_соседа_не_существует():
    with tenancy.с_арендатором("acme"):
        a = прогнать(agent_store.save(name="А", audit_id="omninet-showcase-2026", version=1,
                                      graph={"nodes": []}, autonomy_max="A2"))
    with tenancy.с_арендатором("beta"):
        assert прогнать(agent_store.get(a["id"])) is None, "чужой агент читается по id"
        assert прогнать(agent_store.list_for()) == []
        assert прогнать(agent_store.delete(a["id"])) is False, "чужого агента можно удалить"
        assert прогнать(agent_store.set_status(a["id"], "retired")) is None
    with tenancy.с_арендатором("acme"):
        assert прогнать(agent_store.get(a["id"])) is not None, "свой агент пропал"


def test_одна_вертикаль_у_двух_арендаторов_не_сталкивается():
    """Первое, что случится при втором заказчике: тот же audit_id и тот же id агента.

    Пока ключ был одиночным, второй заказчик затирал первого — молча и необратимо.
    """
    созданные = {}
    for кто in ("acme", "beta"):
        with tenancy.с_арендатором(кто):
            a = прогнать(agent_store.save(name=f"Контролёр {кто}",
                                          audit_id="omninet-showcase-2026", version=1,
                                          graph={"nodes": []}, autonomy_max="A2"))
            созданные[кто] = a["id"]
    assert созданные["acme"] == созданные["beta"], "идентификаторы и должны совпадать — в этом суть"
    for кто in ("acme", "beta"):
        with tenancy.с_арендатором(кто):
            свой = прогнать(agent_store.get(созданные[кто]))
            assert свой and свой["name"] == f"Контролёр {кто}", (
                f"агент арендатора {кто} затёрт соседом")


def test_прогон_соседа_не_читается():
    """В прогоне находки, суммы и выгрузка данных компании."""
    with tenancy.с_арендатором("acme"):
        прогнать(run_store.save({"id": "run-1", "agent_id": "a.v1", "verdict": {"ok": True}}))
    with tenancy.с_арендатором("beta"):
        assert прогнать(run_store.get("run-1")) is None
        assert прогнать(run_store.list_runs()) == []
        assert прогнать(run_store.purge()) == 0, "чистка журнала задела соседа"
    with tenancy.с_арендатором("acme"):
        assert прогнать(run_store.get("run-1")) is not None


def test_правка_правил_у_каждого_своя():
    """Набор «showcase-b» правят у себя все, кто поставил вертикаль — и правки не смешиваются."""
    for кто, правил in (("acme", 3), ("beta", 7)):
        with tenancy.с_арендатором(кто):
            прогнать(rules_store.save("showcase-b",
                                      {"id": "showcase-b", "предмет": "feed_card",
                                       "правила": [{"код": f"{кто}-{i}"} for i in range(правил)]},
                                      editor=кто))
    for кто, правил in (("acme", 3), ("beta", 7)):
        with tenancy.с_арендатором(кто):
            строка = прогнать(rules_store.get("showcase-b"))
            assert строка and len(строка["набор"]["правила"]) == правил, кто
            assert строка["кто"] == кто
            assert len(прогнать(rules_store.all())) == 1, "в списке правок виден сосед"


def test_рецепты_и_коннекторы_не_видны_соседу():
    """В рецепте адрес источника, в коннекторе — доступ к системе заказчика."""
    with tenancy.с_арендатором("acme"):
        прогнать(dataplane_store.save_recipe("omninet_feed", {"recipe": "omninet_feed",
                                                              "entity": "feed_card"}))
        прогнать(dataplane_store.save_connector("c1", {"id": "c1", "target": "https://acme/erp"}))
    with tenancy.с_арендатором("beta"):
        assert прогнать(dataplane_store.recipes_all()) == []
        assert прогнать(dataplane_store.connectors_all()) == []
        assert прогнать(dataplane_store.delete_recipe("omninet_feed")) is False
        assert прогнать(dataplane_store.delete_connector("c1")) is False
    with tenancy.с_арендатором("acme"):
        assert len(прогнать(dataplane_store.recipes_all())) == 1


def test_контракт_той_же_вертикали_у_двух_арендаторов():
    бандл = {"capability_request": {"audit_id": "omninet-showcase-2026", "family": "audit"}}
    for кто in ("acme", "beta"):
        with tenancy.с_арендатором(кто):
            прогнать(contract_store.save(
                бандл, {"audit_id": "omninet-showcase-2026", "family": "audit", "кто": кто},
                ingested_by=кто))
    for кто in ("acme", "beta"):
        with tenancy.с_арендатором(кто):
            c = прогнать(contract_store.get("omninet-showcase-2026"))
            assert c and c["intake"]["кто"] == кто, f"контракт {кто} затёрт соседом"
            assert len(прогнать(contract_store.list_all())) == 1


# ── сторож раскладки: то, о чём я забыл ───────────────────────────────────────────────────────

# Таблицы, у которых есть колонка `tenant`. Любой запрос к ним обязан нести фильтр по арендатору.
ТАБЛИЦЫ_С_АРЕНДАТОРОМ = {
    "dp_records", "agent_versions", "runs", "check_rules", "dp_recipes", "dp_connectors",
    "contract_sets", "process_map", "process_seats", "process_revisions", "process_changes",
}
# Запросы, которым фильтр не нужен и почему. Каждое исключение — осознанное и названное.
ИСКЛЮЧЕНИЯ = {
    # миграции и создание схемы
    "ALTER", "CREATE", "DROP", "TRUNCATE", "DO $$",
}


def _запросы(текст: str) -> list[str]:
    """SQL-литералы из исходника — через разбор Python, а не регулярку по кавычкам.

    Регулярка на этом коде не работает: один апостроф в русской строке документации «съедает»
    следующие тысячи символов, и сторож тихо находит НОЛЬ запросов. Сторож, который ничего не
    разобрал, хуже отсутствующего — он создаёт уверенность на пустом месте. Поэтому литералы
    берёт `ast`: он же сам склеивает соседние строки, как это делает Python.
    """
    out = []
    for узел in ast.walk(ast.parse(текст)):
        куски = ""
        if isinstance(узел, ast.Constant) and isinstance(узел.value, str):
            куски = узел.value
        elif isinstance(узел, ast.JoinedStr):          # f-строка: берём её текстовые части
            куски = "".join(ч.value for ч in узел.values
                            if isinstance(ч, ast.Constant) and isinstance(ч.value, str))
        кандидат = куски.strip()
        if re.match(r"^(SELECT|INSERT|UPDATE|DELETE)\b", кандидат, re.I):
            out.append(" ".join(кандидат.split()))
    return out


def test_ни_один_запрос_не_ходит_в_таблицу_без_арендатора():
    """Сторож против пропущенного фильтра — то, что поведенческий тест не найдёт.

    Поведенческий тест проверяет путь, о котором я подумал. Этот — все остальные: он разбирает
    SQL в сторах и требует, чтобы каждый запрос к таблице с арендатором нёс `tenant`.
    """
    дыры = []
    for файл in sorted((КОРЕНЬ / "server").glob("*.py")):
        текст = файл.read_text(encoding="utf-8")
        for запрос in _запросы(текст):
            if any(запрос.upper().startswith(x) for x in ИСКЛЮЧЕНИЯ):
                continue
            задетые = {t for t in ТАБЛИЦЫ_С_АРЕНДАТОРОМ
                       if re.search(r"\b" + t + r"\b", запрос)}
            if задетые and "tenant" not in запрос:
                дыры.append(f"{файл.name}: {запрос[:130]}")
    assert not дыры, ("запрос к таблице арендатора без фильтра по нему — это утечка:\n"
                      + "\n".join(дыры))


def test_сторож_работает():
    """Проверка самого сторожа: он обязан увидеть дыру, если её внести."""
    дыра = 'cur = await conn.execute("SELECT payload FROM runs WHERE id=%s", (x,))'
    запросы = _запросы(дыра)
    assert запросы and "runs" in запросы[0] and "tenant" not in запросы[0]


# ── фоновые пути: воркер и планировщик ────────────────────────────────────────────────────────

def test_задание_несёт_арендатора():
    """Очередь сквозная: воркер берёт следующее задание любой компании.

    Поэтому арендатор кладётся в задание при постановке — пока мы внутри запроса и контекст
    известен. Без этого прогон компании А посчитал бы по данным арендатора по умолчанию: не
    пустой ответ, а утечка.
    """
    from server import run_queue
    run_queue._MEM.clear()
    with tenancy.с_арендатором("acme"):
        job = прогнать(run_queue.enqueue(agent_id="a.v1", actor="кто-то", payload={"x": 1}))
    assert job["payload"]["tenant"] == "acme", job["payload"]


def test_воркер_ставит_арендатора_перед_исполнением():
    """Статический сторож: обработчик обязан исполняться внутри контекста арендатора задания."""
    исходник = (КОРЕНЬ / "server" / "run_queue.py").read_text(encoding="utf-8")
    блок = исходник[исходник.index("async def worker_loop("):]
    assert "tenancy.установить(" in блок, (
        "воркер не ставит арендатора — прогон посчитает по чужим данным")
    assert "tenancy.вернуть(" in блок, "контекст не возвращается — следующее задание унаследует чужого"
    # сравниваем с ФАКТИЧЕСКИМ вызовом, а не с упоминанием handler(job) в строке документации
    i, j = блок.index("tenancy.установить("), блок.index("wait_for(handler(job)")
    assert i < j, "арендатор ставится ПОСЛЕ вызова обработчика"


def test_планировщик_обходит_всех_арендаторов():
    """Иначе расписания всех, кроме арендатора по умолчанию, МОЛЧА не запускались бы."""
    исходник = (КОРЕНЬ / "server" / "triggers.py").read_text(encoding="utf-8")
    блок = исходник[исходник.index("async def _tick("):][:900]
    assert "agent_store.арендаторы()" in блок and "с_арендатором" in блок, блок[:300]


def test_перечень_арендаторов_не_утекает_в_обычное_чтение():
    """Сквозной перечень — только у планировщика; обычный список агентов остаётся своим."""
    from server import agent_store as ас
    for кто in ("acme", "beta"):
        with tenancy.с_арендатором(кто):
            прогнать(ас.save(name=кто, audit_id=f"c-{кто}", version=1,
                             graph={"nodes": []}, autonomy_max="A1"))
    assert прогнать(ас.арендаторы()) == ["acme", "beta"]
    with tenancy.с_арендатором("acme"):
        assert [a["name"] for a in прогнать(ас.list_for())] == ["acme"]


# ── интерфейс ─────────────────────────────────────────────────────────────────────────────────

ШАБЛОН = (КОРЕНЬ / "webapp" / "src" / "template.html").read_text(encoding="utf-8")


def test_арендатор_виден_в_интерфейсе():
    """В многоарендной системе «чьи это данные» — часть ответа, а не настройка в глубине."""
    assert "meTenantLine" in ШАБЛОН and "арендатор: " in ШАБЛОН


def test_заголовок_арендатора_едет_со_всеми_запросами():
    """Иначе часть экрана показывала бы своего арендатора, а часть — клиента."""
    обёртка = ШАБЛОН[ШАБЛОН.index("window.fetch=function(url,opts)"):][:2500]
    assert "X-ABOP-Tenant" in обёртка, "заголовок ставится не в общей обёртке fetch"


def test_чужой_арендатор_показан_постоянной_плашкой():
    """Разовый тост забывается за минуту; вендор примет чужие находки за свои."""
    assert "Вы смотрите данные арендатора" in ШАБЛОН, "текста плашки нет"
    i = ШАБЛОН.index("{{ tenantForeignLine }}")      # именно ВЁРСТКА, а не свойство рендера
    assert "position: fixed" in ШАБЛОН[i - 700:i], "плашка не закреплена — её можно прокрутить"
    assert "{{ tenantForeign }}" in ШАБЛОН[i - 1100:i], "плашка показывается не по признаку"
    assert "Вернуться к своему" in ШАБЛОН[i:i + 900], "из плашки нельзя вернуться к своему"


def test_переключатель_только_у_вендора():
    assert "canSwitchTenant: !!(s.me && ['admin', 'support'].indexOf(s.me.level) > -1)" in ШАБЛОН
    i = ШАБЛОН.index("Вендорская поддержка: открыть данные арендатора")
    assert "canSwitchTenant" in ШАБЛОН[i - 700:i], "переключатель показан не только вендору"


def test_бандл_собран_с_арендатором():
    бандл = (КОРЕНЬ / "webapp" / "index.html").read_text(encoding="utf-8")
    assert "X-ABOP-Tenant" in бандл, "бандл не пересобран после правки исходников"


# ── кэши ядра: дыра, которую статический сторож не видит ──────────────────────────────────────

def test_определения_data_plane_читаются_по_арендатору():
    """Поймано живой проверкой на проде, а не тестом.

    Сервер инжектил в ядро ГОТОВЫЙ СПИСОК рецептов — и список не знал про арендатора: вендор,
    открывший данные клиента, видел рецепты своего. Статический сторож таких дыр не видит:
    данные приходят не из запроса, а из кэша процесса.

    Закономерность, которую сторожим: **инъекция функцией знает арендатора, инъекция снимком —
    нет**. Поэтому сервер обязан инжектить читателя, а не список.
    """
    from cli import ape
    from server import dataplane_records as dr

    было_r, было_c = ape._RECIPE_STORE, ape._CONNECTOR_STORE
    ape.set_recipe_store(dr.рецепты)
    ape.set_connector_store(dr.коннекторы)
    try:
        with tenancy.с_арендатором("acme"):
            прогнать(dataplane_store.save_recipe("omninet_feed", {"recipe": "omninet_feed",
                                                                  "entity": "feed_card"}))
            assert ape.data_recipes() == ["omninet_feed"]
        with tenancy.с_арендатором("beta"):
            assert ape.data_recipes() == [], "ядро отдаёт рецепты чужого арендатора"
            assert ape.data_connectors() == []
    finally:
        ape.set_recipe_store(было_r)
        ape.set_connector_store(было_c)


def test_сервер_инжектит_читателя_а_не_снимок():
    """Статический сторож именно для этой закономерности: снимок про арендатора не знает."""
    слой = (КОРЕНЬ / "server" / "routes" / "data.py").read_text(encoding="utf-8")
    блок = слой[слой.index("async def _refresh_dataplane_cache"):][:900]
    assert "ape.set_recipe_store(dataplane_records.рецепты)" in блок, блок[:400]
    assert "recipes_all()" not in блок, (
        "сервер снова инжектит готовый список — он не знает про арендатора")


def test_правка_навыка_не_меняет_карточку_соседу():
    """Стор правок писался, когда компания была одна: правки честно звались «ОБЩИЕ для всех».

    В многоарендной системе это значит: методолог компании А меняет формат вывода навыка — и он
    меняется у компании Б. Не утечка данных, но чужая конфигурация, и чинится тем же приёмом.
    """
    from cli import ape
    from server import dataplane_records as dr
    from server import skill_store

    skill_store._MEM.clear()
    было_ds, было_out = ape._SKILL_DS_OVERRIDES, ape._SKILL_OUTPUT_OVERRIDES
    ape.set_skill_ds_overrides(dr.правки_источников)
    ape.set_skill_output_overrides(dr.правки_вывода)
    try:
        with tenancy.с_арендатором("acme"):
            прогнать(skill_store.save_patch("dcf-valuation", {"output": "freeform"},
                                            editor="acme"))
            прогнать(skill_store.save_datasources("dcf-valuation",
                                                  [{"entity": "transaction"}], editor="acme"))
            assert ape.skill_safety("dcf-valuation")["output"] == "freeform"
            assert ape.skill_datasources("dcf-valuation") == [{"entity": "transaction"}]
        with tenancy.с_арендатором("beta"):
            assert ape.skill_safety("dcf-valuation")["output"] == "structured", (
                "правка формата вывода у соседа изменила карточку навыка")
            assert прогнать(skill_store.get("dcf-valuation")) is None
    finally:
        ape.set_skill_ds_overrides(было_ds)
        ape.set_skill_output_overrides(было_out)
        skill_store._MEM.clear()


def test_инъекции_в_ядро_знают_арендатора():
    """Закономерность целиком: что инжектится в ядро и принадлежит арендатору — только читателем.

    Снимок (dict/list) про арендатора не знает по определению. Это та дыра, которую не видит ни
    статический сторож SQL, ни поведенческий тест конкретной ручки — её нашла живая проверка на
    проде. Поэтому закономерность зафиксирована отдельно.
    """
    ядро = (КОРЕНЬ / "cli" / "ape.py").read_text(encoding="utf-8")
    for сеттер in ("set_recipe_store", "set_connector_store",
                   "set_skill_ds_overrides", "set_skill_output_overrides"):
        блок = ядро[ядро.index("def " + сеттер + "("):][:700]
        assert "callable(" in блок, f"{сеттер} не принимает читателя — снимок не знает арендатора"
    for слой, сеттеры in (
        ("server/routes/data.py", ("set_recipe_store", "set_connector_store")),
        ("server/routes/skills.py", ("set_skill_ds_overrides", "set_skill_output_overrides")),
    ):
        текст = (КОРЕНЬ / слой).read_text(encoding="utf-8")
        for сеттер in сеттеры:
            i = текст.index("ape." + сеттер + "(")
            вызов = текст[i:текст.index(")", i) + 1]
            assert "dataplane_records." in вызов and "await" not in вызов, (
                f"{слой}: {вызов} — инжектируется снимок, а не читатель")
