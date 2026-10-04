# -*- coding: utf-8 -*-
"""Чат отвечает текстом и не отчитывается за систему; подбор идёт только по команде.

04.10 владелец прислал переписку, где модель написала: «цепочка выполнена», «отчёт собран в PDF»,
имя файла «отчет_по_идеи_сервиса_над_гитхабом.pdf» и путь «docs/». Ни файла, ни отчёта не
существовало — под сообщением стояла подпись модели и ноль рублей. Это худший вид отказа: не ошибка,
а правдоподобный отчёт о несделанном.

Там же видно вторую беду: подбор исполнителя запускался на КАЖДОЕ сообщение, поэтому уточнения в
переписке превращались в предложения собрать агента, а потокового ответа не было вовсе — вместо него
приходила карточка.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

from server import web_api  # noqa: E402

CHAT = (ROOT / "desktop" / "ui" / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")
SRC = (ROOT / "server" / "web_api.py").read_text(encoding="utf-8")


def test_рамка_запрещает_отчитываться_за_систему():
    g = web_api.CHAT_GUARD
    for must in ("не выполняешь действий", "не выдумывай", "/work", "карточек прогона"):
        assert must in g, f"в рамке нет правила про «{must}»"
    for word in ("собрал", "отправил", "прикрепил"):
        assert word not in g.lower().split(), "рамка не должна сама подсказывать такие формулировки"


def test_рамка_стоит_первой_в_обоих_каналах():
    """И в обычном ответе, и в стриминге: правило рантайма не зависит от канала."""
    blocks = re.findall(r'messages: list\[dict\] = \[\{"role": "system", "content": CHAT_GUARD\}\]', SRC)
    assert len(blocks) == 2, f"рамка подставлена в {len(blocks)} из 2 каналов"


def test_рамка_живёт_на_сервере_а_не_в_клиенте():
    """Иначе она действовала бы только в обновлённом десктопе, а старые клиенты врали бы дальше."""
    side = (ROOT / "desktop" / "sidecar" / "modules" / "chat" / "module.py").read_text(encoding="utf-8")
    assert "CHAT_GUARD" not in side, "рамка переехала в клиент — на сервере она надёжнее"
    assert "CHAT_GUARD = (" in SRC


def test_подбор_только_по_команде():
    i = CHAT.index("async function sendFromInput")
    body = CHAT[i:i + 2600]
    assert "work|задача|агент" in body, "команда /work не разбирается"
    assert "decideAndOffer(task)" in body, "подбор не вызывается по команде"
    assert "sendPrompt(v)" in body, "обычное сообщение должно уходить в чат потоком"
    # Решение не должно вызываться на каждое сообщение: вне ветки /work его быть не может.
    before = body[:body.index("const work = v.match")]
    assert "decide(" not in before, "решение всё ещё принимается до проверки команды"


def test_мёртвой_ветки_не_осталось():
    assert "if (false)" not in CHAT, "в коде осталась отключённая ветка вместо удалённой"


def test_команда_объявлена_человеку():
    assert "/work — подобрать или собрать агента" in CHAT, "подсказки про команду нет в поле ввода"
    i = CHAT.index('.clDraft')
    assert '"/work "' in CHAT[i:i + 700], "«Дописать задачу» не возвращает команду — подбор не повторится"


def test_карточка_показывает_что_сказал_прогон():
    """Для навыка, отдающего документ, счётчик находок равен нулю — результат надо показать словами."""
    i = CHAT.index("function runCard")
    body = CHAT[i:CHAT.index("function pipelineHTML", i)]   # функция длинная: берём её целиком
    assert "s.summary" in body, "карточка не показывает сводку прогона"
    assert "Находок не выявлено" in body and "(s.summary || []).length ? \"\"" in body, \
        "«находок нет» печатается даже когда есть что сказать"
    assert "def run_summary" in SRC and '"summary": run_summary(result)' in SRC, \
        "сервер не передаёт сводку в шаги цепочки"
