# -*- coding: utf-8 -*-
"""Папка под присмотром: новый файл сам становится задачей агента — и ни одного тихого отказа.

Папка местная: акты падают во «Входящие», выписки выгружаются в папку банка, сканы приходят из МФУ.
Веб-клиент этого не видит по определению, десктоп видит — это одно из двух, чего веб не умеет
(второе — уведомления ОС, они закрыты отдельно).

Здесь проверяется ровно то, на чём присмотр легко сделать вредным:

* **Молчаливый отказ.** Скан без текстового слоя, запертый файл, упавший запуск — если об этом не
  сказать, человек будет считать, что работа идёт. Присмотр, молча не берущий файлы, хуже
  отсутствия присмотра.
* **Деньги.** Указать на «Загрузки» — обычное дело, а там сотня файлов. Сотня прогонов, которых
  никто не просил, — это счёт заказчику. Поэтому лежавшее до присмотра не берётся, а за один заход
  берётся не больше пяти файлов, и остаток назван числом.
* **Половина документа.** Файл, который ещё копируется, прочитать можно — и агент честно разберёт
  половину акта. Размер обязан совпасть в двух заходах подряд.
* **Чужая папка.** Область та же, что у вложений чата: установка, человек и вендорский переход.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
DESKTOP = КОРЕНЬ / "desktop"
sys.path.insert(0, str(DESKTOP))

ПАНЕЛЬ = (DESKTOP / "ui" / "modules" / "watch" / "panel.js").read_text(encoding="utf-8")
MAIN = (DESKTOP / "electron" / "main.js").read_text(encoding="utf-8")


@pytest.fixture()
def присмотр(tmp_path, monkeypatch):
    """Настоящий модуль, настоящая папка на диске, подменённые вход и ABOP."""
    from sidecar import auth, config, db
    # Своя база на каждый тест: присмотр пишет журнал, и прогон не должен зависеть от того, что
    # осталось от прошлого. `DATA_DIR` подменяем вместе с `DB_FILE` — `ensure_dirs` смотрит в него.
    дом = tmp_path / "home"
    дом.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", дом)
    monkeypatch.setattr(config, "DB_FILE", дом / "app.db")
    db.init()
    from sidecar import abop_client as abop
    from sidecar.modules.watch import module as watch

    monkeypatch.setattr(auth, "token", lambda: "t")
    monkeypatch.setattr(auth, "claims", lambda: {"sub": "u-1"})
    monkeypatch.setattr(config, "ABOP", "http://abop.acme.ru:8091")
    запуски: list = []

    def run_async(agent_id, context="", no_cache=False, deliver=""):
        запуски.append({"agent_id": agent_id, "context": context})
        return {"job_id": "job-" + str(len(запуски)), "status": "queued"}

    monkeypatch.setattr(abop, "run_async", run_async)
    watch._размеры.clear()
    папка = tmp_path / "Входящие"
    папка.mkdir()
    return watch, папка, запуски


def _файл(папка: pathlib.Path, имя: str, текст: str = "Акт № 7 на сумму 100 000 ₽") -> pathlib.Path:
    p = папка / имя
    p.write_text(текст, encoding="utf-8")
    return p


def _добавить(watch, папка, **kw):
    from sidecar.modules.watch.module import ПапкаIn
    тело = {"path": str(папка), "agent_id": "audit1c", "agent_name": "Аудитор"}
    тело.update(kw)
    return watch.add_folder(ПапкаIn(**тело))


def _дважды(watch):
    """Два захода: первый запоминает размеры, второй берёт. Так устроена защита от докопировки."""
    watch._заход()
    return watch._заход()


# ── файл превращается в задачу ───────────────────────────────────────────────────────────────────

def test_новый_файл_становится_прогоном(присмотр):
    watch, папка, запуски = присмотр
    _добавить(watch, папка)
    _файл(папка, "akt-7.txt")
    сводка = _дважды(watch)
    assert сводка["взято"] == 1, сводка
    assert len(запуски) == 1
    assert "akt-7.txt" in запуски[0]["context"], "агент не получил содержимое файла"
    assert "Акт № 7" in запуски[0]["context"]
    assert запуски[0]["agent_id"] == "audit1c"


def test_результат_ждёт_в_чате_папки(присмотр):
    """Задание догона — то же, что у чата: результат доедет, даже если приложение закрыли."""
    watch, папка, _ = присмотр
    from sidecar import db
    _добавить(watch, папка)
    _файл(папка, "akt-8.txt")
    _дважды(watch)
    чаты = db.q("SELECT id,title FROM threads")
    assert чаты and "Присмотр" in чаты[0]["title"], чаты
    ждут = db.q("SELECT job_id,thread_id FROM pending_runs")
    assert len(ждут) == 1 and ждут[0]["thread_id"] == чаты[0]["id"]
    вложения = db.q("SELECT name,chars FROM attachments")
    assert вложения and вложения[0]["name"] == "akt-8.txt" and вложения[0]["chars"] > 0


def test_один_файл_не_берётся_дважды(присмотр):
    watch, папка, запуски = присмотр
    _добавить(watch, папка)
    _файл(папка, "akt-9.txt")
    _дважды(watch)
    watch._заход()
    watch._заход()
    assert len(запуски) == 1, "тот же файл запустил агента повторно — это счёт за ту же работу"


def test_изменённый_файл_это_новая_работа(присмотр):
    """Отпечаток включает размер и время правки: исправленный акт надо разобрать заново."""
    watch, папка, запуски = присмотр
    _добавить(watch, папка)
    p = _файл(папка, "akt-10.txt")
    _дважды(watch)
    p.write_text("Акт № 10 ИСПРАВЛЕННЫЙ на сумму 90 000 ₽", encoding="utf-8")
    import os
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 60))
    _дважды(watch)
    assert len(запуски) == 2, "правка документа осталась незамеченной"


# ── деньги и аккуратность ────────────────────────────────────────────────────────────────────────

def test_лежавшее_до_присмотра_не_берётся(присмотр):
    """Сотня прогонов, которых никто не просил, — это счёт заказчику."""
    watch, папка, запуски = присмотр
    for i in range(3):
        _файл(папка, f"старое-{i}.txt")
    r = _добавить(watch, папка)
    assert r["skipped"] == 3, r
    _дважды(watch)
    assert запуски == [], "присмотр взял файлы, лежавшие до его включения"
    журнал = watch.list_folders()["folders"][0]["журнал"]
    assert all(з["state"] == "был до присмотра" for з in журнал), журнал
    assert all(з["note"] for з in журнал), "состояние без причины — человеку нечего понять"


def test_взять_лежавшее_можно_явно(присмотр):
    watch, папка, запуски = присмотр
    for i in range(2):
        _файл(папка, f"старое-{i}.txt")
    r = _добавить(watch, папка)
    осв = watch.take_existing(r["id"])
    assert осв["released"] == 2, осв
    _дважды(watch)
    assert len(запуски) == 2


def test_за_заход_берётся_не_больше_предела_и_остаток_назван(присмотр):
    watch, папка, запуски = присмотр
    _добавить(watch, папка)
    for i in range(watch.ПРЕДЕЛ_ЗА_ЗАХОД + 3):
        _файл(папка, f"akt-{i}.txt")
    сводка = _дважды(watch)
    assert len(запуски) == watch.ПРЕДЕЛ_ЗА_ЗАХОД, len(запуски)
    assert "ещё 3" in сводка["почему"], (
        f"остаток не назван числом — человек не узнает, что файлы ждут: {сводка['почему']!r}")
    _дважды(watch)
    assert len(запуски) == watch.ПРЕДЕЛ_ЗА_ЗАХОД + 3, "остаток так и не взяли"


def test_докопировка_не_берётся(присмотр):
    """Размер обязан совпасть в двух заходах: иначе агент разберёт половину акта."""
    watch, папка, запуски = присмотр
    _добавить(watch, папка)
    p = _файл(папка, "растёт.txt", "начало")
    watch._заход()
    p.write_text("начало и продолжение, файл ещё копировался", encoding="utf-8")
    watch._заход()
    assert запуски == [], "файл взят, пока он ещё дописывался"
    watch._заход()
    assert len(запуски) == 1, "файл дописался, а присмотр его так и не взял"
    assert "продолжение" in запуски[0]["context"], "взята прежняя, короткая версия"


# ── отказ виден ──────────────────────────────────────────────────────────────────────────────────

def test_скан_без_текста_попадает_в_журнал_с_причиной(присмотр):
    watch, папка, запуски = присмотр
    r = _добавить(watch, папка)
    _файл(папка, "скан.txt", "   ")
    _дважды(watch)
    assert запуски == [], "пустой файл ушёл агенту"
    журнал = watch.list_folders()["folders"][0]["журнал"]
    отказ = next(з for з in журнал if з["name"] == "скан.txt")
    assert отказ["state"] == "не вышло"
    assert "Распознавание" in отказ["note"], (
        "сказано, что не вышло, но не сказано, что с этим делать")
    assert watch.list_folders()["folders"][0]["счёт"].get("не вышло") == 1
    assert r["ok"]


def test_упавший_запуск_не_съедает_файл(присмотр, monkeypatch):
    """Прогон не запустился — это «не вышло» с причиной, а не тишина."""
    watch, папка, _ = присмотр
    from sidecar import abop_client as abop

    def boom(**kw):
        raise abop.AbopError(503, "очередь переполнена")

    monkeypatch.setattr(abop, "run_async", boom)
    _добавить(watch, папка)
    _файл(папка, "akt-11.txt")
    _дважды(watch)
    журнал = watch.list_folders()["folders"][0]["журнал"]
    отказ = next(з for з in журнал if з["name"] == "akt-11.txt")
    assert отказ["state"] == "не вышло"
    assert "очередь переполнена" in отказ["note"], "ответ сервера потерян — причина неизвестна"


def test_пропавшая_папка_названа(присмотр):
    watch, папка, _ = присмотр
    _добавить(watch, папка)
    папка.rename(папка.parent / "переехала")
    сводка = watch._заход()
    assert "не найдена" in сводка["почему"], сводка
    assert watch.list_folders()["folders"][0]["exists"] is False, (
        "интерфейс не узнает, что папки больше нет")


def test_без_входа_присмотр_честно_стоит(присмотр, monkeypatch):
    watch, папка, запуски = присмотр
    from sidecar import auth
    _добавить(watch, папка)
    _файл(папка, "akt-12.txt")
    monkeypatch.setattr(auth, "token", lambda: None)
    сводка = watch._заход()
    assert запуски == []
    assert "ждёт входа" in сводка["почему"], сводка
    assert watch.list_folders()["authed"] is False


# ── границы ──────────────────────────────────────────────────────────────────────────────────────

def test_папка_принадлежит_области(присмотр):
    """Папка заказчика не присматривается, когда вошли в другую установку."""
    watch, папка, _ = присмотр
    from sidecar import config
    _добавить(watch, папка)
    assert len(watch._папки()) == 1
    config.ABOP = "http://abop.beta.ru:8091"
    assert watch._папки() == [], "папка одной установки видна в другой"


def test_маска_ограничивает_расширения(присмотр):
    watch, папка, запуски = присмотр
    _добавить(watch, папка, mask="pdf")
    _файл(папка, "akt.txt")
    _дважды(watch)
    assert запуски == [], "маска не ограничила расширения"
    assert watch._подходит("x.pdf", "pdf") and not watch._подходит("x.txt", "pdf")
    assert watch._подходит("x.txt", ""), "без маски берём всё, что умеем читать"


def test_без_агента_папку_не_добавить(присмотр):
    watch, папка, _ = присмотр
    r = _добавить(watch, папка, agent_id="")
    assert r["ok"] is False and "агент" in r["error"]


def test_пауза_останавливает_обход(присмотр):
    watch, папка, запуски = присмотр
    r = _добавить(watch, папка)
    watch.pause(r["id"], on=0)
    _файл(папка, "akt-13.txt")
    _дважды(watch)
    assert запуски == []
    watch.pause(r["id"], on=1)
    _дважды(watch)
    assert len(запуски) == 1


def test_убрать_папку_снимает_и_журнал(присмотр):
    watch, папка, _ = присмотр
    r = _добавить(watch, папка)
    _файл(папка, "akt-14.txt")
    _дважды(watch)
    watch.remove(r["id"])
    assert watch.list_folders()["folders"] == []
    from sidecar import db
    assert db.q("SELECT 1 FROM watched_seen WHERE folder_id=?", (r["id"],)) == []


# ── интерфейс ────────────────────────────────────────────────────────────────────────────────────

def test_журнал_и_цена_названы_в_панели():
    for ожидаем in ("журналHTML", "был до присмотра", "не вышло", "стоят денег"):
        assert ожидаем in ПАНЕЛЬ, f"в разделе нет «{ожидаем}»"
    assert "не превратится в пятьсот прогонов" in ПАНЕЛЬ, "предел за заход человеку не объяснён"


def test_удаление_папки_предупреждает_о_повторных_прогонах():
    i = ПАНЕЛЬ.index("Убрать папку из присмотра?")
    блок = ПАНЕЛЬ[i:i + 700]
    assert "те же документы" in блок or "тем же документам" in блок, (
        "не сказано, что после возврата папки те же файлы пройдут заново")
    assert "okLabel" in блок and "danger: true" in блок


def test_выбор_папки_диалогом_а_в_браузере_руками():
    assert "os:pickFolder" in MAIN and "openDirectory" in MAIN
    assert "pickFolder" in ПАНЕЛЬ
    i = ПАНЕЛЬ.index("window.ape?.os?.pickFolder")
    assert "впишите путь вручную" in ПАНЕЛЬ[i:i + 400], (
        "в браузере кнопка молча ничего не делает")
