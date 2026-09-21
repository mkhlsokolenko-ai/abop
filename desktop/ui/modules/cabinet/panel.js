// Модуль «Кабинет» — вёрстка 1:1 из макета APE Desktop.dc.html (экран «Кабинет»):
// KPI расхода · гистограмма по неделе · роль и доступ (RBAC) · рабочие источники · настройки.
const C = "/api/modules/cabinet";
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const fmt = (n) => (n || 0).toLocaleString("ru-RU");
const LBL = "font-family:var(--mono);font-size:9.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3)";
const CARD = "padding:20px;border-radius:16px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:14px";

const ROLE_CAPS = {
  manager: ["Чат и запуск агентов", "Экспорт результатов", "✗ Админ-функции и запись коннекторов"],
  analyst: ["Чат, агенты, данные (чтение)", "Экспорт", "✗ Админ-функции"],
  lecturer: ["Полный доступ ко всем вкладкам", "Правка политики RBAC и аудит", "Управление арендаторами"],
  admin: ["Полный доступ", "Управление доступом и биллингом"],
};
// 1:1 из эталона APE Desktop: глиф · заголовок · подпись · подключён(on).
const SOURCES = [
  ["▦", "Office 365", "Word, Excel, Outlook — надстройка активна", true],
  ["⛁", "1С: учёт", "чтение справочников и документов", true],
  ["◵", "Хранилище отчётов", "подключение по правам пользователя", false],
  ["⎘", "Последние файлы", "12 документов за неделю", true],
];
const SHORTCUTS = [["Ctrl K", "командная палитра"], ["Ctrl N", "новый тред"], ["Enter", "отправить"], ["Esc", "стоп / закрыть шторку"]];

export async function mount(root, ctx) {
  const { api } = ctx;
  const myRoles = (ctx.roles && ctx.roles.length) ? ctx.roles : ["manager"];

  let usage = {}; try { const u = await api(C + "/usage"); if (u.ok) usage = u.report || {}; } catch {}
  const w = usage.week || {};
  const kpis = [
    ["токены · неделя", fmt(w.tokens_used), "из " + fmt(w.limit || 25000000), "var(--ink)"],
    ["осталось", (100 - Math.round(w.used_pct || 0)) + "%", "квота недели", "var(--accent-ink)"],
    ["потрачено", (w.cost_rub != null ? w.cost_rub.toFixed(0) : "0") + " ₽", "self-host ≈ 0", "var(--ink)"],
    ["вызовов", fmt(w.calls), "через шлюз", "var(--ink)"],
  ];
  const bars = [["неделя", Math.min(92, Math.round((w.used_pct || 0) * 0.92) || 6)]];

  const kpi = (k) => `<div style="padding:17px;border-radius:14px;background:var(--panel);border:1px solid var(--line);backdrop-filter:blur(16px);display:flex;flex-direction:column;gap:6px">
    <span style="${LBL}">${esc(k[0])}</span><span style="font-size:26px;font-weight:800;letter-spacing:-.8px;color:${k[3]}">${k[1]}</span>
    <span style="font-size:11.5px;color:var(--ink-3)">${esc(k[2])}</span></div>`;

  const roleBtns = ["manager", "analyst", "lecturer"].map((r) => { const on = myRoles.includes(r); return `<button data-role="${r}" style="padding:9px 15px;border:1px solid ${on ? "var(--accent)" : "var(--line)"};border-radius:9999px;background:${on ? "var(--accent-bg)" : "var(--panel)"};color:${on ? "var(--accent-ink)" : "var(--ink-2)"};font-size:12.5px;font-weight:600;cursor:pointer">${r}</button>`; }).join("");
  const capsOf = (r) => (ROLE_CAPS[r] || []).map((t) => { const no = t.startsWith("✗"); return `<span style="display:flex;align-items:flex-start;gap:9px;font-size:12.5px;line-height:1.5;color:var(--ink-2)"><span style="color:${no ? "var(--danger-ink)" : "var(--ok-ink)"};font-weight:700">${no ? "✗" : "✓"}</span>${esc(t.replace(/^✗\s*/, ""))}</span>`; }).join("");
  const primaryRole = myRoles.find((r) => ROLE_CAPS[r]) || "manager";

  const sources = SOURCES.map((s) => { const on = s[3]; return `<div style="display:flex;align-items:center;gap:12px;padding:11px 13px;border-radius:11px;background:var(--hover);border:1px solid var(--line)">
    <span style="font-size:15px">${s[0]}</span>
    <span style="flex:1;display:flex;flex-direction:column;gap:2px"><span style="font-size:12.5px;font-weight:600">${esc(s[1])}</span><span style="font-size:11px;color:var(--ink-3)">${esc(s[2])}</span></span>
    <span class="chip ${on ? "on" : ""}">${on ? "подключён" : "подключить"}</span></div>`; }).join("");

  root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
    <div style="max-width:1020px;margin:0 auto;display:flex;flex-direction:column;gap:22px;animation:ape-in .35s ease-out">
      <div style="display:flex;flex-direction:column;gap:6px">
        <h1 style="margin:0;font-size:26px;font-weight:800;letter-spacing:-.7px">Кабинет</h1>
        <p style="margin:0;font-size:13px;color:var(--ink-2)">Расход, доступы и рабочие источники этого рабочего места.</p>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px">${kpis.map(kpi).join("")}</div>
      <div style="${CARD}">
        <span style="${LBL}">расход по неделе</span>
        <div style="display:flex;align-items:flex-end;gap:8px;height:92px">
          ${bars.map((b) => `<span title="${esc(b[0])}" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:7px">
            <span style="width:100%;height:${b[1]}px;border-radius:5px 5px 0 0;background:linear-gradient(180deg,#8b5cf6,#6366f1);opacity:.8"></span>
            <span style="font-family:var(--mono);font-size:9.5px;color:var(--ink-3)">${esc(b[0])}</span></span>`).join("")}
        </div>
      </div>
      <div style="display:flex;gap:16px;flex-wrap:wrap">
        <div style="flex:1 1 380px;min-width:320px;${CARD}">
          <span style="${LBL}">роль и доступ (RBAC)</span>
          <div style="display:flex;gap:8px;flex-wrap:wrap">${roleBtns}</div>
          <div id="caps" style="display:flex;flex-direction:column;gap:9px">${capsOf(primaryRole)}</div>
          <span style="font-size:11px;color:var(--ink-3)">Роли приходят из Keycloak (твой JWT). Энфорс — на шлюзе.</span>
        </div>
        <div style="flex:1 1 380px;min-width:320px;${CARD};gap:12px">
          <span style="${LBL}">рабочие источники</span>${sources}
        </div>
      </div>
      <div style="${CARD};gap:16px">
        <span style="${LBL}">настройки</span>
        <label style="display:flex;flex-direction:column;gap:7px">
          <span style="font-size:12.5px;font-weight:600">Персона и постоянные инструкции</span>
          <textarea id="persona" rows="3" placeholder="Кто я, как отвечать (подмешивается в ответы)…" style="padding:11px 13px;border-radius:11px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px;line-height:1.55"></textarea>
        </label>
        <div style="display:flex;flex-direction:column;gap:9px">
          <span style="font-size:12.5px;font-weight:600">Горячие клавиши</span>
          ${SHORTCUTS.map((s) => `<span style="display:flex;align-items:center;gap:10px;font-size:12px;color:var(--ink-2)"><span style="font-family:var(--mono);font-size:10.5px;padding:3px 8px;border-radius:7px;background:var(--hover);border:1px solid var(--line)">${esc(s[0])}</span>${esc(s[1])}</span>`).join("")}
        </div>
      </div>
    </div></div>`;

  root.querySelectorAll("[data-role]").forEach((b) => b.onclick = () => { root.querySelector("#caps").innerHTML = capsOf(b.dataset.role); });
  const p = root.querySelector("#persona");
  p.value = localStorage.getItem("ape_persona") || "";
  p.onchange = () => localStorage.setItem("ape_persona", p.value);
}
