# -*- coding: utf-8 -*-
"""Находки стресс-теста 05.10 — каждая закрыта проверкой.

Прогон сценария из docs/STRESS_TEST_DESKTOP.md десктопным путём дал пять дефектов. Первый (страница
входа вместо документа) закрыт в tests/test_doclink.py, остальные — здесь.
"""
from __future__ import annotations

import pathlib

from server import planner

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_подпись_к_ссылке_уходит_вместе_со_ссылкой():
    """№2. «Регламент: http://…» и «Выгрузка счетов: http://…» — указание источника, а не работа.
    Без подписи в тексте оставалось «Регламент:  Выгрузка счетов:», и слово «выгрузка» делало из
    хвоста отдельный этап, который подбирал себе исполнителя."""
    t = ("собери финотчёт с KPI. Регламент: http://host:6875/books/1/page/reglament "
         "Выгрузка счетов: http://host:9000/abop-demo/1c/invoices.json")
    чисто = planner.without_links(t)
    assert "http" not in чисто and "Регламент" not in чисто and "Выгрузка" not in чисто, чисто
    assert len(planner.clauses(t)) == 1, planner.clauses(t)


def test_этапом_считается_только_работа():
    """Кусок без действия — не этап: это подпись, заголовок или остаток разметки."""
    парт = planner.clauses("посчитай дельты по статьям. Приложение 2. Таблица на следующей странице")
    assert len(парт) == 1, парт


def test_сводка_строится_на_обычном_прогоне():
    """№4. Строки «что сказал прогон» собирались ТОЛЬКО при попадании в кэш. На обычном прогоне
    карточка показывала «находок 7» и ни слова о том, что нашли, — при 3–6 тысячах знаков у каждого
    навыка."""
    src = (ROOT / "server" / "web_api.py").read_text(encoding="utf-8")
    assert src.count('result["summary_lines"] = run_summary(result)') == 2, \
        "сводка снова строится только на одном из путей"
    i = src.index("_entries = result.pop(\"board_entries\"")
    assert "summary_lines" in src[max(0, i - 400):i], "сводка не строится перед сохранением прогона"


def test_заминка_опроса_не_выглядит_зависанием():
    """№3. Ответ без статуса (сервер подвис) обнулял строку состояния: фаза «—», навыков 0/0."""
    chat = (ROOT / "desktop" / "ui" / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")
    assert "j.ok === false || !j.status" in chat, "пустой ответ опроса снова рисуется как прогресс"
    assert "сервер отвечает с задержкой" in chat, "человеку не сказано, что это заминка связи"


def test_повторный_опрос_не_ходит_в_сеть():
    """№5. Два окна (или забытая вкладка ожидания) опрашивают одно задание, и каждый опрос занимал
    поток сайдкара на время сетевого запроса — запуск нового прогона переставал доходить до сервера."""
    mod = (ROOT / "desktop" / "sidecar" / "modules" / "chat" / "module.py").read_text(encoding="utf-8")
    assert "_JOB_CACHE" in mod and "_JOB_TTL" in mod, "короткого кэша опроса нет"
    i = mod.index("def run_job_status")
    head = mod[i:i + 600]
    assert "_JOB_CACHE.get(job_id)" in head, "кэш не читается перед походом в сеть"
    assert 'not (_c[1] or {}).get("done")' in head, "готовый результат нельзя держать в кэше статуса"


def test_ci_требует_релиз_только_за_релиз_гейтед():
    """Проверка версии падала на каждой правке UI, хотя интерфейс едет «Путём А» без релиза —
    пять падений CI подряд приучали проходить мимо проверки."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'desktop/sidecar desktop/electron | wc -l' in ci, "релиз снова требуется и за UI"
    assert "UIONLY" in ci and "Путём А" in ci, "про Путь А в проверке не сказано"


def test_лишние_разделы_убраны_из_маршрута_но_не_удалены():
    """Владелец 05.10: «Находки и Источники точно лишние». Находки видны в карточке прогона и в
    отчёте, подключение источников — работа по управлению, её место в вебе. Но убрать кнопку не
    значит отнять раздел: оба открываются из палитры команд."""
    app = (ROOT / "desktop" / "ui" / "core" / "app.js").read_text(encoding="utf-8")
    i = app.index("const HIDDEN_MODULES")
    строка = app[i:i + 200]
    assert '"findings"' in строка and '"connectors"' in строка, "разделы всё ещё в маршруте"
    assert '"connectors"' not in app[app.index("const RAIL_ORDER"):app.index("const RAIL_ORDER") + 160], \
        "источники остались в порядке маршрута"
    cmds = app[app.index("function buildCommands"):app.index("function openPalette")]
    assert "MODULES.filter" in cmds and "HIDDEN_MODULES.has" in cmds, \
        "палитра перестала показывать убранные разделы — это уже не уборка, а потеря функции"
