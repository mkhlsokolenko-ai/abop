#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Замер эмбеддера, который мы хостим сами: модель грузится локально, числа идут в общую таблицу.

Зачем отдельно от `pick_models.py`. Тот спрашивает RouteAI, и на нём есть далеко не всё: ни
`google/embeddinggemma-2`, ни `Qwen/Qwen3-Embedding-0.6B` там нет, а именно их мы и собираемся
ставить своими силами. Здесь модель загружается через `sentence-transformers`, считается на CPU и
кладёт порядок навыков в ТОТ ЖЕ кэш (`ops/pick_models_cache.json`) под тем же ключом, что сетевые
модели. Поэтому итоговая таблица сравнивает их на одном наборе, одной метрикой и одним
планировщиком — иначе это было бы сравнение двух разных опытов.

**Префиксы обязательны.** EmbeddingGemma различает запрос и документ строками
«task: search result | query: » и «title: none | text: », Qwen3-Embedding — своей инструкцией для
запроса. Посчитать её без префиксов значит померить не ту модель, которую потом поставим: вклад
префиксов у этого семейства заметный. Берём их из `config_sentence_transformers.json` самой модели,
а не из своей памяти.

Запуск:
    python ops/pick_models_local.py google/embeddinggemma-2
    python ops/pick_models_local.py Qwen/Qwen3-Embedding-0.6B --случаев 10   # быстрая проба
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))
sys.path.insert(0, str(КОРЕНЬ / "ops"))


def _имена_префиксов(модель) -> tuple[str | None, str | None]:
    """Как ЭТА модель просит помечать запрос и документ. Пусто — значит она не просит ничего."""
    есть = set((getattr(модель, "prompts", None) or {}).keys())
    запрос = next((k for k in ("query", "Retrieval-query", "SearchQuery") if k in есть), None)
    документ = next((k for k in ("document", "Retrieval-document", "Document") if k in есть), None)
    return запрос, документ


def main() -> int:
    ap = argparse.ArgumentParser(description="локальный эмбеддер на наборе подбора")
    ap.add_argument("модель")
    ap.add_argument("--случаев", type=int, default=0)
    ap.add_argument("--вариант", default="б", choices=["а", "б"])
    ap.add_argument("--пачка", type=int, default=4, help="размер пачки (CPU, памяти немного)")
    а = ap.parse_args()

    import numpy as np
    from sentence_transformers import SentenceTransformer

    import pick_models as пм
    import planner_eval as мера
    import planner_ladder as лесенка

    лесенка._ускорить_индекс()
    случаи = пм._случаи(а.случаев)
    cat = мера.каталог()
    ids = sorted(cat)
    задачи = [c[а.вариант] for c in случаи]

    print(f"Загружаю {а.модель} (CPU)…")
    t = time.time()
    модель = SentenceTransformer(а.модель, device="cpu")
    запрос, документ = _имена_префиксов(модель)
    print(f"  загрузилась за {time.time() - t:.0f} с; префиксы: запрос={запрос!r}, "
          f"документ={документ!r}")

    docs = [пм._документ(sid, cat[sid], пм.ДЛЯ_ЭМБЕДДЕРА) for sid in ids]
    t = time.time()
    dv = модель.encode(docs, batch_size=а.пачка, normalize_embeddings=True,
                       show_progress_bar=False, **({"prompt_name": документ} if документ else {}))
    print(f"  {len(docs)} описаний навыков за {time.time() - t:.0f} с")
    t = time.time()
    qv = модель.encode(задачи, batch_size=а.пачка, normalize_embeddings=True,
                       show_progress_bar=False, **({"prompt_name": запрос} if запрос else {}))
    сек_на_запрос = (time.time() - t) / max(1, len(задачи))
    print(f"  {len(задачи)} запросов за {time.time() - t:.0f} с "
          f"({сек_на_запрос * 1000:.0f} мс на запрос, 1 поток CPU)")

    близости = np.asarray(qv) @ np.asarray(dv).T
    кэш = пм._кэш()
    узел = кэш.setdefault("эмбеддер:" + а.модель, {})
    порядки = {}
    for з, строка in zip(задачи, близости):
        порядок = [ids[i] for i in np.argsort(-строка)[:10]]
        узел[з] = порядок
        порядки[з] = порядок
    пм._сохранить(кэш)

    о = пм.оценить(случаи, порядки, cat, а.вариант)
    print(f"\n  {а.модель}")
    print(f"    первым      {пм.доля(о['первым'], о['всего'])}")
    print(f"    в пятёрке   {пм.доля(о['в_пятёрке'], о['всего'])}")
    print(f"    с правилами {пм.доля(о['с_правилами'], о['всего'])}")
    print(f"    в плане     {пм.доля(о['в_плане'], о['всего'])}")
    print("\n  Для сравнения на том же наборе («б», через планировщик): словарь 21/63 в плане, "
          "30B генеративно 48/63,")
    print("  bge-m3 34/63, qwen3-embedding-4b 51/63, gemini-embedding-2 53/63.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
