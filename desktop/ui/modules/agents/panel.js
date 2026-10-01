// Модуль «Мои агенты» — тонкий клиент ABOP: каталог агентов (ABAC) → конструктор → запуск.
// Логика на сервере ABOP (governance/HITL/кэш/observability); панель лишь показывает и дёргает прокси
// сайдкара (/api/modules/agents/* → ABOP /api/*). См. docs/ADR_DESKTOP_ABOP_SMYCHKA.md (Фаза 1).
//
// UX-аудит 25.09: D-H6 — «▶ Запустить» открывает чат и запускает агента там (одна поверхность
// результата, HITL и «сделать регулярной» в карточке чата); D-C1 — 401 показывается как «войдите»,
// а не как «у вас нет агентов»; D-H9 — удаление через модалку подтверждения; D-H14 — токены цветов.
const A = "/api/modules/agents";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CARD = "padding:18px;border-radius:16px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:12px;box-shadow:var(--shadow-1);backdrop-filter:var(--blur)";

export async function mount(root, ctx) {
  const { api, toast, humanError, confirm: confirmDialog } = ctx;
  let agents = [], families = [], mode = "catalog", tab = "mine", loadErr = null;
  let bFamily = "", bSkills = new Set(), bName = "", editingId = null, bOutput = "";

  async function reconfig(id) {
    if (!families.length) await loadFamilies();
    let full = {}; try { full = await api(A + "/detail/" + encodeURIComponent(id)); } catch (e) { toast(humanError(e), "danger"); return; }
    const g = full.graph || {};
    bFamily = full.family || "";
    bSkills = new Set((g.nodes || []).filter((n) => n.kind === "skill" && n.skill).map((n) => n.skill));
    bName = full.name || "";
    bOutput = ((g.nodes || []).find((n) => n.kind === "skill" && n.output) || {}).output || "";
    editingId = id; mode = "builder"; render();
  }
  async function loadAgents() { loadErr = null; try { agents = await api(A + "/catalog"); if (!Array.isArray(agents)) agents = []; } catch (e) { agents = []; loadErr = e; } }
  async function loadFamilies() { try { families = await api(A + "/families"); if (!Array.isArray(families)) families = []; } catch (e) { families = []; toast(humanError(e), "danger"); } }

  function errorBlock() {
    if (!loadErr) return "";
    const st = loadErr.status;
    return `<div style="${CARD};border-color:${st === 401 ? "var(--warn-line)" : "var(--danger-line)"};gap:8px">
      <div style="font-weight:700">${st === 401 ? "Нужен вход в ABOP" : "Каталог агентов недоступен"}</div>
      <div style="font-size:12.5px;color:var(--ink-2)">${esc(humanError(loadErr))}</div>
      <div style="display:flex;gap:8px">${st === 401 ? `<button class="btn primary sm" id="errLogin">🔑 Войти в ABOP</button>` : ""}<button class="btn sm" id="errRetry">Повторить</button></div></div>`;
  }
  function render() {
    if (mode === "builder") return renderBuilder();
    const mine = agents.filter((a) => a.owner), common = agents.filter((a) => !a.owner);
    const shown = tab === "mine" ? mine : common;
    const tabBtn = (id, label, n) => `<button class="tabBtn btn ${tab === id ? "" : ""}" data-tab="${id}" aria-pressed="${tab === id}" style="${tab === id ? "background:var(--accent-bg);color:var(--accent-ink);border-color:var(--line-2)" : "background:transparent;color:var(--ink-2)"}">${label} · ${n}</button>`;
    root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto">
      <div style="display:flex;flex-direction:column;gap:18px;padding:22px 26px;max-width:960px;margin:0 auto;animation:ape-in .35s ease-out">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
          <div><div class="ape-label">рантайм ABOP</div><h1 class="ape-h1" style="font-size:22px">Мои агенты</h1>
            <p style="margin:6px 0 0;font-size:12.5px;color:var(--ink-2)">Запуск идёт в чате: там результат, подтверждение внешних действий и «сделать регулярной».</p></div>
          <span style="display:flex;gap:8px">
            ${tab === "mine" ? `<button id="build" class="btn primary">＋ Собрать агента</button>` : ""}
            <button id="refresh" class="btn">Обновить</button></span>
        </div>
        ${errorBlock()}
        <div style="display:flex;gap:8px">${tabBtn("mine", "Мои агенты", mine.length)}${tabBtn("common", "Общие агенты", common.length)}</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px">
          ${shown.length ? shown.map((a) => `<div class="lift" style="${CARD}">
            <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px">
              <div style="min-width:0"><div class="ape-label">${esc(a.family || "—")}${a.role ? " · " + esc(a.role) : ""}</div>
                <div style="font-size:15px;font-weight:700;color:var(--ink)">${esc(a.name)}</div></div>
              ${a.owner ? `<button class="ico danger del" data-id="${esc(a.id)}" data-name="${esc(a.name)}" title="Удалить моего агента" aria-label="Удалить агента ${esc(a.name)}">✕</button>` : ""}
            </div>
            ${a.description ? `<div style="font-size:12px;line-height:1.45;color:var(--ink-2)">${esc(a.description)}</div>` : ""}
            <div style="font-size:11.5px;color:var(--ink-3)">автономия ${esc(a.autonomy_max || "?")} · v${esc(String(a.version || 1))}${a.owner ? " · мой" : " · общий"}${a.outward ? " · действует наружу 🛡" : ""}</div>
            <div style="display:flex;gap:8px">
              ${a.owner ? `<button class="btn cfg" data-id="${esc(a.id)}" title="Настроить моего агента — сохранит новую версию" style="flex:none">✎ Настроить</button>` : ""}
              ${(a.version || 1) > 1 ? `<button class="btn back" data-id="${esc(a.id)}" data-name="${esc(a.name)}" title="Вернуться к предыдущей версии агента" style="flex:none">↺ Версии</button>` : ""}
              <button class="btn primary run" data-id="${esc(a.id)}" data-name="${esc(a.name)}" title="Откроет чат и запустит агента там" style="flex:1;white-space:nowrap">▶ Запустить</button>
            </div>
          </div>`).join("") : (loadErr ? "" : `<div class="faint" style="padding:20px;grid-column:1/-1">${tab === "mine" ? "У вас пока нет своих агентов — нажмите «＋ Собрать агента»." : "Нет общих агентов, доступных вашей роли."}</div>`)}
        </div>
      </div></div>`;
    root.querySelectorAll(".tabBtn").forEach((b) => (b.onclick = () => { tab = b.dataset.tab; render(); }));
    root.querySelector("#refresh").onclick = async () => { await loadAgents(); render(); };
    const bb = root.querySelector("#build"); if (bb) bb.onclick = async () => { if (!families.length) await loadFamilies(); bFamily = ""; bSkills = new Set(); bName = ""; editingId = null; bOutput = ""; mode = "builder"; render(); };
    root.querySelectorAll(".run").forEach((b) => (b.onclick = () => ctx.open("chat", { runAgent: { id: b.dataset.id, name: b.dataset.name } })));
    root.querySelectorAll(".cfg").forEach((b) => (b.onclick = () => reconfig(b.dataset.id)));
    root.querySelectorAll(".del").forEach((b) => (b.onclick = () => deleteAgent(b.dataset.id, b.dataset.name)));
    root.querySelectorAll(".back").forEach((b) => (b.onclick = () => rollbackAgent(b.dataset.id, b.dataset.name)));
    const el = root.querySelector("#errLogin"); if (el) el.onclick = () => ctx.login();
    const er = root.querySelector("#errRetry"); if (er) er.onclick = async () => { await loadAgents(); render(); };
  }

  // Откат показывает РАЗНИЦУ до действия: «вернуться к прошлой версии» без списка изменений —
  // это просьба довериться на слово.
  async function rollbackAgent(id, name) {
    let p;
    try { p = await api(A + "/rollback/" + encodeURIComponent(id)); }
    catch (e) { toast("Не удалось посмотреть версии: " + humanError(e), "danger"); return; }
    if (p && p.ok === false) { toast(humanError(p.error), "danger"); return; }
    if (!p.prev) {
      ctx.modal(`Версии агента «${name}»`,
        `<div style="font-size:12.5px;color:var(--ink-2);line-height:1.6">Сейчас версия ${esc(String(p.version || 1))}, и это единственная — откатывать некуда.</div>`,
        null, "", { kicker: "агент" });
      return;
    }
    const diff = (p.diff || []).map((d) => `<div style="font-size:12.5px;color:var(--ink-2);margin:3px 0"><span style="font-family:var(--mono);color:var(--ink-3)">${esc(d.sign)}</span> ${esc(d.text)}</div>`).join("");
    const ok = await confirmDialog({
      title: `Вернуться к версии ${esc(String(p.prev_version))}?`,
      kicker: "откат агента",
      okLabel: "Откатить",
      danger: false,
      text: `<div style="font-size:12.5px;color:var(--ink-2);margin-bottom:8px">Сейчас работает версия ${esc(String(p.version))}. После отката работать будет версия ${esc(String(p.prev_version))}, а нынешняя уйдёт в архив — вернуть её можно в любой момент.</div>${diff}`,
    });
    if (!ok) return;
    try { await api(A + "/rollback/" + encodeURIComponent(id), { method: "POST" }); toast(`«${name}» вернулся к версии ${p.prev_version}`, "ok"); }
    catch (e) { toast("Откат не удался: " + humanError(e), "danger"); return; }
    await loadAgents(); render();
  }

  // Удаление было только жёстким: сносило все версии безвозвратно. Сервер умеет архив, поэтому
  // сначала предлагаем обратимый вариант, а безвозвратный оставляем отдельной кнопкой.
  async function deleteAgent(id, name) {
    let choice = "";
    await new Promise((resolve) => {
      const ov = ctx.modal(`Убрать агента «${name}»?`,
        `<div style="font-size:12.5px;color:var(--ink-2);line-height:1.6">
           <b>В архив</b> — агент перестаёт запускаться и пропадает из списка, расписания
           останавливаются. История прогонов остаётся, вернуть можно в любой момент.<br><br>
           <b>Удалить навсегда</b> — сносятся все версии агента. Отменить нельзя.
         </div>
         <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
           <button class="btn primary" id="agArch">В архив</button>
           <button class="btn danger" id="agDel">Удалить навсегда</button>
         </div>`,
        null, "", { kicker: "агент", onClose: () => resolve() });
      const q = (x) => ov.querySelector(x);
      if (q("#agArch")) q("#agArch").onclick = () => { choice = "retire"; ov.close ? ov.close() : q("#mCancel").click(); resolve(); };
      if (q("#agDel")) q("#agDel").onclick = () => { choice = "delete"; ov.close ? ov.close() : q("#mCancel").click(); resolve(); };
    });
    if (!choice) return;
    if (choice === "delete" && !(await confirmDialog({ title: `Удалить «${esc(name)}» навсегда?`, text: "Будут удалены все версии агента. Отменить нельзя.", okLabel: "Удалить навсегда" }))) return;
    try {
      if (choice === "retire") { await api(A + "/retire/" + encodeURIComponent(id), { method: "POST" }); toast(`Агент «${name}» в архиве — можно вернуть`, "ok"); }
      else { await api(A + "/" + encodeURIComponent(id), { method: "DELETE" }); toast(`Агент «${name}» удалён`, "ok"); }
    } catch (e) { toast("Не удалось: " + humanError(e), "danger"); }
    await loadAgents(); render();
  }

  // ── КОНСТРУКТОР: семья → навыки (чекбоксы, порядок клика) → имя → создать (ABOP /author) ──
  function renderBuilder() {
    const fam = families.find((f) => f.id === bFamily);
    const famSkills = fam ? [...new Set((fam.members || []).flatMap((m) => m.skills || []))] : [];
    const chain = [...bSkills];
    root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto">
      <div style="display:flex;flex-direction:column;gap:16px;padding:22px 26px;max-width:820px;margin:0 auto;animation:ape-in .35s ease-out">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:12px">
          <div><div class="ape-label">${editingId ? "настройка · новая версия" : "конструктор"}</div><h1 class="ape-h1" style="font-size:22px">${editingId ? "Настроить агента" : "Собрать агента"}</h1></div>
          <button id="back" class="btn">← Каталог</button>
        </div>
        <div style="${CARD}">
          <label class="ape-label" for="fam">1 · Семья (отдел)</label>
          <select id="fam"><option value="">— выберите семью —</option>${families.map((f) => `<option value="${esc(f.id)}" ${f.id === bFamily ? "selected" : ""}>${esc(f.title)}</option>`).join("")}</select>
          ${fam ? `<div style="font-size:12px;color:var(--ink-2);line-height:1.5">${esc(fam.mission || "")}</div>` : ""}
        </div>
        ${fam ? `<div style="${CARD}">
          <span class="ape-label">2 · Навыки в цепочку (в порядке клика)</span>
          <div style="display:flex;flex-direction:column;gap:6px">
            ${famSkills.map((s) => { const on = bSkills.has(s); const idx = chain.indexOf(s); return `<label style="display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:9px;border:1px solid ${on ? "var(--accent)" : "var(--line)"};background:${on ? "var(--accent-bg)" : "var(--field)"};cursor:pointer">
              <input type="checkbox" class="sk" value="${esc(s)}" ${on ? "checked" : ""} style="accent-color:var(--accent)"/>
              <span style="font-family:var(--mono);font-size:11px;color:var(--accent-ink-2);min-width:18px">${on ? "#" + (idx + 1) : ""}</span>
              <span style="font-size:12.5px;color:var(--ink)">${esc(s)}</span></label>`; }).join("") || '<span class="faint" style="font-size:12px">У этой семьи нет навыков.</span>'}
          </div>
        </div>` : ""}
        ${chain.length ? `<div style="${CARD}">
          <span class="ape-label">3 · Цепочка и имя</span>
          <div style="font-family:var(--mono);font-size:11.5px;color:var(--accent-ink-2)">${chain.map(esc).join(" → ")}</div>
          <input id="bname" placeholder="Имя агента (необязательно)" value="${esc(bName)}"/>
          <span class="ape-label" style="margin-top:2px">Режим вывода</span>
          <div style="display:flex;gap:6px">
            ${[["", "Наследовать"], ["structured", "Структурный"], ["freeform", "Рассуждения"]].map(([v, t]) => `<button type="button" data-o="${v}" class="bout btn" aria-pressed="${bOutput === v}" style="flex:1;${bOutput === v ? "background:var(--accent-bg);color:var(--accent-ink);border-color:var(--line-2)" : ""}">${t}</button>`).join("")}
          </div>
          <div style="font-size:11.5px;color:var(--ink-3);line-height:1.4">Рассуждения — больше места для плана/письма/анализа; структурный — компактные находки. Пусто = как у навыка.</div>
          <button id="create" class="btn primary" style="margin-top:4px">${editingId ? "Сохранить новую версию" : "Создать агента"} (навыков: ${chain.length})</button>
          ${editingId ? `<div style="font-size:11.5px;color:var(--ink-3)">Сохранение под тем же именем «${esc(bName)}» → новая версия того же агента.</div>` : ""}
          <div id="berr"></div>
        </div>` : ""}
      </div></div>`;
    root.querySelector("#back").onclick = () => { editingId = null; mode = "catalog"; render(); };
    root.querySelector("#fam").onchange = (e) => { bFamily = e.target.value; bSkills = new Set(); render(); };
    root.querySelectorAll(".sk").forEach((c) => (c.onchange = () => { if (c.checked) bSkills.add(c.value); else bSkills.delete(c.value); render(); }));
    const nm = root.querySelector("#bname"); if (nm) nm.oninput = (e) => { bName = e.target.value; };
    root.querySelectorAll(".bout").forEach((b) => (b.onclick = () => { bOutput = b.dataset.o; render(); }));
    const cr = root.querySelector("#create");
    if (cr) cr.onclick = async () => {
      const label = cr.textContent; cr.textContent = "Создаю…"; cr.disabled = true;
      try {
        const r = await api(A + "/author", { method: "POST", body: JSON.stringify({ family: bFamily, skills: [...bSkills], name: bName, output: bOutput }) });
        if (r && r.ok) {
          // Конверт личного агента не объявляется, а выводится из навыков — человеку надо сказать,
          // какой потолок он получил и почему. И сразу показать, чего навыкам не хватает для работы:
          // раньше это выяснялось только из пустого результата прогона.
          const ag = r.agent || {};
          const env = ag.envelope || {};
          const gaps = ag.input_gaps || [];
          toast(editingId ? "Новая версия сохранена" : "Агент создан", "ok");
          if (env["почему"] || gaps.length) {
            ctx.modal(editingId ? "Новая версия готова" : "Агент готов",
              `<div style="font-size:12.5px;color:var(--ink-2);line-height:1.6">
                 ${env["почему"] ? `<div><b>Границы агента:</b> ${esc(env["почему"])}</div>` : ""}
                 ${gaps.length ? `<div style="margin-top:12px"><b>Чего не хватает для работы:</b>
                   ${gaps.map((g) => `<div style="margin-top:5px;color:var(--warn-ink)">• ${esc(g.text || "")}</div>`).join("")}
                   <div style="margin-top:9px;font-size:11.5px;color:var(--ink-3)">Агент сохранён — но пока это не закрыть, он отработает вхолостую: наполните сущность данными или добавьте навык, который даёт недостающий вход.</div></div>`
                   : `<div style="margin-top:10px;color:var(--ok-ink)">Входы навыков покрыты — агент готов к запуску.</div>`}
               </div>`, null, "", { kicker: "личный агент" });
          }
          editingId = null; await loadAgents(); mode = "catalog"; render();
        }
        else { root.querySelector("#berr").innerHTML = `<div class="danger-ink" style="font-size:12px">Ошибка: ${esc(humanError((r && r.error) || "не удалось"))}</div>`; cr.textContent = label; cr.disabled = false; }
      } catch (e) { root.querySelector("#berr").innerHTML = `<div class="danger-ink" style="font-size:12px">${esc(humanError(e))}</div>`; cr.textContent = label; cr.disabled = false; }
    };
  }

  await loadAgents(); render();
}
