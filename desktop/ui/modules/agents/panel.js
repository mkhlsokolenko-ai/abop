// Модуль «Агенты» — вёрстка 1:1 из макета: каталог + полноэкранный «Конструктор агента»
// (имя/скиллы/шаги ↑↓✕/цепочка ролей/DoD/анти + sticky-превью). Логика — сайдкар CRUD/run.
const A = "/api/modules/agents";
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const LBL = "font-family:var(--mono);font-size:9.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3)";
const CARD = "padding:20px;border-radius:16px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:14px";
const FIELD = "padding:11px 13px;border-radius:11px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px;line-height:1.55";

export async function mount(root, ctx) {
  const { api, mascot } = ctx;
  let agents = [], skills = [], roles = [], chainSel = new Set();
  try { skills = await api(A + "/skills"); } catch {}
  try { roles = await api(A + "/agent-roles"); } catch {}

  async function load() { agents = await api(A + "/catalog"); renderCatalog(); }

  // ── КАТАЛОГ ──
  function renderCatalog() {
    root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
      <div style="max-width:1060px;margin:0 auto;display:flex;flex-direction:column;gap:20px;animation:ape-in .35s ease-out">
        <div style="display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap">
          <div style="flex:1 1 340px;display:flex;flex-direction:column;gap:6px">
            <h1 style="margin:0;font-size:26px;font-weight:800;letter-spacing:-.7px">Каталог агентов</h1>
            <p style="margin:0;font-size:13px;color:var(--ink-2)">Готовые агенты и цепочки. Соберите своего или запустите готового в тред.</p>
          </div>
          <div style="display:flex;gap:8px">
            <button id="runChain" disabled style="padding:10px 16px;border:1px solid var(--line-2);border-radius:10px;background:var(--panel);color:var(--ink);font-size:12.5px;font-weight:600">▶ Запустить цепочку</button>
            <button id="newAgent" style="padding:10px 18px;border:none;border-radius:10px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-size:12.5px;font-weight:600;cursor:pointer">＋ Создать агента</button>
          </div>
        </div>
        <div id="grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px"></div>
      </div></div>`;
    const $ = (id) => root.querySelector("#" + id);
    $("grid").innerHTML = agents.map((a) => `
      <div style="${CARD};padding:16px;gap:8px">
        <div style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" class="pick" data-id="${a.id}" ${chainSel.has(a.id) ? "checked" : ""} title="в цепочку"/>
          <span style="font-weight:600;font-size:14px;flex:1">${esc(a.name)}${a.outward ? ` <span class="chip" style="color:var(--warn-ink)">🛡</span>` : ""}</span>
          <button class="ed" data-id="${a.id}" title="Редактировать" style="width:28px;height:28px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--ink-2);font-size:11px;cursor:pointer">✎</button>
          <button class="del" data-id="${a.id}" title="Удалить" style="width:28px;height:28px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--ink-2);font-size:11px;cursor:pointer">🗑</button>
        </div>
        <div style="font-size:12.5px;color:var(--ink-3);min-height:32px">${esc(a.description)}</div>
        <div style="display:flex;gap:4px;flex-wrap:wrap">${(a.skills || []).map((s) => `<span class="chip">${esc(s)}</span>`).join("")}</div>
        <button class="run" data-id="${a.id}" style="margin-top:auto;padding:9px;border:none;border-radius:10px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-size:12.5px;font-weight:600;cursor:pointer">▶ Запустить</button>
      </div>`).join("") || `<div style="color:var(--ink-3)">Пока пусто. «＋ Создать агента».</div>`;
    $("newAgent").onclick = () => openBuilder(null);
    $("grid").querySelectorAll(".ed").forEach((e) => e.onclick = () => openBuilder(agents.find((a) => a.id == e.dataset.id)));
    $("grid").querySelectorAll(".del").forEach((e) => e.onclick = async () => { if (confirm("Удалить агента?")) { await api(A + "/catalog/" + e.dataset.id, { method: "DELETE" }); load(); } });
    $("grid").querySelectorAll(".run").forEach((e) => e.onclick = () => runAgents([+e.dataset.id]));
    $("grid").querySelectorAll(".pick").forEach((c) => c.onclick = () => { const id = +c.dataset.id; c.checked ? chainSel.add(id) : chainSel.delete(id); $("runChain").disabled = !chainSel.size; $("runChain").textContent = "▶ Запустить цепочку" + (chainSel.size ? " (" + chainSel.size + ")" : ""); });
    $("runChain").onclick = () => runAgents([...chainSel]);
  }

  // ── КОНСТРУКТОР (полный экран, 1:1 из макета) ──
  function openBuilder(a) {
    const m = a || { name: "Новый агент", description: "", skills: [], steps: "", dod: "", antipatterns: "", profile: "standard", outward: 0 };
    let steps = (m.steps || "").split("\n").map((s) => s.replace(/^\s*\d+[.)]\s*/, "").trim()).filter(Boolean); if (!steps.length) steps = [""];
    let sel = new Set(m.skills || []);
    let chain = [];
    let outward = !!m.outward;

    root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
      <div style="max-width:1060px;margin:0 auto;display:flex;flex-direction:column;gap:20px;animation:ape-in .35s ease-out">
        <div style="display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap">
          <div style="flex:1 1 340px;display:flex;flex-direction:column;gap:6px">
            <h1 style="margin:0;font-size:26px;font-weight:800;letter-spacing:-.7px">Конструктор агента</h1>
            <p style="margin:0;font-size:13px;color:var(--ink-2)">Опишите роль, шаги и границы. Цепочка собирается из ролей — как конвейер.</p>
          </div>
          <div style="display:flex;gap:8px">
            <button id="bBack" style="padding:10px 16px;border:1px solid var(--line-2);border-radius:10px;background:var(--panel);color:var(--ink);font-size:12.5px;font-weight:600;cursor:pointer">← Каталог</button>
            <button id="bSave" style="padding:10px 18px;border:none;border-radius:10px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-size:12.5px;font-weight:600;cursor:pointer">Сохранить агента</button>
          </div>
        </div>
        <div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap">
          <div style="flex:1 1 520px;min-width:340px;display:flex;flex-direction:column;gap:16px">
            <div style="${CARD}">
              <label style="display:flex;flex-direction:column;gap:7px"><span style="${LBL}">имя агента</span>
                <input id="bName" value="${esc(m.name)}" style="${FIELD};font-size:14px;font-weight:600"/></label>
              <label style="display:flex;flex-direction:column;gap:7px"><span style="${LBL}">описание</span>
                <input id="bDesc" value="${esc(m.description)}" style="${FIELD}"/></label>
              <div style="display:flex;flex-direction:column;gap:8px"><span style="${LBL}">скиллы</span>
                <div id="bSkills" style="display:flex;gap:8px;flex-wrap:wrap"></div></div>
            </div>
            <div style="${CARD};gap:12px"><span style="${LBL}">шаги работы</span>
              <div id="bSteps" style="display:flex;flex-direction:column;gap:10px"></div>
              <button id="bAddStep" style="align-self:flex-start;padding:8px 13px;border:1px dashed rgba(129,140,248,.5);border-radius:9px;background:rgba(99,102,241,.1);color:var(--accent-ink-2);font-size:12px;font-weight:600;cursor:pointer">＋ шаг</button>
            </div>
            <div style="${CARD};gap:12px"><span style="${LBL}">цепочка ролей</span>
              <div id="bChain" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"></div>
              <div style="display:flex;gap:8px;flex-wrap:wrap;padding-top:10px;border-top:1px solid var(--line)">
                <span style="font-size:11.5px;color:var(--ink-3);align-self:center">добавить роль:</span>
                <div id="bPool" style="display:flex;gap:8px;flex-wrap:wrap"></div></div>
            </div>
            <div style="display:flex;gap:16px;flex-wrap:wrap">
              <label style="flex:1 1 250px;display:flex;flex-direction:column;gap:7px"><span style="${LBL}">definition of done</span>
                <textarea id="bDod" rows="3" style="${FIELD}">${esc(m.dod)}</textarea></label>
              <label style="flex:1 1 250px;display:flex;flex-direction:column;gap:7px"><span style="${LBL}">анти-паттерны</span>
                <textarea id="bAnti" rows="3" style="${FIELD}">${esc(m.antipatterns)}</textarea></label>
            </div>
            <label style="display:flex;gap:8px;align-items:center;font-size:12.5px"><input type="checkbox" id="bOut" ${outward ? "checked" : ""}/> 🛡 Действует наружу (запуск через подтверждение)</label>
          </div>
          <div style="flex:0 1 320px;min-width:280px;position:sticky;top:0;padding:20px;border-radius:16px;background:var(--panel-2);border:1px solid var(--line);display:flex;flex-direction:column;gap:14px">
            <div style="display:flex;align-items:center;gap:10px">${mascot("idle", 30)}<span id="pvName" style="flex:1;font-size:14px;font-weight:700">${esc(m.name)}</span></div>
            <span id="pvSum" style="font-size:12px;line-height:1.5;color:var(--ink-2)"></span>
            <div id="pvRows" style="display:flex;flex-direction:column;gap:7px;padding-top:12px;border-top:1px solid var(--line)"></div>
            <button id="bRun" style="margin-top:auto;padding:11px;border:1px solid rgba(129,140,248,.4);border-radius:11px;background:rgba(99,102,241,.16);color:var(--accent-ink);font-size:12.5px;font-weight:600;cursor:pointer">Запустить в треде</button>
          </div>
        </div>
      </div></div>`;
    const $ = (id) => root.querySelector("#" + id);

    const paintSkills = () => { $("bSkills").innerHTML = skills.map((s) => { const on = sel.has(s.id); return `<button data-s="${s.id}" style="padding:8px 13px;border:1px solid ${on ? "var(--accent)" : "var(--line)"};border-radius:9999px;background:${on ? "var(--accent-bg)" : "var(--panel)"};color:${on ? "var(--accent-ink)" : "var(--ink-2)"};font-size:12px;font-weight:600;cursor:pointer">${s.id}</button>`; }).join(""); $("bSkills").querySelectorAll("[data-s]").forEach((b) => b.onclick = () => { sel.has(b.dataset.s) ? sel.delete(b.dataset.s) : sel.add(b.dataset.s); paintSkills(); paintPreview(); }); };
    const paintSteps = () => {
      $("bSteps").innerHTML = steps.map((v, i) => `<div style="display:flex;align-items:center;gap:9px">
        <span style="width:24px;height:24px;flex:none;border-radius:8px;background:rgba(99,102,241,.2);color:var(--accent-ink-2);font-family:var(--mono);font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center">${i + 1}</span>
        <input class="stp" data-i="${i}" value="${esc(v)}" style="${FIELD};flex:1;min-width:0" placeholder="что сделать на шаге"/>
        <button class="sup" data-i="${i}" title="Выше" style="width:28px;height:28px;flex:none;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--ink-2);font-size:11px;cursor:pointer">↑</button>
        <button class="sdn" data-i="${i}" title="Ниже" style="width:28px;height:28px;flex:none;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--ink-2);font-size:11px;cursor:pointer">↓</button>
        <button class="srm" data-i="${i}" title="Удалить" style="width:28px;height:28px;flex:none;border:1px solid rgba(239,68,68,.28);border-radius:8px;background:rgba(239,68,68,.1);color:var(--danger-ink);font-size:11px;cursor:pointer">✕</button></div>`).join("");
      $("bSteps").querySelectorAll(".stp").forEach((x) => x.oninput = () => { steps[+x.dataset.i] = x.value; paintPreview(); });
      const sw = (i, j) => { if (j < 0 || j >= steps.length) return; [steps[i], steps[j]] = [steps[j], steps[i]]; paintSteps(); };
      $("bSteps").querySelectorAll(".sup").forEach((x) => x.onclick = () => sw(+x.dataset.i, +x.dataset.i - 1));
      $("bSteps").querySelectorAll(".sdn").forEach((x) => x.onclick = () => sw(+x.dataset.i, +x.dataset.i + 1));
      $("bSteps").querySelectorAll(".srm").forEach((x) => x.onclick = () => { steps.splice(+x.dataset.i, 1); if (!steps.length) steps = [""]; paintSteps(); paintPreview(); });
    };
    const paintChain = () => {
      $("bChain").innerHTML = chain.map((r, i) => `<span style="display:inline-flex;align-items:center;gap:8px">
        <span style="display:inline-flex;align-items:center;gap:8px;padding:10px 13px;border-radius:12px;background:rgba(99,102,241,.14);border:1px solid rgba(129,140,248,.32)">
          <span style="font-size:12.5px;font-weight:600;color:var(--accent-ink)">${esc(roleName(r))}</span>
          <button class="cl" data-i="${i}" title="Раньше" style="width:20px;height:20px;border:none;border-radius:6px;background:var(--hover);color:var(--ink-2);font-size:10px;cursor:pointer">←</button>
          <button class="cr" data-i="${i}" title="Позже" style="width:20px;height:20px;border:none;border-radius:6px;background:var(--hover);color:var(--ink-2);font-size:10px;cursor:pointer">→</button>
          <button class="cx" data-i="${i}" title="Убрать" style="width:20px;height:20px;border:none;border-radius:6px;background:rgba(239,68,68,.14);color:var(--danger-ink);font-size:10px;cursor:pointer">✕</button></span>
        ${i < chain.length - 1 ? `<span style="color:var(--ink-3);font-size:14px">→</span>` : ""}</span>`).join("") || `<span style="font-size:12px;color:var(--ink-3)">Пусто — добавьте роли ниже (иначе агент работает соло).</span>`;
      const sw = (i, j) => { if (j < 0 || j >= chain.length) return; [chain[i], chain[j]] = [chain[j], chain[i]]; paintChain(); };
      $("bChain").querySelectorAll(".cl").forEach((x) => x.onclick = () => sw(+x.dataset.i, +x.dataset.i - 1));
      $("bChain").querySelectorAll(".cr").forEach((x) => x.onclick = () => sw(+x.dataset.i, +x.dataset.i + 1));
      $("bChain").querySelectorAll(".cx").forEach((x) => x.onclick = () => { chain.splice(+x.dataset.i, 1); paintChain(); });
      $("bPool").innerHTML = roles.map((r) => `<button data-r="${r.id}" style="padding:7px 12px;border:1px dashed var(--line-2);border-radius:9999px;background:transparent;color:var(--ink-2);font-size:11.5px;cursor:pointer">＋ ${esc(r.name)}</button>`).join("");
      $("bPool").querySelectorAll("[data-r]").forEach((b) => b.onclick = () => { chain.push(b.dataset.r); paintChain(); });
    };
    const roleName = (id) => (roles.find((r) => r.id === id) || {}).name || id;
    const readData = () => ({ name: $("bName").value.trim(), description: $("bDesc").value.trim(), skills: [...sel], steps: steps.filter(Boolean).join("\n"), dod: $("bDod").value.trim(), antipatterns: $("bAnti").value.trim(), profile: m.profile || "standard", outward: $("bOut").checked ? 1 : 0 });
    const paintPreview = () => {
      $("pvName").textContent = $("bName").value || "Агент";
      const sc = steps.filter(Boolean).length;
      $("pvSum").textContent = `Агент с ${sel.size} скилл(ами) и ${sc} шаг(ами)` + (chain.length ? `, в цепочке ${chain.length} рол(ей).` : ".");
      $("pvRows").innerHTML = [["скиллы", [...sel].join(", ") || "—"], ["шагов", sc], ["цепочка", chain.map(roleName).join(" → ") || "соло"], ["наружу", $("bOut").checked ? "да (гейт)" : "нет"]]
        .map((r) => `<span style="display:flex;gap:10px;font-size:11.5px;line-height:1.45"><span style="width:74px;flex:none;${LBL};font-size:9.5px">${r[0]}</span><span style="flex:1;color:var(--ink-2)">${esc(String(r[1]))}</span></span>`).join("");
    };

    paintSkills(); paintSteps(); paintChain(); paintPreview();
    $("bName").oninput = paintPreview; $("bOut").onchange = paintPreview;
    $("bAddStep").onclick = () => { steps.push(""); paintSteps(); };
    $("bBack").onclick = () => renderCatalog();
    $("bSave").onclick = async () => {
      const d = readData(); if (!d.name) { alert("Укажи имя"); return; }
      if (a) await api(A + "/catalog/" + a.id, { method: "PATCH", body: JSON.stringify(d) });
      else await api(A + "/catalog", { method: "POST", body: JSON.stringify(d) });
      await load();
    };
    $("bRun").onclick = async () => {
      const d = readData();
      if (a) { await api(A + "/catalog/" + a.id, { method: "PATCH", body: JSON.stringify(d) }); runAgents([a.id]); }
      else { const r = await api(A + "/catalog", { method: "POST", body: JSON.stringify(d) }); await load(); runAgents([r.id]); }
    };
  }

  // ── запуск (с governance-гейтом для outward) ──
  async function runAgents(ids) {
    if (!ids.length) return;
    const outward = agents.filter((a) => ids.includes(a.id) && a.outward);
    if (outward.length && ctx.gate) {
      const ok = await ctx.gate({ title: "Запуск агента с действием наружу", fields: [["Агенты", outward.map((a) => a.name).join(", ")]], body: "Помечены как действующие наружу. Запустить?", allowLabel: "Разрешить запуск" });
      if (!ok) return;
    }
    const task = prompt("Задача для агента(ов):"); if (!task) return;
    const ov = document.createElement("div");
    ov.style = "position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:60;backdrop-filter:blur(2px)";
    ov.innerHTML = `<div style="width:min(640px,94vw);max-height:86vh;overflow:auto;padding:20px;border-radius:16px;background:var(--panel);backdrop-filter:blur(20px);border:1px solid var(--line);box-shadow:0 24px 70px rgba(0,0,0,.45)"><div style="font-weight:700;margin-bottom:12px">Запуск</div><div id="res" style="font-size:13px;color:var(--ink-3)">▍ агенты работают…</div><div style="text-align:right;margin-top:14px"><button class="btn" id="cl">Закрыть</button></div></div>`;
    document.body.appendChild(ov); ov.querySelector("#cl").onclick = () => ov.remove();
    const r = await api(A + "/run", { method: "POST", body: JSON.stringify({ agent_ids: ids, task }) });
    ov.querySelector("#res").innerHTML = r.ok ? r.steps.map((s) => `<div style="${CARD};padding:12px;gap:6px;margin-bottom:8px"><b style="font-size:13px">🤖 ${esc(s.name)}</b><div style="white-space:pre-wrap;font-size:13px;color:var(--ink-2)">${esc(s.text)}</div></div>`).join("") : `<div style="color:var(--danger-ink)">${r.error === "auth_required" ? "Нужен вход через GitHub" : "Ошибка: " + esc(r.error)}</div>`;
  }

  await load();
}
