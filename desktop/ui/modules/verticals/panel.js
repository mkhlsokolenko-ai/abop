// Раздел «Вертикали» — поставка представления из приложения, а не из консоли.
//
// До этого вертикаль ставили ручкой API: человек, который внедряет, открывал терминал. Здесь он
// видит то же, что видит платформа, и теми же словами: что лежит в поставке, годен ли пакет, чего
// не хватает в системе, установлена ли вертикаль и какая активна.
//
// Четыре правила экрана, и каждое против удобной неправды:
//   1. **негодный пакет не ставится молча** — ошибки сверки манифеста и список «чего нет в
//      системе» показаны до кнопки, а сама кнопка выключена;
//   2. **установка отчитывается за каждый пункт** словом «поставлен / обновлён / пропущен» и
//      причиной: заказчик должен видеть, что именно изменилось в его системе;
//   3. **нулевой прогон — не «обновить»**, а точка отсчёта: сколько записей пришло и какие
//      находки были НА ВХОДЕ. Без этого списка через месяц не отличить «мы нашли и починили» от
//      «оно и так было найдено до нас»;
//   4. **широкий режим — это выбор**, а не состояние «не настроено»: он назван и доступен кнопкой.
import { CARD, LBL, BTN, BTN_OK } from "../../core/cards.js";

const M = "/api/modules/verticals";

export async function mount(root, ctx) {
  const { api, esc, toast, humanError, card, origin } = ctx;
  let данные = null, ошибка = "", занят = "", отчёт = null, база = null, loading = true;

  async function load() {
    loading = true; render();
    try { данные = await api(M + "/list"); ошибка = ""; }
    catch (e) { данные = null; ошибка = humanError(e); }
    loading = false; render();
  }

  async function действие(vid, путь, подпись) {
    занят = vid; отчёт = null; render();
    try {
      const р = await api(`${M}/${encodeURIComponent(vid)}/${путь}`, { method: "POST", body: "{}" });
      отчёт = { вертикаль: vid, что: подпись, ответ: р };
      toast(`${подпись}: готово`);
      await load();
    } catch (e) { toast(humanError(e), "err"); }
    finally { занят = ""; render(); }
  }

  async function показатьБазу(vid) {
    занят = vid; база = null; render();
    try { база = { вертикаль: vid, ответ: await api(`${M}/${encodeURIComponent(vid)}/baseline`) }; }
    catch (e) { toast(humanError(e), "err"); }
    finally { занят = ""; render(); }
  }

  const активна = (vid) => String(((данные || {}).активная || {}).id || "") === String(vid);

  function чегоНет(в) {
    const нет = в["чего_нет"] || {};
    const части = Object.entries(нет).filter(([, v]) => (v || []).length)
      .map(([что, список]) => `${что}: ${(список || []).join(", ")}`);
    return части.join(" · ");
  }

  function карточка(в) {
    const ждёт = занят === в.id;
    const нет = чегоНет(в);
    const ошибки = в["ошибки"] || [];
    const годен = !!в["годен"] && !нет;
    const действия = [];
    if (годен) {
      действия.push(`<button data-install="${esc(в.id)}" style="${BTN_OK}" ${ждёт ? "disabled" : ""}>${
        в["установлена"] ? "Поставить заново" : "Поставить"}</button>`);
    }
    if (в["установлена"]) {
      действия.push(`<button data-zero="${esc(в.id)}" style="${BTN}" ${ждёт ? "disabled" : ""}>Нулевой прогон</button>`);
      действия.push(`<button data-base="${esc(в.id)}" style="${BTN}" ${ждёт ? "disabled" : ""}>Точка отсчёта</button>`);
      if (!активна(в.id)) {
        действия.push(`<button data-act="${esc(в.id)}" style="${BTN}" ${ждёт ? "disabled" : ""}>Сделать активной</button>`);
      }
    }
    const состояние = активна(в.id) ? "активна"
      : (в["установлена"] ? "установлена" : (годен ? "готова к установке" : "не готова"));
    const части = [
      `${(в["наборы"] || []).length} ${(в["наборы"] || []).length === 1 ? "набор правил" : "наборов правил"}`,
      `${(в["рецепты"] || []).length} источников данных`,
      (в["процессы"] || []).length ? `процессы: ${(в["процессы"] || []).join(", ")}` : "",
      в["бланк"] ? `бланк документа: ${в["бланк"]}` : "",
    ].filter(Boolean);
    return card({
      "что": в["название"] || в.id,
      "класс": в["версия"] || "",
      "состояние": состояние,
      "справа": в["область"] || "",
      "суть": String(в["описание"] || "").slice(0, 400),
      "происхождение": части.join(" · "),
      "действия": действия,
    }) + (ошибки.length || нет ? `<div style="${CARD};gap:6px;border-color:var(--warn-line);margin:-6px 0 10px">
      ${ошибки.length ? `<span style="${LBL}">пакет не сходится с манифестом</span>
        ${ошибки.slice(0, 6).map((о) => `<div style="font-size:12px;color:var(--ink-2)">${esc(о)}</div>`).join("")}` : ""}
      ${нет ? `<span style="${LBL}">нет в системе</span>
        <div style="font-size:12px;color:var(--ink-2)">${esc(нет)}</div>
        ${origin("Пакет не привозит чужой код: навыки, адаптеры и сущности ставит вендор. Честный отказ лучше установки, которая «прошла», а прогон падает у заказчика.")}` : ""}
    </div>` : "");
  }

  function отчётHTML() {
    if (!отчёт) return "";
    const о = отчёт.ответ || {};
    const пункты = о["отчёт"] || [];
    const линия = (п) => `<tr>
      <td>${esc(String(п["что"] || п["рецепт"] || ""))}</td>
      <td class="mono" style="font-size:11.5px">${esc(String(п["id"] || п["сущность"] || ""))}</td>
      <td><span class="chip${String(п["итог"]) === "поставлен" ? " on" : ""}">${
        esc(String(п["итог"] || ""))}</span></td>
      <td style="font-size:12px;color:var(--ink-2)">${esc(String(п["почему"] || ""))}${
        п["записано"] !== undefined ? ` записей: ${esc(String(п["записано"]))}` : ""}</td></tr>`;
    const б = о["базовая_линия"] || {};
    return `<div style="${CARD};gap:10px">
      <span style="${LBL}">${esc(отчёт.что)} · ${esc(отчёт.вертикаль)}</span>
      ${пункты.length ? `<div style="overflow-x:auto"><table class="reg"><thead><tr>
        <th>что</th><th>id</th><th>итог</th><th>почему</th></tr></thead>
        <tbody>${пункты.map(линия).join("")}</tbody></table></div>` : ""}
      ${б["когда"] ? `<div style="font-size:12.5px;color:var(--ink-2)">
        Точка отсчёта зафиксирована ${esc(String(б["когда"]))}: записей по источникам — ${
          esc(JSON.stringify(б["сущности"] || {}))}, находок на входе — ${
          esc(String((б["находки_на_входе"] || []).length))}.</div>
        ${origin(String(б["почему"] || ""))}` : ""}
      ${о["агент"] ? `<div style="font-size:12.5px;color:var(--ink-2)">Агент поставлен: <span class="mono">${
        esc(String(о["агент"]))}</span> — посадить его на участок карты процессов нужно отдельно.</div>` : ""}
    </div>`;
  }

  function базаHTML() {
    if (!база) return "";
    const б = (база.ответ || {})["базовая_линия"] || {};
    if (!б["когда"]) {
      return `<div style="${CARD};gap:8px"><span style="${LBL}">точка отсчёта · ${esc(база.вертикаль)}</span>
        <div style="font-size:13px">Нулевой прогон ещё не делался.</div>
        ${origin("Это отсутствие замера, а не отсутствие проблем: сравнивать через месяц будет не с чем.")}</div>`;
    }
    const находки = б["находки_на_входе"] || [];
    return `<div style="${CARD};gap:8px">
      <span style="${LBL}">точка отсчёта · ${esc(база.вертикаль)} · ${esc(String(б["когда"]))}</span>
      <div style="font-size:12.5px;color:var(--ink-2)">Зафиксировал: ${esc(String(б["кто"] || "—"))}.
        Записей по источникам: <span class="mono">${esc(JSON.stringify(б["сущности"] || {}))}</span>.
        Находок на входе: ${находки.length}. Метрики на входе: <span class="mono">${
          esc(JSON.stringify(б["метрики_на_входе"] || {}))}</span>.</div>
      ${origin(String(б["почему"] || ""))}
    </div>`;
  }

  function render() {
    const список = (данные || {}).вертикали || [];
    const активная = (данные || {}).активная || {};
    root.innerHTML = `<div style="display:flex;flex-direction:column;gap:var(--gap-group);max-width:900px">
      <div style="display:flex;flex-direction:column;gap:4px">
        <h2 style="margin:0;font-size:20px;font-weight:700">Вертикали</h2>
        ${origin("Одно ядро — много представлений. Вертикаль это набор данных (процессы, правила, источники, бланк документа, экран), а не отдельная сборка: переключение не требует новой версии приложения.")}
      </div>
      ${ошибка ? `<div style="font-size:12.5px;color:var(--danger-ink)">${esc(ошибка)}</div>` : ""}
      ${loading && !данные ? `<div style="${LBL}">читаю каталог…</div>` : ""}
      ${активная.id ? `<div style="${CARD};gap:6px;border-color:var(--ok-line)">
        <span style="${LBL}">активное представление</span>
        <div style="font-size:13px;font-weight:600">${esc(String(активная["название"] || активная.id))}</div>
        <div style="font-size:12px;color:var(--ink-2)">Разделы: ${
          esc((активная["разделы"] || []).join(", "))}${активная["начальный_экран"]
            ? ` · начальный экран: ${esc(String(активная["начальный_экран"]))}` : ""}</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px">
          <button data-act="-" style="${BTN}">Вернуться в широкий режим</button>
        </div>
        ${origin("Широкий режим — это выбор, а не состояние «не настроено»: в нём доступны все разделы и агенты всех семей.")}
      </div>` : `<div style="${CARD};gap:6px">
        <span style="${LBL}">активное представление</span>
        <div style="font-size:13px">Широкий режим: доступны все разделы.</div>
        ${origin("Узкое представление ставится ниже — оно показывает человеку только то, что нужно его работе, и называет вещи его словами.")}
      </div>`}
      ${отчётHTML()}
      ${базаHTML()}
      ${список.length ? `<div style="display:flex;flex-direction:column;gap:4px;margin-top:4px">
        <h2 style="margin:0;font-size:17px;font-weight:700">Что есть в поставке</h2>
        ${origin("Манифест пакета сверяется с его содержимым: обещанный файл, которого нет, — отказ установки, а не предупреждение.")}
      </div>` : ""}
      ${список.map(карточка).join("")}
      ${!loading && !ошибка && !список.length ? `<div style="${CARD};gap:8px">
        <span style="${LBL}">пусто</span>
        <div style="font-size:13px">В поставке нет ни одного пакета вертикали.</div>
        ${origin("Это отсутствие поставки, а не ошибка: широкий режим работает и без вертикалей.")}
      </div>` : ""}
    </div>`;
    root.querySelectorAll("[data-install]").forEach((b) => b.onclick = () =>
      действие(b.dataset.install, "install", "Установка"));
    root.querySelectorAll("[data-zero]").forEach((b) => b.onclick = () =>
      действие(b.dataset.zero, "zero-scan", "Нулевой прогон"));
    root.querySelectorAll("[data-act]").forEach((b) => b.onclick = () =>
      действие(b.dataset.act, "activate", b.dataset.act === "-" ? "Широкий режим" : "Переключение"));
    root.querySelectorAll("[data-base]").forEach((b) => b.onclick = () =>
      показатьБазу(b.dataset.base));
  }

  await load();
  return { refresh: load };
}
