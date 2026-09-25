// Модуль «Источники» — тонкий клиент ABOP: каталог коннекторов и рецептов Data Plane из ABOP
// (что реально видит агент, ABAC по отделу) + локальные файлы под правами пользователя ОС.
const K = "/api/modules/connectors";
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const LBL = "font-family:var(--mono);font-size:9.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3)";
const CARD = "padding:20px;border-radius:16px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:14px;box-shadow:var(--shadow-1);backdrop-filter:var(--blur)";

function row(icon, title, note, right) {
  return `<div style="display:flex;align-items:center;gap:12px;padding:11px 13px;border-radius:11px;background:var(--hover);border:1px solid var(--line)">
    <span style="font-size:15px">${icon}</span>
    <span style="flex:1;min-width:0;display:flex;flex-direction:column;gap:2px"><span style="font-size:12.5px;font-weight:600">${esc(title)}</span><span style="font-size:11px;color:var(--ink-3)">${esc(note)}</span></span>
    ${right || ""}</div>`;
}

export async function mount(root, ctx) {
  const { api } = ctx;
  let cat = {}; try { cat = await api(K + "/list"); } catch {}

  const abopConns = (cat.connectors || []).map((c) => {
    const ok = c.status !== "blocked";
    const right = ok ? `<span class="chip on">агент видит</span>`
      : `<span class="chip" title="${esc(c.access_reason || "")}">нет доступа</span>`;
    return row("🗄", c.title, c.note, right);
  }).join("");

  const abopRecipes = (cat.recipes || []).map((r) =>
    row("🧬", r.title, [r.entity, r.note].filter(Boolean).join(" · "), `<span class="chip on">рецепт</span>`)
  ).join("");

  const localConns = (cat.local || []).map((c) => row("📄", c.title, c.note, `<span class="chip on">готов</span>`)).join("");

  const abopBlock = cat.abop_error
    ? `<div style="${CARD};gap:8px;border-color:var(--danger-line,rgba(239,68,68,.4))"><span style="${LBL}">ABOP</span><span style="font-size:12.5px;color:var(--danger-ink)">Не удалось получить источники из ABOP: ${esc(cat.abop_error)}</span></div>`
    : `${abopConns ? `<div style="${CARD};gap:12px"><span style="${LBL}">коннекторы ABOP · Data Plane</span>${abopConns}</div>` : ""}
       ${abopRecipes ? `<div style="${CARD};gap:12px"><span style="${LBL}">рецепты ABOP · источник → entity</span>${abopRecipes}</div>` : ""}`;

  root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
    <div style="max-width:1020px;margin:0 auto;display:flex;flex-direction:column;gap:20px;animation:ape-in .35s ease-out">
      <div style="display:flex;flex-direction:column;gap:6px">
        <h1 style="margin:0;font-size:26px;font-weight:800;letter-spacing:-.7px">Рабочие источники</h1>
        <p style="margin:0;font-size:13px;color:var(--ink-2)">Коннекторы и рецепты Data Plane тянутся из ABOP — это ровно то, что видит агент под вашими правами (ABAC). Локальные файлы читаются под вашими правами ОС.</p>
      </div>

      ${abopBlock || `<div style="${CARD}"><span style="${LBL}">коннекторы</span><span style="font-size:12.5px;color:var(--ink-3)">В ABOP пока нет подключённых коннекторов.</span></div>`}

      <div style="${CARD};gap:12px"><span style="${LBL}">локальные источники (права ОС)</span>${localConns}</div>

      <div style="${CARD};gap:12px">
        <span style="${LBL}">добавить локальный файл</span>
        <p style="margin:0;font-size:12.5px;color:var(--ink-2)">docx · xlsx · csv · txt · json · md → распознаём и кладём в знания треда.</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn primary" id="pickFile">Выбрать файл…</button>
          <input type="file" id="fileIn" accept=".txt,.md,.csv,.json,.docx,.xlsx" style="display:none"/>
        </div>
        <div id="ingRes" style="font-size:12.5px;color:var(--ink-3)"></div>
      </div>

      <div style="${CARD};gap:10px">
        <span style="${LBL}">последние рабочие файлы</span>
        <div id="recent" style="font-size:12.5px;color:var(--ink-3)">Загрузка…</div>
      </div>

      <div style="font-size:12px;color:var(--ink-3)">Удалённые системы (CRM/ERP/почта) подключаются администратором в ABOP (реестр систем + коннекторы Data Plane), агент ходит под вашими правами (ABAC/делегированный доступ).</div>
    </div></div>`;
  const $ = (id) => root.querySelector("#" + id);

  async function ingestPath(path) {
    $("ingRes").textContent = "Читаю…";
    const r = await api(K + "/ingest", { method: "POST", body: JSON.stringify({ path }) });
    $("ingRes").innerHTML = r.ok
      ? `<span style="color:var(--ok-ink)">✓ «${esc(r.name)}» распознан (${r.chars} симв.) — откройте чат и приложите через 📎, текст попадёт в контекст.</span>`
      : `<span style="color:var(--danger-ink)">Не удалось: ${esc(r.error === "not_found" ? "файл не найден" : r.error)}</span>`;
  }

  // выбор файла: Electron file input даёт .path (полный путь) — читаем под правами ОС
  $("pickFile").onclick = () => $("fileIn").click();
  $("fileIn").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    if (f.path) { await ingestPath(f.path); }
    else { const text = await f.text(); $("ingRes").textContent = "Файл прочитан локально (" + text.length + " симв.) — откройте чат и приложите через 📎."; }
    e.target.value = "";
  };

  try {
    const rec = await api(K + "/recent");
    $("recent").innerHTML = (rec.files || []).length
      ? rec.files.map((f) => `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)">
          <span style="flex:1;min-width:0;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.name)}</span>
          <button class="rec" data-p="${esc(f.path)}" style="padding:5px 11px;border:1px solid var(--line);border-radius:9px;background:transparent;color:var(--ink-2);font-size:11.5px;cursor:pointer">Распознать</button></div>`).join("")
      : `<span>Нет недавних файлов в Documents/Downloads/Desktop.</span>`;
    $("recent").querySelectorAll(".rec").forEach((b) => b.onclick = () => ingestPath(b.dataset.p));
  } catch { $("recent").textContent = "Не удалось получить список."; }
}
