// Модуль «Мои агенты» — тонкий клиент ABOP: каталог агентов (ABAC) → конструктор → запуск.
// Логика на сервере ABOP (governance/HITL/кэш/observability); панель лишь показывает и дёргает прокси
// сайдкара (/api/modules/agents/* → ABOP /api/*). См. docs/ADR_DESKTOP_ABOP_SMYCHKA.md (Фаза 1).
//
// UX-аудит 25.09: D-H6 — «▶ Запустить» открывает чат и запускает агента там (одна поверхность
// результата, HITL и «сделать регулярной» в карточке чата); D-C1 — 401 показывается как «войдите»,
// а не как «у вас нет агентов»; D-H9 — удаление через модалку подтверждения; D-H14 — токены цветов.
import { CARD, head, card } from "../../core/cards.js";
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

  // ── рабочее место отдела (мокап v5, перенесён 15.11.2026) ────────────────────────────────
  // Рейл отделов с ИКА-светофором слева; в главном — полоса здоровья, прогрессивная очередь
  // (горячая находка раскрыта целиком, остальные — компактные строки) и фирмы справа.
  // Решения по находкам идут теми же эндпоинтами «Решений»: две поверхности — один журнал,
  // поэтому ни своих статусов, ни своей записи решения здесь не заводим. Правки разбора v5:
  // пустое состояние очереди, компактные фирмы с раскрытием, выбор фирмы режет очередь,
  // иконки отделов в рейле, в полосе только главные числа (расход и веса — в карточке).
  let deptSel = "", deptГотово = false, старыйСайдкар = false;
  let ика_карты = {}, ncrКарты = {}, клиентыРеестр = [];
  let очередь = [], разрезыОч = {}, сводкаОч = {}, ошибкаОчереди = "", очередиГрузят = false;
  let фирма = "", раскрыта = "", busyН = "", сканИдёт = "";
  // Задачи отдела (шаг 5): дверь супервайзера в десктопе. Сайдкар 1.0.43+ — с ручками задач;
  // на старом секция честно говорит про релиз, а не делает вид.
  let задачи = [], задачаТекст = "", раскрыта_задача = "", задачи_умеет = null, busyЗ = "";
  const МОЙ_ОТДЕЛ = String(((ctx.user || {}).department) || "");
  // Иконки отделов (разбор v5, #5): сканирование рейла без чтения. Иконка — из спрайта ядра,
  // не символ шрифта; незнакомая семья получает нейтральный значок, а не случайный.
  const ИКОНКИ_ОТДЕЛОВ = { finance: "doc", audit: "search", management: "board", production: "chain" };
  const иконкаОтдела = (f) => icon(ИКОНКИ_ОТДЕЛОВ[f.id] || "agents");
  // Светофор ИКА — те же границы, что у динамической автономии (ADR-085): ≥ 90 зелёный,
  // 70–90 жёлтый, ниже красный. Не покрытый индекс — «—», а не ноль: ноль значил бы «отдел плох».
  const светофорКласс = (и) => (и == null ? "" : и >= 90 ? " ok" : и >= 70 ? " mid" : " sev");
  const серьёзностьКлассН = (с) => {
    const т = String(с || "").toLowerCase();
    return т.startsWith("выс") ? "sev" : т.startsWith("сред") ? "mid" : "";
  };

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

  // ── ОТДЕЛЫ (шаг 3 «Отделов агентов»): отдел — контекст, а не меню ──
  // Карточка отдела собирается из существующих ручек: ИКА (шаг 2) + аудит назначения (шаг 1) +
  // состав из ростера. Ничего не считаем на клиенте: числа без происхождения на этом экране
  // означали бы, что мы показываем оценку, не объяснив, из чего она сложилась.
  function провалыHTML(rows) {
    if (!rows.length) return `<div style="font-size:12.5px;color:var(--ok-ink)">Назначение всех агентов отдела подтверждено аудитом — каждый делает то, чем назван.</div>`;
    return rows.map((r) => `<div style="padding:8px 11px;border-radius:9px;background:var(--field);border:1px solid var(--line);display:flex;flex-direction:column;gap:3px">
        <div style="display:flex;gap:7px;align-items:center">
          <span class="tag${r["серьёзность"] === "crit" ? " sev" : " mid"}">${r["серьёзность"] === "crit" ? "не то, чем назван" : "не хватает навыков"}</span>
          <b style="font-size:12.5px;color:var(--ink)">${esc(r.name || r.id)} · v${esc(String(r.version || 1))}</b>
        </div>
        <div style="font-size:12px;color:var(--ink-2);line-height:1.45">${esc(r["причина"] || "")}</div>
      </div>`).join("");
  }

  function слагаемоеСтрокой(k, v) {
    const з = v["значение"];
    const число = з == null ? "—" : String(Math.round(з * 100)) + " %";
    const откуда = v["источник"] || v["почему"] || "";
    return строка(`${esc(k)} · <span style="color:var(--ink-3)">${esc(откуда)}</span>`,
                  число + (з != null ? ` · данных: ${v["данных"]}` : ""),
                  з == null ? "var(--ink-3)" : "var(--ink)");
  }

  async function openDeptCard(f) {
    const [ика, аудит] = await Promise.all([
      api(A + "/families/ika?family=" + encodeURIComponent(f.id) + "&дней=30").catch(() => null),
      api(A + "/assignment-audit").catch(() => null),
    ]);
    const старыйСайдкар = !ика && !аудит;   // обе ручки незнакомы установленной версии сайдкара
    const р = ика || {};
    const факты = р["факты"] || {};
    const сл = р["слагаемые"] || {};
    const rows = (((аудит || {})["строки"] || [])).filter((r) => r.family === f.id);
    const состав = (f.members || []).map((m) => строка(
      `<b style="color:var(--ink)">${esc(m.title || m.key)}</b>`,
      (m.skills || []).length + " навыков")).join("");
    const html = `<div style="font-size:12.5px;line-height:1.55;color:var(--ink-2)">
      ${f.mission ? `<p style="margin:0 0 12px">${esc(f.mission)}</p>` : ""}
      ${старыйСайдкар ? `<div style="padding:10px 12px;border-radius:10px;background:var(--warn-bg, var(--field));border:1px solid var(--line);margin-bottom:12px;font-size:12px;color:var(--ink-2)">ИКА и аудит назначения приедут с релизом 1.0.41 — сайдкар установленного приложения ещё не знает этих ручек. Обновление: Кабинет → Обновление.</div>` : ""}
      ${ика ? `<div style="display:flex;gap:14px;align-items:baseline;padding:12px 14px;border-radius:11px;background:var(--field);border:1px solid var(--line)">
        <span style="font-size:30px;font-weight:800;color:var(--ink)">${р["ика"] == null ? "—" : esc(String(р["ика"]))}</span>
        <div style="display:flex;flex-direction:column">
          <span class="ape-label">ИКА отдела · окно ${esc(String(р["окно_дней"] || 30))} дн.</span>
          <span style="font-size:11.5px;color:var(--ink-3)">веса: ${esc((р["веса"] || {})["источник"] || "—")} · прогонов ${esc(String(факты["прогонов"] || 0))} · в среднем ${факты["средняя_стоимость_руб"] != null ? факты["средняя_стоимость_руб"] + " ₽" : "—"} и ${факты["средние_секунды"] != null ? факты["средние_секунды"] + " с" : "—"} за прогон</span>
        </div>
      </div>` : ""}
      ${р["почему_пусто"] ? `<div style="margin-top:9px;font-size:12px;color:var(--warn-ink)">${esc(р["почему_пусто"])}</div>` : ""}
      ${ика ? блок("слагаемые индекса" + (р["не_покрыто"] && р["не_покрыто"].length ? " · не покрыто: " + р["не_покрыто"].join(", ") : ""),
             Object.keys(сл).map((k) => слагаемоеСтрокой(k, сл[k])).join("")) : ""}
      ${аудит ? блок("аудит назначения", провалыHTML(rows)) : ""}
      ${блок("состав ролей", состав || `<div class="faint" style="font-size:12px">Ростер пуст: роли не объявлены.</div>`)}
      ${ика ? `<div style="margin-top:12px;font-size:11.5px;color:var(--ink-3)">ИКА считает система в роли аудитора; агент на свою оценку повлиять не может. Слагаемое без данных — не ноль, а «не покрыто»: ноль означал бы «отдел плох».</div>` : ""}
    </div>`;
    ctx.modal(f.title || f.id, html, null, "Закрыть", { kicker: "карточка отдела", width: "640px" });
  }

  // ── данные рабочей области ─────────────────────────────────────────────────────────────────
  // ИКА по всем семьям сразу: светофор в рейле нужен ДО входа в отдел, иначе человек открывал
  // бы каждый отдел, чтобы узнать его состояние. Отказ ручки (старый сайдкар) не ломает
  // очередь и фирмы — он убирает только числа, которых старая версия не умеет считать.
  async function загрузитьИКА(fid) {
    try { ика_карты[fid] = await api(A + "/families/ika?family=" + encodeURIComponent(fid) + "&дней=30"); }
    catch (e) {
      if (e && e.status === 404) старыйСайдкар = true;
      ика_карты[fid] = null;
    }
  }
  const загрузитьИКАвсех = () => Promise.allSettled(families.map((f) => загрузитьИКА(f.id)));

  async function загрузитьNCR() {
    if (!deptSel) return;
    try { ncrКарты[deptSel] = await api(A + "/ncr?family=" + encodeURIComponent(deptSel)); }
    catch (e) { ncrКарты[deptSel] = null; }   // старый сайдкар — элемент полосы просто не рисуем
  }

  async function загрузитьОчередь() {
    if (!deptSel) return;
    очередиГрузят = true;
    const фильтр = ["отдел=" + encodeURIComponent(deptSel)];
    if (фирма) фильтр.push("organisation=" + encodeURIComponent(фирма));
    try {
      const р = await api("/api/modules/decisions/findings?" + фильтр.join("&"));
      очередь = р.findings || []; разрезыОч = р["разрезы"] || {}; сводкаОч = р["сводка"] || {};
      ошибкаОчереди = "";
    } catch (e) { очередь = []; ошибкаОчереди = humanError(e); }
    // Прогрессивная очередь: раскрыта одна находка — по умолчанию первая (сервер отдал её
    // как самую важную). Решённая ушла из списка — выбор чиним, а не оставляем дыру.
    if (!очередь.some((н) => н.id === раскрыта)) раскрыта = (очередь[0] || {}).id || "";
    очередиГрузят = false;
  }

  // Фирмы — из реестра «Клиентов» (одна поверхность правды: там же правки и состояния),
  // счётчики «ждёт» — из разрезов очереди, которые сервер считает по всему реестру находок.
  async function загрузитьКлиентов() {
    try { const о = await api("/api/modules/clients/registry?все=1"); клиентыРеестр = о.clients || []; }
    catch (e) { клиентыРеестр = []; }   // колонка фирм необязательна — очередь не должна от неё зависеть
  }

  // ── задачи отдела (шаг 5): дверь супервайзера ─────────────────────────────────────────────
  async function загрузитьЗадачи() {
    if (!deptSel) return;
    try {
      const о = await api(A + "/departments/" + encodeURIComponent(deptSel) + "/tasks");
      задачи = о["задачи"] || [];
      задачи_умеет = true;
    } catch (e) {
      if (e && e.status === 404) { задачи_умеет = false; задачи = []; }
      // другой отказ (сеть и т.п.) — задачи_умеет не переворачиваем: ручка умеется, просто сбой
    }
  }

  async function отдатьЗадачу() {
    const текст = (задачаТекст || "").trim();
    if (!текст) { toast("Опишите задачу отделу — супервайзер сам разложит на подзадачи", "warn"); return; }
    const предмет = {};
    if (фирма) {
      const к = клиентыРеестр.find((x) => (x["организация"] || x["название"] || x.id) === фирма);
      if (к) предмет["клиент"] = к.id;
    }
    busyЗ = "create"; render();
    try {
      const р = await api(A + "/departments/" + encodeURIComponent(deptSel) + "/tasks",
        { method: "POST", body: JSON.stringify({ текст, предмет }) });
      задачаТекст = "";
      if (р["почему_пусто"]) {
        toast("Супервайзер не разложил: " + р["почему_пусто"], "warn");
      } else {
        toast("Задача отделу: подзадач — " + ((р["подзадачи"] || []).length || 0), "ok");
        if (р["задача"] && р["задача"].id) раскрыта_задача = р["задача"].id;
      }
      await загрузитьЗадачи();
    } catch (e) {
      toast(humanError(e), "danger");
    }
    busyЗ = ""; render();
  }

  async function развернутьЗадачу(id) {
    if (раскрыта_задача === id) { раскрыта_задача = ""; render(); return; }
    раскрыта_задача = id;
    render();
    try {
      const р = await api(A + "/departments/" + encodeURIComponent(deptSel) + "/tasks/" + encodeURIComponent(id));
      const т = задачи.find((з) => з.id === id);
      if (т) т["карточка"] = р;
    } catch (e) { /* карточка доедет при следующем раскрытии */ }
    render();
  }

  async function вернутьПодзадачу(tid, sid) {
    busyЗ = sid; render();
    try {
      const р = await api(A + "/departments/" + encodeURIComponent(deptSel) + "/tasks/"
        + encodeURIComponent(tid) + "/subtasks/" + encodeURIComponent(sid) + "/retry",
        { method: "POST", body: "{}" });
      toast(р["статус"] === "в очереди" ? "Возвращено в очередь" : "Статус: " + (р["статус"] || "—"), "ok");
      await загрузитьЗадачи();
      const т = задачи.find((з) => з.id === tid);
      if (т && раскрыта_задача === tid) {
        try { т["карточка"] = await api(A + "/departments/" + encodeURIComponent(deptSel) + "/tasks/" + encodeURIComponent(tid)); } catch (e) { /* доедет */ }
      }
    } catch (e) {
      toast(humanError(e), "danger");
    }
    busyЗ = ""; render();
  }

  async function initDept() {
    deptГотово = true;
    if (!deptSel) {
      let сохран = ""; try { сохран = localStorage.getItem("ape_dept_sel") || ""; } catch { /* приватное окно */ }
      // Свой отдел — по умолчанию (шаг 3): расчётчик открывает рабочее место и сразу видит
      // свою очередь. Владелец (отдел «*») получает последний выбор или первый отдел.
      deptSel = (МОЙ_ОТДЕЛ && МОЙ_ОТДЕЛ !== "*" && families.some((f) => f.id === МОЙ_ОТДЕЛ)) ? МОЙ_ОТДЕЛ
        : (families.some((f) => f.id === сохран) ? сохран : ((families[0] || {}).id || ""));
    }
    render();
    await Promise.all([загрузитьИКАвсех(), загрузитьОчередь(), загрузитьNCR(), загрузитьКлиентов(), загрузитьЗадачи()]);
    render();
  }

  async function выбратьОтдел(id) {
    deptSel = id; фирма = ""; раскрыта = ""; раскрыта_задача = "";
    try { localStorage.setItem("ape_dept_sel", id); } catch { /* приватное окно */ }
    render();
    await Promise.all([загрузитьОчередь(), загрузитьNCR(), загрузитьЗадачи()]);
    render();
  }

  async function выбратьФирму(id) {
    const к = клиентыРеестр.find((x) => x.id === id);
    фирма = id && к ? (к["организация"] || к["название"] || к.id) : "";
    раскрыта = "";
    await загрузитьОчередь();
    render();
  }

  // ── решения: те же эндпоинты, что в «Решениях» ────────────────────────────────────────────
  async function решитьНаходку(id, состояние, способ) {
    busyН = id; render();
    try {
      // Какой именно способ принял человек — в журнал решения: у половины кодов способов
      // несколько, и все законны; «подтверждено» без способа через месяц не восстановить.
      const почему = способ ? "принят способ: " + способ : "";
      await api("/api/modules/decisions/findings/" + encodeURIComponent(id) + "/decide",
                { method: "POST", body: JSON.stringify({ состояние, почему }) });
      toast(состояние === "подтверждено" ? (способ ? "Принято: " + способ : "Отмечено как проблема") : "Отмечено как норма", "ok");
    } catch (e) { toast(humanError(e), "danger"); }
    busyН = "";
    // ИКА пересчитывается сразу после слова человека: «вечером вижу, сколько я исправил»
    // должно работать и в 14:32, без ожидания следующего окна.
    await Promise.all([загрузитьИКА(deptSel), загрузитьОчередь(), загрузитьNCR()]);
    render();
  }

  // Отсрочка — законный исход, но только с датой: «отложить навсегда» это не решение, а способ
  // не решать. Пакетная ручка берётся одна находка: у неё есть поле «до», у одиночной — нет.
  async function отложитьНаходку(id) {
    const через = new Date(Date.now() + 7 * 86400000).toISOString().slice(0, 10);
    let дата = "";
    await new Promise((готово) => {
      ctx.modal("Отложить до даты",
        `<div style="display:flex;flex-direction:column;gap:8px">
           <div style="font-size:12.5px;color:var(--ink-2)">Находка вернётся в очередь в этот день; до тех пор она видна в сводке как отложенная.</div>
           <input id="dDo" type="date" value="${через}" style="padding:7px 10px;border-radius:9px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px"/></div>`,
        (b) => { дата = (b.querySelector("#dDo") || {}).value || ""; готово(); },
        "Отложить", { cancelLabel: "Отмена", onClose: () => готово() });
    });
    if (!дата) return;
    busyН = id; render();
    try {
      await api("/api/modules/decisions/findings/decide-batch",
                { method: "POST", body: JSON.stringify({ ids: [id], состояние: "отложено", почему: "", до: дата }) });
      toast("Отложено до " + дата, "ok");
    } catch (e) {
      toast(e && e.status === 404
        ? "Отложенная дата появилась в версии 1.0.32 — обновите приложение (Кабинет → Обновление)"
        : humanError(e), "danger");
    }
    busyН = "";
    await загрузитьОчередь();
    render();
  }

  // ── NCR: несоответствия аудитора качества ─────────────────────────────────────────────────
  function показатьNCR(f) {
    const р = ncrКарты[f.id];
    if (!р) return;
    const строки = (р.ncr || []).map((n) => `<div style="display:flex;flex-direction:column;gap:4px;padding:10px 12px;border-radius:10px;background:var(--field);border:1px solid var(--line)">
        <div style="display:flex;gap:7px;align-items:center;min-width:0">
          <span class="tag${String(n["тяжесть"] || "").toLowerCase().startsWith("выс") ? " sev" : " mid"}">${esc(n["тип"] || "NCR")}</span>
          <b style="font-size:12.5px;color:var(--ink);flex:1;min-width:0">${esc(n["предмет"] || "")}</b>
        </div>
        ${n["что_нарушено"] ? `<div style="font-size:12px;color:var(--ink-2);line-height:1.45">${esc(n["что_нарушено"])}</div>` : ""}
        ${n["источник"] ? `<div style="font-family:var(--mono,monospace);font-size:10.5px;color:var(--ink-3)">${esc(n["источник"])}</div>` : ""}
        ${n["статус"] === "открыто" ? `<div><button type="button" class="btn sm ncrClose" data-id="${esc(n.id)}" title="Закрыть: несоответствие устранено или принято осознанно; менеджер отдела">Закрыть</button></div>` : ""}
      </div>`).join("");
    const ov = ctx.modal("Несоответствия отдела",
      строки || `<div style="font-size:12.5px;color:var(--ok-ink)">Открытых несоответствий нет — аудитор качества молчит, пока факты чистые.</div>`,
      null, "Закрыть", { kicker: (f.title || f.id) + " · NCR", width: "560px" });
    ov.querySelectorAll(".ncrClose").forEach((b) => (b.onclick = async () => {
      b.disabled = true;
      try {
        await api(A + "/ncr/" + encodeURIComponent(b.dataset.id) + "/close", { method: "POST" });
        toast("NCR закрыт", "ok");
        ov.close(true);
        await загрузитьNCR(); render();
      } catch (e) { b.disabled = false; toast(humanError(e), "danger"); }
    }));
  }

  // ── скан фирмы с рабочего места отдела ────────────────────────────────────────────────────
  // Та же цепочка, что в «Клиентах» (вычитка → тред → прогон сверщика → карточка в чате), но
  // без панели прогресса: результат приходит в чат, и отдел не ждёт у экрана. Полный
  // интерактив (выбор организации, поллинг с состоянием) остаётся в «Клиентах».
  async function пересчитать(к) {
    if (!к) return;
    if (сканИдёт) { toast("Скан уже идёт: «" + сканИдёт + "»", "warn"); return; }
    const живые = agents.filter((a) => String(a.role || "") === "svershik" && String(a.status || "") === "deployed")
      .sort((x, y) => (y.version || 0) - (x.version || 0));
    if (!живые.length) {
      toast("Нет развёрнутого агента роли «Сверщик расхождений» — разверните его в этом отделе", "danger");
      return;
    }
    const имя = к["организация"] || к["название"] || к.id;
    сканИдёт = имя; render();
    toast("Вычитаю базу «" + имя + "» его же рецептами…");
    try {
      await api("/api/modules/clients/refresh/" + encodeURIComponent(к.id), { method: "POST" });
      const тред = await api("/api/modules/chat/threads", { method: "POST", body: JSON.stringify({ title: "Скан: " + имя }) });
      const tid = (тред || {}).id;
      if (!tid) throw new Error("тред чата не создался");
      const запуск = await api("/api/modules/chat/threads/" + tid + "/run-agent", { method: "POST",
        body: JSON.stringify({ agent_id: живые[0].id, scope: { "клиент": к.id }, deliver: "chat" }) });
      if (!запуск || запуск.ok === false) throw new Error((запуск || {}).error || "прогон не встал в очередь");
      if (!запуск.job_id) { сканИдёт = ""; toast("Скан «" + имя + "» готов — разбор в чате", "ok"); render(); return; }
      toast("Скан «" + имя + "» в очереди — результат появится в чате «Скан: " + имя + "»", "ok");
      // Поллинг за карточкой: run-job на стороне сайдкара кэширует и пишет карточку в тред по done.
      // Финал — тостом: рабочий стол отдела в это время остаётся рабочим столом.
      const jobId = запуск.job_id, агент = живые[0].id;
      (async () => {
        for (let i = 0; i < 120; i++) {
          await new Promise((r) => setTimeout(r, 5000));
          if (сканИдёт !== имя) return;   // другой скан занял флаг — этот уже не наш
          let ст = null;
          try { ст = await api("/api/modules/chat/threads/" + tid + "/run-job/" + encodeURIComponent(jobId) + "?agent_id=" + encodeURIComponent(агент)); } catch (e) { continue; }
          if (ст && ст.done) {
            сканИдёт = "";
            toast(ст.ok === false ? "Скан «" + имя + "» не выполнился: " + (ст.error || "—") : "Скан «" + имя + "» готов — разбор в чате", ст.ok === false ? "danger" : "ok");
            render();
            return;
          }
        }
        сканИдёт = "";
        toast("Скан «" + имя + "» ещё не завершился за 10 минут — продолжение в чате «Скан: " + имя + "»", "warn");
        render();
      })();
    } catch (e) { сканИдёт = ""; toast(humanError(e), "danger"); render(); }
  }

  // ── разметка рабочей области ──────────────────────────────────────────────────────────────
  function рейлОтделов() {
    const бейдж = (f) => {
      if (старыйСайдкар) return "";
      const и = (ика_карты[f.id] || {})["ика"];
      return `<span class="tag${светофорКласс(и)}" title="ИКА отдела · окно 30 дней${и == null ? " · не покрыто: нет прогонов или решений" : ""}">${и == null ? "—" : esc(String(и))}</span>`;
    };
    return `<div class="dept-rail">
      <span class="ape-label">отделы</span>
      ${families.map((f) => `<button type="button" class="btn sm dnav${f.id === deptSel ? " primary" : ""}" data-ds="${esc(f.id)}"
           style="justify-content:flex-start;width:100%;text-align:left;gap:9px" aria-pressed="${f.id === deptSel}">
          <span style="width:18px;display:inline-grid;place-items:center;flex-shrink:0">${иконкаОтдела(f)}</span>
          <b style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12.5px">${esc(f.title || f.id)}</b>
          ${бейдж(f)}
        </button>`).join("")}
      <span style="font-size:10.5px;color:var(--ink-3);padding:4px 2px;line-height:1.5">светофор — ИКА отдела: ≥ 90 зелёный, ниже 70 красный, «—» не покрыто</span>
    </div>`;
  }

  // Полоса здоровья: ИКА большим числом (светофор красит), слагаемые — барами. Расход, веса и
  // прогонов — в карточке отдела по кнопке: разбор v5 (#3) — в ежедневном поле зрения только
  // главные числа.
  function полосаЗдоровья(f) {
    const и = ика_карты[f.id];
    if (старыйСайдкар)
      return `<div class="card" style="padding:12px 14px;font-size:12.5px;color:var(--ink-2);line-height:1.5">ИКА и аудит назначения приедут с релизом 1.0.41 — сайдкар установленного приложения ещё не знает этих ручек. Обновление: Кабинет → Обновление. Очередь и фирмы ниже работают.</div>`;
    if (!и)
      return `<div class="card" style="padding:12px 14px;display:flex;gap:10px;align-items:center">
        <span style="font-size:12.5px;color:var(--ink-2)">ИКА отдела не посчитался — обычно это мгновенный сбой связи.</span>
        <button type="button" class="btn sm" id="deptIkaRetry">Повторить</button></div>`;
    const сл = и["слагаемые"] || {};
    const ключи = Object.keys(сл);
    const бары = ключи.map((k) => {
      const з = (сл[k] || {})["значение"];
      const ширина = з == null ? 0 : Math.round(з * 100);
      const не_покрыто = (и["не_покрыто"] || []).includes(k);
      const подпись = не_покрыто || з == null ? "не покрыто" : ширина + " %";
      return `<div style="min-width:0">
        <div style="font-size:10px;color:var(--ink-3);margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(k)}</div>
        <div style="height:5px;border-radius:3px;background:var(--line);overflow:hidden"><i style="display:block;height:100%;border-radius:3px;width:${ширина}%;background:${ширина >= 90 ? "var(--ok-ink)" : "var(--accent)"}"></i></div>
        <div style="font-family:var(--mono,monospace);font-size:9.5px;color:${не_покрыто || з == null ? "var(--ink-3)" : "var(--ink-2)"};margin-top:3px">${esc(подпись)}</div>
      </div>`;
    }).join("");
    const ика = и["ика"];
    const n = ncrКарты[f.id];
    const ncrЧисло = n ? Number(n["открытых"] || 0) : null;
    return `<div class="card" style="padding:13px 16px;display:grid;grid-template-columns:auto 1fr auto;gap:18px;align-items:center">
      <div>
        <span style="font-family:var(--mono,monospace);font-size:32px;font-weight:600;font-variant-numeric:tabular-nums;color:${ика == null ? "var(--ink-3)" : ика >= 90 ? "var(--ok-ink)" : ика >= 70 ? "var(--warn-ink)" : "var(--danger-ink)"}">${ика == null ? "—" : esc(String(ика))}</span>
        <div style="font-size:10px;color:var(--ink-3);margin-top:3px">ИКА · окно ${esc(String(и["окно_дней"] || 30))} дн.</div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(${Math.max(ключи.length, 1)},1fr);gap:6px 14px;min-width:0">${бары}</div>
      <div style="display:flex;flex-direction:column;gap:6px">
        ${ncrЧисло == null ? "" : `<button type="button" class="btn sm${ncrЧисло ? " danger" : " ghost"}" id="ncrBtn" title="Несоответствия аудитора качества — список по клику">${ncrЧисло ? "NCR " + ncrЧисло : "NCR чисто"}</button>`}
        <button type="button" class="btn sm ghost" id="deptCard" title="Карточка отдела: аудит назначения, расход, состав ролей">карточка</button>
      </div>
    </div>`;
  }

  // Карточка находки — тот же примитив card() и те же поля, что в «Решениях»: способ, порядок
  // действий, норма. Кнопка называет способ словами — он же попадает в журнал.
  function карточкаНаходки(н, горячая) {
    const ждёт = busyН === н.id;
    const исход = String(н["исход_способа"] || "");
    const способ = String(н["способ"] || "");
    const что_сделать = исход === "выбран" && способ
      ? `<div style="display:flex;flex-direction:column;gap:4px">
           <span class="ape-label">что сделать</span>
           <div style="font-size:13px;font-weight:600;color:var(--ink)">${esc(способ)}</div>
           ${н["как_устранить"] ? `<div style="font-size:12.5px;line-height:1.5;color:var(--ink-2)">${esc(String(н["как_устранить"]))}</div>` : ""}
           ${н["основание_способа"] ? `<div style="font-size:11.5px;color:var(--ink-3)">основание: ${esc(String(н["основание_способа"]))}</div>` : ""}
         </div>`
      : исход
        ? `<div style="display:flex;flex-direction:column;gap:4px">
             <span class="ape-label">способ устранения — ${esc(исход)}</span>
             <div style="font-size:12.5px;line-height:1.5;color:var(--ink-2)">${esc(String(н["почему_способ"] || "") || "методолог способ не объявил")}</div>
           </div>`
        : "";
    return card({
      "тон": горячая ? "важное" : "",
      "что": н["код"] || "находка",
      "класс": н["серьёзность"] || "",
      "класс_тон": серьёзностьКлассН(н["серьёзность"]),
      "состояние": н["величина"] || "",
      "справа": [н["организация"], н["процесс"]].filter(Boolean).join(" · "),
      "суть": [н["название"], н["предмет"] ? "предмет: " + н["предмет"] : ""].filter(Boolean).join(" — "),
      "почему_лид": горячая ? "Почему это первым:" : "Почему это здесь:",
      "почему": (н["почему_наверху"] || []).join(" · "),
      "доказательство": н["доказательство"] || "",
      "происхождение": [н["основание"] ? "основание: " + н["основание"] : "",
                        (н["чего_не_хватило"] || []).join(" · ")].filter(Boolean).join(" · "),
      "что_сделать": что_сделать,
      "действия": [
        исход === "выбран" && способ
          ? `<button data-fy="${esc(н.id)}" data-fsp="${esc(способ)}" class="btn ok sm" ${ждёт ? "disabled" : ""}>${icon("check")}Принять: ${esc(способ)}</button>`
          : `<button data-fy="${esc(н.id)}" class="btn ok sm" ${ждёт ? "disabled" : ""}>${icon("check")}Это проблема</button>`,
        `<button data-fn="${esc(н.id)}" class="btn ghost sm" ${ждёт ? "disabled" : ""}>Так и должно быть</button>`,
        `<button data-fp="${esc(н.id)}" class="btn ghost sm" ${ждёт ? "disabled" : ""}>${icon("clock")}Отложить</button>`,
      ],
    });
  }

  // Прогрессивная очередь (мокап v5): один взгляд — одна находка. Горячая раскрыта целиком,
  // остальные — компактные строки; клик раскрывает. Строка — div, а не button: внутри раскрытой
  // карточки свои кнопки, а кнопка в кнопке — невалидная разметка.
  function находкаСтрокой(н, горячая) {
    if (раскрыта === н.id) return карточкаНаходки(н, горячая);
    return `<div class="card lift" data-fx="${esc(н.id)}" role="button" tabindex="0"
        style="display:flex;gap:10px;align-items:center;padding:10px 13px;cursor:pointer">
        <span class="tag${серьёзностьКлассН(н["серьёзность"])}">${esc(н["код"] || "—")}</span>
        <span style="flex:1;min-width:0">
          <b style="display:block;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)">${esc(н["название"] || "без названия")}</b>
          <span style="font-size:10.5px;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block">${esc([н["организация"], н["процесс"], н["предмет"]].filter(Boolean).join(" · "))}</span>
        </span>
        ${н["величина"] ? `<span style="font-family:var(--mono,monospace);font-size:12px;font-variant-numeric:tabular-nums;flex-shrink:0;color:var(--ink-2)">${esc(н["величина"])}</span>` : ""}
      </div>`;
  }

  function очередьБлок() {
    const заголовок = `требует вашего слова · ${фирма ? фирма : "все фирмы"} · ${очередь.length}`;
    let тело;
    if (очередиГрузят) тело = `<div class="faint" style="padding:14px;font-size:12.5px">собираю очередь отдела…</div>`;
    else if (ошибкаОчереди) тело = `<div class="card" style="padding:12px 14px;font-size:12.5px;color:var(--danger-ink)">${esc(ошибкаОчереди)}</div>`;
    else if (!очередь.length) {
      // Пустое состояние (разбор v5, #1): пустота — тоже контент. Что уже разобрано и куда
      // идти за историей — иначе «пусто» читается как «сломалось».
      const признано = Number(сводкаОч["подтверждено"] || 0), норм = Number(сводкаОч["отклонено"] || 0);
      const скл = (ч, од, дв, мн) => {
        const о = Math.abs(ч) % 100, е = о % 10;
        if (о > 10 && о < 20) return мн;
        if (е > 1 && е < 5) return дв;
        return е === 1 ? од : мн;
      };
      тело = `<div class="card" style="padding:16px;display:flex;flex-direction:column;gap:10px;align-items:flex-start">
        <div style="font-size:13px;font-weight:600;color:var(--ok-ink)">Очередь пуста — все находки разобраны.</div>
        <div style="font-size:12px;color:var(--ink-2)">За всё время отдел признавал проблемой ${признано} ${скл(признано, "находку", "находки", "находок")} · отмечал нормой ${норм}.</div>
        <button type="button" class="btn sm" id="histBtn">История решений — в «Решениях»</button>
      </div>`;
    } else {
      тело = очередь.map((н, i) => находкаСтрокой(н, i === 0)).join(`<div style="height:8px"></div>`);
    }
    return `<div style="display:flex;flex-direction:column;gap:8px;min-width:0">
      <span class="ape-label">${esc(заголовок)}</span>
      ${тело}
    </div>`;
  }

  // Фирмы (разбор v5, #2 и #4): компактная карточка в две строки; клик выбирает фирму —
  // разрез очереди — и раскрывает карточку. Выбор — и раскрытие одним действием: два разных
  // клика по одной строке человек бы не различил.
  function фирмаКарта(к) {
    const имя = к["организация"] || к["название"] || к.id;
    const счёт = {};
    (разрезыОч["организации"] || []).forEach((о) => { счёт[о["имя"]] = Number(о["открытых"] || 0); });
    const ждёт = счёт[имя] || 0;
    const выбрана = фирма === имя;
    const выключен = String(к["состояние"] || "") === "выключен";
    if (!выбрана) {
      return `<div class="card lift" data-firm="${esc(к.id)}" role="button" tabindex="0"
          style="display:flex;flex-direction:column;gap:4px;padding:10px 12px;cursor:pointer;${выключен ? "opacity:.55" : ""}">
          <div style="display:flex;gap:8px;align-items:center;min-width:0">
            <b style="flex:1;min-width:0;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)">${esc(имя)}</b>
            ${ждёт ? `<span class="tag mid">ждёт ${ждёт}</span>` : `<span class="tag">тихо</span>`}
          </div>
          <div style="font-size:10.5px;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">схема ${esc(к["схема_учёта"] || "базовая")} · ${esc((к["период_с"] || "—") + " → " + (к["период_по"] || "—"))}</div>
        </div>`;
    }
    return `<div class="card" style="display:flex;flex-direction:column;gap:7px;padding:12px 13px;border-color:var(--accent);${выключен ? "opacity:.55" : ""}">
        <div style="display:flex;gap:8px;align-items:center;min-width:0">
          <b style="flex:1;min-width:0;font-size:13px;color:var(--ink)">${esc(имя)}</b>
          <span class="tag${выключен ? "" : " ok"}">${выключен ? "выключен" : "активен"}</span>
        </div>
        <div style="font-size:11.5px;color:var(--ink-2)">схема учёта ${esc(к["схема_учёта"] || "базовая")} · период ${esc((к["период_с"] || "—") + " → " + (к["период_по"] || "—"))}</div>
        <div style="font-family:var(--mono,monospace);font-size:10.5px;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">граница ${esc(к["система"] || "—")}</div>
        <div style="font-size:11px;color:var(--ink-3)">доступ: ${esc(к["секрет"] || "не задан")}</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <button type="button" class="btn sm" data-rescan="${esc(к.id)}" ${сканИдёт ? "disabled" : ""} title="Вычитать базу и поставить прогон сверщика — результат в чате">Пересчитать</button>
          <button type="button" class="btn sm ghost" data-refreshonly="${esc(к.id)}" ${сканИдёт ? "disabled" : ""} title="Только перечитать базу, без прогона">Только вычитать</button>
        </div>
      </div>`;
  }

  function фирмыБлок() {
    const счёт = {};
    (разрезыОч["организации"] || []).forEach((о) => { счёт[о["имя"]] = Number(о["открытых"] || 0); });
    const активных = клиентыРеестр.filter((к) => String(к["состояние"] || "") !== "выключен");
    const список = клиентыРеестр.filter((к) => фирма === (к["организация"] || к["название"] || к.id))
      .concat(активных.filter((к) => фирма !== (к["организация"] || к["название"] || к.id)));
    return `<div style="display:flex;flex-direction:column;gap:8px;min-width:0">
      <span class="ape-label">фирмы · ${активных.length}</span>
      <div class="card lift" data-firm="" role="button" tabindex="0"
        style="padding:9px 12px;cursor:pointer;display:flex;gap:8px;align-items:center;${!фирма ? "border-color:var(--accent);background:var(--accent-bg)" : ""}">
        <b style="font-size:12px;color:${!фирма ? "var(--accent-ink)" : "var(--ink-2)"}">все фирмы</b>
        <span style="flex:1"></span>
        <span class="tag${разрезыОч["открытых_всего"] ? " mid" : ""}">всего ${разрезыОч["открытых_всего"] || 0}</span>
      </div>
      ${список.map(фирмаКарта).join("")}
      <div class="card" style="padding:10px 12px;display:flex;gap:8px;align-items:center">
        <span style="font-size:11px;color:var(--ink-3);flex:1;min-width:0">подключение — редкое действие: форма целиком в разделе «Клиенты»</span>
        <button type="button" class="btn sm" id="goClients">Подключить</button>
      </div>
    </div>`;
  }

  // ── разметка задач отдела ──────────────────────────────────────────────────────────────────
  const СТАТУС_КЛАСС = { "выполнено": " ok", "возврат": " mid", "эскалация": " sev", "в очереди": " mid" };

  function задачаБлок() {
    if (задачи_умеет === false)
      return `<div style="display:flex;flex-direction:column;gap:8px">
        <span class="ape-label">задачи отдела</span>
        <div class="card" style="padding:12px 14px;font-size:12.5px;color:var(--ink-2)">Задачи отдела приедут с релизом 1.0.43 — сайдкар установленного приложения ещё не знает этих ручек. Обновление: Кабинет → Обновление.</div>
      </div>`;
    const строки = задачи.map((з) => {
      const стат = Object.entries(з["по_статусу"] || {}).map(([с, n]) =>
        `<span class="tag${СТАТУС_КЛАСС[с] || ""}">${esc(с)}: ${n}</span>`).join(" ");
      const открыт = раскрыта_задача === з.id;
      let карточка = "";
      if (открыт) {
        const к = з["карточка"];
        if (!к) карточка = `<div class="faint" style="font-size:12px;padding:8px 2px">открываю подзадачи…</div>`;
        else {
          const поды = (к["подзадачи"] || []).map((п) => {
            const крит = Array.isArray(п["критерии"]) ? п["критерии"] : [];
            return `<div style="display:flex;flex-direction:column;gap:3px;padding:9px 11px;border-radius:9px;background:var(--field);border:1px solid var(--line)">
              <div style="display:flex;gap:7px;align-items:center;min-width:0">
                <span class="tag${СТАТУС_КЛАСС[п["статус"]] || ""}">${esc(п["статус"] || "—")}</span>
                <b style="font-size:12.5px;color:var(--ink);flex:1;min-width:0">${esc(п["вид"] || "")}</b>
                ${п["попытка"] != null ? `<span style="font-size:10.5px;color:var(--ink-3)">попытка: ${esc(String(п["попытка"]))}</span>` : ""}
              </div>
              ${крит.length ? `<div style="font-size:11px;color:var(--ink-2);line-height:1.5">DoD: ${крит.map((кр) => esc(String(кр))).join(" · ")}</div>` : ""}
              ${(п["причины"] || []).length ? `<div style="font-size:11px;color:var(--ink-3)">${esc((п["причины"] || []).join(" · "))}</div>` : ""}
              ${["возврат", "эскалация"].includes(String(п["статус"])) ? `<div><button type="button" class="btn sm subretry" data-tid="${esc(з.id)}" data-sid="${esc(п.id)}" ${busyЗ === п.id ? "disabled" : ""} title="Вернуть в очередь — если попытки остались; иначе эскалация остаётся честной">Повторить</button></div>` : ""}
            </div>`;
          }).join("");
          карточка = `<div style="display:flex;flex-direction:column;gap:6px;padding:4px 0">${поды || `<span class="faint" style="font-size:12px">подзадач нет — уточните предметы задачи</span>`}</div>`;
        }
      }
      return `<div class="card lift" role="button" tabindex="0" data-tx="${esc(з.id)}"
          style="display:flex;flex-direction:column;gap:6px;padding:10px 13px;cursor:pointer">
          <div style="display:flex;gap:8px;align-items:center;min-width:0">
            <b style="flex:1;min-width:0;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)">${esc(з["текст"] || "")}</b>
            <span class="tag">${esc(String(з["подзадач"] || 0))} подзадач</span>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">${стат || `<span class="faint" style="font-size:10.5px">подзадачи не разложены</span>`}
            <span style="flex:1"></span>
            <span style="font-size:10.5px;color:var(--ink-3)">${esc(String(з["создал"] || ""))} ${esc(String(з["created_at"] || "").slice(0, 10))}</span>
          </div>
          ${карточка}
        </div>`;
    }).join(`<div style="height:8px"></div>`);
    return `<div style="display:flex;flex-direction:column;gap:8px;min-width:0">
      <span class="ape-label">задачи отдела — супервайзер разложит и поставит исполнителям</span>
      <div class="card" style="padding:10px 12px;display:flex;gap:8px;align-items:center">
        <input id="taskText" value="${esc(задачаТекст)}" placeholder="Например: разобрать группу E1 по Жигулёвой — подтвердить и провести"
               style="flex:1;min-width:0;padding:7px 10px;border-radius:9px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px"/>
        <button type="button" class="btn sm primary" id="taskGo" ${busyЗ === "create" ? "disabled" : ""}>Отдать отделу</button>
      </div>
      ${строки || `<div class="faint" style="font-size:12px;padding:4px 2px">Задач отделу ещё не было. Опишите работу — супервайзер разложит на подзадачи-результаты с DoD и раздаст исполнителям.</div>`}
    </div>`;
  }

  function renderОтделы() {
    if (!families.length) return `<div class="faint" style="padding:20px">Ростер семей не загрузился — обновите список.</div>`;
    const f = families.find((x) => x.id === deptSel) || families[0];
    return `<div class="dept-wrap">
      ${рейлОтделов()}
      <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:14px">
        ${полосаЗдоровья(f)}
        <div class="dept-cols">
          ${очередьБлок()}
          ${фирмыБлок()}
        </div>
        ${задачаБлок()}
      </div>
    </div>`;
  }

  // Привязки рабочей области: вызываются из render(), когда открыта вкладка «Отделы».
  function привязатьОтделы() {
    const f = families.find((x) => x.id === deptSel) || families[0] || {};
    root.querySelectorAll(".dnav").forEach((b) => (b.onclick = async () => {
      if (b.dataset.ds !== deptSel) await выбратьОтдел(b.dataset.ds);
    }));
    root.querySelectorAll("[data-fx]").forEach((b) => {
      const откр = () => { раскрыта = b.dataset.fx; render(); };
      b.onclick = откр;
      b.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); откр(); } };
    });
    root.querySelectorAll("[data-fy]").forEach((b) => (b.onclick = () => решитьНаходку(b.dataset.fy, "подтверждено", b.dataset.fsp || "")));
    root.querySelectorAll("[data-fn]").forEach((b) => (b.onclick = () => решитьНаходку(b.dataset.fn, "норма")));
    root.querySelectorAll("[data-fp]").forEach((b) => (b.onclick = () => отложитьНаходку(b.dataset.fp)));
    root.querySelectorAll("[data-firm]").forEach((b) => {
      const выбор = () => { const id = b.dataset.firm; if ((id || "") !== фирмаИд()) выбратьФирму(id); };
      b.onclick = выбор;
      b.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); выбор(); } };
    });
    root.querySelectorAll("[data-rescan]").forEach((b) => (b.onclick = () => пересчитать(клиентыРеестр.find((к) => к.id === b.dataset.rescan))));
    root.querySelectorAll("[data-refreshonly]").forEach((b) => (b.onclick = async () => {
      const к = клиентыРеестр.find((x) => x.id === b.dataset.refreshonly);
      if (!к) return;
      b.disabled = true;
      try {
        await api("/api/modules/clients/refresh/" + encodeURIComponent(к.id), { method: "POST" });
        toast("База «" + (к["организация"] || к.id) + "» вычитана — данные свежие", "ok");
      } catch (e) { toast(humanError(e), "danger"); }
      b.disabled = false;
    }));
    const nb = root.querySelector("#ncrBtn"); if (nb) nb.onclick = () => показатьNCR(f);
    const dc = root.querySelector("#deptCard"); if (dc) dc.onclick = () => openDeptCard(f);
    const hb = root.querySelector("#histBtn"); if (hb) hb.onclick = () => ctx.open("decisions");
    const ir = root.querySelector("#deptIkaRetry");
    if (ir) ir.onclick = async () => { await загрузитьИКА(deptSel); render(); };
    const gc = root.querySelector("#goClients"); if (gc) gc.onclick = () => ctx.open("clients");
    // задачи отдела
    const tt = root.querySelector("#taskText");
    if (tt) {
      tt.oninput = (e) => { задачаТекст = e.target.value; };
      tt.onkeydown = (e) => { if (e.key === "Enter") отдатьЗадачу(); };
    }
    const tg = root.querySelector("#taskGo"); if (tg) tg.onclick = отдатьЗадачу;
    root.querySelectorAll("[data-tx]").forEach((b) => {
      const откр = () => развернутьЗадачу(b.dataset.tx);
      b.onclick = откр;
      b.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); откр(); } };
    });
    root.querySelectorAll(".subretry").forEach((b) => (b.onclick = (e) => {
      e.stopPropagation();   // клик по «Повторить» не должен сворачивать карточку задачи
      вернутьПодзадачу(b.dataset.tid, b.dataset.sid);
    }));
  }
  const фирмаИд = () => {
    const к = клиентыРеестр.find((x) => (x["организация"] || x["название"] || x.id) === фирма);
    return к ? к.id : "";
  };

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
    const shown = tab === "mine" ? mine : (tab === "arch" ? archived : (tab === "dept" ? "" : common));
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
      <div style="display:flex;flex-direction:column;gap:18px;padding:22px 26px;max-width:${tab === "dept" ? "1120px" : "960px"};margin:0 auto;animation:ape-in .35s ease-out">
        ${head({
          "глаз": "рантайм ABOP",
          "что": tab === "dept" ? "Отделы" : "Мои агенты",
          "подпись": tab === "dept"
            ? ((families.find((x) => x.id === deptSel) || families[0] || {}).mission
               || "Рабочее место отдела: полоса здоровья, очередь находок и фирмы.")
            : "Запуск идёт в чате: там результат, подтверждение внешних действий и «сделать регулярной».",
          "действия": [
            tab === "mine" ? `<button id="build" class="btn primary">${icon("plus")} Собрать агента</button>` : "",
            `<button id="refresh" class="ico" title="Обновить" aria-label="Обновить данные раздела">${icon("refresh")}</button>`,
          ],
        })}
        ${errorBlock()}
        <div style="display:flex;gap:8px">${tabBtn("mine", "Мои агенты", mine.length)}${tabBtn("common", "Общие агенты", common.length)}${tabBtn("dept", "Отделы", families.length)}${tabBtn("arch", "Архив", archived.length)}</div>
        ${tab === "dept" ? renderОтделы() : `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px">
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
        </div>`}
      </div></div>`;
    root.querySelectorAll(".tabBtn").forEach((b) => (b.onclick = () => {
      tab = b.dataset.tab;
      if (tab === "dept" && !deptГотово) { initDept(); return; }   // отрисует и перевяжет после загрузки
      render();
    }));
    if (tab === "dept") привязатьОтделы();
    root.querySelector("#refresh").onclick = async () => {
      await loadAgents();
      if (tab === "dept") await Promise.all([загрузитьИКАвсех(), загрузитьОчередь(), загрузитьNCR(), загрузитьКлиентов(), загрузитьЗадачи()]);
      render();
    };
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

  // Ростер грузим ТИХО: вкладка «Отделы» считает агентов по семьям, а отказ ростера не должен
  // пугать тостом того, кто открыл каталог агентов.
  try { families = await api(A + "/families"); if (!Array.isArray(families)) families = []; } catch (e) { families = []; }
  await loadAgents(); render();
  // Интенты оболочки: клик по отделу в рейле (первое открытие — через takeIntent, повторное —
  // событием ape:intent на уже смонтированном разделе).
  const открытьИнтент = async (инт) => {
    if (!инт || !инт.dept) return;
    tab = "dept"; deptSel = инт.dept; deptГотово = true;
    render();
    await Promise.all([загрузитьИКАвсех(), загрузитьОчередь(), загрузитьNCR(), загрузитьКлиентов(), загрузитьЗадачи()]);
    render();
  };
  const инт0 = (typeof ctx.takeIntent === "function" ? ctx.takeIntent() : null) || (root.intent || null);
  root.addEventListener("ape:intent", (e) => { открытьИнтент((e || {}).detail); });
  if (инт0 && инт0.dept) await открытьИнтент(инт0);
}
