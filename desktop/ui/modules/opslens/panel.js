// Модуль «Операции» (OpsLens) — интерактивная карта процесса: зум (±/reset), слои LIVE/AS-IS/TO-BE,
// участки (из сохранённых графов + базовый процесс), клик по участку → инспектор. Вёрстка по ДС.
const G = "/api/modules/graphlens";
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const LBL = "font-family:var(--mono);font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--ink-3)";

const BASE_ZONES = [
  { id: "b1", title: "Приём заявок", agents: 2, state: "ok" },
  { id: "b2", title: "Квалификация", agents: 1, state: "ok" },
  { id: "b3", title: "Подготовка КП", agents: 1, state: "warn" },
  { id: "b4", title: "Согласование", agents: 0, state: "idle" },
  { id: "b5", title: "Отправка", agents: 1, state: "warn" },
  { id: "b6", title: "Аналитика", agents: 1, state: "ok" },
];
const DOT = { ok: "var(--ok-ink)", warn: "var(--warn-ink)", idle: "var(--ink-3)" };

export async function mount(root, ctx) {
  const { api } = ctx;
  let graphs = []; try { graphs = await api(G + "/graphs"); } catch {}
  let zoom = 1, layer = "LIVE", selZone = null;

  // участки: базовый процесс + узлы-агенты из сохранённых графов
  const zones = [...BASE_ZONES];
  graphs.forEach((g) => { const ag = (g.nodes || []).filter((n) => n.kind === "agent").length; zones.push({ id: "g" + g.id, title: g.name, agents: ag, state: ag ? "ok" : "idle", graph: g.id }); });

  root.innerHTML = `<div style="flex:1;min-width:0;display:flex;flex-direction:column">
    <div style="display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap">
      <div style="display:flex;flex-direction:column;gap:4px">
        <span style="${LBL}">карта процесса</span>
        <h1 style="margin:0;font-size:22px;font-weight:800;letter-spacing:-.6px">Операции</h1>
      </div>
      <div style="display:flex;gap:4px;padding:4px;border-radius:10px;background:var(--hover);border:1px solid var(--line)">
        ${["LIVE", "AS-IS", "TO-BE"].map((l) => `<button class="lay" data-l="${l}" style="padding:7px 13px;border:none;border-radius:8px;font-family:var(--mono);font-size:11px;font-weight:600;cursor:pointer">${l}</button>`).join("")}
      </div>
      <div style="margin-left:auto;display:flex;align-items:center;gap:4px;padding:4px;border-radius:10px;border:1px solid var(--line)">
        <button id="zOut" style="width:26px;height:26px;border:none;border-radius:7px;background:transparent;color:var(--accent-ink);font-size:15px;cursor:pointer">−</button>
        <button id="zRst" style="min-width:42px;height:26px;border:none;border-radius:7px;background:transparent;color:var(--ink-2);font-family:var(--mono);font-size:10.5px;font-weight:600;cursor:pointer">100%</button>
        <button id="zIn" style="width:26px;height:26px;border:none;border-radius:7px;background:transparent;color:var(--accent-ink);font-size:15px;cursor:pointer">＋</button>
      </div>
    </div>
    <div id="hint" style="padding:10px 20px;font-size:12.5px;color:var(--ink-2);border-bottom:1px solid var(--line)"></div>
    <div style="flex:1;display:flex;min-height:0">
      <div id="mapWrap" style="flex:1;overflow:auto;padding:24px">
        <div id="map" style="transform-origin:0 0;display:flex;align-items:stretch;gap:0;flex-wrap:wrap;transition:transform .15s"></div>
      </div>
      <div style="width:280px;flex:none;border-left:1px solid var(--line);padding:16px;overflow:auto">
        <span style="${LBL}">инспектор участка</span>
        <div id="insp" style="margin-top:10px"></div>
      </div>
    </div></div>`;
  const $ = (id) => root.querySelector("#" + id);

  function paintLayers() { root.querySelectorAll(".lay").forEach((b) => { const on = b.dataset.l === layer; b.style.background = on ? "var(--panel)" : "transparent"; b.style.color = on ? "var(--ink)" : "var(--ink-2)"; }); $("hint").textContent = layer === "LIVE" ? "Слой LIVE — только наблюдение. Чтобы менять схему и сажать агентов, переключитесь на AS-IS/TO-BE (вкладка Граф)." : `Слой ${layer} — редактирование схемы процесса (открывается в Графе).`; }
  function paintMap() {
    $("map").style.transform = `scale(${zoom})`;
    $("map").innerHTML = zones.map((z, i) => `
      <div class="zone" data-id="${z.id}" style="cursor:pointer;padding:14px 16px;border-radius:16px;background:var(--panel);border:1px solid ${selZone === z.id ? "#818cf8" : "var(--line)"};backdrop-filter:blur(16px);min-width:170px;flex:0 0 auto;display:flex;flex-direction:column;gap:6px;margin:0 8px 8px 0">
        <div style="display:flex;align-items:center;gap:8px"><span style="width:8px;height:8px;border-radius:9999px;background:${DOT[z.state]};flex:none"></span><b style="font-size:13.5px">${esc(z.title)}</b></div>
        <div style="font-size:12px;color:var(--ink-3)">${z.agents ? z.agents + " агент(ов)" : "нет агентов"}${z.graph ? " · граф" : ""}</div>
      </div>${i < zones.length - 1 ? `<div style="align-self:center;color:var(--ink-3);padding:0 4px;flex:none">→</div>` : ""}`).join("");
    $("map").querySelectorAll(".zone").forEach((el) => el.onclick = () => { selZone = el.dataset.id; paintMap(); paintInsp(); });
  }
  function paintInsp() {
    const z = zones.find((x) => x.id === selZone);
    if (!z) { $("insp").innerHTML = `<div style="padding:20px 4px;text-align:center;font-size:12.5px;color:var(--ink-3)">Выберите участок на карте.</div>`; return; }
    $("insp").innerHTML = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px"><span style="width:8px;height:8px;border-radius:9999px;background:${DOT[z.state]}"></span><b style="font-size:14px">${esc(z.title)}</b></div>
      ${[["агентов", z.agents], ["статус", z.state === "ok" ? "работает" : z.state === "warn" ? "внимание" : "нет активности"], ["автономия", z.state === "warn" ? "с подтверждением" : "по контракту"], ["слой", layer]].map((r) => `<div style="display:flex;gap:10px;font-size:12px;margin-bottom:6px"><span style="${LBL};width:80px">${r[0]}</span><span style="color:var(--ink-2)">${esc(String(r[1]))}</span></div>`).join("")}
      ${z.graph ? `<button id="openG" style="margin-top:12px;width:100%;padding:9px;border:1px solid rgba(129,140,248,.4);border-radius:10px;background:rgba(99,102,241,.16);color:var(--accent-ink);font-size:12px;font-weight:600;cursor:pointer">Открыть граф участка ▸</button>` : `<div style="margin-top:10px;font-size:11.5px;color:var(--ink-3)">Базовый участок процесса. Схему меняют в слоях AS-IS/TO-BE (Граф).</div>`}`;
    if (z.graph && $("openG")) $("openG").onclick = () => { const nav = root.closest("body")?.querySelector(`[data-id="graphlens"]`) || document.querySelector('#railNav [data-id="graphlens"]'); if (nav) nav.click(); };
  }

  root.querySelectorAll(".lay").forEach((b) => b.onclick = () => { layer = b.dataset.l; paintLayers(); });
  $("zIn").onclick = () => { zoom = Math.min(1.8, zoom + 0.15); $("zRst").textContent = Math.round(zoom * 100) + "%"; paintMap(); };
  $("zOut").onclick = () => { zoom = Math.max(0.5, zoom - 0.15); $("zRst").textContent = Math.round(zoom * 100) + "%"; paintMap(); };
  $("zRst").onclick = () => { zoom = 1; $("zRst").textContent = "100%"; paintMap(); };

  paintLayers(); paintMap(); paintInsp();
}
