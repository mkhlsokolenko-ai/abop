// Модуль «Безопасность» (AdminScreens) — рецепты 1:1 из ДС: панель-карточки, mono-лейблы,
// политика роль→разрешено/запрещено, периметр/эскалация/ре-аттестация/аудит + демо approve/deny.
const S = "/api/modules/security";
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const LBL = "font-family:var(--mono);font-size:9.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3)";
const CARD = "padding:20px;border-radius:16px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:14px";

export async function mount(root, ctx) {
  const { api, gate } = ctx;
  let p = {}; try { p = await api(S + "/policy"); } catch {}
  let me = {}; try { me = await api(S + "/me"); } catch {}
  const enforced = me.ok && me.enforced;
  const badge = enforced
    ? `<span class="chip on">RBAC активен · энфорс на шлюзе</span>`
    : `<span class="chip">${me.error === "auth_required" ? "войдите — покажу вашу роль" : "предпросмотр"}</span>`;
  const meBar = me.ok ? `<div style="${CARD};padding:14px 16px;gap:8px">
      <span style="${LBL}">ваш доступ (из JWT Keycloak)</span>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span style="${LBL};width:74px">роли</span>${(me.roles || []).map((r) => `<span class="chip on">${esc(r)}</span>`).join("")}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span style="${LBL};width:74px">можно</span>${(me.allowed || []).map((x) => `<span class="chip on">${esc(x)}</span>`).join("")}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span style="${LBL};width:74px">нельзя</span>${(me.denied || []).map((x) => `<span class="chip" style="color:var(--danger-ink)">${esc(x)}</span>`).join("") || `<span style="font-size:12px;color:var(--ink-3)">—</span>`}</div>
    </div>` : "";

  const roleCard = (r) => `<div style="${CARD};padding:16px;gap:10px">
    <div style="display:flex;align-items:center;gap:8px"><b style="flex:1;font-size:14px">${esc(r.role)}</b>
      <button title="Править (позже)" disabled style="width:28px;height:28px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--ink-3);font-size:11px">✎</button></div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span style="${LBL};width:74px">разрешено</span>${(r.allowed || []).map((x) => `<span class="chip on">${esc(x)}</span>`).join("")}</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span style="${LBL};width:74px">запрещено</span>${(r.denied || []).map((x) => `<span class="chip" style="color:var(--danger-ink)">${esc(x)}</span>`).join("") || `<span style="font-size:12px;color:var(--ink-3)">—</span>`}</div></div>`;
  const info = (t, body) => `<div style="${CARD};padding:16px;gap:8px"><span style="${LBL}">${esc(t)}</span><div style="font-size:13px;line-height:1.5;color:var(--ink-2)">${body}</div></div>`;

  root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
    <div style="max-width:1020px;margin:0 auto;display:flex;flex-direction:column;gap:20px;animation:ape-in .35s ease-out">
      <div style="display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap">
        <div style="flex:1 1 340px;display:flex;flex-direction:column;gap:6px">
          <h1 style="margin:0;font-size:26px;font-weight:800;letter-spacing:-.7px">Безопасность</h1>
          <p style="margin:0;font-size:13px;color:var(--ink-2)">Кто что может, периметр агентов, эскалация, ре-аттестация и неизменяемый аудит ИБ.</p>
        </div>${badge}
        <button id="gateDemo" style="padding:10px 16px;border:1px solid var(--line-2);border-radius:10px;background:var(--panel);color:var(--ink);font-size:12.5px;font-weight:600;cursor:pointer">Демо approve/deny</button>
      </div>
      ${meBar}
      <span style="${LBL}">политика доступа · роль → разрешено / запрещено</span>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px">${(p.roles || []).map(roleCard).join("")}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px">
        ${info("Периметр агента", esc(p.perimeter || ""))}
        ${info("Модель эскалации", esc((p.escalation || {}).note || ""))}
        ${info("Ре-аттестация прав", "Период " + ((p.reattest || {}).period_days || "—") + " дн. · " + esc((p.reattest || {}).note || ""))}
        ${info("Аудит ИБ · хэш-цепочка", esc(p.audit_note || ""))}
      </div>
      <div style="font-size:12px;color:var(--ink-3)">Роли приходят из Keycloak; правки версионируются и попадают в аудит ИБ. Реальный энфорс — на шлюзе (агент × инструмент × источник).</div>
    </div></div>`;

  root.querySelector("#gateDemo").onclick = async () => {
    const ok = await gate({ title: "Оценка идеи · отправка письма",
      fields: [["Кому", "partner@example.com"], ["Тема", "Оценка идеи · маркетплейс подрядчиков"], ["Провенанс", "312 записей · маска применена"]],
      body: "Добрый день! Прогон завершён: результат положительный при удержании. Критик отметил риск концентрации трафика — предлагаю проверить на втором канале до масштабирования.",
      allowLabel: "Разрешить отправку" });
    alert(ok ? "✓ Разрешено (при коннекторах — отправится)" : "✕ Отклонено — наружу ничего не ушло");
  };
}
