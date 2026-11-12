// Модуль «Клиенты» — реестр обслуживаемых компаний.
//
// У аутсорсера в одной установке сотни обслуживаемых фирм. До 12.11.2026 завести клиента можно
// было только по ssh: переменная с паролем в .env, строка в файле-источнике, пересоздание
// контейнера, прогон рецепта. Этот экран нужен затем, чтобы это делал тот, кто ведёт клиентов.
//
// Два правила здесь взяты из провалов на живых базах, а не из вкусовщины:
//  * организацию человек ВЫБИРАЕТ из базы, а не набирает. Прогон делит данные по этому полю, и
//    имя, набранное руками, разойдётся с данными на один символ — расчёт даст ноль документов
//    молча. Поэтому «Проверить связь» обязательна, а список организаций приходит из самой базы;
//  * пароль здесь только проезжает. Он не остаётся в состоянии панели дольше отправки, не
//    попадает в журнал и не возвращается с сервера — ни значение, ни путь к секрету.
import { LBL, CARD, head } from "../../core/cards.js";
import { icon } from "../../core/icons.js";

const M = "/api/modules/clients";

export async function mount(root, ctx) {
  const { api, toast, confirm, humanError, esc } = ctx;

  // Пароль живёт ТОЛЬКО здесь и только до отправки: в состоянии экрана его нет.
  let реестр = [], схемы = [], проба = null, выбрана = "";
  let сказано = "", вид = "", занято = "", пусто = "";

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
        <button class="btn primary" data-act="refresh" data-id="${esc(к.id)}"
                title="Перечитать базу ЭТОГО клиента его же рецептами — до минуты">Обновить данные</button>
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
                 "подпись": "Обслуживаемые компании: где база каждой, в каких границах считаем и по какому устройству учёта. Подключение клиента — запись в реестре, а не работа с сервером." })}

        <span style="${LBL}">кого обслуживаем</span>
        ${реестр.length
          ? `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px">${реестр.map(строка).join("")}</div>`
          : `<div style="${CARD};padding:16px;font-size:13px;color:var(--ink-2)">${esc(пусто || "в реестре нет ни одного клиента")}</div>`}

        <span style="${LBL}">завести клиента</span>
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
            <button id="clProbe" class="btn">${icon("search")} Проверить связь</button>
            <button id="clSave" class="btn primary">${выбрана ? "Завести «" + esc(выбрана) + "»" : "Завести клиента"}</button>
          </div>
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
        сказано = "связь есть; выберите организацию и заводите"; вид = "ok";
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
      try {
        const о = await api(M + "/save", { method: "POST", body: JSON.stringify(тело) });
        проба = null; выбрана = "";
        сказано = "клиент заведён. " + (о["дальше"] || "Нажмите «Обновить данные»"); вид = "ok";
        занято = ""; await загрузить();
        toast("Клиент заведён", "ok");
      } catch (e) {
        сказано = humanError(e); вид = "bad"; занято = ""; render();
      }
    };

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
}
