// Раздел «Вертикали» — витрина поставок: что это, кому, что внутри и что изменится.
//
// До этого вертикаль ставили ручкой API: человек, который внедряет, открывал терминал. Потом
// появился список со строками состояния — поставить стало можно, но решать «ставить или нет»
// по-прежнему приходилось по названию. Владелец о макетах (27.10.2026): «нужны карточки
// вертикалей с модальными окнами — что в поставке, на что влияет, как на любом маркетплейсе
// приложений».
//
// Четыре правила экрана, и каждое против удобной неправды:
//   1. **карточка отвечает на «зачем»**, а не на «как называется»: первая строка — что человек
//      получит, и только потом — чем пакет это делает;
//   2. **«на что влияет» — обязательная часть окна**: установка меняет разделы, слова и состав
//      проверок, и это главный вопрос покупателя, а не приложение к составу;
//   3. **чего не хватает — до кнопки**, а не после попытки: установка «с сюрпризом» хуже отказа,
//      поэтому негодный пакет называет причину, а кнопка выключена;
//   4. **зависимость видна как связь, а не как ошибка**: «требует: Аудит 1С — установлена» — это
//      нормальный порядок вещей, а не поломка.
//
// Нулевой прогон остаётся точкой отсчёта, а широкий режим — выбором, а не состоянием «не
// настроено»: обе мысли переехали сюда без изменений.
import { CARD, LBL, TAG, BTN, BTN_GHOST, BTN_OK, head } from "../../core/cards.js";
import { icon } from "../../core/icons.js";

const M = "/api/modules/verticals";

export async function mount(root, ctx) {
  const { api, esc, toast, humanError, origin, modal } = ctx;
  // Включение представления делает ОБОЛОЧКА: она пересобирает рейл, слова и начальный экран.
  // Раньше раздел звал ручку сайдкара сам — пакет становился активным, а на экране не менялось
  // ничего до перезапуска, и выглядело это как «кнопка не работает».
  const включить = ctx["включитьПредставление"];
  let данные = null, ошибка = "", занят = "", отчёт = null, база = null, loading = true;
  let вкладка = "состав";

  async function load() {
    loading = true; render();
    try { данные = await api(M + "/list"); ошибка = ""; }
    catch (e) { данные = null; ошибка = humanError(e); }
    loading = false; render();
  }

  async function действие(vid, путь, подпись) {
    занят = vid; отчёт = null; render();
    try {
      if (путь === "activate" && typeof включить === "function") {
        // Через оболочку: иначе представление включится, а экран останется прежним.
        const имя = await включить(vid === "-" ? "" : vid);
        toast("Представление: " + имя, "ok");
        await load();
        return;
      }
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
  const пакет = (vid) => ((данные || {}).вертикали || []).find((в) => String(в.id) === String(vid));

  function чегоНет(в) {
    const нет = в["чего_нет"] || {};
    return Object.entries(нет).filter(([, v]) => (v || []).length)
      .map(([что, список]) => `${что}: ${(список || []).join(", ")}`);
  }

  // Состояние пакета одним словом — тем же, что и в витрине приложений: «активна» значит, что
  // человек прямо сейчас видит её разделы, а не просто что она стоит.
  function состояние(в) {
    if (активна(в.id)) return { текст: "активна", цвет: "var(--ok-ink)" };
    if (в["установлена"]) return { текст: "установлена", цвет: "var(--ink-3)" };
    if (в["годен"]) return { текст: "годна к установке", цвет: "var(--warn-ink)" };
    const нет = чегоНет(в);
    return { текст: нет.length ? "не хватает в системе" : "пакет не сходится", цвет: "var(--danger-ink)" };
  }

  // ── карточка витрины ─────────────────────────────────────────────────────────────────────
  function карточка(в) {
    const с = состояние(в);
    const нужна = ((в["требует"] || {})["вертикали"] || []);
    const метки = [
      (в["процессы"] || []).length ? `процесс: ${(в["процессы"] || []).join(", ")}` : "",
      нужна.length ? `требует: ${нужна.join(", ")}` : "",
      в["бланк"] ? `бланк: ${в["бланк"]}` : "",
    ].filter(Boolean);
    const первая = чегоНет(в)[0] || (в["ошибки"] || [])[0] || "";
    return `<article style="${CARD};gap:10px;min-width:0">
      <div style="display:flex;gap:11px;align-items:center;min-width:0">
        <span style="width:38px;height:38px;flex:none;border-radius:11px;display:grid;place-items:center;background:var(--accent-bg);border:1px solid var(--line-2);color:var(--accent-ink)">${icon("abop", "lg")}</span>
        <span style="min-width:0;display:flex;flex-direction:column;gap:2px">
          <span style="font-size:15px;font-weight:700;letter-spacing:-.2px">${esc(в["название"] || в.id)}</span>
          <span style="${LBL}">${esc(в["автор"] || "ABOP")} · ${(в["рецепты"] || []).length} ${
            (в["рецепты"] || []).length === 1 ? "рецепт" : "рецептов"}</span>
        </span>
      </div>
      <div style="font-size:12.5px;color:var(--ink-2);line-height:1.5">${esc(String(в["описание"] || в["область"] || "").slice(0, 220))}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">${метки.map((м) =>
        `<span style="${TAG}">${esc(м)}</span>`).join("")}</div>
      ${первая ? `<div style="font-size:11.5px;color:var(--warn-ink)">${esc(первая)}</div>` : ""}
      <div style="display:flex;align-items:center;gap:8px;margin-top:2px;flex-wrap:wrap">
        <span style="font:600 11px/1 var(--mono);color:${с.цвет}">● ${esc(с.текст)}</span>
        <span style="margin-left:auto;display:flex;gap:8px;flex-wrap:wrap">
          ${в["установлена"] && !активна(в.id)
            ? `<button data-act="${esc(в.id)}" style="${BTN_OK}">${icon("play")}Включить представление</button>` : ""}
          ${активна(в.id) ? `<button data-act="-" style="${BTN_GHOST}">Вернуться в широкий режим</button>` : ""}
          <button data-open="${esc(в.id)}" style="${в["годен"] && !в["установлена"] ? BTN_OK : BTN}">${
            в["установлена"] ? "Что в поставке" : "Посмотреть"}</button>
        </span>
      </div>
    </article>`;
  }

  // ── окно пакета ──────────────────────────────────────────────────────────────────────────
  function составHTML(в) {
    const пункты = [
      ["правила", (в["наборы"] || []).map((н) => `${н.id} — ${н["правил"]} ${
        н["правил"] === 1 ? "правило" : "правил"}`).join(" · "),
       "проверки расчёта: код находки, серьёзность и подсказка правятся методологом без релиза"],
      ["рецепты", (в["рецепты"] || []).map((р) => `${р.id} → ${р["сущность"]}`).join(" · "),
       "откуда берутся данные и во что они превращаются"],
      ["данные", (в["данные"] || []).map((д) => д["файл"] + (д["есть"] ? "" : " — ФАЙЛА НЕТ")).join(" · "),
       "нормативы, пороги и фразы-признаки: значения, а не код"],
      ["бланк", в["бланк"] || "", "состав разделов документа задан формой, а не тем, что нашлось"],
      ["экран", ((в["оболочка"] || {})["панели"] || []).map((п) => п.title || п.id).join(" · "),
       "панель приезжает пакетом и живёт, пока вертикаль активна"],
      ["агент", (в["контракт"] || {})["семья"] ? `семья «${(в["контракт"] || {})["семья"]}»` : "",
       "контракт автономии: необратимое — только через человека"],
    ].filter(([, значение]) => String(значение || "").trim());
    return пункты.map(([что, значение, зачем]) => `<div style="display:grid;grid-template-columns:92px 1fr;gap:10px;padding:8px 0;border-top:1px solid var(--line)">
      <span style="${LBL}">${esc(что)}</span>
      <span style="min-width:0"><span style="font-size:12.5px">${esc(значение)}</span>
        <span style="display:block;font-size:11px;color:var(--ink-3);margin-top:2px">${esc(зачем)}</span></span>
    </div>`).join("");
  }

  function влияниеHTML(в) {
    const об = в["оболочка"] || {};
    const разделы = (об["разделы"] || []).join(", ");
    const термины = Object.entries(об["термины"] || {}).map(([k, v]) => `«${k}» → «${v}»`).join(" · ");
    const правил = (в["наборы"] || []).reduce((н, x) => н + Number(x["правил"] || 0), 0);
    const сущности = [...new Set((в["рецепты"] || []).map((р) => р["сущность"]).filter(Boolean))];
    const строки = [
      ["разделы", разделы ? `в рейле станет видно: ${разделы}${об["начальный_экран"]
        ? ` · начальный экран: ${об["начальный_экран"]}` : ""}` : "рейл не меняется — пакет не объявляет своих разделов"],
      ["слова", термины || "термины остаются общими"],
      ["расчёт", правил ? `+${правил} ${правил === 1 ? "проверка" : "проверок"} в процессах: ${
        (в["процессы"] || []).join(", ") || "—"}` : "новых проверок пакет не приносит"],
      ["данные", сущности.length ? `наполняются сущности: ${сущности.join(", ")}` : "новых источников нет"],
      ["что не меняется", "ничего не удаляется и не перезаписывается: пакет только добавляет. "
        + "Снять вертикаль можно в любой момент — данные и находки остаются"],
    ];
    return строки.map(([k, v]) => `<div style="display:grid;grid-template-columns:92px 1fr;gap:10px;padding:8px 0;border-top:1px solid var(--line)">
      <span style="${LBL}">${esc(k)}</span><span style="font-size:12.5px;color:var(--ink-2)">${esc(v)}</span></div>`).join("");
  }

  function нуженHTML(в) {
    const нет = чегоНет(в), ошибки = в["ошибки"] || [];
    const нужна = ((в["требует"] || {})["вертикали"] || []).map((id) => {
      const б = пакет(id);
      return { id, стоит: !!(б && б["установлена"]) };
    });
    return `<div style="display:flex;flex-direction:column;gap:10px">
      ${нужна.length ? `<div style="${CARD};gap:6px;background:var(--panel)">
        <span style="${LBL}">зависимость</span>
        ${нужна.map((н) => `<div style="font-size:12.5px;display:flex;align-items:center;gap:7px">
          <span style="color:${н.стоит ? "var(--ok-ink)" : "var(--warn-ink)"}">${icon(н.стоит ? "check" : "alert")}</span>
          вертикаль «${esc(н.id)}» — ${н.стоит ? "установлена" : "не установлена: поставьте её первой"}</div>`).join("")}
      </div>` : ""}
      ${нет.length ? `<div style="${CARD};gap:6px;border-color:var(--warn-line);background:var(--warn-bg)">
        <span style="${LBL};color:var(--warn-ink)">чего не хватает в системе</span>
        ${нет.map((н) => `<div style="font-size:12.5px;color:var(--ink-2)">${esc(н)}</div>`).join("")}
        <div style="font-size:11px;color:var(--ink-3);line-height:1.5">Пакет не привозит чужой код: навыки, адаптеры и сущности ставит вендор. Честный отказ лучше установки, которая «прошла», а прогон падает у заказчика.</div>
      </div>` : ""}
      ${ошибки.length ? `<div style="${CARD};gap:6px;border-color:var(--danger-line)">
        <span style="${LBL};color:var(--danger-ink)">манифест не сходится с содержимым</span>
        ${ошибки.slice(0, 8).map((о) => `<div style="font-size:12px;color:var(--ink-2)">${esc(о)}</div>`).join("")}
      </div>` : ""}
      ${!нет.length && !ошибки.length ? `<div style="${CARD};gap:6px;border-color:var(--ok-line)">
        <span style="${LBL};color:var(--ok-ink)">всё на месте</span>
        <div style="font-size:12.5px;color:var(--ink-2)">Навыки, адаптеры и сущности, которые просит пакет, в системе есть. Манифест сходится с содержимым.</div>
      </div>` : ""}
      <div style="font-size:11.5px;color:var(--ink-3);line-height:1.55">После установки: нулевой прогон фиксирует точку отсчёта — сколько записей пришло и какие находки были НА ВХОДЕ. Без него через месяц не отличить «мы нашли и починили» от «оно и так было найдено до нас».</div>
    </div>`;
  }

  function окноПакета(vid) {
    const в = пакет(vid); if (!в) return;
    вкладка = "состав";
    const вкладки = [["состав", "Что в поставке"], ["влияние", "На что влияет"], ["нужно", "Что нужно"]];
    const тело = () => `<div style="display:flex;flex-direction:column;gap:12px">
      <div style="display:flex;gap:6px;flex-wrap:wrap">${вкладки.map(([id, т]) =>
        `<button data-tab="${id}" class="chip${вкладка === id ? " on" : ""}">${esc(т)}</button>`).join("")}</div>
      <div id="vBody">${вкладка === "состав" ? составHTML(в)
        : вкладка === "влияние" ? влияниеHTML(в) : нуженHTML(в)}</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding-top:6px;border-top:1px solid var(--line)">
        ${в["годен"] ? `<button data-do="install" style="${BTN_OK}">${icon("download")}${
          в["установлена"] ? "Поставить заново" : "Установить"}</button>` : ""}
        ${в["установлена"] ? `<button data-do="zero-scan" style="${BTN}">${icon("play")}Нулевой прогон</button>` : ""}
        ${в["установлена"] && !активна(в.id) ? `<button data-do="activate" style="${BTN}">Сделать активной</button>` : ""}
        ${в["установлена"] ? `<button data-do="baseline" style="${BTN_GHOST}">Точка отсчёта</button>` : ""}
      </div>
    </div>`;
    const ov = modal(в["название"] || в.id, тело(), null, null,
                     { width: "640px", kicker: "вертикаль · " + (в["версия"] || "без версии") });
    const подвязать = () => {
      ov.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => {
        вкладка = b.dataset.tab;
        ov.querySelector("#mBody").innerHTML = тело();
        подвязать();
      });
      ov.querySelectorAll("[data-do]").forEach((b) => b.onclick = () => {
        const что = b.dataset.do;
        ov.close(false);
        if (что === "baseline") показатьБазу(в.id);
        else действие(в.id, что, что === "install" ? "Установка"
                      : что === "zero-scan" ? "Нулевой прогон" : "Переключение");
      });
    };
    подвязать();
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
      ${отчёт.что === "Установка" && !активна(отчёт.вертикаль) ? `<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding-top:6px;border-top:1px solid var(--line)">
        <span style="font-size:12.5px;color:var(--ink-2)">Пакет установлен. Чтобы увидеть его разделы и слова, включите представление — это обратимо.</span>
        <button data-act="${esc(отчёт.вертикаль)}" style="${BTN_OK};margin-left:auto">${icon("play")}Включить представление</button>
      </div>` : ""}
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
    const поставлено = список.filter((в) => в["установлена"]).length;
    root.innerHTML = `<div style="display:flex;flex-direction:column;gap:var(--gap-group);max-width:1040px">
      ${head({ "глаз": "поставка", "что": "Вертикали",
               "метки": [`установлено: ${поставлено}`, `доступно: ${список.length}`],
               "подпись": "Вертикаль — это пакет: процессы, правила, рецепты на ваши системы, бланк документа, экран и контракт автономии. Установка меняет и то, что видят люди, и то, что считает расчёт, — поэтому до кнопки показано и то, и другое. Установка и включение — разные шаги: поставленный пакет ничего не меняет на экране, пока вы не включите его представление (кнопка на карточке или переключатель в шапке приложения)." })}
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
        ${origin("Узкое представление показывает человеку только то, что нужно его работе, и называет вещи его словами.")}
      </div>`}
      ${отчётHTML()}
      ${базаHTML()}
      ${список.length ? `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px">
        ${список.map(карточка).join("")}</div>` : ""}
      ${!loading && !ошибка && !список.length ? `<div style="${CARD};gap:8px">
        <span style="${LBL}">пусто</span>
        <div style="font-size:13px">В поставке нет ни одного пакета вертикали.</div>
        ${origin("Это отсутствие поставки, а не ошибка: широкий режим работает и без вертикалей.")}
      </div>` : ""}
    </div>`;
    root.querySelectorAll("[data-open]").forEach((b) => b.onclick = () => окноПакета(b.dataset.open));
    root.querySelectorAll("[data-act]").forEach((b) => b.onclick = () =>
      действие(b.dataset.act, "activate", b.dataset.act === "-" ? "Широкий режим" : "Переключение"));
  }

  await load();
  return { refresh: load };
}
