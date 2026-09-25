// Модуль «Граф» (GraphLens) — интерактивный редактор: палитра→холст→связи→инспектор→проверка→запуск.
// Вёрстка в рецептах ДС. Узлы двигаются мышью, связи тянутся от правого порта к левому.
const G = "/api/modules/graphlens";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const LBL = "font-family:var(--mono);font-size:11px;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3)";
const NW = 150, NH = 52;

export async function mount(root, ctx) {
  const { api, toast, humanError, confirm: confirmDialog } = ctx;
  let palette = [], graphId = null, name = "Новый граф", nodes = [], edges = [], sel = null, nextId = 1, dirty = false;
  let linkFrom = null; // id узла, из которого тянем связь
  try { palette = await api(G + "/palette"); } catch {}
  let agentsCat = []; try { agentsCat = await api("/api/modules/agents/catalog"); } catch {}
  let saved = []; try { saved = await api(G + "/graphs"); } catch {}

  root.innerHTML = `<div style="flex:1;min-width:0;display:flex;flex-direction:column;animation:ape-in .3s ease-out">
    <div style="display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap">
      <div style="display:flex;flex-direction:column;gap:4px">
        <span style="${LBL}">граф агента</span>
        <input id="gName" style="width:220px;padding:6px 10px;border-radius:9px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:14px;font-weight:700"/>
      </div>
      <select id="gPick" title="Сохранённые графы" style="padding:8px 10px;border-radius:9px;border:1px solid var(--line);background:var(--field);color:var(--ink-2);font-size:12px"></select>
      <span id="gDirty" class="chip"></span>
      <div style="margin-left:auto;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <input id="gTask" placeholder="Задача для графа…" style="width:210px;padding:9px 12px;border-radius:10px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px"/>
        <button id="gNew" class="btn sm">＋ Новый</button>
        <button id="gDel" class="btn sm danger" style="display:none" title="Удалить сохранённый граф">Удалить</button>
        <button id="gCheck" style="padding:9px 16px;border:1px solid rgba(52,211,153,.4);border-radius:10px;background:rgba(16,185,129,.14);color:var(--ok-ink);font-size:12.5px;font-weight:600;cursor:pointer">Проверить</button>
        <button id="gSave" class="btn sm">Сохранить</button>
        <button id="gRun" style="padding:9px 16px;border:none;border-radius:10px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-size:12.5px;font-weight:600;cursor:pointer;box-shadow:var(--shadow-accent)">Запустить ▸</button>
      </div>
    </div>
    <div style="flex:1;display:flex;min-height:0">
      <div style="width:172px;flex:none;border-right:1px solid var(--line);padding:14px;display:flex;flex-direction:column;gap:8px;overflow:auto">
        <span style="${LBL}">палитра · клик добавит блок</span>
        <div id="gPal" style="display:flex;flex-direction:column;gap:8px"></div>
        <div style="font-size:11.5px;line-height:1.5;color:var(--ink-3);margin-top:6px">Связь: клик на правый порт «→», затем на левый «▸» другого блока. Клик по связи — удалить её.</div>
      </div>
      <div id="gCanvas" style="flex:1;position:relative;overflow:hidden;background:radial-gradient(circle at center, rgba(255,255,255,.05) 1px, transparent 1px) 0 0/22px 22px;cursor:default">
        <svg id="gWires" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible"></svg>
      </div>
      <div style="width:264px;flex:none;border-left:1px solid var(--line);padding:14px;overflow:auto">
        <span style="${LBL}">инспектор узла</span>
        <div id="gInsp" style="margin-top:10px"></div>
        <span style="${LBL};display:block;margin-top:18px">проверка исполнимости</span>
        <div id="gChk" style="margin-top:8px;font-size:12px;color:var(--ink-3)">Нажмите «Проверить».</div>
      </div>
    </div></div>`;
  const $ = (id) => root.querySelector("#" + id);

  function setDirty(v) { dirty = v; $("gDirty").textContent = v ? "● не сохранено" : (graphId ? "сохранён" : "черновик"); $("gDirty").style.color = v ? "var(--warn-ink)" : "var(--ink-3)"; }
  function paintPalette() {
    $("gPal").innerHTML = palette.map((p) => `<button class="pal" data-k="${p.kind}" style="display:flex;align-items:center;gap:8px;padding:10px 12px;border:1px solid var(--line);border-radius:11px;background:var(--panel);color:var(--ink);font-size:12.5px;font-weight:600;text-align:left;cursor:pointer"><span>${p.glyph}</span>${esc(p.label)}</button>`).join("");
    $("gPal").querySelectorAll(".pal").forEach((b) => b.onclick = () => addNode(b.dataset.k));
  }
  function paintPick() {
    $("gPick").innerHTML = `<option value="">— графы (${saved.length}) —</option>` + saved.map((g) => `<option value="${g.id}" ${g.id === graphId ? "selected" : ""}>${esc(g.name)}</option>`).join("");
    $("gPick").onchange = (e) => { const g = saved.find((x) => x.id == e.target.value); if (g) loadGraph(g); };
  }
  function addNode(kind) {
    const p = palette.find((x) => x.kind === kind) || { label: kind, glyph: "▦" };
    nodes.push({ id: nextId++, kind, label: p.label, glyph: p.glyph, x: 60 + (nodes.length % 4) * 40, y: 60 + nodes.length * 30, agent_id: null });
    setDirty(true); paint();
  }
  function nodeEl(n) {
    const acc = n.kind === "agent" ? "rgba(129,140,248,.4)" : "var(--line)";
    return `<div class="gn" data-id="${n.id}" style="position:absolute;left:${n.x}px;top:${n.y}px;width:${NW}px;height:${NH}px;border:1px solid ${sel === n.id ? "#818cf8" : acc};border-radius:12px;background:var(--panel);backdrop-filter:blur(16px);display:flex;align-items:center;gap:8px;padding:0 12px;cursor:grab;user-select:none;box-shadow:0 8px 24px rgba(0,0,0,.25)">
      <span class="gport gl" data-id="${n.id}" title="вход ▸" style="position:absolute;left:-7px;top:50%;transform:translateY(-50%);width:14px;height:14px;border-radius:9999px;background:var(--rail);border:1px solid var(--line-2);cursor:crosshair"></span>
      <span style="font-size:15px;color:${edgeColor(n.kind)}">${n.glyph || "▦"}</span>
      <span style="flex:1;min-width:0;font-size:12.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(n.label)}</span>
      <span class="gport gr" data-id="${n.id}" title="выход → (клик, затем вход другого)" style="position:absolute;right:-7px;top:50%;transform:translateY(-50%);width:14px;height:14px;border-radius:9999px;background:${linkFrom === n.id ? "#6366f1" : "var(--rail)"};border:1px solid var(--line-2);cursor:crosshair"></span>
    </div>`;
  }
  // Семантика рёбер (перенос из webapp): цвет + пунктир-поток по КИНДУ узла-источника —
  // видно, что течёт по графу (триггер→, данные→, навык→, агент→, решение→, выход→).
  const KIND_COL = { trigger: "#f59e0b", source: "#22d3ee", data: "#22d3ee", skill: "#818cf8", agent: "#a855f7", decision: "#fbbf24", output: "#34d399" };
  function edgeColor(kind) { return KIND_COL[kind] || "var(--accent-2)"; }
  function paintWires() {
    const svg = $("gWires");
    const center = (id, side) => { const n = nodes.find((x) => x.id === id); if (!n) return null; return { x: n.x + (side === "r" ? NW : 0), y: n.y + NH / 2 }; };
    svg.innerHTML = edges.map(([a, b]) => {
      const p = center(a, "r"), q = center(b, "l"); if (!p || !q) return "";
      const src = nodes.find((n) => n.id === a) || {}; const col = edgeColor(src.kind);
      const mx = (p.x + q.x) / 2;
      const d = `M${p.x} ${p.y} C ${mx} ${p.y} ${mx} ${q.y} ${q.x} ${q.y}`;
      return `<path d="${d}" stroke="transparent" stroke-width="14" fill="none" class="gedge" data-a="${a}" data-b="${b}" style="pointer-events:stroke;cursor:pointer"><title>Удалить связь</title></path><path d="${d}" stroke="${col}" stroke-width="2" fill="none" opacity=".8" stroke-dasharray="7 6" style="animation:gwire-flow 1.1s linear infinite;pointer-events:none"/>`;
    }).join("");
    svg.querySelectorAll(".gedge").forEach((e) => e.onclick = async (ev) => {
      ev.stopPropagation();
      const a = +e.dataset.a, b = +e.dataset.b; const na = nodes.find((n) => n.id === a) || {}, nb = nodes.find((n) => n.id === b) || {};
      if (!(await confirmDialog({ title: "Удалить связь?", text: `${esc(na.label || a)} → ${esc(nb.label || b)}`, okLabel: "Удалить связь" }))) return;
      edges = edges.filter(([x, y]) => !(x === a && y === b)); setDirty(true); paint();
    });
  }
  function paint() {
    const cv = $("gCanvas");
    cv.querySelectorAll(".gn").forEach((e) => e.remove());
    if (!nodes.length) {
      if (!cv.querySelector("#gEmpty")) cv.insertAdjacentHTML("beforeend", `<div id="gEmpty" style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;text-align:center;color:var(--ink-3);pointer-events:none"><div style="font-size:15px;font-weight:600;color:var(--ink-2)">Холст пуст — три шага до результата</div><div style="font-size:13px;max-width:360px">1. Клик по блоку в палитре · 2. Свяжите порты · 3. «Проверить»</div></div>`);
    } else { const e = cv.querySelector("#gEmpty"); if (e) e.remove(); }
    nodes.forEach((n) => cv.insertAdjacentHTML("beforeend", nodeEl(n)));
    paintWires(); wireNodes(); paintInsp();
  }
  function wireNodes() {
    const cv = $("gCanvas");
    cv.querySelectorAll(".gn").forEach((el) => {
      const id = +el.dataset.id;
      el.onmousedown = (e) => {
        if (e.target.classList.contains("gport")) return;
        sel = id; paintInsp(); highlightSel();
        const n = nodes.find((x) => x.id === id); const sx = e.clientX, sy = e.clientY, ox = n.x, oy = n.y;
        const mv = (ev) => { n.x = Math.max(0, ox + ev.clientX - sx); n.y = Math.max(0, oy + ev.clientY - sy); el.style.left = n.x + "px"; el.style.top = n.y + "px"; paintWires(); };
        const up = () => { document.removeEventListener("mousemove", mv); document.removeEventListener("mouseup", up); setDirty(true); };
        document.addEventListener("mousemove", mv); document.addEventListener("mouseup", up);
      };
    });
    cv.querySelectorAll(".gr").forEach((p) => p.onclick = (e) => { e.stopPropagation(); linkFrom = +p.dataset.id; paint(); });
    cv.querySelectorAll(".gl").forEach((p) => p.onclick = (e) => {
      e.stopPropagation(); const to = +p.dataset.id;
      if (linkFrom && linkFrom !== to && !edges.some(([a, b]) => a === linkFrom && b === to)) { edges.push([linkFrom, to]); setDirty(true); }
      linkFrom = null; paint();
    });
  }
  function highlightSel() { $("gCanvas").querySelectorAll(".gn").forEach((el) => { el.style.borderColor = (+el.dataset.id === sel) ? "#818cf8" : (nodes.find((x) => x.id == el.dataset.id).kind === "agent" ? "rgba(129,140,248,.4)" : "var(--line)"); }); }
  function paintInsp() {
    if (!sel) { $("gInsp").innerHTML = `<div style="padding:20px 4px;text-align:center;font-size:12.5px;color:var(--ink-3)">Выберите узел на холсте.</div>`; return; }
    const n = nodes.find((x) => x.id === sel); if (!n) { sel = null; return paintInsp(); }
    const agOpts = n.kind === "agent" ? `<label style="display:flex;flex-direction:column;gap:6px;margin-top:10px"><span style="${LBL}">агент из каталога</span>
      <select id="iAgent" style="padding:8px 10px;border-radius:9px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12px"><option value="">— выбрать —</option>${agentsCat.map((a) => `<option value="${esc(a.id)}" ${String(n.agent_id) === String(a.id) ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select></label>` : "";
    $("gInsp").innerHTML = `<label style="display:flex;flex-direction:column;gap:6px"><span style="${LBL}">название</span>
      <input id="iLabel" value="${esc(n.label)}" style="padding:8px 10px;border-radius:9px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px"/></label>
      <div style="font-size:11.5px;color:var(--ink-3);margin-top:8px">тип: ${esc(n.kind)}</div>${agOpts}
      <button id="iDel" style="margin-top:14px;width:100%;padding:9px;border:1px solid rgba(239,68,68,.3);border-radius:9px;background:rgba(239,68,68,.1);color:var(--danger-ink);font-size:12px;font-weight:600;cursor:pointer">Удалить узел</button>`;
    $("iLabel").oninput = (e) => { n.label = e.target.value; setDirty(true); const el = $("gCanvas").querySelector(`.gn[data-id="${n.id}"] span:nth-child(3)`); if (el) el.textContent = n.label; };
    if ($("iAgent")) $("iAgent").onchange = (e) => { n.agent_id = e.target.value ? String(e.target.value) : null; const a = agentsCat.find((x) => String(x.id) === String(e.target.value)); if (a) n.label = a.name; setDirty(true); paint(); };
    $("iDel").onclick = () => { nodes = nodes.filter((x) => x.id !== sel); edges = edges.filter(([a, b]) => a !== sel && b !== sel); sel = null; setDirty(true); paint(); };
  }
  $("gCanvas").onclick = (e) => { if (e.target.id === "gCanvas" || e.target.id === "gWires") { sel = null; linkFrom = null; paint(); } };

  function syncDel() { $("gDel").style.display = graphId ? "" : "none"; }
  function loadGraph(g) { graphId = g.id; name = g.name; nodes = (g.nodes || []).map((n) => ({ ...n })); edges = (g.edges || []).map((e) => [...e]); nextId = Math.max(0, ...nodes.map((n) => n.id)) + 1; sel = null; linkFrom = null; $("gName").value = name; setDirty(false); paint(); paintPick(); syncDel(); }

  $("gName").value = name;
  $("gName").oninput = (e) => { name = e.target.value; setDirty(true); };
  $("gNew").onclick = () => { graphId = null; name = "Новый граф"; nodes = []; edges = []; nextId = 1; sel = null; $("gName").value = name; setDirty(false); paint(); paintPick(); syncDel(); };
  $("gDel").onclick = async () => {
    if (!graphId) return;
    if (!(await confirmDialog({ title: "Удалить граф?", text: `«${esc(name)}» будет удалён без возможности восстановления.`, okLabel: "Удалить граф" }))) return;
    try { await api(G + "/graphs/" + graphId, { method: "DELETE" }); toast("Граф удалён", "ok"); } catch (e) { toast(humanError(e), "danger"); return; }
    try { saved = await api(G + "/graphs"); } catch { saved = []; }
    $("gNew").onclick();
  };
  $("gSave").onclick = async () => {
    const payload = { name, nodes, edges };
    try {
      if (graphId) await api(G + "/graphs/" + graphId, { method: "PATCH", body: JSON.stringify(payload) });
      else { const r = await api(G + "/graphs", { method: "POST", body: JSON.stringify(payload) }); graphId = r.id; }
      saved = await api(G + "/graphs"); setDirty(false); paintPick(); syncDel(); toast("Граф сохранён", "ok");
    } catch (e) { toast("Не удалось сохранить: " + humanError(e), "danger"); throw e; }
  };
  $("gCheck").onclick = async () => {
    let r; try { r = await api(G + "/check", { method: "POST", body: JSON.stringify({ name, nodes, edges }) }); } catch (e) { $("gChk").innerHTML = `<span class="danger-ink">${esc(humanError(e))}</span>`; return; }
    const warns = (r.warnings || []).map((w) => `<div style="color:var(--ink-3);margin-top:4px;font-size:11.5px">ⓘ ${esc(w)}</div>`).join("");
    $("gChk").innerHTML = (r.ok
      ? `<span class="ok-ink">✓ Исполнимо · агентов к запуску: ${r.runnable ?? "—"} · связей: ${r.edges}</span>`
      : (r.issues || []).map((i) => `<div class="warn-ink" style="margin-bottom:4px">• ${esc(i)}</div>`).join("")) + warns;
  };
  $("gTask").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); $("gRun").onclick(); } };
  let running = false;
  $("gRun").onclick = async () => {
    if (running) return;
    const task = ($("gTask").value || "").trim();
    if (!task) { const t = $("gTask"); t.focus(); t.style.borderColor = "var(--warn-ink)"; setTimeout(() => (t.style.borderColor = "var(--line)"), 1400); toast("Опишите задачу для графа", "warn"); return; }
    try { if (dirty || !graphId) await $("gSave").onclick(); } catch { return; }
    running = true; $("gRun").disabled = true; $("gRun").textContent = "Выполняется…";
    $("gChk").innerHTML = `<span class="faint">▍ граф выполняется…</span>`;
    try {
      const r = await api(G + "/run", { method: "POST", body: JSON.stringify({ id: graphId, task }) });
      if (!r.ok) { $("gChk").innerHTML = `<span class="danger-ink">${esc(humanError(r.message || r.error))}</span>`; }
      else $("gChk").innerHTML = (r.steps || []).map((s) => `<div style="border:1px solid var(--line);border-radius:10px;padding:9px;margin-bottom:6px;background:var(--panel);box-shadow:var(--shadow-1);animation:ape-in .3s ease-out"><b style="font-size:12px">🤖 ${esc(s.name)}</b><div style="font-size:11.5px;color:var(--ink-2);white-space:pre-wrap;margin-top:3px">${esc((s.text || "").slice(0, 400))}</div></div>`).join("") || '<span class="faint">Шагов нет.</span>';
    } catch (e) { $("gChk").innerHTML = `<span class="danger-ink">${esc(humanError(e))}</span>`; }
    running = false; $("gRun").disabled = false; $("gRun").textContent = "Запустить ▸";
  };

  paintPalette(); paintPick(); setDirty(false); paint(); syncDel();
}
