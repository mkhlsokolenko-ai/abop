"""Кому агент вправе написать по-настоящему.

Письмо наружу необратимо, а демо-данные стенда полны выдуманных адресов вида glavbuh@demo.local.
Реальный канал («Яндекс.Почта») отправлял по любому адресу, который окажется в узле вывода: такое
письмо даёт отказ почтовика и портит репутацию ящика-отправителя, а перед показом заказчику ещё и
рискует уйти живому человеку.

Каналов два и они разные по последствию: «Почта стенда» принимает всё и никуда не пересылает,
«Яндекс.Почта» отправляет по-настоящему. Подменять одно другим по наличию настройки нельзя — выбор
канала и есть решение человека.
"""
from __future__ import annotations

import importlib

import pytest


def reload_with(monkeypatch, **env):
    """Настройки читаются при импорте — перечитываем модуль с нужным окружением."""
    for k in ("YANDEX_SMTP_USER", "YANDEX_SMTP_PASSWORD", "MAIL_ALLOW"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import cli.ape as ape
    return importlib.reload(ape)


@pytest.mark.parametrize("addr", ["glavbuh@demo.local", "a@host.localhost", "b@x.test",
                                  "c@y.invalid", "d@z.example", "e@corp.internal"])
def test_invented_domains_never_leave(monkeypatch, addr):
    """Домены из RFC 2606/6761 не существуют: это адреса стенда, а не получатели."""
    ape = reload_with(monkeypatch)
    ok, why = ape.mail_deliverable(addr)
    assert not ok and "не существует" in why


def test_real_address_passes(monkeypatch):
    ape = reload_with(monkeypatch)
    assert ape.mail_deliverable("chief@company.ru")[0]


def test_allow_list_narrows_recipients(monkeypatch):
    """Перед показом заказчику список разрешённых — единственная гарантия, что лишний не получит."""
    ape = reload_with(monkeypatch, MAIL_ALLOW="company.ru, chief@partner.com")
    assert ape.mail_deliverable("anyone@company.ru")[0]
    assert ape.mail_deliverable("someone@mail.company.ru")[0], "поддомен разрешённого домена"
    assert ape.mail_deliverable("chief@partner.com")[0]
    ok, why = ape.mail_deliverable("stranger@gmail.com")
    assert not ok and "MAIL_ALLOW" in why


def test_empty_allow_list_does_not_block(monkeypatch):
    """Пустой список — не запрет: ограничитель включают осознанно, он не навязан."""
    ape = reload_with(monkeypatch)
    assert ape.mail_deliverable("stranger@gmail.com")[0]


def test_garbage_is_not_an_address(monkeypatch):
    ape = reload_with(monkeypatch)
    assert not ape.mail_deliverable("просто текст")[0]
    assert not ape.mail_deliverable("")[0]


def test_real_channel_refuses_demo_address_instead_of_sending(monkeypatch):
    """Отказ объясняется и называет канал, которым такое письмо отправить можно."""
    ape = reload_with(monkeypatch, YANDEX_SMTP_USER="robot@ya.ru", YANDEX_SMTP_PASSWORD="app-pass")
    out = ape._t_yandex_email({"to": "glavbuh@demo.local", "run": "true"})
    assert "наружу не отправлено" in out and "Mailpit" in out


def test_dry_run_is_the_default_for_the_real_channel(monkeypatch):
    """Действие наружу без run=true не выполняется — подтверждение человека обязательно."""
    ape = reload_with(monkeypatch, YANDEX_SMTP_USER="robot@ya.ru", YANDEX_SMTP_PASSWORD="app-pass")
    assert "[dry_run]" in ape._t_yandex_email({"to": "chief@company.ru"})
