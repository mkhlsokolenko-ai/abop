// Модуль «Кабинет» — реальные данные: мой доступ (роли/отдел из учётной записи ABOP), мои агенты,
// источники, настройки (персона — реально уходит в чат) и горячие клавиши.
//
// UX-аудит 25.09 D-H7: «Персона» раньше писалась в localStorage и нигде не читалась — теперь чат
// передаёт её в system-подсказку каждого ответа; хардкод-политика ролей помечена честно как пример,
// пока энфорс на шлюзе не отдаёт реальную; «источники» считаются из реального каталога коннекторов.
// D-H13: хоткей Ctrl+Shift+A описан здесь. Терминология без JWT/realm (D-H12).
import { CARD, head } from "../../core/cards.js";
import { icon } from "../../core/icons.js";
// Стили — из ядра: шесть разных карточек и пять разных подписей в панелях были
// расхождением, а не выбором (DIZAJN, раздел 2).
const C = "/api/modules/cabinet";
const SEC = "/api/modules/security";
const AG = "/api/modules/agents";
const K = "/api/modules/connectors";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Что бот умеет и чего не умеет — рядом с кнопкой привязки, а не в документации: человек
// решает, нужен ли ему канал, именно здесь.
const TG_МОЖЕТ = "Поставить задачу словами, получить результат, посмотреть что считается и расписания.";
const TG_НЕ_МОЖЕТ = "Не пишет в чужие системы (письма, задачи в трекере) и не собирает агентов — "
  + "это из приложения, где видно последствия и есть подтверждение.";

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
  // Состояние апдейтера берём у главного процесса: «баннера нет» значит сразу две разные вещи —
  // «у вас последняя версия» и «проверка не доходит», и различать их должен экран, а не человек.
  let апдейтер = null;
  try {
    if (window.ape && window.ape.updater && window.ape.updater.state) {
      апдейтер = await window.ape.updater.state();
    }
  } catch { апдейтер = null; }

  const [u, me, pol, agents, conns, guides, деньги] = await Promise.all([
    api(C + "/usage").catch(keep),
    api(SEC + "/me").catch(keep),
    api(SEC + "/policy").catch(keep),
    api(AG + "/catalog").catch(keep),
    api(K + "/list").catch(keep),
    api(C + "/guides").catch(() => ({ guides: [] })),
    // Квота и расход: вопрос «сколько осталось» задают раньше, чем «сколько потрачено».
    api(C + "/billing").catch(() => null),
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

  // ── состояние установки: обновление, связь, расход ────────────────────────────────────────
  const давность = (когда) => {
    if (!когда) return "проверки ещё не было";
    const мин = Math.round((Date.now() - Number(когда)) / 60000);
    if (мин < 1) return "проверено только что";
    if (мин < 60) return `проверено ${мин} мин назад`;
    const ч = Math.round(мин / 60);
    return ч < 48 ? `проверено ${ч} ч назад` : `проверено ${Math.round(ч / 24)} дн. назад`;
  };
  const карточкаОбновления = () => {
    if (!апдейтер) {
      return `<div class="card calm">
        <div class="card-head"><span class="lbl">обновление</span>
          <span class="t">Авто-обновление в этой сборке выключено</span></div>
        <span style="font-size:11.5px;color:var(--ink-3)">Так собран клиент под macOS (нет подписи Developer ID) и веб-версия. Обновлять вручную — новым установщиком.</span></div>`;
    }
    const состояние = String(апдейтер.state || "");
    const готово = состояние === "ready";
    const ошибка = String(апдейтер["ошибка"] || "");
    // Тон карточки — класс темы: «скачано» спокойное, сбой проверки жёлтый. Подбирать цвета
    // в панели значит заводить ещё одно определение тех же состояний.
    const тонКласс = ошибка ? " warn" : готово ? "" : " calm";
    const подпись = готово ? `Обновление v${esc(String(апдейтер.version || ""))} скачано`
      : ошибка ? "Проверка обновлений не удалась"
        : состояние === "available" ? `Найдено обновление v${esc(String(апдейтер.version || ""))} — скачиваю`
          : `Установлена последняя версия · v${esc(String(апдейтер["версия"] || ""))}`;
    const пояснение = ошибка
      ? esc(ошибка) + " · повторю сама, можно и вручную"
      : готово
        ? "Приложение обновится при перезапуске."
        : "Проверка идёт при запуске, каждые 6 часов и после пробуждения машины — ждать запуска больше не нужно.";
    return `<div class="card${тонКласс}"${готово ? ' style="border-color:var(--ok-line)"' : ""}>
      <div class="card-head">
        <span style="color:${готово ? "var(--ok-ink)" : ошибка ? "var(--warn-ink)" : "var(--ink-2)"};display:flex">${
          icon(готово ? "download" : ошибка ? "alert" : "check", "xl")}</span>
        <span class="t">${подпись}</span>
        <span class="tag ${ошибка ? "mid" : "ok"} right">${esc(давность(апдейтер["проверено"]))}</span>
      </div>
      <div class="why">${пояснение}</div>
      <div class="acts">
        ${готово ? `<button class="btn ok" id="cabUpdNow">${icon("refresh")}Перезапустить и обновить</button>` : ""}
        <button class="btn ghost sm" id="cabUpdCheck">${icon("download")}Проверить обновления</button>
      </div></div>`;
  };
  const карточкаСвязи = () => {
    const свой = !ctx.tenant || ctx.tenant === ctx.tenantOwn;
    // Строка переносится целиком, а не по буквам: в узкой карточке колонка «1fr» ломала адрес
    // сервера в столбик из отдельных символов — на экране это читалось как сбой.
    const строка = (подпись, значение, метка, тон) => `<div class="row r4" style="border:0;padding:6px 0">
      <span>${esc(подпись)}</span>
      <span class="num" style="color:var(--ink-2);word-break:break-word">${esc(значение)}</span>
      <span class="tag ${тон === "ok" ? "ok" : "mid"}">${esc(метка)}</span><span></span></div>`;
    return `<div class="card calm">
      <div class="card-head">${icon("link", "lg")}<span class="t">Связь и данные</span></div>
      ${строка("Сервер ABOP", String(ctx.base || "—"), ctx.authed ? "есть связь" : "нет входа", ctx.authed ? "ok" : "warn")}
      ${строка("Организация", String(ctx.tenant || "—"),
               свой ? "вы в своих данных" : "данные клиента", свой ? "ok" : "warn")}
      ${строка("Движок приложения", "сайдкар " + String((rt && rt.version) || u.version || "—"),
               ok ? "работает" : "не отвечает", ok ? "ok" : "warn")}
      <div class="origin">«Вы в своих данных» сказано утвердительно: состояние должно быть видно и когда всё в порядке, а не только когда что-то не так.</div></div>`;
  };
  const карточкаРасхода = () => {
    const б = деньги || {};
    if (!б || (!б.tokens && !б.spent_rub)) return "";
    const доля = Math.min(100, Math.round(Number(б.tokens_pct || 0)));
    return `<div class="card calm">
      <span class="lbl">расход</span>
      <div class="kpi" style="border:0;background:none;box-shadow:none;padding:0">
        <b style="font-size:26px">${esc(б.spent_rub != null ? Number(б.spent_rub).toLocaleString("ru-RU") + " ₽" : "≈0 ₽")}</b>
        <span class="cap">из квоты ${esc(String(б.token_quota || "—"))} токенов</span>
        <span class="sub">использовано ${esc(String(б.tokens || 0))} · остаток ${esc(String(б.tokens_remaining || "—"))}</span>
      </div>
      <div style="height:6px;border-radius:6px;background:var(--hover);overflow:hidden">
        <div style="width:${доля}%;height:100%;background-image:var(--grad)"></div></div>
      <div class="origin">Токены реальные, из прогонов агентов и чата.</div></div>`;
  };

  root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
    <div style="max-width:1020px;margin:0 auto;display:flex;flex-direction:column;gap:22px;animation:ape-in .35s ease-out">
      ${head({
        "глаз": "рабочее место",
        "что": "Личный кабинет",
        "подпись": "Мой доступ, мои агенты и источники, настройки ответа и горячие клавиши.",
        "действия": [enforced
          ? '<span class="tag ok">права проверяются на шлюзе ABOP</span>'
          : (authed
            ? '<span class="tag">права из учётной записи ABOP</span>'
            : `<button class="btn primary sm" id="cabLogin">${icon("key")} Войти в ABOP</button>`)],
      })}
      <div class="two">
        <div style="display:flex;flex-direction:column;gap:10px">
          ${карточкаОбновления()}
          ${карточкаСвязи()}
        </div>
        <div style="display:flex;flex-direction:column;gap:10px">
          ${карточкаРасхода()}
        </div>
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
          <div><button class="btn sm" id="goAgents">${icon("agents")}Мои агенты</button> <button class="btn sm" id="goSrc">${icon("connectors")}Источники</button></div>
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
        <div id="tgBox" style="display:flex;flex-direction:column;gap:11px;padding-top:4px"></div>
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

  // ── Telegram: код, привязки, отзыв ────────────────────────────────────────────────────────
  async function нарисоватьКанал() {
    const узел = root.querySelector("#tgBox");
    if (!узел) return;
    let привязки = [];
    let ошибка = "";
    try {
      const r = await api(C + "/channels");
      привязки = (r && r["привязки"]) || [];
      if (r && r.ok === false) ошибка = r.error || "";
    } catch (e) { ошибка = humanError(e); }
    const когда = (t) => t ? new Date(t).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "ещё не пользовались";
    узел.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:7px">
        <span style="font-size:12.5px;font-weight:600">Telegram <span style="font-weight:400;color:var(--ink-3)">· задачи с телефона</span></span>
        <span style="font-size:11.5px;color:var(--ink-2);line-height:1.5">${esc(TG_МОЖЕТ)}</span>
        <span style="font-size:11.5px;color:var(--ink-3);line-height:1.5">${esc(TG_НЕ_МОЖЕТ)}</span>
      </div>
      ${ошибка ? `<div style="font-size:11.5px;color:var(--danger-ink)">Привязки недоступны: ${esc(ошибка)}</div>` : ""}
      ${привязки.length ? `<div style="display:flex;flex-direction:column;gap:6px">${привязки.map((b) => `
        <div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap">
          <span style="font-size:12px;color:var(--ink-2)">чат ${esc(String(b["куда"] || "—"))}</span>
          <span style="font-size:11.5px;color:var(--ink-3)">последний раз: ${esc(когда(b["последнее"]))}</span>
          <button class="btn sm danger tgOff" data-id="${esc(String(b.id))}" style="margin-left:auto">Отвязать</button>
        </div>`).join("")}</div>`
        : `<span style="font-size:12px;color:var(--ink-3)">Ни один чат не привязан.</span>`}
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <button class="btn sm" id="tgCode">Получить код привязки</button>
        <span id="tgOut" style="font-family:var(--mono);font-size:13px;color:var(--ink)"></span>
      </div>`;
    const кнопка = узел.querySelector("#tgCode");
    if (кнопка) кнопка.onclick = async () => {
      кнопка.disabled = true;
      try {
        const r = await api(C + "/telegram/code", { method: "POST" });
        if (!r || r.ok === false || !r["код"]) throw new Error((r && r.error) || "сервер не выдал код");
        узел.querySelector("#tgOut").textContent = r["код"];
        toast(`Код ${r["код"]} — отправьте его боту одним сообщением. Живёт ${r["живёт_минут"]} мин и один раз.`, "ok", { ttl: 12000 });
      } catch (e) { toast("Код не выдан: " + humanError(e), "danger"); }
      кнопка.disabled = false;
    };
    узел.querySelectorAll(".tgOff").forEach((b) => b.onclick = async () => {
      const ok = await ctx.confirm({
        title: "Отвязать чат от вашей учётной записи?",
        text: "Бот перестанет запускать агентов от вашего имени. Запущенные прогоны это не отменяет — "
            + "они уже идут и их результат останется в журнале.",
        okLabel: "Отвязать", danger: true, kicker: "Telegram" });
      if (!ok) return;
      try {
        const r = await api(C + "/channels/" + encodeURIComponent(b.dataset.id), { method: "DELETE" });
        if (r && r.ok === false) throw new Error(r.error || "не отозвано");
        toast("Чат отвязан", "ok");
      } catch (e) { toast("Отвязать не удалось: " + humanError(e), "danger"); }
      нарисоватьКанал();
    });
  }
  нарисоватьКанал();

  const p = root.querySelector("#persona");
  if (p) {
    try { p.value = localStorage.getItem("ape_persona") || ""; } catch { /* noop */ }
    const noteEl = root.querySelector("#personaNote");
    const show = () => { noteEl.textContent = p.value.trim() ? "Применяется ко всем новым ответам в чате." : "Пусто — чат отвечает без персональных инструкций."; };
    show();
    p.onchange = () => { try { localStorage.setItem("ape_persona", p.value); toast("Персона сохранена — чат учтёт её в следующем ответе", "ok"); } catch { toast("Не удалось сохранить на этом устройстве", "danger"); } show(); };
  }
  const lg = root.querySelector("#cabLogin"); if (lg) lg.onclick = () => ctx.login();
  // Обновление ставит тот же путь, что и баннер в шапке: один способ установки на приложение.
  const upNow = root.querySelector("#cabUpdNow");
  if (upNow) upNow.onclick = () => { if (window.ape && window.ape.updater) window.ape.updater.install(); };
  const upChk = root.querySelector("#cabUpdCheck");
  if (upChk) upChk.onclick = async () => {
    if (!(window.ape && window.ape.updater)) return;
    window.ape.updater.check();
    toast("Проверяю обновления…", "ok");
    // Состояние придёт не мгновенно: перечитываем карточку через пару секунд, чтобы человек
    // увидел исход проверки, а не остался с прежней надписью.
    setTimeout(() => mount(root, ctx), 2500);
  };
  root.querySelector("#goAgents").onclick = () => ctx.open("agents");
  root.querySelector("#goSrc").onclick = () => ctx.open("connectors");
}
