// Модуль «Прогоны» — журнал работы агентов: список с поиском и фильтрами, карточка прогона целиком,
// сравнение с предыдущим, отчёт. Закрывает разрывы аудита 29.09: истории прогонов в десктопе не было
// вовсе (карточки жили внутри чатов и терялись вместе с ними), находки показывались по восемь штук,
// сравнение прогонов и выбор шаблона отчёта были доступны только в вебе.
//
// Выборка и фильтры считаются на сервере (Postgres — источник истины), панель ничего не хранит.
const R = "/api/modules/runs";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CARD = "padding:16px;border-radius:14px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:10px;box-shadow:var(--shadow-1)";
const LBL = "font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)";
const INP = "padding:8px 11px;border-radius:10px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:13px";

const fmtDate = (s) => {
  const d = new Date(s);
  return isNaN(d) ? String(s || "") : d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
};
// Стоимость приходит то числом, то блоком метрик {rub, input_tokens, …}: Number({...}) даёт NaN,
// и в журнале вместо цены стояло «NaN ₽».
const rub = (c) => (c && typeof c === "object" ? c.rub : c);
const fmtCost = (c) => { const v = Number(rub(c)); return (rub(c) == null || !isFinite(v)) ? ""
  : (v === 0 ? "0 ₽" : v.toFixed(2) + " ₽"); };

export async function mount(root, ctx) {
  const { api, toast, humanError } = ctx;
  let runs = [], agentsList = [], total = 0, loadErr = null, busy = false;
  let q = "", agentId = "", verdict = "", days = 0, deep = false, hits = {};
  let open = null, openData = null, openTab = "findings", openErr = "", templates = [];

  async function load() {
    busy = true; loadErr = null; render();
    const p = new URLSearchParams();
    if (q) p.set("q", q);
    if (agentId) p.set("agent_id", agentId);
    if (verdict) p.set("verdict", verdict);
    if (days) p.set("days", String(days));
    try {
      if (deep && q) {
        // поиск по содержимому прогонов считает Postgres на сервере: находки, ответы навыков, доставка
        const r = await api(R + "/search?" + p.toString());
        runs = r.runs || []; total = r.count || runs.length;
        hits = {}; runs.forEach((x) => { if ((x.hits || []).length) hits[x.id] = x.hits; });
      } else {
        const r = await api(R + "/list?" + p.toString());
        runs = r.runs || []; total = r.total || 0; agentsList = r.agents || []; hits = {};
      }
    } catch (e) { loadErr = e; runs = []; }
    busy = false; render();
  }

  async function openRun(id) {
    open = id; openData = null; openErr = ""; openTab = "findings"; render();
    try { openData = await api(R + "/item/" + encodeURIComponent(id)); }
    catch (e) { openErr = humanError(e); }
    render();
  }

  // ── список ──
  function filterBar() {
    const opts = [`<option value="">все агенты</option>`]
      .concat(agentsList.map((a) => `<option value="${esc(a.id)}"${a.id === agentId ? " selected" : ""}>${esc(a.name)}</option>`)).join("");
    const vOpts = [["", "любой итог"], ["ok", "пройден"], ["issues", "есть замечания"]]
      .map(([v, t]) => `<option value="${v}"${v === verdict ? " selected" : ""}>${t}</option>`).join("");
    const dOpts = [[0, "за всё время"], [1, "за сутки"], [7, "за неделю"], [30, "за месяц"]]
      .map(([v, t]) => `<option value="${v}"${v === days ? " selected" : ""}>${t}</option>`).join("");
    return `<div style="display:flex;gap:9px;flex-wrap:wrap;align-items:center">
      <input id="rq" value="${esc(q)}" placeholder="Поиск: агент, отдел, а с галочкой — по находкам и ответам навыков" style="${INP};flex:1;min-width:220px"/>
      <select id="rag" style="${INP}">${opts}</select>
      <select id="rv" style="${INP}">${vOpts}</select>
      <select id="rd" style="${INP}">${dOpts}</select>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--ink-2);cursor:pointer">
        <input type="checkbox" id="rDeep"${deep ? " checked" : ""}/> искать внутри прогонов
      </label>
      <button class="btn sm" id="rReload">Обновить</button>
    </div>`;
  }

  function rowHTML(r) {
    const ok = r.verdict_ok;
    return `<button type="button" class="rrow" data-id="${esc(r.id)}" style="display:flex;align-items:center;gap:12px;padding:11px 13px;border-radius:12px;border:1px solid var(--line);background:var(--panel);text-align:left;width:100%">
      <span style="width:8px;height:8px;border-radius:9999px;flex:none;background:${ok ? "var(--ok-ink)" : "var(--warn-ink)"}"></span>
      <span style="flex:1;min-width:0;display:flex;flex-direction:column;gap:2px">
        <span style="font-size:13px;font-weight:600;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.agent_name || r.agent_id)}</span>
        <span style="font-size:11px;color:var(--ink-3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(fmtDate(r.created_at))}${r.family ? " · " + esc(r.family) : ""}${r.started_by ? " · " + esc(r.started_by) : ""}</span>
      </span>
      <span style="font-size:11px;color:var(--ink-3);font-family:var(--mono);flex:none">${esc(fmtCost(r.cost))}</span>
      <span style="font-size:11px;flex:none;color:${ok ? "var(--ok-ink)" : "var(--warn-ink)"}">${ok ? "пройден" : "замечания"}</span>
    </button>${(hits[r.id] || []).length ? `<div style="margin:-2px 0 6px 30px;display:flex;flex-direction:column;gap:3px">${hits[r.id].map((h) => `<div style="font-size:11.5px;color:var(--ink-3);line-height:1.45">…${esc(h)}…</div>`).join("")}</div>` : ""}`;
  }

  function listHTML() {
    if (busy && !runs.length) return `<div style="${CARD}"><div class="skeleton" style="height:14px;width:40%"></div><div class="skeleton" style="height:52px"></div><div class="skeleton" style="height:52px"></div></div>`;
    if (loadErr) {
      const st = loadErr.status;
      return `<div style="${CARD};border-color:${st === 401 ? "var(--warn-line)" : "var(--danger-line)"}">
        <div style="font-weight:700">${st === 401 ? "Нужен вход в ABOP" : "Журнал прогонов недоступен"}</div>
        <div style="font-size:12.5px;color:var(--ink-2)">${esc(humanError(loadErr))}</div>
        <button class="btn sm" id="errRetry" style="align-self:flex-start">Повторить</button></div>`;
    }
    if (!runs.length) {
      return `<div style="${CARD};align-items:flex-start">
        <div style="font-weight:700">${q || agentId || verdict || days ? "Ничего не нашлось" : "Прогонов пока нет"}</div>
        <div style="font-size:12.5px;color:var(--ink-2)">${q || agentId || verdict || days ? "Измените условия поиска — всего в журнале записей: " + total : "Запустите агента в чате — его работа появится здесь."}</div>
        ${q || agentId || verdict || days ? `<button class="btn sm" id="rClear">Сбросить фильтры</button>` : ""}</div>`;
    }
    return `<div style="display:flex;flex-direction:column;gap:7px">${runs.map(rowHTML).join("")}</div>`;
  }

  // ── карточка прогона ──
  function findingHTML(f) {
    if (typeof f === "string") return `<div style="font-size:12.5px;color:var(--ink);border-left:2px solid var(--accent);padding-left:10px;margin:5px 0;white-space:pre-wrap">${esc(f)}</div>`;
    if (!f || typeof f !== "object") return "";
    const head = f["проверка"] || f["наблюдение"] || f["заголовок"] || f.skill || "";
    const cls = f["класс"] || f["ранг"] || "";
    const body = Object.entries(f)
      .filter(([k, v]) => !["проверка", "наблюдение", "заголовок", "класс", "ранг", "structured", "text"].includes(k) && v && typeof v !== "object")
      .slice(0, 8)
      .map(([k, v]) => `<div style="font-size:12px;color:var(--ink-2);margin-top:3px"><span style="color:var(--ink-3)">${esc(k)}:</span> ${esc(String(v).slice(0, 400))}</div>`).join("");
    return `<div style="padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--field);margin:6px 0">
      ${cls ? `<span style="font-size:10.5px;font-weight:700;color:#fff;background:var(--accent);border-radius:6px;padding:1px 7px;margin-right:6px">${esc(cls)}</span>` : ""}
      <span style="font-size:13px;font-weight:600;color:var(--ink)">${esc(String(head).slice(0, 300))}</span>${body}</div>`;
  }

  let boardData = null, boardErr = "";
  async function loadBoard() {
    try { boardData = await api(R + "/board/" + encodeURIComponent(open)); boardErr = ""; }
    catch (e) { boardErr = humanError(e); }
    render();
  }

  // ── доска прогона: чей это вывод и чем кончился спор ──
  // Запись доски показываем человеческой строкой «поле: значение · поле: значение», а не сырым JSON:
  // на доску попадает результат навыка, и читать его должен человек, а не разработчик.
  function recordLine(x) {
    if (x == null) return "—";
    if (typeof x !== "object") return String(x).slice(0, 220);
    const parts = Object.entries(x)
      .filter(([, v]) => v != null && typeof v !== "object" && String(v) !== "")
      .slice(0, 4)
      .map(([k, v]) => `${k}: ${String(v).slice(0, 90)}`);
    return parts.length ? parts.join(" · ") : JSON.stringify(x).slice(0, 220);
  }
  function valueHTML(v) {
    if (v == null) return "—";
    if (typeof v !== "object") return esc(String(v).slice(0, 300));
    if (Array.isArray(v)) {
      return v.slice(0, 6).map((x) => `<div style="font-size:12px;color:var(--ink-2);margin:2px 0">• ${esc(recordLine(x))}</div>`).join("")
        + (v.length > 6 ? `<div style="font-size:11.5px;color:var(--ink-3)">…ещё ${v.length - 6}</div>` : "");
    }
    const rows = Object.entries(v).slice(0, 8).map(([k, x]) => `<div style="display:grid;grid-template-columns:150px 1fr;gap:10px;font-size:12px;margin-top:3px">
      <span style="color:var(--ink-3)">${esc(k)}</span><span style="color:var(--ink-2)">${esc(Array.isArray(x) ? x.slice(0, 3).map(recordLine).join(" · ") + (x.length > 3 ? " …" : "") : recordLine(x))}</span></div>`).join("");
    return rows || `<span style="font-size:12px;color:var(--ink-3)">пусто</span>`;
  }
  function boardHTML() {
    if (boardErr) return `<div style="font-size:12.5px;color:var(--warn-ink)">Доска не открылась: ${esc(boardErr)}</div>`;
    if (!boardData) return `<div class="skeleton" style="height:90px"></div>`;
    const b = boardData;
    const entries = b.entries || [], contr = b.contradictions || [], arb = b.arbitration || {};
    if (!entries.length) {
      return `<div style="font-size:12.5px;color:var(--ink-3)">Навыки этого прогона ничего не выкладывали на общую доску — она нужна, когда над задачей работают несколько ветвей.</div>`;
    }
    const snap = b.data_snapshot && b.data_snapshot.taken_at
      ? `<div style="font-size:11.5px;color:var(--ink-3);margin-bottom:8px">Снимок данных на старте: ${esc(b.data_snapshot.taken_at)}${(b.data_snapshot.drift || []).length ? ` · данные с тех пор менялись: ${esc((b.data_snapshot.drift || []).join(", "))}` : " · данные не менялись"}</div>` : "";
    // Решения арбитра идут первыми: это то, что человеку важнее всего увидеть.
    const decisions = (arb.decisions || []).map((d) => `<div style="padding:10px 12px;border-radius:10px;border:1px solid var(--ok-line);background:var(--ok-bg);margin:6px 0">
      <div style="font-size:12.5px;font-weight:600">Спор о «${esc(d.key)}»${d.item ? " · " + esc(d.item) : ""} разрешён</div>
      <div style="font-size:12px;color:var(--ink-2);margin-top:4px">Выбрано: ${valueHTML(d.chosen)}</div>
      <div style="font-size:11.5px;color:var(--ink-3);margin-top:4px">Кем: ${esc(d.by || "—")}${d.reason ? " · " + esc(d.reason) : ""}</div></div>`).join("");
    const open = contr.filter((c) => !(arb.decisions || []).some((d) => d.key === c.key && (d.item || "") === (c.item || "")));
    const disputes = open.map((c) => `<div style="padding:10px 12px;border-radius:10px;border:1px solid var(--warn-line);background:var(--warn-bg);margin:6px 0">
      <div style="font-size:12.5px;font-weight:600">Ветви разошлись: «${esc(c.key)}»${c.item ? " · " + esc(c.item) : ""}</div>
      <div style="font-size:11.5px;color:var(--ink-3);margin-top:3px">расходятся поля: ${esc((c.fields || []).join(", ") || "—")}</div>
      ${(c.variants || []).map((v) => `<div style="margin-top:6px"><div style="font-size:11.5px;color:var(--ink-3)">${esc((v.authors || []).join(", "))}</div>${valueHTML(v.value)}</div>`).join("")}
      <div style="font-size:11.5px;color:var(--ink-3);margin-top:6px">Решения нет — вопрос ждёт человека.</div></div>`).join("");
    const rows = entries.map((e) => `<div style="padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--field);margin:6px 0">
      <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
        <span style="font-size:12.5px;font-weight:600">${esc(e.key || "")}</span>
        <span style="font-size:11.5px;color:var(--ink-3)">выложил: ${esc(e.author || "—")}${e.at ? " · " + esc(e.at) : ""}</span></div>
      <div style="margin-top:5px">${valueHTML(e.value)}</div></div>`).join("");
    return `${snap}${decisions}${disputes}
      <div style="${LBL};margin:12px 0 4px">что выложено на доску</div>${rows}`;
  }

  function openHTML() {
    if (openErr) return `<div style="${CARD};border-color:var(--danger-line)"><div style="font-weight:700">Прогон не открылся</div><div style="font-size:12.5px;color:var(--ink-2)">${esc(openErr)}</div><button class="btn sm" id="oRetry" style="align-self:flex-start">Повторить</button></div>`;
    if (!openData) return `<div style="${CARD}"><div class="skeleton" style="height:18px;width:50%"></div><div class="skeleton" style="height:120px"></div></div>`;
    const d = openData;
    const v = d.verdict || {};
    const fnd = d.findings || [];
    const so = d.skill_outputs || [];
    const rm = d.run_metrics || {};
    const miss = so.filter((o) => (o.schema_miss || []).length);
    const bs = (rm.board || {});
    const tabs = [["findings", "Находки · " + fnd.length], ["skills", "Навыки · " + so.length],
                  ["board", "Доска" + (bs.entries ? " · " + bs.entries : "")], ["delivery", "Доставка"], ["metrics", "Затраты"]]
      .map(([id, t]) => `<button type="button" class="btn sm rtab${id === openTab ? " primary" : ""}" data-tab="${id}">${esc(t)}</button>`).join("");
    let body = "";
    if (openTab === "findings") {
      body = fnd.length
        ? fnd.map(findingHTML).join("")
        : `<div style="font-size:12.5px;color:var(--ink-3)">Находок нет — агент не нашёл расхождений.</div>`;
    } else if (openTab === "skills") {
      body = so.length ? so.map((o) => `<div style="padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--field);margin:6px 0">
        <div style="font-size:12.5px;font-weight:600">${esc(o.skill)}${o.template_id ? ` <span style="color:var(--ink-3);font-weight:400">· шаблон ${esc(o.template_id)}</span>` : ""}</div>
        ${(o.schema_miss || []).length ? `<div style="font-size:11.5px;color:var(--warn-ink);margin-top:4px">не заполнено: ${esc((o.schema_miss || []).join(", "))}</div>` : ""}
        <div style="font-size:12px;color:var(--ink-2);margin-top:5px;white-space:pre-wrap">${esc(JSON.stringify(o.structured || {}, null, 1).slice(0, 2500))}</div></div>`).join("")
        : `<div style="font-size:12.5px;color:var(--ink-3)">Навыки не вернули структурного результата.</div>`;
    } else if (openTab === "board") {
      body = boardHTML();
    } else if (openTab === "delivery") {
      const dl = d.delivery || [];
      body = dl.length ? dl.map((x) => `<div style="font-size:12.5px;color:var(--ink-2);margin:5px 0">${esc(x.channel || "")}${x.to ? " → " + esc(x.to) : ""} · ${esc(x.mode || "")}${x.subject ? " · «" + esc(x.subject) + "»" : ""}</div>`).join("")
        : `<div style="font-size:12.5px;color:var(--ink-3)">Ничего наружу не уходило.</div>`;
    } else {
      // Время и токены лежат в run_metrics.timings и run_metrics.cost, а не в корне: раньше вкладка
      // показывала «—» и «0 → 0» ровно там, где человек спрашивает «сколько это стоило».
      const tm = rm.timings || {}, cst = rm.cost || {};
      const sec = (ms) => Math.round((ms || 0) / 100) / 10 + " с";
      const rows = [["Время", tm.total_ms ? sec(tm.total_ms) : "—"],
        ["Стоимость", fmtCost(rm.cost != null ? rm.cost : d.cost)],
        ["Токены", cst.input_tokens != null ? ((cst.input_tokens || 0).toLocaleString("ru-RU") + " → " + (cst.output_tokens || 0).toLocaleString("ru-RU")) : "—"],
        ["Вызовов модели", cst.calls != null ? String(cst.calls) : "—"],
        ["Модель", Object.keys(cst.by_model || {}).join(", ") || "—"],
        ["Волн", String((d.waves || []).length || "—")],
        ["Дольше всех", tm.slowest ? (tm.slowest.skill + " · " + sec(tm.slowest.ms)) : "—"]];
      const bySkill = Object.entries(tm.by_skill_ms || {}).sort((a, b) => b[1] - a[1]);
      body = `<div style="display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:12.5px">`
        + rows.map(([k, x]) => `<div style="color:var(--ink-3)">${esc(k)}</div><div>${esc(x)}</div>`).join("") + `</div>`
        + (bySkill.length ? `<div style="${LBL};margin:14px 0 6px">сколько занял каждый навык</div>`
          + bySkill.map(([sid, ms]) => `<div style="display:grid;grid-template-columns:1fr auto;gap:10px;font-size:12px;margin-top:4px">
              <span style="color:var(--ink-2)">${esc(sid)}</span><span style="font-family:var(--mono);color:var(--ink-3)">${esc(sec(ms))}</span></div>`).join("") : "")
        + (rm.limits ? `<div style="${LBL};margin:14px 0 6px">лимиты, по которым шёл прогон</div>
            <div style="font-size:12px;color:var(--ink-2)">ответ навыка ${esc(rm.limits.max_tokens)} · со схемой ${esc(rm.limits.max_tokens_template)} · рассуждающий ${esc(rm.limits.max_tokens_free)} · шаги инструментов ${esc(rm.limits.tool_steps)} · параллельность ${esc(rm.limits.concurrency)}<br>источник: ${esc(rm.limits.source || "по умолчанию")}</div>` : "");
    }
    const tplOpts = templates.length
      ? `<select id="oTpl" style="${INP}">${templates.map((t) => `<option value="${esc(t.id)}">${esc(t.name || t.id)}</option>`).join("")}</select>` : "";
    return `<div style="${CARD}">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <button class="btn sm" id="oBack">← К журналу</button>
        <span style="font-size:15px;font-weight:700">${esc(d.agent_name || d.agent_id || "")}</span>
        <span style="font-size:11.5px;color:${v.ok ? "var(--ok-ink)" : "var(--warn-ink)"}">${v.ok ? "пройден" : "есть замечания"}${v.autonomy_used ? " · " + esc(v.autonomy_used) : ""}</span>
        <span style="margin-left:auto;display:flex;gap:7px;align-items:center">${tplOpts}
          <button class="btn sm" id="oHtml">Показать отчёт</button>
          <button class="btn sm primary" id="oPdf">Отчёт PDF</button>
          <button class="btn sm" id="oDiff">Сравнить с прошлым</button></span>
      </div>
      <div style="font-size:11.5px;color:var(--ink-3);font-family:var(--mono)">${esc(open)}</div>
      ${miss.length ? `<div style="font-size:12px;color:var(--warn-ink)">Навыки заполнили схему не полностью: ${esc(miss.map((m) => m.skill).join(", "))}. В отчёте это отмечено.</div>` : ""}
      <div style="display:flex;gap:7px;flex-wrap:wrap">${tabs}</div>
      <div style="max-height:52vh;overflow:auto">${body}</div></div>`;
  }

  function render() {
    root.innerHTML = `<div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:14px;padding:26px 28px;overflow:auto;animation:ape-in .3s ease-out">
      <div style="display:flex;flex-direction:column;gap:5px">
        <span style="${LBL}">журнал</span>
        <h1 style="margin:0;font-size:26px;font-weight:800;letter-spacing:-.7px">Прогоны</h1>
        <p style="margin:0;font-size:13px;color:var(--ink-2)">Вся работа агентов: находки целиком, доставка, затраты и отчёт. Записи приходят с сервера ABOP и не зависят от чатов.</p>
      </div>
      ${open ? openHTML() : filterBar() + listHTML()}</div>`;
    wire();
  }

  function wire() {
    const $ = (id) => root.querySelector("#" + id);
    if (!open) {
      const rq = $("rq");
      if (rq) {
        rq.onkeydown = (e) => { if (e.key === "Enter") { q = rq.value.trim(); load(); } };
        rq.oninput = () => { clearTimeout(wire._t); wire._t = setTimeout(() => { q = rq.value.trim(); load(); }, 400); };
      }
      if ($("rag")) $("rag").onchange = (e) => { agentId = e.target.value; load(); };
      if ($("rv")) $("rv").onchange = (e) => { verdict = e.target.value; load(); };
      if ($("rd")) $("rd").onchange = (e) => { days = +e.target.value; load(); };
      if ($("rDeep")) $("rDeep").onchange = (e) => { deep = e.target.checked; load(); };
      if ($("rReload")) $("rReload").onclick = () => load();
      if ($("errRetry")) $("errRetry").onclick = () => load();
      if ($("rClear")) $("rClear").onclick = () => { q = ""; agentId = ""; verdict = ""; days = 0; load(); };
      root.querySelectorAll(".rrow").forEach((b) => { b.onclick = () => openRun(b.dataset.id); });
      return;
    }
    if ($("oBack")) $("oBack").onclick = () => { open = null; openData = null; boardData = null; boardErr = ""; render(); };
    if ($("oRetry")) $("oRetry").onclick = () => openRun(open);
    root.querySelectorAll(".rtab").forEach((b) => { b.onclick = async () => { openTab = b.dataset.tab; render(); if (openTab === "board" && !boardData && !boardErr) await loadBoard(); }; });
    if ($("oPdf")) $("oPdf").onclick = async (e) => {
      const tpl = $("oTpl") ? $("oTpl").value : "";
      e.target.disabled = true; e.target.textContent = "…";
      try { const r = await api(R + "/report/" + encodeURIComponent(open) + (tpl ? "?template=" + encodeURIComponent(tpl) : ""), { method: "POST" }); ctx.fileToast("Отчёт сохранён", r.path); }
      catch (er) { toast(humanError(er), "danger"); }
      e.target.disabled = false; e.target.textContent = "Отчёт PDF";
    };
    if ($("oHtml")) $("oHtml").onclick = async () => {
      const tpl = $("oTpl") ? $("oTpl").value : "";
      try {
        const r = await api(R + "/report-html/" + encodeURIComponent(open) + (tpl ? "?template=" + encodeURIComponent(tpl) : ""));
        const safe = String(r.html || "").replace(/<script[\s\S]*?<\/script>/gi, "").replace(/\son\w+="[^"]*"/gi, "");
        ctx.modal("Отчёт прогона", `<div style="max-height:64vh;overflow:auto;background:#fff;color:#111;border-radius:10px;padding:8px">${safe}</div>`, null, "", { width: "980px" });
      } catch (er) { toast(humanError(er), "danger"); }
    };
    if ($("oDiff")) $("oDiff").onclick = async () => {
      try {
        const d = await api(R + "/diff/" + encodeURIComponent(open));
        const f = d["находки"] || {};
        const m = d["метрики"] || {};
        const line = (x) => esc(typeof x === "string" ? x : (x.text || x["проверка"] || x["наблюдение"] || x["заголовок"] || JSON.stringify(x).slice(0, 200)));
        const sec = (t, arr) => `<div style="margin:10px 0"><div style="${LBL}">${esc(t)} · ${(arr || []).length}</div>${(arr || []).slice(0, 30).map((x) => `<div style="font-size:12.5px;color:var(--ink-2);margin:3px 0">${line(x)}</div>`).join("") || `<div style="font-size:12px;color:var(--ink-3)">—</div>`}</div>`;
        const cost = (m["было"] && m["стало"]) ? `<div style="font-size:12.5px;color:var(--ink-2);margin-top:8px">Было ${esc(fmtCost(m["было"].rub))} за ${Math.round((m["было"].total_ms || 0) / 1000)} с, стало ${esc(fmtCost(m["стало"].rub))} за ${Math.round((m["стало"].total_ms || 0) / 1000)} с.</div>` : "";
        ctx.modal("Сравнение с прошлым прогоном",
          `<div style="font-size:12px;color:var(--ink-3);margin-bottom:8px">Предыдущий прогон: ${esc(d.base_run_id || "—")}. Находок было ${esc(f["всего_было"])}, стало ${esc(f["всего_стало"])}.</div>`
          + sec("появилось", f["добавились"]) + sec("ушло", f["ушли"]) + sec("изменилось", f["изменились"]) + cost,
          null, "", { width: "760px" });
      } catch (er) { toast(humanError(er), "danger"); }
    };
  }

  // переход из чата: «Открыть прогон» приносит идентификатор — открываем сразу нужную карточку
  root.addEventListener("ape:intent", (e) => { const id = e.detail && e.detail.run; if (id) openRun(id); });

  try { templates = await api(R + "/templates"); if (!Array.isArray(templates)) templates = []; } catch { templates = []; }
  const first = ctx.takeIntent ? ctx.takeIntent() : null;
  if (first && first.run) { await openRun(first.run); } else { await load(); }
}
