"""Модуль «Распознавание» (OCR) — локально, под правами пользователя, без внешних бинарей.
Движок: RapidOCR (onnxruntime), rus+eng. Картинка/скан → текст → знания треда (RAG через шлюз).
RapidOCR грузится лениво (тяжёлый импорт) — только при первом вызове.
"""
from __future__ import annotations

import base64
import os
import tempfile

from fastapi import APIRouter
from pydantic import BaseModel

from ... import gateway

MANIFEST = {"id": "ocr", "title": "Распознать", "icon": "ocr", "ui": "ocr", "order": 60}
router = APIRouter()

_engine = None


def _ocr():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


class OcrIn(BaseModel):
    # либо путь к локальному файлу (под правами ОС), либо data-URL (base64) из перетащенной картинки
    path: str = ""
    data_url: str = ""
    session_id: str = "desktop-ocr"
    to_knowledge: bool = True


def _recognize(img_path: str) -> str:
    res, _ = _ocr()(img_path)
    if not res:
        return ""
    # res: [[box, text, score], ...] — собираем построчно
    return "\n".join(line[1] for line in res)


@router.get("/status")
def status() -> dict:
    """Готовность движка (без принудительной загрузки моделей)."""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return {"ok": True, "engine": "RapidOCR (onnxruntime)", "lang": "rus+eng"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.post("/recognize")
def recognize(body: OcrIn) -> dict:
    tmp = None
    try:
        if body.data_url:
            b = body.data_url.split(",", 1)[-1]
            raw = base64.b64decode(b)
            fd, tmp = tempfile.mkstemp(suffix=".png")
            os.write(fd, raw); os.close(fd)
            src = tmp
        elif body.path:
            src = os.path.expanduser(body.path)
            if not os.path.isfile(src):
                return {"ok": False, "error": "not_found"}
        else:
            return {"ok": False, "error": "no_input"}
        try:
            text = _recognize(src)
        except Exception as e:  # noqa: BLE001 — например, движок не в бандле
            return {"ok": False, "error": f"engine: {e}"}
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    if not text.strip():
        return {"ok": True, "text": "", "chars": 0, "indexed": 0, "note": "текст не найден"}
    indexed = 0
    if body.to_knowledge:
        try:
            r = gateway.call("rag_index", {"documents": [text], "session_id": body.session_id})
            indexed = r.get("indexed", 1)
        except gateway.AuthRequired:
            return {"ok": True, "text": text, "chars": len(text), "indexed": 0, "note": "нужен вход для сохранения в знания"}
        except gateway.GatewayError as e:
            return {"ok": True, "text": text, "chars": len(text), "indexed": 0, "note": str(e)}
    return {"ok": True, "text": text, "chars": len(text), "indexed": indexed}
