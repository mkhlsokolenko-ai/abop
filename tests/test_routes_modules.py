# -*- coding: utf-8 -*-
"""Модули маршрутов после расщепления монолита (п.23 исследования 08.10).

При переносе домена из `web_api.py` ломается не поведение, а ИМЕНА: функция уехала, а звавший её
код остался — и наоборот. Python обнаруживает это только в момент вызова, то есть на живом
запросе. За один заход такая ошибка повторилась четыре раза подряд, и каждый раз её ловил не
тест, а падение ручки.

Проверка статическая: разбираем AST каждого модуля HTTP-слоя и ищем имена, которые читаются, но
нигде не объявлены. Это дешевле и надёжнее, чем надеяться, что все ручки покрыты интеграционным
тестом.
"""
from __future__ import annotations

import ast
import builtins
import pathlib

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
СЕРВЕР = КОРЕНЬ / "server"
МОДУЛИ = [СЕРВЕР / "web_api.py", СЕРВЕР / "deps.py"] + \
         sorted(p for p in (СЕРВЕР / "routes").glob("*.py") if p.name != "__init__.py")


def _неразрешённые(путь: pathlib.Path) -> list[str]:
    дерево = ast.parse(путь.read_text(encoding="utf-8"))
    объявлены = set(dir(builtins))
    локальные: set[str] = set()
    for n in ast.walk(дерево):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                объявлены.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            объявлены.add(n.name)
            арг = getattr(n, "args", None)
            for a in (getattr(арг, "args", []) or []) + (getattr(арг, "kwonlyargs", []) or []) \
                    + (getattr(арг, "posonlyargs", []) or []):
                локальные.add(a.arg)
            for имя in ("vararg", "kwarg"):
                if getattr(арг, имя, None):
                    локальные.add(getattr(арг, имя).arg)
        elif isinstance(n, ast.Lambda):
            # Аргументы lambda — тоже объявления: без них `key=lambda kv: -kv[1]` читается как
            # обращение к неизвестному имени.
            арг = n.args
            for a in (getattr(арг, "args", []) or []) + (getattr(арг, "kwonlyargs", []) or [])                     + (getattr(арг, "posonlyargs", []) or []):
                локальные.add(a.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            локальные.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            локальные.add(n.name)
        elif isinstance(n, ast.Global):
            локальные.update(n.names)
    читаются = {n.id for n in ast.walk(дерево)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(читаются - объявлены - локальные - {"__file__", "__name__", "__doc__"})


def test_в_http_слое_нет_неразрешённых_имён():
    """Главная проверка: ручка не должна падать на NameError при первом же вызове."""
    беда = {}
    for m in МОДУЛИ:
        нет = _неразрешённые(m)
        if нет:
            беда[m.name] = нет
    assert not беда, "имена читаются, но нигде не объявлены: " + str(беда)


def test_модули_маршрутов_не_тянут_приложение_сверху():
    """Импорт `web_api` на верхнем уровне модуля маршрутов — это цикл и возврат монолита.

    Поздних импортов внутри функций тоже не осталось: единственный был у повтора задания из DLQ
    и держался на том, что прогон ещё жил в монолите. Слой исполнения вынесен — долг закрыт, и
    проверка теперь требует ноль, а не «не больше одного».
    """
    нарушители = []
    поздние = []
    for m in (p for p in МОДУЛИ if p.parent.name == "routes"):
        дерево = ast.parse(m.read_text(encoding="utf-8"))
        верхние = {id(n) for n in дерево.body}
        for n in ast.walk(дерево):
            if not isinstance(n, (ast.Import, ast.ImportFrom)):
                continue
            имена = [a.name for a in n.names] + ([n.module] if isinstance(n, ast.ImportFrom) else [])
            if any("web_api" in str(x) for x in имена):
                (нарушители if id(n) in верхние else поздние).append(m.name)
    assert not нарушители, "модуль маршрутов импортирует приложение сверху: " + str(нарушители)
    # Долг закрыт 09.10: слой исполнения прогона вынесен в server/run_exec.py, и единственный
    # поздний импорт (повтор задания из DLQ) стал обычным. Теперь их должно быть НОЛЬ — иначе
    # «временное исключение» заведётся снова.
    assert not поздние, (
        "поздний импорт web_api вернулся в домен: " + str(sorted(set(поздние))))


def test_аутентификация_берётся_из_общего_модуля():
    """Своя копия разбора токена в домене — худшее, что может случиться при расщеплении."""
    for m in (p for p in МОДУЛИ if p.parent.name == "routes"):
        текст = m.read_text(encoding="utf-8")
        if "Depends(user)" not in текст:
            continue
        assert "from ..deps import" in текст, m.name + ": user берётся не из server/deps"
        assert "jwt.decode" not in текст, m.name + ": домен завёл свой разбор токена"


def test_домены_не_пустые_и_не_слиплись():
    """Модуль без ручек — мусор; модуль на полсотни — не домен, а второй монолит."""
    for m in (p for p in МОДУЛИ if p.parent.name == "routes"):
        n = m.read_text(encoding="utf-8").count("@router.")
        assert 1 <= n <= 40, f"{m.name}: {n} ручек — это не домен"
