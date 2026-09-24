// Модуль «Чат» — вёрстка 1:1 из макета APE Desktop.dc.html (структура/рецепты из ДС),
// логика привязана к сайдкару: треды, стриминг, вложения→RAG, агенты (шторка), экспорт, скиллы.
const M = "/api/modules/chat";
const A_AG = "/api/modules/agents";   // роуты агентов/расписаний ABOP через сайдкар
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

function md(t) {
  let h = esc(t);
  h = h.replace(/```([\s\S]*?)```/g, (_m, c) =>
    `<span style="display:flex;flex-direction:column;border-radius:10px;overflow:hidden;border:1px solid rgba(255,255,255,.12)"><span style="display:flex;align-items:center;gap:8px;padding:7px 11px;background:rgba(15,23,42,.9)"><span style="flex:1;font-family:var(--mono);font-size:10px;color:rgba(255,255,255,.5)">code</span><button class="codecopy" style="padding:3px 9px;border:1px solid rgba(255,255,255,.16);border-radius:7px;background:rgba(255,255,255,.06);color:rgba(255,255,255,.75);font-size:10.5px;cursor:pointer">⧉ копировать</button></span><span style="padding:12px;background:var(--code);font-family:var(--mono);font-size:11.5px;line-height:1.65;color:#c7d2fe;white-space:pre-wrap">${c.replace(/^\n/, "")}</span></span>`);
  h = h.replace(/`([^`\n]+)`/g, '<code style="background:var(--hover);padding:1px 5px;border-radius:5px;font-family:var(--mono);font-size:.92em">$1</code>');
  h = h.replace(/^\s*#{1,4}\s+(.*)$/gm, "<b>$1</b>");
  h = h.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  h = h.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2">$1</a>');
  h = h.replace(/^\s*[-*]\s+(.*)$/gm, "• $1");
  return h;
}

// универсальная модалка в стиле ДС
function modal(title, bodyHTML, onOk, okLabel) {
  const ov = document.createElement("div");
  ov.style = "position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:50;backdrop-filter:blur(2px)";
  ov.innerHTML = `<div style="width:min(560px,92vw);max-height:86vh;overflow:auto;padding:20px;border-radius:16px;background:var(--panel);backdrop-filter:blur(20px);border:1px solid var(--line);box-shadow:0 24px 70px rgba(0,0,0,.45);animation:ape-drop .18s ease">
    <div style="font-weight:700;font-size:15px;letter-spacing:-.2px;margin-bottom:14px">${esc(title)}</div>
    <div id="mBody">${bodyHTML}</div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">
      <button class="btn" id="mCancel">Отмена</button>${onOk ? `<button class="btn primary" id="mOk">${esc(okLabel || "Готово")}</button>` : ""}</div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.querySelector("#mCancel").onclick = close;
  if (onOk) ov.querySelector("#mOk").onclick = () => { if (onOk(ov.querySelector("#mBody")) !== false) close(); };
  ov.onclick = (e) => { if (e.target === ov) close(); };
  return ov;
}

// 1:1 из эталона APE Desktop (standalone): глифы/заголовки/подписи/промпты дословно.
const TEMPLATES = [
  ["✉", "Письмо клиенту", "Черновик по короткой вводной, тон на выбор", "Напиши письмо клиенту: переносим срок поставки на две недели, нужно сохранить отношения.", "email-draft"],
  ["▤", "Саммари встречи", "Из расшифровки — решения и задачи", "Сделай саммари встречи: решения, ответственные, сроки.", ""],
  ["★", "Разбор отзывов", "Кластеры боли и частота", "Разбери отзывы клиентов за квартал: кластеры проблем и частота.", ""],
  ["◈", "Проверка идеи", "Экономика, риски, что проверить первым", "Оцени идею внутреннего маркетплейса подрядчиков: экономика и риски.", "devils-advocate"],
  ["⇄", "Сравнение вариантов", "Таблица критериев и вывод", "Сравни два варианта подрядчика по стоимости, срокам и рискам.", ""],
  ["◷", "План на неделю", "Приоритеты из списка задач", "Собери план на неделю из списка задач с приоритетами.", ""],
];
const ONBOARD = [["1", "войдите через GitHub"], ["2", "выберите шаблон"], ["3", "перетащите файл"]];

export async function mount(root, ctx) {
  const { api, mascot } = ctx;
  let threads = [], cur = null, messages = [], skills = [], roles = [], search = "", curAbort = null, abopAgents = [];
  let schedules = [], _schedSeen = {}, _schedTimer = null;   // расписания + отметки последних прогонов (уведомления)
  try { skills = await api(M + "/skills"); if (!Array.isArray(skills)) skills = []; } catch {}
  try { roles = await api(M + "/agent-roles"); } catch {}
  try { abopAgents = await api(M + "/abop-agents"); if (!Array.isArray(abopAgents)) abopAgents = []; } catch {}

  root.innerHTML = `
    <aside style="flex:none;width:252px;display:flex;flex-direction:column;gap:10px;padding:14px 12px;border-right:1px solid var(--line);background:var(--rail);min-height:0">
      <button id="newTh" style="display:flex;align-items:center;justify-content:center;gap:8px;padding:10px;border:none;border-radius:10px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-size:13px;font-weight:600;cursor:pointer">＋ Новый тред</button>
      <input id="thSearch" placeholder="Поиск по тредам" style="padding:9px 12px;border-radius:10px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px"/>
      <div id="thList" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:5px;min-height:0"></div>
    </aside>
    <section id="chatSec" style="flex:1;display:flex;flex-direction:column;min-width:0;min-height:0;position:relative">
      <div id="dropHint" style="display:none;position:absolute;inset:14px;z-index:8;flex-direction:column;align-items:center;justify-content:center;gap:12px;border:2px dashed #818cf8;border-radius:18px;background:rgba(99,102,241,.14);backdrop-filter:blur(6px);pointer-events:none">
        ${mascot("scan", 52)}<span style="font-size:15px;font-weight:600;color:var(--accent-ink)">Отпустите файл — добавлю в знания треда</span>
      </div>
      <div id="scroll" style="flex:1;overflow-y:auto;padding:22px 26px 8px;min-height:0">
        <div id="col" style="max-width:760px;margin:0 auto;display:flex;flex-direction:column;gap:18px"></div>
      </div>
      <div style="flex:none;padding:10px 26px 18px">
        <div style="max-width:760px;margin:0 auto;display:flex;flex-direction:column;gap:10px">
          <div id="schedBar"></div>
          <div id="kb"></div>
          <div style="padding:12px 14px;border-radius:16px;background:var(--panel);border:1px solid var(--line-2);backdrop-filter:blur(16px);display:flex;flex-direction:column;gap:11px">
            <div id="tools" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"></div>
            <div style="display:flex;align-items:flex-end;gap:10px">
              <textarea id="inp" rows="2" placeholder="Опишите задачу…  (Enter — отправить, Shift+Enter — перенос)" style="flex:1;min-width:0;padding:10px 12px;border-radius:12px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:13.5px;line-height:1.55"></textarea>
              <button id="sendBtn" title="Отправить · Enter" style="width:44px;height:44px;flex:none;border:none;border-radius:12px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-size:16px;cursor:pointer">↑</button>
            </div>
          </div>
        </div>
      </div>
      <div id="drawer" style="position:absolute;top:0;right:0;bottom:0;width:386px;max-width:88%;transform:translateX(100%);transition:transform .28s cubic-bezier(.4,0,.2,1);background:var(--rail);backdrop-filter:blur(20px);border-left:1px solid var(--line-2);z-index:41;display:flex;flex-direction:column;box-shadow:-20px 0 50px rgba(0,0,0,.32)">
        <div style="flex:none;display:flex;align-items:center;gap:8px;padding:14px 16px;border-bottom:1px solid var(--line)">
          <div style="flex:1;display:flex;gap:4px;padding:4px;border-radius:11px;background:var(--hover);border:1px solid var(--line)">
            <button id="drTabTools" style="flex:1;padding:8px 10px;border:none;border-radius:8px;font-size:12.5px;font-weight:600;cursor:pointer">Инструменты</button>
            <button id="drTabAgents" style="flex:1;padding:8px 10px;border:none;border-radius:8px;font-size:12.5px;font-weight:600;cursor:pointer">Агенты</button>
          </div>
          <button id="drClose" title="Убрать шторку · Esc" style="width:30px;height:30px;flex:none;border:1px solid var(--line);border-radius:9px;background:transparent;color:var(--ink-2);font-size:14px;cursor:pointer">→</button>
        </div>
        <div id="drBody" style="flex:1;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:11px"></div>
      </div>
    </section>`;
  const $ = (id) => root.querySelector("#" + id);

  $("thSearch").oninput = (e) => { search = e.target.value.toLowerCase(); renderThreads(); };
  $("drClose").onclick = () => ($("drawer").style.transform = "translateX(100%)");
  const sec = $("chatSec");
  sec.addEventListener("dragover", (e) => { e.preventDefault(); if (cur) $("dropHint").style.display = "flex"; });
  sec.addEventListener("dragleave", (e) => { if (!sec.contains(e.relatedTarget)) $("dropHint").style.display = "none"; });
  sec.addEventListener("drop", async (e) => { e.preventDefault(); $("dropHint").style.display = "none"; if (!cur) return; for (const f of [...(e.dataTransfer.files || [])]) if (/.(txt|md|csv|json|pdf|docx|xlsx)$/i.test(f.name)) await attachFile(f); });

  if (window.__apeChatKey) document.removeEventListener("keydown", window.__apeChatKey);
  window.__apeChatKey = (e) => {
    if (!root.isConnected) return;
    if (e.ctrlKey && (e.key === "n" || e.key === "N")) { e.preventDefault(); $("newTh").click(); }
    else if (e.key === "Escape") { if ($("drawer").style.transform === "translateX(0px)") $("drawer").style.transform = "translateX(100%)"; else if (curAbort) curAbort.abort(); }
  };
  document.addEventListener("keydown", window.__apeChatKey);

  // ── треды ──
  function renderThreads() {
    const list = threads.filter((t) => !search || (t.title || "").toLowerCase().includes(search));
    const fav = list.filter((t) => t.favorite), rest = list.filter((t) => !t.favorite);
    const group = (folder, arr) => arr.length ? `<div style="display:flex;flex-direction:column;gap:5px">
      <span style="padding:0 4px;font-family:var(--mono);font-size:9px;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3)">${folder}</span>
      ${arr.map((t) => `<div class="thr" data-id="${t.id}" style="display:flex;align-items:center;gap:6px;padding:8px 9px;border:1px solid ${cur && t.id === cur.id ? "var(--line-2)" : "var(--line)"};border-radius:10px;background:${cur && t.id === cur.id ? "var(--hover)" : "transparent"}">
        <button class="thopen" data-id="${t.id}" title="Двойной клик — переименовать" style="flex:1;min-width:0;padding:0;border:none;background:transparent;text-align:left;display:flex;flex-direction:column;gap:2px;cursor:pointer">
          <span style="font-size:12.5px;font-weight:600;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.title)}</span>
          <span style="font-size:10.5px;color:var(--ink-3)">${t.profile}${t.skills && t.skills.length ? " · " + t.skills.length + " ск." : ""}</span>
        </button>
        <button class="thfav" data-id="${t.id}" title="В избранное" style="width:22px;height:22px;flex:none;border:none;border-radius:6px;background:transparent;color:${t.favorite ? "var(--warn-ink)" : "var(--ink-3)"};font-size:12px;cursor:pointer">★</button>
        <button class="thdel" data-id="${t.id}" title="Удалить тред" style="width:22px;height:22px;flex:none;border:none;border-radius:6px;background:transparent;color:var(--ink-3);font-size:11px;cursor:pointer">✕</button>
      </div>`).join("")}</div>` : "";
    $("thList").innerHTML = (group("избранное", fav) + group("треды", rest)) ||
      `<div style="padding:20px 10px;text-align:center;font-size:12px;color:var(--ink-3)">${search ? "Ничего не нашлось по «" + esc(search) + "»" : "Тредов пока нет"}</div>`;
    $("thList").querySelectorAll(".thopen").forEach((b) => { b.onclick = () => openThread(threads.find((x) => x.id == b.dataset.id)); b.ondblclick = () => renameThread(threads.find((x) => x.id == b.dataset.id)); });
    $("thList").querySelectorAll(".thfav").forEach((b) => b.onclick = async () => { const t = threads.find((x) => x.id == b.dataset.id); t.favorite = t.favorite ? 0 : 1; await saveThread(t); await loadThreads(); });
    $("thList").querySelectorAll(".thdel").forEach((b) => b.onclick = async () => { if (confirm("Удалить тред?")) { await api(M + "/threads/" + b.dataset.id, { method: "DELETE" }); if (cur && cur.id == b.dataset.id) cur = null; await loadThreads(); render(); } });
  }
  function renameThread(t) {
    modal("Название треда", `<input id="tt" value="${esc(t.title)}" style="width:100%"/>
      <button class="btn sm" id="autoT" style="margin-top:8px">✨ Авто-тема</button>`, (b) => {
      const v = b.querySelector("#tt").value.trim(); if (!v) return false; t.title = v; saveThread(t).then(loadThreads);
    }, "Сохранить");
    document.querySelector("#autoT").onclick = async () => { const r = await api(M + "/threads/" + t.id + "/autotitle", { method: "POST" }); if (r.ok) document.querySelector("#tt").value = r.title; };
  }
  async function saveThread(t) { await api(M + "/threads/" + t.id, { method: "PATCH", body: JSON.stringify({ title: t.title, profile: t.profile, skills: t.skills, favorite: t.favorite || 0 }) }); }
  async function loadThreads() { threads = await api(M + "/threads"); renderThreads(); }
  async function openThread(t) { cur = { ...t, skills: t.skills || [] }; renderThreads(); renderTools(); messages = await api(M + "/threads/" + t.id + "/messages"); render(); renderKb(); }

  // очистка текста находки: если пришёл сырой JSON ({"находки":[...]}) — вынимаем наблюдения читаемо
  function cleanFinding(t) {
    t = (t || "").trim();
    if (t[0] === "{" || t[0] === "[") {
      try {
        const o = JSON.parse(t);
        const arr = Array.isArray(o) ? o : (o["находки"] || o.findings || []);
        if (Array.isArray(arr) && arr.length) {
          return arr.map((x) => (typeof x === "string" ? x : (x["наблюдение"] || x.observation || x["запись"] || JSON.stringify(x)))).join("; ");
        }
      } catch (e) { /* не JSON — вернём как есть */ }
    }
    return t.replace(/^[•\s]+/, "");
  }
  // карточка прогона реального ABOP-агента (находки/доставка/HITL) — чат как среда управления
  function runCard(s) {
    const fnd = (s.findings || []).map((t) => `<div style="font-size:12px;color:var(--ink);border-left:2px solid var(--accent);padding-left:10px;margin:4px 0">${esc(cleanFinding(t).slice(0, 400))}</div>`).join("");
    const dl = (s.delivery || []).map((d) => { const wait = d.mode === "awaiting_hitl"; return `<div style="font-size:11.5px;color:${wait ? "#fbbf24" : "var(--ink-2)"}">${esc(d.channel)}${d.to ? " → " + esc(d.to) : ""} · ${wait ? "ожидает подтверждения (HITL)" : esc(d.mode)}</div>`; }).join("");
    const hasWait = (s.delivery || []).some((d) => d.mode === "awaiting_hitl");
    const verd = s.verdict && s.verdict.within_envelope != null ? `<span style="font-family:var(--mono);font-size:10px;color:${s.verdict.within_envelope ? "#6ee7b7" : "#fca5a5"}">конверт: ${s.verdict.within_envelope ? "в рамках" : "превышен"}${s.verdict.autonomy_used ? " · " + esc(s.verdict.autonomy_used) : ""}</span>` : "";
    return `<div style="display:flex;flex-direction:column;gap:8px">
      <div style="display:flex;align-items:center;gap:8px"><span style="font-family:var(--mono);font-size:9.5px;letter-spacing:.6px;text-transform:uppercase;color:var(--ink-3)">агент ${esc(s.agent_id || "")}</span>${s.cached ? '<span style="font-size:10px;color:var(--ink-3)">· из кэша</span>' : ""}${s.trace_id ? `<span style="font-size:10px;color:var(--ink-3)">· trace ${esc((s.trace_id || "").slice(0, 8))}</span>` : ""}</div>
      <div style="font-size:12.5px;color:var(--ink-2)">находок: <b>${esc(String(s.findings_total ?? 0))}</b>${s.investigations_total != null ? ` · расследований: <b>${esc(String(s.investigations_total))}</b>` : ""} ${verd}</div>
      ${fnd || '<div style="font-size:12px;color:var(--ink-3)">Находок не выявлено.</div>'}
      ${dl ? `<div style="font-family:var(--mono);font-size:9.5px;letter-spacing:.6px;text-transform:uppercase;color:var(--ink-3);margin-top:4px">доставка</div>${dl}` : ""}
      ${hasWait ? `<div data-agent="${esc(s.agent_id || "")}" style="margin-top:6px;padding:11px 12px;border-radius:11px;border:1px solid rgba(245,158,11,.45);background:rgba(245,158,11,.12);display:flex;flex-direction:column;gap:8px">
        <span style="font-size:12px;color:#fbbf24;font-weight:600">🛡 Конверт агента требует вашего подтверждения внешнего действия</span>
        <span style="display:flex;gap:8px"><button class="hitlOk" style="padding:8px 14px;border:none;border-radius:10px;background:rgba(16,185,129,.18);color:#6ee7b7;font-size:12px;font-weight:600;cursor:pointer">✓ Подтвердить действие</button>
          <button class="hitlNo" style="padding:8px 14px;border:1px solid var(--line);border-radius:10px;background:transparent;color:var(--ink-2);font-size:12px;font-weight:600;cursor:pointer">Отклонить</button></span></div>` : ""}
      <button class="mkRecurring" data-agent="${esc(s.agent_id || "")}" data-name="${esc(s.agent_name || s.agent_id || "")}" style="align-self:flex-start;margin-top:4px;padding:7px 12px;border:1px solid var(--line);border-radius:10px;background:transparent;color:var(--ink-2);font-size:11.5px;font-weight:600;cursor:pointer">🔁 Сделать регулярной</button></div>`;
  }

  // ── сообщения ──
  function bubble(m, idx) {
    const mine = m.role === "user";
    const radius = mine ? "16px 16px 4px 16px" : "16px 16px 16px 4px";
    const bg = mine ? "rgba(99,102,241,.16)" : "var(--panel)";
    const bd = mine ? "rgba(129,140,248,.3)" : "var(--line)";
    const inner = mine ? `<span style="font-size:13.5px;line-height:1.6;white-space:pre-wrap">${esc(m.content)}</span>`
      : (m.meta && m.meta.run_agent ? runCard(m.meta.run_agent)
        : (m.meta && m.meta.decision ? decisionHTML(m.meta.decision) : md(m.content)));
    const cost = m.meta && m.meta.model ? `<span style="margin-left:4px;font-family:var(--mono);font-size:10.5px;color:var(--ink-3)">${m.meta.model} · ${m.meta.cost_rub ?? 0} ₽</span>` : "";
    const acts = mine
      ? `<button data-edit="${idx}" style="padding:4px 9px;border:1px solid transparent;border-radius:8px;background:transparent;color:var(--ink-3);font-size:11px;cursor:pointer">✎ изменить</button>`
      : `<button data-copy="${idx}" style="padding:4px 9px;border:1px solid transparent;border-radius:8px;background:transparent;color:var(--ink-3);font-size:11px;cursor:pointer">⧉ копировать</button><button data-regen="${idx}" style="padding:4px 9px;border:1px solid transparent;border-radius:8px;background:transparent;color:var(--ink-3);font-size:11px;cursor:pointer">↻ ещё раз</button>${cost}`;
    return `<div style="display:flex;flex-direction:column;align-items:${mine ? "flex-end" : "flex-start"};gap:7px;animation:ape-in .3s ease-out">
      <div class="bub" style="max-width:88%;padding:13px 16px;border-radius:${radius};background:${bg};border:1px solid ${bd};backdrop-filter:blur(16px);white-space:pre-wrap;font-size:13.5px;line-height:1.6">${inner}</div>
      <div style="display:flex;align-items:center;gap:6px">${acts}</div></div>`;
  }
  function emptyState() {
    return `<div style="display:flex;flex-direction:column;gap:22px;padding:26px 0;animation:ape-in .4s ease-out">
      <div style="display:flex;align-items:center;gap:18px">${mascot("idle", 66)}
        <div style="display:flex;flex-direction:column;gap:6px">
          <h1 style="margin:0;font-size:24px;font-weight:800;letter-spacing:-.6px">С чего начнём?</h1>
          <p style="margin:0;max-width:460px;font-size:13.5px;line-height:1.55;color:var(--ink-2)">Опишите задачу словами или возьмите готовый шаблон. Файлы можно просто перетащить в окно.</p></div></div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(214px,1fr));gap:12px">
        ${TEMPLATES.map((t, i) => `<button data-tpl="${i}" style="display:flex;flex-direction:column;align-items:flex-start;gap:7px;padding:15px;border:1px solid var(--line);border-radius:14px;background:var(--panel);backdrop-filter:blur(16px);text-align:left;cursor:pointer">
          <span style="font-size:17px">${t[0]}</span><span style="font-size:13.5px;font-weight:600;color:var(--ink)">${t[1]}</span><span style="font-size:11.5px;line-height:1.45;color:var(--ink-3)">${t[2]}</span></button>`).join("")}
      </div>
      <div style="display:flex;align-items:center;gap:16px;padding:14px 18px;border-radius:14px;background:var(--panel);border:1px solid var(--line);flex-wrap:wrap">
        <span style="font-family:var(--mono);font-size:9.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3)">первый запуск</span>
        ${ONBOARD.map((o) => `<span style="display:inline-flex;align-items:center;gap:8px;font-size:12.5px;color:var(--ink-2)"><span style="width:20px;height:20px;border-radius:7px;background:var(--hover);color:var(--accent-ink);font-family:var(--mono);font-size:10px;font-weight:700;display:flex;align-items:center;justify-content:center">${o[0]}</span>${o[1]}</span>`).join("")}
      </div></div>`;
  }
  function render() {
    if (!cur) { $("col").innerHTML = `<div style="margin:auto;text-align:center;color:var(--ink-3);padding:60px 0">Создай или выбери тред слева.</div>`; return; }
    $("col").innerHTML = messages.length ? messages.map(bubble).join("") : emptyState();
    $("col").querySelectorAll("[data-tpl]").forEach((e) => e.onclick = async () => { const [, , , prompt, skill] = TEMPLATES[+e.dataset.tpl]; if (skill && !cur.skills.includes(skill)) { cur.skills.push(skill); await saveThread(cur); renderTools(); } $("inp").value = prompt; $("inp").focus(); });
    $("col").querySelectorAll("[data-copy]").forEach((e) => e.onclick = () => { navigator.clipboard.writeText(messages[+e.dataset.copy].content); const o = e.textContent; e.textContent = "✓"; setTimeout(() => e.textContent = o, 1200); });
    $("col").querySelectorAll("[data-regen]").forEach((e) => e.onclick = () => { const p = messages[+e.dataset.regen - 1]; if (p && p.role === "user") sendPrompt(p.content); });
    $("col").querySelectorAll("[data-edit]").forEach((e) => e.onclick = () => { $("inp").value = messages[+e.dataset.edit].content; $("inp").focus(); });
    $("col").querySelectorAll(".codecopy").forEach((b) => b.onclick = () => { const code = b.closest("span").parentElement.querySelector("span:last-child"); navigator.clipboard.writeText(code ? code.textContent : ""); const o = b.textContent; b.textContent = "✓"; setTimeout(() => b.textContent = o, 1200); });
    $("col").querySelectorAll(".hitlOk").forEach((b) => b.onclick = () => decideDelivery(b, "approve"));
    $("col").querySelectorAll(".hitlNo").forEach((b) => b.onclick = () => decideDelivery(b, "reject"));
    $("col").querySelectorAll(".mkRecurring").forEach((b) => b.onclick = () => recurringModal(b.dataset.agent, b.dataset.name));
    // дерево решений: запуск подобранного агента с выбранной доставкой / другой агент / просто ответить
    $("col").querySelectorAll(".dcRun").forEach((b) => b.onclick = () => { const dc = _lastDecision(); runAbopAgentDeliver(b.dataset.id, b.dataset.name, dc ? dc.text : "", b.dataset.deliver || ""); });
    $("col").querySelectorAll(".dcAlt").forEach((b) => b.onclick = () => { const dc = _lastDecision(); runAbopAgentDeliver(b.dataset.id, b.dataset.name, dc ? dc.text : "", ""); });
    $("col").querySelectorAll(".dcChat").forEach((b) => b.onclick = () => { const dc = _lastDecision(); if (dc) sendPrompt(dc.text); });
    $("scroll").scrollTop = $("scroll").scrollHeight;
  }

  // «Сделать регулярной»: выбор периодичности + куда доставлять (в чат / в систему) → крон-триггер
  function recurringModal(agentId, agentName) {
    const body = `<div style="display:flex;flex-direction:column;gap:12px">
      <div><label style="font-size:11.5px;color:var(--ink-3)">Когда запускать</label>
        <select id="rcCron" style="width:100%;margin-top:4px;padding:8px 10px;border-radius:9px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px">
          <option value="09:00">Ежедневно в 09:00</option><option value="18:00">Ежедневно в 18:00</option>
          <option value="*/60">Каждый час</option><option value="*/30">Каждые 30 минут</option>
          <option value="*/15">Каждые 15 минут (тест)</option></select></div>
      <div><label style="font-size:11.5px;color:var(--ink-3)">Куда результат</label>
        <div id="rcDeliver" style="display:flex;gap:8px;margin-top:4px">
          <button data-d="chat" class="rcd" style="flex:1;padding:8px;border:1px solid var(--accent);border-radius:9px;background:var(--accent-bg);color:var(--accent-ink);font-size:12px;font-weight:600;cursor:pointer">💬 В чат (уведомить)</button>
          <button data-d="system" class="rcd" style="flex:1;padding:8px;border:1px solid var(--line);border-radius:9px;background:var(--field);color:var(--ink-2);font-size:12px;font-weight:600;cursor:pointer">↗ В систему (почта/трекер)</button></div></div>
      <div style="font-size:11px;color:var(--ink-3);line-height:1.5">Задача станет узлом-расписанием в графе агента (виден на «Строю»), запуск на сервере ABOP. Внешняя доставка — под HITL узлов агента.</div></div>`;
    let deliver = "chat";
    const ov = modal(`🔁 Регулярный запуск «${agentName}»`, body, async () => {
      const cron = ov.querySelector("#rcCron").value;
      const r = await api(A_AG + "/trigger", { method: "POST", body: JSON.stringify({ agent_id: agentId, cron, deliver, title: agentName }) });
      const res = (r && r.result) || {};
      messages.push({ role: "assistant", content: r && r.ok ? `🔁 Расписание создано: «${agentName}» — ${cron}, результат ${deliver === "chat" ? "в чат" : "в систему"}. Новая версия ${res.agent_id || ""} на «Строю».` : "Не удалось создать расписание: " + ((r && r.error) || ""), meta: {} });
      render(); loadSchedules();
    }, "Создать расписание");
    ov.querySelectorAll(".rcd").forEach((b) => b.onclick = () => { deliver = b.dataset.d; ov.querySelectorAll(".rcd").forEach((x) => { const on = x.dataset.d === deliver; x.style.border = "1px solid " + (on ? "var(--accent)" : "var(--line)"); x.style.background = on ? "var(--accent-bg)" : "var(--field)"; x.style.color = on ? "var(--accent-ink)" : "var(--ink-2)"; }); });
  }

  // ── Мои расписания: загрузка + уведомления о завершении регулярных задач ──
  async function loadSchedules(notify) {
    let prev = _schedSeen;
    try { schedules = await api(A_AG + "/schedules"); if (!Array.isArray(schedules)) schedules = []; } catch { schedules = []; }
    // уведомление: если у расписания появился НОВЫЙ последний прогон (сравниваем время) — сообщаем в чат
    schedules.forEach((s) => {
      const key = s.agent_id + "/" + s.trigger_id;
      const at = s.last_run && s.last_run.at;
      if (notify && at && prev[key] && prev[key] !== at) {
        const f = (s.last_run && s.last_run.findings);
        messages.push({ role: "assistant", content: `🔔 Регулярная задача «${s.agent}» (${s.cron}) выполнена${f != null ? ` — находок: ${f}` : ""}.`, meta: {} });
        if (cur) render();
      }
      _schedSeen[key] = at || prev[key];
    });
    renderSchedBar();
  }
  function renderSchedBar() {
    const bar = $("schedBar"); if (!bar) return;
    if (!schedules.length) { bar.innerHTML = ""; return; }
    bar.innerHTML = `<details style="border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:8px 12px">
      <summary style="cursor:pointer;font-size:12px;color:var(--ink-2);font-weight:600">🔁 Мои расписания · ${schedules.length}</summary>
      <div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">
      ${schedules.map((s) => `<div style="display:flex;align-items:center;gap:8px;font-size:11.5px">
        <span style="flex:1;min-width:0;color:var(--ink)">${esc(s.agent)} <span style="color:var(--ink-3)">· ${esc(s.cron)} · ${s.deliver === "chat" ? "в чат" : "в систему"}${s.enabled ? "" : " · выкл"}</span>${s.last_run && s.last_run.at ? `<span style="color:var(--ink-3)"> · посл.: ${esc(String(s.last_run.at).slice(11, 16))}${s.last_run.findings != null ? " (" + s.last_run.findings + " нах.)" : ""}</span>` : ""}</span>
        <button class="schDel" data-a="${esc(s.agent_id)}" data-t="${esc(s.trigger_id)}" title="Убрать расписание" style="width:20px;height:20px;border:none;border-radius:6px;background:var(--hover);color:var(--ink-3);font-size:10px;cursor:pointer">✕</button></div>`).join("")}
      </div></details>`;
    bar.querySelectorAll(".schDel").forEach((b) => b.onclick = async () => { await api(A_AG + "/trigger/" + encodeURIComponent(b.dataset.a) + "/" + encodeURIComponent(b.dataset.t), { method: "DELETE" }); loadSchedules(); });
  }

  // HITL прямо в основном окне чата: подтвердить/отклонить внешнее действие ЭТОГО прогона.
  // Точечно — по agent_id прогона (не подтверждаем чужую очередь скопом).
  async function decideDelivery(btn, decision) {
    const wrap = btn.closest("[data-agent]");
    const agentId = wrap ? wrap.getAttribute("data-agent") : "";
    btn.textContent = decision === "approve" ? "Подтверждаю…" : "Отклоняю…"; btn.disabled = true;
    try {
      let q = []; try { q = await api("/api/modules/agents/hitl"); } catch {}
      if (!Array.isArray(q)) q = [];
      const mine = q.filter((it) => !agentId || it.agent_id === agentId || !it.agent_id);
      let n = 0;
      for (const it of mine) { try { await api("/api/modules/agents/hitl/" + encodeURIComponent(it.id) + "/approve", { method: "POST", body: JSON.stringify({ decision }) }); n++; } catch {} }
      if (wrap) wrap.innerHTML = `<span style="font-size:12px;color:${decision === "approve" ? "#6ee7b7" : "var(--ink-3)"};font-weight:600">${decision === "approve" ? "✓ Действие подтверждено" : "⃠ Действие отклонено"}${n ? " (" + n + ")" : ""}</span>`;
    } catch (e) { btn.textContent = "Ошибка"; btn.disabled = false; }
  }

  async function renderKb() {
    if (!cur) { $("kb").innerHTML = ""; return; }
    let fs = []; try { fs = await api(M + "/threads/" + cur.id + "/files"); } catch {}
    $("kb").innerHTML = fs.length ? `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span style="font-family:var(--mono);font-size:9.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3)">знания треда</span>
      ${fs.map((f) => `<span style="display:inline-flex;align-items:center;gap:8px;padding:5px 10px;border:1px solid var(--line);border-radius:9999px;background:var(--panel);font-size:11.5px;color:var(--ink-2)">${esc(f.name)}<span style="font-family:var(--mono);font-size:9.5px;color:var(--ink-3)">${f.chunks}фр.</span><button class="kbrm" data-id="${f.id}" title="Убрать" style="width:15px;height:15px;border:none;border-radius:9999px;background:var(--hover);color:var(--ink-3);font-size:9px;cursor:pointer">✕</button></span>`).join("")}</div>` : "";
    $("kb").querySelectorAll(".kbrm").forEach((x) => x.onclick = async () => { await api(M + "/threads/" + cur.id + "/files/" + x.dataset.id, { method: "DELETE" }); renderKb(); });
  }

  // ── композер (профиль/скиллы/инструменты) ──
  function renderTools() {
    if (!cur) return;
    const prof = `<select id="prof" title="Профиль ответа" style="padding:6px 10px;border-radius:9999px;border:1px solid var(--line);background:var(--field);color:var(--ink-2);font-size:11.5px">
      <option value="standard"${cur.profile === "standard" ? " selected" : ""}>standard · 30B</option>
      <option value="code"${cur.profile === "code" ? " selected" : ""}>code</option>
      <option value="research"${cur.profile === "research" ? " selected" : ""}>ask</option></select>`;
    // компактно: только ВКЛЮЧЁННЫЕ навыки чипами (снять кликом) + кнопка «＋ навыки» (модалка каталога).
    // Раньше рендерился весь каталог (десятки тегов простынёй) — убрано.
    const onChips = cur.skills.map((sid) => { const s = skills.find((x) => x.id === sid) || { id: sid }; return `<button class="skc" data-id="${esc(sid)}" title="${esc(s.hint || "")}" style="padding:6px 11px;border:1px solid var(--accent);border-radius:9999px;background:var(--accent-bg);color:var(--accent-ink);font-size:11.5px;font-weight:600;cursor:pointer">${esc(sid)} ✕</button>`; }).join("");
    const moreBtn = `<button id="skMore" title="Каталог навыков" style="padding:6px 11px;border:1px dashed var(--line-2);border-radius:9999px;background:transparent;color:var(--ink-2);font-size:11.5px;font-weight:600;cursor:pointer">＋ навыки${cur.skills.length ? "" : ""}</button>`;
    const ic = (id, gl, ti) => `<button id="${id}" title="${ti}" style="width:30px;height:30px;border:1px solid var(--line);border-radius:9px;background:transparent;color:var(--ink-2);font-size:13px;cursor:pointer">${gl}</button>`;
    $("tools").innerHTML = prof + onChips + moreBtn + `<span id="agentSug" style="display:flex;gap:6px;flex-wrap:wrap"></span>` + `<span style="margin-left:auto;display:flex;align-items:center;gap:6px">${ic("tRag", "📎", "Прикрепить файл")}${ic("tAgents", "🕸", "Агенты ABOP")}${ic("tExport", "📥", "Экспорт")}</span><input type="file" id="fileIn" accept=".txt,.md,.csv,.json,.pdf,.docx,.xlsx" style="display:none"/>`;
    $("prof").onchange = async (e) => { cur.profile = e.target.value; await saveThread(cur); };
    $("tools").querySelectorAll(".skc").forEach((c) => c.onclick = async () => { const i = cur.skills.indexOf(c.dataset.id); if (i >= 0) cur.skills.splice(i, 1); await saveThread(cur); renderTools(); });
    const skMore = $("skMore"); if (skMore) skMore.onclick = openSkillPicker;
    $("tRag").onclick = () => $("fileIn").click();
    $("fileIn").onchange = async (e) => { const f = e.target.files[0]; await attachFile(f); e.target.value = ""; };
    $("tAgents").onclick = openAgents;
    $("tExport").onclick = openExport;
    renderAgentSuggest($("inp") ? $("inp").value : "");
  }

  // каталог навыков — модалка (чекбоксы), чтобы не забивать композер простынёй тегов
  function openSkillPicker() {
    const rows = skills.map((s) => { const on = cur.skills.includes(s.id); return `<label style="display:flex;align-items:flex-start;gap:9px;padding:7px 9px;border-radius:9px;border:1px solid ${on ? "rgba(99,102,241,.4)" : "var(--line)"};background:${on ? "rgba(99,102,241,.1)" : "var(--field)"};cursor:pointer;margin:4px 0">
      <input type="checkbox" class="skp" value="${esc(s.id)}" ${on ? "checked" : ""} style="accent-color:#6366f1;margin-top:2px"/>
      <span style="display:flex;flex-direction:column;gap:2px;min-width:0"><span style="font-size:12.5px;font-weight:600">${esc(s.id)}</span><span style="font-size:11px;color:var(--ink-3)">${esc(s.hint || "")}</span></span></label>`; }).join("");
    const ov = modal(`Каталог навыков · ${skills.length}`, `<input id="skSearch" placeholder="Поиск навыка…" style="width:100%;margin-bottom:8px"/><div id="skList" style="max-height:52vh;overflow:auto">${rows}</div>`,
      async (b) => { cur.skills = [...b.querySelectorAll(".skp:checked")].map((x) => x.value); await saveThread(cur); renderTools(); }, "Применить");
    const srch = ov.querySelector("#skSearch");
    if (srch) srch.oninput = () => { const q = srch.value.toLowerCase(); ov.querySelectorAll("#skList label").forEach((l) => { l.style.display = l.textContent.toLowerCase().includes(q) ? "" : "none"; }); };
  }

  // авто-подсказка агентов по тексту задачи: матчим по ключевым словам семьи/имени/роли → зелёные
  // хештеги; клик — запуск агента с текущим запросом как контекстом (цепочка активируется по контексту).
  const FAM_KW = {
    management: ["почт", "письм", "задач", "тикет", "джир", "jira", "трекер", "редмайн", "redmine", "статус", "совещ", "митинг", "переписк", "ветк", "напоминан", "отчёт", "дайджест"],
    finance: ["оплат", "счёт", "счет", "платёж", "ндс", "сверк", "дебитор", "финанс", "бюджет", "деньг", "проводк"],
    audit: ["аудит", "1с", "1c", "расхожд", "первичк", "фактур", "контрагент", "следовател"],
    analytics: ["аналит", "метрик", "дашборд", "показател", "динамик", "прогноз"],
    research: ["ресёрч", "рынок", "исслед", "конкурент", "клиент", "icp"],
  };
  function matchAgents(text) {
    const t = (text || "").toLowerCase().trim();
    if (t.length < 5 || !abopAgents.length) return [];
    const scored = abopAgents.map((a) => {
      let s = 0;
      for (const k of (FAM_KW[a.family] || [])) if (t.includes(k)) s += 1;
      for (const w of ((a.name || "") + " " + (a.role || "")).toLowerCase().split(/[^a-zа-яё0-9]+/)) if (w.length > 3 && t.includes(w)) s += 2;
      return { a, s };
    }).filter((x) => x.s > 0).sort((x, y) => y.s - x.s);
    return scored.slice(0, 3).map((x) => x.a);
  }
  let _sugTimer = null;
  function renderAgentSuggest(text) {
    const box = $("agentSug"); if (!box) return;
    const matched = matchAgents(text);
    box.innerHTML = matched.map((a) => `<button class="agSug" data-id="${esc(a.id)}" title="Запустить агента «${esc(a.name)}» по этой задаче" style="padding:6px 11px;border:1px solid rgba(16,185,129,.5);border-radius:9999px;background:rgba(16,185,129,.16);color:#6ee7b7;font-size:11.5px;font-weight:600;cursor:pointer">▶ #${esc(a.name)}</button>`).join("");
    box.querySelectorAll(".agSug").forEach((b) => b.onclick = () => { const a = abopAgents.find((x) => x.id === b.dataset.id) || {}; runAbopAgent(b.dataset.id, a.name || b.dataset.id, $("inp").value.trim()); });
  }

  const BINARY_RE = /\.(pdf|docx|xlsx)$/i;   // извлечение текста на стороне сайдкара
  function _b64(buf) { let s = ""; const b = new Uint8Array(buf); for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]); return btoa(s); }

  async function attachFile(f) {
    if (!f || !cur) return;
    messages.push({ role: "assistant", content: `📎 прикрепляю «${f.name}»…`, meta: {} }); render();
    let r;
    if (BINARY_RE.test(f.name)) {
      const data_b64 = _b64(await f.arrayBuffer());   // pdf/docx/xlsx → байты, сайдкар извлечёт текст
      r = await api(M + "/threads/" + cur.id + "/attach-file", { method: "POST", body: JSON.stringify({ name: f.name, data_b64 }) });
    } else {
      const text = await f.text();                    // txt/md/csv/json → текст как есть
      r = await api(M + "/threads/" + cur.id + "/attach", { method: "POST", body: JSON.stringify({ name: f.name, documents: [text] }) });
    }
    messages.pop();
    const sz = r.chars ? ` · ${r.chars} симв.` : "";
    const okMsg = (r.chars ? `📎 Файл «${f.name}» распознан${sz} и добавлен в контекст диалога — задайте вопрос по нему.`
      : `📎 Файл «${f.name}» приложен, но текст не извлечён (возможно скан — используйте «Распознать»).`);
    messages.push({ role: "assistant", content: r.ok ? okMsg : "Не удалось: " + r.error, meta: {} });
    render(); renderKb();
  }

  // ── отправка со стримингом ──
  function setBusy(on) {
    const b = $("sendBtn");
    if (on) { b.textContent = "⏹"; b.title = "Остановить"; b.style.background = "rgba(239,68,68,.14)"; b.style.color = "var(--danger-ink)"; b.style.border = "1px solid rgba(239,68,68,.35)"; b.onclick = () => curAbort && curAbort.abort(); }
    else { b.textContent = "↑"; b.title = "Отправить · Enter"; b.style.background = "linear-gradient(135deg,#6366f1,#8b5cf6)"; b.style.color = "#fff"; b.style.border = "none"; b.onclick = sendFromInput; }
  }
  // Дерево решений чата: перед свободным ответом подбираем агента (лексика+семантика). Сильный матч →
  // карточка «это задача для агента X, куда результат?» вместо галлюцинации LLM.
  async function sendFromInput() {
    const v = ($("inp") ? $("inp").value : "").trim(); if (!v || !cur) return;
    $("inp").value = "";
    let matches = [];
    try { const r = await api(M + "/match", { method: "POST", body: JSON.stringify({ q: v }) }); matches = (r && r.matches) || []; } catch {}
    const top = matches[0];
    if (top && top.score >= 0.45) { decisionCard(v, matches); return; }
    sendPrompt(v);
  }

  // карточка выбора: агент + куда положить результат (чат / Redmine / почта / просто ответить)
  function decisionCard(text, matches) {
    const top = matches[0];
    const alt = matches.slice(1, 3).filter((m) => m.score >= 0.25);
    const msg = { role: "assistant", content: "", meta: { decision: { text, top, alt } } };
    messages.push({ role: "user", content: text, meta: {} });
    messages.push(msg); render();
  }
  function decisionHTML(dc) {
    const t = dc.top;
    const ch = (t.channels || []);
    const chLine = ch.length ? `<div style="font-size:11px;color:var(--ink-3)">каналы агента: ${ch.map(esc).join(", ")}</div>` : "";
    const altBtns = (dc.alt || []).map((a) => `<button class="dcAlt" data-id="${esc(a.id)}" data-name="${esc(a.name)}" style="padding:5px 10px;border:1px solid var(--line);border-radius:9px;background:var(--field);color:var(--ink-2);font-size:11px;cursor:pointer">${esc(a.name)}</button>`).join("");
    return `<div style="display:flex;flex-direction:column;gap:10px">
      <div style="font-size:13px;color:var(--ink)">🎯 Похоже, это задача для агента <b>${esc(t.name)}</b> <span style="font-family:var(--mono);font-size:10px;color:var(--ink-3)">${esc(t.family || "")}</span></div>
      ${chLine}
      <div style="font-size:11.5px;color:var(--ink-2)">Куда положить результат?</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="dcRun" data-id="${esc(t.id)}" data-name="${esc(t.name)}" data-deliver="" style="padding:8px 13px;border:none;border-radius:10px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-size:12px;font-weight:600;cursor:pointer">▶ Запустить (как настроено)</button>
        <button class="dcRun" data-id="${esc(t.id)}" data-name="${esc(t.name)}" data-deliver="chat" style="padding:8px 13px;border:1px solid var(--line);border-radius:10px;background:var(--field);color:var(--ink);font-size:12px;font-weight:600;cursor:pointer">💬 Только в чат</button>
        ${ch.includes("redmine") ? `<button class="dcRun" data-id="${esc(t.id)}" data-name="${esc(t.name)}" data-deliver="redmine" style="padding:8px 13px;border:1px solid var(--line);border-radius:10px;background:var(--field);color:var(--ink);font-size:12px;font-weight:600;cursor:pointer">🎫 В Redmine</button>` : ""}
        ${ch.includes("email") ? `<button class="dcRun" data-id="${esc(t.id)}" data-name="${esc(t.name)}" data-deliver="email" style="padding:8px 13px;border:1px solid var(--line);border-radius:10px;background:var(--field);color:var(--ink);font-size:12px;font-weight:600;cursor:pointer">✉ На почту</button>` : ""}
        <button class="dcChat" style="padding:8px 13px;border:1px solid var(--line);border-radius:10px;background:transparent;color:var(--ink-3);font-size:12px;cursor:pointer">Просто ответить</button>
      </div>
      ${altBtns ? `<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap"><span style="font-size:10.5px;color:var(--ink-3)">другой агент:</span>${altBtns}</div>` : ""}</div>`;
  }
  function _lastDecision() { for (let i = messages.length - 1; i >= 0; i--) { if (messages[i].meta && messages[i].meta.decision) return messages[i].meta.decision; } return null; }

  // запуск подобранного агента с выбранной доставкой (deliver: '' как настроено | chat | redmine | email)
  async function runAbopAgentDeliver(agentId, agentName, task, deliver) {
    const run = { role: "assistant", content: "", meta: {} }; messages.push(run); render();
    const el = $("col").querySelector("div:last-child .bub");
    if (el) el.innerHTML = `<span style="display:inline-flex;gap:12px;align-items:center">${mascot("thinking", 26)}<span style="color:var(--ink-2)">агент «${esc(agentName)}» работает…</span></span>`;
    try {
      const r = await api(M + "/threads/" + cur.id + "/run-agent", { method: "POST", body: JSON.stringify({ agent_id: agentId, context: task || "", deliver: deliver || "" }) });
      if (r.ok) { run.content = "[агент " + agentId + "]"; run.meta = { run_agent: r.run }; }
      else { run.content = "Ошибка запуска: " + (r.error || "не удалось"); }
    } catch (e) { run.content = "Сбой: " + (e && e.message || e); }
    render(); loadThreads();
  }

  async function sendPrompt(text) {
    text = (text || "").trim(); if (!text || !cur) return;
    const wasNew = messages.length === 0;
    messages.push({ role: "user", content: text, meta: {} });
    const asst = { role: "assistant", content: "", meta: {} }; messages.push(asst); render();
    const _bubs = $("col").querySelectorAll(".bub"); const el = _bubs[_bubs.length - 1];  // последний пузырь — надёжнее div:last-child
    if (el) el.innerHTML = `<span style="display:inline-flex;gap:12px;align-items:center">${mascot("thinking", 26)}<span style="color:var(--ink-2)">думает…</span></span>`;
    curAbort = new AbortController(); setBusy(true);
    try {
      const resp = await fetch(ctx.base + M + "/threads/" + cur.id + "/send-stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt: text }), signal: curAbort.signal });
      if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
      const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = "", first = true;
      const caret = `<span style="display:inline-block;width:7px;height:15px;background:#818cf8;animation:ape-caret 1s steps(1) infinite;vertical-align:text-bottom"></span>`;
      while (true) {
        const { done, value } = await reader.read(); if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const line = buf.slice(0, i).split("\n").find((l) => l.startsWith("data:")); buf = buf.slice(i + 2);
          if (!line) continue; let d; try { d = JSON.parse(line.slice(5).trim()); } catch { continue; }
          if (d.delta) { if (first) { first = false; } asst.content += d.delta; if (el) { el.innerHTML = md(asst.content) + caret; $("scroll").scrollTop = $("scroll").scrollHeight; } }
          else if (d.error) { const msg = d.error === "auth_required" ? "нужен вход через GitHub (вверху справа)" : (d.error === "forbidden" ? "🛡 " + (d.message || "профиль недоступен вашей роли") : (d.message || d.error)); asst.content = asst.content || ("Ошибка: " + msg); if (el) el.textContent = asst.content; }
          else if (d.done && d.meta) asst.meta = d.meta;
        }
      }
    } catch (e) { if (e.name === "AbortError") asst.content += "\n\n⏹ остановлено"; else asst.content = asst.content || ("Сбой: " + e.message); }
    curAbort = null; setBusy(false); render();
    if (wasNew && cur.title === "Новый тред") { try { const a = await api(M + "/threads/" + cur.id + "/autotitle", { method: "POST" }); if (a.ok) { cur.title = a.title; } } catch {} }
    loadThreads();
  }

  // ── шторка агентов ──
  // ── шторка с табами Инструменты / Агенты (1:1 из макета Overlays) ──
  let drTab = "tools";
  function openAgents(tab) { if (!cur) return; drTab = tab || "agents"; $("drawer").style.transform = "translateX(0)"; renderDrawer(); }
  function drTabStyle(on) { return on ? "background:var(--panel);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.2)" : "background:transparent;color:var(--ink-2)"; }
  // запуск РЕАЛЬНОГО ABOP-агента из треда (находки/доставка/HITL карточкой)
  async function runAbopAgent(agentId, agentName, task) {
    $("drawer").style.transform = "translateX(100%)";
    if (task) messages.push({ role: "user", content: "▶ запустить агента «" + agentName + "»" + (task ? ": " + task : ""), meta: {} });
    const run = { role: "assistant", content: "", meta: {} }; messages.push(run); render();
    const el = $("col").querySelector("div:last-child .bub");
    if (el) el.innerHTML = `<span style="display:inline-flex;gap:12px;align-items:center">${mascot("thinking", 26)}<span style="color:var(--ink-2)">агент «${esc(agentName)}» работает…</span></span>`;
    try {
      const r = await api(M + "/threads/" + cur.id + "/run-agent", { method: "POST", body: JSON.stringify({ agent_id: agentId, context: task || "" }) });
      if (r.ok) {
        run.content = "[агент " + agentId + "]"; run.meta = { run_agent: r.run };
        // HITL рендерится прямо в карточке прогона в основном окне чата (runCard) — без отдельной модалки.
      } else {
        run.content = "Ошибка запуска: " + (r.error || "не удалось");
      }
    } catch (e) { run.content = "Сбой: " + (e && e.message || e); }
    render(); loadThreads();
  }

  // быстрые роли (импровизация LLM без Data Plane) — остаётся как лёгкий режим
  async function runRolesInThread(rl, task) {
    $("drawer").style.transform = "translateX(100%)";
    messages.push({ role: "user", content: "[роли] " + task, meta: {} });
    const run = { role: "assistant", content: "", meta: {} }; messages.push(run); render();
    const el = $("col").querySelector("div:last-child .bub");
    if (el) el.innerHTML = `<span style="display:inline-flex;gap:12px;align-items:center">${mascot("thinking", 26)}<span style="color:var(--ink-2)">роли работают…</span></span>`;
    const r = await api(M + "/threads/" + cur.id + "/agents", { method: "POST", body: JSON.stringify({ task, roles: rl }) });
    run.content = r.ok ? r.content : ("Ошибка: " + (r.error === "auth_required" ? "нужен вход через GitHub" : r.error));
    render(); loadThreads();
  }
  async function renderDrawer() {
    $("drTabTools").style.cssText += ";" + drTabStyle(drTab === "tools");
    $("drTabAgents").style.cssText += ";" + drTabStyle(drTab === "agents");
    $("drTabTools").onclick = () => { drTab = "tools"; renderDrawer(); };
    $("drTabAgents").onclick = () => { drTab = "agents"; renderDrawer(); };
    const b = $("drBody");
    if (drTab === "tools") {
      const tool = (glyph, title, note, inner) => `<div style="padding:14px;border-radius:13px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:10px">
        <div style="display:flex;align-items:center;gap:10px"><span style="font-size:15px">${glyph}</span>
          <span style="flex:1;display:flex;flex-direction:column;gap:2px"><span style="font-size:13px;font-weight:600">${title}</span><span style="font-size:11.5px;line-height:1.4;color:var(--ink-3)">${note}</span></span></div>${inner || ""}</div>`;
      const fmts = ["md", "pdf", "docx", "xlsx"].map((f) => `<button class="expf" data-f="${f}" style="padding:6px 11px;border:1px solid var(--line);border-radius:9999px;background:var(--hover);color:var(--ink-2);font-family:var(--mono);font-size:10.5px;font-weight:600;cursor:pointer">${f}</button>`).join("");
      b.innerHTML =
        tool("📎", "RAG · вложения", "файл → знания треда, ответы с опорой на него", `<button id="tlRag" style="padding:9px;border:1px solid rgba(129,140,248,.4);border-radius:10px;background:rgba(99,102,241,.16);color:var(--accent-ink);font-size:12px;font-weight:600;cursor:pointer">Прикрепить файл</button>`) +
        tool("🔎", "OCR", "скан/картинка → текст → знания (модуль в разработке)", `<button disabled style="padding:9px;border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--ink-3);font-size:12px;font-weight:600">Скоро</button>`) +
        tool("🧠", "NLP", "извлечение сущностей / классификация (в разработке)", `<button disabled style="padding:9px;border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--ink-3);font-size:12px;font-weight:600">Скоро</button>`) +
        tool("📥", "Экспорт треда", "сохранить в «Загрузки»", `<div style="display:flex;gap:6px;flex-wrap:wrap">${fmts}</div>`);
      const fileIn = document.createElement("input"); fileIn.type = "file"; fileIn.accept = ".txt,.md,.csv,.json,.pdf,.docx,.xlsx"; fileIn.style.display = "none"; b.appendChild(fileIn);
      b.querySelector("#tlRag").onclick = () => fileIn.click();
      fileIn.onchange = async (e) => { const f = e.target.files[0]; $("drawer").style.transform = "translateX(100%)"; await attachFile(f); e.target.value = ""; };
      b.querySelectorAll(".expf").forEach((x) => x.onclick = () => { $("drawer").style.transform = "translateX(100%)"; exportThread(x.dataset.f); });
      return;
    }
    // agents tab — РЕАЛЬНЫЕ ABOP-агенты (инженерная платформа), запуск из чата (среда управления)
    b.innerHTML = `<div class="faint">Загрузка агентов ABOP…</div>`;
    let cat = []; try { cat = await api(M + "/abop-agents"); } catch {}
    const roleTxt = (ctx.roles && ctx.roles[0]) || "manager";
    const catHTML = cat.map((a) => `<label style="padding:13px;border-radius:13px;background:var(--panel);border:1px solid var(--line);display:flex;align-items:flex-start;gap:9px;cursor:pointer">
      <input type="radio" name="abopAgent" class="da" value="${esc(a.id)}" style="accent-color:#6366f1;margin-top:2px"/>
      <span style="display:flex;flex-direction:column;gap:2px;min-width:0">
        <span style="font-size:13px;font-weight:600">${esc(a.name)}${a.outward ? " 🛡" : ""}</span>
        <span style="font-size:11px;line-height:1.4;color:var(--ink-3)">${esc(a.family || "")}${a.role ? " · " + esc(a.role) : ""}${a.autonomy_max ? " · автономия " + esc(a.autonomy_max) : ""}</span>
      </span></label>`).join("")
      || `<div style="font-size:12.5px;color:var(--ink-3)">Нет доступных агентов ABOP (проверьте вход/роль). Соберите агента во вкладке 🤖 Агенты.</div>`;
    b.innerHTML = `<div style="display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:11px;background:var(--hover);border:1px solid var(--line)">
        <span style="font-size:11.5px;color:var(--ink-2)">Агенты ABOP · видно по роли:</span><span style="font-family:var(--mono);font-size:11px;font-weight:600;color:var(--accent-ink-2)">${esc(roleTxt)}</span></div>
      <textarea id="drTask" rows="3" style="${"padding:11px 13px;border-radius:11px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px"}" placeholder="Контекст/задача (необязательно): ссылка на документ, выделенный текст, уточнение…"></textarea>
      ${catHTML}
      <button id="drRun" style="padding:11px;border:none;border-radius:11px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-size:12.5px;font-weight:600;cursor:pointer">▶ Запустить агента в тред</button>
      <details><summary style="cursor:pointer;font-size:12px;color:var(--ink-3)">Быстрые роли (импровизация без данных)</summary><div style="margin-top:6px">${roles.map((r) => `<label style="display:flex;gap:8px;align-items:flex-start;padding:5px 0;font-size:12.5px"><input type="checkbox" class="rl" value="${r.id}"/><span><b>${esc(r.name)}</b> <span style="color:var(--ink-3)">${esc(r.brief)}</span></span></label>`).join("")}</div>
        <button id="drRunRoles" style="margin-top:6px;padding:9px;border:1px solid var(--line);border-radius:10px;background:var(--hover);color:var(--ink-2);font-size:12px;font-weight:600;cursor:pointer">Запустить роли</button></details>`;
    if ($("inp").value.trim()) b.querySelector("#drTask").value = $("inp").value.trim();
    b.querySelector("#drRun").onclick = () => {
      const task = b.querySelector("#drTask").value.trim();
      const sel = b.querySelector(".da:checked");
      if (!sel) { alert("Выберите агента ABOP"); return; }
      const name = (cat.find((a) => a.id === sel.value) || {}).name || sel.value;
      runAbopAgent(sel.value, name, task);
    };
    const rr = b.querySelector("#drRunRoles");
    if (rr) rr.onclick = () => {
      const task = b.querySelector("#drTask").value.trim();
      const rl = [...b.querySelectorAll(".rl:checked")].map((x) => x.value);
      if (!task || !rl.length) { alert("Укажи задачу и роль(и)"); return; }
      runRolesInThread(rl, task);
    };
  }

  function openExport() {
    if (!cur) return;
    const ov = modal("Экспорт треда в «Загрузки»", `<div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn" data-f="md">Markdown</button><button class="btn" data-f="pdf">PDF</button>
      <button class="btn" data-f="docx">Word</button><button class="btn" data-f="xlsx">Excel</button></div>`, null);
    ov.querySelectorAll("[data-f]").forEach((b) => b.onclick = async () => { ov.remove(); await exportThread(b.dataset.f); });
  }
  async function exportThread(fmt) {
    if (fmt === "pdf") {
      if (!(window.ape && window.ape.exportPdf)) { alert("PDF — только в установленном приложении."); return; }
      const html = `<html><head><meta charset="utf-8"><style>body{font-family:sans-serif;padding:24px;color:#111}h2{margin:16px 0 4px;font-size:14px}</style></head><body><h1>${esc(cur.title)}</h1>${messages.map((m) => `<h2>${m.role === "user" ? "Вы" : "Ассистент"}</h2><div style="white-space:pre-wrap">${esc(m.content)}</div>`).join("")}</body></html>`;
      const r = await window.ape.exportPdf(html, (cur.title || "chat").replace(/[^\w\-. ]/g, "_").slice(0, 60) + ".pdf");
      alert(r.ok ? "Сохранено в Загрузки:\n" + r.path : "Ошибка: " + r.error); return;
    }
    const r = await api(M + "/threads/" + cur.id + "/export", { method: "POST", body: JSON.stringify({ format: fmt }) });
    alert(r.ok ? "Сохранено в Загрузки:\n" + r.path : "Не удалось: " + r.error);
  }

  $("newTh").onclick = async () => { const t = await api(M + "/threads", { method: "POST", body: JSON.stringify({ title: "Новый тред", profile: "standard", skills: [] }) }); await loadThreads(); openThread(t); };

  // приём выделенного текста из Word/Excel/браузера по глобальному хоткею (Ctrl+Shift+A):
  // создаём тред, кладём текст в поле ввода и фокусируемся — пользователь жмёт Enter или выбирает агента
  window.__apeAnalyze = async (text) => {
    const inp = $("inp"); if (!inp) return;
    text = (text || "").trim();
    if (!text) { inp.focus(); inp.placeholder = "Выделите текст в Word/Excel/браузере и снова нажмите Ctrl+Shift+A"; return; }
    const clip = text.length > 4000 ? text.slice(0, 4000) + "…" : text;
    if (!cur) { const t = await api(M + "/threads", { method: "POST", body: JSON.stringify({ title: "Анализ выделенного", profile: "standard", skills: [] }) }); await loadThreads(); await openThread(t); }
    $("inp").value = "Проанализируй этот фрагмент:\n\n" + clip;
    $("inp").focus(); $("inp").scrollTop = $("inp").scrollHeight;
    renderAgentSuggest($("inp").value);   // сразу подсветим подходящих агентов по вставленному тексту
  };
  $("inp").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFromInput(); } };
  $("inp").oninput = () => { clearTimeout(_sugTimer); _sugTimer = setTimeout(() => renderAgentSuggest($("inp").value), 280); };
  setBusy(false);
  await loadThreads();
  if (threads.length) openThread(threads[0]); else { render(); renderTools(); }

  // расписания: первичная загрузка + поллинг каждые 60с (уведомления о завершении регулярных задач)
  loadSchedules(false);
  if (window.__apeSchedTimer) clearInterval(window.__apeSchedTimer);
  window.__apeSchedTimer = setInterval(() => { if (root.isConnected) loadSchedules(true); else clearInterval(window.__apeSchedTimer); }, 60000);
}
