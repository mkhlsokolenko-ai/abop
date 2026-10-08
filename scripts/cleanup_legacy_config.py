# -*- coding: utf-8 -*-
"""Убрать из `app_config` настройки, которые код больше не читает.

**Зачем.** До перехода на собственные таблицы карты процессов (ADR-042/043) дерево процессов и
назначения агентов лежали двумя JSON-ключами в `app_config`: `tree` и `assignments`. После переноса
(`scripts/migrate_process_map.py`) их никто не читает — но они остались в базе и в белом списке
записи. Настройка, которую можно изменить и не получить ничего, хуже отсутствующей: человек правит
её и считает, что повлиял на систему.

**Осторожность по умолчанию.** Сухой прогон: печатает, что нашлось, и ничего не трогает. Удаление —
флагом `--apply`, и только после того, как скрипт убедится, что карта действительно переехала:
есть применённая редакция в `process_map` и хотя бы одна посадка. Если переезда не было, ключи —
единственная копия карты, и удалять их нельзя.

Перед удалением содержимое выгружается в файл (`--backup`, по умолчанию рядом, с датой).

Запуск:
    docker exec abop-webapi python scripts/cleanup_legacy_config.py
    docker exec abop-webapi python scripts/cleanup_legacy_config.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from server import admin_store, process_store  # noqa: E402

МЁРТВЫЕ = ("tree", "assignments")


async def главное(применить: bool, бэкап: pathlib.Path) -> int:
    await admin_store.init()
    await process_store.init()

    конфиг = await admin_store.all()
    есть = {k: конфиг[k] for k in МЁРТВЫЕ if k in конфиг}
    if not есть:
        print("чистить нечего: мёртвых ключей в app_config нет")
        return 0

    for k, v in есть.items():
        print(f"найден ключ app_config.{k}: {len(json.dumps(v, ensure_ascii=False))} символов")

    карта = await process_store.map_of(process_store.КАРТА)
    посадки = await process_store.seats_of(None)
    print(f"карта процессов: редакция {карта.get('revision')}, посадок {len(посадки)}")
    if not (карта.get("revision") and посадки):
        print("ОТКАЗ: карта не переехала в process_map — эти ключи ещё единственная её копия",
              file=sys.stderr)
        return 2

    if not применить:
        print("сухой прогон: ничего не удалено. Повторите с --apply")
        return 0

    бэкап.write_text(json.dumps(есть, ensure_ascii=False, indent=1), encoding="utf-8")
    print("выгружено в", бэкап)
    for k in есть:
        print("удалён" if await admin_store.delete(k) else "не найден", "app_config." + k)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="удалить (по умолчанию — сухой прогон)")
    p.add_argument("--backup", default="", help="куда выгрузить содержимое перед удалением")
    a = p.parse_args()
    путь = pathlib.Path(a.backup or f"/tmp/app_config-legacy-{_dt.date.today()}.json")
    raise SystemExit(asyncio.run(главное(a.apply, путь)))
