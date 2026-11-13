// Electron main: спавнит Python-сайдкар (движок) и рендерит модульный UI поверх него.
// Оболочка тонкая — вся логика в сайдкаре; окно можно заменить, не трогая движок.
const { app, BrowserWindow, Menu, Tray, Notification, nativeImage, dialog, shell, ipcMain, globalShortcut, clipboard, powerMonitor, net: сеть } = require("electron");
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
    icon: path.join(__dirname, "..", "build", process.platform === "win32" ? "icon.ico" : "icon.png"),
    title: "ABOP Desktop",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });
  installContextMenu(win);
  // Закрытие окна ПРЯЧЕТ приложение, а не завершает: иначе уведомление о конце прогона некому
  // показать — прогон идёт минуты, и окно за это время закрывают. Выход — из меню в трее.
  win.on("close", (e) => {
    if (выходим) return;
    e.preventDefault();
    win.hide();
    if (!былоПодсказки) {
      былоПодсказки = true;
      уведомить({ title: "ABOP продолжает следить за прогонами",
                  body: "Значок в трее: открыть окно или выйти совсем." });
    }
  });
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

// ── Трей и уведомления ОС ───────────────────────────────────────────────────────────────────
// Счётчик и текст приходят ИЗ UI: он один знает, что уже показывал, и не станет извещать на
// каждый опрос очереди. Main только показывает — так логика живёт в одном месте.
let tray = null;
let выходим = false;
let былоПодсказки = false;
let ждут = 0;

function значок(размер) {
  // macOS не декодирует .ico — nativeImage отдаёт пустую картинку, и трей выходит НЕВИДИМЫМ.
  // На Mac это не косметика: окно там закрывают крестиком постоянно, приложение остаётся жить в
  // фоне (см. window-all-closed), и трей — единственный путь обратно. Поэтому вне Windows берём
  // .png. Размер просим только для трея: в уведомление нужна полноразмерная картинка.
  const win32 = process.platform === "win32";
  const файл = path.join(__dirname, "..", "build", win32 ? "icon.ico" : "icon.png");
  let img = nativeImage.createFromPath(файл);
  if (img.isEmpty()) return nativeImage.createEmpty();
  if (размер) img = img.resize({ width: размер, height: размер });
  return img;
}

function обновитьТрей() {
  if (!tray) return;
  const хвост = ждут ? ` · ждут решения: ${ждут}` : "";
  tray.setToolTip("ABOP Desktop" + хвост);
  // Про обновление трей говорит сам: окно закрывают на недели, и баннер в нём никто не видит —
  // ровно поэтому 1.0.31 неделю не доезжала до машины, где стояла 1.0.27.
  const строкаОбновления = обновление.state === "ready"
    ? { label: `Установить обновление v${обновление.version}`, click: () => установитьОбновление() }
    : обновление.state === "manual"
      // macOS: ставит человек, поэтому трей ведёт к файлу, а не к установке.
      ? { label: `Скачать обновление v${обновление.version}`, click: () => открытьЗагрузку() }
    : обновление.state === "error"
      ? { label: "Проверка обновлений не удалась — повторить", click: () => проверитьОбновление("по кнопке") }
      : { label: "Проверить обновления", click: () => проверитьОбновление("по кнопке") };
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: ждут ? `Ждут вашего решения: ${ждут}` : "Ничего не ждёт", enabled: false },
    { type: "separator" },
    { label: "Открыть ABOP", click: () => показать() },
    строкаОбновления,
    { label: "Выход (полностью закрыть)", click: () => { выходим = true; stop(); app.quit(); } },
  ]));
  // Значок на иконке приложения поддерживают не все системы — там, где нет, остаётся трей.
  try { if (app.setBadgeCount) app.setBadgeCount(ждут); } catch (e) { /* не критично */ }
}

function показать(куда) {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
  if (куда) win.webContents.send("ape:open", куда);
}

function уведомить({ title, body, куда }) {
  if (!Notification.isSupported()) return false;
  const n = new Notification({ title: String(title || "ABOP"), body: String(body || ""),
                               icon: значок(), silent: false });
  // Клик по уведомлению обязан приводить ТУДА, о чём уведомили: иначе человек ищет сам.
  n.on("click", () => показать(куда));
  n.show();
  return true;
}

function создатьТрей() {
  if (tray) return;
  try {
    tray = new Tray(значок(process.platform === "win32" ? 0 : 18));
    tray.on("double-click", () => показать());
    обновитьТрей();
  } catch (e) {
    tray = null;   // без трея приложение работает как раньше, просто без фонового режима
  }
}

// Выбор папки для присмотра. Путь руками — верный способ опечататься в кириллическом пути и
// получить «папки нет» вместо работы; диалог ОС даёт ровно то, что существует.
ipcMain.handle("os:pickFolder", async () => {
  const r = await dialog.showOpenDialog(win, {
    title: "Папка под присмотром", properties: ["openDirectory"], buttonLabel: "Присматривать",
  });
  return (r && !r.canceled && r.filePaths && r.filePaths[0]) ? r.filePaths[0] : "";
});

ipcMain.handle("os:notify", (_e, данные) => уведомить(данные || {}));
ipcMain.handle("os:badge", (_e, n) => { ждут = Math.max(0, Number(n) || 0); обновитьТрей(); return ждут; });

// Видимый авто-апдейт: события electron-updater транслируем в UI (баннер с кнопкой).
//
// **Почему проверка повторяется, а не одна при старте.** Приложение живёт в трее: окно закрывают,
// процесс остаётся, и «запуск» случается раз в несколько недель. 27.10.2026 владелец сидел на
// 1.0.27 при вышедшей 1.0.31 — в журнале апдейтера единственная запись от 16.10, ровно та
// проверка при старте. Одноразовая проверка в приложении, которое не перезапускают, означает
// «обновлений не бывает».
//
// Поэтому три повода проверить: старт, каждые шесть часов и возвращение машины к жизни (сон,
// блокировка экрана). И четвёртый — повтор после ошибки: сорвавшаяся проверка (релиз ещё
// заливается, сеть мигнула) иначе гасила обновления до следующего запуска. Ровно это и
// случилось 16.10: `Cannot find latest.yml` на v1.0.27, пока CI не дозалил артефакты.
let updater = null;
const ЧАС = 3600000;
const ПРОВЕРЯТЬ_РАЗ_В = 6 * ЧАС;
const ПОВТОР_ПОСЛЕ_ОШИБКИ = 30 * 60000;
const НЕ_ЧАЩЕ = 15 * 60000;          // resume и unlock-screen приходят парой — не дёргаем дважды
// Состояние апдейтера для интерфейса: молчание не должно читаться как «у вас последняя версия».
const обновление = { state: "", version: "", проверено: 0, ошибка: "", ссылка: "" };
// На macOS авто-установка невозможна (ad-hoc подпись), поэтому канал там ручной: проверяем и
// показываем, а скачивает и ставит человек. Включается в setupUpdater.
let ручнойКанал = false;
let повторТаймер = null;

const send = (s) => { if (win && !win.isDestroyed()) win.webContents.send("updater:status", s); };

// Состояние — в одном месте для обоих каналов: авто-обновления Windows и ручного канала macOS.
// Пока это жило внутри ветки electron-updater, мак не мог сказать о себе ничего, и «Кабинет»
// показывал там «установлена последняя версия» — ровно то же, что на свежей сборке.
const запомнить = (s) => {
  обновление.state = s.state || "";
  if (s.version) обновление.version = s.version;
  if (s.ссылка !== undefined) обновление.ссылка = s.ссылка || "";
  if (s.state === "error") обновление.ошибка = String(s.message || "").slice(0, 300);
  if (s.state === "none" || s.state === "available" || s.state === "ready" || s.state === "manual") обновление.ошибка = "";
  if (s.state !== "checking") обновление.проверено = Date.now();
  // Сорвавшаяся проверка (релиз ещё заливается, сеть мигнула) иначе гасила обновления до
  // следующего запуска приложения, а его не перезапускают неделями.
  if (s.state === "error") {
    if (повторТаймер) clearTimeout(повторТаймер);
    повторТаймер = setTimeout(() => { повторТаймер = null; обновление.проверено = 0;
                                      проверитьОбновление("повтор после ошибки"); },
                              ПОВТОР_ПОСЛЕ_ОШИБКИ);
  }
  send(s);
  обновитьТрей();              // готовое обновление видно и при закрытом окне
};

function проверитьОбновление(повод) {
  if (!updater && !ручнойКанал) return;
  const сейчас = Date.now();
  if (повод !== "по кнопке" && обновление.проверено && сейчас - обновление.проверено < НЕ_ЧАЩЕ) return;
  обновление.проверено = сейчас;
  obsЛог("[updater] проверка: " + повод);
  if (ручнойКанал) { проверитьМакВручную(); return; }
  try {
    const p = updater.checkForUpdates();
    if (p && p.catch) p.catch((e) => obsЛог("[updater] проверка не удалась: " + ((e && e.message) || e)));
  } catch (e) { obsЛог("[updater] проверка не удалась: " + ((e && e.message) || e)); }
}

function obsЛог(строка) {
  try { console.log(строка); } catch (e) { /* noop */ }
}

// ── Ручной канал обновления (macOS) ──────────────────────────────────────────────────────────
//
// Установку обновления на маке делает Squirrel.Mac, и он требует, чтобы новая сборка была
// подписана ТЕМ ЖЕ сертификатом, что установленная. Подпись у нас ad-hoc (`codesign --sign -`):
// её хватает, чтобы приложение вообще запускалось на Apple Silicon, но для обновления она не
// годится — у каждой сборки она своя. Поэтому авто-установка на маке выключена и останется
// выключенной до покупки Developer ID ($99/год).
//
// Но «не ставим сами» не обязано означать «человек не знает». Раньше ветка darwin просто
// возвращалась: приложение на маке не проверяло НИЧЕГО, а «Кабинет» писал «установлена последняя
// версия» — то же самое, что написал бы на свежей сборке. О новой версии там узнавали, только
// если кто-то скажет словами.
//
// Ручной канал берёт версию из того же `latest-mac.yml`, по которому обновляется Windows, по
// тому же расписанию — и доводит до баннера со ссылкой. Скачивает и ставит человек.
const СЕТЕВОЙ_ТАЙМАУТ = 15000;

function реквизитыРелизов() {
  // Откуда качать — там же, куда публикует CI: один источник правды в `build.publish`.
  try {
    const пакет = require("../package.json");
    const куда = ((пакет.build || {}).publish || [])[0] || {};
    if (куда.owner && куда.repo) return { owner: куда.owner, repo: куда.repo };
  } catch (e) { /* ниже — значения по умолчанию */ }
  return { owner: "mkhlsokolenko-ai", repo: "abop" };
}

function запрос(адрес, метод) {
  // `net` Electron, а не node-овский `https`: он ходит через системные настройки прокси (в
  // организациях прямого выхода часто нет) и сам идёт по редиректам, которыми GitHub отдаёт
  // файлы релиза.
  return new Promise((resolve, reject) => {
    let r;
    try { r = сеть.request({ method: метод || "GET", url: адрес, redirect: "follow" }); }
    catch (e) { reject(e); return; }
    const таймер = setTimeout(() => {
      try { r.abort(); } catch (e) { /* noop */ }
      reject(new Error("нет ответа за " + Math.round(СЕТЕВОЙ_ТАЙМАУТ / 1000) + " с"));
    }, СЕТЕВОЙ_ТАЙМАУТ);
    r.on("response", (ответ) => {
      let тело = "";
      ответ.on("data", (кусок) => { тело += кусок.toString("utf8"); });
      ответ.on("end", () => { clearTimeout(таймер); resolve({ код: ответ.statusCode, тело }); });
    });
    r.on("error", (e) => { clearTimeout(таймер); reject(e); });
    r.end();
  });
}

function новееЧем(какая, чем) {
  // Числовое сравнение по частям, а не строковое: "1.0.9" > "1.0.10" как строки, и обновление
  // бы не предложилось. Предрелизный хвост (`1.0.40-rc1`) отбрасываем — нам хватает тройки.
  const части = (в) => String(в || "").split("-")[0].split(".").map((ч) => parseInt(ч, 10) || 0);
  const [a, b] = [части(какая), части(чем)];
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const разница = (a[i] || 0) - (b[i] || 0);
    if (разница) return разница > 0;
  }
  return false;
}

async function ссылкаНаЗагрузку(версия) {
  const { owner, repo } = реквизитыРелизов();
  const страница = `https://github.com/${owner}/${repo}/releases/tag/v${версия}`;
  const арка = process.arch === "arm64" ? "arm64" : "x64";
  const образ = `https://github.com/${owner}/${repo}/releases/download/v${версия}/ABOP-Desktop-${версия}-mac-${арка}.dmg`;
  // Образ собирается шагом, которому РАЗРЕШЕНО падать (на Intel-раннере том держит системная
  // служба, и так ушли три релиза подряд), поэтому прямая ссылка может вести в никуда. Ведём на
  // файл, только если он действительно есть; иначе на страницу релиза — там человек возьмёт ZIP.
  try {
    const ответ = await запрос(образ, "HEAD");
    if (ответ.код === 200) return образ;
  } catch (e) { /* ниже — страница релиза */ }
  return страница;
}

async function проверитьМакВручную() {
  const { owner, repo } = реквизитыРелизов();
  // Тот же манифест, по которому обновляется Windows-канал, и он сверен с содержимым релиза на
  // стороне CI — то есть версия в нём соответствует файлам, которые человек скачает.
  const адрес = `https://github.com/${owner}/${repo}/releases/latest/download/latest-mac.yml`;
  запомнить({ state: "checking" });
  try {
    const ответ = await запрос(адрес);
    if (ответ.код !== 200) throw new Error(`манифест обновления недоступен (${ответ.код})`);
    const найдено = /^version:\s*['"]?([0-9][\w.+-]*?)['"]?\s*$/m.exec(ответ.тело || "");
    if (!найдено) throw new Error("в манифесте обновления нет версии");
    const свежая = найдено[1];
    if (!новееЧем(свежая, app.getVersion())) { запомнить({ state: "none", ссылка: "" }); return; }
    запомнить({ state: "manual", version: свежая, ссылка: await ссылкаНаЗагрузку(свежая) });
  } catch (e) {
    запомнить({ state: "error", message: String((e && e.message) || e) });
  }
}

function открытьЗагрузку() {
  // Скачивание отдаём браузеру: тянуть 200 МБ внутрь приложения, которое всё равно не сможет
  // себя обновить, смысла нет — человеку нужен файл в «Загрузках», а не прогресс в баннере.
  if (!обновление.ссылка) return false;
  try { shell.openExternal(обновление.ссылка); return true; } catch (e) { return false; }
}

// Расписание общее для обоих каналов: поводы проверить не зависят от того, кто ставит.
function завестиРасписание() {
  проверитьОбновление("старт");
  setInterval(() => проверитьОбновление("по расписанию"), ПРОВЕРЯТЬ_РАЗ_В);
  // Машину закрывают крышкой, а не выключают: пробуждение — самый частый момент, когда
  // приложение снова в сети, а проверка по расписанию ещё не подошла.
  try {
    powerMonitor.on("resume", () => проверитьОбновление("после сна"));
    powerMonitor.on("unlock-screen", () => проверитьОбновление("после разблокировки"));
  } catch (e) { /* не на всех системах есть */ }
}

function setupUpdater() {
  if (!app.isPackaged) return;
  if (process.platform === "darwin") {
    // electron-updater на macOS ПРОВЕРЯЕТ подпись скачанного бандла, а Developer ID у сборки нет
    // (ad-hoc), поэтому АВТО-установка здесь невозможна: кнопка «Перезапустить и обновить» падала
    // бы на установке. Но проверять и показывать — можно и нужно: см. «Ручной канал» выше.
    // Появится Developer ID — убрать эту ветку вместе с ручным каналом, остальное уже готово.
    ручнойКанал = true;
    console.warn("[updater] macOS: авто-установка выключена (нет Developer ID), канал ручной");
    завестиРасписание();
    return;
  }
  try {
    const { autoUpdater } = require("electron-updater");
    updater = autoUpdater;
    try { autoUpdater.logger = require("electron-log"); autoUpdater.logger.transports.file.level = "info"; } catch (e) { /* лог опционален */ }
    autoUpdater.autoDownload = true;
    autoUpdater.autoInstallOnAppQuit = true;
    autoUpdater.on("checking-for-update", () => запомнить({ state: "checking" }));
    autoUpdater.on("update-available", (i) => запомнить({ state: "available", version: i && i.version }));
    autoUpdater.on("update-not-available", () => запомнить({ state: "none" }));
    autoUpdater.on("download-progress", (p) => send({ state: "downloading", percent: Math.round(p.percent || 0) }));
    autoUpdater.on("update-downloaded", (i) => запомнить({ state: "ready", version: i && i.version }));
    // Повтор после ошибки заводит `запомнить` — один на оба канала, и один таймер в очереди:
    // иначе на каждую ошибку заводился бы свой, и они множились.
    autoUpdater.on("error", (e) => запомнить({ state: "error", message: String((e && e.message) || e) }));
    завестиРасписание();
  } catch (e) {
    console.error("[updater] недоступен:", e && e.message);
  }
}

// действия из UI
ipcMain.handle("updater:check", () => { проверитьОбновление("по кнопке"); });
// Состояние апдейтера для интерфейса: когда проверяли в последний раз и чем кончилось. Без этого
// «баннера нет» значит сразу две разные вещи: «у вас последняя версия» и «проверка не доходит».
// `канал` отличает «ставим сами» от «показываем и даём ссылку»: без него экран не может
// сказать правду про мак, а «поддержано: false» читалось там как «мы ничего не знаем».
ipcMain.handle("updater:state", () => ({ ...обновление, версия: app.getVersion(),
                                         поддержано: !!updater || ручнойКанал,
                                         канал: ручнойКанал ? "ручной" : "авто" }));
ipcMain.handle("updater:download", () => открытьЗагрузку());
function установитьОбновление() {
  // Выход должен быть НАСТОЯЩИМ: при живом трее приложение остаётся в фоне, а установщик
  // упирается в запущенный процесс и встаёт. Поэтому снимаем трей, гасим сайдкар и только
  // потом отдаём управление установщику.
  выходим = true;
  try { if (tray) { tray.destroy(); tray = null; } } catch (e) { /* noop */ }
  stop();
  try {
    // isSilent=false: человек видит ход установки. isForceRunAfter=true: после установки
    // приложение само поднимается — иначе выглядит так, будто обновление «закрыло программу».
    updater && updater.quitAndInstall(false, true);
  } catch (e) { /* noop */ }
}
ipcMain.handle("updater:install", () => установитьОбновление());

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

// Один экземпляр на машину. Вторая копия держала бы те же файлы в папке установки, и обновление
// останавливалось бы, даже если «старую версию» человек закрыл: закрыл он одну из двух.
const одинЭкземпляр = app.requestSingleInstanceLock();
if (!одинЭкземпляр) {
  app.quit();
} else {
  app.on("second-instance", () => { выходим = false; показать(); });
  app.whenReady().then(() => { installMenu(); createWindow(); registerHotkey(); создатьТрей(); });
}
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
  if (!sidecar) return;
  const pid = sidecar.pid;
  sidecar.kill();
  // На Windows kill() гасит ОДИН процесс, а `ape-sidecar.exe` живёт в папке установки и держит
  // файлы: установщик обновления тогда останавливается на «нельзя закрыть приложение». Поэтому
  // гасим дерево синхронно — к началу установки процесса не должно быть вовсе.
  if (process.platform === "win32" && pid) {
    try {
      require("child_process").execFileSync("taskkill", ["/PID", String(pid), "/T", "/F"],
                                            { stdio: "ignore", timeout: 8000 });
    } catch (e) { /* процесс мог уже умереть — это и есть нужный исход */ }
  }
  sidecar = null;
}
app.on("window-all-closed", () => {
  // Пока жив трей — приложение остаётся в фоне и может известить о конце прогона. Выход только
  // осознанный: из меню в трее (там выставляется `выходим`). Без трея поведение прежнее.
  if (tray && !выходим) return;
  stop();
  if (process.platform !== "darwin") app.quit();
});
app.on("before-quit", stop);
