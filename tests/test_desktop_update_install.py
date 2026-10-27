# -*- coding: utf-8 -*-
"""Обновление десктопа должно УСТАНАВЛИВАТЬСЯ, а не упираться в живую старую версию.

Владелец (16.10.2026): «1.0.26 не ставится, нельзя закрыть старую версию, установка
останавливается». Причины были наши, и все три — про процессы:

* трей держал приложение живым: человек закрыл окно, процесс остался, NSIS не смог заменить exe;
* `sidecar.kill()` на Windows гасит ОДИН процесс, а `ape-sidecar.exe` из папки установки выживал
  и держал файлы;
* одноэкземплярного замка не было — вторая копия держала те же файлы.

Тест держит исправления на месте: и в главном процессе, и в скрипте установщика (он нужен, когда
ставят поверх уже сломанной версии, где правки main.js ещё нет).
"""
from __future__ import annotations

import io
import json
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
MAIN = io.open(КОРЕНЬ / "desktop" / "electron" / "main.js", encoding="utf-8").read()
NSH = io.open(КОРЕНЬ / "desktop" / "build" / "installer.nsh", encoding="utf-8").read()
PKG = json.load(io.open(КОРЕНЬ / "desktop" / "package.json", encoding="utf-8"))


def test_сайдкар_гасится_деревом_процессов():
    блок = MAIN[MAIN.index("function stop() {"):][:900]
    assert "taskkill" in блок and '"/T"' in блок and '"/F"' in блок, блок[:300]
    assert "execFileSync" in блок, "гасить надо синхронно: установщик стартует сразу за выходом"
    assert "win32" in блок, "taskkill только на Windows"


def test_установка_обновления_выходит_по_настоящему():
    # Тело установки вынесено в функцию: её зовут и кнопка в окне, и пункт трея (окно закрывают
    # на недели, и баннер в нём человек не видит). Смотрим на функцию, а не на обработчик ipc.
    блок = MAIN[MAIN.index("function установитьОбновление()"):][:900]
    assert "выходим = true" in блок, "при живом трее приложение останется в фоне"
    assert "tray.destroy()" in блок, "трей держит процесс и ломает установку"
    assert "stop()" in блок, "сайдкар держит файлы папки установки"
    assert "quitAndInstall(false, true)" in блок, "после установки приложение должно подняться само"


def test_один_экземпляр_на_машину():
    assert "requestSingleInstanceLock()" in MAIN
    assert 'app.on("second-instance"' in MAIN, "вторая копия должна показывать первую, а не жить рядом"
    i = MAIN.index("requestSingleInstanceLock()")
    assert "app.quit()" in MAIN[i:i + 260], "без замка вторая копия обязана выйти"


def test_выход_из_трея_гасит_сайдкар():
    i = MAIN.index('{ label: "Выход')
    блок = MAIN[i:i + 160]
    assert "stop()" in блок, "выход из трея оставлял сайдкар живым"


def test_установщик_сам_закрывает_остатки():
    """Нужен для случая «ставим поверх 1.0.26», где правки главного процесса ещё нет."""
    assert PKG["build"]["nsis"].get("include") == "build/installer.nsh"
    for макрос in ("!macro customInit", "!macro customUnInit"):
        assert макрос in NSH, макрос
    assert 'taskkill /F /T /IM "ABOP Desktop.exe"' in NSH
    assert 'taskkill /F /T /IM "ape-sidecar.exe"' in NSH
    # Имя процесса должно совпадать с productName сборки — иначе taskkill бьёт в пустоту.
    assert PKG["build"].get("productName") == "ABOP Desktop"


def test_версия_поднята_над_сломанной():
    было = tuple(int(x) for x in "1.0.26".split("."))
    стало = tuple(int(x) for x in str(PKG["version"]).split("."))
    assert стало > было, "обновление без новой версии до пользователя не доедет"


def test_поставить_обновление_можно_и_из_трея():
    """Окно закрывают на недели — баннер с кнопкой в нём никто не видит."""
    assert 'ipcMain.handle("updater:install", () => установитьОбновление())' in MAIN
    трей = MAIN[MAIN.index("function обновитьТрей()"):][:1400]
    assert "установитьОбновление()" in трей, "из трея обновление не поставить"
    assert "Проверить обновления" in трей, "из трея нельзя даже запросить проверку"

