"""Обрыв опроса не должен выбрасывать человека из чата.

03.10 владелец заметил: чат «постоянно обновляется» — начатый новый чат пропадает, и открывается
последний, где шла цепочка. Причина цепочкой: любой неудачный запрос (status 0/502/503/504) помечал
связь потерянной, следующий успешный пинг считал её восстановленной и ПЕРЕСОБИРАЛ все разделы, а
чат при монтировании открывал самый свежий тред. Свежий — это тот, где только что был прогон: он
поднимается наверх списка.

Поэтому два правила, и оба проверяются статически: возврат связи обновляет раздел на месте, а чат
при монтировании возвращается туда, где человек был.
"""
from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "desktop" / "ui"
APP = (UI / "core" / "app.js").read_text(encoding="utf-8")
CHAT = (UI / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")


def test_возврат_связи_не_пересобирает_разделы():
    m = re.search(r"if \(ok && !linkOk\) \{([^}]*)\}", APP)
    assert m, "не нашёл ветку восстановления связи"
    body = m.group(1)
    assert "reloadAll" not in body, "возврат связи пересобирает разделы — незаписанное состояние теряется"
    assert "relinkModules" in body, "возврат связи не обновляет разделы вовсе"


def test_пересборка_осталась_для_смены_пользователя():
    """reloadAll нужен там, где сброс правилен: вход и выход из учётной записи."""
    assert "reloadAll()" in APP
    assert APP.count("reloadAll()") >= 2, "пересборка пропала там, где она уместна"


def test_мягкое_обновление_доходит_до_раздела():
    assert 'dispatchEvent(new CustomEvent("ape:relink"))' in APP
    assert 'addEventListener("ape:relink"' in CHAT, "чат не слушает возврат связи"


def test_чат_возвращается_в_тот_же_тред():
    """Открытый чат запоминается, и монтирование не подменяет его самым свежим из списка."""
    assert "LS_OPEN" in CHAT and "localStorage" in CHAT
    assert re.search(r"openThread\(prev\)", CHAT), "нет возврата в ранее открытый чат"
    bare = re.findall(r"(?<!else if \(!last && threads\.length\) )await openThread\(threads\[0\]\)", CHAT)
    assert not bare, "чат всё ещё открывает самый свежий тред без оглядки на прежнее состояние"


def test_начатый_новый_чат_не_подменяется():
    """Пометка «новый» означает: человек начал новый чат и ещё ничего не отправил."""
    assert "NEW_MARK" in CHAT
    assert re.search(r"last !== NEW_MARK", CHAT), "пометка нового чата не учитывается при монтировании"


def test_черновик_переживает_пересборку():
    assert "LS_DRAFT" in CHAT
    assert re.search(r"remember\(LS_DRAFT, null\)", CHAT), "черновик не очищается после отправки"
    assert re.search(r'\$\("inp"\)\.value = draft', CHAT), "черновик не возвращается в поле ввода"


def test_доступ_к_хранилищу_не_роняет_раздел():
    """В приватном окне и при запрете на данные сайта localStorage бросает — читаем через try/catch."""
    for fn in ("function remember", "function recall"):
        i = CHAT.index(fn)
        assert "try {" in CHAT[i:i + 260], f"{fn}: обращение к localStorage без try/catch"


# ── отмена предложения и сборка из навыков ───────────────────────────────────────────────────────

def test_карточка_предложения_имеет_отмену():
    """Отказаться от запуска было нечем: на карточке стояла одна кнопка «Собрать и запустить»."""
    assert "function cardCloseHTML" in CHAT and "cancelCard" in CHAT
    for kicker in ("цепочка из ${cs.steps.length} шагов", "задача для агента",
                   "готового агента нет — соберём из навыков"):
        i = CHAT.index(kicker)
        assert "cardCloseHTML" in CHAT[i:i + 200], f"у карточки «{kicker[:28]}…» нет крестика"
    assert 'class="btn sm cardNo"' in CHAT, "нет явной кнопки отказа"


def test_отмена_не_теряет_задачу_и_держится_после_перезагрузки():
    i = CHAT.index("async function cancelCard")
    body = CHAT[i:i + 1200]
    assert "remember(LS_DRAFT, task)" in body, "текст задачи не возвращается в поле ввода"
    assert "/meta" in body and "PATCH" in body, "отмена не записывается в историю — карточка вернётся"
    assert "meta.cancelled ? cancelledHTML" in CHAT, "отменённая карточка рендерится как живая"


def test_нет_агента_значит_сборка_из_навыков():
    """Готового агента нет — предлагаем цепочку из навыков, а не просто отвечаем текстом."""
    i = CHAT.index("if (top && top.score >= 0.32)")
    tail = CHAT[i:i + 900]
    assert '/plan"' in tail, "после неудачного подбора план из навыков не запрашивается"
    assert "assembleCard" in tail, "план не предлагается человеку"
    assert "sendPrompt(v)" in tail, "без плана чат должен просто ответить"
    assert "function buildAndRunPlan" in CHAT and "/plan/build" in CHAT, "сборку нечем выполнить"
