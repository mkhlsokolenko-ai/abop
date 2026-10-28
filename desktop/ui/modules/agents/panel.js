// Модуль «Мои агенты» — тонкий клиент ABOP: каталог агентов (ABAC) → конструктор → запуск.
// Логика на сервере ABOP (governance/HITL/кэш/observability); панель лишь показывает и дёргает прокси
// сайдкара (/api/modules/agents/* → ABOP /api/*). См. docs/ADR_DESKTOP_ABOP_SMYCHKA.md (Фаза 1).
//
// UX-аудит 25.09: D-H6 — «▶ Запустить» открывает чат и запускает агента там (одна поверхность
// результата, HITL и «сделать регулярной» в карточке чата); D-C1 — 401 показывается как «войдите»,
// а не как «у вас нет агентов»; D-H9 — удаление через модалку подтверждения; D-H14 — токены цветов.
import { CARD, head, S_TAG } from "../../core/cards.js";
import { icon } from "../../core/icons.js";
// Стили — из ядра: шесть разных карточек и пять разных подписей в панелях были
// расхождением, а не выбором (DIZAJN, раздел 2).
const A = "/api/modules/agents";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export async function mount(root, ctx) {
  const { api, toast, humanError, confirm: confirmDialog } = ctx;
  let agents = [], families = [], mode = "catalog", tab = "mine", loadErr = null;
  let archived = [];   // Лимб: выведенные из работы агенты, вкладка «Архив»
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
  // ── ПАСПОРТ: что агент знает, к чему привязан и во что обходится прогон ──
  // Карточка в каталоге говорит три вещи: семья, автономия, версия. Чем агент питается, сработает
  // ли он вообще (сущность может быть пустой) и во сколько обходится прогон — из неё узнать было
  // нельзя, и за этим шли в веб. Считает всё сервер: второй расчёт на клиенте разошёлся бы с
  // первым, и человек не знал бы, какому верить.
  const блок = (title, inner) => inner
    ? `<div style="display:flex;flex-direction:column;gap:7px;margin-top:14px">
         <span class="ape-label">${esc(title)}</span>${inner}</div>`
    : "";
  const строка = (left, right, fg) =>
    `<div style="display:flex;align-items:center;gap:9px;padding:8px 11px;border-radius:9px;background:var(--field);border:1px solid var(--line)">
       <span style="flex:1;min-width:0;font-size:12.5px;color:var(--ink-2)">${left}</span>
       ${right ? `<span style="font-family:var(--mono);font-size:11px;color:${fg || "var(--ink-3)"}">${right}</span>` : ""}
     </div>`;

  function passportHTML(p) {
    const мета = ["v" + esc(String(p.version || 1)), esc(p.status_ru || ""), p.owner ? "мой" : "общий",
                  p.edit_scope === "methodologist" ? "правит методолог" : "правлю сам",
                  "автор " + esc(p.created_by || "—")].filter(Boolean).join(" · ");
    const env = p.envelope || {};
    const нав = (p.skills || []).map((s) => строка(
      `<b style="color:var(--ink)">${esc(s.title || s.id)}</b>${s.short ? " — " + esc(s.short) : ""}`,
      [esc(s.mode || "read"), s.egress === "external" ? "наружу" : "", s.hitl ? "HITL" : "",
       s.autonomy && s.autonomy !== "A0" ? esc(s.autonomy) : ""].filter(Boolean).join(" · "))).join("");
    const данные = ((p.depends || {}).data || []).map((d) => строка(
      `${esc(d.entity)} · ${esc((d.fields || []).join(", ") || "поля по схеме")}`,
      d.recipe ? esc(d.recipe) + " · " + (d.records == null ? "наполнение неизвестно" : d.records + " зап.")
               : "нет рецепта — создать",
      d.ready ? "var(--ok-ink)" : "var(--warn-ink)")).join("");
    const дыры = ((p.depends || {}).gaps || []).map((g) =>
      `<div style="font-size:12px;color:var(--warn-ink);padding:7px 10px;border-radius:9px;background:var(--warn-bg);border:1px solid var(--warn-line)">• ${esc(g.text || "")}</div>`).join("");
    const b = p.bindings || {};
    const привязки = [
      ...(b.triggers || []).map((t) => строка(
        "⏱ " + (t.type === "schedule" ? "расписание " + esc(t.cron || "") : t.type === "event" ? "событие " + esc(t.source || "") : esc(t.type || "триггер"))
        + " → " + (t.deliver === "system" ? "в систему" : "в чат")
        + `<br><span style="font-size:11px;color:var(--ink-3)">${t.last_fire ? "последний запуск " + esc(String(t.last_fire).slice(0, 16).replace("T", " ")) : "ещё не срабатывал"}</span>`,
        t.enabled ? "включено" : "выключено", t.enabled ? "var(--ok-ink)" : "var(--ink-3)")),
      ...(b.seats || []).map((s) => строка("🗺 посадка: " + esc(s.title || s.op_id) + " · область " + esc(s.area || "—"),
                                           s.since ? "с " + esc(String(s.since).slice(0, 10)) : "")),
      ...(b.chains || []).map((c) => строка("⛓ цепочка: " + esc(c.name || c.id),
                                            "шаг " + esc((c.at || []).join(", ")) + " из " + esc(String(c.steps || "?")))),
    ].join("") || `<div class="faint" style="font-size:12px">Ни расписаний, ни посадок на карте, ни цепочек — агент запускается только вручную.</div>`;
    const a = p.assessment || {}, r = a.runs || {}, e = a.estimate;
    const вердикт = { ok: ["конверт проверен авто-верификацией", "var(--ok-ink)"],
                      fail: ["авто-верификация нашла нарушение конверта", "var(--danger-ink)"],
                      draft: ["авто-верификации не было — конверт не проверен", "var(--ink-2)"],
                      contract: ["конверт по контракту LUDA", "var(--ink-2)"],
                      external: ["чужой рантайм: графа нет, осматривать нечем", "var(--ink-2)"] }[a.verify]
                    || ["", "var(--ink-2)"];
    const прогоны = !r.total ? "ещё не запускался — оценивать нечем"
      : `прогонов ${r.total} · вердикт закрыт у ${r.ok} · подтверждений HITL ${r.hitl}`
        + (r.last_at ? ` · последний ${esc(String(r.last_at).slice(0, 16).replace("T", " "))}` + (r.last_ok ? " (пройден)" : " (с замечаниями)") : "");
    const деньги = !r.total ? "" : [r.avg_rub != null ? "в среднем " + r.avg_rub + " ₽ за прогон" : "",
                                    r.cost_rub ? "всего " + r.cost_rub + " ₽" : "",
                                    r.avg_seconds != null ? "около " + r.avg_seconds + " с" : ""].filter(Boolean).join(" · ");
    const прогноз = !e ? "" : (((e.unknown || []).length >= (e.steps || 0))
      ? esc(e.note || "оценить нечем")
      : `прогноз прогона ≈ ${e.rub} ₽ · ${e.input_tokens || 0}→${e.output_tokens || 0} токенов · модель ${esc(e.model || "—")} · ${esc(e.note || "")}`);
    return `<div style="font-size:12.5px;line-height:1.55;color:var(--ink-2)">
      <div style="font-family:var(--mono);font-size:11.5px;color:var(--ink-3)">${esc(p.role_title || p.role || "собран из чата")} · семья ${esc(p.family_title || "—")} · автономия ${esc(p.autonomy_max || "—")}</div>
      <div style="font-family:var(--mono);font-size:11.5px;color:var(--ink-3)">${мета}</div>
      ${p.mission ? `<p style="margin:10px 0 0">${esc(p.mission)}</p>` : ""}
      ${env["почему"] ? `<div style="margin-top:10px;color:var(--accent-ink)">${esc(env["почему"])}</div>` : ""}
      ${блок("навыки агента (из его графа)", нав || `<div class="faint" style="font-size:12px">В графе этой версии нет узлов-навыков — агент ничего не сделает.</div>`)}
      ${блок("зависимости по данным", данные || `<div class="faint" style="font-size:12px">Навыки не объявляют источников — агент работает с тем, что дадите в запросе.</div>`)}
      ${дыры ? блок("чего не хватает для работы", дыры + `<div style="font-size:11.5px;color:var(--ink-3)">Пока это не закрыть, прогон отработает вхолостую.</div>`) : ""}
      ${блок("привязки", привязки)}
      ${блок("оценка", `<div style="display:flex;flex-direction:column;gap:5px;padding:10px 11px;border-radius:9px;background:var(--field);border:1px solid var(--line)">
        ${вердикт[0] ? `<span style="color:${вердикт[1]}">${esc(вердикт[0])}</span>` : ""}
        <span>${прогоны}</span>
        ${деньги ? `<span style="font-family:var(--mono);font-size:11.5px;color:var(--ink-3)">${esc(деньги)}</span>` : ""}
        ${прогноз ? `<span style="font-size:12px;color:var(--ink-3)">${прогноз}</span>` : ""}</div>`)}
    </div>`;
  }

  async function showPassport(id, name) {
    let p;
    try { p = await api(A + "/passport/" + encodeURIComponent(id)); }
    catch (e) { toast("Паспорт не загрузился: " + humanError(e), "danger"); return; }
    ctx.modal(name || p.name || "Агент", passportHTML(p), null, "Закрыть",
              { kicker: "паспорт агента", width: "620px" });
  }

  async function loadAgents() {
    loadErr = null;
    try { agents = await api(A + "/catalog"); if (!Array.isArray(agents)) agents = []; } catch (e) { agents = []; loadErr = e; }
    // Архив грузим отдельно и молча: его отсутствие не должно ломать основной каталог.
    try { archived = await api(A + "/catalog?archived=1"); if (!Array.isArray(archived)) archived = []; } catch (e) { archived = []; }
  }
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
    const shown = tab === "mine" ? mine : (tab === "arch" ? archived : common);
    // Действия знаками, без подписей: три подписанные кнопки не влезали в карточку 280 px и
    // переносились — отсюда и съехавшая вёрстка. Подпись у действия осталась в title и aria-label,
    // то есть и в подсказке, и для чтения с экрана.
    // Разрушающие действия стоят справа, отдельно от запуска: промах мышью не должен стоить агента.
    // Значок — из спрайта ядра, а не символ шрифта. Рисованные «▶ ✎ ↺ ⏹ ✕» были разного веса и
    // размера, зависели от шрифта системы и выглядели дёшево рядом с остальными экранами.
    const ico = (cls, имя, title, a) =>
      `<button class="ico ${cls}" data-id="${esc(a.id)}" data-name="${esc(a.name)}" title="${esc(title)}" aria-label="${esc(title)}: ${esc(a.name)}">${icon(имя)}</button>`;
    // В архиве у агента одно осмысленное действие — вернуть. Диалог архивации обещает
    // «вернуть можно в любой момент», ручка /restore есть с самого начала, а кнопки не было:
    // обещание было невыполнимо из десктопа.
    const archActions = (a) => `<div style="display:flex;gap:6px;align-items:center">
      <button class="btn sm restore" data-id="${esc(a.id)}" data-name="${esc(a.name)}" title="Вернуть из архива в работу" aria-label="Вернуть «${esc(a.name)}» из архива">${icon("repeat")}Вернуть</button>
      <span style="flex:1"></span>
      ${ico("danger del", "trash", "Удалить навсегда — все версии", a)}
    </div>`;
    // Действия — знаками, без подписей: три подписанные кнопки не влезали в карточку 280 px и
    // переносились, ломая ряд (03.10). Подпись живёт в title и aria-label — и в подсказке, и для
    // чтения с экрана. Разрушающее стоит справа, отдельно: промах мышью не должен стоить агента.
    const actions = (a) => `<div style="display:flex;gap:6px;align-items:center">
      ${ico("go run", "play", "Запустить — откроется чат", a)}
      ${a.owner ? ico("cfg", "pencil", "Настроить — сохранит новую версию", a) : ""}
      ${(a.version || 1) > 1 ? ico("back", "clock", "Вернуться к предыдущей версии", a) : ""}
      <span style="flex:1"></span>
      ${a.owner ? ico("arch", "exit", "В архив — перестанет запускаться, расписания остановятся", a) : ""}
      ${a.owner ? ico("danger del", "trash", "Удалить навсегда — все версии", a) : ""}
    </div>`;
    // Вкладка — тот же рецепт, что в «Прогонах»: кнопка ядра со счётчиком. Своя вкладка с
    // инлайновыми цветами была шестым способом нарисовать одно и то же.
    const tabBtn = (id, label, n) =>
      `<button type="button" class="tabBtn btn sm${tab === id ? " primary" : ""}" data-tab="${id}" aria-pressed="${tab === id}">${esc(label)} · ${n}</button>`;
    root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto">
      <div style="display:flex;flex-direction:column;gap:18px;padding:22px 26px;max-width:960px;margin:0 auto;animation:ape-in .35s ease-out">
        ${head({
          "глаз": "рантайм ABOP",
          "что": "Мои агенты",
          "подпись": "Запуск идёт в чате: там результат, подтверждение внешних действий и «сделать регулярной».",
          "действия": [
            tab === "mine" ? `<button id="build" class="btn primary">${icon("plus")} Собрать агента</button>` : "",
            `<button id="refresh" class="ico" title="Обновить список агентов" aria-label="Обновить список агентов">${icon("refresh")}</button>`,
          ],
        })}
        ${errorBlock()}
        <div style="display:flex;gap:8px">${tabBtn("mine", "Мои агенты", mine.length)}${tabBtn("common", "Общие агенты", common.length)}${tabBtn("arch", "Архив", archived.length)}</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px">
          ${shown.length ? shown.map((a) => `<div class="lift" style="${CARD}">
            <div style="min-width:0">
              <div style="min-width:0"><div class="ape-label">${esc(a.family || "—")}${a.role ? " · " + esc(a.role) : ""}</div>
                <button type="button" class="pass" data-id="${esc(a.id)}" data-name="${esc(a.name)}"
                        title="Паспорт агента: навыки, зависимости, привязки, оценка"
                        aria-label="Паспорт агента «${esc(a.name)}»"
                        style="padding:0;border:none;background:transparent;text-align:left;font-size:15px;font-weight:700;color:var(--ink);cursor:pointer">${esc(a.name)}</button></div>
            </div>
            ${a.description ? `<div style="font-size:12px;line-height:1.45;color:var(--ink-2)">${esc(a.description)}</div>` : ""}
            <div style="display:flex;gap:5px;flex-wrap:wrap;align-items:center">
              <span class="tag">автономия ${esc(a.autonomy_max || "?")}</span>
              <span class="tag">v${esc(String(a.version || 1))}</span>
              <span class="tag${a.owner ? "" : " ai"}">${a.owner ? "мой" : "общий"}</span>
              ${a.outward ? `<span class="tag mid">${icon("shield")} действует наружу</span>` : ""}
            </div>
            ${tab === "arch" ? archActions(a) : actions(a)}
          </div>`).join("") : (loadErr ? "" : `<div class="faint" style="padding:20px;grid-column:1/-1">${tab === "mine" ? "У вас пока нет своих агентов — нажмите «Собрать агента» в шапке." : (tab === "arch" ? "Архив пуст: выведенных из работы агентов нет." : "Нет общих агентов, доступных вашей роли.")}</div>`)}
        </div>
      </div></div>`;
    root.querySelectorAll(".tabBtn").forEach((b) => (b.onclick = () => { tab = b.dataset.tab; render(); }));
    root.querySelector("#refresh").onclick = async () => { await loadAgents(); render(); };
    const bb = root.querySelector("#build"); if (bb) bb.onclick = async () => { if (!families.length) await loadFamilies(); bFamily = ""; bSkills = new Set(); bName = ""; editingId = null; bOutput = ""; mode = "builder"; render(); };
    root.querySelectorAll(".pass").forEach((b) => (b.onclick = () => showPassport(b.dataset.id, b.dataset.name)));
    root.querySelectorAll(".run").forEach((b) => (b.onclick = () => ctx.open("chat", { runAgent: { id: b.dataset.id, name: b.dataset.name } })));
    root.querySelectorAll(".cfg").forEach((b) => (b.onclick = () => reconfig(b.dataset.id)));
    root.querySelectorAll(".arch").forEach((b) => (b.onclick = () => archiveAgent(b.dataset.id, b.dataset.name)));
    root.querySelectorAll(".del").forEach((b) => (b.onclick = () => deleteAgent(b.dataset.id, b.dataset.name)));
    root.querySelectorAll(".back").forEach((b) => (b.onclick = () => rollbackAgent(b.dataset.id, b.dataset.name)));
    root.querySelectorAll(".restore").forEach((b) => (b.onclick = () => restoreAgent(b.dataset.id, b.dataset.name)));
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

  // Архив и удаление раньше жили в одном диалоге под кнопкой «✕»: человек нажимал «удалить», а
  // выбирать приходилось между двумя действиями с разными последствиями. Теперь у каждого своя
  // кнопка, и подтверждение спрашивает ровно про то, что нажали.
  async function restoreAgent(id, name) {
    try {
      await api(A + "/restore/" + encodeURIComponent(id), { method: "POST" });
      toast(`Агент «${name}» вернулся в работу`, "ok");
    } catch (e) { toast(humanError(e), "danger"); return; }
    await loadAgents();
    tab = "mine";
    render();
  }

  async function archiveAgent(id, name) {
    if (!(await confirmDialog({ title: `Убрать «${name}» в архив?`,
      text: "Агент перестанет запускаться и пропадёт из списка, расписания остановятся. История прогонов останется, вернуть можно в любой момент.",
      okLabel: "В архив" }))) return;
    try { await api(A + "/retire/" + encodeURIComponent(id), { method: "POST" }); toast(`Агент «${name}» в архиве — можно вернуть`, "ok"); }
    catch (e) { toast("Не удалось: " + humanError(e), "danger"); }
    await loadAgents(); render();
  }
  async function deleteAgent(id, name) {
    if (!(await confirmDialog({ title: `Удалить «${name}» навсегда?`,
      text: "Будут удалены все версии агента. Отменить нельзя. Если нужно просто остановить — отправьте его в архив: оттуда агента можно вернуть в любой момент.",
      okLabel: "Удалить навсегда" }))) return;
    try { await api(A + "/" + encodeURIComponent(id), { method: "DELETE" }); toast(`Агент «${name}» удалён`, "ok"); }
    catch (e) { toast("Не удалось: " + humanError(e), "danger"); }
    await loadAgents(); render();
  }

  // ── КОНСТРУКТОР: семья → навыки (чекбоксы, порядок клика) → имя → создать (ABOP /author) ──
  function renderBuilder() {
    const fam = families.find((f) => f.id === bFamily);
    const famSkills = fam ? [...new Set((fam.members || []).flatMap((m) => m.skills || []))] : [];
    const chain = [...bSkills];
    root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto">
      <div style="display:flex;flex-direction:column;gap:16px;padding:22px 26px;max-width:820px;margin:0 auto;animation:ape-in .35s ease-out">
        ${head({
          "глаз": editingId ? "настройка · новая версия" : "конструктор",
          "что": editingId ? "Настроить агента" : "Собрать агента",
          "действия": [`<button id="back" class="btn ghost">${icon("left")} Каталог</button>`],
        })}
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
