// Модуль «Находки» — расхождения пилота 1С по всем последним прогонам в одном списке.
// В журнале прогонов находка живёт внутри своего прогона: чтобы увидеть картину целиком, надо было
// открыть десяток карточек. Здесь — один список с фильтрами и четырьмя ответами по каждой находке:
// что не сходится, откуда это видно, чем грозит, что проверить.
const F = "/api/modules/findings";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const LBL = "font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)";
const CARD = "display:flex;flex-direction:column;gap:9px;padding:15px 17px;border-radius:16px;border:1px solid var(--line);background:var(--panel);backdrop-filter:var(--blur)";
const INP = "padding:8px 10px;border-radius:9px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px";
const SEV = { "высокая": "var(--danger-ink)", "средняя": "var(--warn-ink)", "низкая": "var(--ink-3)" };

export async function mount(root, ctx) {
  const { api, toast, humanError } = ctx;
  let data = null, err = "", loading = true;
  let cls = "", severity = "", cross = false, q = "", openId = "";

  async function load() {
    loading = true; render();
    const qs = new URLSearchParams();
    if (cls) qs.set("cls", cls);
    if (severity) qs.set("severity", severity);
    if (cross) qs.set("cross", "1");
    if (q) qs.set("q", q);
    try { data = await api(F + "/list?" + qs.toString()); err = ""; }
    catch (e) { err = humanError(e); data = null; }
    loading = false; render();
  }

  function metricsHTML() {
    const m = (data && data.metrics) || {};
    if (!m.total) return "";
    // Метрики пилота считаются по экспертной разметке. Пока её нет — честно говорим, что не считаны,
    // вместо того чтобы показать ноль и выдать его за результат.
    const cell = (label, value, ok, note) => `<div style="display:flex;flex-direction:column;gap:2px;min-width:132px">
      <span style="${LBL}">${esc(label)}</span>
      <span style="font-size:17px;font-weight:700;color:${value == null ? "var(--ink-3)" : (ok ? "var(--ok-ink)" : "var(--warn-ink)")}">${value == null ? "не считана" : esc(String(value))}</span>
      <span style="font-size:11px;color:var(--ink-3)">${esc(note)}</span></div>`;
    const th = m.thresholds || {};
    return `<div style="${CARD}">
      <span style="${LBL}">метрики пилота</span>
      <div style="display:flex;gap:22px;flex-wrap:wrap">
        ${cell("находок", m.total, true, `размечено экспертом: ${m.labeled || 0}`)}
        ${cell("точность", m.precision == null ? null : m.precision + "%", m.precision_ok, `порог ${th.precision || 80}%`)}
        ${cell("межучастковых", m.cross_share == null ? null : m.cross_share + "%", m.cross_ok, `порог ${th.cross_share || 50}%`)}
        ${cell("не нашли бы вручную", m.manual_miss, m.manual_miss_ok, `порог ${th.manual_miss || 3}`)}
      </div>
      ${!m.labeled ? `<div style="font-size:11.5px;color:var(--ink-3)">Точность считается по слепой разметке эксперта — её ставят в веб-интерфейсе. Пока разметки нет, доля межучастковых показана по всем находкам, а не по подтверждённым.</div>` : ""}
    </div>`;
  }

  function filterHTML() {
    const d = data || {};
    const opts = (list, cur) => `<option value="">— все —</option>` + (list || []).map((x) => `<option value="${esc(x)}" ${x === cur ? "selected" : ""}>${esc(x)}</option>`).join("");
    return `<div style="${CARD}">
      <div style="display:flex;gap:9px;flex-wrap:wrap;align-items:center">
        <input id="fq" value="${esc(q)}" placeholder="Поиск: документ, контрагент, проверка…" style="${INP};flex:1;min-width:220px"/>
        <label style="display:flex;flex-direction:column;gap:3px"><span style="${LBL}">класс</span>
          <select id="fcls" style="${INP}">${opts(d.classes, cls)}</select></label>
        <label style="display:flex;flex-direction:column;gap:3px"><span style="${LBL}">существенность</span>
          <select id="fsev" style="${INP}">${opts(d.severities, severity)}</select></label>
        <label style="display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--ink-2);margin-top:14px">
          <input type="checkbox" id="fcross" ${cross ? "checked" : ""} style="accent-color:var(--accent)"/> только межучастковые</label>
        <button class="btn sm" id="fReload" style="margin-top:14px">Обновить</button>
      </div>
      <div style="font-size:11.5px;color:var(--ink-3)">Показано ${esc(String(d.shown || 0))} из ${esc(String(d.total || 0))} находок по ${esc(String((d.runs || []).length))} последним прогонам.</div>
    </div>`;
  }

  function cardHTML(c) {
    const e = c.explain || {};
    const on = openId === c.id;
    const lab = c.label || null;
    const labChip = lab
      ? `<span style="font-size:10.5px;padding:2px 7px;border-radius:6px;border:1px solid var(--line);background:var(--hover);color:var(--ink-2)">эксперт: ${esc(lab.decision === "confirmed" ? "подтвердил" : lab.decision === "rejected" ? "отклонил" : "сомневается")}</span>`
      : "";
    const row = (k, v) => v ? `<div style="display:grid;grid-template-columns:120px 1fr;gap:10px;margin-top:6px">
        <span style="${LBL}">${esc(k)}</span><span style="font-size:12.5px;color:var(--ink-2);line-height:1.55">${esc(v)}</span></div>` : "";
    return `<div class="fcard" data-id="${esc(c.id)}" style="${CARD};cursor:pointer">
      <div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap">
        <span style="font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:6px;background:var(--accent-bg);color:var(--accent-ink)">${esc(c.cls || "")}</span>
        <span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:${SEV[c.severity] || "var(--ink-3)"}">${esc(c.severity || "")}</span>
        <span style="font-family:var(--mono);font-size:11.5px;color:var(--ink-3)">${esc(c.id)}</span>
        ${c.cross ? `<span title="Расхождение видно только на стыке участков" style="font-size:10.5px;padding:2px 7px;border-radius:6px;border:1px solid var(--line);color:var(--ink-2)">${esc(c.section_from)} → ${esc(c.section_to)}</span>` : ""}
        ${labChip}
        ${c.amount_text ? `<span style="margin-left:auto;font-size:12.5px;font-weight:600">${esc(c.amount_text)}</span>` : ""}
      </div>
      <div style="font-size:13.5px;font-weight:600;color:var(--ink)">${esc(c.kind || c.check || "")}</div>
      <div style="font-size:12px;color:var(--ink-3)">${esc(c.doc_line || "")}${c.agent_name ? " · " + esc(c.agent_name) : ""}</div>
      ${on ? `${row("что не сходится", e.what)}${row("откуда", e.where)}${row("чем грозит", e.risk)}
        ${(e.consequences || []).length ? row("последствия", (e.consequences || []).join(" · ")) : ""}
        ${row("что проверить", e.action)}
        ${c.norm && c.norm.title ? row("норма", c.norm.title + (c.norm.section ? " · " + c.norm.section : "")) : ""}
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          ${c.doc_url ? `<a class="btn sm" href="${esc(c.doc_url)}" target="_blank" rel="noopener">Документ в 1С ▸</a>` : ""}
          <button class="btn sm fRun" data-run="${esc(c.run_id)}">Открыть прогон</button>
        </div>` : `<div style="font-size:12px;color:var(--ink-3)">${esc(String(e.what || "").slice(0, 160))}</div>`}
    </div>`;
  }

  function render() {
    const items = (data && data.items) || [];
    let body;
    if (loading) body = `<div style="${CARD}"><div class="skeleton" style="height:18px;width:40%"></div><div class="skeleton" style="height:120px"></div></div>`;
    else if (err) body = `<div style="${CARD};border-color:var(--danger-line)">
        <div style="font-weight:700">Журнал находок недоступен</div>
        <div style="font-size:12.5px;color:var(--ink-2)">${esc(err)}</div>
        <button class="btn sm" id="fRetry" style="align-self:flex-start">Повторить</button></div>`;
    else if (!items.length) body = `<div style="${CARD}">
        <div style="font-weight:700">${(data && data.total) ? "Под фильтры ничего не попало" : "Находок пока нет"}</div>
        <div style="font-size:12.5px;color:var(--ink-2)">${(data && data.total) ? "Всего в журнале находок: " + data.total + ". Снимите фильтры." : "Запустите агента-аудитора 1С — его расхождения появятся здесь."}</div>
        ${(data && data.total) ? `<button class="btn sm" id="fClear" style="align-self:flex-start">Сбросить фильтры</button>` : ""}</div>`;
    else body = `<div style="display:flex;flex-direction:column;gap:9px">${items.map(cardHTML).join("")}</div>`;

    root.innerHTML = `<div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:14px;padding:26px 28px;overflow:auto;animation:ape-in .3s ease-out">
      <div style="display:flex;flex-direction:column;gap:5px">
        <span style="${LBL}">пилот 1С</span>
        <h1 style="margin:0;font-size:26px;font-weight:800;letter-spacing:-.7px">Находки</h1>
        <p style="margin:0;font-size:13px;color:var(--ink-2)">Расхождения по всем последним прогонам аудитора и следователя в одном списке. Числа считает код по ссылкам 1С, объяснение и норму подбирает модель. Разметку эксперта видно, ставится она в веб-интерфейсе.</p>
      </div>
      ${loading || err ? "" : metricsHTML()}
      ${loading || err ? "" : filterHTML()}
      ${body}</div>`;
    wire();
  }

  function wire() {
    const $ = (id) => root.querySelector("#" + id);
    if ($("fRetry")) $("fRetry").onclick = load;
    if ($("fClear")) $("fClear").onclick = () => { cls = ""; severity = ""; cross = false; q = ""; load(); };
    if ($("fReload")) $("fReload").onclick = load;
    if ($("fcls")) $("fcls").onchange = (e) => { cls = e.target.value; load(); };
    if ($("fsev")) $("fsev").onchange = (e) => { severity = e.target.value; load(); };
    if ($("fcross")) $("fcross").onchange = (e) => { cross = e.target.checked; load(); };
    const fq = $("fq");
    if (fq) {
      fq.onkeydown = (e) => { if (e.key === "Enter") { q = fq.value.trim(); load(); } };
      fq.oninput = () => { clearTimeout(wire._t); wire._t = setTimeout(() => { q = fq.value.trim(); load(); }, 420); };
    }
    root.querySelectorAll(".fcard").forEach((el) => el.onclick = (ev) => {
      if (ev.target.closest(".fRun") || ev.target.closest("a")) return;
      openId = openId === el.dataset.id ? "" : el.dataset.id; render();
    });
    root.querySelectorAll(".fRun").forEach((b) => b.onclick = (ev) => { ev.stopPropagation(); ctx.open("runs", { run: b.dataset.run }); });
  }

  await load();
}
