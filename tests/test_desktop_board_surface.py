"""Общая доска в десктопе: поверхность, кнопка «на доску» и прокси сайдкара.

Владелец просил отдельную поверхность: «единый чат, куда агрегируется дистиляция фактов… отдельное
от остальных чатов расстоянием и оранжевой рамкой». Доска именно отдельная: это долгая память
человека и отдела, а не переписка. Отсюда проверки:

1. Доска живёт на сервере — интерфейс ходит в `/board`, а не хранит факты у себя в вкладке: человек
   должен видеть их с любой машины, и агенты по контракту читают ровно эти же записи.
2. Открытие доски не трогает открытый разговор и набранный текст (ровно та беда, из-за которой
   чат уже один раз «постоянно обновлялся»).
3. Кнопка «🧷 На доску» на карточке прогона имеет живой маршрут в сайдкаре — иначе она отдаёт 404,
   а человек видит «ABOP недоступен».
4. Автором факта становится человек: сервер ставит автора сам, интерфейс его не подменяет.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAT = (ROOT / "desktop" / "ui" / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")
SIDE = (ROOT / "desktop" / "sidecar" / "modules" / "agents" / "module.py").read_text(encoding="utf-8")
CLIENT = (ROOT / "desktop" / "sidecar" / "abop_client.py").read_text(encoding="utf-8")
WEBAPI = (ROOT / "server" / "web_api.py").read_text(encoding="utf-8")


def test_доска_читается_с_сервера():
    assert '/board?kind=" + boardKind' in CHAT, "доска не читается с сервера"
    assert "localStorage" not in CHAT.split("async function loadBoard")[1].split("function renderBoard")[0], \
        "факты доски кэшируются в вкладке — чистка кэша не должна их уносить"


def test_у_доски_оранжевая_рамка_и_своё_место():
    """Оранжевая рамка — не украшение: она отделяет общую память от переписки."""
    board = CHAT.split("function renderBoard()")[1].split("function openBoard()")[0]
    assert "var(--warn-line)" in board, "рамка доски не оранжевая"
    assert "boardPin" in CHAT, "доска не закреплена отдельной кнопкой в списке чатов"


def test_открытие_доски_не_трогает_разговор():
    """Доска — отдельное состояние: ни открытый чат, ни черновик не сбрасываются."""
    body = CHAT.split("function openBoard()")[1].split("function closeBoard()")[0]
    for forbidden in ("messages = []", "cur = null", "loadThreads("):
        assert forbidden not in body, f"открытие доски делает лишнее: {forbidden}"


def test_работа_уходит_в_разговор_а_не_на_доску():
    """«▶ В задачу» и отправка сообщения возвращают человека в чат: доска не исполняет."""
    assert "if (boardMode) closeBoard();" in CHAT, "отправка из доски оставляет человека на доске"
    assert "closeBoard(); $(\"inp\").value = boardTaskText(r)" in CHAT, "кнопка «в задачу» не ведёт в чат"


def test_кнопка_на_доску_имеет_маршрут():
    assert '/board/from-run/" + encodeURIComponent' in CHAT, "кнопки «на доску» нет в карточке прогона"
    assert '@router.post("/board/from-run/{run_id}")' in SIDE, "сайдкар не проксирует вынос на доску"
    assert "def board_from_run(" in CLIENT and "to-board" in CLIENT, \
        "клиент сайдкара не умеет выносить прогон на доску"
    assert '@app.post("/api/runs/{run_id}/to-board")' in WEBAPI, "серверной ручки выноса нет"


def test_снятие_факта_спрашивает_подтверждение():
    """Снятие факта — необратимое действие для чужих прогонов тоже: спрашиваем."""
    drop = CHAT.split('querySelectorAll(".bDrop")')[1].split("});")[0]
    assert "confirmDialog" in drop, "факт снимается без подтверждения"
    assert 'method: "DELETE"' in drop


def test_автора_факта_ставит_сервер():
    """Автор — человек, а не агент: это его утверждение, даже если значение из прогона."""
    put = WEBAPI.split("async def board_from_run(")[1].split("@app.")[0]
    assert "author=f\"{run.get('agent_name')" in put, "в факте не видно, какой прогон его дал"
    body = CHAT.split('querySelectorAll(".toBoard")')[1].split("});")[0]
    assert "author" not in body, "интерфейс подменяет автора факта"


def test_обе_области_доски_доступны_человеку():
    """Личная и отдела — разные области: отдел проверяется правами на сервере."""
    assert 'tab("user"' in CHAT and 'tab("family"' in CHAT, "в доске нет выбора области"
    scope = WEBAPI.split("def _board_scope(")[1].split("\n@app.")[0]
    assert "can_see_family" in scope, "доска отдела отдаётся без проверки области видимости"
