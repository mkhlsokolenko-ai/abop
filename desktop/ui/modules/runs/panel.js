// Модуль «Прогоны» — журнал работы агентов: список с поиском и фильтрами, карточка прогона целиком,
// сравнение с предыдущим, отчёт. Закрывает разрывы аудита 29.09: истории прогонов в десктопе не было
// вовсе (карточки жили внутри чатов и терялись вместе с ними), находки показывались по восемь штук,
// сравнение прогонов и выбор шаблона отчёта были доступны только в вебе.
//
// Выборка и фильтры считаются на сервере (Postgres — источник истины), панель ничего не хранит.
import { CARD, LBL, INP, BTN, BTN_OK, kpis, head } from "../../core/cards.js";
// Значок — из общего набора ядра: панель не заводит своих символов.
import { icon } from "../../core/icons.js";
// Стили — из ядра: шесть разных карточек и пять разных подписей в панелях были
// расхождением, а не выбором (DIZAJN, раздел 2).
const R = "/api/modules/runs";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

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
  const { api, toast, humanError, gate, sanitizeHtml } = ctx;
  let runs = [], agentsList = [], total = 0, loadErr = null, busy = false;
  let q = "", agentId = "", verdict = "", days = 0, deep = false, hits = {};
  let open = null, openData = null, openTab = "findings", openErr = "", templates = [];
  // Итог в числах и время у экрана. Секундомер идёт, пока карточка открыта; накопленное уходит на
  // отметку при закрытии и при уходе из раздела. Без отметки при уходе число терялось бы у того,
  // кто читает карточку и закрывает приложение — то есть у большинства.
  let итог = null, _сИтога = 0;

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
    await отметить();                 // уходим с прошлой карточки — её время не теряем
    open = id; openData = null; openErr = ""; openTab = "findings"; итог = null;
    _сИтога = Date.now();
    render();
    try { openData = await api(R + "/item/" + encodeURIComponent(id)); }
    catch (e) { openErr = humanError(e); }
    render();
    // Итог приходит отдельным запросом: числа считает сервер, «вы у экрана» добавляет сайдкар.
    // Если не пришёл — карточка остаётся как была: отсутствие чисел не повод не показать прогон.
    try { итог = await api(R + "/summary/" + encodeURIComponent(id)); render(); }
    catch { итог = null; }
  }

  async function отметить() {
    if (!open || !_сИтога) return;
    const сек = Math.round((Date.now() - _сИтога) / 1000);
    const id = open;
    _сИтога = 0;
    if (сек < 2) return;             // мелькнувшая карточка — не «время у экрана»
    try { await api(R + "/attention/" + encodeURIComponent(id),
                    { method: "POST", body: JSON.stringify({ sec: сек }) }); }
    catch { /* отметка не критична: число личное и не влияет ни на что, кроме карточки */ }
  }

  function итогHTML() {
    if (!итог || !(итог["числа"] || []).length) return "";
    // Собирает примитив ядра — одна карточка на все поверхности. Правило числа тоже его:
    // числа без происхождения он помечает сам.
    return ctx.card({ "что": "Итог прогона", "числа": итог["числа"],
                      "происхождение": итог["происхождение"] || "" });
  }

  // ── четыре числа карточки ────────────────────────────────────────────────────────────────
  //
  // Журнал отвечает на четыре вопроса сразу: что нашли, сколько это стоило, сколько заняло и
  // сколько ждёт человека. До этого они жили в разных вкладках — «сколько это стоило» лежало в
  // «Затратах», а «ждёт решения» не было видно вовсе, пока не откроешь «Доставку».
  //
  // Правило числа действует и здесь: у каждого есть вторая строка — чем оно посчитано. Число
  // без происхождения на этом экране означало бы «поверьте на слово».
  function числаПрогона(d) {
    const rm = d.run_metrics || {};
    const cst = rm.cost || {};
    const tm = rm.timings || {};
    const доставка = d.delivery || [];
    const ждут = доставка.filter((x) => String(x.mode || "") === "awaiting_hitl").length;
    const волн = (d.waves || []).length;
    const секунды = tm.total_ms ? (Math.round(tm.total_ms / 100) / 10) + " с" : "—";
    const плитки = [
      [String((d.findings || []).length), "находок",
       [(d.skill_outputs || []).length ? `${(d.skill_outputs || []).length} навыков` : "",
        волн ? `${волн} ${волн === 1 ? "волна" : "волн"}` : ""].filter(Boolean).join(" · ")
       || "расчёт по источникам прогона"],
      [fmtCost(rm.cost != null ? rm.cost : d.cost) || "—", "стоимость прогона",
       cst.input_tokens != null
         ? `${(cst.input_tokens || 0).toLocaleString("ru-RU")} → ${(cst.output_tokens || 0).toLocaleString("ru-RU")} токенов`
           + (cst.calls != null ? ` · ${cst.calls} вызовов` : "")
         : "токены прогона не записаны"],
      [секунды, "время",
       tm.slowest ? `дольше всех: ${tm.slowest.skill}` : "замер по шагам не записан"],
      [String(ждут), "ждут вашего решения",
       ждут ? "наружу ничего не ушло" : (доставка.length ? "всё решено" : "наружу ничего не уходило")],
    ];
    return kpis(плитки.map(([ч, п, о], i) => ({
      "число": ч, "подпись": п, "откуда": о,
      "тон": i === 3 && ждут ? "ждёт" : i === 1 ? "деньги" : "",
    })));
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
      <button class="ico" id="rReload" title="Обновить журнал прогонов" aria-label="Обновить журнал прогонов">${icon("refresh", "lg")}</button>
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
      <span style="flex:none;color:var(--ink-3);display:flex">${icon("right")}</span>
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
  // Вход от предыдущего шага — часть ответа на вопрос «откуда это взялось», поэтому живёт рядом
  // с доской, а не в отдельной вкладке.
  function inputHTML() {
    const r = (openData || {}).input_received;
    if (!r) return "";
    const from = r["от"] || {};
    const who = [from["агент"], from["шаг"] ? "шаг " + from["шаг"] : "", from["прогон"]].filter(Boolean).join(" · ");
    return `<div style="padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--field);margin:0 0 10px">
      <div style="font-size:12.5px;font-weight:600">Получено на вход: ${esc(r.kind)} · ${esc(String(r["знаков"]))} знаков${r["обрезан"] ? " (показано начало)" : ""}</div>
      ${who ? `<div style="font-size:11.5px;color:var(--ink-3);margin-top:3px">от: ${esc(who)}</div>` : ""}
      <div style="margin-top:6px;font-family:var(--mono);font-size:11px;color:var(--ink-2);white-space:pre-wrap;max-height:180px;overflow:auto">${esc(String(r["блок"] || "").slice(0, 2000))}</div>
    </div>`;
  }

  function boardHTML() {
    if (boardErr) return `<div style="font-size:12.5px;color:var(--warn-ink)">Доска не открылась: ${esc(boardErr)}</div>`;
    if (!boardData) return `<div class="skeleton" style="height:90px"></div>`;
    const b = boardData;
    const entries = b.entries || [], contr = b.contradictions || [], arb = b.arbitration || {};
    const got = inputHTML();
    if (!entries.length) {
      return got + `<div style="font-size:12.5px;color:var(--ink-3)">Навыки этого прогона ничего не выкладывали на общую доску — она нужна, когда над задачей работают несколько ветвей.</div>`;
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
    return `${got}${snap}${decisions}${disputes}
      <div style="${LBL};margin:12px 0 4px">что выложено на доску</div>${rows}`;
  }

  // ── Доставка: что ушло наружу, что ещё ждёт человека ──────────────────────────────────────────
  //
  // Было: строка серым текстом, в которой состояние печаталось машинным словом (`awaiting_hitl`),
  // и ни одной кнопки. На прогоне run-plan-pr2930.v1-d4bf39 это дало тупик: четыре задачи в
  // OpenProject ждали подтверждения, человек видел бледную строку «awaiting_hitl» и не мог ни
  // понять, ушло ли, ни подтвердить — очередь живёт в другом разделе, а решённая заявка исчезает
  // и оттуда. Теперь состояние названо словами, ожидание выделено подложкой, а подтверждение
  // стоит рядом с тем, что подтверждают.
  const РЕЖИМ = {
    "real": ["отправлено", "var(--ok-ink)"],
    "sent": ["отправлено", "var(--ok-ink)"],
    "awaiting_hitl": ["ждёт вашего подтверждения", "var(--warn-ink)"],
    "dry_run": ["черновик — наружу не уходило", "var(--ink-2)"],
    "rejected": ["оставлено черновиком", "var(--ink-2)"],
    "denied": ["доступ закрыт", "var(--danger-ink)"],
    "skipped": ["пропущено", "var(--ink-2)"],
    "только_чтение": ["режим только чтения — наружу ничего не уходит", "var(--ink-2)"],
  };

  function deliveryHTML(dl) {
    if (!dl.length) return `<div style="font-size:12.5px;color:var(--ink-3)">Ничего наружу не уходило.</div>`;
    const ждут = dl.filter((x) => String(x.mode || "") === "awaiting_hitl" && x.hitl_id).length;
    const строки = dl.map((x) => {
      const режим = String(x.mode || "");
      const [слово, цвет] = РЕЖИМ[режим] || [режим || "состояние не объявлено", "var(--ink-2)"];
      const ждёт = режим === "awaiting_hitl";
      const кнопки = ждёт && x.hitl_id
        ? `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
            <button type="button" class="dGo" data-id="${esc(x.hitl_id)}" style="${BTN_OK}">${icon("shield")}Показать и подтвердить</button>
            <button type="button" class="dNo" data-id="${esc(x.hitl_id)}" style="${BTN}">Оставить черновиком</button></div>`
        : ждёт
          ? `<div style="font-size:11.5px;color:var(--ink-3);margin-top:6px">Заявка на подтверждение к этой записи не привязана — подтвердить отсюда нечем, откройте раздел «Решения».</div>`
          : "";
      return `<div class="${ждёт ? "wait-row" : ""}" style="${ждёт ? "" : "border:1px solid var(--line);border-radius:var(--r-md);padding:10px 12px;background:var(--field);"}margin:6px 0">
        <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
          <span style="font-size:12.5px;font-weight:650">${esc(x.title || x.channel || "доставка")}</span>
          <span style="font-size:11.5px;font-weight:700;color:${цвет}">${esc(слово)}</span>
        </div>
        <div style="font-size:12px;color:var(--ink-2);margin-top:3px">${esc(x.channel || "")}${x.to ? " → " + esc(x.to) : " · адресат не указан"}${x.subject ? " · «" + esc(String(x.subject).slice(0, 120)) + "»" : ""}</div>
        ${x.result ? `<div style="font-size:11.5px;color:var(--ink-3);margin-top:3px">${esc(String(x.result).slice(0, 200))}</div>` : ""}
        ${кнопки}</div>`;
    }).join("");
    const шапка = ждут
      ? `<div class="wait-row" style="margin:0 0 8px"><span class="wait-head" style="font-size:12.5px;display:inline-flex;align-items:center;gap:7px">${icon("shield")}Ждёт вашего решения: ${ждут}</span>
          <div style="font-size:12px;color:var(--ink-2);margin-top:4px">Наружу ничего не ушло. Прогон дошёл до конца, действие собрано и ждёт — подтвердите или оставьте черновиком.</div></div>`
      : "";
    return шапка + строки;
  }

  async function решитьДоставку(id, decision) {
    // Решение идёт тем же путём, что в «Решениях»: одна ручка — один смысл. Перед «да» человек
    // обязан увидеть предмет целиком (UX-аудит 25.09, D-C3): подтверждать невидимое нельзя.
    const D = "/api/modules/decisions/item/" + encodeURIComponent(id);
    if (decision === "approve") {
      let предмет = null;
      try { предмет = await api(D); }
      catch (e) { toast(humanError(e), "danger"); return; }
      const поля = (предмет.command_fields || []).map(([k, v]) => [String(k), String(v)]);
      const ок = await gate({
        title: предмет.title || "Внешнее действие",
        kicker: "требуется ваше решение",
        fields: [["кому", предмет.to || "—"], ["канал", предмет.channel || "—"]].concat(поля),
        html: предмет.html ? sanitizeHtml(предмет.html) : "",
        body: предмет.html ? "" : (предмет.body || "Содержимое не приложено."),
        allowLabel: "Подтвердить", denyLabel: "Отмена",
        note: predmetНота(предмет),
      });
      if (!ок) return;
    }
    try {
      await api(D + "/decide", { method: "POST", body: JSON.stringify({ decision, reason: "" }) });
      toast(decision === "approve" ? "Подтверждено — ушло" : "Осталось черновиком", "ok");
    } catch (e) {
      // «Заявка уже обработана» — не ошибка интерфейса, а устаревшая карточка: перечитываем.
      toast(humanError(e), "danger");
    }
    // Перечитываем карточку — и остаёмся на той же вкладке: человек решал доставку, а не уходил
    // смотреть находки.
    await openRun(open);
    openTab = "delivery"; render();
  }

  const predmetНота = (предмет) => (предмет.kind === "command"
    ? "Действие выполнится в вашей системе. Ссылка на созданный объект вернётся в карточку прогона."
    : "Отправится только после вашего подтверждения.");

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
    // Счётчик на вкладке — это не украшение: «Доставка» без числа выглядела пустой, и человек
    // не открывал её, пока агент не напоминал о себе сам.
    const дост = d.delivery || [];
    const ждутРешения = дост.filter((x) => String(x.mode || "") === "awaiting_hitl").length;
    const tabs = [["findings", "Находки · " + fnd.length, ""], ["skills", "Навыки · " + so.length, ""],
                  ["board", "Доска" + (bs.entries ? " · " + bs.entries : ""), ""],
                  ["delivery", "Доставка" + (дост.length ? " · " + дост.length : ""), ждутРешения ? "shield" : "ext"],
                  ["metrics", "Затраты", ""]]
      .map(([id, t, зн]) => `<button type="button" class="btn sm rtab${id === openTab ? " primary" : ""}${
        id === "delivery" && ждутРешения && openTab !== id ? " ok" : ""}" data-tab="${id}">${
        зн ? icon(зн) : ""}${esc(t)}</button>`).join("");
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
      body = deliveryHTML(d.delivery || []);
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
        <button class="ico" id="oBack" title="К журналу" aria-label="Вернуться к журналу прогонов">${icon("left", "lg")}</button>
        <span style="font-size:16px;font-weight:700;letter-spacing:-.3px">${esc(d.agent_name || d.agent_id || "")}</span>
        <span style="font-size:11px;font-family:var(--mono);color:var(--ink-3)">${esc(fmtDate(d.created_at || ""))}</span>
        <span class="chip${v.ok ? " on" : ""}" style="color:${v.ok ? "var(--ok-ink)" : "var(--warn-ink)"};border-color:${v.ok ? "var(--ok-line)" : "var(--warn-line)"}">${
          icon(v.ok ? "check" : "alert")}${v.ok ? "пройден" : "есть замечания"}${v.autonomy_used ? " · " + esc(v.autonomy_used) : ""}</span>
        <span style="margin-left:auto;display:flex;gap:7px;align-items:center">${tplOpts}
          <button class="btn sm" id="oHtml">${icon("doc")}Показать отчёт</button>
          <button class="btn sm primary" id="oPdf">${icon("download")}Отчёт PDF</button>
          <button class="btn sm" id="oDiff">Сравнить с прошлым</button></span>
      </div>
      <div style="font-size:11.5px;color:var(--ink-3);font-family:var(--mono)">${esc(open)}</div>
      ${числаПрогона(d)}
      ${итогHTML()}
      ${miss.length ? `<div style="font-size:12px;color:var(--warn-ink)">Навыки заполнили схему не полностью: ${esc(miss.map((m) => m.skill).join(", "))}. В отчёте это отмечено.</div>` : ""}
      <div style="display:flex;gap:7px;flex-wrap:wrap">${tabs}</div>
      <div style="max-height:52vh;overflow:auto">${body}</div></div>`;
  }

  // Уход из раздела (переключили рейл, закрыли окно) — последняя возможность не потерять замер.
  window.addEventListener("beforeunload", () => { отметить(); });

  function render() {
    root.innerHTML = `<div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:14px;padding:26px 28px;overflow:auto;animation:ape-in .3s ease-out">
      ${head({
        "глаз": "журнал",
        "что": "Прогоны",
        "подпись": "Вся работа агентов: находки целиком, доставка, затраты и отчёт. Записи приходят с сервера ABOP и не зависят от чатов.",
      })}
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
    if ($("oBack")) $("oBack").onclick = () => {
      отметить();                   // время у этой карточки записано до того, как она закрылась
      open = null; openData = null; boardData = null; boardErr = ""; итог = null; render();
    };
    if ($("oRetry")) $("oRetry").onclick = () => openRun(open);
    root.querySelectorAll(".rtab").forEach((b) => { b.onclick = async () => { openTab = b.dataset.tab; render(); if (openTab === "board" && !boardData && !boardErr) await loadBoard(); }; });
    root.querySelectorAll(".dGo").forEach((b) => { b.onclick = () => решитьДоставку(b.dataset.id, "approve"); });
    root.querySelectorAll(".dNo").forEach((b) => { b.onclick = () => решитьДоставку(b.dataset.id, "reject"); });
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
        const safe = ctx.sanitizeHtml(r.html);   // общий санитайзер ядра, а не своя копия
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
