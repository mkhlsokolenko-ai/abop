// Ядро фронта: загрузчик модулей + маршрутизация + API-клиент к сайдкару.
// Модули НЕ хардкодятся: берём список из /api/modules и динамически импортируем панель.
// Добавить фичу = новый backend-модуль + ui/modules/<id>/panel.js. Ядро не меняется.

import { apeLogo, apeMascot } from "./ape.js";

const API = new URLSearchParams(location.search).get("api") || "http://127.0.0.1:8799";

export async function api(path, opts = {}) {
  const r = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
// Approve/deny-гейт (из макета Overlays «Подтверждение действия»). Governance-слой:
// действие наружу (письмо/тикет/коммит) требует явного разрешения. Promise<bool>.
// Сейчас — визуально; при подключении RBAC/коннекторов станет обязательным барьером.
export function apeGate(action) {
  return new Promise((resolve) => {
    const a = action || {};
    const rows = (a.fields || []).map((f) => `<div style="display:flex;gap:10px;font-size:13px;margin-bottom:6px"><span class="faint" style="width:90px;flex:none">${esc(f[0])}</span><span>${esc(f[1])}</span></div>`).join("");
    const ov = document.createElement("div");
    ov.style = "position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:120;backdrop-filter:blur(2px)";
    ov.innerHTML = `<div class="ape-card" style="width:min(520px,92vw);gap:0;padding:20px;animation:ape-drop .18s ease">
      <div class="ape-label" style="margin-bottom:6px">Требуется подтверждение · governance</div>
      <div style="font-size:16px;font-weight:700;letter-spacing:-.3px;margin-bottom:12px">${esc(a.title || "Подтверждение действия")}</div>
      ${rows}
      ${a.body ? `<div style="background:var(--field);border:1px solid var(--line);border-radius:11px;padding:11px 13px;font-size:12.5px;line-height:1.5;margin:8px 0;white-space:pre-wrap">${esc(a.body)}</div>` : ""}
      <div class="faint" style="font-size:11.5px;margin:6px 0 2px">Наружу ничего не уйдёт без вашего решения. Маска ПДн применена.</div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
        <button class="btn" id="gDeny">Отклонить</button><button class="btn primary" id="gAllow">${esc(a.allowLabel || "Разрешить")}</button></div></div>`;
    document.body.appendChild(ov);
    const done = (v) => { ov.remove(); resolve(v); };
    ov.querySelector("#gAllow").onclick = () => done(true);
    ov.querySelector("#gDeny").onclick = () => done(false);
    ov.onclick = (e) => { if (e.target === ov) done(false); };
  });
}
function esc(s) { return (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

export const ctx = { api, base: API, user: null, gate: apeGate, mascot: apeMascot };

const $ = (id) => document.getElementById(id);
let MODULES = [];
let active = null;

function icon(name) {
  // минимальные глиф-иконки рейла (без внешних зависимостей)
  // Реальные глифы из эталона APE Desktop (standalone): геометрические, не смайлы.
  return { chat: "✦", agents: "⎔", cabinet: "◉", graphlens: "◈", opslens: "◎", connectors: "⛁", security: "⛨", ocr: "◵", ml: "❖", abop: "⬡" }[name] || "◆";
}

async function renderAuth() {
  const box = $("authBox");
  let me = { authed: false };
  try { me = await api("/api/auth/me"); } catch {}
  ctx.user = me.user || null;
  ctx.roles = me.roles || [];
  ctx.email = me.email || null;
  if (me.authed) {
    // Показываем, ПОД КАКИМ аккаунтом и ПОЧТОЙ работают агенты (адресность/Identity Map).
    const mail = me.email ? `<span title="почта, под которой действуют агенты" style="font-size:11px;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px">✉ ${me.email}</span>` : "";
    const dept = me.department ? `<span style="font-size:10.5px;color:var(--accent-ink-2,#a5b4fc)">· ${me.department}</span>` : "";
    box.innerHTML = `<span style="display:flex;flex-direction:column;line-height:1.25;min-width:0;max-width:210px">
        <span style="font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">🔑 ${me.name || me.user || ""}${dept}</span>${mail}</span>
      <button class="btn sm" id="logoutBtn" style="flex:none">Выйти</button>`;
    $("logoutBtn").onclick = async () => { await api("/api/auth/logout", { method: "POST" }); renderAuth(); };
  } else {
    // Единый вход: шлюз ABOP (Keycloak) — из него подтягиваются почта и доступы в системы (RBAC/ABAC).
    box.innerHTML = `<span style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">
        <button class="btn primary sm" id="loginBtn">🔑 Войти в ABOP</button>
        <span style="font-size:10px;color:var(--ink-3)">шлюз входа + почта и доступы</span></span>`;
    $("loginBtn").onclick = async () => {
      $("loginBtn").textContent = "Открываю браузер…";
      try {
        const res = await api("/api/auth/login", { method: "POST" });
        if (!res.ok) alert(res.error || "Вход не удался");
      } catch (e) { alert("Ошибка входа: " + e.message); }
      renderAuth();
      if (active) loadModule(active); // перерисуем панель уже авторизованным
    };
  }
}

// RBAC-видимость модулей: admin/advanced — только lecturer/admin (реальные роли из JWT).
// Клиент фильтрует каталог для UX; фактический энфорс — на шлюзе.
const MODULE_ROLES = { security: ["lecturer", "admin"], graphlens: ["lecturer", "admin"], opslens: ["lecturer", "admin"] };
function canSee(id) {
  const req = MODULE_ROLES[id];
  if (!req) return true;
  return (ctx.roles || []).some((r) => req.includes(r));
}
function visibleModules() { return MODULES.filter((m) => canSee(m.id)); }
function railBtn(glyph, label, on) {
  const bg = on ? "var(--accent-bg)" : "var(--panel)";
  const fg = on ? "var(--accent-ink)" : "var(--ink-2)";
  const bd = on ? "var(--line-2)" : "var(--line)";
  return `<span style="font-size:16px;line-height:1">${glyph}</span><span style="font-size:10px;font-weight:600">${label}</span>`
    .replace(/^/, `<button data-rb style="width:100%;padding:10px 4px;display:flex;flex-direction:column;align-items:center;gap:5px;border:1px solid ${bd};border-radius:12px;background:${bg};color:${fg}">`) + `</button>`;
}
// Рейл показывает ВСЕ доступные модули (раньше был жёсткий ["chat","cabinet"] → всё остальное
// пряталось за палитрой/«+» и было «невостребовано»). Порядок — осмысленный, «Чат» первым;
// ролевые модули (security/graphlens/opslens) сами отфильтруются canSee/visibleModules.
const RAIL_ORDER = ["chat", "connectors", "agents", "graphlens", "opslens", "security", "cabinet"];
const RAIL_TITLE = {};
function railModules() {
  const rank = (id) => { const i = RAIL_ORDER.indexOf(id); return i < 0 ? RAIL_ORDER.length : i; };
  return visibleModules().slice().sort((a, b) => rank(a.id) - rank(b.id));
}
function renderNav() {
  $("railNav").innerHTML = railModules().map((m) =>
    railBtn(icon(m.icon), RAIL_TITLE[m.id] || m.title, m.id === active).replace("data-rb", `data-id="${m.id}"`)).join("");
  $("railNav").querySelectorAll("[data-id]").forEach((b) => { b.onclick = () => loadModule(b.dataset.id); });
}

// Быстрые функции в рейле (настраиваемые, persist localStorage) — из макета.
const QF_DEFAULT = ["new"];   // палитра/тема уже в хедере — не дублируем; рейл теперь показывает все модули
function quickActions() {
  const a = [
    { id: "new", label: "Новый чат", icon: "➕", run: async () => { await loadModule("chat"); const b = document.querySelector("#newTh"); if (b) b.click(); } },
    { id: "palette", label: "Команды", icon: "⌘", run: openPalette },
    { id: "theme", label: "Тема", icon: "🌓", run: toggleTheme },
  ];
  visibleModules().forEach((m) => a.push({ id: "mod:" + m.id, label: m.title, icon: icon(m.icon), run: () => loadModule(m.id) }));
  return a;
}
function getPins() { try { return JSON.parse(localStorage.getItem("ape_quickfns")) || QF_DEFAULT; } catch { return QF_DEFAULT; } }
function slotBtn(inner, id, title, dashed) {
  const b = dashed ? "dashed var(--line-2)" : "solid var(--line)";
  return `<button ${id} title="${title}" style="width:44px;height:44px;display:flex;align-items:center;justify-content:center;border:1px ${b};border-radius:12px;background:var(--panel);color:var(--ink-2);font-size:15px">${inner}</button>`;
}
function renderQuick() {
  const el = document.getElementById("railQuick"); if (!el) return;
  const acts = quickActions(), pins = getPins();
  el.innerHTML = pins.map((id) => { const a = acts.find((x) => x.id === id); return a ? slotBtn(a.icon, `data-id="${a.id}"`, a.label) : ""; }).join("")
    + slotBtn("＋", `id="qfAdd"`, "Настроить быстрые функции", true);
  el.querySelectorAll("[data-id]").forEach((b) => b.onclick = () => { const a = acts.find((x) => x.id === b.dataset.id); if (a) a.run(); });
  document.getElementById("qfAdd").onclick = openQuickManage;
}
function openQuickManage() {
  const acts = quickActions(), pins = getPins();
  const ov = document.createElement("div");
  ov.style = "position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:100;backdrop-filter:blur(2px)";
  ov.innerHTML = `<div style="width:min(420px,92vw);background:var(--panel);backdrop-filter:blur(20px);border:1px solid var(--b1);border-radius:14px;padding:18px">
    <div style="font-weight:600;margin-bottom:12px">Быстрые функции в меню</div>
    ${acts.map((a) => `<label style="display:flex;gap:8px;align-items:center;padding:6px 0;font-size:13px"><input type="checkbox" class="qfc" value="${a.id}" ${pins.includes(a.id) ? "checked" : ""}/> ${a.icon} ${a.label}</label>`).join("")}
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px"><button class="btn" id="qfCancel">Закрыть</button><button class="btn primary" id="qfSave">Сохранить</button></div></div>`;
  document.body.appendChild(ov);
  ov.querySelector("#qfCancel").onclick = () => ov.remove();
  ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
  ov.querySelector("#qfSave").onclick = () => { localStorage.setItem("ape_quickfns", JSON.stringify([...ov.querySelectorAll(".qfc:checked")].map((x) => x.value))); ov.remove(); renderQuick(); };
}

async function loadModule(id) {
  active = id;
  renderNav();
  const m = MODULES.find((x) => x.id === id);
  const panel = $("panel");
  panel.innerHTML = `<div class="faint" style="padding:24px">Загрузка модуля «${m ? m.title : id}»…</div>`;
  // root объявлен ДО try — иначе в catch он вне области видимости и сам кидает ReferenceError,
  // маскируя настоящую ошибку загрузки (экран навсегда застревал на «Загрузка модуля…»).
  const root = document.createElement("div");
  root.style.cssText = "flex:1;min-width:0;min-height:0;display:flex";
  try {
    const mod = await import(`../modules/${m.ui}/panel.js`);
    panel.innerHTML = "";
    // единый flex-контейнер: чат кладёт aside+section, прочие — одну панель (flex:1)
    panel.appendChild(root);
    await mod.mount(root, ctx);
  } catch (e) {
    panel.innerHTML = `<div style="padding:24px;color:var(--crit)">Модуль «${m ? m.title : id}» не загрузился: ${esc(String(e && e.message || e))}</div>`;
    console.error(e);
  }
}

function renderUpdate(s) {
  const bar = document.getElementById("updateBar");
  if (!bar) return;
  const show = (html, bg) => {
    bar.style.display = "flex";
    bar.style.cssText += ";align-items:center;gap:12px;padding:8px 16px;border-bottom:1px solid var(--b1);font-size:13px;" + (bg ? "background:var(--accent-bg)" : "background:var(--panel)");
    bar.innerHTML = html;
  };
  if (s.state === "downloading") show(`⬇ Обновление ${s.version ? "v" + s.version : ""} — скачивается ${s.percent || 0}%`);
  else if (s.state === "available") show(`⬇ Найдено обновление v${s.version} — скачиваю…`);
  else if (s.state === "ready") {
    show(`<span style="flex:1">✓ Обновление <b>v${s.version}</b> готово к установке</span>
      <button class="btn primary sm" id="updNow">Перезапустить и обновить</button>`, true);
    const b = document.getElementById("updNow");
    if (b) b.onclick = () => window.ape.updater.install();
  } else if (s.state === "error") show(`<span class="faint">Апдейтер: ${(s.message || "").slice(0, 120)}</span>`);
  else bar.style.display = "none"; // checking / none
}

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const b = document.getElementById("themeBtn");
  if (b) b.textContent = t === "light" ? "☀️" : "🌙";
}
function toggleTheme() {
  const t = (localStorage.getItem("ape_theme") || "dark") === "dark" ? "light" : "dark";
  localStorage.setItem("ape_theme", t); applyTheme(t);
}
function initTheme() {
  applyTheme(localStorage.getItem("ape_theme") || "dark");
  const b = document.getElementById("themeBtn");
  if (b) b.onclick = toggleTheme;
}

// Командная палитра (Ctrl+K) — из макета «Поиск и команды».
function buildCommands() {
  const cmds = visibleModules().map((m) => ({ glyph: icon(m.icon), label: "Открыть: " + m.title, note: "вкладка", run: () => loadModule(m.id) }));
  cmds.push({ glyph: "➕", label: "Новый тред", note: "чат", keys: "Ctrl N", run: async () => { await loadModule("chat"); const b = document.querySelector("#newTh"); if (b) b.click(); } });
  cmds.push({ glyph: "🌓", label: "Переключить тему", note: "светлая / тёмная", run: toggleTheme });
  if (window.ape && window.ape.updater) cmds.push({ glyph: "⬇", label: "Проверить обновления", note: "апдейтер", run: () => window.ape.updater.check() });
  return cmds;
}
function openPalette() {
  if (document.getElementById("cmdPalette")) return;
  const all = buildCommands();
  const ov = document.createElement("div");
  ov.id = "cmdPalette";
  ov.style = "position:fixed;inset:0;z-index:90;display:flex;align-items:flex-start;justify-content:center;padding-top:14vh";
  ov.innerHTML = `<div id="palBack" style="position:absolute;inset:0;background:rgba(4,7,18,.6);backdrop-filter:blur(6px)"></div>
    <div style="position:relative;width:600px;max-width:92vw;border-radius:16px;background:var(--panel-2);border:1px solid var(--line-2);box-shadow:0 24px 70px rgba(0,0,0,.45);backdrop-filter:blur(20px);overflow:hidden;animation:ape-drop .28s ease-out">
      <div style="display:flex;align-items:center;gap:12px;padding:14px 16px;border-bottom:1px solid var(--line)">${apeMascot("idle", 24)}
        <input id="cmdInp" placeholder="Поиск и команды…" autocomplete="off" style="flex:1;min-width:0;padding:8px 0;border:none;background:transparent;color:var(--ink);font-size:15px;outline:none"/>
        <span style="font-family:var(--mono);font-size:10px;color:var(--ink-3)">Esc</span></div>
      <div id="cmdList" style="max-height:46vh;overflow-y:auto;padding:8px"></div></div>`;
  document.body.appendChild(ov);
  let items = all, sel = 0;
  const list = ov.querySelector("#cmdList");
  const close = () => ov.remove();
  const runSel = () => { const c = items[sel]; close(); if (c) c.run(); };
  const paint = () => {
    list.innerHTML = items.map((c, i) => `<button class="cmd" data-i="${i}" style="width:100%;display:flex;align-items:center;gap:12px;padding:11px 12px;border:none;border-radius:10px;background:${i === sel ? "var(--hover)" : "transparent"};text-align:left;cursor:pointer">
      <span style="width:26px;height:26px;flex:none;border-radius:8px;background:var(--hover);display:flex;align-items:center;justify-content:center;font-size:13px">${c.glyph || "▸"}</span>
      <span style="flex:1;display:flex;flex-direction:column;gap:2px"><span style="font-size:13px;font-weight:600;color:var(--ink)">${c.label}</span>${c.note ? `<span style="font-size:11px;color:var(--ink-3)">${c.note}</span>` : ""}</span>
      ${c.keys ? `<span style="font-family:var(--mono);font-size:10px;color:var(--ink-3)">${c.keys}</span>` : ""}</button>`).join("")
      || `<div style="padding:26px;text-align:center;font-size:12.5px;color:var(--ink-3)">Ничего не нашлось.</div>`;
    list.querySelectorAll(".cmd").forEach((e) => { e.onmouseenter = () => { sel = +e.dataset.i; paint(); }; e.onclick = runSel; });
  };
  ov.querySelector("#palBack").onclick = close;
  const inp = ov.querySelector("#cmdInp");
  inp.oninput = () => { const q = inp.value.toLowerCase(); items = all.filter((c) => c.label.toLowerCase().includes(q)); sel = 0; paint(); };
  ov.onkeydown = (e) => {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowDown") { sel = Math.min(sel + 1, items.length - 1); paint(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); paint(); e.preventDefault(); }
    else if (e.key === "Enter") runSel();
  };
  ov.onclick = (e) => { if (e.target === ov) close(); };
  paint(); inp.focus();
}

async function refreshCost() {
  // Реальный биллинг из RunMetrics (тот же источник, что нижний чип в чате), а не старая
  // недельная usage-метрика кабинета — иначе верхняя «квота» вводила в заблуждение (#2).
  try {
    const b = await api("/api/modules/cabinet/billing");
    if (b && !b.error && b.tokens != null) {
      const fmt = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n));
      const cv = document.getElementById("costVal"); if (cv) cv.textContent = (b.spent_rub ? b.spent_rub + " ₽" : "≈0 ₽");
      const qv = document.getElementById("quotaVal");
      if (qv) {
        const pct = Math.round(b.tokens_pct || 0);
        qv.textContent = "токены " + fmt(b.tokens) + " / " + fmt(b.token_quota);
        qv.title = "остаток " + fmt(b.tokens_remaining) + " · " + (100 - pct) + "% квоты";
        qv.style.color = pct >= 90 ? "var(--danger-ink)" : pct >= 70 ? "#fbbf24" : "var(--ink-2)";
      }
    }
  } catch {}
}

async function boot() {
  const lg = document.getElementById("apeLogo"); if (lg) lg.innerHTML = apeLogo(30);
  initTheme();
  try { const h = await api("/api/health"); const v = document.getElementById("verChip"); if (v) v.textContent = "v" + (h.version || "?"); } catch {}
  if (window.ape && window.ape.updater) window.ape.updater.onStatus(renderUpdate);
  // глобальный хоткей: выделенный текст из Word/Excel/браузера → открыть чат и отправить на анализ
  if (window.ape && window.ape.onAnalyze) window.ape.onAnalyze(async (text) => {
    if (active !== "chat") await loadModule("chat");
    // ждём монтирование чата, затем прокидываем текст
    setTimeout(() => { if (window.__apeAnalyze) window.__apeAnalyze(text); }, active === "chat" ? 0 : 260);
  });
  await renderAuth();
  refreshCost();
  try { MODULES = await api("/api/modules"); } catch { MODULES = []; }
  renderNav();
  renderQuick();
  if (MODULES.length) loadModule(MODULES[0].id);
  else $("panel").innerHTML = `<div class="faint" style="padding:24px">Модули не найдены. Проверь сайдкар.</div>`;
  // командная палитра: Ctrl+K + кнопка в топбаре
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && (e.key === "k" || e.key === "K")) { e.preventDefault(); openPalette(); }
  });
  const pb = document.getElementById("cmdBtn");
  if (pb) pb.onclick = openPalette;
}

boot();
