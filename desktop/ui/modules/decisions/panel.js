// Раздел «Решения» — то, что ждёт слова человека, и место, куда ведёт счётчик в рейле.
//
// До этого решения жили карточкой внутри чата: счётчик в рейле был, а идти по нему было некуда.
// Экран собран по DIZAJN 5.4 и PRD 5.4, и в нём четыре решения, каждое — против удобной неправды:
//
// 1. **Полный предмет решения, а не сводка.** Кому, тема, текст письма целиком. Подтверждать
//    невидимое нельзя: на этом уже спотыкались (UX-аудит 25.09, D-C3).
// 2. **Отказ называется «оставить черновиком».** Он и есть выбор обратимого пути: письмо остаётся
//    готовым и ждёт 30 дней. Слово «отклонить» врало бы о последствии.
// 3. **Уровень и основание на карточке.** Человеку сказано, ПОЧЕМУ спрашивают именно его: «У2 —
//    внешняя коммуникация, в рамках ваших должностных обязанностей».
// 4. **Срок виден, и истёкший срок — не ошибка.** «Решение не принято за N ч» означает, что
//    доставка не состоялась, а черновик лежит. Красным это рисовать нельзя: ничего не сломалось.
const M = "/api/modules/decisions";
const LBL = "font-family:var(--mono);font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-3)";
const BTN = "padding:7px 13px;border-radius:9px;border:1px solid var(--line);background:var(--panel-2);color:var(--ink);font-size:12.5px;cursor:pointer";
const BTN_OK = BTN + ";border-color:var(--ok-line, var(--line));font-weight:600";

export async function mount(root, ctx) {
  const { api, esc, toast, humanError, card, origin, sanitizeHtml, setHitlCount } = ctx;
  let данные = null, err = "", loading = true, busy = "";
  let открыт = "", предмет = null;

  async function load() {
    loading = true; render();
    try {
      данные = await api(M + "/queue?mine=1");
      err = "";
      // Счётчик в рейле и в трее считает СЕРВЕР (сколько адресовано мне), а не длина массива на
      // экране: фильтр и прокрутка не должны менять число ждущих решений.
      if (typeof setHitlCount === "function") setHitlCount((данные["queue"] || []).length);
    } catch (e) { err = humanError(e); данные = null; }
    loading = false; render();
  }

  async function раскрыть(id) {
    if (открыт === id) { открыт = ""; предмет = null; render(); return; }
    открыт = id; предмет = null; render();
    try { предмет = await api(M + "/item/" + encodeURIComponent(id)); }
    catch (e) { предмет = { error: humanError(e) }; }
    render();
  }

  async function решить(id, decision) {
    busy = id; render();
    try {
      // Тело — строкой: `api` отдаёт opts прямо в fetch, и объект уехал бы как «[object Object]».
      await api(M + "/item/" + encodeURIComponent(id) + "/decide",
                { method: "POST", body: JSON.stringify({ decision, reason: "" }) });
      toast(decision === "approve" ? "Отправлено" : "Осталось черновиком");
      открыт = ""; предмет = null;
      await load();
    } catch (e) {
      // Отказ по правам приходит от сервера словами — показываем их, а не «что-то пошло не так».
      toast(humanError(e), "err");
    } finally { busy = ""; render(); }
  }

  function срок(з) {
    if (з["срок_истёк"]) return "срок решения прошёл — доставка не состоялась, черновик лежит";
    const до = Number(з["срок_до"] || 0);
    if (!до) return "без срока: ждёт, сколько потребуется";
    const часов = Math.round((до * 1000 - Date.now()) / 3600000);
    if (часов <= 0) return "срок решения истекает";
    if (часов < 48) return `решение ждёт ещё ${часов} ч`;
    return `решение ждёт ещё ${Math.round(часов / 24)} дн.`;
  }

  function предметHTML(з) {
    if (открыт !== з.id) return "";
    if (!предмет) return `<div style="${LBL}">читаю письмо…</div>`;
    if (предмет.error) return `<div style="font-size:12px;color:var(--danger-ink)">${esc(предмет.error)}</div>`;
    // Поля — те, что ручка действительно отдаёт (`routes/hitl.hitl_item`): `to`, `subject`, `html`,
    // а для команды в систему — `body` и `command_fields`. Первая версия читала `cfg.to`, которого
    // в ответе нет: адресат показывался из списка по счастливой случайности.
    const html = предмет.html || предмет.body || "";
    const поля = (предмет.command_fields || []).map(([k, v]) =>
      `<span style="${LBL}">${esc(k)}: <span style="color:var(--ink-2);text-transform:none">${esc(v)}</span></span>`).join(" · ");
    return `<div style="display:flex;flex-direction:column;gap:6px;padding:10px 12px;border-radius:11px;border:1px solid var(--line);background:var(--field, var(--panel))">
      <div style="${LBL}">что уйдёт — целиком</div>
      <div style="font-size:12px;color:var(--ink-2)">кому: ${esc(предмет.to || з.to_addr || "—")}${предмет.subject ? " · тема: " + esc(предмет.subject) : ""}</div>
      ${поля ? `<div style="display:flex;gap:10px;flex-wrap:wrap">${поля}</div>` : ""}
      <div style="max-height:320px;overflow:auto;font-size:12.5px;line-height:1.5">${sanitizeHtml(html) || "<i>текст не пришёл — подтверждать нечего, откройте прогон</i>"}</div>
    </div>`;
  }

  function карточка(з) {
    const ждёт = busy === з.id;
    const что = з["черновик"] ? з["черновик"] + " готов" : (з.title || з.channel || "решение");
    const кому = з.to_addr ? `уйдёт ${esc(з.to_addr)}` : "адресат не указан";
    const действия = [
      `<button data-open="${esc(з.id)}" style="${BTN}">${открыт === з.id ? "Свернуть" : "Показать целиком"}</button>`,
      `<button data-go="${esc(з.id)}" style="${BTN_OK}" ${ждёт ? "disabled" : ""}>Отправить</button>`,
      `<button data-no="${esc(з.id)}" style="${BTN}" ${ждёт ? "disabled" : ""}>Оставить черновиком</button>`,
      з["срок_истёк"] ? `<span style="${LBL};color:var(--warn-ink)">срок прошёл</span>` : "",
    ];
    return card({
      "что": что,
      "класс": "У" + (з["уровень"] || 2),
      "состояние": з["не_отправлено"] === false ? "отправлено" : "не отправлено",
      "справа": з["addressed_to"] ? "ждёт: " + з["addressed_to"] : "",
      "суть": кому + ". " + (з["основание"] || "решение в рамках ваших обязанностей"),
      "происхождение": срок(з) + (з["requested_by"] ? ` · прогон запросил ${з["requested_by"]}` : ""),
      "действия": действия,
    }) + предметHTML(з);
  }

  function render() {
    const q = (данные || {})["queue"] || [];
    root.innerHTML = `<div style="display:flex;flex-direction:column;gap:var(--gap-group);max-width:860px">
      <div style="display:flex;flex-direction:column;gap:4px">
        <h2 style="margin:0;font-size:20px;font-weight:700">Что ждёт вашего решения</h2>
        ${origin("Решает тот, в чьи обязанности это входит: У2 — исполнитель прогона, У3 — владелец операции в карте процессов. Руководитель здесь не нажимает кнопки за других.")}
      </div>
      ${err ? `<div style="font-size:12.5px;color:var(--danger-ink)">${esc(err)}</div>` : ""}
      ${loading && !данные ? `<div style="${LBL}">читаю очередь…</div>` : ""}
      ${!loading && !err && !q.length ? `<div style="display:flex;flex-direction:column;gap:6px">
        <div style="font-size:13px">Решений нет — ничего не ждёт вас.</div>
        ${origin("Письмо наружу и необратимое действие попадут сюда сами: цепочка при этом дойдёт до конца, а наружу ничего не уйдёт без вашего слова.")}
      </div>` : ""}
      ${q.map(карточка).join("")}
    </div>`;
    root.querySelectorAll("[data-open]").forEach((b) => b.onclick = () => раскрыть(b.dataset.open));
    root.querySelectorAll("[data-go]").forEach((b) => b.onclick = () => решить(b.dataset.go, "approve"));
    root.querySelectorAll("[data-no]").forEach((b) => b.onclick = () => решить(b.dataset.no, "reject"));
  }

  render();
  await load();
  return { refresh: load };
}
