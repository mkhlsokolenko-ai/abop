// Модуль «Источники» — рабочие коннекторы (вёрстка в рецептах ДС).
// Локальные (файлы/последние) работают под правами пользователя ОС; удалённые — planned.
const K = "/api/modules/connectors";
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const LBL = "font-family:var(--mono);font-size:9.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3)";
const CARD = "padding:20px;border-radius:16px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:14px";

export async function mount(root, ctx) {
  const { api } = ctx;
  let cat = {}; try { cat = await api(K + "/list"); } catch {}
  const conns = (cat.connectors || []).map((c) => {
    const ready = c.status === "ready";
    const remote = c.kind === "remote";
    const right = ready ? `<span class="chip on">готов</span>`
      : remote ? `<button class="cconn" data-id="${c.id}" data-title="${esc(c.title)}" style="padding:7px 12px;border:1px solid rgba(129,140,248,.4);border-radius:9px;background:rgba(99,102,241,.16);color:var(--accent-ink);font-size:11.5px;font-weight:600;cursor:pointer">Подключить</button>`
      : `<span class="chip">скоро</span>`;
    return `<div style="display:flex;align-items:center;gap:12px;padding:11px 13px;border-radius:11px;background:var(--hover);border:1px solid var(--line)">
      <span style="font-size:15px">${remote ? "🗄" : "📄"}</span>
      <span style="flex:1;display:flex;flex-direction:column;gap:2px"><span style="font-size:12.5px;font-weight:600">${esc(c.title)}</span><span style="font-size:11px;color:var(--ink-3)">${esc(c.note)}</span></span>
      ${right}</div>`;
  }).join("");

  root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
    <div style="max-width:1020px;margin:0 auto;display:flex;flex-direction:column;gap:20px;animation:ape-in .35s ease-out">
      <div style="display:flex;flex-direction:column;gap:6px">
        <h1 style="margin:0;font-size:26px;font-weight:800;letter-spacing:-.7px">Рабочие источники</h1>
        <p style="margin:0;font-size:13px;color:var(--ink-2)">Подключайте документы и системы — контекст попадёт в знания треда. Локальные файлы читаются под вашими правами ОС.</p>
      </div>

      <div style="${CARD};gap:12px"><span style="${LBL}">коннекторы</span>${conns}</div>

      <div style="${CARD};gap:12px">
        <span style="${LBL}">добавить локальный файл</span>
        <p style="margin:0;font-size:12.5px;color:var(--ink-2)">docx · xlsx · csv · txt · json · md → распознаём и кладём в знания (RAG).</p>
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

      <div style="font-size:12px;color:var(--ink-3)">Удалённые системы (CRM/ERP/почта) — через делегированный доступ (token-exchange Keycloak): агент ходит от вашего имени, а не общим ключом. В разработке.</div>
    </div></div>`;
  const $ = (id) => root.querySelector("#" + id);

  async function ingestPath(path) {
    $("ingRes").textContent = "Читаю и индексирую…";
    const r = await api(K + "/ingest", { method: "POST", body: JSON.stringify({ path }) });
    $("ingRes").innerHTML = r.ok
      ? `<span style="color:var(--ok-ink)">✓ «${esc(r.name)}» в знаниях (${r.indexed} фр., ${r.chars} симв.)</span>`
      : `<span style="color:var(--danger-ink)">Не удалось: ${esc(r.error === "auth_required" ? "нужен вход через GitHub" : r.error)}</span>`;
  }

  // делегированный доступ к удалённой системе (token-exchange от имени пользователя)
  root.querySelectorAll(".cconn").forEach((b) => b.onclick = async () => {
    const o = b.textContent; b.textContent = "Подключаю…"; b.disabled = true;
    const r = await api(K + "/connect", { method: "POST", body: JSON.stringify({ target: b.dataset.id }) });
    if (r.ok && r.delegated) { b.textContent = "✓ подключено"; b.style.color = "var(--ok-ink)"; }
    else {
      b.textContent = o; b.disabled = false;
      alert(r.error === "auth_required" ? "Нужен вход через GitHub"
        : (r.message || "Делегированный доступ к «" + b.dataset.title + "» пока не настроен в Keycloak (нужен IdP/клиент цели). Каркас token-exchange готов."));
    }
  });

  // выбор файла: Electron file input даёт .path (полный путь) — читаем под правами ОС
  $("pickFile").onclick = () => $("fileIn").click();
  $("fileIn").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    if (f.path) { await ingestPath(f.path); }
    else { // fallback (нет пути) — читаем как текст на фронте
      const text = await f.text();
      const r = await api("/api/modules/chat/threads"); // no-op guard
      $("ingRes").textContent = "Файл прочитан локально (" + text.length + " симв.) — открой чат и приложи через 📎.";
    }
    e.target.value = "";
  };

  try {
    const rec = await api(K + "/recent");
    $("recent").innerHTML = (rec.files || []).length
      ? rec.files.map((f) => `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)">
          <span style="flex:1;min-width:0;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.name)}</span>
          <button class="rec" data-p="${esc(f.path)}" style="padding:5px 11px;border:1px solid var(--line);border-radius:9px;background:transparent;color:var(--ink-2);font-size:11.5px;cursor:pointer">В знания</button></div>`).join("")
      : `<span>Нет недавних файлов в Documents/Downloads/Desktop.</span>`;
    $("recent").querySelectorAll(".rec").forEach((b) => b.onclick = () => ingestPath(b.dataset.p));
  } catch { $("recent").textContent = "Не удалось получить список."; }
}
