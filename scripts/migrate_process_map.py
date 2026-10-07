#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Перенос карты процессов из `app_config` в собственный стор (ADR-042/043/045, этап 1).

Что было. Дерево процессов и назначения агентов лежали двумя JSON-полями в `app_config` —
админ-настройке среды, общей для всех. Назначение держало группу операций массивом `ops[]`, слой
хранился полем `layer`, а ссылка на агента была ИМЕНЕМ (`«Аудитор 1С»`) плюс несуществующий ключ
`dep: "d1".."d4"`, который ни с чем не совпадал.

Что делает скрипт:

1. `app_config.tree` → применённая редакция карты (`process_map`), с областями на процессах;
2. назначения со `layer: AS-IS` → посадки по ОДНОЙ на операцию, через настоящий путь записи
   (черновик + `apply`), чтобы миграция прошла теми же проверками, что и обычная правка;
3. назначения со `layer: TO-BE` → открытый черновик редакции своей области: план остаётся планом;
4. имя агента ищется в реестре → `contract_audit_id`. Не нашлось — назначение не выдумывается, а
   выводится в отчёт: пусть лучше участок будет виден пустым, чем посажен неизвестно кем.

По умолчанию — сухой прогон. Запись: `--apply`.

    python scripts/migrate_process_map.py                      # показать, что получится
    python scripts/migrate_process_map.py --apply
    python scripts/migrate_process_map.py --area "Закупки=management" --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))

from server import admin_store, agent_store, families_store, process_store as ps  # noqa: E402

# Область процесса — решение владельца, а не догадка кода. Эти три процесса есть в сиде карты;
# для всего остального область остаётся «*» (правит только admin), и скрипт об этом говорит.
ОБЛАСТИ_ПО_УМОЛЧАНИЮ = {
    "Закупки": "management",
    "Учёт и отчётность": "finance",
    "Клиентские заявки": "management",
}


def разметить_области(дерево: dict, карта_областей: dict[str, str], семьи: set[str]) -> list[str]:
    """Проставить `area` процессам первого уровня. Возвращает заметки для отчёта."""
    заметки = []
    for процесс in дерево.get("children") or []:
        имя = процесс.get("label") or процесс.get("id")
        область = карта_областей.get(имя)
        if not область:
            заметки.append(f"  · «{имя}»: область не задана → «*» (правит только admin)")
            continue
        if семьи and область not in семьи:
            заметки.append(f"  · «{имя}»: области «{область}» нет среди семей/отделов → «*»")
            continue
        процесс["area"] = область
        заметки.append(f"  · «{имя}» → область «{область}»")
    return заметки


async def карта_имён() -> dict[str, str]:
    """Имя агента → `contract_audit_id`. Берём последнюю версию каждого агента, включая Лимб."""
    из: dict[str, str] = {}
    for архив in (False, True):
        for a in await agent_store.list_for(None, limit=500, archived=архив):
            имя = (a.get("name") or "").strip()
            if имя:
                из.setdefault(имя, a.get("contract_audit_id") or a["id"].split(".v")[0])
    return из


async def главное(писать: bool, карта_областей: dict[str, str]) -> int:
    cfg = await admin_store.all()
    дерево = cfg.get("tree")
    назначения = cfg.get("assignments") or []
    if not isinstance(дерево, dict) or not дерево.get("id"):
        print("В app_config нет ключа tree — переносить нечего.")
        print("Если карта ещё не создавалась, засейте её через POST /api/process/map (только admin).")
        return 1

    семьи = {f["id"] for f in await families_store.all()}
    заметки = разметить_области(дерево, карта_областей, семьи)
    print("Карта процессов:")
    print(f"  процессов первого уровня: {len(дерево.get('children') or [])}")
    for з in заметки:
        print(з)

    имена = await карта_имён()
    по_слоям: dict[str, list[tuple[str, str]]] = {"AS-IS": [], "TO-BE": []}
    осиротевшие: list[str] = []
    for н in назначения:
        audit_id = имена.get((н.get("agent") or "").strip())
        слой = "TO-BE" if (н.get("layer") == "TO-BE") else "AS-IS"
        for op in н.get("ops") or []:
            if not audit_id:
                осиротевшие.append(f"{н.get('agent')} → {op}")
                continue
            по_слоям[слой].append((op, audit_id))

    print("\nНазначения → посадки (по одной на операцию):")
    print(f"  факт (AS-IS): {len(по_слоям['AS-IS'])}")
    print(f"  план (TO-BE): {len(по_слоям['TO-BE'])} — останется открытым черновиком")
    if осиротевшие:
        print(f"  не сопоставлено с реестром агентов: {len(осиротевшие)} — участки будут пустыми")
        for о in осиротевшие:
            print(f"    · {о}")

    if not писать:
        print("\nСухой прогон. Для записи: --apply")
        return 0

    await ps.init()
    применено = await ps.save_map(ps.КАРТА, дерево, editor="migration",
                                 note="перенос из app_config.tree (ADR-042)")
    print(f"\nКарта записана: редакция {применено['revision']}")

    for слой in ("AS-IS", "TO-BE"):
        по_областям: dict[str, list[dict]] = {}
        for op, audit_id in по_слоям[слой]:
            область = ps.area_of(дерево, op)
            по_областям.setdefault(область, []).append(
                {"kind": "add_seat", "target": op, "payload": {"audit_id": audit_id}})
        for область, правки in по_областям.items():
            try:
                итог = await ps.append(область, base_version=0, changes=правки, author="migration")
            except (ps.Отказ, ps.Конфликт) as ex:
                print(f"  ! область «{область}» ({слой}): {ex}")
                continue
            if слой == "AS-IS":
                # Префлайт миграции не применяем: переносим то, что уже работало на стенде.
                готово = await ps.apply(область, base_version=итог["version"], actor="migration")
                print(f"  область «{область}»: {готово['seats_added']} посадок в факт "
                      f"(редакция {готово['revision']})")
            else:
                print(f"  область «{область}»: {итог['version']} правок оставлены черновиком "
                      f"{итог['revision_id']}")

    print("\nГотово. Ключи app_config.tree / app_config.assignments больше не читаются сервером —")
    print("их можно удалить вручную после проверки карты в интерфейсе.")
    return 0


if __name__ == "__main__":
    парсер = argparse.ArgumentParser(description="Перенос карты процессов в process_store")
    парсер.add_argument("--apply", action="store_true", help="записать (без флага — сухой прогон)")
    парсер.add_argument("--area", action="append", default=[], metavar="ПРОЦЕСС=ОБЛАСТЬ",
                        help="область процесса, можно повторять")
    арг = парсер.parse_args()
    области = dict(ОБЛАСТИ_ПО_УМОЛЧАНИЮ)
    for пара in арг.area:
        если_есть = пара.split("=", 1)
        if len(если_есть) == 2:
            области[если_есть[0].strip()] = если_есть[1].strip()
    sys.exit(asyncio.run(главное(арг.apply, области)))
