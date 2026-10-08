# -*- coding: utf-8 -*-
"""Мёртвая настройка должна удаляться, а не жить вечно.

`app_config` умел только запись: ключ, записанный однажды, оставался навсегда. После переезда карты
процессов в собственные таблицы (ADR-042/043) там остались `tree` и `assignments` — настройки,
которые можно изменить и не получить ничего. Это худший вид мёртвого интерфейса, поэтому:

1. у стора есть удаление;
2. мёртвых ключей нет в белом списке записи — иначе их снова начнут записывать;
3. скрипт чистки отказывается работать, пока карта не переехала: до переезда эти ключи —
   единственная её копия.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]


def test_стор_умеет_удалять():
    from server import admin_store

    async def прогон():
        await admin_store.init()
        await admin_store.save("пробный", {"a": 1}, editor="тест")
        assert "пробный" in await admin_store.all()
        assert await admin_store.delete("пробный") is True
        assert "пробный" not in await admin_store.all()
        assert await admin_store.delete("пробный") is False

    asyncio.run(прогон())


def test_мёртвых_ключей_нет_в_белом_списке():
    """`tree`/`assignments` больше не читаются; принимать их на запись — обманывать оператора."""
    from server.routes import admin as _admin

    for k in ("tree", "assignments"):
        assert k not in _admin._ADMIN_CONFIG_KEYS, f"ключ {k} снова принимается на запись"


def test_скрипт_чистки_проверяет_переезд():
    """Самое опасное в чистке — удалить то, что ещё не перенесено."""
    текст = (КОРЕНЬ / "scripts" / "cleanup_legacy_config.py").read_text(encoding="utf-8")
    код = re.sub(r'"""(?:.|\n)*?"""', "", текст)          # без докстрингов: проверяем код
    assert "map_of" in код and "seats_of" in код, "скрипт не сверяется с картой перед удалением"
    assert "--apply" in код, "удаление должно требовать явного флага"
    assert "backup" in код or "бэкап" in код, "перед удалением содержимое не выгружается"


@pytest.mark.parametrize("ключ", ["tree", "assignments"])
def test_ключ_больше_нигде_не_читается(ключ):
    """Если код снова начнёт читать эти ключи, чистка превратится в поломку."""
    подозрительные = []
    for p in sorted((КОРЕНЬ / "server").rglob("*.py")):
        текст = p.read_text(encoding="utf-8")
        for m in re.finditer(r'\.get\(\s*"%s"' % ключ, текст):
            строка = текст[:m.start()].count("\n") + 1
            подозрительные.append(f"{p.name}:{строка}")
    # process_store и импорт BPMN оперируют собственным деревом — у них ключ свой и к app_config
    # отношения не имеет; всё остальное обязано быть пустым.
    чужие = [x for x in подозрительные
             if not x.startswith(("process_store.py", "bpmn_import.py", "process.py",
                                  "mcp_server.py", "process_deploy.py"))]
    assert not чужие, f"app_config.{ключ} снова читают: {чужие}"
