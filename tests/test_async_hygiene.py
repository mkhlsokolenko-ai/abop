"""Файловое и сетевое чтение не выполняется в событийном цикле.

Сборка контекста отчёта читает canonical store (строка «Источник данных» с числом записей и
свежестью) — это открытие файла и разбор JSONL до сотни тысяч строк. Вызов её напрямую из
async-обработчика останавливает весь сервис на время разбора: один отчёт подвешивает остальные
запросы. Поэтому такие функции вызываются только через `asyncio.to_thread`.

Проверка статическая, по дереву разбора: она ловит ситуацию в момент правки, а не на нагрузке.
"""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "web_api.py"
TREE = ast.parse(SRC.read_text(encoding="utf-8"))

# Синхронные функции, которые ходят на диск: в async-обработчике только через to_thread.
BLOCKING = {"_report_context"}


def _async_bodies():
    for node in ast.walk(TREE):
        if isinstance(node, ast.AsyncFunctionDef):
            yield node


def test_синхронное_чтение_диска_не_вызывается_из_цикла():
    bad = []
    for fn in _async_bodies():
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id not in BLOCKING:
                continue
            # разрешено только как аргумент to_thread: asyncio.to_thread(_report_context, ...)
            bad.append(f"{fn.name}:{node.lineno} → {node.func.id}()")
    assert not bad, ("синхронное чтение диска в событийном цикле — обернуть в asyncio.to_thread:\n  "
                     + "\n  ".join(bad))


def test_обёртка_через_поток_на_месте():
    """Обратная сторона: функция должна где-то вызываться — иначе проверка стала бы бессмысленной."""
    refs = [n for n in ast.walk(TREE)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "to_thread"
            and any(isinstance(a, ast.Name) and a.id in BLOCKING for a in n.args)]
    assert len(refs) >= 3, f"сборка контекста отчёта уходит в поток только {len(refs)} раз"


def test_в_web_api_нет_голого_asyncio():
    """`web_api` импортирует asyncio как `_asyncio`. Любое обращение к голому `asyncio.` без
    локального импорта — это NameError, который сработает только на живом пути.

    Так и случилось 05.10: чтение документа по ссылке было написано как `asyncio.to_thread`, тесты
    этого не видели (ни один не гоняет прогон со ссылкой), и прогон падал ровно в том сценарии, ради
    которого делался. Разбираем деревом, а не поиском по строкам: локальный `import asyncio` внутри
    функции — законный приём, и ругаться на него нельзя.
    """
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "server" / "web_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad: list[str] = []

    def uses(node) -> list[int]:
        out = []
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "asyncio":
                out.append(n.lineno)
        return out

    def imported(node) -> bool:
        for n in ast.walk(node):
            if isinstance(n, ast.Import) and any(a.name == "asyncio" and not a.asname for a in n.names):
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not imported(node):
            for ln in uses(node):
                bad.append(f"{node.name}, строка {ln}")
    assert not bad, "голый asyncio без локального импорта: " + "; ".join(bad)
