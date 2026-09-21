// Модуль «Агенты» — тонкий клиент ABOP: каталог агентов (ABAC) → запуск → результат + HITL-очередь.
// Логика на сервере ABOP (governance/HITL/кэш/observability); панель лишь показывает и дёргает прокси
// сайдкара (/api/modules/agents/* → ABOP /api/*). См. docs/ADR_DESKTOP_ABOP_SMYCHKA.md (Фаза 1).
const A = "/api/modules/agents";
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const LBL = "font-family:var(--mono);font-size:9.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3)";
const CARD = "padding:18px;border-radius:16px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:12px";
const BTN = "padding:9px 14px;border-radius:11px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12px;font-weight:600;cursor:pointer";

export async function mount(root, ctx) {
  const { api } = ctx;
  let agents = [], hitl = [];

  async function loadAgents() { try { agents = await api(A + "/catalog"); if (!Array.isArray(agents)) agents = []; } catch (e) { agents = []; } }
  async function loadHitl() { try { hitl = await api(A + "/hitl"); if (!Array.isArray(hitl)) hitl = []; } catch (e) { hitl = []; } }

  function render() {
    root.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:18px;padding:20px;max-width:960px;margin:0 auto">
        <div style="display:flex;align-items:center;justify-content:space-between">
          <div><div style="${LBL}">РАНТАЙМ ABOP</div><div style="font-size:18px;font-weight:700;color:var(--ink)">Агенты</div></div>
          <button id="refresh" style="${BTN}">Обновить</button>
        </div>
        ${hitl.length ? `<div style="${CARD};border-color:rgba(245,158,11,.4)">
          <div style="${LBL};color:#fbbf24">НА ПОДТВЕРЖДЕНИИ (HITL) · ${hitl.length}</div>
          ${hitl.map((h) => `<div style="display:flex;align-items:center;gap:10px;justify-content:space-between">
            <span style="font-size:12.5px;color:var(--ink)">${esc(h.title || h.channel)} → ${esc(h.to_addr || "")}</span>
            <span style="display:flex;gap:6px">
              <button class="ap" data-id="${esc(h.id)}" style="${BTN};background:rgba(16,185,129,.16);color:#6ee7b7">Подтвердить</button>
              <button class="rj" data-id="${esc(h.id)}" style="${BTN}">Отклонить</button>
            </span></div>`).join("")}
        </div>` : ""}
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px">
          ${agents.length ? agents.map((a) => `<div style="${CARD}">
            <div><div style="${LBL}">${esc(a.family || "—")} · ${esc(a.role || "")}</div>
              <div style="font-size:15px;font-weight:700;color:var(--ink)">${esc(a.name)}</div></div>
            <div style="font-size:11.5px;color:var(--ink-2)">автономия ${esc(a.autonomy_max || "?")} · v${esc(String(a.version || 1))}</div>
            <button class="run" data-id="${esc(a.id)}" style="${BTN};background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border:none">▶ Запустить</button>
          </div>`).join("") : `<div class="faint" style="padding:20px">Нет доступных агентов (проверьте доступ/роль).</div>`}
        </div>
        <div id="result"></div>
      </div>`;

    root.querySelector("#refresh").onclick = async () => { await loadAgents(); await loadHitl(); render(); };
    root.querySelectorAll(".run").forEach((b) => (b.onclick = () => runAgent(b.dataset.id, b)));
    root.querySelectorAll(".ap").forEach((b) => (b.onclick = () => decide(b.dataset.id, "approve")));
    root.querySelectorAll(".rj").forEach((b) => (b.onclick = () => decide(b.dataset.id, "reject")));
  }

  async function runAgent(id, btn) {
    const res = root.querySelector("#result");
    btn.textContent = "⏳ Прогон…"; btn.disabled = true;
    try {
      const r = await api(A + "/run", { method: "POST", body: JSON.stringify({ agent_id: id }) });
      res.innerHTML = renderRun((r && r.run) || {});
    } catch (e) {
      res.innerHTML = `<div style="${CARD};border-color:rgba(239,68,68,.4)"><div style="color:#fca5a5">Ошибка прогона: ${esc(String((e && e.message) || e))}</div></div>`;
    } finally { btn.textContent = "▶ Запустить"; btn.disabled = false; await loadHitl(); render(); }
  }

  function renderRun(run) {
    const fs = run.findings_summary, isum = run.investigations_summary;
    const rm = run.run_metrics || {}, cost = rm.cost || {}, tm = rm.timings || {};
    const board = (run.board || []).filter((b) => b.kind === "finding").slice(0, 6);
    const dl = run.delivery || [];
    return `<div style="${CARD}" id="runres">
      <div style="${LBL}">РЕЗУЛЬТАТ ПРОГОНА · trace ${esc((run.trace_id || "").slice(0, 8))}${run.cached ? " · из кэша" : ""}</div>
      <div style="font-size:12.5px;color:var(--ink-2)">
        ${fs ? `находок: <b>${esc(String(fs.total))}</b> ` : ""}${isum ? `расследований: <b>${esc(String(isum.total))}</b> ` : ""}
        ${tm.total_ms ? `· ${Math.round(tm.total_ms)} мс ` : ""}${cost.rub != null ? `· ${esc(String(cost.rub))} ₽` : ""}
      </div>
      ${board.map((b) => `<div style="font-size:12px;color:var(--ink);border-left:2px solid var(--line);padding-left:10px">${esc((b.text || "").slice(0, 400))}</div>`).join("")}
      ${dl.length ? `<div style="${LBL}">ДОСТАВКА</div>${dl.map((d) => `<div style="font-size:11.5px;color:${d.mode === "awaiting_hitl" ? "#fbbf24" : "var(--ink-2)"}">${esc(d.channel)} → ${esc(d.to || "")} · ${esc(d.mode)}</div>`).join("")}` : ""}
    </div>`;
  }

  async function decide(id, decision) {
    try { await api(A + "/hitl/" + encodeURIComponent(id) + "/approve", { method: "POST", body: JSON.stringify({ decision }) }); } catch (e) {}
    await loadHitl(); render();
  }

  await loadAgents(); await loadHitl(); render();
}
