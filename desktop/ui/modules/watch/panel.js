// Раздел «Присмотр» — папка на диске, новый файл в которой сам становится задачей агента.
// Папка местная: акты падают во «Входящие», выписки выгружаются в папку банка, сканы приходят из
// МФУ. Веб этого не видит по определению — поэтому раздел живёт в приложении.
//
// Главное в этом экране — не список папок, а ЖУРНАЛ: что взято, что не вышло и почему. Присмотр,
// молча не берущий файлы, хуже отсутствия присмотра — человек считает, что работа идёт.
import { LBL, CARD, INP, head } from "../../core/cards.js";
// Стили — из ядра: шесть разных карточек и пять разных подписей в панелях были
// расхождением, а не выбором (DIZAJN, раздел 2).
const W = "/api/modules/watch";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Цвет состояния в журнале: взят — сделано, не вышло — требует человека, был до присмотра — не
// потерян, но и не взят. Третье состояние нужно именно потому, что иначе его приняли бы за отказ.
const СОСТ = {
  "взят": "var(--ok-ink)",
  "не вышло": "var(--danger-ink)",
  "был до присмотра": "var(--ink-3)",
};

export async function mount(root, ctx) {
  const { api, toast, humanError, confirm } = ctx;
  let data = null, agents = [], err = "", loading = true, busy = false;
  let openId = 0;

  async function load() {
    loading = true; render();
    try {
      data = await api(W + "/list");
      err = "";
      if (!agents.length) {
        try { agents = await api("/api/modules/chat/abop-agents"); }
        catch { agents = []; }   // без списка агентов папку не добавить — скажем об этом в форме
      }
    } catch (e) { err = humanError(e); data = null; }
    loading = false; render();
  }

  function когда(t) {
    if (!t) return "—";
    return new Date(Number(t) * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
  }

  function сводкаHTML() {
    const d = data || {};
    const last = d.last || {};
    const ждёт = d.authed === false;
    return `<div style="${CARD}">
      <div style="display:flex;gap:22px;flex-wrap:wrap;align-items:baseline">
        <div style="display:flex;flex-direction:column;gap:2px;min-width:150px">
          <span style="${LBL}">папок под присмотром</span>
          <span style="font-size:17px;font-weight:700">${esc(String((d.folders || []).filter((f) => f.active).length))}</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:2px;min-width:150px">
          <span style="${LBL}">последний обход</span>
          <span style="font-size:13px;color:var(--ink-2)">${esc(когда(last.at))}</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:2px;min-width:150px">
          <span style="${LBL}">взято в обходе</span>
          <span style="font-size:17px;font-weight:700">${esc(String(last["взято"] ?? 0))}</span>
        </div>
        <button class="btn sm" id="wScan" style="margin-left:auto">Обойти сейчас</button>
      </div>
      ${ждёт ? `<div style="font-size:12.5px;color:var(--warn-ink)">Присмотр ждёт входа в ABOP: папка привязана к установке и человеку, и присматривать «за кого-то» нельзя.</div>` : ""}
      ${last["почему"] ? `<div style="font-size:12.5px;color:var(--ink-2)">${esc(last["почему"])}</div>` : ""}
      <div style="font-size:11.5px;color:var(--ink-3);line-height:1.5">Обход раз в ${esc(String(Math.round(d.interval || 20)))} с, за обход берётся не больше ${esc(String(d.limit || 5))} файлов — выгрузка в пятьсот документов не превратится в пятьсот прогонов за минуту. Читаются ${esc((d.reads || []).join(", "))}; файл, который ещё копируется, не берётся.</div>
    </div>`;
  }

  function формаHTML() {
    const opts = agents.map((a) => `<option value="${esc(a.id)}">${esc(a.name || a.id)}</option>`).join("");
    return `<div style="${CARD}">
      <span style="${LBL}">добавить папку</span>
      <div style="display:flex;gap:9px;flex-wrap:wrap;align-items:flex-end">
        <label style="display:flex;flex-direction:column;gap:3px;flex:1;min-width:220px"><span style="${LBL}">папка</span>
          <div style="display:flex;gap:6px">
            <input id="wPath" placeholder="C:\\Входящие\\Акты" style="${INP};flex:1"/>
            <button class="btn sm" id="wPick" title="Выбрать папку">Выбрать…</button>
          </div></label>
        <label style="display:flex;flex-direction:column;gap:3px;min-width:200px"><span style="${LBL}">агент</span>
          <select id="wAgent" style="${INP}">${opts || `<option value="">— список агентов не получен —</option>`}</select></label>
        <label style="display:flex;flex-direction:column;gap:3px;min-width:140px"><span style="${LBL}">расширения</span>
          <input id="wMask" placeholder="pdf, xlsx" style="${INP}"/></label>
        <label style="display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--ink-2)">
          <input type="checkbox" id="wTake" style="accent-color:var(--accent)"/> взять и то, что уже лежит</label>
        <button class="btn primary sm" id="wAdd" ${busy ? "disabled" : ""}>Присматривать</button>
      </div>
      <div style="font-size:11.5px;color:var(--ink-3);line-height:1.5">По умолчанию файлы, лежавшие в папке до включения присмотра, НЕ берутся: указать на «Загрузки» — обычное дело, а сто прогонов «на всякий случай» стоят денег. Они попадут в журнал как «был до присмотра», и взять их можно отдельной кнопкой.</div>
    </div>`;
  }

  function папкаHTML(f) {
    const on = openId === f.id;
    const с = f["счёт"] || {};
    const чип = (k, v, цвет) => v ? `<span style="font-size:10.5px;padding:2px 7px;border-radius:6px;border:1px solid var(--line);color:${цвет}">${esc(k)}: ${esc(String(v))}</span>` : "";
    return `<div style="${CARD}">
      <div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap">
        <span style="font-family:var(--mono);font-size:12.5px;color:var(--ink);word-break:break-all">${esc(f.path)}</span>
        ${f.exists ? "" : `<span style="font-size:10.5px;padding:2px 7px;border-radius:6px;background:var(--danger-bg);color:var(--danger-ink)">папки нет на диске</span>`}
        <span style="font-size:10.5px;padding:2px 7px;border-radius:6px;background:${f.active ? "var(--ok-bg)" : "var(--hover)"};color:${f.active ? "var(--ok-ink)" : "var(--ink-3)"}">${f.active ? "присматриваю" : "на паузе"}</span>
        ${чип("взято", с["взят"], "var(--ok-ink)")}
        ${чип("не вышло", с["не вышло"], "var(--danger-ink)")}
        ${чип("было до", с["был до присмотра"], "var(--ink-3)")}
      </div>
      <div style="font-size:12px;color:var(--ink-3)">агент: ${esc(f.agent_name || f.agent_id)}${f.mask ? " · только " + esc(f.mask) : ""}${f.thread_id ? " · результаты в чате «Присмотр: " + esc(f.path.split(/[\\/]/).pop()) + "»" : ""}</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn sm wOpen" data-id="${f.id}">${on ? "Скрыть журнал" : "Журнал"}</button>
        <button class="btn sm wPause" data-id="${f.id}" data-on="${f.active ? 0 : 1}">${f.active ? "Пауза" : "Продолжить"}</button>
        ${с["был до присмотра"] ? `<button class="btn sm wTakeOld" data-id="${f.id}">Взять то, что лежало (${esc(String(с["был до присмотра"]))})</button>` : ""}
        ${f.thread_id ? `<button class="btn sm wChat" data-tid="${f.thread_id}">Открыть чат</button>` : ""}
        <button class="btn sm danger wDel" data-id="${f.id}">Убрать</button>
      </div>
      ${on ? журналHTML(f) : ""}
    </div>`;
  }

  function журналHTML(f) {
    const j = f["журнал"] || [];
    if (!j.length) return `<div style="font-size:12px;color:var(--ink-3);margin-top:8px">Присмотр ещё ничего не видел в этой папке.</div>`;
    return `<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:5px">
      <span style="${LBL}">последние файлы</span>
      ${j.map((r) => `<div style="display:grid;grid-template-columns:1fr auto;gap:8px;align-items:baseline">
        <div style="min-width:0">
          <span style="font-size:12.5px;color:var(--ink);word-break:break-all">${esc(r.name)}</span>
          <span style="font-size:11.5px;color:${СОСТ[r.state] || "var(--ink-3)"};margin-left:8px">${esc(r.state)}</span>
          ${r.note ? `<div style="font-size:11.5px;color:var(--ink-2);line-height:1.45">${esc(r.note)}</div>` : ""}
        </div>
        <span style="font-size:11px;color:var(--ink-3);font-family:var(--mono);white-space:nowrap">${esc(когда(r.at))}</span>
      </div>`).join("")}
    </div>`;
  }

  function render() {
    const folders = (data && data.folders) || [];
    let body;
    if (loading) body = `<div style="${CARD}"><div class="skeleton" style="height:18px;width:40%"></div><div class="skeleton" style="height:90px"></div></div>`;
    else if (err) body = `<div style="${CARD};border-color:var(--danger-line)">
        <div style="font-weight:700">Присмотр недоступен</div>
        <div style="font-size:12.5px;color:var(--ink-2)">${esc(err)}</div>
        <button class="btn sm" id="wRetry" style="align-self:flex-start">Повторить</button></div>`;
    else if (!folders.length) body = `<div style="${CARD}">
        <div style="font-weight:700">Пока ни одной папки</div>
        <div style="font-size:12.5px;color:var(--ink-2);line-height:1.55">Укажите папку, куда складывают документы, и агента, который должен их разбирать. Новый файл станет задачей сам, а результат придёт в чат и уведомлением.</div></div>`;
    else body = `<div style="display:flex;flex-direction:column;gap:9px">${folders.map(папкаHTML).join("")}</div>`;

    root.innerHTML = `<div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:14px;padding:26px 28px;overflow:auto;animation:ape-in .3s ease-out">
      ${head({ "глаз": "документы с диска", "что": "Присмотр",
               "подпись": "Новый файл в папке сам становится задачей агента. Что взято и что не вышло — видно в журнале папки: присмотр, молча не берущий файлы, хуже отсутствия присмотра." })}
      ${loading || err ? "" : сводкаHTML()}
      ${loading || err ? "" : формаHTML()}
      ${body}</div>`;
    wire();
  }

  function wire() {
    const $ = (id) => root.querySelector("#" + id);
    if ($("wRetry")) $("wRetry").onclick = load;
    if ($("wScan")) $("wScan").onclick = async () => {
      try { const r = await api(W + "/scan", { method: "POST" }); toast(r["взято"] ? ("Взято файлов: " + r["взято"]) : (r["почему"] || "Новых файлов нет"), r["взято"] ? "ok" : ""); }
      catch (e) { toast(humanError(e), "danger"); }
      load();
    };
    // Выбор папки диалогом ОС. В браузере моста нет — поле остаётся для ручного ввода пути.
    if ($("wPick")) $("wPick").onclick = async () => {
      const мост = window.ape?.os?.pickFolder;
      if (!мост) return toast("Выбор папки доступен в приложении — здесь впишите путь вручную", "warn");
      try { const p = await мост(); if (p) $("wPath").value = p; }
      catch (e) { toast("Диалог не открылся: " + humanError(e), "danger"); }
    };
    if ($("wAdd")) $("wAdd").onclick = async () => {
      const path = String($("wPath").value || "").trim();
      const agent = String($("wAgent").value || "");
      if (!path) return toast("Укажите папку", "danger");
      if (!agent) return toast("Выберите агента: присмотр без агента ничего не делает", "danger");
      busy = true; render();
      try {
        const r = await api(W + "/add", { method: "POST", body: JSON.stringify({
          path, agent_id: agent, agent_name: (agents.find((a) => a.id === agent) || {}).name || agent,
          mask: String($("wMask").value || "").trim(), take_existing: !!$("wTake").checked }) });
        if (!r.ok) toast(r.error || "Папку не добавить", "danger");
        else toast(r.skipped ? ("Присматриваю. Файлов, лежавших до включения: " + r.skipped + " — они в журнале") : "Присматриваю за папкой", "ok");
      } catch (e) { toast(humanError(e), "danger"); }
      busy = false; await load();
    };
    root.querySelectorAll(".wOpen").forEach((b) => b.onclick = () => {
      openId = openId === Number(b.dataset.id) ? 0 : Number(b.dataset.id); render();
    });
    root.querySelectorAll(".wPause").forEach((b) => b.onclick = async () => {
      try { await api(W + "/" + b.dataset.id + "/pause?on=" + b.dataset.on, { method: "POST" }); }
      catch (e) { toast(humanError(e), "danger"); }
      load();
    });
    root.querySelectorAll(".wTakeOld").forEach((b) => b.onclick = async () => {
      const ok = await confirm({
        title: "Взять файлы, лежавшие до присмотра?",
        text: "Каждый файл станет отдельным прогоном агента — это расход токенов и денег. Брать их по одному можно, перетащив файл в чат.",
        okLabel: "Взять все", danger: true, kicker: "Присмотр" });
      if (!ok) return;
      try { const r = await api(W + "/" + b.dataset.id + "/take-existing", { method: "POST" }); toast("К работе принято файлов: " + (r.released || 0), "ok"); }
      catch (e) { toast(humanError(e), "danger"); }
      load();
    });
    root.querySelectorAll(".wChat").forEach((b) => b.onclick = () => ctx.open("chat", { thread: Number(b.dataset.tid) }));
    // Намерение «открыть этот чат» обрабатывает сам чат (handleIntent): раздел присмотра не знает
    // про его внутреннее состояние и не должен.
    root.querySelectorAll(".wDel").forEach((b) => b.onclick = async () => {
      const ok = await confirm({
        title: "Убрать папку из присмотра?",
        text: "Файлы на диске и уже полученные результаты останутся. Пропадёт журнал присмотра за этой папкой — а с ним память о том, какие файлы уже брались: включив её снова, вы получите прогоны по тем же документам.",
        okLabel: "Убрать", danger: true, kicker: "Присмотр" });
      if (!ok) return;
      try { await api(W + "/" + b.dataset.id, { method: "DELETE" }); toast("Папка убрана", "ok"); }
      catch (e) { toast(humanError(e), "danger"); }
      load();
    });
  }

  await load();
  // Журнал меняется сам, без действий человека: присмотр идёт в фоне.
  const t = setInterval(() => { if (root.isConnected) load(); else clearInterval(t); }, 30000);
}
