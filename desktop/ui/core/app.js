// Ядро фронта: загрузчик модулей + маршрутизация + API-клиент к сайдкару + общие примитивы UI.
// Примитивы карточки живут отдельным модулем (`core/cards.js`): там они чистые функции,
// и правило числа проверяется их ВЫЗОВОМ, а не поиском имени переменной в этом файле.
import { card, tile, origin, esc } from "./cards.js";
export { card, tile, origin, esc };
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
    const _off = new ApiError(0, "движок недоступен", null);
    try { markOffline(_off); } catch { /* индикатор не критичен */ }
    throw _off;
  }
  const txt = await r.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch { data = null; }
  if (!r.ok) {
    const detail = (data && (data.error || data.detail || data.message)) || txt.slice(0, 200) || r.statusText;
    const st = (data && Number(data.status)) || r.status;
    if (st === 401) queueAuthRefresh();
    const _err = new ApiError(st, String(detail), data);
    if (st === 502 || st === 503 || st === 504) { try { markOffline(_err); } catch { /* индикатор не критичен */ } }
    throw _err;
  }
  if (!linkOk) { try { recheckLink(); } catch { /* индикатор не критичен */ } }
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
  // Enter подтверждает В ЛЮБОМ диалоге. Раньше это зависело от того, повесил ли автор конкретного
  // окна свой обработчик: в одном Enter подтверждал, в соседнем не делал ничего.
  // Исключения осознанные: в многострочном поле Enter — перенос строки, на кнопке и в списке его
  // обрабатывает сам элемент, а Ctrl/Cmd+Enter подтверждает отовсюду, включая поле ввода текста.
  if (onOk) ov.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing) return;
    const t = e.target, tag = (t && t.tagName) || "";
    const hard = e.ctrlKey || e.metaKey;
    if (!hard && (tag === "TEXTAREA" || tag === "BUTTON" || tag === "SELECT" || (t && t.isContentEditable))) return;
    const ok = ov.querySelector("#mOk");
    if (!ok || ok.disabled) return;
    e.preventDefault(); ok.click();
  });
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
// Файл сохранён — сразу предлагаем открыть его или показать в папке. Раньше человеку доставался
// только путь в уведомлении, а открыть файл из приложения было нечем.
export function fileToast(msg, path) {
  const name = String(path || "").split(/[\/]/).pop();
  if (window.ape && window.ape.openFile) {
    toast(msg + (name ? ": " + name : ""), "ok", { ttl: 9000, action: { label: "Открыть", run: async () => {
      const r = await window.ape.openFile(path);
      if (!r || !r.ok) { if (window.ape.revealFile) window.ape.revealFile(path); toast("Не удалось открыть файл — показали папку", "warn"); }
    } } });
  } else {
    toast(msg + (path ? ": " + path : ""), "ok", { ttl: 9000 });
  }
}

// Копирование отвечает ОДИНАКОВО везде: отметка прямо на нажатой кнопке (она рядом с тем, что
// скопировано) плюс объявление для экранного диктора. Всплывающее уведомление для этого не нужно —
// раньше половина мест отвечала им, и одно и то же действие выглядело как два разных.
// Отказ буфера (нет разрешения, пустой источник) теперь виден: прежде кнопка врала «скопировано».
export async function copyText(text, btn, what) {
  const v = String(text == null ? "" : text);
  // Три пути по убыванию надёжности. `navigator.clipboard` зависит от разрешений и фокуса документа
  // и отказывает МОЛЧА — тогда кнопка говорила «скопировано», а вставлять было нечего. Путь через
  // main (модуль clipboard Electron) таких условий не имеет; execCommand остаётся для веб-версии.
  let ok = false;
  if (window.ape && window.ape.clipboard) {
    try { ok = !!(await window.ape.clipboard.writeText(v)); } catch { ok = false; }
  }
  if (!ok) { try { await navigator.clipboard.writeText(v); ok = true; } catch { ok = false; } }
  if (!ok) {
    try {
      const ta = document.createElement("textarea");
      ta.value = v; ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;left:-9999px;top:0";
      document.body.appendChild(ta); ta.select();
      ok = document.execCommand("copy");
      ta.remove();
    } catch { ok = false; }
  }
  if (btn) {
    const was = btn.dataset.copyWas || btn.textContent;
    btn.dataset.copyWas = was;
    btn.textContent = ok ? "✓ скопировано" : "✕ не удалось";
    setTimeout(() => { btn.textContent = btn.dataset.copyWas || was; delete btn.dataset.copyWas; }, 1400);
  }
  say(ok ? `Скопировано${what ? ": " + what : ""}` : "Скопировать не удалось: буфер обмена недоступен");
  return ok;
}

// Одна область объявлений на всё приложение: короткие сообщения о том, что произошло, для тех,
// кто работает с экранным диктором.
export function say(msg) {
  let el = document.getElementById("apeSay");
  if (!el) {
    el = document.createElement("div"); el.id = "apeSay"; el.className = "sr-only";
    el.setAttribute("role", "status"); el.setAttribute("aria-live", "polite");
    document.body.appendChild(el);
  }
  el.textContent = "";
  setTimeout(() => { el.textContent = String(msg || ""); }, 30);
}

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
// Один санитайзер на приложение. Их было два: строгий в чате и слабее в прогонах — последний
// пропускал обработчики в одинарных кавычках (onclick='…'). Один и тот же HTML отчёта с сервера
// очищался по-разному, и слабый рано или поздно стал бы дырой.
export function sanitizeHtml(html) {
  return String(html || "")
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<iframe[\s\S]*?<\/iframe>/gi, "")
    .replace(/\son\w+\s*=\s*"[^"]*"/gi, "")
    .replace(/\son\w+\s*=\s*'[^']*'/gi, "")
    .replace(/\son\w+\s*=\s*[^\s>]+/gi, "")
    .replace(/javascript:/gi, "");
}

export const ctx = {
  api, base: API, user: null, roles: [], email: null, authed: false,
  card, tile, origin,
  // Чьи данные на экране. Считает СЕРВЕР (`tenant_active`), десктоп только показывает: правило
  // вычисления арендатора живёт на сервере, и местная копия разошлась бы с ним молча.
  tenant: "", tenantOwn: "", tenantKnown: false, tenantCanSwitch: false,
  gate: apeGate, mascot: apeMascot, modal, confirm: confirmDialog, toast, humanError, esc, openOverlay,
  copy: copyText, say, sanitizeHtml,
  // навигация с намерением: ctx.open("chat", { attach: {...} }) — модуль получает intent (см. loadModule)
  open: (id, intent) => loadModule(id, intent),
  fileToast,
  setHitlCount,
  // Уведомление ОС: прогон идёт минуты, и окно за это время закрывают. Решает КОГДА извещать
  // модуль — он знает, что произошло; main только показывает.
  notify: извещение,
  reload: (id) => reloadModule(id),
  login: () => startLogin(),
};

const $ = (id) => document.getElementById(id);
let MODULES = [];
let active = null;
const mounted = new Map();        // id → { root, mod }   (D-H11: кэш модулей)

function icon(name) {
  // Реальные глифы из эталона APE Desktop (standalone): геометрические, не смайлы.
  return { chat: "✦", runs: "◷", findings: "◬", agents: "⎔", cabinet: "◉", graphlens: "◈", opslens: "◎", connectors: "⛁", security: "⛨", ocr: "◵", watch: "▤", ml: "❖", abop: "⬡", decisions: "⌦", control: "◰" }[name] || "◆";
}

// ── вход: единый термин «ABOP» (Keycloak за шлюзом — детали пользователю не нужны) (D-H12) ──────
let _authTimer = null;
function queueAuthRefresh() { clearTimeout(_authTimer); _authTimer = setTimeout(() => renderAuth(), 150); }
let _loginBusy = false;
async function startLogin(запомнить) {
  if (_loginBusy) return;
  // По умолчанию запоминаем: обычная сессия живёт сутки, и утром приложение встречало человека
  // просьбой войти заново — ровно то, что выглядело как «токен не приходит».
  const помнить = запомнить === undefined ? true : !!запомнить;
  _loginBusy = true; renderAuthBox({ authed: false, busy: true });
  try {
    const res = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ "запомнить": помнить }) });
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
    // Учётная запись занимала треть шапки: имя, почта и «Выйти» в строку. Теперь одна плашка с
    // инициалами, а имя, почта, отдел и выход — по нажатию: шапка про состояние работы, а не про то,
    // кто вошёл. Почта остаётся видимой в подсказке — под ней действуют агенты.
    const nm = String(me.name || me.user || "").trim();
    const ini = (nm.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("") || "·").toUpperCase();
    box.innerHTML = `<button id="meBtn" title="${esc(nm + (me.email ? " · " + me.email : ""))}" aria-label="Учётная запись: ${esc(nm)}"
        style="display:flex;align-items:center;gap:9px;padding:4px 11px 4px 4px;border:1px solid var(--line);border-radius:9999px;background-color:var(--panel);background-image:var(--panel-grad);box-shadow:var(--edge);color:var(--ink-2)">
        <span aria-hidden="true" style="width:28px;height:28px;flex:none;border-radius:9999px;background-image:var(--grad);color:#fff;font-family:var(--mono);font-size:11.5px;font-weight:700;display:flex;align-items:center;justify-content:center;box-shadow:var(--edge-strong)">${esc(ini)}</span>
        <span style="font-size:12.5px;font-weight:600;color:var(--ink);max-width:132px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(nm)}</span></button>`;
    $("meBtn").onclick = () => {
      const rows = [["имя", nm], ["почта", me.email || "—"], ["отдел", me.department || "—"],
                    ["сессия", me["запомнили"] ? "запомнена (действует до выхода)" : "до суток"],
                    ["роль", (ctx.roles || []).join(", ") || ctx.level || "—"],
                    ["арендатор", ctx.tenantKnown
                      ? (ctx.tenant + (ctx.tenant !== ctx.tenantOwn ? " (данные клиента)" : ""))
                      : "неизвестно — нет связи"]];
      const ov = modal("Учётная запись",
        `<div style="display:flex;flex-direction:column;gap:8px">${rows.map(([k, v]) => `<div style="display:flex;gap:10px;align-items:baseline">
          <span class="ape-label" style="min-width:52px">${k}</span><span style="font-size:13px;color:var(--ink);word-break:break-all">${esc(String(v))}</span></div>`).join("")}
          <div class="dcard-note">Под этой почтой агенты отправляют письма и заводят задачи — проверяйте её перед внешними действиями.${
            me["запомнили"] ? " Сессия запомнена: на этом компьютере лежит действующий ключ доступа — выходите, если оставляете его другим." : ""}</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px">
            <button class="btn danger" id="logoutBtn">Выйти из ABOP</button>
            <button class="btn sm" id="tenantBtn">Чьи данные на экране</button>
          </div></div>`,
        null, "Закрыть", { width: "420px" });
      ov.querySelector("#tenantBtn").onclick = () => { ov.close(true); tenantDetails(); };
      ov.querySelector("#logoutBtn").onclick = async () => {
        // «Вышел» не должно означать «забыл локально»: с запомненной сессией не отозванный токен
        // остаётся действующим ключом, и об этом человеку надо сказать, а не промолчать.
        let итог = null;
        try { итог = await api("/api/auth/logout", { method: "POST" }); } catch { /* noop */ }
        if (итог && итог["отозван"] === false) {
          toast("Вы вышли на этом компьютере, но сессию на сервере закрыть не удалось"
            + (итог["почему"] ? " (" + итог["почему"] + ")" : "") + " — закройте её в браузере", "warn");
        }
        ov.close(true); await renderAuth(); reloadAll();
      };
    };
  } else if (me.busy) {
    box.innerHTML = `<span style="display:flex;align-items:center;gap:8px">
        <span style="font-size:12px;color:var(--ink-2)">${apeMascot("thinking", 18)}</span>
        <span style="font-size:12px;color:var(--ink-2)">Вход в браузере… завершите там</span>
        <button class="btn sm" id="loginCancel" title="Убрать ожидание (окно браузера можно закрыть)">Отмена</button></span>`;
    $("loginCancel").onclick = () => { _loginBusy = false; renderAuthBox({ authed: false }); };
  } else {
    // Причина, по которой вход нужен снова, важнее общей подписи: «сессия истекла» и «нет связи
    // с сервером входа» требуют разных действий, а выглядели одинаково — пустым окном с кнопкой.
    const почему = String(me["почему"] || "");
    const подпись = me["нужен_вход"] && почему ? почему : "одна учётная запись: почта и доступы";
    const цвет = me["нужен_вход"] && почему ? "var(--warn, #fbbf24)" : "var(--ink-3)";
    box.innerHTML = `<span style="display:flex;flex-direction:column;align-items:flex-end;gap:3px">
        <button class="btn primary sm" id="loginBtn">🔑 Войти в ABOP</button>
        <label for="rememberMe" style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--ink-3);cursor:pointer">
          <input type="checkbox" id="rememberMe" checked style="margin:0">Запомнить меня</label>
        <span style="font-size:11px;color:${цвет};max-width:260px;text-align:right">${esc(подпись)}</span></span>`;
    $("loginBtn").onclick = () => startLogin($("rememberMe") ? $("rememberMe").checked : true);
  }
}
async function renderAuth() {
  let me = { authed: false };
  try { me = await api("/api/auth/me"); } catch { me = { authed: false }; }
  const was = ctx.authed;
  ctx.user = me.user || null; ctx.roles = me.roles || []; ctx.email = me.email || null; ctx.authed = !!me.authed;
  ctx.level = me.level || "analyst"; ctx.canApprove = me.can_approve !== false;
  ctx.tenant = me.tenant || ""; ctx.tenantOwn = me.tenant_own || "";
  ctx.tenantKnown = !!me.tenant_known; ctx.tenantCanSwitch = !!me.tenant_can_switch;
  renderTenant();
  if (_loginBusy && !me.authed) return;   // ждём завершения входа — не мигаем кнопкой
  renderAuthBox(me);
  refreshCost();
  if (was !== ctx.authed) { renderNav(); renderQuick(); }
}

// RBAC-видимость модулей по ШКАЛЕ ABOP (analyst < manager < support < admin) — её же считает
// сайдкар в /api/modules/.../me и сервер в require_level. Раньше здесь стояли роли "lecturer" и
// "admin" из чужого realm: роли lecturer в ABOP нет, поэтому «Граф» был скрыт от manager и
// support молча — человек уровня «менеджер» не видел раздела и не получал объяснения.
const LEVELS = ["analyst", "manager", "support", "admin"];
// Лента контроля — сводка по ЛЮДЯМ отдела: аналитику её показывать незачем, у него и своих решений
// хватает. Это не «режим руководителя»: раздел просто не виден тем, кому он не адресован.
const MODULE_MIN_LEVEL = { graphlens: "manager", control: "manager", security: "support", opslens: "support" };
function levelAtLeast(min) {
  const have = LEVELS.indexOf(ctx.level || "analyst");
  const need = LEVELS.indexOf(min);
  return need < 0 || (have >= 0 && have >= need);
}
function canSee(id) { const min = MODULE_MIN_LEVEL[id]; return !min || levelAtLeast(min); }
// «Операции» (opslens) убраны как лишние (#10); «Безопасность» слита в «Кабинет» (#11).
// «Находки» и «Источники» убраны из маршрута по решению владельца 05.10: находки видны в карточке
// прогона и в отчёте, а подключение источников — работа по управлению, её место в вебе (там же
// создаются агенты и правятся рецепты). Разделы не удалены: они открываются из палитры команд
// (Ctrl+K) и по прямой ссылке — убрана только постоянная кнопка, которой никто не пользовался.
const HIDDEN_MODULES = new Set(["opslens", "security", "findings", "connectors", "ocr"]);
function visibleModules() { return MODULES.filter((m) => !HIDDEN_MODULES.has(m.id) && canSee(m.id)); }
// «Решения» стоят вторыми: там ждут человека, и это важнее журнала прогонов. Переключателя
// режимов нет (DIZAJN, 5.1) — руководитель тоже ставит задачи, и прыгать между двумя
// приложениями ради одного подтверждения он не должен.
const RAIL_ORDER = ["chat", "decisions", "control", "runs", "agents", "watch", "graphlens", "cabinet"];
const RAIL_TITLE = { agents: "Мои агенты" };
function railModules() {
  const rank = (id) => { const i = RAIL_ORDER.indexOf(id); return i < 0 ? RAIL_ORDER.length : i; };
  return visibleModules().slice().sort((a, b) => rank(a.id) - rank(b.id));
}
// Сколько действий ждёт решения — показываем на кнопке чата в боковом меню: очередь жила только
// внутри чата, и из «Прогонов» или «Кабинета» о висящих письмах и задачах было не узнать.
let hitlCount = 0;
// ── строка живой работы ─────────────────────────────────────────────────────────────────────
// «что идёт, где оно и сколько уже стоило» — одной строкой и из любого раздела. Опрос идёт, только
// пока что-то действительно идёт: пустой опрос раз в пять секунд — это батарея ноутбука за то, что
// никому не нужно. Поэтому пауза растёт, когда живого нет.
let _живоеTimer = null;
let _живоеПусто = 0;
function живаяСтрокаHTML(список) {
  if (!список.length) return "";
  const r = список[0];
  const ещё = список.length > 1 ? ` · и ещё ${список.length - 1}` : "";
  const сек = r["начато"] ? Math.max(0, Math.round((Date.now() - Date.parse(r["начато"])) / 1000)) : 0;
  const длит = длительность(сек);
  const знак = r.status === "running" ? "●" : (r.status === "awaiting_hitl" ? "◐" : "○");
  const куда = r.status === "awaiting_hitl" ? "decisions" : "runs";
  return `<div id="liveWork" style="display:flex;gap:10px;align-items:center;padding:6px 12px;border-bottom:1px solid var(--line);font-size:12px">
    <span style="color:var(--ok-ink)">${знак}</span>
    <b style="font-weight:650">${esc(r["что"])}</b>
    <span style="color:var(--ink-2)">${esc(r.agent_id || "агент")}</span>
    ${r["шаг"] ? `<span style="color:var(--ink-3)">${esc(r["шаг"])}</span>` : ""}
    ${длит ? `<span style="font-family:var(--mono);color:var(--ink-3)">${esc(длит)}</span>` : ""}
    ${r["место"] ? `<span style="color:var(--ink-3)">в очереди ${esc(r["место"])}</span>` : ""}
    <span style="color:var(--ink-3)">${esc(ещё)}</span>
    <button id="liveOpen" data-go="${esc(куда)}" style="margin-left:auto;padding:3px 10px;border-radius:8px;border:1px solid var(--line);background:var(--panel-2);color:var(--ink);font-size:11.5px;cursor:pointer">Открыть</button>
  </div>`;
}
function длительность(с) {
  if (!с) return "";
  if (с < 60) return с + " с";
  const м = Math.floor(с / 60), о = с % 60;
  return о ? `${м} м ${String(о).padStart(2, "0")} с` : `${м} м`;
}
async function tickЖивое() {
  const узел = $("liveWorkSlot");
  if (!узел) return;
  let список = [];
  try { список = ((await api("/api/modules/runs/live")) || {})["живое"] || []; }
  catch { список = []; }            // движок недоступен — строки просто нет, это не ошибка
  узел.innerHTML = живаяСтрокаHTML(список);
  const b = $("liveOpen");
  if (b) b.onclick = () => loadModule(b.dataset.go);
  _живоеПусто = список.length ? 0 : Math.min(_живоеПусто + 1, 6);
  clearTimeout(_живоеTimer);
  _живоеTimer = setTimeout(tickЖивое, список.length ? 5000 : 5000 + _живоеПусто * 5000);
}
export function startЖивое() { clearTimeout(_живоеTimer); tickЖивое(); }

export function setHitlCount(n) {
  const v = Math.max(0, Number(n) || 0);
  if (v === hitlCount) return;
  const было = hitlCount;
  hitlCount = v;
  renderNav();
  // Счётчик в трее — всегда, уведомление — только на ПРИБАВЛЕНИЕ. Иначе каждый опрос очереди
  // превращался бы в всплывающее окно, а это верный способ научить человека их закрывать не читая.
  try { window.ape?.os?.badge?.(v); } catch (e) { /* не в electron — ничего страшного */ }
  if (v > было) {
    извещение({
      title: v === 1 ? "Действие ждёт вашего решения" : `Ждут решения: ${v}`,
      body: "Агент подготовил внешнее действие и остановился до вашего «да».",
      куда: "chat",
    });
  }
}

// Уведомление ОС. Работает только в приложении (в браузере моста нет) и не повторяет одно и то
// же чаще, чем раз в минуту: прогон может доложиться дважды, человек не должен получать дубль.
const _извещено = new Map();
export function извещение({ title, body, куда, ключ }) {
  const мост = window.ape?.os?.notify;
  if (!мост) return false;
  const k = String(ключ || title || "");
  const t = Date.now();
  if (t - (_извещено.get(k) || 0) < 60000) return false;
  _извещено.set(k, t);
  try { мост({ title, body, куда }); return true; } catch (e) { return false; }
}

// Раздел в рейле — строка маршрута, как в вебе: плашка знака, подпись, полоса слева у текущего.
// Форма и цвет живут в теме (.navitem), здесь только данные: иначе вид расходился между рейлом
// десктопа и навигацией веба при каждой правке.
function railBtn(m, on) {
  const label = RAIL_TITLE[m.id] || m.title;
  const badge = (m.id === "chat" && hitlCount)
    ? `<span class="bdg" title="Ждут вашего решения">${hitlCount > 99 ? "99+" : hitlCount}</span>` : "";
  const aria = (m.id === "chat" && hitlCount) ? `${label}, ждут решения: ${hitlCount}` : label;
  return `<button class="navitem${on ? " on" : ""}" data-id="${esc(m.id)}" title="${esc(label)}" aria-label="${esc(aria)}" aria-current="${on ? "page" : "false"}">
    <span class="glyph" aria-hidden="true">${icon(m.icon)}</span><span class="lbl">${esc(label)}</span>${badge}</button>`;
}
function renderNav() {
  const nav = $("railNav"); if (!nav) return;
  nav.innerHTML = railModules().map((m) => railBtn(m, m.id === active)).join("");
  nav.querySelectorAll("[data-id]").forEach((b) => { b.onclick = () => loadModule(b.dataset.id); });
}

// Свёрнутый рейл: место под работу на небольшом экране. Выбор человека переживает перезапуск —
// иначе каждый вход возвращал меню в состояние по умолчанию.
const LS_RAIL = "ape_rail_narrow";
function railNarrow() { try { return localStorage.getItem(LS_RAIL) === "1"; } catch { return false; } }
function applyRail() {
  const aside = $("rail"), btn = $("railToggle");
  if (!aside) return;
  const narrow = railNarrow();
  aside.classList.toggle("rail-narrow", narrow);
  aside.style.width = narrow ? "68px" : "var(--rail-w)";
  aside.style.padding = narrow ? "14px 8px" : "14px 10px 14px 0";
  const q = $("railQuick"); if (q) q.style.padding = narrow ? "0" : "0 0 0 11px";
  if (btn) {
    btn.textContent = narrow ? "»" : "«";
    btn.title = btn.ariaLabel = narrow ? "Развернуть меню разделов" : "Свернуть меню разделов";
    btn.setAttribute("aria-expanded", narrow ? "false" : "true");
  }
  renderQuick();
}
function toggleRail() {
  try { localStorage.setItem(LS_RAIL, railNarrow() ? "0" : "1"); } catch { /* приватное окно */ }
  applyRail();
}

// Быстрые функции в рейле (настраиваемые, persist localStorage) — из макета.
const QF_DEFAULT = ["new"];
function quickActions() {
  const a = [
    { id: "new", label: "Новый чат", icon: "➕", run: () => loadModule("chat", { newChat: true }) },
    { id: "palette", label: "Команды", icon: "⌘", run: openPalette },
    { id: "theme", label: "Тема", icon: "🌓", run: toggleTheme },
  ];
  // В палитре — ВСЕ разделы, включая убранные из маршрута: убрать кнопку не значит отнять раздел.
  MODULES.filter((m) => canSee(m.id)).forEach((m) => a.push({ id: "mod:" + m.id, label: m.title, icon: icon(m.icon), run: () => loadModule(m.id) }));
  return a;
}
function getPins() { try { return JSON.parse(localStorage.getItem("ape_quickfns")) || QF_DEFAULT; } catch { return QF_DEFAULT; } }
function slotBtn(inner, attrs, title, dashed) {
  // Свёрнутый рейл — только плашка знака; развёрнутый — строка с подписью, как у разделов:
  // два разных вида одной кнопки в одном меню выглядели как две разные системы.
  if (railNarrow()) {
    const b = dashed ? "dashed var(--line-2)" : "solid var(--line)";
    return `<button ${attrs} title="${esc(title)}" aria-label="${esc(title)}" style="width:44px;height:44px;display:flex;align-items:center;justify-content:center;border:1px ${b};border-radius:var(--r-md);background:var(--panel);color:var(--ink-2);font-size:15px">${inner}</button>`;
  }
  return `<button class="navitem" ${attrs} title="${esc(title)}" aria-label="${esc(title)}" style="border-radius:var(--r-md);padding:7px 10px 7px 8px">
    <span class="glyph" aria-hidden="true"${dashed ? ' style="border:1px dashed var(--line-2);background:transparent"' : ""}>${inner}</span><span class="lbl">${esc(title)}</span></button>`;
}
function renderQuick() {
  const el = $("railQuick"); if (!el) return;
  const acts = quickActions(), pins = getPins();
  el.innerHTML = pins.map((id) => { const a = acts.find((x) => x.id === id); return a ? slotBtn(a.icon, `data-id="${esc(a.id)}"`, a.label) : ""; }).join("")
    + slotBtn("＋", `id="qfAdd"`, "Настроить", true);
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
      // Ошибка РАЗБОРА файла раздела (SyntaxError) — это не проблема сети, а сломанный код:
      // «ABOP недоступен — проверьте сеть» отправляло чинить не то место.
      const broken = e instanceof SyntaxError || /Invalid or unexpected token|Unexpected (token|identifier|end of input)|failed to fetch dynamically imported module/i.test(String(e && (e.message || e)));
      const why = broken
        ? "Код раздела повреждён: " + esc(String((e && e.message) || e).slice(0, 200)) + ". Сеть тут ни при чём — обновите приложение или сообщите об этом."
        : esc(humanError(e));
      root.innerHTML = `<div style="padding:24px;display:flex;flex-direction:column;gap:10px;max-width:560px"><div style="font-weight:700">Раздел «${esc(m.title)}» не загрузился</div><div class="danger-ink" style="font-size:12.5px;line-height:1.55">${why}</div><button class="btn" id="modRetry" style="align-self:flex-start">Повторить</button></div>`;
      root.querySelector("#modRetry").onclick = () => { root.remove(); loadModule(id, intent); };
      console.error(e);
    }
    return;
  }
  if (intent) rec.root.dispatchEvent(new CustomEvent("ape:intent", { detail: intent }));
}
function reloadModule(id) { const rec = mounted.get(id); if (rec) { rec.root.remove(); mounted.delete(id); } if (active === id) loadModule(id); }
// Мягкое обновление после возврата связи: раздел перечитывает свои данные САМ, оставаясь на месте.
// Пересборка разделов тут не годится — она уничтожает то, чего ещё нет на сервере: незаписанный
// новый чат, набранный текст, выбранный профиль. Один сорвавшийся опрос — и человека выбрасывало
// в последний чат, где шла цепочка.
function relinkModules() { mounted.forEach((rec) => rec.root.dispatchEvent(new CustomEvent("ape:relink"))); }
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
  // Палитра знает про ВСЕ разделы, включая убранные из маршрута («Находки», «Источники»): кнопку
  // убрали, раздел остался — иначе это не уборка интерфейса, а потеря функции.
  const cmds = MODULES.filter((m) => canSee(m.id)).map((m) => ({
    glyph: icon(m.icon), label: "Открыть: " + (RAIL_TITLE[m.id] || m.title),
    note: HIDDEN_MODULES.has(m.id) ? "раздел · не в маршруте" : "раздел",
    run: () => loadModule(m.id) }));
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

// ── состояние связи с ABOP: честная деградация вместо пустых панелей ──────────────────────
// Локальный движок приложения может быть жив, а сервер — нет: тогда история чата и настройки доступны,
// а запуск агентов и очередь подтверждений — нет. Показываем это в шапке, а не в каждой ошибке.
let linkOk = true, linkTimer = null;
let linkState = { phase: "checking", url: "", error: "", at: 0, engine: true };

// Светофор связи: зелёный — ABOP отвечает, жёлтый — идёт проверка, красный — не отвечает.
// Индикатор виден ВСЕГДА: раньше он появлялся только при аварии, и рабочее состояние ничем не
// подтверждалось — было не отличить «всё хорошо» от «проверка зависла».
const LINK_LOOK = {
  ok:       { dot: "var(--ok-ink)",     ink: "var(--ink-2)",      text: "ABOP на связи",      bg: "var(--ok-bg)",     br: "var(--ok-line)" },
  checking: { dot: "var(--warn-ink)",   ink: "var(--ink-2)",      text: "проверяю связь…",    bg: "var(--warn-bg)",   br: "var(--warn-line)" },
  down:     { dot: "var(--danger-ink)", ink: "var(--danger-ink)", text: "Нет связи с ABOP",   bg: "var(--danger-bg)", br: "var(--danger-line)" },
  engine:   { dot: "var(--danger-ink)", ink: "var(--danger-ink)", text: "Движок не отвечает", bg: "var(--danger-bg)", br: "var(--danger-line)" },
};

// Плашка арендатора: видна ТОЛЬКО когда на экране данные не своей организации. Это состояние
// опасно именно незаметностью — поддержка вендора, забывшая вернуться к себе, продолжает работать
// в данных клиента, а по экрану это ничем не отличается от обычной работы.
function renderTenant() {
  const pill = $("tenantPill"), txt = $("tenantText");
  if (!pill) return;
  const чужое = ctx.authed && ctx.tenantKnown && ctx.tenant && ctx.tenant !== ctx.tenantOwn;
  pill.hidden = !чужое;
  if (!чужое) return;
  if (txt) txt.textContent = "данные клиента: " + ctx.tenant;
  pill.title = "Вы смотрите установку клиента «" + ctx.tenant + "», а не свою («" + (ctx.tenantOwn || "—")
    + "»). Переход записан в журнал сервера. Нажмите, чтобы вернуться к своим данным.";
  pill.onclick = () => tenantDetails();
}

// Подробности и переход. Переключатель показываем только поддержке вендора — но отказ всё равно
// придёт с сервера, и показываем мы то, что ПО ЕГО МНЕНИЮ действует, а не то, что попросили.
function tenantDetails() {
  const своё = !ctx.tenant || ctx.tenant === ctx.tenantOwn;
  const rows = [
    ["Арендатор", ctx.tenantKnown ? ctx.tenant : "неизвестно — нет связи с сервером"],
    ["Своя организация", ctx.tenantOwn || "—"],
    ["Установка", linkState.url || "—"],
  ];
  const свитч = ctx.tenantCanSwitch
    ? `<div style="display:flex;flex-direction:column;gap:7px;margin-top:12px;padding-top:11px;border-top:1px solid var(--line)">
         <span class="ape-label">посмотреть установку клиента</span>
         <div style="display:flex;gap:8px;flex-wrap:wrap">
           <input id="tnInp" value="${esc(своё ? "" : ctx.tenant)}" placeholder="имя арендатора"
                  style="flex:1;min-width:160px;padding:8px 10px;border-radius:9px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px"/>
           <button class="btn sm" id="tnGo">Смотреть</button>
           ${своё ? "" : `<button class="btn sm" id="tnBack">Вернуться к своим</button>`}
         </div>
         <div class="dcard-note">Переход записывается в журнал сервера как событие управления. Вложения и переписка клиента остаются в своей области — со своими они не смешаются.</div>
       </div>`
    : `<div class="dcard-note" style="margin-top:12px">Сменить арендатора может только поддержка вендора. Ваши агенты и данные — в организации «${esc(ctx.tenantOwn || "—")}».</div>`;
  const body = `<div style="display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:12.5px;line-height:1.55">`
    + rows.map(([k, v]) => `<div style="color:var(--ink-3)">${esc(k)}</div><div style="color:var(--ink-1);word-break:break-word">${esc(String(v))}</div>`).join("")
    + `</div>` + свитч;
  const ov = modal("Чьи данные на экране", body, null, "Закрыть", { kicker: "Арендатор", width: "460px" });
  const перейти = async (куда) => {
    try {
      const r = await api("/api/auth/tenant", { method: "POST", body: JSON.stringify({ tenant: куда }) });
      ctx.tenant = r.tenant || ""; ctx.tenantOwn = r.tenant_own || ctx.tenantOwn;
      ctx.tenantKnown = !!r.tenant_known;
      renderTenant();
      ov.close(true);
      // Сервер мог переход НЕ пустить. Говорим о том, что действует, а не о том, что просили.
      if (куда && ctx.tenant !== куда) toast("Сервер оставил арендатора «" + (ctx.tenant || "—") + "»: переход не разрешён", "danger");
      else toast(куда ? ("Смотрим данные клиента «" + ctx.tenant + "»") : "Вернулись к своим данным", "ok");
      reloadAll();
    } catch (e) { toast("Переход не удался: " + humanError(e), "danger"); }
  };
  const go = ov.querySelector("#tnGo");
  if (go) go.onclick = () => перейти(String((ov.querySelector("#tnInp") || {}).value || "").trim());
  const back = ov.querySelector("#tnBack");
  if (back) back.onclick = () => перейти("");
}

function renderLink(state) {
  const pill = $("linkPill"), txt = $("linkText"), dot = $("linkDot");
  if (!pill) return;
  linkState = { ...linkState, ...state, at: Date.now() };
  const phase = state.phase || (state.ok ? "ok" : (state.engine === false ? "engine" : "down"));
  linkState.phase = phase;
  const look = LINK_LOOK[phase] || LINK_LOOK.checking;
  pill.style.background = look.bg;
  pill.style.borderColor = look.br;
  if (dot) dot.style.background = look.dot;
  if (txt) { txt.style.color = look.ink; txt.textContent = look.text; }
  const why = String(linkState.error || "").replace(/^AbopError:\s*/, "");
  pill.title = phase === "ok"
    ? "Связь с ABOP есть · " + (linkState.url || "") + " · нажмите для подробностей"
    : (why || "Нажмите, чтобы посмотреть причину");
}

function linkDetails() {
  const when = linkState.at ? new Date(linkState.at).toLocaleTimeString("ru-RU") : "—";
  const phase = linkState.phase;
  const rows = [
    ["Состояние", phase === "ok" ? "связь есть, запуск агентов доступен"
      : phase === "engine" ? "локальный движок приложения не отвечает"
      : phase === "checking" ? "идёт проверка" : "сервер ABOP не отвечает"],
    ["Адрес сервера", linkState.url || "—"],
    // Адрес установки без арендатора не отвечает на вопрос «чьи данные я вижу»: одна установка
    // обслуживает несколько организаций, и фильтруют их сторы, а не адрес.
    ["Арендатор", !ctx.authed ? "вы не вошли"
      : (ctx.tenantKnown ? (ctx.tenant + (ctx.tenant !== ctx.tenantOwn ? " — данные клиента, не ваши" : "")) : "неизвестно — сервер не ответил")],
    ["Последняя проверка", when],
  ];
  if (linkState.error) rows.push(["Ответ сервера", String(linkState.error).slice(0, 400)]);
  const body = `<div style="display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:12.5px;line-height:1.55">`
    + rows.map(([k, v]) => `<div style="color:var(--ink-3)">${esc(k)}</div><div style="color:var(--ink-1);word-break:break-word">${esc(v)}</div>`).join("")
    + `</div>`
    + (phase === "ok" ? "" : `<div style="margin-top:12px;font-size:12px;color:var(--ink-3);line-height:1.55">История чата и настройки доступны без сервера. Запуск агентов и очередь подтверждений вернутся сами, как только связь восстановится.</div>`);
  modal("Связь с ABOP", body, () => { pingLink(true); }, "Проверить сейчас", { kicker: "Состояние" });
}

function markOffline(e) {
  const st = e && typeof e === "object" ? Number(e.status || 0) : 0;
  if (!(st === 0 || st === 502 || st === 503 || st === 504)) return;
  if (linkOk) { linkOk = false; renderLink({ ok: false, engine: st !== 0, error: (e && (e.detail || e.message)) || "" }); }
  recheckLink();
}

// Перепроверка связи не чаще раза в 3 с: индикатор чинится фактом ответа, а не таймером.
let _recheckAt = 0;
function recheckLink() {
  const now = Date.now();
  if (now - _recheckAt < 3000) return;
  _recheckAt = now;
  setTimeout(() => { pingLink().catch(() => {}); }, 150);
}

async function pingLink(manual) {
  if (manual) renderLink({ phase: "checking" });
  let h = null, engine = true;
  try { h = await api("/api/health"); } catch { engine = false; }
  const ok = !!(h && h.ok && h.abop !== false);
  if (ok && !linkOk) { toast("Связь с ABOP восстановлена", "ok"); relinkModules(); }
  if (!ok && manual) toast(engine ? "ABOP всё ещё недоступен" : "Движок приложения не отвечает", "warn");
  linkOk = ok;
  renderLink({ ok, engine, url: (h && h.abop_url) || linkState.url, error: (h && h.abop_error) || "" });
  return ok;
}

async function boot() {
  const lg = $("apeLogo"); if (lg) lg.innerHTML = apeLogo(32);
  const rt = $("railToggle"); if (rt) rt.onclick = toggleRail;
  applyRail();
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
  // Строка живой работы: видна из любого раздела, опрашивается с растущей паузой. Ставим ПОСЛЕ
  // ветки if/else — первая попытка воткнула вызов между `if` и `else`, и модуль перестал
  // разбираться вовсе. Поймал тест на разбор ES-модуля (`node --check`).
  startЖивое();
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && (e.key === "k" || e.key === "K")) { e.preventDefault(); openPalette(); }
    else if (e.ctrlKey && (e.key === "n" || e.key === "N")) { e.preventDefault(); loadModule("chat", { newChat: true }); }
  });
  const pb = $("cmdBtn"); if (pb) pb.onclick = openPalette;
  const lp = $("linkPill"); if (lp) lp.onclick = () => linkDetails();
  await pingLink();
  if (linkTimer) clearInterval(linkTimer);
  linkTimer = setInterval(() => pingLink(), 20000);   // офлайн виден сам, без действий пользователя
  document.addEventListener("visibilitychange", () => { if (!document.hidden) recheckLink(); });
  // Очередь подтверждений опрашивает ядро, а не панель чата: иначе счётчик замирал, стоило уйти
  // в другой раздел, и человек не видел, что письмо или задача ждут решения.
  const pollHitl = async () => {
    try { const q = await api("/api/modules/agents/hitl"); setHitlCount(Array.isArray(q) ? q.length : 0); }
    catch { /* офлайн покажет светофор, счётчик оставляем прежним */ }
  };
  pollHitl();
  setInterval(pollHitl, 45000);
  // Клик по уведомлению обязан приводить ТУДА, о чём известили, иначе человек ищет сам.
  try { window.ape?.os?.onOpen?.((куда) => { if (куда) loadModule(String(куда)); }); } catch (e) { /* не electron */ }
  window.addEventListener("online", () => pingLink());
  window.addEventListener("offline", () => { linkOk = false; renderLink({ ok: false }); });
}

boot();
