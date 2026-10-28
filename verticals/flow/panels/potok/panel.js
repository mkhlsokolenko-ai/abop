// Панель «Поток» — экран вертикали «Поток разработки». Приезжает С ПАКЕТОМ вертикали, а не живёт
// в ядре: у графика и у сверки 1С свои экраны, и держать их все в оболочке значило бы раздувать
// широкий продукт чужими нуждами.
//
// Данные панель не считает и не хранит: всё посчитано кодом платформы в сущности `flow_line`
// (путь задачи, метрики с целями, блокировки с цитатами, показатели качества). Панель читает их
// через платформу (`/api/panel-data/...` — только GET и только разрешённые пути) и показывает.
//
// Три правила этого экрана, и каждое против удобной неправды:
//   1. «хуже цели» — не «плохая команда»: поток свойство системы работы, и подпись об этом стоит
//      рядом с метриками, а не в документации;
//   2. показатель из заглушки подписан заглушкой — решение по нему принимать нельзя;
//   3. пустой экран объясняет себя: «нет данных» и «всё в порядке» выглядят одинаково, если не
//      сказать, чего не хватает.
import { CARD, LBL, kpi } from "../../core/cards.js";

const П = "/api/panel-data/api/data/query/flow_line?limit=400";

export async function mount(root, ctx) {
  const { api, esc, humanError, origin } = ctx;
  let строки = [], ошибка = "";

  try {
    const ответ = await api(П);
    строки = ((ответ && ответ.data && ответ.data.records) || []);
  } catch (e) { ошибка = humanError(e); }

  const поВиду = (вид) => строки.filter((с) => String(с["вид"] || "") === вид);
  const контроль = поВиду("контроль")[0] || {};
  const метрики = поВиду("метрика");
  const блокировки = поВиду("блокировка");
  const задачи = поВиду("задача");
  const качество = поВиду("качество");

  // Ноль — это факт: `||` превратил бы «закрыто 0» в пустоту, и худшее число исчезло бы с экрана.
  const знач = (v) => (v === null || v === undefined || v === "" ? "—" : String(v));

  // Плитка числа — из ядра: панель вертикали говорит тем же языком, что оболочка. Раньше она
  // рисовала её классами `.k` и `.v`, которых в теме нет, — на экране выходили голые строки.
  const плашка = (подпись, значение, тон) => kpi({
    "число": знач(значение), "подпись": подпись, "откуда": "",
    "тон": тон === "warn" ? "ждёт" : тон === "ok" ? "готово" : "",
  });

  const метрикаСтрока = (м) => {
    const хуже = String(м["исход"] || "") === "хуже цели";
    const заглушка = /заглушк/i.test(String(м["источник"] || ""));
    return `<tr>
      <td>${esc(String(м["название"] || ""))}${заглушка
        ? ` <span class="chip" title="решение по такому показателю принимать нельзя">заглушка</span>` : ""}</td>
      <td class="mono" style="text-align:right">${esc(знач(м["факт"]))}</td>
      <td class="mono" style="text-align:right;color:var(--ink-3)">${esc(знач(м["цель"]))}</td>
      <td style="color:var(--ink-3)">${esc(String(м["единица"] || ""))}</td>
      <td><span class="chip${хуже ? "" : " on"}">${esc(String(м["исход"] || "—"))}</span></td>
      <td style="color:var(--ink-3);font-size:11.5px">${esc(String(м["из_чего"] || ""))}</td></tr>`;
  };

  const блокировкаКарточка = (б) => `<div style="${CARD};gap:6px">
      <div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
        <span style="font-size:13px;font-weight:700">${esc(String(б["код"] || "блокировка"))}</span>
        <span class="mono" style="font-size:11.5px;color:var(--ink-2)">задача ${esc(знач(б["задача"]))}</span>
        <span class="chip${String(б["просрочена"]) === "да" ? "" : " on"}">ждёт ${esc(знач(б["ждёт_дней"]))} р.д.${
          String(б["просрочена"]) === "да" ? " · просрочено" : ""}</span>
        <span style="${LBL}">фраза: ${esc(String(б["фраза"] || ""))}</span>
      </div>
      <div style="font-size:12.5px;color:var(--ink-2)">${esc(String(б["тема"] || ""))}</div>
      <div style="font-size:12.5px;padding:8px 10px;border-radius:9px;background:var(--field, var(--panel));border:1px solid var(--line)">
        «${esc(String(б["цитата"] || "цитата не сохранена"))}»</div>
      ${б["подсказка"] ? `<div style="${LBL}">что делать: <span style="text-transform:none;color:var(--ink-2)">${esc(String(б["подсказка"]))}</span></div>` : ""}</div>`;

  const задачаСтрока = (з) => `<tr>
      <td class="mono">${esc(знач(з["задача"]))}</td>
      <td>${esc(String(з["тема"] || ""))}</td>
      <td style="color:var(--ink-3)">${esc(String(з["статус"] || ""))}</td>
      <td class="mono" style="font-size:11.5px">${esc(знач(з["поставлена"]))}</td>
      <td class="mono" style="font-size:11.5px">${esc(знач(з["первый_коммит"]))}</td>
      <td class="mono" style="font-size:11.5px">${esc(знач(з["закрыта"]))}</td>
      <td class="mono" style="text-align:right">${esc(знач(з["прохождение_дней"]))}</td>
      <td class="mono" style="text-align:right">${esc(знач(з["работа_дней"]))}</td>
      <td class="mono" style="text-align:right">${esc(знач(з["ожидание_дней"]))}</td></tr>`;

  const заглушки = качество.filter((к) => /заглушк/i.test(String(к["источник"] || "")));

  root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
    <div style="max-width:1020px;margin:0 auto;display:flex;flex-direction:column;gap:var(--gap-group)">
      <div style="display:flex;flex-direction:column;gap:6px">
        <h1 style="margin:0;font-size:26px;font-weight:800;letter-spacing:-.7px">Поток разработки</h1>
        ${origin("От постановки задачи до коммита в главной ветке. Статус задачи и срок её прохождения — разные вещи: статус можно переставить, дату постановки и дату коммита нельзя. Цели метрик объявил владелец потока в данных.")}
      </div>

      ${ошибка ? `<div style="${CARD};border-color:var(--danger-line)"><span style="${LBL}">данные</span>
        <span style="font-size:12.5px;color:var(--danger-ink)">${esc(ошибка)}</span></div>` : ""}

      ${!ошибка && !строки.length ? `<div style="${CARD};gap:8px">
        <span style="${LBL}">пусто</span>
        <div style="font-size:13px">Расчёт потока ещё не делался.</div>
        ${origin("Это отсутствие прогона, а не отсутствие проблем: прогоните рецепты вертикали («Нулевой прогон» в разделе источников) и запустите агента потока.")}
      </div>` : ""}

      ${строки.length ? `<div class="kpis">
        ${плашка("Задач", контроль["задач"])}
        ${плашка("Закрыто", контроль["закрыто"])}
        ${плашка("В работе", контроль["в_работе"],
                 Number(контроль["в_работе"]) > 3 ? "warn" : "ok")}
        ${плашка("Блокировок", контроль["блокировок"],
                 Number(контроль["блокировок"]) > 0 ? "warn" : "ok")}
        ${плашка("Метрик хуже цели", контроль["метрик_хуже_цели"],
                 Number(контроль["метрик_хуже_цели"]) > 0 ? "warn" : "ok")}
        ${плашка("Из заглушки", контроль["заглушек"])}
      </div>` : ""}

      ${метрики.length ? `<div style="${CARD};gap:10px">
        <span style="${LBL}">метрики потока · факт против цели</span>
        <div style="overflow-x:auto"><table class="reg"><thead><tr>
          <th>метрика</th><th style="text-align:right">факт</th><th style="text-align:right">цель</th>
          <th>ед.</th><th>исход</th><th>из чего посчитано</th></tr></thead>
          <tbody>${метрики.map(метрикаСтрока).join("")}</tbody></table></div>
        <div class="dcard-note">«Хуже цели» не означает «плохая команда»: поток — свойство системы работы. Цель и окно расчёта — решение владельца потока, объявленное в данных.</div>
      </div>` : ""}

      ${блокировки.length ? `<div style="display:flex;flex-direction:column;gap:var(--gap-item)">
        <span style="${LBL}">что стоит в ожидании · ${блокировки.length}</span>
        ${блокировки.map(блокировкаКарточка).join("")}
      </div>` : ""}

      ${задачи.length ? `<div style="${CARD};gap:10px">
        <span style="${LBL}">путь задач · путь минус работа и есть ожидание</span>
        <div style="overflow-x:auto"><table class="reg"><thead><tr>
          <th>задача</th><th>тема</th><th>статус</th><th>поставлена</th><th>1-й коммит</th>
          <th>закрыта</th><th style="text-align:right">путь</th>
          <th style="text-align:right">работа</th><th style="text-align:right">ожидание</th>
        </tr></thead><tbody>${задачи.map(задачаСтрока).join("")}</tbody></table></div>
      </div>` : ""}

      ${заглушки.length ? `<div style="${CARD};gap:8px;border-color:var(--warn-line)">
        <span style="${LBL}">показатели из заглушки</span>
        ${заглушки.map((к) => `<div style="font-size:12.5px;color:var(--ink-2)">${
          esc(String(к["название"] || ""))}: <span class="mono">${esc(знач(к["факт"]))}</span> — источник «${
          esc(String(к["источник"]))}», решение по нему принимать нельзя</div>`).join("")}
      </div>` : ""}
    </div></div>`;

  return {
    refresh: async () => {
      const узел = root;
      узел.innerHTML = "";
      await mount(узел, ctx);
    },
  };
}
