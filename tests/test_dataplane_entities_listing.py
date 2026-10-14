# -*- coding: utf-8 -*-
"""Перечень сущностей Data Plane обязан показывать то, что лежит в БД, а не только в файлах.

Канонический стор переехал в Postgres (08.10), но `/api/data/entities` продолжал перечислять
сущности, перебирая наследие `~/.ape/data/*.jsonl`. Сущность, у которой файла нет, показывалась
с «0 строк» при живых записях в базе. Это не косметика: по этому экрану делают вывод «навыкам не
на чем работать» — и вывод выходит ложным, что однажды и произошло.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

from cli import ape  # noqa: E402
from server import dataplane_records as dr  # noqa: E402


def test_сущность_только_из_стора_видна_с_числом_строк():
    dr._сброс_для_тестов()
    dr.init()
    ape.set_record_store(dr)
    try:
        # Файла `meeting.jsonl` нет и не будет: запись идёт прямо в стор.
        dr.append("meeting", [{"id": "m1", "тема": "планёрка",
                               "provenance": {"fetched_at": time.time(), "recipe": "r", "source": "s"}}])
        from server.routes.data import data_entities
        ответ = asyncio.run(data_entities(u={"level": "admin", "name": "test"}))
        строки = {e["entity"]: e["rows"] for e in ответ["entities"]}
        assert строки.get("meeting") == 1, "сущность из стора должна быть в перечне с числом строк"
    finally:
        ape.set_record_store(None)
        dr._сброс_для_тестов()
