#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Какая модель нужна подбору: сравнение эмбеддеров и реранкеров на тех же 126 запросах.

**Повод.** Предложено поставить на подбор небольшой эмбеддер (EmbeddingGemma). Прежде чем выбирать
модель, два вопроса надо разделить, потому что ответы у них разные:

1. **Чем мерить близость** — эмбеддер (один вектор на текст, дёшево) против реранкера
   (кросс-энкодер смотрит пару «запрос ↔ навык», дороже и точнее).
2. **Как применять результат.** Это оказалось важнее выбора модели: тот эмбеддер, что уже стоит в
   системе, даёт 36 попаданий из 63, когда ведёт сам, и 19 из 63, когда его близость идёт
   прибавкой с порогом `SEM_FLOOR=0.45`. Средняя близость к ВЕРНОМУ навыку 0.518, к лучшему
   ЧУЖОМУ 0.509 — абсолютное число не несёт сведений, порядок несёт.

**Что меряем.** Для каждой модели — три числа на отложенной формулировке «б»:

* `первым` — верный навык оказался первым в её порядке;
* `в пятёрке` — верный навык попал в первые пять. Это главное число для РОЛИ СУЖАТЕЛЯ: если
  верный навык почти всегда в пятёрке, генеративной модели можно показывать пять навыков вместо
  шестидесяти трёх — промпт падает с ~2950 токенов до ~300, и задача «выбери из пяти» посильна
  модели в сотню раз меньше;
* `с правилами` — что получится, если отдать её порядок планировщику ведущим голосом.

**Чего замер не доказывает.** Фразы и правильные ответы составлены ассистентом, не людьми.
Абсолютный уровень — мера согласия с этими формулировками; сравнивать модели между собой на ОДНОМ
наборе он позволяет, объявлять «точность продукта» — нет.

**Деньги.** Каждый прогон новой модели — платные запросы к RouteAI: эмбеддеру 189 коротких текстов,
реранкеру 126 запросов по 63 документа. Ответы кэшируются (`ops/pick_models_cache.json`), повторный
прогон бесплатен. Поэтому список моделей задаётся явно, а не «все подряд».

Запуск:
    python ops/pick_models.py --эмбеддеры baai/bge-m3 intfloat/multilingual-e5-large
    python ops/pick_models.py --реранкеры cohere/rerank-v3.5 --случаев 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))
sys.path.insert(0, str(КОРЕНЬ / "ops"))

НАБОР = КОРЕНЬ / "tests" / "fixtures" / "planner_sim.json"
КЭШ = КОРЕНЬ / "ops" / "pick_models_cache.json"
# Документ навыка для реранкера короче, чем для эмбеддера: кросс-энкодер считает пару «запрос ↔
# документ» целиком, и шестьдесят три методики по 1600 знаков — это счёт за пустое место.
ДЛЯ_РЕРАНКЕРА = 420
ДЛЯ_ЭМБЕДДЕРА = 1600


def _случаи(предел: int = 0) -> list[dict]:
    d = json.loads(НАБОР.read_text(encoding="utf-8"))
    сл = d["случаи"] if isinstance(d, dict) and "случаи" in d else d
    return сл[:предел] if предел else сл


def _кэш() -> dict:
    if not КЭШ.is_file():
        return {}
    try:
        return json.loads(КЭШ.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _сохранить(кэш: dict) -> None:
    КЭШ.write_text(json.dumps(кэш, ensure_ascii=False, indent=0, sort_keys=True) + "\n",
                   encoding="utf-8")


def _документ(sid: str, meta: dict, предел: int) -> str:
    """Текст навыка — как его строит прод (`_skill_semantics`), только с заданным пределом."""
    шапка = " ".join(str(meta.get(k) or "") for k in ("title", "short"))[:400]
    return (шапка + " " + str(meta.get("body") or ""))[:предел]


async def _порядок_эмбеддером(модель: str, задачи: list[str], cat: dict, кэш: dict,
                              база: str = "", ключ: str = "") -> dict:
    """{задача: [навыки по убыванию близости]}. Считает тем же клиентом, что и прод.

    `база` позволяет спросить СВОЙ эндпоинт (поднятый эмбеддер) той же меркой, что и RouteAI:
    иначе сравнение «что мы хостим» против «что покупаем» было бы сравнением двух разных опытов.
    """
    import httpx

    from server.config import settings
    from server.vectors import cosine

    база = (база or settings.routeai_base_url).rstrip("/")
    ключ = ключ or settings.routeai_api_key or "EMPTY"
    ids = sorted(cat)
    # Ключ кэша включает адрес: одно и то же имя модели на RouteAI и на своём боксе — это разные
    # развёртывания (квантование, версия), и смешивать их ответы в одной записи нельзя.
    метка = модель if not база or база == settings.routeai_base_url.rstrip("/") else f"{модель}@{база}"
    узел = кэш.setdefault("эмбеддер:" + метка, {})
    нет = [з for з in задачи if з not in узел]
    if not нет:
        return {з: узел[з] for з in задачи}

    async def векторы(тексты: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=120) as cli:
            r = await cli.post(f"{база}/embeddings",
                               headers={"Authorization": f"Bearer {ключ}"},
                               json={"model": модель, "input": тексты})
            r.raise_for_status()
            return [x["embedding"] for x in r.json()["data"]]

    навыки = await векторы([_документ(sid, cat[sid], ДЛЯ_ЭМБЕДДЕРА) for sid in ids])
    for i in range(0, len(нет), 32):
        куски = нет[i:i + 32]
        qs = await векторы(куски)
        for з, qv in zip(куски, qs):
            близости = sorted(((cosine(qv, dv), sid) for sid, dv in zip(ids, навыки)),
                              key=lambda x: (-x[0], x[1]))
            узел[з] = [sid for _, sid in близости[:10]]
        print(f"    {модель}: {min(i + 32, len(нет))}/{len(нет)}")
    return {з: узел[з] for з in задачи}


async def _порядок_реранкером(модель: str, задачи: list[str], cat: dict, кэш: dict) -> dict:
    import httpx

    from server.config import settings

    ids = sorted(cat)
    docs = [_документ(sid, cat[sid], ДЛЯ_РЕРАНКЕРА) for sid in ids]
    узел = кэш.setdefault("реранкер:" + модель, {})
    нет = [з for з in задачи if з not in узел]
    for n, з in enumerate(нет, 1):
        async with httpx.AsyncClient(timeout=120) as cli:
            r = await cli.post(f"{settings.routeai_base_url}/rerank",
                               headers={"Authorization": f"Bearer {settings.routeai_api_key}"},
                               json={"model": модель, "query": з, "documents": docs, "top_n": 10})
            r.raise_for_status()
            d = r.json()
        res = d.get("results") or d.get("data") or []
        узел[з] = [ids[x["index"]] for x in res[:10]]
        if n % 20 == 0:
            print(f"    {модель}: {n}/{len(нет)}")
    return {з: узел[з] for з in задачи}


def _с_правилами(задача: str, cat: dict, ведущий: str) -> list[str]:
    from server import planner, semantic_pick
    import planner_eval as мера

    p = planner.plan(задача, cat, entities=мера.СУЩНОСТИ, slots=set(), max_steps=4,
                     model_pick={ведущий: semantic_pick.ВЕДЁТ} if ведущий else {})
    return [s.get("skill") for s in (p.get("steps") or [])]


def доля(ч: int, в: int) -> str:
    return f"{ч}/{в} ({round(100 * ч / в) if в else 0} %)"


def оценить(случаи: list[dict], порядки: dict, cat: dict, вариант: str,
            с_правилами: bool = True) -> dict:
    первым = в_пятёрке = правилами = в_плане_правилами = 0
    for c in случаи:
        задача, ждём = c[вариант], c["навык"]
        порядок = порядки.get(задача) or []
        if порядок[:1] == [ждём]:
            первым += 1
        if ждём in порядок[:5]:
            в_пятёрке += 1
        if с_правилами and порядок:
            шаги = _с_правилами(задача, cat, порядок[0])
            if шаги[:1] == [ждём]:
                правилами += 1
            # «В плане» считаем отдельно: планировщик вправе поставить впереди поставщика входа, и
            # по одному «первым» выходит, будто он ошибся. Из шести таких случаев в прошлом замере
            # все шесть были законными.
            if ждём in шаги:
                в_плане_правилами += 1
    return {"первым": первым, "в_пятёрке": в_пятёрке, "с_правилами": правилами,
            "в_плане": в_плане_правилами, "всего": len(случаи)}


async def main() -> int:
    ap = argparse.ArgumentParser(description="сравнение моделей близости для подбора")
    ap.add_argument("--эмбеддеры", nargs="*", default=[])
    ap.add_argument("--реранкеры", nargs="*", default=[])
    ap.add_argument("--случаев", type=int, default=0, help="взять только первые N (экономия)")
    ap.add_argument("--вариант", default="б", choices=["а", "б"])
    ap.add_argument("--база", default="", help="свой OpenAI-совместимый адрес (…/v1)")
    ap.add_argument("--ключ", default="", help="ключ для своего адреса (по умолчанию EMPTY)")
    а = ap.parse_args()
    if not а.эмбеддеры and not а.реранкеры:
        print("Укажите --эмбеддеры и/или --реранкеры: список задаётся явно, потому что каждый "
              "новый прогон — платные запросы.", file=sys.stderr)
        return 2

    import planner_eval as мера
    import planner_ladder as лесенка

    лесенка._ускорить_индекс()
    случаи = _случаи(а.случаев)
    cat = мера.каталог()
    задачи = [c[а.вариант] for c in случаи]
    кэш = _кэш()

    print(f"Набор: {len(случаи)} случаев, формулировка «{а.вариант}». Каталог: {len(cat)} навыков.")
    print("«первым» — верный навык первый в порядке модели; «в пятёрке» — попал в первые пять "
          "(это число решает, годится ли модель сужать список для генеративной);")
    print("«с правилами» — её первый выбор отдан планировщику ведущим голосом.\n")

    итоги = []
    for вид, модели, добыть in (("эмбеддер", а.эмбеддеры, _порядок_эмбеддером),
                                ("реранкер", а.реранкеры, _порядок_реранкером)):
        for модель in модели:
            t = time.time()
            доп = {"база": а.база, "ключ": а.ключ} if вид == "эмбеддер" else {}
            try:
                порядки = await добыть(модель, задачи, cat, кэш, **доп)
            except Exception as ex:            # noqa: BLE001 — недоступная модель не ломает отчёт
                print(f"  {вид} {модель}: НЕ ВЫШЛО — {type(ex).__name__}: {str(ex)[:160]}")
                continue
            _сохранить(кэш)
            о = оценить(случаи, порядки, cat, а.вариант)
            о.update({"вид": вид, "модель": модель, "сек": round(time.time() - t, 1)})
            итоги.append(о)
            print(f"  {вид:9} {модель:34} первым {доля(о['первым'], о['всего']):>14} "
                  f"в пятёрке {доля(о['в_пятёрке'], о['всего']):>14} "
                  f"с правилами {доля(о['с_правилами'], о['всего']):>14} "
                  f"в плане {доля(о['в_плане'], о['всего'])}")

    if not итоги:
        return 1
    print("\n  ИТОГ")
    лучший_пятёрка = max(итоги, key=lambda x: x["в_пятёрке"])
    лучший_первым = max(итоги, key=lambda x: x["первым"])
    print(f"    сужать список лучше всех: {лучший_пятёрка['модель']} — "
          f"верный навык в пятёрке {доля(лучший_пятёрка['в_пятёрке'], лучший_пятёрка['всего'])}")
    print(f"    выбирать сама лучше всех: {лучший_первым['модель']} — "
          f"первым {доля(лучший_первым['первым'], лучший_первым['всего'])}")
    print("    Для сравнения на том же наборе: правила 18/63 первым, ведущий голос 30B — 41/63.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
