"""Засев стенда под стресс-тест десктопа: СЫРЫЕ данные без выводов.

Условие владельца: «в источниках должна быть информация входная, но без обработки и выводов —
обработку и выводы должен сделать сам агент». Поэтому здесь нет ни одной посчитанной дельты, ни
одной пометки «просрочено» и ни одного вывода: только реестры фактов — платежи, счета, задачи,
правила регламента. Всё, что выглядит как анализ, должен произвести агент, иначе стресс-тест
проверяет не агента, а наш засев.

Данные детерминированы (seed фиксирован): повторный запуск даёт те же цифры, и прогоны сравнимы
между собой — иначе непонятно, изменился результат из-за правки или из-за новых данных.

Запуск НА СЕРВЕРЕ:
    python3 /opt/abop/ops/seed_stress_q3.py --out /root/stress
Затем выгрузка в MinIO (бакет читают рецепты payments/invoices):
    docker cp /root/stress/payments.json svod-scenarios-minio-1:/tmp/payments.json
    docker exec svod-scenarios-minio-1 mc cp /tmp/payments.json loc/abop-demo/1c/payments.json
    (то же для invoices.json)
И обновление Data Plane:
    curl -s -X POST http://127.0.0.1:8091/api/data/refresh   # либо «Источники → обновить» в UI
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import random
import urllib.request

SEED = 20261005
КВАРТАЛ = (dt.date(2026, 7, 1), dt.date(2026, 9, 30))

ПОДРЯДЧИКИ = [
    ("ООО «Сигма-Интегро»", "ДГ-2026/114", "интеграция"),
    ("АО «Вектор-Телеком»", "ДГ-2026/127", "связь"),
    ("ООО «Прайм-Строй»", "ДГ-2026/131", "строительство"),
    ("ООО «Дата-Лаб»", "ДГ-2026/140", "аналитика"),
    ("ИП Орлов А.В.", "ДГ-2026/152", "обучение"),
]
СТАТЬИ = ["ИТ-инфраструктура", "Связь", "СМР", "Консалтинг", "Обучение"]


def _dates(rnd: int) -> dt.date:
    a, b = КВАРТАЛ
    return a + dt.timedelta(days=rnd % ((b - a).days + 1))


def платежи(n: int) -> list[dict]:
    """Реестр платежей: факт перечисления. Без признаков «просрочен», «завышен» и прочих оценок."""
    r = random.Random(SEED)
    out = []
    for i in range(n):
        под, дог, _ = ПОДРЯДЧИКИ[i % len(ПОДРЯДЧИКИ)]
        out.append({
            "id": f"PAY-{2000 + i}",
            "date": _dates(r.randrange(0, 92)).isoformat(),
            "amount": round(r.choice([48_000, 96_500, 118_000, 240_000, 312_400, 75_300]) * r.uniform(0.9, 1.3), 2),
            "counterparty": под,
            "contract": дог,
            "article": СТАТЬИ[i % len(СТАТЬИ)],
            "invoice_id": f"INV-{3000 + (i % 60)}",
            "payment_order": f"ПП-{4100 + i}",
        })
    return out


def счета(n: int) -> list[dict]:
    """Счета: выставлен, срок оплаты, сумма. «Статус» — факт системы, а не вывод о дисциплине."""
    r = random.Random(SEED + 1)
    out = []
    for i in range(n):
        под, дог, _ = ПОДРЯДЧИКИ[i % len(ПОДРЯДЧИКИ)]
        issued = _dates(r.randrange(0, 80))
        out.append({
            "id": f"INV-{3000 + i}",
            "date": issued.isoformat(),
            "due_date": (issued + dt.timedelta(days=r.choice([10, 15, 30, 45]))).isoformat(),
            "amount": round(r.choice([52_000, 99_000, 126_000, 250_000, 330_000]) * r.uniform(0.95, 1.25), 2),
            "counterparty": под,
            "contract": дог,
            "article": СТАТЬИ[i % len(СТАТЬИ)],
            "status": r.choice(["выставлен", "оплачен", "частично оплачен"]),
        })
    return out


def задачи(n: int) -> list[dict]:
    """Задачи подрядных работ для трекера: название, статус, плановая дата. Без оценок исполнения."""
    r = random.Random(SEED + 2)
    темы = ["Монтаж узла учёта", "Поставка коммутаторов", "Настройка каналов связи",
            "Приёмка этапа СМР", "Обучение операторов", "Передача исполнительной документации",
            "Пусконаладка", "Инвентаризация оборудования", "Сверка актов"]
    out = []
    for i in range(n):
        под, дог, _ = ПОДРЯДЧИКИ[i % len(ПОДРЯДЧИКИ)]
        out.append({
            "subject": f"{темы[i % len(темы)]} · {дог}",
            "description": (f"Подрядчик: {под}. Договор: {дог}. "
                            f"Плановая дата: {_dates(r.randrange(0, 92)).isoformat()}. "
                            f"Объём по спецификации: {r.randrange(2, 40)} ед."),
        })
    return out


РЕГЛАМЕНТ = """Регламент расчётов с подрядчиками (извлечение, редакция 2026-03)

1. Оплата выполненных работ производится в течение 15 рабочих дней с даты подписания акта.
2. Авансовый платёж не может превышать 30 процентов цены договора.
3. Платёж без счёта и без акта выполненных работ не допускается.
4. Разбивка по статьям затрат обязательна для каждого платежа.
5. Превышение плановой суммы договора согласовывается до перечисления средств.
6. Срок предоставления исполнительной документации — 10 рабочих дней с даты завершения этапа.

Документ содержит только правила. Оценка соблюдения правил в документе не приводится.
"""

ПРИЛОЖЕНИЕ = """реестр_этапов_q3.csv — реестр этапов договоров за III квартал 2026 (сырой, без оценок)
договор;этап;плановая_дата;фактическая_дата;плановая_сумма;фактическая_сумма;акт
ДГ-2026/114;Поставка оборудования;2026-07-15;2026-07-18;1180000;1180000;АКТ-114-1
ДГ-2026/114;Монтаж;2026-08-20;2026-09-02;940000;1012000;АКТ-114-2
ДГ-2026/127;Каналы связи I;2026-07-10;2026-07-10;610000;610000;АКТ-127-1
ДГ-2026/127;Каналы связи II;2026-08-30;;540000;;
ДГ-2026/131;СМР этап 1;2026-07-25;2026-08-14;2310000;2498000;АКТ-131-1
ДГ-2026/131;СМР этап 2;2026-09-20;;1870000;;
ДГ-2026/140;Витрина данных;2026-08-05;2026-08-05;780000;780000;АКТ-140-1
ДГ-2026/140;Доработка отчётов;2026-09-15;2026-09-29;320000;412000;АКТ-140-2
ДГ-2026/152;Обучение группы 1;2026-07-30;2026-07-30;150000;150000;АКТ-152-1
ДГ-2026/152;Обучение группы 2;2026-09-10;;150000;;
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/root/stress")
    ap.add_argument("--payments", type=int, default=120)
    ap.add_argument("--invoices", type=int, default=60)
    ap.add_argument("--issues", type=int, default=18)
    ap.add_argument("--redmine", action="store_true", help="завести задачи в Redmine")
    ap.add_argument("--wiki", action="store_true", help="положить регламент страницей в BookStack")
    a = ap.parse_args()

    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "payments.json").write_text(json.dumps(платежи(a.payments), ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    (out / "invoices.json").write_text(json.dumps(счета(a.invoices), ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    (out / "реестр_этапов_q3.csv").write_text(ПРИЛОЖЕНИЕ, encoding="utf-8")
    (out / "регламент.txt").write_text(РЕГЛАМЕНТ, encoding="utf-8")
    print(f"файлы готовы в {out}: платежей {a.payments}, счетов {a.invoices}, приложение и регламент")

    if a.redmine:
        base = os.environ.get("REDMINE_BASE") or "http://127.0.0.1:3000"
        key = os.environ.get("REDMINE_API_KEY") or ""
        proj = os.environ.get("REDMINE_PROJECT") or "demo"
        done = 0
        for t in задачи(a.issues):
            body = json.dumps({"issue": {"project_id": proj, "subject": t["subject"],
                                         "description": t["description"]}}).encode()
            req = urllib.request.Request(base.rstrip("/") + "/issues.json", data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "X-Redmine-API-Key": key})
            try:
                with urllib.request.urlopen(req, timeout=20):
                    done += 1
            except Exception as e:  # noqa: BLE001 — засев не должен падать целиком из-за одной задачи
                print("  задача не создана:", type(e).__name__, str(e)[:80])
        print(f"в Redmine заведено задач: {done}")

    if a.wiki:
        base = os.environ.get("BOOKSTACK_BASE") or "http://127.0.0.1:6875"
        tok = os.environ.get("BOOKSTACK_TOKEN") or ""
        body = json.dumps({"book_id": int(os.environ.get("BOOKSTACK_BOOK", "1")),
                           "name": "Регламент расчётов с подрядчиками (извлечение)",
                           "markdown": РЕГЛАМЕНТ}).encode()
        req = urllib.request.Request(base.rstrip("/") + "/api/pages", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Token " + tok})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                print("страница регламента создана:", json.loads(r.read()).get("slug"))
        except Exception as e:  # noqa: BLE001
            print("страница не создана:", type(e).__name__, str(e)[:120])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
