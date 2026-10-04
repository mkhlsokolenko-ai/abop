"""Копирование и вставка в десктопе: меню держит акселераторы, у буфера есть прямой путь.

04.10 владелец: «текст не вставляется, если скопировать его из чата, и вставка из Word/Excel по
сочетанию клавиш тоже не работает».

Две причины, обе в оболочке. Приложение НЕ ставило меню Electron вовсе — а без меню с ролями правки
Ctrl+C/V/X/A в окне не регистрируются. И `navigator.clipboard` в окне зависит от разрешений и фокуса
документа: он отказывает молча, поэтому кнопка «копировать» говорила «скопировано», а вставлять было
нечего.

Третья, отдельная: `globalShortcut.register` возвращает false, когда сочетание занято другим
приложением, и исключения не бросает — мы проверяли только try/catch, и занятый хоткей просто молчал.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop" / "electron" / "main.js").read_text(encoding="utf-8")
PRELOAD = (ROOT / "desktop" / "electron" / "preload.js").read_text(encoding="utf-8")
APP = (ROOT / "desktop" / "ui" / "core" / "app.js").read_text(encoding="utf-8")
CHAT = (ROOT / "desktop" / "ui" / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")


def test_меню_правки_установлено():
    """Без него Ctrl+C/V/X/A в окне не работают — это и была причина «не вставляется»."""
    assert "Menu.setApplicationMenu" in MAIN
    i = MAIN.index("function installMenu")
    body = MAIN[i:i + 1400]
    for role in ('role: "cut"', 'role: "copy"', 'role: "paste"', 'role: "selectAll"'):
        assert role in body, f"в меню правки нет роли {role}"
    assert "installMenu();" in MAIN, "меню объявлено, но не установлено при запуске"


def test_правый_щелчок_даёт_вставку():
    """Путь мышью нужен и сам по себе, и как страховка, если сочетание перехватили."""
    i = MAIN.index("function installContextMenu")
    body = MAIN[i:i + 900]
    assert "params.isEditable" in body and 'role: "paste"' in body
    assert "installContextMenu(win);" in MAIN


def test_занятый_хоткей_больше_не_молчит():
    i = MAIN.index("const wanted = [")
    body = MAIN[i:i + 900]
    assert "globalShortcut.register(combo, onFire)" in body and "isRegistered" in body
    assert body.count('"CommandOrControl+') >= 2, "нет запасного сочетания"
    assert 'send("ape:hotkey"' in MAIN, "интерфейс не узнаёт, какое сочетание занято"
    assert "onHotkey" in PRELOAD and "hotkey:active" in PRELOAD


def test_у_буфера_есть_прямой_путь():
    """Модуль clipboard Electron не зависит от разрешений окна — он и должен быть основным."""
    assert 'ipcMain.handle("clip:write"' in MAIN and 'ipcMain.handle("clip:read"' in MAIN
    assert "clipboard: {" in PRELOAD and "writeText" in PRELOAD and "readText" in PRELOAD


def test_копирование_не_врёт_об_успехе():
    i = APP.index("export async function copyText")
    body = APP[i:i + 1500]
    assert "window.ape.clipboard" in body, "прямой путь не используется"
    assert "navigator.clipboard" in body and "execCommand" in body, "нет запасных путей"
    # Сравниваем положение ВЫЗОВОВ, а не упоминаний: в комментарии выше «navigator.clipboard»
    # назван первым, потому что объясняет, почему он не основной.
    assert body.index("await window.ape.clipboard.writeText") < body.index("await navigator.clipboard.writeText"), \
        "ненадёжный путь идёт первым"


def test_вставка_в_поле_ввода_имеет_запасной_путь():
    """И учитывает русскую раскладку: при Ctrl+V с ней e.key приходит как «м»."""
    i = CHAT.index('$("inp").onkeydown')
    body = CHAT[i:i + 1400]
    assert "ape.clipboard" in body and "readText" in body
    assert '"м"' in body or '"М"' in body, "русская раскладка не учтена"
    assert "_pasteBefore" in body, "нет защиты от удвоения текста"
