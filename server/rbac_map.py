# -*- coding: utf-8 -*-
"""Карта прав, выведенная из кода, а не из текста о коде.

Экран «Роли и области» показывал две колонки «разрешено / запрещено», которые были литеральным
словарём рядом с ручкой. Словарь никто не сверял с фактическими гейтами `require_level(...)`, и
экран отвечал на вопрос «что мы написали про права», а не «как права работают». Для продукта,
который продаёт управляемость, это худшее место для декларации.

Здесь матрица собирается разбором собственного исходника: каждый роут `@app.<метод>("/api/…")`
сопоставляется с `require_level(u, "<уровень>")` в его теле. Нет вызова — роут доступен любому
аутентифицированному (ABAC по отделу при этом продолжает действовать отдельно).

Разбор идёт по AST, а не регуляркой: иначе закомментированный или упомянутый в строке вызов
посчитался бы за настоящий гейт — ровно та подмена декларации фактом, против которой модуль и
написан.
"""
from __future__ import annotations

import ast
import pathlib

УРОВНИ = ("analyst", "manager", "support", "admin")
_ИСХОДНИК = pathlib.Path(__file__).with_name("web_api.py")
_КЭШ: dict | None = None


def _гейт(узел: ast.AST) -> str | None:
    """Минимальный уровень, который требует тело функции, или None."""
    for n in ast.walk(узел):
        if not isinstance(n, ast.Call):
            continue
        имя = n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        if имя != "require_level" or len(n.args) < 2:
            continue
        арг = n.args[1]
        if isinstance(арг, ast.Constant) and isinstance(арг.value, str):
            return арг.value
    return None


def _роуты(узел: ast.AST) -> list[tuple[str, str]]:
    """Пары (метод, путь) из декораторов `@app.get("/api/…")`."""
    out = []
    for d in getattr(узел, "decorator_list", []):
        if not isinstance(d, ast.Call) or not isinstance(d.func, ast.Attribute):
            continue
        хозяин = getattr(d.func.value, "id", "")
        метод = d.func.attr
        if хозяин != "app" or метод not in ("get", "post", "put", "patch", "delete"):
            continue
        if d.args and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str):
            out.append((метод.upper(), d.args[0].value))
    return out


def собрать() -> dict:
    """{level: {"gated": [{method, path, level}], "open": int}} + сводка. Считается один раз."""
    global _КЭШ
    if _КЭШ is not None:
        return _КЭШ
    дерево = ast.parse(_ИСХОДНИК.read_text(encoding="utf-8"))
    по_уровням: dict[str, list[dict]] = {lv: [] for lv in УРОВНИ}
    открытых = 0
    всего = 0
    for узел in ast.walk(дерево):
        if not isinstance(узел, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        роуты = _роуты(узел)
        if not роуты:
            continue
        уровень = _гейт(узел)
        for метод, путь in роуты:
            всего += 1
            if уровень in по_уровням:
                по_уровням[уровень].append({"method": метод, "path": путь})
            else:
                открытых += 1
    for lv in по_уровням:
        по_уровням[lv].sort(key=lambda r: (r["path"], r["method"]))
    _КЭШ = {"by_level": по_уровням, "open": открытых, "total": всего,
            "source": "ast:" + _ИСХОДНИК.name}
    return _КЭШ


def для_уровня(уровень: str) -> dict:
    """Что фактически может уровень: свои гейты плюс всё, что требует уровня ниже.

    Шкала накопительная (`analyst < manager < support < admin`), поэтому admin проходит и те
    гейты, которые объявлены для manager. Матрица обязана это показывать так же, как рантайм.
    """
    карта = собрать()
    if уровень not in УРОВНИ:
        return {"allowed": [], "denied": [], "open": карта["open"]}
    порог = УРОВНИ.index(уровень)
    можно: list[dict] = []
    нельзя: list[dict] = []
    for lv in УРОВНИ:
        сюда = можно if УРОВНИ.index(lv) <= порог else нельзя
        for r in карта["by_level"][lv]:
            сюда.append({**r, "needs": lv})
    return {"allowed": можно, "denied": нельзя, "open": карта["open"]}


def сводка(уровень: str) -> dict:
    """Короткие числа для карточки роли: сколько мутаций открыто и сколько закрыто."""
    d = для_уровня(уровень)
    return {"allowed_count": len(d["allowed"]), "denied_count": len(d["denied"]),
            "open_count": d["open"],
            "denied_sample": [r["method"] + " " + r["path"] for r in d["denied"][:6]],
            "allowed_sample": [r["method"] + " " + r["path"] for r in d["allowed"][-6:]]}


def _сброс_для_тестов() -> None:
    global _КЭШ
    _КЭШ = None
