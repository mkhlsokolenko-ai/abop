// Ядро фронта: загрузчик модулей + маршрутизация + API-клиент к сайдкару + общие примитивы UI.
// Модули НЕ хардкодятся: берём список из /api/modules и динамически импортируем панель.
// Добавить фичу = новый backend-модуль + ui/modules/<id>/panel.js. Ядро не меняется.
//
// UX-аудит 25.09 (docs/UX_AUDIT_2026-09-25.md, раздел Desktop): здесь закрыты D-C1 (api() читает
// тело ошибки и переводит её человеку), D-H2 (экран «Запускаю движок…» с ретраем), D-H11 (кэш
// модулей — рейл не пересоздаёт панель и не стирает черновики), D-H12 (401 → шапка «Войти», один
// термин «ABOP»), D-H15 (стек оверлеев: Esc закрывает верхний, фокус-ловушка), D-H16 (стек тостов).

import { apeLogo, apeMascot } from "./ape.js";

const API = new URLSearchParams(location.search).get("api") || "http://127.0.0.1:8799";

// ── API-клиент ──────────────────────────────────────────────────────────────────────────────────
export class ApiError extends Error {
  constructor(status, detail, body) { super(detail || ("HTTP " + status)); this.status = status; this.detail = detail || ""; this.body = body || null; }
}
export async function api(path, opts = {}) {
  let r;
  try {
    r = await fetch(API + path, { headers: { "Content-Type": "application/json" }, ...opts });
  } catch (e) {
    if (e && e.name === "AbortError") throw e;
    throw new ApiError(0, "движок недоступен", null);
  }
  const txt = await r.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch { data = null; }
  if (!r.ok) {
    const detail = (data && (data.error || data.detail || data.message)) || txt.slice(0, 200) || r.statusText;
    const st = (data && Number(data.status)) || r.status;
    if (st === 401) queueAuthRefresh();
    throw new ApiError(st, String(detail), data);
  }
  return data;
}
// Ошибка → человеческая фраза (D-C1). Понимает ApiError, строки вида «ABOP 401: …» и коды сайдкара.
export function humanError(e) {
  const raw = e == null ? "" : (typeof e === "string" ? e : (e.detail || e.message || String(e)));
  let st = e && typeof e === "object" ? Number(e.status || 0) : 0;
  const m = /ABOP (\d{3})\s*:\s*(.*)/.exec(raw);
  let d = raw;
  if (m) { st = st || Number(m[1]); d = m[2] || ""; }
  if (/^auth_required$|401/.test(raw) || st === 401) return "Нужно войти в ABOP — кнопка «Войти» вверху справа.";
  if (st === 403 || /^forbidden$/.test(raw)) return "Нет прав: " + (d && d !== "forbidden" ? d : "действие недоступно вашей роли.");
  if (st === 404) return d || "Не найдено.";
  if (st === 409) return d || "Уже обработано.";
  if (st === 0 || st === 502 || st === 503 || st === 504 || /движок недоступен|Failed to fetch|ConnectionRefused|URLError|timed out/i.test(raw))
    return "ABOP недоступен — проверьте сеть и повторите." + (d && !/движок недоступен/.test(d) ? " (" + d.slice(0, 120) + ")" : "");
  if (st >= 500) return "Ошибка на сервере ABOP: " + (d || st);
  return d || "Не удалось выполнить действие.";
}
export function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// ── стек оверлеев: Esc закрывает верхний, фокус-ловушка, возврат фокуса (D-H15) ─────────────────
const overlays = [];
export function openOverlay({ html, onClose, closeOnBackdrop = true, label = "Диалог", className = "" } = {}) {
  const ov = document.createElement("div");
  ov.className = "ape-overlay";
  ov.style.zIndex = String(300 + overlays.length);
  ov.setAttribute("role", "dialog"); ov.setAttribute("aria-modal", "true"); ov.setAttribute("aria-label", label);
  ov.innerHTML = html;
  const prev = document.activeElement;
  let closed = false;
  const close = (v) => {
    if (closed) return; closed = true;
    const i = overlays.indexOf(rec); if (i >= 0) overlays.splice(i, 1);
    HTMLElement.prototype.remove.call(ov);
    if (onClose) { try { onClose(v); } catch (e) { console.error(e); } }
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch { /* noop */ } }
  };
  const rec = { el: ov, close };
  overlays.push(rec);
  ov.remove = () => close(undefined);   // старые модули зовут ov.remove() — стек всё равно чистится
  ov.close = close;
  if (closeOnBackdrop) ov.addEventListener("mousedown", (e) => { if (e.target === ov) close(undefined); });
  ov.addEventListener("keydown", (e) => {   // фокус-ловушка внутри верхнего оверлея
    if (e.key !== "Tab") return;
    const f = [...ov.querySelectorAll("button,[href],input,select,textarea,[tabindex]:not([tabindex='-1'])")].filter((x) => !x.disabled && x.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
    else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
  });
  document.body.appendChild(ov);
  const auto = ov.querySelector("[autofocus]") || ov.querySelector("input,textarea,select") || ov.querySelector("button.primary, button");
  if (auto) setTimeout(() => { try { auto.focus(); } catch { /* noop */ } }, 0);
  if (className) ov.classList.add(className);
  return ov;
}
export function overlayOpen() { return overlays.length > 0; }
// Esc — на фазе захвата, чтобы модуль (напр. чат) не остановил стрим, закрывая модалку.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !overlays.length) return;
  e.stopPropagation(); e.preventDefault();
  overlays[overlays.length - 1].close(undefined);
}, true);

// Универсальная модалка ДС. onOk(body) → false = не закрывать. Возвращает элемент оверлея
// (querySelector/close/remove работают).
export function modal(title, bodyHTML, onOk, okLabel, opts = {}) {
  const ov = openOverlay({
    label: title || "Диалог",
    closeOnBackdrop: opts.closeOnBackdrop !== false,
    html: `<div class="ape-modal" style="${opts.width ? "width:min(" + opts.width + ",92vw)" : ""}">
      ${opts.kicker ? `<div class="ape-label">${esc(opts.kicker)}</div>` : ""}
      <h2>${esc(title || "")}</h2>
      <div id="mBody">${bodyHTML || ""}</div>
      <div class="ape-actions">
        <button class="btn" id="mCancel">${esc(opts.cancelLabel || (onOk ? "Отмена" : "Закрыть"))}</button>
        ${onOk ? `<button class="btn ${opts.danger ? "danger" : "primary"}" id="mOk">${esc(okLabel || "Готово")}</button>` : ""}
      </div></div>`,
    onClose: opts.onClose,
  });
  ov.querySelector("#mCancel").onclick = () => ov.close(false);
  if (onOk) ov.querySelector("#mOk").onclick = async () => {
    const b = ov.querySelector("#mOk"); const t = b.textContent;
    b.disabled = true;
    try { const r = await onOk(ov.querySelector("#mBody")); if (r !== false) ov.close(true); else { b.disabled = false; b.textContent = t; } }
    catch (e) { b.disabled = false; b.textContent = t; toast(humanError(e), "danger"); }
  };
  return ov;
}
// Подтверждение опасного действия → Promise<bool> (D-H9: вместо confirm()).
export function confirmDialog({ title, text, okLabel = "Удалить", danger = true, kicker } = {}) {
  return new Promise((resolve) => {
    const ov = modal(title || "Подтвердите действие", `<div style="font-size:13px;line-height:1.6;color:var(--ink-2)">${text || ""}</div>`,
      () => { resolve(true); }, okLabel, { danger, kicker, onClose: (v) => { if (v !== true) resolve(false); } });
    return ov;
  });
}
// Approve/deny-гейт (из макета Overlays «Подтверждение действия»). Governance-слой:
// действие наружу (письмо/тикет/публикация) требует явного разрешения. Promise<bool>.
export function apeGate(action) {
  const a = action || {};
  const rows = (a.fields || []).map((f) => `<div style="display:flex;gap:10px;font-size:13px;margin-bottom:6px"><span class="faint" style="width:110px;flex:none">${esc(f[0])}</span><span style="min-width:0;overflow-wrap:anywhere">${esc(f[1])}</span></div>`).join("");
  return new Promise((resolve) => {
    modal(a.title || "Подтверждение действия",
      `${rows}${a.body ? `<div style="background:var(--field);border:1px solid var(--line);border-radius:11px;padding:11px 13px;font-size:12.5px;line-height:1.5;margin:8px 0;white-space:pre-wrap;max-height:40vh;overflow:auto">${esc(a.body)}</div>` : ""}
       ${a.html ? `<div style="background:var(--field);border:1px solid var(--line);border-radius:11px;padding:11px 13px;font-size:12.5px;line-height:1.5;margin:8px 0;max-height:40vh;overflow:auto">${a.html}</div>` : ""}
       <div class="faint" style="font-size:11.5px;margin:6px 0 2px">${esc(a.note || "Наружу ничего не уйдёт без вашего решения.")}</div>`,
      () => { resolve(true); }, a.allowLabel || "Разрешить",
      { kicker: a.kicker || "Требуется подтверждение · governance", cancelLabel: a.denyLabel || "Отклонить", onClose: (v) => { if (v !== true) resolve(false); } });
  });
}

// ── стек тостов (D-H16: один слот 2.6 с терял итог платного прогона) ────────────────────────────
export function toast(msg, kind = "", opts = {}) {
  let box = document.getElementById("apeToasts");
  if (!box) { box = document.createElement("div"); box.id = "apeToasts"; box.setAttribute("aria-live", "polite"); document.body.appendChild(box); }
  const t = document.createElement("div");
  t.className = "ape-toast" + (kind ? " " + kind : "");
  t.innerHTML = `<span style="min-width:0">${esc(msg)}</span>${opts.action ? `<button type="button">${esc(opts.action.label)}</button>` : ""}`;
  if (opts.action) t.querySelector("button").onclick = () => { try { opts.action.run(); } finally { t.remove(); } };
  box.appendChild(t);
  while (box.children.length > 4) box.firstChild.remove();
  const ttl = opts.ttl || (opts.action ? 7000 : 4200);
  setTimeout(() => { t.style.transition = "opacity .25s"; t.style.opacity = "0"; setTimeout(() => t.remove(), 260); }, ttl);
  return t;
}

// ── контекст модулей ────────────────────────────────────────────────────────────────────────────
export const ctx = {
  api, base: API, user: null, roles: [], email: null, authed: false,
  gate: apeGate, mascot: apeMascot, modal, confirm: confirmDialog, toast, humanError, esc, openOverlay,
  // навигация с намерением: ctx.open("chat", { attach: {...} }) — модуль получает intent (см. loadModule)
  open: (id, intent) => loadModule(id, intent),
  reload: (id) => reloadModule(id),
  login: () => startLogin(),
};

const $ = (id) => document.getElementById(id);
let MODULES = [];
let active = null;
const mounted = new Map();        // id → { root, mod }   (D-H11: кэш модулей)

function icon(name) {
  // Реальные глифы из эталона APE Desktop (standalone): геометрические, не смайлы.
  return { chat: "✦", agents: "⎔", cabinet: "◉", graphlens: "◈", opslens: "◎", connectors: "⛁", security: "⛨", ocr: "◵", ml: "❖", abop: "⬡" }[name] || "◆";
}

// ── вход: единый термин «ABOP» (Keycloak за шлюзом — детали пользователю не нужны) (D-H12) ──────
let _authTimer = null;
function queueAuthRefresh() { clearTimeout(_authTimer); _authTimer = setTimeout(() => renderAuth(), 150); }
let _loginBusy = false;
async function startLogin() {
  if (_loginBusy) return;
  _loginBusy = true; renderAuthBox({ authed: false, busy: true });
  try {
    const res = await api("/api/auth/login", { method: "POST" });
    if (!res || !res.ok) toast("Вход не удался: " + ((res && res.error) || "попробуйте ещё раз"), "danger");
    else toast("Вы вошли в ABOP", "ok");
  } catch (e) { toast("Вход не удался: " + humanError(e), "danger"); }
  _loginBusy = false;
  await renderAuth();
  reloadAll();
}
function renderAuthBox(me) {
  const box = $("authBox"); if (!box) return;
  if (me.authed) {
    const mail = me.email ? `<span title="почта, под которой действуют агенты" style="font-size:11px;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px">✉ ${esc(me.email)}</span>` : "";
    const dept = me.department ? `<span style="font-size:11px;color:var(--accent-ink-2)">· ${esc(me.department)}</span>` : "";
    box.innerHTML = `<span style="display:flex;flex-direction:column;line-height:1.25;min-width:0;max-width:210px">
        <span style="font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">🔑 ${esc(me.name || me.user || "")}${dept}</span>${mail}</span>
      <button class="btn sm" id="logoutBtn" style="flex:none">Выйти</button>`;
    $("logoutBtn").onclick = async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch { /* noop */ } await renderAuth(); reloadAll(); };
  } else if (me.busy) {
    box.innerHTML = `<span style="display:flex;align-items:center;gap:8px">
        <span style="font-size:12px;color:var(--ink-2)">${apeMascot("thinking", 18)}</span>
        <span style="font-size:12px;color:var(--ink-2)">Вход в браузере… завершите там</span>
        <button class="btn sm" id="loginCancel" title="Убрать ожидание (окно браузера можно закрыть)">Отмена</button></span>`;
    $("loginCancel").onclick = () => { _loginBusy = false; renderAuthBox({ authed: false }); };
  } else {
    box.innerHTML = `<span style="display:flex;flex-direction:column;align-items:flex-end;gap:3px">
        <button class="btn primary sm" id="loginBtn">🔑 Войти в ABOP</button>
        <span style="font-size:11px;color:var(--ink-3)">одна учётная запись: почта и доступы</span></span>`;
    $("loginBtn").onclick = startLogin;
  }
}
async function renderAuth() {
  let me = { authed: false };
  try { me = await api("/api/auth/me"); } catch { me = { authed: false }; }
  const was = ctx.authed;
  ctx.user = me.user || null; ctx.roles = me.roles || []; ctx.email = me.email || null; ctx.authed = !!me.authed;
  if (_loginBusy && !me.authed) return;   // ждём завершения входа — не мигаем кнопкой
  renderAuthBox(me);
  refreshCost();
  if (was !== ctx.authed) { renderNav(); renderQuick(); }
}

// RBAC-видимость модулей: admin/advanced — только lecturer/admin (реальные роли из JWT).
// Клиент фильтрует каталог для UX; фактический энфорс — на шлюзе.
const MODULE_ROLES = { security: ["lecturer", "admin"], graphlens: ["lecturer", "admin"], opslens: ["lecturer", "admin"] };
function canSee(id) { const req = MODULE_ROLES[id]; if (!req) return true; return (ctx.roles || []).some((r) => req.includes(r)); }
// «Операции» (opslens) убраны как лишние (#10); «Безопасность» слита в «Кабинет» (#11).
const HIDDEN_MODULES = new Set(["opslens", "security"]);
function visibleModules() { return MODULES.filter((m) => !HIDDEN_MODULES.has(m.id) && canSee(m.id)); }
const RAIL_ORDER = ["chat", "connectors", "agents", "graphlens", "cabinet", "ocr"];
const RAIL_TITLE = { agents: "Мои агенты" };
function railModules() {
  const rank = (id) => { const i = RAIL_ORDER.indexOf(id); return i < 0 ? RAIL_ORDER.length : i; };
  return visibleModules().slice().sort((a, b) => rank(a.id) - rank(b.id));
}
function railBtn(m, on) {
  const bg = on ? "var(--accent-bg)" : "var(--panel)", fg = on ? "var(--accent-ink)" : "var(--ink-2)", bd = on ? "var(--line-2)" : "var(--line)";
  const label = RAIL_TITLE[m.id] || m.title;
  return `<button data-id="${esc(m.id)}" aria-label="${esc(label)}" aria-current="${on ? "page" : "false"}" style="width:100%;padding:10px 4px;display:flex;flex-direction:column;align-items:center;gap:5px;border:1px solid ${bd};border-radius:12px;background:${bg};color:${fg}">
    <span style="font-size:16px;line-height:1" aria-hidden="true">${icon(m.icon)}</span><span style="font-size:11px;font-weight:600">${esc(label)}</span></button>`;
}
function renderNav() {
  const nav = $("railNav"); if (!nav) return;
  nav.innerHTML = railModules().map((m) => railBtn(m, m.id === active)).join("");
  nav.querySelectorAll("[data-id]").forEach((b) => { b.onclick = () => loadModule(b.dataset.id); });
}

// Быстрые функции в рейле (настраиваемые, persist localStorage) — из макета.
const QF_DEFAULT = ["new"];
function quickActions() {
  const a = [
    { id: "new", label: "Новый чат", icon: "➕", run: () => loadModule("chat", { newChat: true }) },
    { id: "palette", label: "Команды", icon: "⌘", run: openPalette },
    { id: "theme", label: "Тема", icon: "🌓", run: toggleTheme },
  ];
  visibleModules().forEach((m) => a.push({ id: "mod:" + m.id, label: m.title, icon: icon(m.icon), run: () => loadModule(m.id) }));
  return a;
}
function getPins() { try { return JSON.parse(localStorage.getItem("ape_quickfns")) || QF_DEFAULT; } catch { return QF_DEFAULT; } }
function slotBtn(inner, attrs, title, dashed) {
  const b = dashed ? "dashed var(--line-2)" : "solid var(--line)";
  return `<button ${attrs} title="${esc(title)}" aria-label="${esc(title)}" style="width:44px;height:44px;display:flex;align-items:center;justify-content:center;border:1px ${b};border-radius:12px;background:var(--panel);color:var(--ink-2);font-size:15px">${inner}</button>`;
}
function renderQuick() {
  const el = $("railQuick"); if (!el) return;
  const acts = quickActions(), pins = getPins();
  el.innerHTML = pins.map((id) => { const a = acts.find((x) => x.id === id); return a ? slotBtn(a.icon, `data-id="${esc(a.id)}"`, a.label) : ""; }).join("")
    + slotBtn("＋", `id="qfAdd"`, "Настроить быстрые функции", true);
  el.querySelectorAll("[data-id]").forEach((b) => b.onclick = () => { const a = acts.find((x) => x.id === b.dataset.id); if (a) a.run(); });
  $("qfAdd").onclick = openQuickManage;
}
function openQuickManage() {
  const acts = quickActions(), pins = getPins();
  modal("Быстрые функции в меню",
    acts.map((a) => `<label style="display:flex;gap:8px;align-items:center;padding:7px 0;font-size:13px;cursor:pointer"><input type="checkbox" class="qfc" value="${esc(a.id)}" ${pins.includes(a.id) ? "checked" : ""}/> ${a.icon} ${esc(a.label)}</label>`).join(""),
    (b) => { localStorage.setItem("ape_quickfns", JSON.stringify([...b.querySelectorAll(".qfc:checked")].map((x) => x.value))); renderQuick(); }, "Сохранить", { width: "420px" });
}

// ── модули: кэш вместо пересоздания (D-H11), намерения при переходе (D-H6/D-H8) ─────────────────
async function loadModule(id, intent) {
  const m = MODULES.find((x) => x.id === id);
  if (!m) { toast("Модуль «" + id + "» недоступен", "warn"); return; }
  active = id; renderNav();
  const panel = $("panel");
  mounted.forEach((rec, k) => { rec.root.hidden = k !== id; });
  let rec = mounted.get(id);
  if (!rec) {
    const root = document.createElement("div");
    root.dataset.module = id;
    root.innerHTML = `<div style="flex:1;padding:24px;display:flex;flex-direction:column;gap:12px;max-width:760px"><div class="skeleton" style="height:28px;width:40%"></div><div class="skeleton" style="height:14px;width:70%"></div><div class="skeleton" style="height:120px"></div></div>`;
    panel.appendChild(root);
    rec = { root, mod: null, intent: intent || null };
    mounted.set(id, rec);
    try {
      const mod = await import(`../modules/${m.ui}/panel.js`);
      root.innerHTML = "";
      rec.mod = mod;
      ctx.takeIntent = () => { const i = rec.intent; rec.intent = null; return i; };
      await mod.mount(root, ctx);
    } catch (e) {
      mounted.delete(id);
      root.innerHTML = `<div style="padding:24px;display:flex;flex-direction:column;gap:10px;max-width:520px"><div style="font-weight:700">Модуль «${esc(m.title)}» не загрузился</div><div class="danger-ink" style="font-size:12.5px">${esc(humanError(e))}</div><button class="btn" id="modRetry" style="align-self:flex-start">Повторить</button></div>`;
      root.querySelector("#modRetry").onclick = () => { root.remove(); loadModule(id, intent); };
      console.error(e);
    }
    return;
  }
  if (intent) rec.root.dispatchEvent(new CustomEvent("ape:intent", { detail: intent }));
}
function reloadModule(id) { const rec = mounted.get(id); if (rec) { rec.root.remove(); mounted.delete(id); } if (active === id) loadModule(id); }
function reloadAll() { const cur = active; mounted.forEach((rec) => rec.root.remove()); mounted.clear(); if (cur) loadModule(cur); }

function renderUpdate(s) {
  const bar = $("updateBar"); if (!bar) return;
  const show = (html, bg) => {
    bar.style.display = "flex";
    bar.style.cssText += ";align-items:center;gap:12px;padding:8px 16px;border-bottom:1px solid var(--line);font-size:13px;" + (bg ? "background:var(--accent-bg)" : "background:var(--panel)");
    bar.innerHTML = html;
  };
  if (s.state === "downloading") show(`⬇ Обновление ${s.version ? "v" + esc(s.version) : ""} — скачивается ${s.percent || 0}%`);
  else if (s.state === "available") show(`⬇ Найдено обновление v${esc(s.version)} — скачиваю…`);
  else if (s.state === "ready") {
    show(`<span style="flex:1">✓ Обновление <b>v${esc(s.version)}</b> готово к установке</span><button class="btn primary sm" id="updNow">Перезапустить и обновить</button>`, true);
    const b = $("updNow"); if (b) b.onclick = () => window.ape.updater.install();
  } else if (s.state === "error") show(`<span class="faint">Апдейтер: ${esc((s.message || "").slice(0, 120))}</span>`);
  else bar.style.display = "none";
}

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const b = $("themeBtn");
  if (b) { b.textContent = t === "light" ? "☀️" : "🌙"; b.setAttribute("aria-label", t === "light" ? "Тема: светлая. Переключить на тёмную" : "Тема: тёмная. Переключить на светлую"); }
}
function toggleTheme() { const t = (localStorage.getItem("ape_theme") || "dark") === "dark" ? "light" : "dark"; localStorage.setItem("ape_theme", t); applyTheme(t); }
function initTheme() { applyTheme(localStorage.getItem("ape_theme") || "dark"); const b = $("themeBtn"); if (b) b.onclick = toggleTheme; }

// ── командная палитра (Ctrl+K) — из макета «Поиск и команды» ────────────────────────────────────
function buildCommands() {
  const cmds = visibleModules().map((m) => ({ glyph: icon(m.icon), label: "Открыть: " + (RAIL_TITLE[m.id] || m.title), note: "раздел", run: () => loadModule(m.id) }));
  cmds.push({ glyph: "➕", label: "Новый чат", note: "чат", keys: "Ctrl N", run: () => loadModule("chat", { newChat: true }) });
  cmds.push({ glyph: "🌓", label: "Переключить тему", note: "светлая / тёмная", run: toggleTheme });
  if (!ctx.authed) cmds.push({ glyph: "🔑", label: "Войти в ABOP", note: "вход", run: startLogin });
  if (window.ape && window.ape.updater) cmds.push({ glyph: "⬇", label: "Проверить обновления", note: "апдейтер", run: () => window.ape.updater.check() });
  return cmds;
}
function openPalette() {
  if (document.getElementById("cmdPalette")) return;
  const all = buildCommands();
  const ov = openOverlay({
    label: "Поиск и команды",
    html: `<div id="cmdPalette" style="position:absolute;top:14vh;width:600px;max-width:92vw;border-radius:16px;background:var(--panel-2);border:1px solid var(--line-2);box-shadow:var(--shadow-2);backdrop-filter:var(--blur-strong);overflow:hidden;animation:ape-drop .28s ease-out">
      <div style="display:flex;align-items:center;gap:12px;padding:14px 16px;border-bottom:1px solid var(--line)">${apeMascot("idle", 24)}
        <input id="cmdInp" placeholder="Поиск и команды…" autocomplete="off" aria-label="Поиск команд" style="flex:1;min-width:0;padding:8px 0;border:none;background:transparent;color:var(--ink);font-size:15px;outline:none;box-shadow:none"/>
        <span style="font-family:var(--mono);font-size:11px;color:var(--ink-3)">Esc</span></div>
      <div id="cmdList" role="listbox" style="max-height:46vh;overflow-y:auto;padding:8px"></div></div>`,
  });
  ov.style.alignItems = "flex-start";
  const pal = ov.querySelector("#cmdPalette"); if (document.documentElement.dataset.theme !== "light") pal.style.background = "rgba(15,23,42,.94)";
  let items = all, sel = 0;
  const list = ov.querySelector("#cmdList");
  const runSel = () => { const c = items[sel]; ov.close(); if (c) c.run(); };
  const paint = () => {
    list.innerHTML = items.map((c, i) => `<button class="cmd" role="option" aria-selected="${i === sel}" data-i="${i}" style="width:100%;display:flex;align-items:center;gap:12px;padding:11px 12px;border:none;border-radius:10px;background:${i === sel ? "var(--hover)" : "transparent"};text-align:left;cursor:pointer;color:inherit">
      <span style="width:26px;height:26px;flex:none;border-radius:8px;background:var(--hover);display:flex;align-items:center;justify-content:center;font-size:13px">${c.glyph || "▸"}</span>
      <span style="flex:1;display:flex;flex-direction:column;gap:2px"><span style="font-size:13px;font-weight:600;color:var(--ink)">${esc(c.label)}</span>${c.note ? `<span style="font-size:11px;color:var(--ink-3)">${esc(c.note)}</span>` : ""}</span>
      ${c.keys ? `<span style="font-family:var(--mono);font-size:11px;color:var(--ink-3)">${esc(c.keys)}</span>` : ""}</button>`).join("")
      || `<div style="padding:26px;text-align:center;font-size:12.5px;color:var(--ink-3)">Ничего не нашлось.</div>`;
    list.querySelectorAll(".cmd").forEach((e) => { e.onmouseenter = () => { sel = +e.dataset.i; paint(); }; e.onclick = runSel; });
  };
  const inp = ov.querySelector("#cmdInp");
  inp.oninput = () => { const q = inp.value.toLowerCase(); items = all.filter((c) => c.label.toLowerCase().includes(q)); sel = 0; paint(); };
  ov.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { sel = Math.min(sel + 1, items.length - 1); paint(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); paint(); e.preventDefault(); }
    else if (e.key === "Enter") { e.preventDefault(); runSel(); }
  });
  paint(); inp.focus();
}

// ── расход в шапке: реальный биллинг из RunMetrics (тот же источник, что чип в чате) ─────────────
async function refreshCost() {
  try {
    const b = await api("/api/modules/cabinet/billing");
    const cv = $("costVal"), qv = $("quotaVal");
    if (b && !b.error && b.tokens != null) {
      const fmt = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n));
      if (cv) cv.textContent = (b.spent_rub ? (Math.round(Number(b.spent_rub) * 100) / 100) + " ₽" : "≈0 ₽");
      if (qv) {
        const pct = Math.round(b.tokens_pct || 0);
        qv.textContent = "токены " + fmt(b.tokens) + " / " + fmt(b.token_quota);
        qv.title = "остаток " + fmt(b.tokens_remaining) + " · " + (100 - pct) + "% квоты";
        qv.style.color = pct >= 90 ? "var(--danger-ink)" : pct >= 70 ? "var(--warn-ink)" : "var(--ink-2)";
      }
    } else { if (cv) cv.textContent = "— ₽"; if (qv) { qv.textContent = ctx.authed ? "квота —" : "войдите"; qv.style.color = "var(--ink-3)"; } }
  } catch { /* шапка не критична */ }
}
window.__apeRefreshCost = refreshCost;   // модули дёргают после платных прогонов (два счётчика — один источник)

// ── загрузка: движок может подниматься дольше окна → экран с ретраем, а не «Проверь сайдкар» (D-H2) ──
async function waitEngine() {
  const panel = $("panel");
  for (let i = 0; ; i++) {
    try { return await api("/api/health"); }
    catch {
      if (i === 0) panel.innerHTML = `<div style="flex:1;display:flex;align-items:center;justify-content:center"><div style="display:flex;flex-direction:column;align-items:center;gap:14px;text-align:center;max-width:380px;animation:ape-in .3s ease">
        ${apeMascot("thinking", 64)}<div style="font-size:17px;font-weight:700">Запускаю движок…</div>
        <div id="engNote" style="font-size:12.5px;color:var(--ink-2);line-height:1.5">Локальный сервис ABOP Desktop поднимается. Обычно это несколько секунд.</div>
        <button class="btn" id="engRetry" style="display:none">Повторить</button></div></div>`;
      if (i >= 12) { const n = $("engNote"), b = $("engRetry"); if (n) n.textContent = "Движок не отвечает. Перезапустите приложение или нажмите «Повторить»."; if (b) { b.style.display = ""; await new Promise((r) => { b.onclick = r; }); i = 0; continue; } }
      await new Promise((r) => setTimeout(r, 1200));
    }
  }
}

async function boot() {
  const lg = $("apeLogo"); if (lg) lg.innerHTML = apeLogo(30);
  initTheme();
  const h = await waitEngine();
  const v = $("verChip"); if (v) v.textContent = "v" + ((h && h.version) || "?");
  if (window.ape && window.ape.updater) window.ape.updater.onStatus(renderUpdate);
  // глобальный хоткей: выделенный текст из Word/Excel/браузера → открыть чат и отправить на анализ
  if (window.ape && window.ape.onAnalyze) window.ape.onAnalyze((text) => loadModule("chat", { analyze: text || "" }));
  await renderAuth();
  try { MODULES = await api("/api/modules"); } catch { MODULES = []; }
  renderNav(); renderQuick();
  $("panel").innerHTML = "";
  if (MODULES.length) loadModule(MODULES.find((m) => m.id === "chat") ? "chat" : MODULES[0].id);
  else $("panel").innerHTML = `<div style="padding:24px;display:flex;flex-direction:column;gap:10px"><div style="font-weight:700">Модули не найдены</div><div class="faint" style="font-size:12.5px">Движок запущен, но не отдал список разделов. Перезапустите приложение.</div></div>`;
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && (e.key === "k" || e.key === "K")) { e.preventDefault(); openPalette(); }
    else if (e.ctrlKey && (e.key === "n" || e.key === "N")) { e.preventDefault(); loadModule("chat", { newChat: true }); }
  });
  const pb = $("cmdBtn"); if (pb) pb.onclick = openPalette;
}

boot();
