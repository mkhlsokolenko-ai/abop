// Electron main: спавнит Python-сайдкар (движок) и рендерит модульный UI поверх него.
// Оболочка тонкая — вся логика в сайдкаре; окно можно заменить, не трогая движок.
const { app, BrowserWindow, Menu, shell, ipcMain, globalShortcut, clipboard } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const net = require("net");
const http = require("http");
const fs = require("fs");
const os = require("os");

// UI по умолчанию — ЛОКАЛЬНЫЙ (из asar): надёжно, без ограничений Chromium Private Network Access
// (публичная http-страница НЕ вправе fetch'ить loopback 127.0.0.1 → «Модули не найдены»). Локальный UI
// собирается из desktop/ui при сборке .exe, т.е. содержит все изменения. Авто-обновление UI без
// пересборки — через сайдкар-прокси (следующий шаг): сайдкар тянет свежий UI с ABOP server-side (без PNA)
// и отдаёт с 127.0.0.1 (same-origin). Включить удалённый UI вручную: APE_UI_URL=<url>.
const REMOTE_UI = process.env.APE_UI_URL || "";

let sidecar = null;
let win = null;

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => {
      const p = s.address().port;
      s.close(() => resolve(p));
    });
  });
}

function pyCmd() {
  // dev: системный Python. В упакованной сборке позже подменим на bundled sidecar (PyInstaller).
  return process.platform === "win32" ? "py" : "python3";
}

function startSidecar(port) {
  // APE_APP_VERSION — реальная версия приложения (из package.json) → сайдкар отдаёт её в /api/health,
  // шапка UI показывает актуальную версию, а не захардкоженную.
  const env = Object.assign({}, process.env, { APE_SIDECAR_PORT: String(port), APE_APP_VERSION: app.getVersion() });
  let proc;
  if (app.isPackaged) {
    // прод: автономный бинарь сайдкара из extraResources (Python пользователю не нужен)
    const exe = process.platform === "win32" ? "ape-sidecar.exe" : "ape-sidecar";
    const bin = path.join(process.resourcesPath, "ape-sidecar", exe);
    proc = spawn(bin, [], { env });
  } else {
    // dev: системный Python из репозитория
    proc = spawn(pyCmd(), ["-m", "sidecar.app"], { cwd: path.join(__dirname, ".."), env });
  }
  proc.stdout.on("data", (d) => console.log("[sidecar]", d.toString().trim()));
  proc.stderr.on("data", (d) => console.error("[sidecar]", d.toString().trim()));
  proc.on("exit", (code) => console.log("[sidecar] exit", code));
  return proc;
}

function waitHealth(port, tries = 80) {
  return new Promise((resolve, reject) => {
    const timer = setInterval(() => {
      const req = http.get(
        { host: "127.0.0.1", port, path: "/api/health", timeout: 1000 },
        (r) => {
          if (r.statusCode === 200) {
            clearInterval(timer);
            resolve();
          }
          r.resume();
        }
      );
      req.on("error", () => {
        if (--tries <= 0) {
          clearInterval(timer);
          reject(new Error("sidecar не поднялся"));
        }
      });
      req.on("timeout", () => req.destroy());
    }, 400);
  });
}

async function createWindow() {
  const port = await freePort();
  sidecar = startSidecar(port);
  try {
    await waitHealth(port);
  } catch (e) {
    console.error(e);
  }
  win = new BrowserWindow({
    width: 1240,
    height: 820,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: "#0f172a",   // = --bg темы (без «чужой» вспышки при старте)
    icon: path.join(__dirname, "..", "build", "icon.ico"),
    title: "ABOP Desktop",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });
  installContextMenu(win);
  // внешние ссылки — в системный браузер (например, окно логина при необходимости)
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  // UX-аудит 25.09 D-C5: ссылка в ответе LLM не должна уводить само окно на внешний сайт
  // (кнопки «назад» нет) — любая навигация не на наш UI уходит в системный браузер.
  win.webContents.on("will-navigate", (e, url) => {
    const ours = url.startsWith("http://127.0.0.1:") || url.startsWith("file://") || (REMOTE_UI && url.startsWith(REMOTE_UI));
    if (ours) return;
    e.preventDefault();
    if (/^https?:/i.test(url)) shell.openExternal(url);
  });
  const apiBase = `http://127.0.0.1:${port}`;
  const localIndex = path.join(__dirname, "..", "ui", "index.html");
  // Правильный путь А: грузим UI С САЙДКАРА (127.0.0.1/ui) — один origin с /api/* (без PNA), UI свежий
  // (сайдкар синхронит с ABOP при старте). При сбое — откат на локальную копию из asar.
  // APE_UI_URL по-прежнему может переопределить (свой хост).
  const uiUrl = REMOTE_UI || (apiBase + "/ui/index.html");
  win.webContents.once("did-fail-load", () => win.loadFile(localIndex, { query: { api: apiBase } }));
  win.loadURL(uiUrl + (uiUrl.includes("?") ? "&" : "?") + "api=" + encodeURIComponent(apiBase));
  win.webContents.once("did-finish-load", setupUpdater); // UI уже слушает события апдейтера
}

// Видимый авто-апдейт: события electron-updater транслируем в UI (баннер с кнопкой).
let updater = null;
function setupUpdater() {
  if (!app.isPackaged) return;
  try {
    const { autoUpdater } = require("electron-updater");
    updater = autoUpdater;
    try { autoUpdater.logger = require("electron-log"); autoUpdater.logger.transports.file.level = "info"; } catch (e) { /* лог опционален */ }
    autoUpdater.autoDownload = true;
    autoUpdater.autoInstallOnAppQuit = true;
    const send = (s) => { if (win && !win.isDestroyed()) win.webContents.send("updater:status", s); };
    autoUpdater.on("checking-for-update", () => send({ state: "checking" }));
    autoUpdater.on("update-available", (i) => send({ state: "available", version: i && i.version }));
    autoUpdater.on("update-not-available", () => send({ state: "none" }));
    autoUpdater.on("download-progress", (p) => send({ state: "downloading", percent: Math.round(p.percent || 0) }));
    autoUpdater.on("update-downloaded", (i) => send({ state: "ready", version: i && i.version }));
    autoUpdater.on("error", (e) => send({ state: "error", message: String((e && e.message) || e) }));
    autoUpdater.checkForUpdates();
  } catch (e) {
    console.error("[updater] недоступен:", e && e.message);
  }
}

// действия из UI
ipcMain.handle("updater:check", () => { try { updater && updater.checkForUpdates(); } catch (e) { /* noop */ } });
ipcMain.handle("updater:install", () => { try { updater && updater.quitAndInstall(); } catch (e) { /* noop */ } });

// экспорт в PDF: рендерим HTML в скрытом окне → printToPDF → в «Загрузки» (кириллица ок, Chromium)
// Готовый файл (отчёт, выгрузка) раньше показывался только путём в уведомлении: открыть его или
// найти в папке из приложения было нельзя.
ipcMain.handle("file:open", async (_e, p) => {
  try { const err = await shell.openPath(String(p || "")); return { ok: !err, error: err || "" }; }
  catch (e) { return { ok: false, error: String(e) }; }
});
ipcMain.handle("file:reveal", (_e, p) => {
  try { shell.showItemInFolder(String(p || "")); return { ok: true }; }
  catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("export:pdf", async (_e, { html, filename }) => {
  let w = null;
  try {
    w = new BrowserWindow({ show: false, webPreferences: { offscreen: true } });
    await w.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html || "<html></html>"));
    const pdf = await w.webContents.printToPDF({ printBackground: true, margins: { marginType: "default" } });
    const dl = path.join(os.homedir(), "Downloads");
    const dir = fs.existsSync(dl) ? dl : os.homedir();
    const p = path.join(dir, filename || "chat.pdf");
    fs.writeFileSync(p, pdf);
    return { ok: true, path: p };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  } finally {
    if (w) w.destroy();
  }
});

// Глобальный хоткей Ctrl+Shift+A: берём выделенный текст (через буфер обмена) из ЛЮБОГО
// приложения — Word/Excel/браузер/PDF — и отправляем в чат ABOP на анализ. Один шаг для пользователя.
let hotkeyActive = "";      // какое сочетание в итоге занято; "" — ни одно (все были заняты)

function registerHotkey() {
  const { execSync } = require("child_process");
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // Грабим выделение НАДЁЖНО: (1) ЧИСТИМ буфер (маркер) — тогда любое непустое значение = свежая копия,
  // и исчезает баг «вернули старую ссылку из буфера»; (2) шлём Ctrl+C активному окну (Word/Excel — оно
  // ещё в фокусе, своё окно фокусим ПОСЛЕ); (3) ПОЛЛИМ буфер до ~900мс, пока не появится текст (Office
  // кладёт данные асинхронно) — вместо фикс. задержки; по таймауту честно возвращаем пусто.
  const grab = async () => {
    const saved = clipboard.readText();       // вернём назад в конце (вежливость к буферу)
    const marker = "__ABOP_SEL__";   // обычная строка: символ NUL буфер Windows обрезал, и «чистка» работала случайно
    try { clipboard.writeText(marker); } catch (e) { /* noop */ }
    try {
      if (process.platform === "win32") {
        execSync('powershell -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait(\'^c\')"', { timeout: 2500, windowsHide: true });
      }
    } catch (e) { /* SendKeys недоступен — поллинг ниже вернёт пусто */ }
    let fresh = "";
    for (let i = 0; i < 16; i++) {            // до ~800мс: ждём свежую копию
      await sleep(50);
      const t = clipboard.readText();
      if (t && t !== marker) { fresh = t; break; }
    }
    // приоритет: свежескопированное (SendKeys сработал); иначе — фолбэк на прежний буфер
    // (сценарий «скопировал Ctrl+C → Ctrl+Shift+A» работает всегда). Никогда не отдаём пусто зря.
    const out = fresh || saved || "";
    // D-H13: буфер обмена пользователя ВСЕГДА возвращаем как был — хоткей не должен его переписывать.
    try { clipboard.writeText(saved || ""); } catch (e) { /* noop */ }
    return out;
  };
  const onFire = async () => {
    const text = (await grab()).trim();
    if (!win || win.isDestroyed()) return;
    if (win.isMinimized()) win.restore();
    win.show(); win.focus();
    win.webContents.send("ape:analyze", text);   // пусто → UI покажет подсказку «выделите текст»
  };
  // register() возвращает false, когда сочетание уже занято другим приложением, и исключения при
  // этом НЕ бросает — мы проверяли только try/catch, поэтому «хоткей не работает» выглядело как
  // молчание. Пробуем по очереди и сообщаем в интерфейс, какое сочетание в итоге живое.
  const wanted = ["CommandOrControl+Shift+A", "CommandOrControl+Alt+A", "CommandOrControl+Shift+F9"];
  hotkeyActive = "";
  for (const combo of wanted) {
    try {
      if (globalShortcut.register(combo, onFire) && globalShortcut.isRegistered(combo)) {
        hotkeyActive = combo;
        break;
      }
      console.warn("[hotkey] занято другим приложением:", combo);
    } catch (e) { console.error("[hotkey] не зарегистрирован:", combo, e && e.message); }
  }
  if (!hotkeyActive) console.error("[hotkey] ни одно сочетание не удалось занять — анализ выделенного недоступен");
  const tell = () => { try { if (win && !win.isDestroyed()) win.webContents.send("ape:hotkey", hotkeyActive); } catch (e) { /* noop */ } };
  tell();
  if (win && !win.isDestroyed()) win.webContents.on("did-finish-load", tell);
}

// Меню приложения держит акселераторы правки. Без него (а мы его не ставили вовсе) Ctrl+C, Ctrl+V,
// Ctrl+X и Ctrl+A в окне не работают: на Windows Electron без меню их просто не регистрирует —
// отсюда «текст не вставляется» и «скопировал из чата, а вставлять нечего». Полоску меню прячем,
// чтобы вид приложения не менялся: горячие клавиши работают и со скрытым меню.
function installMenu() {
  const mac = process.platform === "darwin";
  const template = [
    ...(mac ? [{ role: "appMenu" }] : []),
    {
      label: "Правка",
      submenu: [
        { role: "undo", label: "Отменить" },
        { role: "redo", label: "Повторить" },
        { type: "separator" },
        { role: "cut", label: "Вырезать" },
        { role: "copy", label: "Копировать" },
        { role: "paste", label: "Вставить" },
        { role: "pasteAndMatchStyle", label: "Вставить без форматирования" },
        { role: "selectAll", label: "Выделить всё" },
      ],
    },
    {
      label: "Вид",
      submenu: [
        { role: "reload", label: "Обновить" },
        { role: "resetZoom", label: "Обычный размер" },
        { role: "zoomIn", label: "Крупнее" },
        { role: "zoomOut", label: "Мельче" },
        { type: "separator" },
        { role: "toggleDevTools", label: "Инструменты разработчика" },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// Правый клик в поле ввода: путь к буферу мышью. Нужен и сам по себе, и как страховка — если
// сочетание клавиш перехватит другое приложение, вставить всё равно можно.
function installContextMenu(w) {
  w.webContents.on("context-menu", (_e, params) => {
    const items = [];
    if (params.isEditable) {
      items.push({ role: "cut", label: "Вырезать", enabled: params.editFlags.canCut });
      items.push({ role: "copy", label: "Копировать", enabled: params.editFlags.canCopy });
      items.push({ role: "paste", label: "Вставить", enabled: params.editFlags.canPaste });
      items.push({ type: "separator" });
      items.push({ role: "selectAll", label: "Выделить всё" });
    } else if (params.selectionText) {
      items.push({ role: "copy", label: "Копировать" });
    }
    if (items.length) Menu.buildFromTemplate(items).popup({ window: w });
  });
}

app.whenReady().then(() => { installMenu(); createWindow(); registerHotkey(); });
// Буфер обмена напрямую: `navigator.clipboard` в окне зависит от разрешений и фокуса документа и
// молча отказывает — кнопка «копировать» тогда врёт. Модуль clipboard Electron таких условий не
// имеет, поэтому интерфейсу нужен этот путь как основной.
ipcMain.handle("clip:write", (_e, text) => { try { clipboard.writeText(String(text == null ? "" : text)); return true; } catch (e) { return false; } });
ipcMain.handle("clip:read", () => { try { return clipboard.readText(); } catch (e) { return ""; } });
ipcMain.handle("hotkey:active", () => hotkeyActive);

app.on("will-quit", () => { try { globalShortcut.unregisterAll(); } catch (e) { /* noop */ } });
app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
function stop() {
  if (sidecar) {
    sidecar.kill();
    sidecar = null;
  }
}
app.on("window-all-closed", () => {
  stop();
  if (process.platform !== "darwin") app.quit();
});
app.on("before-quit", stop);
