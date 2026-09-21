"""Модуль «Операции» (OpsLens) — карта процессов с живыми агентами на участках (semantic zoom).
Пока предпросмотр раскладки по макету; интерактивная карта — отдельный заход. UI-only.
"""
from __future__ import annotations
from fastapi import APIRouter

MANIFEST = {"id": "opslens", "title": "Операции", "icon": "opslens", "ui": "opslens", "order": 40}
router = APIRouter()
