// Модуль «Кабинет» — реальные данные: мой доступ (роли/отдел из учётной записи ABOP), мои агенты,
// источники, настройки (персона — реально уходит в чат) и горячие клавиши.
//
// UX-аудит 25.09 D-H7: «Персона» раньше писалась в localStorage и нигде не читалась — теперь чат
// передаёт её в system-подсказку каждого ответа; хардкод-политика ролей помечена честно как пример,
// пока энфорс на шлюзе не отдаёт реальную; «источники» считаются из реального каталога коннекторов.
// D-H13: хоткей Ctrl+Shift+A описан здесь. Терминология без JWT/realm (D-H12).
const C = "/api/modules/cabinet";
const SEC = "/api/modules/security";
const AG = "/api/modules/agents";
const K = "/api/modules/connectors";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CARD = "padding:20px;border-radius:16px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:14px;box-shadow:var(--shadow-1);backdrop-filter:var(--blur)";
const SHORTCUTS = [
  ["Ctrl K", "поиск и команды"], ["Ctrl N", "новый чат"], ["Enter", "отправить · Shift+Enter — перенос строки"],
  ["Esc", "остановить ответ · закрыть шторку или окно"],
  ["Ctrl C / Ctrl V", "копировать и вставить — работают и в поле ввода, и правым щелчком мыши"],
];

// Какое сочетание «анализа выделенного» занято на самом деле. Прежде мы писали Ctrl+Shift+A как
// данность, а если его перехватило другое приложение, человек видел неработающую подсказку: занятый
// хоткей молчал. Теперь главный процесс сообщает занятое сочетание, и подпись говорит правду.
const HOTKEY_RU = { "CommandOrControl+Shift+A": "Ctrl Shift A", "CommandOrControl+Alt+A": "Ctrl Alt A",
                    "CommandOrControl+Shift+F9": "Ctrl Shift F9" };

async function analyzeShortcut() {
  let combo = "CommandOrControl+Shift+A";
  try { if (window.ape && window.ape.hotkey) combo = (await window.ape.hotkey()) || ""; } catch { /* старая сборка */ }
  if (!combo) {
    return ["— занято", "анализ выделенного недоступен: все сочетания перехвачены другими приложениями. "
            + "Скопируйте текст и вставьте в чат (Ctrl V)"];
  }
  return [HOTKEY_RU[combo] || combo,
          "из любого приложения: выделенный текст → в чат ABOP на анализ (буфер обмена не меняется)"];
}

export async function mount(root, ctx) {
  const { api, toast, humanError } = ctx;
  root.innerHTML = `<div style="flex:1;padding:30px;display:flex;flex-direction:column;gap:12px;max-width:760px"><div class="skeleton" style="height:28px;width:40%"></div><div class="skeleton" style="height:14px;width:70%"></div><div class="skeleton" style="height:120px"></div></div>`;
  // Раньше все пять запросов глушились, и при недоступном ABOP кабинет выглядел рабочим:
  // отдел «—», уровень «—», источников 0. Теперь сбой виден, и есть чем его повторить.
  let loadError = "";
  const keep = (e) => { if (!loadError) loadError = humanError(e); return null; };
  // Подпись «анализа выделенного» — по факту занятого сочетания, а не по тому, что мы задумали.
  const analyzeKey = await analyzeShortcut();
  // Руководства отдаёт сам ABOP: документ, лежащий в репозитории, до пользователя не доходит.
  const [u, me, pol, agents, conns, guides] = await Promise.all([
    api(C + "/usage").catch(keep),
    api(SEC + "/me").catch(keep),
    api(SEC + "/policy").catch(keep),
    api(AG + "/catalog").catch(keep),
    api(K + "/list").catch(keep),
    api(C + "/guides").catch(() => ({ guides: [] })),
  ]).then((r) => r.map((x, i) => x === null ? (i === 3 ? [] : {}) : x));

  // Ссылки открываем во внешнем браузере: руководство — документ на 90 страниц, ему не место в окне чата.
  const guideRows = ((guides && guides.guides) || []).map((g) => `
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--ink-2)">
      <span style="font-weight:600;color:var(--ink)">${esc(g.title)}</span>
      <a href="${esc(g.html)}" target="_blank" rel="noopener" class="btn" style="padding:4px 10px;font-size:11.5px;text-decoration:none">Открыть</a>
      ${g.pdf ? `<a href="${esc(g.pdf)}" target="_blank" rel="noopener" class="btn" style="padding:4px 10px;font-size:11.5px;text-decoration:none">PDF</a>` : ""}
      <span style="color:var(--ink-3)">${esc(g.about || "")}</span>
    </div>`).join("");
  if (loadError && !(u && u.ok)) {
    root.innerHTML = `<div style="flex:1;padding:30px;display:flex;flex-direction:column;gap:10px;max-width:560px">
      <div style="font-size:16px;font-weight:700">Кабинет не загрузился</div>
      <div style="font-size:12.5px;color:var(--ink-2);line-height:1.6">${esc(loadError)}</div>
      <button class="btn" id="cabRetry" style="align-self:flex-start;margin-top:6px">Повторить</button></div>`;
    const rb = root.querySelector("#cabRetry"); if (rb) rb.onclick = () => mount(root, ctx);
    return;
  }
  const ok = u && u.ok;
  const myRoles = (me.roles && me.roles.length) ? me.roles : ((ok && u.roles) || (ctx.roles || []));
  const dept = (ok && u.department) || "—";
  const level = (ok && u.level) || "—";
  const rt = (ok && u.runtime) || {};
  const llm = rt.llm && (rt.llm.model || rt.llm.active || rt.llm.name) ? (rt.llm.model || rt.llm.active || rt.llm.name) : (typeof rt.llm === "string" ? rt.llm : "—");
  const catl = Array.isArray(agents) ? agents : [];
  const mine = catl.filter((a) => a.owner), common = catl.filter((a) => !a.owner);
  const abopConns = (conns && conns.connectors) || [], localConns = (conns && conns.local) || [];
  const srcOk = abopConns.filter((c) => c.status !== "blocked");
  const enforced = me.ok && me.enforced;
  const authed = !!ctx.authed;

  const kpi = (label, val, note, col) => `<div style="padding:17px;border-radius:14px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:6px;min-width:0">
    <span class="ape-label">${esc(label)}</span><span style="font-size:20px;font-weight:800;letter-spacing:-.6px;color:${col || "var(--ink)"};overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(String(val))}">${esc(String(val))}</span>
    <span style="font-size:11.5px;color:var(--ink-3)">${esc(note)}</span></div>`;
  const chips = (arr, danger) => (arr && arr.length ? arr.map((x) => `<span class="chip ${danger ? "" : "on"}"${danger ? ' style="color:var(--danger-ink);border-color:var(--danger-line)"' : ""}>${esc(x)}</span>`).join("") : `<span style="font-size:12px;color:var(--ink-3)">—</span>`);
  const roleCard = (r) => `<div style="${CARD};padding:16px;gap:10px">
    <b style="font-size:14px">${esc(r.role)}</b>
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span class="ape-label" style="width:84px">разрешено</span>${chips(r.allowed)}</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span class="ape-label" style="width:84px">запрещено</span>${chips(r.denied, true)}</div></div>`;

  root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
    <div style="max-width:1020px;margin:0 auto;display:flex;flex-direction:column;gap:22px;animation:ape-in .35s ease-out">
      <div style="display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap">
        <div style="flex:1 1 340px;display:flex;flex-direction:column;gap:6px">
          <h1 class="ape-h1">Личный кабинет</h1>
          <p style="margin:0;font-size:13px;color:var(--ink-2)">Мой доступ, мои агенты и источники, настройки ответа и горячие клавиши.</p>
        </div>${enforced ? '<span class="chip on">права проверяются на шлюзе ABOP</span>' : (authed ? '<span class="chip">права из учётной записи ABOP</span>' : `<button class="btn primary sm" id="cabLogin">🔑 Войти в ABOP</button>`)}
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px">
        ${kpi("отдел", dept, "область доступа к данным")}
        ${kpi("уровень", level, "потолок автономии агентов", "var(--accent-ink)")}
        ${kpi("роли", myRoles.join(", ") || "—", "из учётной записи ABOP")}
        ${kpi("модель", llm, "собственная модель · ≈0 ₽ за вызов")}
      </div>
      <div style="display:flex;gap:16px;flex-wrap:wrap">
        <div style="flex:1 1 380px;min-width:320px;${CARD}">
          <span class="ape-label">мой доступ</span>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span class="ape-label" style="width:84px">роли</span>${chips(myRoles)}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span class="ape-label" style="width:84px">можно</span>${chips(me.allowed)}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span class="ape-label" style="width:84px">нельзя</span>${chips(me.denied, true)}</div>
          <span style="font-size:11.5px;color:var(--ink-3)">${authed ? "Роли и отдел приходят из вашей учётной записи ABOP; агент не получает прав больше, чем у вас." : "Войдите — покажу ваши роли, отдел и что разрешено агентам."}</span>
        </div>
        <div style="flex:1 1 380px;min-width:320px;${CARD}">
          <span class="ape-label">мои агенты и источники</span>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px">
            ${kpi("мои агенты", mine.length, "созданы мной")}
            ${kpi("общие", common.length, "доступны по роли")}
            ${kpi("источники", srcOk.length + localConns.length, abopConns.length ? `из ${abopConns.length} в ABOP` : "локальные файлы")}
          </div>
          ${mine.length ? `<div style="display:flex;flex-direction:column;gap:5px">${mine.slice(0, 6).map((a) => `<span style="font-size:12px;color:var(--ink-2)">🤖 ${esc(a.name)} <span style="color:var(--ink-3)">· ${esc(a.family || "")} · v${esc(String(a.version || 1))}</span></span>`).join("")}</div>` : `<span style="font-size:12px;color:var(--ink-3)">Своих агентов пока нет — соберите в разделе «Мои агенты».</span>`}
          ${abopConns.length ? `<div style="display:flex;flex-direction:column;gap:5px">${abopConns.slice(0, 6).map((s) => `<span style="font-size:12px;color:var(--ink-2)">🔌 ${esc(s.title || s.id)} <span style="color:${s.status === "blocked" ? "var(--danger-ink)" : "var(--ink-3)"}">· ${s.status === "blocked" ? "нет доступа" : "агент видит"}</span></span>`).join("")}</div>` : ""}
          <div><button class="btn sm" id="goAgents">Мои агенты →</button> <button class="btn sm" id="goSrc">Источники →</button></div>
        </div>
      </div>
      ${(pol.roles && pol.roles.length) ? `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><span class="ape-label">политика ролей · роль → разрешено / запрещено</span>${enforced ? "" : '<span class="chip" title="Реальная политика применяется на шлюзе ABOP; здесь — справочный пример">пример</span>'}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px">${pol.roles.map(roleCard).join("")}</div>` : ""}
      <div style="${CARD};gap:16px">
        <span class="ape-label">настройки</span>
        <label style="display:flex;flex-direction:column;gap:7px">
          <span style="font-size:12.5px;font-weight:600">Персона и постоянные инструкции <span style="font-weight:400;color:var(--ink-3)">· чат учитывает в каждом ответе · хранится на этом устройстве</span></span>
          <textarea id="persona" rows="3" placeholder="Например: я руководитель отдела продаж; отвечай кратко, по пунктам, на «вы»; суммы в рублях"></textarea>
          <span id="personaNote" style="font-size:11.5px;color:var(--ink-3)"></span>
        </label>
        ${guideRows ? `<div style="display:flex;flex-direction:column;gap:9px">
          <span style="font-size:12.5px;font-weight:600">Руководства</span>
          ${guideRows}
        </div>` : ""}
        <div style="display:flex;flex-direction:column;gap:9px">
          <span style="font-size:12.5px;font-weight:600">Горячие клавиши</span>
          ${SHORTCUTS.concat([analyzeKey]).map((s) => `<span style="display:flex;align-items:center;gap:10px;font-size:12px;color:var(--ink-2)"><span style="font-family:var(--mono);font-size:11px;padding:3px 8px;border-radius:7px;background:var(--hover);border:1px solid var(--line);white-space:nowrap">${esc(s[0])}</span>${esc(s[1])}</span>`).join("")}
        </div>
      </div>
    </div></div>`;

  const p = root.querySelector("#persona");
  if (p) {
    try { p.value = localStorage.getItem("ape_persona") || ""; } catch { /* noop */ }
    const noteEl = root.querySelector("#personaNote");
    const show = () => { noteEl.textContent = p.value.trim() ? "Применяется ко всем новым ответам в чате." : "Пусто — чат отвечает без персональных инструкций."; };
    show();
    p.onchange = () => { try { localStorage.setItem("ape_persona", p.value); toast("Персона сохранена — чат учтёт её в следующем ответе", "ok"); } catch { toast("Не удалось сохранить на этом устройстве", "danger"); } show(); };
  }
  const lg = root.querySelector("#cabLogin"); if (lg) lg.onclick = () => ctx.login();
  root.querySelector("#goAgents").onclick = () => ctx.open("agents");
  root.querySelector("#goSrc").onclick = () => ctx.open("connectors");
}
