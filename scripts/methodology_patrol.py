# -*- coding: utf-8 -*-
"""Патруль методолога по расписанию (крон): считает заявки и пишет снимок в Data Plane
(сущность methodology_patrol — последняя запись = свежий патруль). Нотификация приедет
с телегой-ботом; пока результат читается чипом на карточке отдела.

Крон (владелец):  0 6 * * * docker exec abop-webapi python /app/scripts/methodology_patrol.py
"""
import asyncio
import json
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/cli")


async def main() -> int:
    from server import dataplane_records as dr, methodology
    from server.routes.data import _attach_record_store
    _attach_record_store()
    dr.init()
    итог = await methodology.патруль()
    dr.append("methodology_patrol", [{
        "id": "patrol-" + итог["дата"].replace(":", "-").replace(" ", "_"),
        "дата": итог["дата"], "счёт": json.dumps(итог["счёт"], ensure_ascii=False),
        "заявки": json.dumps(итог["заявки"], ensure_ascii=False),
        "provenance": {"recipe": "methodology-patrol"},
    }], recipe="methodology-patrol")
    print("патруль:", итог["всего"], "заявок ·", json.dumps(итог["счёт"], ensure_ascii=False))
    return 0


asyncio.run(main())
