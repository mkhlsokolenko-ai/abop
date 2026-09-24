"""Засев демо-писем в Mailpit (SMTP 127.0.0.1:1025 на сервере): цепочка переписки с цитированием
(для навыка email-thread-reconstruct / «Следопыт»), БФТ-письмо со вложением, письма-задачи со вложениями.
Запуск НА СЕРВЕРЕ: python3 seed_emails.py"""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

s = smtplib.SMTP("127.0.0.1", 1025)
NL = "\n"


def plain(frm, subj, body):
    m = MIMEText(body, "plain", "utf-8")
    m["From"], m["To"], m["Subject"] = frm, "me@corp.local", subj
    s.sendmail(frm, ["me@corp.local"], m.as_string())


def attach(frm, subj, body, fname, content):
    m = MIMEMultipart()
    m["From"], m["To"], m["Subject"] = frm, "me@corp.local", subj
    m.attach(MIMEText(body, "plain", "utf-8"))
    p = MIMEApplication(content.encode("utf-8"), _subtype="octet-stream")
    p.add_header("Content-Disposition", "attachment", filename=fname)
    m.attach(p)
    s.sendmail(frm, ["me@corp.local"], m.as_string())


def quote(prev):
    return NL.join("> " + ln for ln in prev.split(NL))


# ── Цепочка переписки с вложенным цитированием (для «Следопыта») ──
q1 = "Коллеги, стартуем ДОГ-2026-051. Предлагаю срок сдачи 30 сентября. Возражения?" + NL + "-- Анна, PM"
plain("anna.pm@corp.local", "Договор ДОГ-2026-051: сроки", q1)

q2 = ("Анна, 30-е нереально: материалы от подрядчика только к 25-му, нужен буфер. Предлагаю 10 октября." + NL
      + "-- Игорь, поставки" + NL + NL + quote(q1))
plain("igor.supply@corp.local", "Re: Договор ДОГ-2026-051: сроки", q2)

q3 = ("Игорь, 10 октября ломает сдачу заказчику (его дедлайн 5-го). Фиксируем 3 октября и ускоряем подрядчика доплатой." + NL
      + "-- Анна" + NL + NL + quote(q2))
plain("anna.pm@corp.local", "Re: Re: Договор ДОГ-2026-051: сроки", q3)

q4 = ("Согласовано: срок сдачи 3 октября, доплата за ускорение утверждена финансами. Игорь готовит допсоглашение. РЕШЕНИЕ ПРИНЯТО." + NL
      + "-- Анна" + NL + NL + quote(q3))
plain("anna.pm@corp.local", "Re: Re: Re: Договор ДОГ-2026-051: сроки", q4)

# ── БФТ со вложением ──
bft = ("БИЗНЕС-ФУНКЦИОНАЛЬНЫЕ ТРЕБОВАНИЯ (черновик)" + NL + NL
       + "1. Личный кабинет клиента" + NL + "2. Онлайн-оплата (ЮKassa)" + NL + "3. Уведомления по email" + NL + NL
       + "Открытые вопросы: роли пользователей не определены; SLA не указан.")
attach("o.director@corp.local", "Задача: проверить БФТ по новому кабинету",
       "Во вложении черновик БФТ. Проверьте полноту, отметьте пробелы, подготовьте вики-отчёт.",
       "БФТ_кабинет_v1.txt", bft)

# ── Счёт-задача со вложением ──
inv = "Счёт СК-902" + NL + "Позиция;Кол-во;Цена" + NL + "Материал А;100;500" + NL + "Материал Б;50;1200" + NL + "ИТОГО;;110000"
attach("snab@snabkomplekt.ru", "Счёт СК-902 на согласование",
       "Направляем счёт СК-902. Проверьте позиции и согласуйте оплату.", "СК-902.csv", inv)

s.quit()
print("засеяно: цепочка(4) + БФТ+вложение + счёт+вложение")
