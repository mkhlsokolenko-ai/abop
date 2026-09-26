// Модуль «Чат» — вёрстка по ДС APE Desktop, логика привязана к сайдкару: чаты, стриминг,
// вложения → контекст, агенты ABOP (карточки прогонов, HITL), цепочки, расписания, экспорт, навыки.
//
// UX-аудит 25.09 (docs/UX_AUDIT_2026-09-25.md, Desktop). Закрыто здесь:
//  D-H1  первый экран: чат создаётся лениво по первому Enter/шаблону, композер живой сразу; «тред» → «чат»
//  D-C3  HITL: превью содержимого перед подтверждением, approve строго по hitl_id, «все» — за подтверждением
//  D-H3  карточки цепочек/решений/уведомлений сохраняются в историю (note) и рендерятся из данных
//  D-H4  busy-стейт: Enter во время ответа не запускает второй стрим; агент/цепочка — без дублей
//  D-H5  BookStack в доставке цепочек и в дереве решений
//  D-H6  «Мои агенты → Запустить» и «Источники/OCR → В чат» приходят сюда намерениями (ctx.open)
//  D-H9  удаление чатов/цепочек/расписаний/файлов — через подтверждение; цели ≥ 28px
//  D-H14 цвета статусов — только токены темы
//  D-H16 один док-ряд чипов над композером вместо пяти баров; профили ответа по-русски
//  средние: esc() с кавычками, md() с таблицами/списками, автоскролл не мешает читать, alert() → тосты
const M = "/api/modules/chat";
const A_AG = "/api/modules/agents";
const A_CAB = "/api/modules/cabinet";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ── markdown → HTML (заголовки, жирный/курсив, код, ссылки, списки, таблицы) ──
function mdInline(t) {
  let h = t;
  h = h.replace(/`([^`\n]+)`/g, '<code style="background:var(--hover);padding:1px 5px;border-radius:5px;font-family:var(--mono);font-size:.92em">$1</code>');
  h = h.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  h = h.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<i>$2</i>");
  h = h.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return h;
}
function md(t) {
  const src = esc(t || "");
  const blocks = [];
  let h = src.replace(/```(\w+)?\n?([\s\S]*?)```/g, (_m, lang, c) => {
    blocks.push(`<span style="display:flex;flex-direction:column;border-radius:10px;overflow:hidden;border:1px solid var(--line-2);margin:6px 0"><span style="display:flex;align-items:center;gap:8px;padding:7px 11px;background:var(--code)"><span style="flex:1;font-family:var(--mono);font-size:11px;color:rgba(255,255,255,.55)">${esc(lang || "code")}</span><button type="button" class="codecopy" style="padding:3px 9px;border:1px solid rgba(255,255,255,.16);border-radius:7px;background:rgba(255,255,255,.06);color:rgba(255,255,255,.8);font-size:11px;cursor:pointer">⧉ копировать</button></span><span class="codebody" style="padding:12px;background:var(--code);font-family:var(--mono);font-size:11.5px;line-height:1.65;color:#c7d2fe;white-space:pre-wrap">${c.replace(/^\n/, "")}</span></span>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const lines = h.split("\n"); const out = []; let i = 0;
  const isTableRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const isSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  while (i < lines.length) {
    const l = lines[i];
    if (isTableRow(l) && i + 1 < lines.length && isSep(lines[i + 1])) {
      const cells = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => mdInline(c.trim()));
      const head = cells(l); i += 2; const rows = [];
      while (i < lines.length && isTableRow(lines[i])) { rows.push(cells(lines[i])); i++; }
      out.push(`<table class="md-table"><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    if (/^\s*[-*•]\s+/.test(l)) { const items = []; while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) { items.push(mdInline(lines[i].replace(/^\s*[-*•]\s+/, ""))); i++; } out.push(`<ul class="md-list">${items.map((x) => `<li>${x}</li>`).join("")}</ul>`); continue; }
    if (/^\s*\d+[.)]\s+/.test(l)) { const items = []; while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) { items.push(mdInline(lines[i].replace(/^\s*\d+[.)]\s+/, ""))); i++; } out.push(`<ol class="md-list">${items.map((x) => `<li>${x}</li>`).join("")}</ol>`); continue; }
    const hm = /^\s*(#{1,4})\s+(.*)$/.exec(l);
    if (hm) { out.push(`<b style="display:block;margin:${out.length ? "8px" : "0"} 0 2px">${mdInline(hm[2])}</b>`); i++; continue; }
    out.push(mdInline(l)); i++;
  }
  // список/таблица уже блочные — не добавляем к ним переносы; текстовые строки соединяем \n (white-space:pre-wrap)
  let res = ""; for (let k = 0; k < out.length; k++) { const b = out[k]; const block = /^<(ul|ol|table|b style)/.test(b); res += b; if (k < out.length - 1 && !block && !/^<(ul|ol|table)/.test(out[k + 1])) res += "\n"; }
  return res.replace(/\u0000(\d+)\u0000/g, (_m, n) => blocks[+n]);
}

// 1:1 из эталона APE Desktop: глифы/заголовки/подписи/промпты дословно.
const TEMPLATES = [
  ["✉", "Письмо клиенту", "Черновик по короткой вводной, тон на выбор", "Напиши письмо клиенту: переносим срок поставки на две недели, нужно сохранить отношения.", "email-draft"],
  ["▤", "Саммари встречи", "Из расшифровки — решения и задачи", "Сделай саммари встречи: решения, ответственные, сроки.", ""],
  ["★", "Разбор отзывов", "Кластеры боли и частота", "Разбери отзывы клиентов за квартал: кластеры проблем и частота.", ""],
  ["◈", "Проверка идеи", "Экономика, риски, что проверить первым", "Оцени идею внутреннего маркетплейса подрядчиков: экономика и риски.", "devils-advocate"],
  ["⇄", "Сравнение вариантов", "Таблица критериев и вывод", "Сравни два варианта подрядчика по стоимости, срокам и рискам.", ""],
  ["◷", "План на неделю", "Приоритеты из списка задач", "Собери план на неделю из списка задач с приоритетами.", ""],
];
const PROFILES = [["standard", "Обычный ответ"], ["code", "Код и таблицы"], ["research", "Исследование"]];
const DELIVER_LABEL = { chat: "в чат", email: "на почту", redmine: "в Redmine", bookstack: "в BookStack", system: "в систему" };
const CH_ICON = (ch) => { const c = (ch || "").toLowerCase(); return c.includes("redmine") ? "🎫" : (c.includes("book") || c.includes("вики")) ? "📚" : (c.includes("почт") || c.includes("mail") || c.includes("email")) ? "✉" : c.includes("pdf") ? "📄" : c.includes("yougile") ? "📋" : c === "chat" ? "💬" : "↗"; };
const NEW_TITLE = "Новый чат";

export async function mount(root, ctx) {
  const { api, mascot, modal, confirm: confirmDialog, toast, humanError } = ctx;
  let threads = [], cur = null, messages = [], skills = [], roles = [], search = "", curAbort = null, abopAgents = [];
  let schedules = [], _schedSeen = {}, pipelines = [], hitlQueue = [], quota = null, kbFiles = [];
  let busy = false;          // идёт стрим/прогон — композер и запуски блокируются (D-H4)
  let curJob = null;         // задание очереди ABOP (async-прогон): можно отменить
  const persona = () => { try { return localStorage.getItem("ape_persona") || ""; } catch { return ""; } };

  // Каталоги грузим ПАРАЛЛЕЛЬНО и не блокируем ими первый экран (D-H11: раньше чат ждал 3 запроса подряд).
  const catalogs = Promise.all([
    api(M + "/skills").then((r) => { skills = Array.isArray(r) ? r : []; }).catch(() => { skills = []; }),
    api(M + "/agent-roles").then((r) => { roles = Array.isArray(r) ? r : []; }).catch(() => { roles = []; }),
    api(M + "/abop-agents").then((r) => { abopAgents = Array.isArray(r) ? r : []; }).catch(() => { abopAgents = []; }),
  ]);

  root.innerHTML = `
    <aside aria-label="Чаты" style="flex:none;width:252px;display:flex;flex-direction:column;gap:10px;padding:14px 12px;border-right:1px solid var(--line);background:var(--rail);min-height:0">
      <button id="newTh" class="btn primary" style="display:flex;align-items:center;justify-content:center;gap:8px;padding:10px;border-radius:11px;font-size:13px">＋ Новый чат</button>
      <input id="thSearch" placeholder="Поиск по чатам" aria-label="Поиск по чатам" style="padding:9px 12px;border-radius:10px;font-size:12.5px"/>
      <div id="thList" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:5px;min-height:0"></div>
    </aside>
    <section id="chatSec" style="flex:1;display:flex;flex-direction:column;min-width:0;min-height:0;position:relative">
      <div id="dropHint" style="display:none;position:absolute;inset:14px;z-index:8;flex-direction:column;align-items:center;justify-content:center;gap:12px;border:2px dashed var(--accent-2);border-radius:18px;background:var(--accent-bg);backdrop-filter:blur(6px);pointer-events:none">
        ${mascot("scan", 52)}<span style="font-size:15px;font-weight:600;color:var(--accent-ink)">Отпустите файл — добавлю в контекст чата</span>
      </div>
      <div id="scroll" style="flex:1;overflow-y:auto;padding:22px 26px 8px;min-height:0">
        <div id="col" role="log" aria-live="polite" style="max-width:760px;margin:0 auto;display:flex;flex-direction:column;gap:18px"></div>
      </div>
      <div style="flex:none;padding:8px 26px 18px">
        <div style="max-width:760px;margin:0 auto;display:flex;flex-direction:column;gap:9px">
          <div id="hitlBar"></div>
          <div id="dock" class="dock"></div>
          <div style="padding:12px 14px;border-radius:16px;background:var(--panel);border:1px solid var(--line-2);backdrop-filter:var(--blur-strong);box-shadow:var(--shadow-2);display:flex;flex-direction:column;gap:10px">
            <div id="tools" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"></div>
            <div style="display:flex;align-items:flex-end;gap:10px">
              <textarea id="inp" rows="2" aria-label="Сообщение" placeholder="Опишите задачу…  (Enter — отправить, Shift+Enter — перенос)" style="flex:1;min-width:0;padding:10px 12px;border-radius:12px;font-size:13.5px;line-height:1.55"></textarea>
              <button id="sendBtn" class="btn primary" title="Отправить · Enter" aria-label="Отправить" style="width:44px;height:44px;flex:none;padding:0;border-radius:12px;font-size:16px">↑</button>
            </div>
          </div>
        </div>
      </div>
      <div id="drawer" role="complementary" aria-label="Инструменты и агенты" style="position:absolute;top:0;right:0;bottom:0;width:386px;max-width:88%;transform:translateX(100%);transition:transform .28s cubic-bezier(.4,0,.2,1);background:var(--rail);backdrop-filter:blur(20px);border-left:1px solid var(--line-2);z-index:41;display:flex;flex-direction:column;box-shadow:-20px 0 50px rgba(0,0,0,.32)">
        <div style="flex:none;display:flex;align-items:center;gap:8px;padding:14px 16px;border-bottom:1px solid var(--line)">
          <div style="flex:1;display:flex;gap:4px;padding:4px;border-radius:11px;background:var(--hover);border:1px solid var(--line)">
            <button id="drTabTools" style="flex:1;padding:8px 10px;border:none;border-radius:8px;font-size:12.5px;font-weight:600">Инструменты</button>
            <button id="drTabAgents" style="flex:1;padding:8px 10px;border:none;border-radius:8px;font-size:12.5px;font-weight:600">Агенты</button>
          </div>
          <button id="drClose" class="ico" title="Убрать шторку · Esc" aria-label="Закрыть шторку">→</button>
        </div>
        <div id="drBody" style="flex:1;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:11px"></div>
      </div>
    </section>`;
  const $ = (id) => root.querySelector("#" + id);
  const drawerOpen = () => $("drawer").style.transform === "translateX(0px)";
  const closeDrawer = () => ($("drawer").style.transform = "translateX(100%)");

  $("thSearch").oninput = (e) => { search = e.target.value.toLowerCase(); renderThreads(); };
  $("drClose").onclick = closeDrawer;
  const sec = $("chatSec");
  sec.addEventListener("dragover", (e) => { e.preventDefault(); $("dropHint").style.display = "flex"; });
  sec.addEventListener("dragleave", (e) => { if (!sec.contains(e.relatedTarget)) $("dropHint").style.display = "none"; });
  sec.addEventListener("drop", async (e) => { e.preventDefault(); $("dropHint").style.display = "none"; for (const f of [...(e.dataTransfer.files || [])]) if (/\.(txt|md|csv|json|pdf|docx|xlsx)$/i.test(f.name)) await attachFile(f); });

  if (window.__apeChatKey) document.removeEventListener("keydown", window.__apeChatKey);
  window.__apeChatKey = (e) => {
    if (!root.isConnected || root.hidden) return;
    if (e.key === "Escape") { if (drawerOpen()) closeDrawer(); else if (curAbort) curAbort.abort(); }
  };
  document.addEventListener("keydown", window.__apeChatKey);

  // ── скролл: автопрокрутка только если читатель у низа (иначе не мешаем читать) ──
  function nearBottom() { const s = $("scroll"); return s.scrollHeight - s.scrollTop - s.clientHeight < 90; }
  function scrollDown(force) { const s = $("scroll"); if (force || nearBottom()) s.scrollTop = s.scrollHeight; }

  // ── чаты ──
  function renderThreads() {
    const list = threads.filter((t) => !search || (t.title || "").toLowerCase().includes(search));
    const fav = list.filter((t) => t.favorite), rest = list.filter((t) => !t.favorite);
    const row = (t) => { const on = cur && t.id === cur.id; return `<div class="thr" data-id="${t.id}" style="display:flex;align-items:center;gap:4px;padding:6px 6px 6px 9px;border:1px solid ${on ? "var(--line-2)" : "var(--line)"};border-radius:10px;background:${on ? "var(--hover)" : "transparent"}">
        <button class="thopen" data-id="${t.id}" style="flex:1;min-width:0;padding:2px 0;border:none;background:transparent;text-align:left;display:flex;flex-direction:column;gap:2px;color:inherit">
          <span style="font-size:12.5px;font-weight:600;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%">${esc(t.title)}</span>
          <span style="font-size:11px;color:var(--ink-3)">${esc((PROFILES.find((p) => p[0] === t.profile) || [0, t.profile])[1])}${t.skills && t.skills.length ? " · навыков: " + t.skills.length : ""}</span>
        </button>
        <button class="ico ghost thren" data-id="${t.id}" title="Переименовать" aria-label="Переименовать чат" style="width:26px;height:26px;font-size:12px">✎</button>
        <button class="ico ghost thfav" data-id="${t.id}" title="${t.favorite ? "Убрать из избранного" : "В избранное"}" aria-label="Избранное" style="width:26px;height:26px;font-size:12px;color:${t.favorite ? "var(--warn-ink)" : "var(--ink-3)"}">★</button>
        <button class="ico ghost danger thdel" data-id="${t.id}" title="Удалить чат" aria-label="Удалить чат" style="width:26px;height:26px;font-size:11px">✕</button>
      </div>`; };
    const group = (folder, arr) => arr.length ? `<div style="display:flex;flex-direction:column;gap:5px"><span class="ape-label" style="padding:0 4px">${folder}</span>${arr.map(row).join("")}</div>` : "";
    $("thList").innerHTML = (group("избранное", fav) + group("чаты", rest)) ||
      `<div style="padding:20px 10px;text-align:center;font-size:12px;color:var(--ink-3);line-height:1.5">${search ? "Ничего не нашлось по «" + esc(search) + "»" : "Чатов пока нет.<br>Напишите задачу справа — чат создастся сам."}</div>`;
    $("thList").querySelectorAll(".thopen").forEach((b) => { b.onclick = () => openThread(threads.find((x) => x.id == b.dataset.id)); b.ondblclick = () => renameThread(threads.find((x) => x.id == b.dataset.id)); });
    $("thList").querySelectorAll(".thren").forEach((b) => b.onclick = () => renameThread(threads.find((x) => x.id == b.dataset.id)));
    $("thList").querySelectorAll(".thfav").forEach((b) => b.onclick = async () => { const t = threads.find((x) => x.id == b.dataset.id); t.favorite = t.favorite ? 0 : 1; await saveThread(t); await loadThreads(); });
    $("thList").querySelectorAll(".thdel").forEach((b) => b.onclick = async () => {
      const t = threads.find((x) => x.id == b.dataset.id);
      if (!(await confirmDialog({ title: "Удалить чат?", text: `«${esc(t ? t.title : "")}» — история, вложения и карточки прогонов будут удалены безвозвратно.`, okLabel: "Удалить чат" }))) return;
      try { await api(M + "/threads/" + b.dataset.id, { method: "DELETE" }); } catch (e) { toast(humanError(e), "danger"); return; }
      if (cur && cur.id == b.dataset.id) { cur = null; messages = []; }
      await loadThreads(); render(); renderTools(); renderDock();
    });
  }
  function renameThread(t) {
    if (!t) return;
    const ov = modal("Название чата", `<input id="tt" value="${esc(t.title)}" style="width:100%" autofocus/>
      <button class="btn sm" id="autoT" style="margin-top:8px;align-self:flex-start">✨ Подобрать по содержанию</button>`, async (b) => {
      const v = b.querySelector("#tt").value.trim(); if (!v) return false; t.title = v; if (cur && cur.id === t.id) cur.title = v; await saveThread(t); await loadThreads();
    }, "Сохранить");
    ov.querySelector("#autoT").onclick = async (e) => { e.preventDefault(); try { const r = await api(M + "/threads/" + t.id + "/autotitle", { method: "POST" }); if (r.ok) ov.querySelector("#tt").value = r.title; } catch (er) { toast(humanError(er), "danger"); } };
    ov.querySelector("#tt").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); ov.querySelector("#mOk").click(); } };
  }
  async function saveThread(t) { await api(M + "/threads/" + t.id, { method: "PATCH", body: JSON.stringify({ title: t.title, profile: t.profile, skills: t.skills, favorite: t.favorite || 0 }) }); }
  async function loadThreads() { try { threads = await api(M + "/threads"); if (!Array.isArray(threads)) threads = []; } catch (e) { threads = []; } renderThreads(); }
  async function openThread(t) {
    if (!t) return;
    cur = { ...t, skills: t.skills || [] }; renderThreads(); renderTools();
    try { messages = await api(M + "/threads/" + t.id + "/messages"); } catch (e) { messages = []; toast(humanError(e), "danger"); }
    render(); scrollDown(true); loadKb();
  }
  // Ленивое создание чата (D-H1): первый Enter/шаблон/вложение сами заводят чат.
  let pendingProfile = "standard", pendingSkills = [];
  async function ensureThread(title) {
    if (cur) return cur;
    const t = await api(M + "/threads", { method: "POST", body: JSON.stringify({ title: title || NEW_TITLE, profile: pendingProfile, skills: pendingSkills }) });
    cur = { ...t, skills: t.skills || pendingSkills }; messages = []; kbFiles = [];
    await loadThreads(); renderTools();
    return cur;
  }
  async function newChat() { cur = null; messages = []; kbFiles = []; pendingProfile = "standard"; pendingSkills = []; renderThreads(); render(); renderTools(); renderDock(); $("inp").focus(); }
  // Служебное сообщение ассистента с meta → в историю (D-H3)
  async function note(content, meta) {
    const m = { role: "assistant", content: content || "", meta: meta || {} };
    messages.push(m);
    if (cur) { try { await api(M + "/threads/" + cur.id + "/note", { method: "POST", body: JSON.stringify({ content: m.content, meta: m.meta }) }); } catch { /* история — best effort */ } }
    return m;
  }

  // ── находки → читаемый текст ──
  function fmtObjFinding(x) {
    if (typeof x === "string") return x;
    if (!x || typeof x !== "object") return String(x == null ? "" : x);
    const obs = x["наблюдение"] || x.observation || x["запись"] || x["описание"] || "";
    if (obs) { const extra = [x["сумма"] && ("— " + x["сумма"]), x["норма"] && ("· норма: " + x["норма"]), x["класс"] && ("[" + x["класс"] + "]")].filter(Boolean).join(" "); return obs + (extra ? " " + extra : ""); }
    if (typeof x.text === "string" && x.text.trim()) return cleanFinding(x.text);
    const s = x.structured;
    if (s && typeof s === "object") { const arr = Array.isArray(s) ? s : (s["находки"] || s.findings || null); if (Array.isArray(arr) && arr.length) return arr.map(fmtObjFinding).join("; "); }
    return JSON.stringify(x);
  }
  function cleanFinding(t) {
    if (t && typeof t === "object") return fmtObjFinding(t);
    t = (t == null ? "" : String(t)).trim();
    const i = t.indexOf("{"), j = t.lastIndexOf("}");
    if (i >= 0 && j > i) { try { const o = JSON.parse(t.slice(i, j + 1)); const arr = Array.isArray(o) ? o : (o["находки"] || o.findings || []); if (Array.isArray(arr) && arr.length) return arr.map(fmtObjFinding).join("; "); } catch { /* не JSON */ } }
    return t.replace(/^[•\s]+/, "");
  }
  const agentName = (id, fallback) => (abopAgents.find((a) => a.id === id) || {}).name || fallback || id || "";

  // ── карточка прогона реального агента ABOP (находки / доставка / HITL) ──
  function runCard(s) {
    const fnd = (s.findings || []).map((t) => `<div style="font-size:12.5px;color:var(--ink);border-left:2px solid var(--accent);padding-left:10px;margin:4px 0">${esc(cleanFinding(t).slice(0, 400))}</div>`).join("");
    const dl = (s.delivery || []).map((d) => { const wait = d.mode === "awaiting_hitl"; const mode = wait ? "ожидает вашего подтверждения" : d.mode === "real" ? "отправлено" : d.mode === "dry_run" ? "черновик (без отправки)" : d.mode === "denied" ? "доступ закрыт" : esc(d.mode || ""); return `<div style="font-size:11.5px;color:${wait ? "var(--warn-ink)" : d.mode === "denied" ? "var(--danger-ink)" : "var(--ink-2)"}">${CH_ICON(d.channel)} ${esc(d.channel)}${d.to ? " → " + esc(d.to) : ""} · ${mode}${d.result && d.mode === "real" ? ` <span style="color:var(--ink-3)">${esc(String(d.result).slice(0, 90))}</span>` : ""}</div>`; }).join("");
    const waits = (s.delivery || []).filter((d) => d.mode === "awaiting_hitl");
    const hitlIds = waits.map((d) => d.hitl_id).filter(Boolean);
    const verd = s.verdict && s.verdict.within_envelope != null ? `<span style="font-family:var(--mono);font-size:11px;color:${s.verdict.within_envelope ? "var(--ok-ink)" : "var(--danger-ink)"}">конверт: ${s.verdict.within_envelope ? "в рамках" : "превышен"}${s.verdict.autonomy_used ? " · " + esc(s.verdict.autonomy_used) : ""}</span>` : "";
    const name = s.agent_name || agentName(s.agent_id, s.agent_id);
    return `<div style="display:flex;flex-direction:column;gap:8px">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><span class="ape-label">агент · ${esc(name)}</span>${s.cached ? '<span style="font-size:11px;color:var(--ink-3)">· из кэша</span>' : ""}${s.trace_id ? `<span style="font-size:11px;color:var(--ink-3)" title="идентификатор трассы прогона">· ${esc((s.trace_id || "").slice(0, 8))}</span>` : ""}</div>
      <div style="font-size:12.5px;color:var(--ink-2)">находок: <b>${esc(String(s.findings_total ?? 0))}</b>${s.investigations_total != null ? ` · расследований: <b>${esc(String(s.investigations_total))}</b>` : ""}${s.tokens ? ` · <span title="токены этого прогона">🎫 ${s.tokens >= 1000 ? Math.round(s.tokens / 1000) + "k" : s.tokens}</span>` : ""} ${verd}</div>
      ${fnd || '<div style="font-size:12px;color:var(--ink-3)">Находок не выявлено.</div>'}
      ${dl ? `<div class="ape-label" style="margin-top:4px">доставка</div>${dl}` : ""}
      ${waits.length ? (s.hitl_done ? `<div style="font-size:12px;font-weight:600;color:${s.hitl_done === "approve" ? "var(--ok-ink)" : "var(--ink-3)"}">${s.hitl_done === "approve" ? "✓ Действие подтверждено" : "⃠ Действие отклонено"}${s.hitl_result ? `<div style="font-size:11px;color:var(--ink-3);font-weight:400;margin-top:3px">${esc(String(s.hitl_result).slice(0, 140))}</div>` : ""}</div>`
        : `<div class="hitl-panel" data-hitl="${esc(hitlIds.join(","))}" data-agent="${esc(s.agent_id || "")}" style="margin-top:4px">
        <span class="hitl-head">🛡 Агент подготовил внешнее действие — нужно ваше решение</span>
        <span style="font-size:11.5px;color:var(--ink-2)">${waits.map((d) => `${CH_ICON(d.channel)} ${esc(d.title || d.channel)}${d.to ? " → " + esc(d.to) : ""}`).join(" · ")}</span>
        <span style="display:flex;gap:8px;flex-wrap:wrap"><button type="button" class="btn ok hitlOk">Посмотреть и подтвердить</button><button type="button" class="btn hitlNo">Отклонить</button></span></div>`) : ""}
      <button type="button" class="btn sm mkRecurring" data-agent="${esc(s.agent_id || "")}" data-name="${esc(name)}" style="align-self:flex-start;margin-top:2px">🔁 Сделать регулярной</button></div>`;
  }
  // карточка результата цепочки — из данных (D-H3), legacy-строка HTML тоже поддерживается
  function pipelineHTML(pr) {
    if (typeof pr === "string") return pr;
    const steps = pr.steps || [];
    const totTok = steps.reduce((a, s) => a + (s.tokens || 0), 0), totFnd = steps.reduce((a, s) => a + (s.findings_total || 0), 0);
    const cards = steps.map((s, i) => `<div style="border-top:1px solid var(--line);padding-top:8px;margin-top:8px">
      <div class="ape-label" style="margin-bottom:4px">шаг ${i + 1}/${steps.length}${s.deliver ? " · результат " + esc(DELIVER_LABEL[s.deliver] || s.deliver) : " · только в чат"}</div>
      ${s.error ? `<span class="danger-ink" style="font-size:12px">ошибка: ${esc(humanError(s.error))}</span>` : runCard(s)}</div>`).join("");
    return `<div style="font-weight:600;margin-bottom:2px">🔗 Цепочка «${esc(pr.name || pr.pid)}» — шагов: ${steps.length} · находок: ${totFnd}${totTok ? ` · 🎫 ${totTok >= 1000 ? Math.round(totTok / 1000) + "k" : totTok} токенов` : ""}</div>${pr.task ? `<div style="font-size:12px;color:var(--ink-3)">задача: ${esc(pr.task.slice(0, 200))}</div>` : ""}${cards}`;
  }

  // ── сообщения ──
  function bubble(m, idx) {
    const mine = m.role === "user";
    const radius = mine ? "16px 16px 4px 16px" : "16px 16px 16px 4px";
    const meta = m.meta || {};
    const inner = mine ? `<span style="font-size:13.5px;line-height:1.6;white-space:pre-wrap">${esc(m.content)}</span>`
      : (meta.run_agent ? runCard(meta.run_agent)
        : (meta.pipeline_result ? pipelineHTML(meta.pipeline_result)
          : (meta.chain_suggest ? chainSuggestHTML(meta.chain_suggest)
            : (meta.decision ? decisionHTML(meta.decision) : (meta.notice ? noticeHTML(meta.notice, m.content) : md(m.content))))));
    const cost = meta.model ? `<span style="margin-left:4px;font-family:var(--mono);font-size:11px;color:var(--ink-3)">${esc(meta.model)} · ${meta.cost_rub ?? 0} ₽</span>` : "";
    const isCard = !!(meta.run_agent || meta.pipeline_result || meta.chain_suggest || meta.decision || meta.notice);
    const acts = mine
      ? `<button type="button" data-edit="${idx}" class="msgact">✎ изменить</button>`
      : `<button type="button" data-copy="${idx}" class="msgact">⧉ копировать</button>${meta.decision || meta.chain_suggest || meta.notice ? "" : `<button type="button" data-regen="${idx}" class="msgact">↻ ещё раз</button>`}${cost}`;
    return `<div style="display:flex;flex-direction:column;align-items:${mine ? "flex-end" : "flex-start"};gap:7px;animation:ape-in .3s ease-out">
      <div class="bub" style="max-width:${isCard ? "96%" : "88%"};padding:13px 16px;border-radius:${radius};background:${mine ? "var(--user-bubble)" : "var(--panel)"};border:1px solid ${mine ? "var(--user-bubble-line)" : "var(--line)"};backdrop-filter:blur(16px);box-shadow:${mine ? "0 2px 10px rgba(99,102,241,.14)" : "var(--shadow-1)"};white-space:${isCard ? "normal" : "pre-wrap"};font-size:13.5px;line-height:1.6;min-width:0">${inner}</div>
      <div style="display:flex;align-items:center;gap:6px">${acts}</div></div>`;
  }
  function noticeHTML(n, content) { return `<div style="display:flex;gap:10px;align-items:flex-start"><span style="font-size:15px">${n.icon || "🔔"}</span><span style="font-size:13px;line-height:1.55">${md(content)}</span></div>`; }
  function emptyState() {
    const steps = [ctx.authed ? null : ["1", "войдите в ABOP (кнопка вверху справа)"], [ctx.authed ? "1" : "2", "выберите шаблон или опишите задачу"], [ctx.authed ? "2" : "3", "перетащите файл — он попадёт в контекст"]].filter(Boolean);
    return `<div style="display:flex;flex-direction:column;gap:22px;padding:26px 0;animation:ape-in .4s ease-out">
      <div style="display:flex;align-items:center;gap:18px">${mascot("idle", 66)}
        <div style="display:flex;flex-direction:column;gap:6px">
          <h1 style="margin:0;font-size:24px;font-weight:800;letter-spacing:-.6px">С чего начнём?</h1>
          <p style="margin:0;max-width:460px;font-size:13.5px;line-height:1.55;color:var(--ink-2)">Опишите задачу словами или возьмите готовый шаблон — чат создастся сам. Файлы можно просто перетащить в окно.</p></div></div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(214px,1fr));gap:12px">
        ${TEMPLATES.map((t, i) => `<button type="button" data-tpl="${i}" class="lift" style="display:flex;flex-direction:column;align-items:flex-start;gap:7px;padding:15px;border:1px solid var(--line);border-radius:14px;background:var(--panel);backdrop-filter:blur(16px);box-shadow:var(--shadow-1);text-align:left;color:inherit">
          <span style="font-size:17px" aria-hidden="true">${t[0]}</span><span style="font-size:13.5px;font-weight:600;color:var(--ink)">${t[1]}</span><span style="font-size:11.5px;line-height:1.45;color:var(--ink-3)">${t[2]}</span></button>`).join("")}
      </div>
      <div style="display:flex;align-items:center;gap:16px;padding:14px 18px;border-radius:14px;background:var(--panel);border:1px solid var(--line);flex-wrap:wrap">
        <span class="ape-label">${ctx.authed ? "как это работает" : "первый запуск"}</span>
        ${steps.map((o) => `<span style="display:inline-flex;align-items:center;gap:8px;font-size:12.5px;color:var(--ink-2)"><span style="width:20px;height:20px;border-radius:7px;background:var(--hover);color:var(--accent-ink);font-family:var(--mono);font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center">${o[0]}</span>${o[1]}</span>`).join("")}
      </div></div>`;
  }
  function render() {
    $("col").innerHTML = messages.length ? messages.map(bubble).join("") : emptyState();
    $("col").querySelectorAll("[data-tpl]").forEach((e) => e.onclick = async () => {
      const [, , , prompt, skill] = TEMPLATES[+e.dataset.tpl];
      if (skill) { if (cur) { if (!cur.skills.includes(skill)) { cur.skills.push(skill); await saveThread(cur); } } else if (!pendingSkills.includes(skill)) pendingSkills.push(skill); renderTools(); }
      $("inp").value = prompt; $("inp").focus();
    });
    $("col").querySelectorAll("[data-copy]").forEach((e) => e.onclick = () => {
      const m = messages[+e.dataset.copy]; const meta = m.meta || {};
      const text = meta.run_agent ? (meta.run_agent.findings || []).map(cleanFinding).join("\n") || m.content : (meta.pipeline_result && typeof meta.pipeline_result === "object" ? (meta.pipeline_result.steps || []).flatMap((s) => (s.findings || []).map(cleanFinding)).join("\n") : m.content);
      navigator.clipboard.writeText(text || ""); const o = e.textContent; e.textContent = "✓ скопировано"; setTimeout(() => e.textContent = o, 1200);
    });
    $("col").querySelectorAll("[data-regen]").forEach((e) => e.onclick = () => {
      const idx = +e.dataset.regen; const m = messages[idx]; const meta = (m && m.meta) || {};
      if (meta._rerun && meta._rerun.pid) { runPipeline(meta._rerun.pid, meta._rerun.task); return; }
      if (meta.pipeline_result && typeof meta.pipeline_result === "object" && meta.pipeline_result.pid) { runPipeline(meta.pipeline_result.pid, meta.pipeline_result.task); return; }
      if (meta.run_agent) { runAbopAgentDeliver(meta.run_agent.agent_id, meta.run_agent.agent_name || agentName(meta.run_agent.agent_id), meta.run_agent.task || "", meta.run_agent.deliver || "", true); return; }
      const p = messages[idx - 1]; if (p && p.role === "user") sendPrompt(p.content);
    });
    $("col").querySelectorAll("[data-edit]").forEach((e) => e.onclick = () => { $("inp").value = messages[+e.dataset.edit].content; $("inp").focus(); });
    $("col").querySelectorAll(".codecopy").forEach((b) => b.onclick = () => { const code = b.closest("span").parentElement.querySelector(".codebody"); navigator.clipboard.writeText(code ? code.textContent : ""); const o = b.textContent; b.textContent = "✓"; setTimeout(() => b.textContent = o, 1200); });
    $("col").querySelectorAll(".hitlOk").forEach((b) => b.onclick = () => decideDelivery(b, "approve"));
    $("col").querySelectorAll(".hitlNo").forEach((b) => b.onclick = () => decideDelivery(b, "reject"));
    $("col").querySelectorAll(".mkRecurring").forEach((b) => b.onclick = () => recurringModal(b.dataset.agent, b.dataset.name));
    $("col").querySelectorAll(".dcRun").forEach((b) => b.onclick = () => { const dc = _decisionOf(b); runAbopAgentDeliver(b.dataset.id, b.dataset.name, dc ? dc.text : "", b.dataset.deliver || ""); });
    $("col").querySelectorAll(".dcChat").forEach((b) => b.onclick = () => { const dc = _decisionOf(b); if (dc) sendPrompt(dc.text); });
    $("col").querySelectorAll(".dcChain").forEach((b) => b.onclick = () => { const dc = _decisionOf(b); if (dc) suggestChain(dc.text); });
    $("col").querySelectorAll(".chainRun").forEach((b) => b.onclick = () => { const i = +b.dataset.i; const m = messages[i]; if (m && m.meta && m.meta.chain_suggest) saveAndRunChain(m.meta.chain_suggest); });
    $("col").querySelectorAll(".msgact").forEach((b) => { b.style.cssText += ";padding:5px 9px;border:1px solid transparent;border-radius:8px;background:transparent;color:var(--ink-3);font-size:11.5px;min-height:26px"; });
  }
  function _decisionOf(btn) { const idx = +btn.closest("[data-di]").dataset.di; const m = messages[idx]; return m && m.meta && m.meta.decision; }

  // «Сделать регулярной»: периодичность + куда доставлять → крон-триггер (новая версия агента на «Строю»)
  function recurringModal(agentId, agentNm) {
    const body = `<div style="display:flex;flex-direction:column;gap:12px">
      <label style="display:flex;flex-direction:column;gap:4px"><span style="font-size:11.5px;color:var(--ink-3)">Когда запускать</span>
        <select id="rcCron" style="width:100%"><option value="09:00">Ежедневно в 09:00</option><option value="18:00">Ежедневно в 18:00</option><option value="*/60">Каждый час</option><option value="*/30">Каждые 30 минут</option><option value="*/15">Каждые 15 минут (тест)</option></select></label>
      <div><div style="font-size:11.5px;color:var(--ink-3);margin-bottom:4px">Куда результат</div>
        <div style="display:flex;gap:8px"><button type="button" data-d="chat" class="rcd chip on" style="flex:1;padding:8px;font-family:var(--ui);font-size:12px">💬 В чат (уведомить)</button><button type="button" data-d="system" class="rcd chip" style="flex:1;padding:8px;font-family:var(--ui);font-size:12px">↗ В систему (почта/трекер)</button></div></div>
      <div style="font-size:11.5px;color:var(--ink-3);line-height:1.5">Задача станет узлом-расписанием в графе агента (виден на «Строю»), запуск на сервере ABOP. Внешняя доставка — под вашим подтверждением.</div></div>`;
    let deliver = "chat";
    const ov = modal(`🔁 Регулярный запуск «${agentNm}»`, body, async () => {
      const cron = ov.querySelector("#rcCron").value;
      const r = await api(A_AG + "/trigger", { method: "POST", body: JSON.stringify({ agent_id: agentId, cron, deliver, title: agentNm }) });
      const res = (r && r.result) || {};
      await note(`Расписание создано: «${agentNm}» — ${cron}, результат ${DELIVER_LABEL[deliver]}. Новая версия ${res.agent_id || ""} на «Строю».`, { notice: { icon: "🔁" } });
      render(); scrollDown(true); loadSchedules(false); toast("Расписание создано", "ok");
    }, "Создать расписание");
    ov.querySelectorAll(".rcd").forEach((b) => b.onclick = () => { deliver = b.dataset.d; ov.querySelectorAll(".rcd").forEach((x) => x.classList.toggle("on", x.dataset.d === deliver)); });
  }

  // ── док статуса над композером: расписания · цепочки · токены · знания чата (D-H16) ──
  async function loadSchedules(notify) {
    const prev = _schedSeen;
    try { schedules = await api(A_AG + "/schedules"); if (!Array.isArray(schedules)) schedules = []; } catch { schedules = []; }
    for (const s of schedules) {
      const key = s.agent_id + "/" + s.trigger_id, at = s.last_run && s.last_run.at;
      if (notify && at && prev[key] && prev[key] !== at) {
        const f = s.last_run && s.last_run.findings;
        await note(`Регулярная задача «${s.agent}» (${s.cron}) выполнена${f != null ? ` — находок: ${f}` : ""}.`, { notice: { icon: "🔔" } });
        render(); scrollDown(false); toast(`🔔 «${s.agent}» выполнена`, "ok");
      }
      _schedSeen[key] = at || prev[key];
    }
    renderDock();
  }
  async function loadPipelines() { try { const r = await api(A_AG + "/pipelines"); pipelines = (r && r.pipelines) || []; } catch { pipelines = []; } renderDock(); }
  async function loadQuota() { try { const b = await api(A_CAB + "/billing"); quota = (b && !b.error && b.tokens != null) ? b : null; } catch { quota = null; } renderDock(); if (window.__apeRefreshCost) window.__apeRefreshCost(); }
  async function loadKb() { kbFiles = []; if (cur) { try { kbFiles = await api(M + "/threads/" + cur.id + "/files"); if (!Array.isArray(kbFiles)) kbFiles = []; } catch { kbFiles = []; } } renderDock(); }
  const fmtK = (n) => (n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n));
  function renderDock() {
    const d = $("dock"); if (!d) return;
    const chips = [];
    if (schedules.length) chips.push(`<button type="button" class="dock-chip" id="dkSched" title="Мои расписания">🔁 Расписания <b>${schedules.length}</b></button>`);
    chips.push(`<button type="button" class="dock-chip" id="dkPipes" title="Цепочки агентов">🔗 Цепочки <b>${pipelines.length}</b></button>`);
    if (quota) { const pct = Math.round(quota.tokens_pct || 0); chips.push(`<button type="button" class="dock-chip${pct >= 90 ? " danger" : pct >= 70 ? " warn" : ""}" id="dkQuota" title="Реальные токены из прогонов. Остаток ${fmtK(quota.tokens_remaining)}">🎫 <b>${fmtK(quota.tokens)}</b> / ${fmtK(quota.token_quota)}<span class="meter"><span style="width:${Math.min(100, pct)}%"></span></span></button>`); }
    kbFiles.forEach((f) => chips.push(`<span class="dock-chip" title="${esc(f.name)} · в контексте чата">📎 ${esc(String(f.name).length > 22 ? String(f.name).slice(0, 20) + "…" : f.name)}<button type="button" class="kbrm" data-id="${f.id}" aria-label="Убрать файл ${esc(f.name)}" style="width:20px;height:20px;border:none;border-radius:9999px;background:var(--hover);color:var(--ink-3);font-size:11px;margin:-4px -6px -4px 0">✕</button></span>`));
    d.innerHTML = chips.join("");
    const sc = d.querySelector("#dkSched"); if (sc) sc.onclick = openSchedules;
    const pp = d.querySelector("#dkPipes"); if (pp) pp.onclick = openPipelines;
    const qq = d.querySelector("#dkQuota"); if (qq) qq.onclick = () => modal("Токены и квота", `<div style="font-size:13px;line-height:1.7;color:var(--ink-2)">Использовано: <b style="color:var(--ink)">${fmtK(quota.tokens)}</b> из ${fmtK(quota.token_quota)} (${Math.round(quota.tokens_pct || 0)}%)<br>Остаток: <b style="color:var(--ink)">${fmtK(quota.tokens_remaining)}</b><br>Стоимость: ${quota.spent_rub ? quota.spent_rub + " ₽" : "≈0 ₽ (собственная модель)"}<br><span style="font-size:12px;color:var(--ink-3)">Токены — реальные из прогонов агентов и чата. Тот же счётчик в шапке.</span></div>`, null, null, { width: "420px" });
    d.querySelectorAll(".kbrm").forEach((x) => x.onclick = async () => { if (!cur) return; try { await api(M + "/threads/" + cur.id + "/files/" + x.dataset.id, { method: "DELETE" }); } catch (e) { toast(humanError(e), "danger"); } loadKb(); });
  }
  function openSchedules() {
    const rows = schedules.map((s) => `<div class="hitl-row" style="background:var(--field)"><span style="font-size:15px">🔁</span><span style="flex:1;min-width:0;display:flex;flex-direction:column;gap:2px"><span style="font-size:12.5px;font-weight:600">${esc(s.agent)}</span><span style="font-size:11.5px;color:var(--ink-3)">${esc(s.cron)} · ${DELIVER_LABEL[s.deliver] || esc(s.deliver)}${s.enabled === false ? " · выключено" : ""}${s.last_run && s.last_run.at ? ` · последний: ${esc(String(s.last_run.at).slice(0, 16).replace("T", " "))}${s.last_run.findings != null ? " (" + s.last_run.findings + " нах.)" : ""}` : ""}</span></span>
      <button type="button" class="ico danger schDel" data-a="${esc(s.agent_id)}" data-t="${esc(s.trigger_id)}" data-n="${esc(s.agent)}" title="Убрать расписание" aria-label="Убрать расписание">✕</button></div>`).join("");
    const ov = modal("Мои расписания", `<div style="display:flex;flex-direction:column;gap:6px">${rows || '<span style="font-size:12.5px;color:var(--ink-3)">Расписаний нет. Кнопка «🔁 Сделать регулярной» есть на карточке прогона агента.</span>'}</div>`, null, null, { width: "560px" });
    ov.querySelectorAll(".schDel").forEach((b) => b.onclick = async () => {
      if (!(await confirmDialog({ title: "Убрать расписание?", text: `«${esc(b.dataset.n)}» перестанет запускаться автоматически. Будет создана новая версия агента без триггера.`, okLabel: "Убрать" }))) return;
      try { await api(A_AG + "/trigger/" + encodeURIComponent(b.dataset.a) + "/" + encodeURIComponent(b.dataset.t), { method: "DELETE" }); toast("Расписание убрано", "ok"); } catch (e) { toast(humanError(e), "danger"); }
      ov.close(); await loadSchedules(false);
    });
  }
  function openPipelines() {
    const rows = pipelines.map((p) => `<div class="hitl-row" style="background:var(--field)"><span style="font-size:15px">🔗</span><span style="flex:1;min-width:0;display:flex;flex-direction:column;gap:2px"><span style="font-size:12.5px;font-weight:600">${esc(p.name)}</span><span style="font-size:11.5px;color:var(--ink-3)">${(p.steps || []).map((s) => esc(s.agent_name || agentName(s.agent_id))).join(" → ")}</span></span>
      <button type="button" class="btn sm plRun" data-id="${esc(p.id)}">▶ Запустить</button><button type="button" class="ico danger plDel" data-id="${esc(p.id)}" data-n="${esc(p.name)}" title="Удалить цепочку" aria-label="Удалить цепочку">✕</button></div>`).join("");
    const ov = modal("Цепочки агентов", `<div style="font-size:12px;color:var(--ink-2);margin-bottom:4px">Выход одного агента идёт в контекст следующего; последний шаг доставляет результат.</div><div style="display:flex;flex-direction:column;gap:6px">${rows || '<span style="font-size:12.5px;color:var(--ink-3)">Пока нет цепочек — соберите из двух и более агентов.</span>'}</div>`,
      () => { ov.close(); openPipeBuilder(); return false; }, "＋ Собрать цепочку", { width: "600px", cancelLabel: "Закрыть" });
    ov.querySelectorAll(".plRun").forEach((b) => b.onclick = () => { ov.close(); runPipeline(b.dataset.id, ($("inp").value || "").trim()); });
    ov.querySelectorAll(".plDel").forEach((b) => b.onclick = async () => {
      if (!(await confirmDialog({ title: "Удалить цепочку?", text: `«${esc(b.dataset.n)}» будет удалена. Прогоны в истории чата останутся.`, okLabel: "Удалить" }))) return;
      try { await api(A_AG + "/pipelines/" + encodeURIComponent(b.dataset.id), { method: "DELETE" }); toast("Цепочка удалена", "ok"); } catch (e) { toast(humanError(e), "danger"); }
      ov.close(); await loadPipelines();
    });
  }
  function openPipeBuilder() {
    let chosen = [];
    const opts = abopAgents.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}${a.family ? " · " + esc(a.family) : ""}</option>`).join("");
    let finalDeliver = "chat";
    const dOpt = (d, label) => `<button type="button" data-d="${d}" class="pld chip${d === finalDeliver ? " on" : ""}" style="flex:1;padding:7px;font-family:var(--ui);font-size:11.5px">${label}</button>`;
    const body = `<input id="plName" placeholder="Название цепочки (напр. Почта → Менеджер)" style="width:100%;margin-bottom:10px" autofocus/>
      <div style="font-size:12px;color:var(--ink-2);margin-bottom:6px">Шаги по порядку — выход агента идёт в контекст следующего:</div>
      <div id="plSteps" style="display:flex;flex-direction:column;gap:6px;margin-bottom:8px"></div>
      <div style="display:flex;gap:8px;margin-bottom:12px"><select id="plPick" style="flex:1">${opts}</select><button type="button" id="plAdd" class="btn">＋ Шаг</button></div>
      <div style="font-size:12px;color:var(--ink-2);margin-bottom:6px">Куда результат цепочки (последний шаг):</div>
      <div id="plDeliver" style="display:flex;gap:6px;flex-wrap:wrap">${dOpt("chat", "💬 В чат")}${dOpt("email", "✉ Почта")}${dOpt("redmine", "🎫 Redmine")}${dOpt("bookstack", "📚 BookStack")}</div>
      <div style="font-size:11.5px;color:var(--ink-3);margin-top:8px">Отправка наружу идёт через подтверждение (HITL), если оно включено у агента.</div>`;
    const ov = modal("Собрать цепочку агентов", body, async (b) => {
      const name = (b.querySelector("#plName").value || "").trim();
      if (!name || chosen.length < 2) { toast("Нужно имя и минимум 2 шага", "warn"); return false; }
      const steps = chosen.map((id, i) => ({ agent_id: id, deliver: i < chosen.length - 1 ? "chat" : finalDeliver }));
      await api(A_AG + "/pipelines", { method: "POST", body: JSON.stringify({ name, steps }) }); await loadPipelines(); toast("Цепочка сохранена", "ok");
    }, "Сохранить цепочку", { width: "600px" });
    ov.querySelectorAll(".pld").forEach((b) => b.onclick = () => { finalDeliver = b.dataset.d; ov.querySelectorAll(".pld").forEach((x) => x.classList.toggle("on", x.dataset.d === finalDeliver)); });
    const stepsEl = ov.querySelector("#plSteps");
    const drawSteps = () => { stepsEl.innerHTML = chosen.map((id, i) => `<div style="display:flex;align-items:center;gap:8px;font-size:12px;padding:6px 9px;border-radius:9px;background:var(--field);border:1px solid var(--line)"><span style="color:var(--ink-3)">${i + 1}.</span><span style="flex:1">${esc(agentName(id))}</span><button type="button" data-i="${i}" class="ico ghost plX" aria-label="Убрать шаг" style="width:26px;height:26px">✕</button></div>`).join("") || `<span style="font-size:11.5px;color:var(--ink-3)">Добавьте шаги ниже.</span>`; stepsEl.querySelectorAll(".plX").forEach((x) => x.onclick = () => { chosen.splice(+x.dataset.i, 1); drawSteps(); }); };
    ov.querySelector("#plAdd").onclick = () => { const v = ov.querySelector("#plPick").value; if (v) { chosen.push(v); drawSteps(); } };
    drawSteps();
  }
  async function runPipeline(pid, task) {
    if (busy) { toast("Дождитесь завершения текущего ответа", "warn"); return; }
    const p = pipelines.find((x) => x.id === pid) || {};
    await ensureThread(task ? task.slice(0, 50) : `Цепочка «${p.name || pid}»`);
    if (task) messages.push({ role: "user", content: `🔗 запустить цепочку «${p.name || pid}»: ${task}`, meta: {} });
    const run = { role: "assistant", content: "", meta: { _rerun: { pid, task: task || "" } } }; messages.push(run); render(); scrollDown(true);
    const bubs = $("col").querySelectorAll(".bub"); const el = bubs[bubs.length - 1];
    if (el) el.innerHTML = `<span style="display:inline-flex;gap:12px;align-items:center">${mascot("thinking", 26)}<span style="color:var(--ink-2)">цепочка «${esc(p.name || pid)}» работает… шагов: ${(p.steps || []).length}</span></span>`;
    setBusy(true, "цепочка");
    try {
      const r = await api(A_AG + "/pipelines/" + encodeURIComponent(pid) + "/run", { method: "POST", body: JSON.stringify({ context: task || "" }) });
      const steps = (r && r.steps) || [];
      messages.pop();
      await note(`цепочка «${p.name || pid}» выполнена`, { pipeline_result: { pid, name: p.name || pid, task: task || "", steps } });
      const waits = steps.some((s) => (s.delivery || []).some((d) => d.mode === "awaiting_hitl"));
      toast(waits ? "Цепочка выполнена — есть действия на ваше подтверждение" : "Цепочка выполнена", waits ? "warn" : "ok");
    } catch (e) { run.content = "Сбой цепочки: " + humanError(e); run.meta = { _rerun: { pid, task: task || "" }, notice: { icon: "⚠" } }; toast(humanError(e), "danger"); }
    setBusy(false);
    render(); scrollDown(true); loadQuota(); loadThreads(); loadHitlQueue();
  }

  // #5 авто-цепочка: семантика + LLM собирают цепочку под задачу → карточка «собрать и запустить».
  async function suggestChain(task) {
    if (!task) return;
    await ensureThread(task.slice(0, 50));
    const run = { role: "assistant", content: "", meta: {} }; messages.push(run); render(); scrollDown(true);
    const bubs = $("col").querySelectorAll(".bub"); const el = bubs[bubs.length - 1];
    if (el) el.innerHTML = `<span style="display:inline-flex;gap:12px;align-items:center">${mascot("thinking", 26)}<span style="color:var(--ink-2)">подбираю цепочку под задачу…</span></span>`;
    let r = null; try { r = await api(A_AG + "/pipelines/suggest", { method: "POST", body: JSON.stringify({ q: task }) }); } catch (e) { toast(humanError(e), "danger"); }
    messages.pop();
    const steps = (r && r.steps) || [];
    if (steps.length < 2) { await note("Под эту задачу цепочка не нужна — хватит одного агента (кнопки выше).", { notice: { icon: "💡" } }); render(); return; }
    await note("предложена цепочка", { chain_suggest: { steps, name: (r && r.name) || "Авто-цепочка", deliver: (r && r.deliver) || "chat", reason: (r && r.reason) || "", task } });
    render(); scrollDown(true);
  }
  function chainSuggestHTML(cs) {
    const i = messages.findIndex((m) => m.meta && m.meta.chain_suggest === cs);
    const chain = cs.steps.map((s) => esc(s.agent_name || agentName(s.agent_id))).join(" → ");
    return `<div style="display:flex;flex-direction:column;gap:9px">
      <div style="font-size:13px;color:var(--ink)">🔗 Предлагаю цепочку: <b>${chain}</b></div>
      ${cs.reason ? `<div style="font-size:11.5px;color:var(--ink-3)">${esc(cs.reason)}</div>` : ""}
      <div style="font-size:11.5px;color:var(--ink-2)">результат → <b>${esc(DELIVER_LABEL[cs.deliver] || cs.deliver)}</b></div>
      <button type="button" class="btn primary chainRun" data-i="${i}" style="align-self:flex-start">▶ Собрать и запустить</button></div>`;
  }
  async function saveAndRunChain(cs) {
    const steps = cs.steps.map((s, i) => ({ agent_id: s.agent_id, deliver: i < cs.steps.length - 1 ? "chat" : cs.deliver }));
    let saved = null;
    try { saved = await api(A_AG + "/pipelines", { method: "POST", body: JSON.stringify({ name: cs.name, steps }) }); } catch (e) { toast(humanError(e), "danger"); }
    await loadPipelines();
    const pid = (saved && (saved.id || (saved.pipeline && saved.pipeline.id))) || (pipelines.find((p) => p.name === cs.name) || {}).id;
    if (pid) runPipeline(pid, cs.task);
    else { await note("Не удалось сохранить цепочку — попробуйте собрать вручную («🔗 Цепочки» под полем ввода).", { notice: { icon: "⚠" } }); render(); }
  }

  // ── HITL: очередь над композером + карточки прогонов. Превью → решение строго по hitl_id (D-C3) ──
  async function loadHitlQueue() { try { const q = await api(A_AG + "/hitl"); hitlQueue = Array.isArray(q) ? q : []; } catch { hitlQueue = []; } renderHitlBar(); }
  function renderHitlBar() {
    const bar = $("hitlBar"); if (!bar) return;
    if (!hitlQueue.length) { bar.innerHTML = ""; return; }
    const rows = hitlQueue.slice(0, 6).map((h) => `<div class="hitl-row">
      <span style="font-size:15px;flex:none" aria-hidden="true">${CH_ICON(h.channel)}</span>
      <span style="flex:1;min-width:0;display:flex;flex-direction:column;gap:2px">
        <span style="font-size:12.5px;font-weight:600;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(h.title || h.channel || "Внешнее действие")}</span>
        <span style="font-size:11px;color:var(--ink-3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(agentName(h.agent_id, h.agent_id))} · ${esc(h.channel || "")}${h.to_addr ? " → " + esc(h.to_addr) : ""}</span></span>
      <button type="button" class="btn sm ok hqOk" data-id="${esc(h.id)}">Посмотреть и подтвердить</button>
      <button type="button" class="ico hqNo" data-id="${esc(h.id)}" title="Отклонить" aria-label="Отклонить">✕</button></div>`).join("");
    bar.innerHTML = `<div class="hitl-panel">
      <div class="hitl-head">🛡 Требуют вашего подтверждения · ${hitlQueue.length}
        ${hitlQueue.length > 1 ? `<button type="button" id="hqAllOk" class="btn sm" style="margin-left:auto">Подтвердить все…</button>` : ""}</div>
      <div style="display:flex;flex-direction:column;gap:6px;max-height:236px;overflow-y:auto">${rows}${hitlQueue.length > 6 ? `<span style="font-size:11.5px;color:var(--ink-3)">…и ещё ${hitlQueue.length - 6}</span>` : ""}</div></div>`;
    bar.querySelectorAll(".hqOk").forEach((b) => b.onclick = () => previewAndDecide([b.dataset.id], b));
    bar.querySelectorAll(".hqNo").forEach((b) => b.onclick = () => decideHitl([b.dataset.id], "reject", b));
    const all = bar.querySelector("#hqAllOk"); if (all) all.onclick = async () => {
      const list = hitlQueue.map((h) => `• ${CH_ICON(h.channel)} ${esc(h.title || h.channel)}${h.to_addr ? " → " + esc(h.to_addr) : ""}`).join("<br>");
      if (await confirmDialog({ title: `Подтвердить все действия (${hitlQueue.length})?`, kicker: "governance · внешние действия", text: `Отправятся наружу без просмотра каждого:<br><br>${list}`, okLabel: "Подтвердить все", danger: false })) decideHitl(hitlQueue.map((h) => h.id), "approve", all);
    };
  }
  async function previewAndDecide(ids, btn) {
    const id = ids[0];
    let it = null, legacy = false;
    try { it = await api(A_AG + "/hitl/" + encodeURIComponent(id)); }
    catch (e) {
      // старый сайдкар (≤ 1.0.5) не умеет превью — подтверждаем по данным очереди, честно предупредив
      const q = hitlQueue.find((h) => h.id === id);
      if ((e.status === 404 || e.status === 405) && q) { it = { ...q, to: q.to_addr }; legacy = true; }
      else { toast(humanError(e), "danger"); return; }
    }
    if (!it || it.ok === false) { toast("Заявка уже обработана или недоступна", "warn"); loadHitlQueue(); return; }
    if (legacy) it.body = "Предпросмотр содержимого недоступен в этой версии ABOP Desktop — обновите приложение. Подтверждение отправит отчёт агента в указанный канал.";
    const fields = [["Агент", it.agent_name || agentName(it.agent_id, it.agent_id)], ["Канал", (CH_ICON(it.channel) + " " + (it.channel || ""))], ["Адресат", it.to || "—"]];
    if (it.subject) fields.push(["Тема", it.subject]);
    if (it.format) fields.push(["Формат", it.format]);
    const ok = await ctx.gate({ title: it.title || "Внешнее действие", kicker: "подтверждение · governance", fields, html: it.html ? sanitize(it.html) : "", body: it.html ? "" : (it.body || "Содержимое не приложено."), allowLabel: "Подтвердить и отправить", denyLabel: "Отклонить", note: "Отправится только после вашего подтверждения. Персональные данные замаскированы." });
    await decideHitl(ids, ok ? "approve" : "reject", btn);
  }
  function sanitize(html) {   // превью отчёта: убираем скрипты/обработчики, остальное показываем как есть
    return String(html || "").replace(/<script[\s\S]*?<\/script>/gi, "").replace(/<style[\s\S]*?<\/style>/gi, "").replace(/\son\w+="[^"]*"/gi, "").replace(/\son\w+='[^']*'/gi, "").replace(/javascript:/gi, "");
  }
  async function decideHitl(ids, decision, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    let n = 0, last = "", err = "";
    for (const id of ids) {
      try { const r = await api(A_AG + "/hitl/" + encodeURIComponent(id) + "/approve", { method: "POST", body: JSON.stringify({ decision }) }); n++; const d = (r && (r.delivery || (r.result && r.result.delivery))); if (d) last = d; }
      catch (e) { err = humanError(e); }
    }
    if (n) toast(decision === "approve" ? `✓ Подтверждено: ${n}${last ? " · " + String(last).slice(0, 80) : ""}` : `⃠ Отклонено: ${n}`, decision === "approve" ? "ok" : "");
    if (err) toast(err, "danger");
    // отметить карточки прогонов в чате
    messages.forEach((m) => { const ra = m.meta && m.meta.run_agent; if (ra && (ra.delivery || []).some((d) => ids.includes(d.hitl_id))) { ra.hitl_done = decision; ra.hitl_result = last; } });
    render(); await loadHitlQueue();
  }
  async function decideDelivery(btn, decision) {
    const wrap = btn.closest("[data-hitl]");
    const ids = ((wrap && wrap.getAttribute("data-hitl")) || "").split(",").filter(Boolean);
    if (!ids.length) { toast("Заявка без идентификатора — подтвердите её в панели «Требуют подтверждения» над полем ввода", "warn"); loadHitlQueue(); return; }
    if (decision === "approve") previewAndDecide(ids, btn); else decideHitl(ids, "reject", btn);
  }

  // ── композер: профиль/навыки/инструменты ──
  function renderTools() {
    const prof = cur ? cur.profile : pendingProfile, sk = cur ? cur.skills : pendingSkills;
    const profSel = `<select id="prof" title="Профиль ответа" aria-label="Профиль ответа" style="padding:6px 10px;border-radius:9999px;color:var(--ink-2);font-size:11.5px">${PROFILES.map(([v, l]) => `<option value="${v}"${prof === v ? " selected" : ""}>${l}</option>`).join("")}</select>`;
    const onChips = sk.map((sid) => { const s = skills.find((x) => x.id === sid) || { id: sid }; return `<button type="button" class="skc chip on" data-id="${esc(sid)}" title="${esc(s.hint || "")} · снять" style="padding:6px 11px;font-family:var(--ui);font-size:11.5px;font-weight:600">${esc(s.title || sid)} ✕</button>`; }).join("");
    const moreBtn = `<button type="button" id="skMore" title="Каталог навыков" style="padding:6px 11px;border:1px dashed var(--line-2);border-radius:9999px;background:transparent;color:var(--ink-2);font-size:11.5px;font-weight:600">＋ навыки</button>`;
    const ic = (id, gl, ti) => `<button type="button" id="${id}" class="ico" title="${ti}" aria-label="${ti}">${gl}</button>`;
    $("tools").innerHTML = profSel + onChips + moreBtn + `<span id="agentSug" style="display:flex;gap:6px;flex-wrap:wrap"></span>` + `<span style="margin-left:auto;display:flex;align-items:center;gap:6px">${ic("tRag", "📎", "Прикрепить файл")}${ic("tAgents", "🕸", "Агенты ABOP")}${ic("tExport", "📥", "Экспорт чата")}</span><input type="file" id="fileIn" accept=".txt,.md,.csv,.json,.pdf,.docx,.xlsx" style="display:none"/>`;
    $("prof").onchange = async (e) => { if (cur) { cur.profile = e.target.value; await saveThread(cur); renderThreads(); } else pendingProfile = e.target.value; };
    $("tools").querySelectorAll(".skc").forEach((c) => c.onclick = async () => { const arr = cur ? cur.skills : pendingSkills; const i = arr.indexOf(c.dataset.id); if (i >= 0) arr.splice(i, 1); if (cur) await saveThread(cur); renderTools(); });
    $("skMore").onclick = openSkillPicker;
    $("tRag").onclick = () => $("fileIn").click();
    $("fileIn").onchange = async (e) => { const f = e.target.files[0]; await attachFile(f); e.target.value = ""; };
    $("tAgents").onclick = () => openAgents("agents");
    $("tExport").onclick = openExport;
    renderAgentSuggest($("inp") ? $("inp").value : "");
  }
  function openSkillPicker() {
    const sk = cur ? cur.skills : pendingSkills;
    const rows = skills.map((s) => { const on = sk.includes(s.id); return `<label style="display:flex;align-items:flex-start;gap:9px;padding:7px 9px;border-radius:9px;border:1px solid ${on ? "var(--accent)" : "var(--line)"};background:${on ? "var(--accent-bg)" : "var(--field)"};cursor:pointer;margin:4px 0">
      <input type="checkbox" class="skp" value="${esc(s.id)}" ${on ? "checked" : ""} style="accent-color:var(--accent);margin-top:2px"/>
      <span style="display:flex;flex-direction:column;gap:2px;min-width:0"><span style="font-size:12.5px;font-weight:600">${esc(s.title || s.id)}</span><span style="font-size:11px;color:var(--ink-3)">${esc(s.hint || "")}</span></span></label>`; }).join("");
    const ov = modal(`Каталог навыков · ${skills.length}`, `<input id="skSearch" placeholder="Поиск навыка…" style="width:100%;margin-bottom:8px" autofocus/><div id="skList" style="max-height:52vh;overflow:auto">${rows || '<span class="faint">Каталог навыков пуст или недоступен.</span>'}</div>`,
      async (b) => { const v = [...b.querySelectorAll(".skp:checked")].map((x) => x.value); if (cur) { cur.skills = v; await saveThread(cur); } else pendingSkills = v; renderTools(); }, "Применить");
    const srch = ov.querySelector("#skSearch");
    srch.oninput = () => { const q = srch.value.toLowerCase(); ov.querySelectorAll("#skList label").forEach((l) => { l.style.display = l.textContent.toLowerCase().includes(q) ? "" : "none"; }); };
  }
  // авто-подсказка агентов по тексту задачи (семантика через /match) → зелёные хештеги
  let _sugTimer = null, _sugSeq = 0;
  async function renderAgentSuggest(text) {
    const box = $("agentSug"); if (!box) return;
    const t = (text || "").trim();
    if (t.length < 5) { box.innerHTML = ""; return; }
    const seq = ++_sugSeq;
    let matched = [];
    try { const r = await api(M + "/match", { method: "POST", body: JSON.stringify({ q: t }) }); matched = ((r && r.matches) || []).filter((m) => (m.score || 0) >= 0.3).slice(0, 3); } catch { /* подсказка не критична */ }
    if (seq !== _sugSeq || !box.isConnected) return;
    box.innerHTML = matched.map((a) => `<button type="button" class="agSug chip" data-id="${esc(a.id)}" data-name="${esc(a.name)}" title="Запустить агента «${esc(a.name)}» по этой задаче (совпадение ${Math.round((a.score || 0) * 100)}%)" style="padding:6px 11px;border-color:var(--ok-line);background:var(--ok-bg);color:var(--ok-ink);font-family:var(--ui);font-size:11.5px;font-weight:600">▶ #${esc(a.name)}</button>`).join("");
    box.querySelectorAll(".agSug").forEach((b) => b.onclick = () => runAbopAgentDeliver(b.dataset.id, b.dataset.name || b.dataset.id, $("inp").value.trim(), ""));
  }

  const BINARY_RE = /\.(pdf|docx|xlsx)$/i;
  function _b64(buf) { let s = ""; const b = new Uint8Array(buf); for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]); return btoa(s); }
  async function attachFile(f) {
    if (!f) return;
    await ensureThread("Документ: " + f.name.slice(0, 40));
    const wait = { role: "assistant", content: `📎 прикрепляю «${f.name}»…`, meta: {} }; messages.push(wait); render(); scrollDown(true);
    let r;
    try {
      if (BINARY_RE.test(f.name)) { const data_b64 = _b64(await f.arrayBuffer()); r = await api(M + "/threads/" + cur.id + "/attach-file", { method: "POST", body: JSON.stringify({ name: f.name, data_b64 }) }); }
      else { const text = await f.text(); r = await api(M + "/threads/" + cur.id + "/attach", { method: "POST", body: JSON.stringify({ name: f.name, documents: [text] }) }); }
    } catch (e) { r = { ok: false, error: humanError(e) }; }
    messages.splice(messages.indexOf(wait), 1);
    const sz = r.chars ? ` · ${r.chars} симв.` : "";
    const okMsg = r.chars ? `Файл «${f.name}» распознан${sz} и добавлен в контекст чата — задайте вопрос по нему.` : `Файл «${f.name}» приложен, но текст не извлечён (возможно скан — используйте раздел «Распознать»).`;
    await note(r.ok ? okMsg : "Не удалось приложить файл: " + (r.error || ""), { notice: { icon: r.ok ? "📎" : "⚠" } });
    if (!r.ok) toast("Не удалось приложить файл: " + (r.error || ""), "danger");
    render(); scrollDown(true); loadKb();
  }
  // текст из «Источников»/«Распознать» приходит намерением (D-H8)
  async function attachText(name, text) {
    if (!text) return;
    await ensureThread("Документ: " + (name || "текст").slice(0, 40));
    let r; try { r = await api(M + "/threads/" + cur.id + "/attach", { method: "POST", body: JSON.stringify({ name: name || "текст", documents: [text] }) }); } catch (e) { r = { ok: false, error: humanError(e) }; }
    await note(r.ok ? `«${name || "Текст"}» добавлен в контекст чата (${r.chars || text.length} симв.) — задайте вопрос по нему.` : "Не удалось добавить: " + (r.error || ""), { notice: { icon: r.ok ? "📎" : "⚠" } });
    render(); scrollDown(true); loadKb(); $("inp").focus();
  }

  // ── отправка со стримингом; busy-стейт (D-H4) ──
  function setBusy(on, what) {
    busy = !!on;
    const b = $("sendBtn"), inp = $("inp");
    if (on) {
      if (curAbort) { b.textContent = "⏹"; b.title = "Остановить"; b.setAttribute("aria-label", "Остановить"); b.className = "btn danger"; b.onclick = () => curAbort && curAbort.abort(); }
      else if (curJob) { b.textContent = "⏹"; b.title = "Отменить прогон"; b.setAttribute("aria-label", "Отменить прогон"); b.className = "btn danger"; b.disabled = false; b.onclick = async () => { const j = curJob; if (!j) return; try { await api(M + "/threads/" + cur.id + "/run-job/" + encodeURIComponent(j.id) + "/cancel", { method: "POST" }); toast("Запросил отмену прогона", "warn"); } catch (e) { toast(humanError(e), "danger"); } }; }
      else { b.textContent = "…"; b.title = (what || "агент") + " работает"; b.className = "btn"; b.disabled = true; }
      inp.placeholder = what ? `${what} работает… Enter отправит сообщение после завершения` : "Ответ печатается… Esc — остановить";
    } else { b.disabled = false; b.textContent = "↑"; b.title = "Отправить · Enter"; b.setAttribute("aria-label", "Отправить"); b.className = "btn primary"; b.onclick = sendFromInput; inp.placeholder = "Опишите задачу…  (Enter — отправить, Shift+Enter — перенос)"; }
    b.style.cssText = "width:44px;height:44px;flex:none;padding:0;border-radius:12px;font-size:16px";
  }
  async function sendFromInput() {
    if (busy) { toast(curAbort ? "Ответ ещё печатается — дождитесь или остановите (Esc)" : "Дождитесь завершения запуска", "warn"); return; }
    const v = ($("inp") ? $("inp").value : "").trim(); if (!v) return;
    $("inp").value = ""; renderAgentSuggest("");
    // Вставленный по хоткею текст или длинный кусок — это работа в чате, НЕ команда агенту.
    const isPasted = /^Проанализируй этот фрагмент/i.test(v);
    if (!isPasted && v.length <= 240) {
      let matches = [];
      try { const r = await api(M + "/match", { method: "POST", body: JSON.stringify({ q: v }) }); matches = (r && r.matches) || []; } catch { /* без подсказки */ }
      const top = matches[0];
      if (top && top.score >= 0.6) { await decisionCard(v, matches); return; }
    }
    sendPrompt(v);
  }
  // карточка выбора: агент + куда положить результат (чат / Redmine / почта / BookStack / просто ответить)
  async function decisionCard(text, matches) {
    const top = matches[0];
    const alt = matches.slice(1, 3).filter((m) => m.score >= 0.25);
    await ensureThread(text.slice(0, 50));
    await note("предложен агент", { decision: { text, top, alt } });
    render(); scrollDown(true);
  }
  function decisionHTML(dc) {
    const i = messages.findIndex((m) => m.meta && m.meta.decision === dc);
    const t = dc.top; const ch = (t.channels || []);
    const chanBtn = (d, label) => ch.includes(d) ? `<button type="button" class="btn dcRun" data-id="${esc(t.id)}" data-name="${esc(t.name)}" data-deliver="${d}">${label}</button>` : "";
    const altBtns = (dc.alt || []).map((a) => `<button type="button" class="btn sm dcRun" data-id="${esc(a.id)}" data-name="${esc(a.name)}" data-deliver="">${esc(a.name)}</button>`).join("");
    return `<div data-di="${i}" style="display:flex;flex-direction:column;gap:10px">
      <div style="font-size:12px;color:var(--ink-3)">«${esc(dc.text.slice(0, 160))}»</div>
      <div style="font-size:13px;color:var(--ink)">🎯 Похоже, это задача для агента <b>${esc(t.name)}</b> <span style="font-family:var(--mono);font-size:11px;color:var(--ink-3)">${esc(t.family || "")}</span></div>
      ${ch.length ? `<div style="font-size:11px;color:var(--ink-3)">каналы агента: ${ch.map((c) => CH_ICON(c) + " " + esc(c)).join(", ")}</div>` : ""}
      ${(dc.alt && dc.alt.length) ? `<button type="button" class="btn sm dcChain" style="align-self:flex-start;border-color:var(--info-line);background:var(--info-bg);color:var(--info-ink)">🔗 Задача многошаговая — собрать цепочку</button>` : ""}
      <div style="font-size:11.5px;color:var(--ink-2)">Куда положить результат?</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button type="button" class="btn primary dcRun" data-id="${esc(t.id)}" data-name="${esc(t.name)}" data-deliver="">▶ Запустить (как настроено)</button>
        <button type="button" class="btn dcRun" data-id="${esc(t.id)}" data-name="${esc(t.name)}" data-deliver="chat">💬 Только в чат</button>
        ${chanBtn("redmine", "🎫 В Redmine")}${chanBtn("email", "✉ На почту")}${chanBtn("bookstack", "📚 В BookStack")}
      </div>
      ${altBtns ? `<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap"><span style="font-size:11px;color:var(--ink-3)">другой агент:</span>${altBtns}</div>` : ""}
      <button type="button" class="btn sm dcChat" style="align-self:flex-start;margin-top:2px">✖ Не нужен агент — продолжить в чате</button></div>`;
  }

  // Гейт расписания: если у агента активное расписание — предупреждаем о дубле (Tier 0 #4).
  function _scheduleGuard(agentId, agentNm, proceed) {
    const sch = schedules.find((s) => String(s.agent_id) === String(agentId) && s.enabled !== false);
    if (!sch) { proceed(); return; }
    const last = sch.last_run && sch.last_run.at ? ` · последний прогон ${esc(String(sch.last_run.at).slice(0, 16).replace("T", " "))}` : "";
    modal("Задача уже стоит по расписанию",
      `<div style="font-size:13px;line-height:1.6;color:var(--ink-2)">У агента <b>${esc(agentNm)}</b> настроено расписание <b>${esc(sch.cron)}</b>${last} — он выполнит задачу автоматически.<br><br>Ручной запуск сейчас создаст ещё один прогон (и, возможно, дубль письма/задачи). Всё равно запустить сейчас?</div>`,
      () => { proceed(); }, "Да, запустить сейчас");
  }
  // единая точка запуска агента (D-H6): карточка решения, подсказки, шторка, «Мои агенты», повтор
  function runAbopAgentDeliver(agentId, agentNm, task, deliver, isRerun) {
    if (busy) { toast("Дождитесь завершения текущего ответа", "warn"); return; }
    closeDrawer();
    _scheduleGuard(agentId, agentNm, () => _doRunAbopAgent(agentId, agentNm, task, deliver, isRerun));
  }
  async function _doRunAbopAgent(agentId, agentNm, task, deliver, isRerun) {
    await ensureThread(task ? task.slice(0, 50) : `Агент «${agentNm}»`);
    if (isRerun || !messages.some((m) => m.role === "user" && m.content === task)) messages.push({ role: "user", content: `▶ запустить агента «${agentNm}»${task ? ": " + task : ""}${deliver ? " · результат " + (DELIVER_LABEL[deliver] || deliver) : ""}`, meta: {} });
    const run = { role: "assistant", content: "", meta: {} }; messages.push(run); render(); scrollDown(true);
    const bubs = $("col").querySelectorAll(".bub"); const el = bubs[bubs.length - 1];
    if (el) el.innerHTML = `<span style="display:inline-flex;gap:12px;align-items:center">${mascot("thinking", 26)}<span style="color:var(--ink-2)">агент «${esc(agentNm)}» работает… это может занять до минуты</span></span>`;
    setBusy(true, "агент");
    // Через очередь ABOP (гейт масштабирования): run-agent → 202 job_id → поллинг run-job. Пока ждём,
    // показываем место в очереди; кнопка ⏹ отменяет задание. Старый ABOP отвечает done сразу.
    const status = (txt) => { if (el && el.isConnected) el.innerHTML = `<span style="display:inline-flex;gap:12px;align-items:center">${mascot("thinking", 26)}<span style="color:var(--ink-2)">${txt}</span></span>`; };
    const finish = (r) => {
      if (r.ok && r.run) {
        run.content = "[агент " + agentId + "]"; run.meta = { run_agent: { ...r.run, agent_name: r.run.agent_name || agentNm, task: task || "", deliver: deliver || "" } };
        const waits = ((r.run || {}).delivery || []).some((d) => d.mode === "awaiting_hitl");
        toast(waits ? "Агент подготовил внешнее действие — подтвердите в карточке" : `Агент «${agentNm}» завершил: находок ${r.run.findings_total ?? 0}`, waits ? "warn" : "ok");
      } else { const msg = humanError(r.error || "не удалось"); run.content = (r.status === "cancelled" ? "Прогон отменён" : "Не удалось выполнить прогон: " + msg); run.meta = { notice: { icon: r.status === "cancelled" ? "⏹" : "⚠" } }; if (r.status !== "cancelled") toast(msg, "danger"); }
    };
    try {
      const r = await api(M + "/threads/" + cur.id + "/run-agent", { method: "POST", body: JSON.stringify({ agent_id: agentId, context: task || "", deliver: deliver || "", no_cache: !!isRerun }) });
      if (!r.ok) finish(r);
      else if (r.done || r.run || !r.job_id) finish(r);   // старый сайдкар (≤1.0.6) отдаёт результат сразу
      else {
        curJob = { id: r.job_id, agent: agentId }; setBusy(true, "агент");
        if (r.deduped) toast("Такой прогон уже в очереди — присоединяюсь к нему", "warn");
        status(r.position > 1 ? `в очереди · впереди ${r.position - 1} · агент «${esc(agentNm)}»` : `агент «${esc(agentNm)}» запускается…`);
        const t0 = Date.now(); let res = null;
        while (Date.now() - t0 < 15 * 60 * 1000) {
          await new Promise((ok) => setTimeout(ok, 2500));
          if (!root.isConnected) return;
          let j; try { j = await api(M + "/threads/" + cur.id + "/run-job/" + encodeURIComponent(r.job_id) + "?agent_id=" + encodeURIComponent(agentId)); } catch (e) { status(`связь с ABOP прервалась, повторяю… (${humanError(e)})`); continue; }
          if (j.done) { res = j; break; }
          const sec = Math.round((Date.now() - t0) / 1000);
          status(j.status === "queued" ? `в очереди · впереди ${Math.max(0, (j.position || 1) - 1)} · ${sec} с` : `агент «${esc(agentNm)}» работает · ${sec} с`);
        }
        finish(res || { ok: false, error: "прогон не завершился за 15 минут — проверьте журнал прогонов позже" });
      }
    } catch (e) { run.content = "Не удалось запустить агента: " + humanError(e); run.meta = { notice: { icon: "⚠" } }; toast(humanError(e), "danger"); }
    curJob = null; setBusy(false);
    render(); scrollDown(true); loadThreads(); loadQuota(); loadHitlQueue();
  }

  async function sendPrompt(text) {
    text = (text || "").trim(); if (!text) return;
    if (busy) { toast("Дождитесь завершения текущего ответа", "warn"); return; }
    await ensureThread(text.slice(0, 50));
    const wasNew = messages.length === 0;
    messages.push({ role: "user", content: text, meta: {} });
    const asst = { role: "assistant", content: "", meta: {} }; messages.push(asst); render(); scrollDown(true);
    const _bubs = $("col").querySelectorAll(".bub"); const el = _bubs[_bubs.length - 1];
    if (el) el.innerHTML = `<span style="display:inline-flex;gap:12px;align-items:center">${mascot("thinking", 26)}<span style="color:var(--ink-2)">думает…</span></span>`;
    curAbort = new AbortController(); setBusy(true);
    try {
      const resp = await fetch(ctx.base + M + "/threads/" + cur.id + "/send-stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt: text, persona: persona() }), signal: curAbort.signal });
      if (!resp.ok || !resp.body) throw Object.assign(new Error("HTTP " + resp.status), { status: resp.status });
      const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = "";
      const caret = `<span style="display:inline-block;width:7px;height:15px;background:var(--accent-2);animation:ape-caret 1s steps(1) infinite;vertical-align:text-bottom"></span>`;
      while (true) {
        const { done, value } = await reader.read(); if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const line = buf.slice(0, i).split("\n").find((l) => l.startsWith("data:")); buf = buf.slice(i + 2);
          if (!line) continue; let d; try { d = JSON.parse(line.slice(5).trim()); } catch { continue; }
          if (d.delta) { asst.content += d.delta; if (el) { el.innerHTML = md(asst.content) + caret; scrollDown(false); } }
          else if (d.error) { const msg = d.error === "auth_required" ? humanError({ status: 401 }) : (d.error === "forbidden" ? "🛡 " + (d.message || "профиль недоступен вашей роли") : humanError(d.message || d.error)); asst.content = asst.content || ("Ошибка: " + msg); asst.meta = { notice: { icon: "⚠" } }; if (el) el.textContent = asst.content; if (d.error === "auth_required") toast(msg, "warn", { action: { label: "Войти", run: () => ctx.login() } }); }
          else if (d.done && d.meta) asst.meta = d.meta;
        }
      }
    } catch (e) { if (e.name === "AbortError") asst.content += "\n\n⏹ остановлено"; else { asst.content = asst.content || ("Сбой: " + humanError(e)); asst.meta = { notice: { icon: "⚠" } }; } }
    curAbort = null; setBusy(false); render(); scrollDown(false);
    if (wasNew && cur.title === NEW_TITLE) { try { const a = await api(M + "/threads/" + cur.id + "/autotitle", { method: "POST" }); if (a.ok) cur.title = a.title; } catch { /* noop */ } }
    loadThreads(); loadQuota();
  }

  // ── шторка Инструменты / Агенты (1:1 из макета Overlays) ──
  let drTab = "tools", drAgTab = "mine";
  function openAgents(tab) { drTab = tab || "agents"; $("drawer").style.transform = "translateX(0)"; renderDrawer(); }
  function drTabStyle(on) { return on ? "background:var(--panel);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.2)" : "background:transparent;color:var(--ink-2)"; }
  async function runRolesInThread(rl, task) {
    if (busy) { toast("Дождитесь завершения текущего ответа", "warn"); return; }
    closeDrawer(); await ensureThread(task.slice(0, 50));
    messages.push({ role: "user", content: "[роли] " + task, meta: {} });
    const run = { role: "assistant", content: "", meta: {} }; messages.push(run); render(); scrollDown(true);
    const bubs = $("col").querySelectorAll(".bub"); const el = bubs[bubs.length - 1];
    if (el) el.innerHTML = `<span style="display:inline-flex;gap:12px;align-items:center">${mascot("thinking", 26)}<span style="color:var(--ink-2)">роли работают…</span></span>`;
    setBusy(true, "роли");
    try { const r = await api(M + "/threads/" + cur.id + "/agents", { method: "POST", body: JSON.stringify({ task, roles: rl }) }); run.content = r.ok ? r.content : ("Ошибка: " + humanError(r.error)); }
    catch (e) { run.content = "Ошибка: " + humanError(e); }
    setBusy(false); render(); scrollDown(true); loadThreads(); loadQuota();
  }
  async function renderDrawer() {
    $("drTabTools").style.cssText += ";" + drTabStyle(drTab === "tools");
    $("drTabAgents").style.cssText += ";" + drTabStyle(drTab === "agents");
    $("drTabTools").onclick = () => { drTab = "tools"; renderDrawer(); };
    $("drTabAgents").onclick = () => { drTab = "agents"; renderDrawer(); };
    const b = $("drBody");
    if (drTab === "tools") {
      const tool = (glyph, title, note_, inner) => `<div style="padding:14px;border-radius:13px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:10px">
        <div style="display:flex;align-items:center;gap:10px"><span style="font-size:15px" aria-hidden="true">${glyph}</span>
          <span style="flex:1;display:flex;flex-direction:column;gap:2px"><span style="font-size:13px;font-weight:600">${title}</span><span style="font-size:11.5px;line-height:1.4;color:var(--ink-3)">${note_}</span></span></div>${inner || ""}</div>`;
      const fmts = ["md", "pdf", "docx", "xlsx"].map((f) => `<button type="button" class="expf chip" data-f="${f}" style="padding:6px 11px;font-weight:600">${f}</button>`).join("");
      b.innerHTML =
        tool("📎", "Вложения", "файл → контекст чата, ответы с опорой на него", `<button type="button" id="tlRag" class="btn" style="border-color:var(--accent);background:var(--accent-bg);color:var(--accent-ink)">Прикрепить файл</button>`) +
        tool("🔎", "Распознать", "скан или картинка → текст → в контекст чата", `<button type="button" id="tlOcr" class="btn">Открыть «Распознать»</button>`) +
        tool("🗄", "Источники", "коннекторы и рецепты ABOP, локальные файлы", `<button type="button" id="tlSrc" class="btn">Открыть «Источники»</button>`) +
        tool("📥", "Экспорт чата", "сохранить в «Загрузки»", `<div style="display:flex;gap:6px;flex-wrap:wrap">${fmts}</div>`);
      const fileIn = document.createElement("input"); fileIn.type = "file"; fileIn.accept = ".txt,.md,.csv,.json,.pdf,.docx,.xlsx"; fileIn.style.display = "none"; b.appendChild(fileIn);
      b.querySelector("#tlRag").onclick = () => fileIn.click();
      fileIn.onchange = async (e) => { const f = e.target.files[0]; closeDrawer(); await attachFile(f); e.target.value = ""; };
      b.querySelector("#tlOcr").onclick = () => { closeDrawer(); ctx.open("ocr"); };
      b.querySelector("#tlSrc").onclick = () => { closeDrawer(); ctx.open("connectors"); };
      b.querySelectorAll(".expf").forEach((x) => x.onclick = () => { closeDrawer(); exportThread(x.dataset.f); });
      return;
    }
    b.innerHTML = `<div class="faint">Загрузка агентов ABOP…</div>`;
    let cat = []; let err = "";
    try { cat = await api(M + "/abop-agents"); if (!Array.isArray(cat)) cat = []; } catch (e) { err = humanError(e); }
    if (cat.length) abopAgents = cat;
    const roleTxt = (ctx.roles && ctx.roles[0]) || "manager";
    const _mine = cat.filter((a) => a.owner), _common = cat.filter((a) => !a.owner);
    const _shown = drAgTab === "mine" ? _mine : _common;
    const _tab = (id, label, n) => `<button type="button" class="drAgTab chip${drAgTab === id ? " on" : ""}" data-t="${id}" style="flex:1;padding:7px;font-family:var(--ui);font-size:11.5px;font-weight:600">${label} · ${n}</button>`;
    const empty = err ? `<div class="danger-ink" style="font-size:12.5px">${esc(err)}</div>${!ctx.authed ? `<button type="button" id="drLogin" class="btn primary sm" style="align-self:flex-start">🔑 Войти в ABOP</button>` : ""}`
      : `<div style="font-size:12.5px;color:var(--ink-3)">${drAgTab === "mine" ? "У вас пока нет своих агентов — соберите в разделе «Мои агенты»." : "Нет общих агентов, доступных вашей роли."}</div>`;
    const catHTML = _shown.map((a) => `<label style="padding:13px;border-radius:13px;background:var(--panel);border:1px solid var(--line);display:flex;align-items:flex-start;gap:9px;cursor:pointer">
      <input type="radio" name="abopAgent" class="da" value="${esc(a.id)}" style="accent-color:var(--accent);margin-top:2px"/>
      <span style="display:flex;flex-direction:column;gap:3px;min-width:0">
        <span style="font-size:13px;font-weight:600">${esc(a.name)}${a.outward ? " 🛡" : ""}</span>
        <span style="font-size:11px;line-height:1.4;color:var(--ink-3)">${esc(a.family || "")}${a.role ? " · " + esc(a.role) : ""}${a.autonomy_max ? " · автономия " + esc(a.autonomy_max) : ""}</span>
        ${a.description ? `<span style="font-size:11px;line-height:1.4;color:var(--ink-2)">${esc(a.description)}</span>` : ""}
        ${(a.systems && a.systems.length) ? `<span style="font-size:11px;color:var(--accent-ink-2)">🔌 ${esc(a.systems.join(", "))}</span>` : ""}
      </span></label>`).join("") || empty;
    b.innerHTML = `<div style="display:flex;gap:6px;margin-bottom:2px">${_tab("mine", "Мои", _mine.length)}${_tab("common", "Общие", _common.length)}</div>
      <div style="display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:11px;background:var(--hover);border:1px solid var(--line)">
        <span style="font-size:11.5px;color:var(--ink-2)">видно по роли:</span><span style="font-family:var(--mono);font-size:11px;font-weight:600;color:var(--accent-ink-2)">${esc(roleTxt)}</span></div>
      <textarea id="drTask" rows="3" placeholder="Контекст/задача (необязательно): ссылка на документ, выделенный текст, уточнение…"></textarea>
      ${catHTML}
      <button type="button" id="drRun" class="btn primary" style="padding:11px">▶ Запустить агента в чат</button>
      <details><summary style="cursor:pointer;font-size:12px;color:var(--ink-3)">Быстрые роли (импровизация без данных)</summary><div style="margin-top:6px">${roles.map((r) => `<label style="display:flex;gap:8px;align-items:flex-start;padding:5px 0;font-size:12.5px"><input type="checkbox" class="rl" value="${esc(r.id)}"/><span><b>${esc(r.name)}</b> <span style="color:var(--ink-3)">${esc(r.brief)}</span></span></label>`).join("")}</div>
        <button type="button" id="drRunRoles" class="btn sm" style="margin-top:6px">Запустить роли</button></details>`;
    if ($("inp").value.trim()) b.querySelector("#drTask").value = $("inp").value.trim();
    b.querySelectorAll(".drAgTab").forEach((x) => x.onclick = () => { drAgTab = x.dataset.t; renderDrawer(); });
    const dl = b.querySelector("#drLogin"); if (dl) dl.onclick = () => ctx.login();
    b.querySelector("#drRun").onclick = () => {
      const task = b.querySelector("#drTask").value.trim();
      const sel = b.querySelector(".da:checked");
      if (!sel) { toast("Выберите агента", "warn"); return; }
      runAbopAgentDeliver(sel.value, agentName(sel.value), task, "");
    };
    b.querySelector("#drRunRoles").onclick = () => {
      const task = b.querySelector("#drTask").value.trim();
      const rl = [...b.querySelectorAll(".rl:checked")].map((x) => x.value);
      if (!task || !rl.length) { toast("Укажите задачу и хотя бы одну роль", "warn"); return; }
      runRolesInThread(rl, task);
    };
  }

  function openExport() {
    if (!cur) { toast("Сначала начните чат", "warn"); return; }
    const ov = modal("Экспорт чата в «Загрузки»", `<div style="display:flex;gap:8px;flex-wrap:wrap">
      <button type="button" class="btn" data-f="md">Markdown</button><button type="button" class="btn" data-f="pdf">PDF</button>
      <button type="button" class="btn" data-f="docx">Word</button><button type="button" class="btn" data-f="xlsx">Excel</button></div>`, null);
    ov.querySelectorAll("[data-f]").forEach((b) => b.onclick = async () => { ov.close(); await exportThread(b.dataset.f); });
  }
  async function exportThread(fmt) {
    if (!cur) { toast("Сначала начните чат", "warn"); return; }
    try {
      let r;
      if (fmt === "pdf") {
        if (!(window.ape && window.ape.exportPdf)) { toast("PDF доступен только в установленном приложении", "warn"); return; }
        const html = `<html><head><meta charset="utf-8"><style>body{font-family:sans-serif;padding:24px;color:#111}h2{margin:16px 0 4px;font-size:14px}</style></head><body><h1>${esc(cur.title)}</h1>${messages.map((m) => `<h2>${m.role === "user" ? "Вы" : "Ассистент"}</h2><div style="white-space:pre-wrap">${esc(m.content)}</div>`).join("")}</body></html>`;
        r = await window.ape.exportPdf(html, (cur.title || "chat").replace(/[^\w\-. ]/g, "_").slice(0, 60) + ".pdf");
      } else r = await api(M + "/threads/" + cur.id + "/export", { method: "POST", body: JSON.stringify({ format: fmt }) });
      if (r.ok) toast("Сохранено в Загрузки: " + String(r.path || "").split(/[\\/]/).pop(), "ok", { ttl: 6000 }); else toast("Не удалось: " + (r.error || ""), "danger");
    } catch (e) { toast("Не удалось: " + humanError(e), "danger"); }
  }

  $("newTh").onclick = newChat;
  // Намерения от ядра/других модулей: новый чат, анализ выделенного (Ctrl+Shift+A), текст из
  // «Источников»/«Распознать», запуск агента из «Моих агентов» (D-H6, D-H8).
  async function handleIntent(it) {
    if (!it) return;
    if (it.newChat) { await newChat(); return; }
    if (it.analyze !== undefined) {
      const inp = $("inp"); const text = (it.analyze || "").trim();
      if (!text) { inp.focus(); inp.placeholder = "Выделите текст в Word/Excel/браузере и снова нажмите Ctrl+Shift+A"; toast("Ничего не выделено", "warn"); return; }
      const clip = text.length > 4000 ? text.slice(0, 4000) + "…" : text;
      if (!cur) await newChat();
      inp.value = "Проанализируй этот фрагмент:\n\n" + clip; inp.focus(); inp.scrollTop = inp.scrollHeight; renderAgentSuggest(inp.value); return;
    }
    if (it.attach) { await attachText(it.attach.name, it.attach.text); return; }
    if (it.runAgent) { await newChat(); runAbopAgentDeliver(it.runAgent.id, it.runAgent.name || agentName(it.runAgent.id), it.runAgent.task || "", it.runAgent.deliver || ""); return; }
  }
  root.addEventListener("ape:intent", (e) => handleIntent(e.detail));
  window.__apeAnalyze = (text) => handleIntent({ analyze: text });

  $("inp").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFromInput(); } };
  $("inp").oninput = () => { clearTimeout(_sugTimer); _sugTimer = setTimeout(() => renderAgentSuggest($("inp").value), 280); };
  setBusy(false);
  render(); renderTools();            // первый экран сразу: шаблоны + живой композер
  await loadThreads();
  const intent = ctx.takeIntent ? ctx.takeIntent() : null;
  if (threads.length && !(intent && (intent.newChat || intent.runAgent))) await openThread(threads[0]);
  loadSchedules(false); loadPipelines(); loadQuota(); loadHitlQueue();
  catalogs.then(() => { renderTools(); if (messages.length) render(); });   // подписи навыков/агентов, когда каталоги доехали
  if (intent) handleIntent(intent);
  if (window.__apeSchedTimer) clearInterval(window.__apeSchedTimer);
  window.__apeSchedTimer = setInterval(() => { if (root.isConnected) { loadSchedules(true); loadHitlQueue(); } else clearInterval(window.__apeSchedTimer); }, 60000);
}
