# -*- coding: utf-8 -*-
"""Чекпойнт шага цепочки: режем контекст, а не источник входа.

03.10 владелец проверял сборку агента из навыков и получил «Сбой цепочки: … (TypeError: unhashable
type: 'slice')». Причина не в сборке: в `_pipeline_job` ограничение длины стояло на соседнем ключе —
`prev_src[:6000]` вместо `prev_ctx`. `prev_src` это словарь {шаг, агент, прогон}, срез словаря —
исключение, поэтому падала ЛЮБАЯ цепочка из очереди. Падала после того, как агент уже отработал:
прогон сохранялся, а задание рушилось, и человек видел «сбой» вместо результата.

Регрессия жила с 01.10 (коммит 63c7f33) и живым прогоном не ловилась, потому что цепочки на стенде
запускались вторым путём — без очереди.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "ABOP_BUS", "ABOP_KAFKA_BROKERS"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

from server.web_api import _pipeline_checkpoint  # noqa: E402

SRC = {"шаг": "p1", "агент": "Оценка идеи", "прогон": "run-1"}


def test_источник_входа_остаётся_объектом():
    """Его читает следующий шаг и карточка прогона: «от кого пришёл вход»."""
    cp = _pipeline_checkpoint(1, 3, [{"agent_id": "a"}], "короткий контекст", SRC)
    assert cp["prev_src"] == SRC, "источник входа испорчен"
    assert cp["step"] == 1 and cp["steps_total"] == 3 and len(cp["steps"]) == 1


def test_контекст_урезан_по_лимиту():
    cp = _pipeline_checkpoint(1, 2, [], "я" * 9000, SRC)
    assert len(cp["prev_ctx"]) == 6000, "контекст уехал в следующий шаг целиком"


def test_первый_шаг_без_источника_не_падает():
    """На первом шаге источника ещё нет — None обязан пережить чекпойнт."""
    cp = _pipeline_checkpoint(0, 2, [], None, None)
    assert cp["prev_ctx"] == "" and cp["prev_src"] is None


def test_чекпойнт_сериализуем():
    """Он уходит в очередь как JSON: несериализуемое значение убьёт задание уже после прогона."""
    import json
    json.dumps(_pipeline_checkpoint(1, 2, [{"agent_id": "a", "tokens": 7}], "ctx", SRC), ensure_ascii=False)


def test_срез_не_применяется_к_источнику():
    """Проверка самой строки кода: ограничение стоит на контексте, и только на нём."""
    src = (ROOT / "server" / "web_api.py").read_text(encoding="utf-8")
    assert "prev_src[:" not in src, "срез снова применён к источнику входа"
    i = src.index("def _pipeline_checkpoint")
    body = src[i:i + 1400]
    assert 'str(prev_ctx or "")[:6000]' in body
