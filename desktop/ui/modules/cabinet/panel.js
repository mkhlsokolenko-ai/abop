// Модуль «Личный кабинет» — РЕАЛЬНЫЕ данные, без хардкода: мой доступ (JWT/RBAC), мои агенты и
// инструменты, политика ролей (влита из «Безопасности», #11), рабочие источники, локальные настройки.
const C = "/api/modules/cabinet";
const SEC = "/api/modules/security";   // сайдкар-роуты остались (модуль скрыт, данные берём сюда)
const AG = "/api/modules/agents";
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const LBL = "font-family:var(--mono);font-size:9.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3)";
const CARD = "padding:20px;border-radius:16px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:14px;box-shadow:var(--shadow-1);backdrop-filter:var(--blur)";
const SHORTCUTS = [["Ctrl K", "командная палитра"], ["Ctrl N", "новый тред"], ["Enter", "отправить"], ["Esc", "стоп / закрыть шторку"]];

export async function mount(root, ctx) {
  const { api } = ctx;
  root.innerHTML = `<div style="padding:30px;color:var(--ink-3)">Загрузка кабинета…</div>`;
  // всё реальное, параллельно; каждый источник — с безопасным фолбэком (идемпотентно, без обманок)
  const [u, me, pol, agents, srcRes] = await Promise.all([
    api(C + "/usage").catch(() => ({})),
    api(SEC + "/me").catch(() => ({})),
    api(SEC + "/policy").catch(() => ({})),
    api(AG + "/catalog").catch(() => []),
    api(C + "/sources").catch(() => ({})),
  ]);
  const ok = u && u.ok;
  const myRoles = (me.roles && me.roles.length) ? me.roles : ((ok && u.roles) || (ctx.roles || []));
  const dept = (ok && u.department) || "—";
  const level = (ok && u.level) || "—";
  const rt = (ok && u.runtime) || {};
  const llm = rt.llm && (rt.llm.model || rt.llm.active || rt.llm.name) ? (rt.llm.model || rt.llm.active || rt.llm.name) : (typeof rt.llm === "string" ? rt.llm : "—");
  const catl = Array.isArray(agents) ? agents : [];
  const mine = catl.filter((a) => a.owner), common = catl.filter((a) => !a.owner);
  const srcList = (srcRes && srcRes.connectors) || [];
  const enforced = me.ok && me.enforced;

  const kpi = (label, val, note, col) => `<div style="padding:17px;border-radius:14px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:6px">
    <span style="${LBL}">${esc(label)}</span><span style="font-size:20px;font-weight:800;letter-spacing:-.6px;color:${col || "var(--ink)"};overflow:hidden;text-overflow:ellipsis">${esc(String(val))}</span>
    <span style="font-size:11.5px;color:var(--ink-3)">${esc(note)}</span></div>`;
  const chips = (arr, danger) => (arr && arr.length ? arr.map((x) => `<span class="chip ${danger ? "" : "on"}"${danger ? ' style="color:var(--danger-ink)"' : ""}>${esc(x)}</span>`).join("") : `<span style="font-size:12px;color:var(--ink-3)">—</span>`);
  const roleCard = (r) => `<div style="${CARD};padding:16px;gap:10px">
    <b style="font-size:14px">${esc(r.role)}</b>
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span style="${LBL};width:74px">разрешено</span>${chips(r.allowed)}</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span style="${LBL};width:74px">запрещено</span>${chips(r.denied, true)}</div></div>`;

  root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
    <div style="max-width:1020px;margin:0 auto;display:flex;flex-direction:column;gap:22px;animation:ape-in .35s ease-out">
      <div style="display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap">
        <div style="flex:1 1 340px;display:flex;flex-direction:column;gap:6px">
          <h1 style="margin:0;font-size:26px;font-weight:800;letter-spacing:-.7px">Личный кабинет</h1>
          <p style="margin:0;font-size:13px;color:var(--ink-2)">Мой доступ, мои агенты и инструменты, права и рабочие источники.</p>
        </div>${enforced ? '<span class="chip on">RBAC активен · энфорс на шлюзе</span>' : `<span class="chip">${me.error === "auth_required" ? "войдите — покажу ваши права" : "предпросмотр"}</span>`}
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px">
        ${kpi("отдел (ABAC)", dept, "область доступа к данным")}
        ${kpi("уровень", level, "потолок автономии", "var(--accent-ink)")}
        ${kpi("роли (RBAC)", myRoles.join(", ") || "—", "realm abop · из JWT")}
        ${kpi("LLM рантайм", llm, "self-host ≈ 0 ₽/вызов")}
      </div>
      <div style="display:flex;gap:16px;flex-wrap:wrap">
        <div style="flex:1 1 380px;min-width:320px;${CARD}">
          <span style="${LBL}">мой доступ (из JWT Keycloak)</span>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span style="${LBL};width:74px">роли</span>${chips(myRoles)}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span style="${LBL};width:74px">можно</span>${chips(me.allowed)}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span style="${LBL};width:74px">нельзя</span>${chips(me.denied, true)}</div>
          <span style="font-size:11px;color:var(--ink-3)">Роли из Keycloak (твой JWT). Энфорс — в ABOP на шлюзе (агент × инструмент × источник).</span>
        </div>
        <div style="flex:1 1 380px;min-width:320px;${CARD}">
          <span style="${LBL}">мои агенты и инструменты</span>
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            ${kpi("мои агенты", mine.length, "созданы под моим ID")}
            ${kpi("общие (ABAC)", common.length, "доступны по роли")}
            ${kpi("источники", srcList.length, "рабочие подключения")}
          </div>
          ${mine.length ? `<div style="display:flex;flex-direction:column;gap:5px">${mine.slice(0, 6).map((a) => `<span style="font-size:12px;color:var(--ink-2)">🤖 ${esc(a.name)} <span style="color:var(--ink-3)">· ${esc(a.family || "")} · v${esc(String(a.version || 1))}</span></span>`).join("")}</div>` : `<span style="font-size:12px;color:var(--ink-3)">Своих агентов пока нет — соберите во вкладке «Мои агенты».</span>`}
          ${srcList.length ? `<div style="display:flex;flex-direction:column;gap:5px">${srcList.slice(0, 6).map((s) => `<span style="font-size:12px;color:var(--ink-2)">🔌 ${esc(s.title || s.id)} <span style="color:var(--ink-3)">· ${s.status === "ready" ? "готов" : "скоро"}</span></span>`).join("")}</div>` : `<span style="font-size:12px;color:var(--ink-3)">Нет подключённых источников.</span>`}
        </div>
      </div>
      ${(pol.roles && pol.roles.length) ? `<span style="${LBL}">политика ролей · роль → разрешено / запрещено</span>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px">${pol.roles.map(roleCard).join("")}</div>` : ""}
      <div style="${CARD};gap:16px">
        <span style="${LBL}">настройки</span>
        <label style="display:flex;flex-direction:column;gap:7px">
          <span style="font-size:12.5px;font-weight:600">Персона и постоянные инструкции <span style="font-weight:400;color:var(--ink-3)">· сохраняется локально на этом устройстве</span></span>
          <textarea id="persona" rows="3" placeholder="Кто я, как отвечать…" style="padding:11px 13px;border-radius:11px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px;line-height:1.55"></textarea>
        </label>
        <div style="display:flex;flex-direction:column;gap:9px">
          <span style="font-size:12.5px;font-weight:600">Горячие клавиши</span>
          ${SHORTCUTS.map((s) => `<span style="display:flex;align-items:center;gap:10px;font-size:12px;color:var(--ink-2)"><span style="font-family:var(--mono);font-size:10.5px;padding:3px 8px;border-radius:7px;background:var(--hover);border:1px solid var(--line)">${esc(s[0])}</span>${esc(s[1])}</span>`).join("")}
        </div>
      </div>
    </div></div>`;

  const p = root.querySelector("#persona");
  if (p) { p.value = localStorage.getItem("ape_persona") || ""; p.onchange = () => localStorage.setItem("ape_persona", p.value); }
}
