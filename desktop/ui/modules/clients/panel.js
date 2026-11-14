// Модуль «Клиенты» — реестр обслуживаемых компаний.
//
// У аутсорсера в одной установке сотни обслуживаемых фирм. До 12.11.2026 завести клиента можно
// было только по ssh: переменная с паролем в .env, строка в файле-источнике, пересоздание
// контейнера, прогон рецепта. Этот экран нужен затем, чтобы это делал тот, кто ведёт клиентов.
//
// Три правила здесь взяты из провалов на живых базах, а не из вкусовщины:
//  * организацию человек ВЫБИРАЕТ из базы, а не набирает. Прогон делит данные по этому полю, и
//    имя, набранное руками, разойдётся с данными на один символ — расчёт даст ноль документов
//    молча. Поэтому «Проверить связь» обязательна, а список организаций приходит из самой базы;
//  * пароль здесь только проезжает. Он не остаётся в состоянии панели дольше отправки, не
//    попадает в журнал и не возвращается с сервера — ни значение, ни путь к секрету;
//  * подключение и первичный скан — ОДНО решение человека (14.11.2026): карточка, вычитка базы
//    и прогон агента-сверщика по границе карточки идут одной кнопкой. Прогон едёт через чат
//    (тред «Скан: …») — там живёт результат, и наружу от первичного скана ничего не уходит
//    (deliver: chat): найденное ждёт слова человека в «Решениях», а не улетает контрагентам.
import { LBL, CARD, head } from "../../core/cards.js";
import { icon } from "../../core/icons.js";

const M = "/api/modules/clients";
const A = "/api/modules/agents";
const CH = "/api/modules/chat";

export async function mount(root, ctx) {
  const { api, toast, confirm, humanError, esc } = ctx;

  // Пароль живёт ТОЛЬКО здесь и только до отправки: в состоянии экрана его нет.
  let реестр = [], схемы = [], проба = null, выбрана = "";
  let сказано = "", вид = "", занято = "", пусто = "";
  // Прогресс скана: идёт он или уже готов. Поллинг живёт до закрытия раздела — по отмене
  // цикл замечает флаг после очередного сна и тихо уходит: прогон продолжается на сервере.
  let скан = null, сканПауза = null, сканОтменён = false;

  const год = new Date().getFullYear();
  const сегодня = new Date().toISOString().slice(0, 10);

  async function загрузить() {
    try {
      const о = await api(M + "/registry?все=1");
      реестр = о.clients || [];
      схемы = о.схемы || [];
      пусто = (о["контроль"] || {})["почему_пусто"] || "";
    } catch (e) {
      реестр = []; пусто = "реестр не загрузился: " + humanError(e);
    }
    render();
  }

  // Агент первичного скана: развёрнутый «Сверщик расхождений» из отдела «Аудит», последний
  // по версии. Имя роли — ростер семьи, не строка на удачу: sverщик объявлен в коде и сеется.
  async function агент_скана() {
    let кат = [];
    try { кат = await api(A + "/catalog"); } catch (e) { return { отказ: "каталог агентов не загрузился: " + humanError(e) }; }
    const живые = (Array.isArray(кат) ? кат : [])
      .filter((a) => String(a.role || "") === "sverщик" && String(a.status || "") === "deployed")
      .sort((x, y) => (y.version || 0) - (x.version || 0));
    if (!живые.length)
      return { отказ: "в отделе «Аудит» нет развёрнутого агента роли «Сверщик расхождений» — разверните его во вкладке «Отделы» и повторите скан" };
    return живые[0];
  }

  // Первичный/повторный скан клиента: вычитка базы → тред чата → прогон в очередь с предметом
  // работы. Все звенья — прокси сайдкара версии 1.0.37+; scope у запуска — 1.0.38.
  async function сканировать(карточка) {
    const агент = await агент_скана();
    if (агент && агент.отказ) return { отказ: агент.отказ };
    занято = "вычитаю базу клиента его же рецептами — до минуты…"; сказано = ""; render();
    try {
      await api(M + "/refresh/" + encodeURIComponent(карточка.id), { method: "POST" });
    } catch (e) { return { отказ: "база не вычиталась: " + humanError(e) }; }
    занято = "ставлю прогон сверки в очередь…"; render();
    let тред;
    try {
      тред = await api(CH + "/threads", { method: "POST",
        body: JSON.stringify({ title: "Скан: " + (карточка["организация"] || карточка["название"] || карточка.id) }) });
    } catch (e) { return { отказ: "тред чата не создался: " + humanError(e) }; }
    const tid = (тред || {}).id;
    if (!tid) return { отказ: "тред чата не создался" };
    let запуск;
    try {
      запуск = await api(CH + "/threads/" + tid + "/run-agent", { method: "POST",
        body: JSON.stringify({ agent_id: агент.id, scope: { "клиент": карточка.id }, deliver: "chat" }) });
    } catch (e) { return { отказ: "прогон не встал в очередь: " + humanError(e) }; }
    if (!запуск || запуск["ok"] === false) return { отказ: (запуск || {}).error || "прогон не встал в очередь" };
    if (запуск.done) return { готово: true, итог: запуск.run, тред: tid };
    const jobId = запуск.job_id;
    if (!jobId) return { готово: true, тред: tid };   // старый сервер без очереди — результат уже в треде
    // Поллинг: run-job на стороне сайдкара сам кэширует и сам пишет карточку в тред по done.
    for (let i = 0; i < 120; i++) {
      await new Promise((r) => { сканПауза = setTimeout(r, 5000); });
      if (сканОтменён) { сканОтменён = false; return { отказ: "скан прерван — раздел закрыли; прогон продолжается в чате" }; }
      сканПауза = null;
      let ст;
      try {
        ст = await api(CH + "/threads/" + tid + "/run-job/" + encodeURIComponent(jobId) + "?agent_id=" + encodeURIComponent(агент.id));
      } catch (e) { continue; }   // разовый сбой связи не убивает скан
      if (ст && ст.done) {
        if (ст["ok"] === false) return { отказ: "скан не выполнился: " + (ст.error || "—"), тред: tid };
        return { готово: true, итог: ст.run, тред: tid };
      }
      занято = "скан идёт: " + (ст && ст.status === "running" ? "считает" : (ст && ст.status === "awaiting_hitl" ? "ждёт подтверждения" : "в очереди")) + "…";
      render();
    }
    return { отказ: "скан ещё не завершился за 10 минут — продолжение в чате «Скан: …»", тред: tid };
  }

  function строка(к) {
    const выключен = String(к["состояние"] || "") === "выключен";
    return `<div style="${CARD};padding:16px;gap:10px;${выключен ? "opacity:.55" : ""}">
      <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">
        <b style="flex:1;min-width:0;font-size:14px">${esc(к["организация"] || к["название"] || "")}</b>
        <span class="chip${выключен ? "" : " on"}">${выключен ? "выключен" : "активен"}</span>
      </div>
      <div style="font-family:var(--mono,monospace);font-size:11px;color:var(--ink-3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(к["адрес"] || "")}</div>
      <div style="display:grid;gap:4px;font-size:12.5px;color:var(--ink-2)">
        <span>схема учёта: ${esc(к["схема_учёта"] || "базовая")}</span>
        <span>период ${esc(к["период_с"] || "—")} — ${esc(к["период_по"] || "—")}</span>
        <span>граница расчёта <span style="font-family:var(--mono,monospace);font-size:11px">${esc(к["система"] || "")}</span></span>
        <span>доступ: ${esc(к["секрет"] || "не задан")}</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn primary" data-act="rescan" data-id="${esc(к.id)}" data-org="${esc(к["организация"] || к["название"] || "")}"
                title="Вычитать базу и поставить прогон сверщика по границе карточки — результат в чате">Пересчитать</button>
        <button class="btn" data-act="refresh" data-id="${esc(к.id)}"
                title="Только перечитать базу этого клиента его же рецептами — до минуты, без прогона">Только вычитать</button>
        <button class="btn" data-act="state" data-id="${esc(к.id)}" data-to="${выключен ? "активен" : "выключен"}"
                title="Удаления нет: прогоны и находки остаются историей клиента">${выключен ? "Включить" : "Выключить"}</button>
      </div></div>`;
  }

  function пробаБлок() {
    if (!проба) return "";
    if (проба["почему"]) {
      return `<div style="${CARD};padding:16px;gap:6px">
        <span style="${LBL}">что сказала база</span>
        <div style="font-size:13px;color:var(--danger-ink)">${esc(проба["почему"])}</div></div>`;
    }
    const орг = проба["организации"] || [], об = проба["объём"] || {};
    const подписи = { "контрагентов": "контрагентов", "документов_поступления": "поступлений",
                      "документов_списания": "списаний" };
    return `<div style="${CARD};padding:16px;gap:10px">
      <span style="${LBL}">организации в этой базе — выберите, чей учёт считаем</span>
      <div style="display:flex;gap:8px;flex-wrap:wrap">${орг.map((о) => `
        <button class="btn${о["название"] === выбрана ? " primary" : ""}" data-act="pick" data-org="${esc(о["название"])}"
                style="display:grid;gap:2px;text-align:left">
          <b style="font-size:13px">${esc(о["название"])}</b>
          <span style="font-family:var(--mono,monospace);font-size:10.5px;opacity:.75">ИНН ${esc(о["инн"] || "—")}</span>
        </button>`).join("")}</div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--ink-2)">
        ${Object.keys(подписи).filter((к) => об[к] !== undefined)
          .map((к) => `<span>${подписи[к]} <b style="font-family:var(--mono,monospace)">${об[к]}</b></span>`).join("")}
      </div>
      <div style="font-size:11.5px;color:var(--ink-3);font-family:var(--mono,monospace)">граница расчёта: ${esc(проба["система"] || "")}</div></div>`;
  }

  function render() {
    const активных = реестр.filter((к) => String(к["состояние"] || "") !== "выключен").length;
    root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
      <div style="max-width:1020px;margin:0 auto;display:flex;flex-direction:column;gap:20px;animation:ape-in .35s ease-out">
        ${head({ "глаз": "реестр", "что": "Клиенты",
                 "действия": [`<span class="chip${активных ? " on" : ""}">обслуживаем ${активных}</span>`],
                 "подпись": "Обслуживаемые компании: где база каждой, в каких границах считаем и по какому устройству учёта. Подключение — одно решение: карточка, вычитка базы и первичный скан одной кнопкой." })}

        <span style="${LBL}">кого обслуживаем</span>
        ${реестр.length
          ? `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px">${реестр.map(строка).join("")}</div>`
          : `<div style="${CARD};padding:16px;font-size:13px;color:var(--ink-2)">${esc(пусто || "в реестре нет ни одного клиента")}</div>`}

        <span style="${LBL}">подключить клиента</span>
        <div style="${CARD};padding:16px;gap:12px">
          <div style="display:grid;gap:5px">
            <label style="${LBL}" for="clAddr">адрес базы 1С</label>
            <input id="clAddr" type="url" style="width:100%" placeholder="https://app.1cbo.ru/a/sbp/315653">
            <span style="font-size:12px;color:var(--ink-3)">как в браузере, вместе с https://</span>
          </div>
          <div style="display:grid;gap:5px">
            <label style="${LBL}" for="clPwd">пароль служебного пользователя OData</label>
            <input id="clPwd" type="password" style="width:100%" autocomplete="new-password">
            <span style="font-size:12px;color:var(--ink-3)">уходит в файл-секрет установки ABOP; обратно не отдаётся и в журнал не пишется</span>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div style="display:grid;gap:5px"><label style="${LBL}" for="clFrom">период с</label>
              <input id="clFrom" type="date" style="width:100%" value="${год}-01-01"></div>
            <div style="display:grid;gap:5px"><label style="${LBL}" for="clTo">период по</label>
              <input id="clTo" type="date" style="width:100%" value="${сегодня}"></div>
          </div>
          <div style="display:grid;gap:5px">
            <label style="${LBL}" for="clScheme">схема учёта</label>
            <select id="clScheme">${схемы.map((с) => `
              <option value="${esc(с["схема"] || "")}">${esc(с["название"] || "базовая")} · правил ${с["правил"] || 0}</option>`).join("")}</select>
            <span style="font-size:12px;color:var(--ink-3)">варианты объявлены наборами правил на сервере: схемы, под которую нет набора, здесь не будет</span>
          </div>
          <div style="display:grid;gap:5px">
            <label style="${LBL}" for="clNote">примечание</label>
            <input id="clNote" type="text" style="width:100%" placeholder="чем этот клиент особенный">
          </div>
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <button id="clProbe" class="btn">${icon("search")} Опросить базу</button>
            <button id="clSave" class="btn primary">${icon("shield")} Подключить и сканировать</button>
          </div>
          <div style="font-size:12px;color:var(--ink-3)">Одна кнопка делает всё: карточку, вычитку базы и первичный прогон сверщика по границе карточки. Результат придёт в чат «Скан: …»; наружу от скана ничего не уходит — найденное ждёт вас в «Решениях».</div>
          ${занято ? `<div style="font-size:13px;color:var(--ink-2)">${esc(занято)}</div>` : ""}
          ${сказано ? `<div style="font-size:13px;color:${вид === "bad" ? "var(--danger-ink)" : "var(--ok-ink)"}">${esc(сказано)}</div>` : ""}
        </div>
        ${пробаБлок()}
        <div style="font-size:12px;color:var(--ink-3)">Пароль служебного пользователя OData ABOP кладёт в файл-секрет своей установки: наружу не отдаётся ни значение, ни путь. В учётную базу ABOP не пишет — все запросы только на чтение.</div>
      </div></div>`;
    привязать();
  }

  function привязать() {
    const знач = (ид) => (root.querySelector("#" + ид) || {}).value || "";

    root.querySelectorAll("[data-act=pick]").forEach((б) => б.onclick = () => {
      выбрана = б.dataset.org; render();
    });

    root.querySelector("#clProbe").onclick = async () => {
      const адрес = знач("clAddr"), пароль = знач("clPwd");
      if (!адрес || !пароль) { сказано = "нужны адрес базы и пароль служебного пользователя"; вид = "bad"; render(); return; }
      занято = "читаю базу…"; сказано = ""; render();
      try {
        const о = await api(M + "/probe", { method: "POST", body: JSON.stringify({ адрес, пароль }) });
        проба = о;
        const орг = о["организации"] || [];
        выбрана = орг.length === 1 ? орг[0]["название"] : "";
        сказано = "связь есть; выберите организацию — и подключайте"; вид = "ok";
      } catch (e) {
        проба = { "почему": humanError(e) };
        сказано = humanError(e); вид = "bad";
      }
      занято = ""; render();
    };

    root.querySelector("#clSave").onclick = async () => {
      const тело = { адрес: знач("clAddr"), пароль: знач("clPwd"), организация: выбрана,
                     период_с: знач("clFrom"), период_по: знач("clTo"),
                     схема_учёта: знач("clScheme"), примечание: знач("clNote") };
      if (!тело.адрес || !тело.пароль) { сказано = "нужны адрес базы и пароль"; вид = "bad"; render(); return; }
      занято = "проверяю связь и записываю карточку…"; сказано = ""; render();
      let сохранён;
      try {
        сохранён = await api(M + "/save", { method: "POST", body: JSON.stringify(тело) });
      } catch (e) {
        сказано = humanError(e); вид = "bad"; занято = ""; render(); return;
      }
      const карточка = сохранён.client || {};
      проба = null; выбрана = "";
      await загрузить();
      скан = await сканировать(карточка);
      if (скан.отказ) { сказано = скан.отказ; вид = "bad"; }
      else {
        const находок = скан.итог && скан.итог["findings_total"];
        сказано = "Клиент подключён и просканирован" + (находок != null ? ": находок " + находок : "")
          + (скан.тред ? " — разбор в чате «Скан: …»" : "") + ". Найденное ждёт в «Решениях».";
        вид = "ok";
        toast("Клиент подключён, скан завершён", "ok");
      }
      занято = ""; скан = null; render();
    };

    root.querySelectorAll("[data-act=rescan]").forEach((б) => б.onclick = async () => {
      занято = "вычитаю базу…"; сказано = ""; render();
      скан = await сканировать({ id: б.dataset.id, "организация": б.dataset.org });
      if (скан.отказ) { сказано = скан.отказ; вид = "bad"; }
      else {
        const находок = скан.итог && скан.итог["findings_total"];
        сказано = "Пересчёт готов" + (находок != null ? ": находок " + находок : "") + (скан.тред ? " — разбор в чате «Скан: …»" : "") + ".";
        вид = "ok";
      }
      занято = ""; скан = null; render();
    });

    root.querySelectorAll("[data-act=refresh]").forEach((б) => б.onclick = async () => {
      занято = "читаю базу клиента — это занимает до минуты…"; сказано = ""; render();
      try {
        const о = await api(M + "/refresh/" + encodeURIComponent(б.dataset.id), { method: "POST" });
        const части = Object.keys(о["рецепты"] || {}).map((р) => р + ": " + (о["рецепты"][р]["записано"] || 0));
        сказано = "данные перечитаны — " + (части.join(", ") || "рецептов не нашлось"); вид = "ok";
      } catch (e) { сказано = humanError(e); вид = "bad"; }
      занято = ""; render();
    });

    root.querySelectorAll("[data-act=state]").forEach((б) => б.onclick = async () => {
      const куда = б.dataset.to;
      if (куда === "выключен" && !(await confirm({
            title: "Выключить клиента?",
            text: "Прогоны и находки останутся его историей — карточка просто перестанет считаться. Включить можно обратно.",
            okLabel: "Выключить" }))) return;
      try {
        await api(M + "/state/" + encodeURIComponent(б.dataset.id), { method: "POST", body: JSON.stringify({ состояние: куда }) });
        toast("Клиент " + куда, "ok");
        await загрузить();
      } catch (e) { toast(humanError(e), "danger"); }
    });
  }

  render();
  await загрузить();
  // Раздел закрыли — поллинг обязан затихнуть: флаг заметит цикл после ближайшего сна.
  return { refresh: async () => {
    if (сканПауза) { clearTimeout(сканПауза); сканПауза = null; сканОтменён = true; }
    await загрузить();
  } };
}
