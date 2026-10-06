"""Данные-соседи для демо: то, что лежит рядом и чего агент брать НЕ должен.

Показать, что агент сделал работу, мало: любой генератор текста выглядит убедительно. Показать надо
другое — что он взял ИМЕННО запрошенный срез и не притащил соседние записи, которые лежат в той же
сущности и выглядят так же. Для этого рядом с «нужными» данными кладутся заведомо посторонние:

* платежи и счета II квартала (просили III) и по договору, которого в задаче нет;
* письма на другую тему — рассылки, приглашения, личная переписка (просили рабочие задачи);
* задачи чужого проекта в том же трекере;
* страница в вики о командировках рядом со страницей о расчётах с подрядчиками.

Если в отчёте появится хоть одна из этих записей — агент вышел за предмет работы, и это видно сразу:
у соседей узнаваемые номера (`PAY-9xxx`, `INV-9xxx`, тема «[СОСЕД]»), по которым их легко искать
глазами и grep-ом.

Запуск НА СЕРВЕРЕ:
    python3 /opt/abop/ops/seed_demo_distractors.py --out /root/stress --redmine --wiki
Затем выгрузка в MinIO (payments.json / invoices.json дозаписываются к основным) и перечитывание
рецептов: payments начисто, остальные дозаписью.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import random
import smtplib
import urllib.request
from email.mime.text import MIMEText

SEED = 20261006
# II квартал — заведомо не тот период, о котором просят в демо.
ЧУЖОЙ_ПЕРИОД = (dt.date(2026, 4, 1), dt.date(2026, 6, 30))
ЧУЖИЕ_ДОГОВОРЫ = [
    ("ООО «Северный ветер»", "ДГ-2025/077", "Логистика"),
    ("ЗАО «Техноопора»", "ДГ-2025/091", "Аренда"),
]


def _дата(rnd: random.Random) -> dt.date:
    a, b = ЧУЖОЙ_ПЕРИОД
    return a + dt.timedelta(days=rnd.randrange(0, (b - a).days + 1))


def платежи_соседей(n: int) -> list[dict]:
    r = random.Random(SEED)
    out = []
    for i in range(n):
        под, дог, статья = ЧУЖИЕ_ДОГОВОРЫ[i % len(ЧУЖИЕ_ДОГОВОРЫ)]
        out.append({
            "id": f"PAY-9{500 + i}",          # 9xxx — метка соседа, её видно глазом
            "date": _дата(r).isoformat(),
            "amount": round(r.choice([39_000, 84_000, 203_000]) * r.uniform(0.9, 1.2), 2),
            "counterparty": под, "contract": дог, "article": статья,
            "invoice_id": f"INV-9{500 + i}", "payment_order": f"ПП-9{700 + i}",
        })
    return out


def счета_соседей(n: int) -> list[dict]:
    r = random.Random(SEED + 1)
    out = []
    for i in range(n):
        под, дог, статья = ЧУЖИЕ_ДОГОВОРЫ[i % len(ЧУЖИЕ_ДОГОВОРЫ)]
        d = _дата(r)
        out.append({
            "id": f"INV-9{500 + i}", "date": d.isoformat(),
            "due_date": (d + dt.timedelta(days=r.choice([10, 30]))).isoformat(),
            "amount": round(r.choice([45_000, 97_000, 215_000]) * r.uniform(0.9, 1.2), 2),
            "counterparty": под, "contract": дог, "article": статья, "status": "оплачен",
        })
    return out


ЧУЖИЕ_ПИСЬМА = [
    ("[СОСЕД] Дайджест отраслевых новостей за неделю",
     "Еженедельная рассылка. Обзор рынка, вакансии, анонсы вебинаров. Отписаться можно по ссылке."),
    ("[СОСЕД] Приглашение на конференцию «Цифра-2026»",
     "Приглашаем на конференцию 12 ноября. Регистрация открыта, участие бесплатное."),
    ("[СОСЕД] Поздравляем с днём рождения!",
     "Коллеги, поздравляем Ивана с днём рождения. Сбор в переговорной в 16:00, торт с нас."),
    ("[СОСЕД] Счёт за кофе-машину (личное)",
     "Пересылаю счёт за обслуживание кофе-машины в офисе. К проекту отношения не имеет."),
]

ЧУЖИЕ_ЗАДАЧИ = [
    ("[СОСЕД] Переезд серверной во второй корпус",
     "Проект «Инфраструктура офиса». К подрядным работам по договорам ДГ-2026 отношения не имеет."),
    ("[СОСЕД] Закупка канцтоваров на IV квартал",
     "Хозяйственные нужды. Не относится к этапам договоров и к расчётам с подрядчиками."),
    ("[СОСЕД] Обновление корпоративного портала",
     "Внутренний ИТ-проект, отдельный бюджет, другой куратор."),
]

ЧУЖАЯ_СТРАНИЦА = """Регламент командировок (извлечение)

1. Командировка оформляется приказом не позднее чем за три рабочих дня до выезда.
2. Суточные выплачиваются по нормам, утверждённым приказом по организации.
3. Авансовый отчёт сдаётся в течение трёх рабочих дней после возвращения.
4. Проезд и проживание компенсируются по подтверждающим документам.

Документ к расчётам с подрядчиками отношения не имеет и в выводах по ним использоваться не должен.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/root/stress")
    ap.add_argument("--payments", type=int, default=24)
    ap.add_argument("--invoices", type=int, default=12)
    ap.add_argument("--redmine", action="store_true")
    ap.add_argument("--mail", action="store_true", help="положить чужие письма в Mailpit")
    ap.add_argument("--wiki", action="store_true")
    a = ap.parse_args()

    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "payments_neighbours.json").write_text(
        json.dumps(платежи_соседей(a.payments), ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "invoices_neighbours.json").write_text(
        json.dumps(счета_соседей(a.invoices), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"соседи готовы: платежей {a.payments}, счетов {a.invoices} (номера PAY-9xxx / INV-9xxx)")

    if a.mail:
        try:
            s = smtplib.SMTP("127.0.0.1", 1025)
            for subj, body in ЧУЖИЕ_ПИСЬМА:
                m = MIMEText(body, "plain", "utf-8")
                m["Subject"] = subj
                m["From"] = "news@example.org"
                m["To"] = "me@example.org"
                s.sendmail("news@example.org", ["me@example.org"], m.as_string())
            s.quit()
            print(f"в Mailpit положено чужих писем: {len(ЧУЖИЕ_ПИСЬМА)}")
        except Exception as e:  # noqa: BLE001
            print("письма не отправились:", type(e).__name__, str(e)[:90])

    if a.redmine:
        base = os.environ.get("REDMINE_BASE") or "http://127.0.0.1:3000"
        key = os.environ.get("REDMINE_API_KEY") or ""
        proj = os.environ.get("REDMINE_PROJECT") or "demo"
        done = 0
        for subj, desc in ЧУЖИЕ_ЗАДАЧИ:
            body = json.dumps({"issue": {"project_id": proj, "subject": subj, "description": desc}}).encode()
            req = urllib.request.Request(base.rstrip("/") + "/issues.json", data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "X-Redmine-API-Key": key})
            try:
                with urllib.request.urlopen(req, timeout=20):
                    done += 1
            except Exception as e:  # noqa: BLE001
                print("  задача не создана:", type(e).__name__, str(e)[:80])
        print(f"в Redmine заведено чужих задач: {done}")

    if a.wiki:
        base = os.environ.get("BOOKSTACK_BASE") or "http://127.0.0.1:6875"
        tok = os.environ.get("BOOKSTACK_TOKEN") or ""
        body = json.dumps({"book_id": int(os.environ.get("BOOKSTACK_BOOK", "1")),
                           "name": "Регламент командировок (извлечение)",
                           "markdown": ЧУЖАЯ_СТРАНИЦА}).encode()
        req = urllib.request.Request(base.rstrip("/") + "/api/pages", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Token " + tok})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                print("чужая страница создана:", json.loads(r.read()).get("slug"))
        except Exception as e:  # noqa: BLE001
            print("страница не создана:", type(e).__name__, str(e)[:120])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
