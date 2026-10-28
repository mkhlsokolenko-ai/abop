// Примитивы карточки: экранирование, плитка числа, строка происхождения, карточка.
//
// Почему отдельным файлом, а не в `app.js`. Разметка карточки жила в каждой панели своей копией
// (`const CARD = "..."` в пяти файлах), и одно и то же место выглядело по-разному. Вынести её в
// ядро — половина дела; вторая половина в том, что **правило числа** («ни одного числа без второй
// строки, чем оно посчитано», DIZAJN раздел 4) должно быть ПРОВЕРЯЕМЫМ.
//
// В `app.js` правило проверить нечем: этот модуль — оболочка приложения, он трогает DOM на импорте,
// и запустить его в тесте без браузера нельзя. Первая версия сторожа искала в тексте `app.js` имя
// переменной — и мутация, обнулившая само условие (`= false`), прошла незамеченной: проверка
// смотрела на наличие кода, а не на поведение. Здесь функции чистые, и тест вызывает их на самом
// деле (`tests/test_desktop_decisions.py`).
//
// Пять частей карточки в неизменном порядке: шапка, суть, числа, происхождение, действия.

export function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Карточка: стекло с градиентом, кромкой света и тенью — так она выглядит в макетах. Плоский
// прямоугольник без кромки отличает «дорогую» поверхность от дешёвой сильнее, чем кажется:
// именно из-за него живые экраны читались иначе, чем концепты.
export const S_CARD = "display:flex;flex-direction:column;gap:var(--gap-item);padding:var(--pad-card) var(--pad-card-2);border-radius:var(--r-md);border:1px solid var(--line);background-color:var(--panel);background-image:var(--panel-grad);box-shadow:var(--shadow-1),var(--edge)";
export const S_LBL = "font-family:var(--mono);font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-3)";
// Надзаголовок раздела: тот же моношрифт, но акцентным цветом — он отвечает на вопрос «где я»,
// а подпись поля (S_LBL) на вопрос «что это». Разные вопросы — разный цвет.
export const S_EYE = "display:block;font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--accent-2)";
export const S_NUM = "font-family:var(--mono);font-size:17px;font-weight:700;line-height:1.1";
export const S_ORIGIN = "font-size:11px;color:var(--ink-3);line-height:1.45";
// Метка: тот же рецепт, что класс `.tag` в теме — панели вставляют её инлайном, и вид обязан
// совпадать до пикселя, иначе одна и та же метка в двух разделах будет разной.
export const S_TAG = "display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);font-size:10.5px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;padding:4px 8px;border-radius:999px;border:1px solid var(--line);color:var(--ink-3);white-space:nowrap";

// ── общие стили панелей: ОДИН источник на приложение ───────────────────────────────────────────
//
// До этого каждая панель держала свои `const CARD/LBL/INP/BTN`, и они разошлись: шесть разных
// карточек (отступ 15, 16, 18, 20 px) и пять разных подписей (9.5, 10, 10.5, 11 px, три варианта
// межбуквенного интервала). Одно и то же место в двух разделах выглядело по-разному — а это ровно
// то свойство корпоративного интерфейса, которого у нас не было: предсказуемость.
//
// Значения — из `docs/DIZAJN_DESKTOPA_ENTERPRISE.md`, раздел 2: отступ карточки 12/16, строка
// списка 32 px, основной текст 12.5 px. Через токены темы, а не числами: плотность меняется в
// одном месте, и «компактного режима» второй настройкой не будет.
export const CARD = S_CARD;
export const LBL = S_LBL;
export const EYE = S_EYE;
export const TAG = S_TAG;
export const INP = "padding:7px 10px;border-radius:9px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px";
export const BTN = "display:inline-flex;align-items:center;gap:7px;padding:7px 13px;border-radius:9px;border:1px solid var(--line-2);background:var(--panel-2);color:var(--ink);font-size:12.5px;font-weight:600;cursor:pointer";
// Тихая кнопка: отказ, отмена, «потом». Отличается от обычной тем, что не спорит за внимание —
// до этого отказ выглядел ровно как согласие, и выбирать приходилось чтением подписи.
export const BTN_GHOST = "display:inline-flex;align-items:center;gap:7px;padding:7px 13px;border-radius:9px;border:1px solid var(--line);background:transparent;color:var(--ink-2);font-size:12.5px;font-weight:600;cursor:pointer";
// Утвердительное действие — заливка, а не подкрашенная рамка.
//
// `BTN_OK` отличался от обычной кнопки ОДНИМ оттенком рамки, и на тёмном фоне «Отправить» в
// решениях выглядело как серый текст: владелец искал на экране кнопку подтверждения и не нашёл.
// Необратимое действие обязано быть самым заметным элементом карточки — иначе человек либо не
// найдёт его, либо нажмёт соседнее. Цвет берётся из токенов темы (`--ok-solid`), а не литералом:
// в светлой теме это тёмно-зелёный с белым текстом, и оба проходят AA (tests/test_desktop_contrast).
export const BTN_OK = "display:inline-flex;align-items:center;gap:7px;padding:7px 13px;border-radius:9px;border:1px solid transparent;background:var(--ok-solid);color:var(--ok-solid-ink);font-size:12.5px;font-weight:700;cursor:pointer;box-shadow:0 10px 26px var(--ok-glow)";
// Строка списка: высота из токена, а не из воздуха вокруг текста. Двенадцать строк на экране
// 1366×768 вместо восьми — критерий из раздела 9 того же документа.
export const ROW = "display:flex;align-items:center;gap:10px;min-height:var(--row-h);padding:0 var(--pad-card);font-size:12.5px;border-bottom:1px solid var(--line)";

// Плитка числа: само число и подпись, что оно значит. Числа моноширинные — колонки выравниваются
// по разряду, и «1 240» не выглядит длиннее «840».
export function tile(число, подпись) {
  return `<div style="display:flex;flex-direction:column;gap:2px;min-width:92px">
    <span style="${S_NUM}">${esc(число)}</span><span style="${S_LBL}">${esc(подпись)}</span></div>`;
}

/**
 * Большая плитка числа — та, что в макетах стоит в шапке экрана: {число, подпись, откуда, тон}.
 *
 * `откуда` обязательно: правило числа действует и здесь — на этих числах человек принимает
 * решения, и «поверьте на слово» не годится. Пустое `откуда` рисуется честной пометкой, а не
 * молча опускается: так же, как карточка помечает число без происхождения.
 *
 * `тон`: «» · «ждёт» (тёплая подложка) · «готово» · «деньги» (акцентное число).
 */
export function kpi(o = {}) {
  const тон = String(o["тон"] || "");
  const класс = тон === "ждёт" ? " alert" : тон === "готово" ? " good" : тон === "деньги" ? " money" : "";
  const откуда = String(o["откуда"] || "");
  return `<div class="kpi${класс}">
    <b>${esc(o["число"])}</b>
    <span class="cap">${esc(o["подпись"] || "")}</span>
    <span class="sub">${esc(откуда || "чем посчитано — не объявлено")}</span>
  </div>`;
}

/** Ряд плиток: одна сетка на все экраны, чтобы шапки разделов не расходились по ширине колонок. */
export function kpis(список) {
  const плитки = (список || []).filter(Boolean);
  if (!плитки.length) return "";
  return `<div class="kpis">${плитки.map((п) => (typeof п === "string" ? п : kpi(п))).join("")}</div>`;
}

/**
 * Шапка раздела: надзаголовок, название, метки и действия справа.
 *
 * Собиралась в каждой панели заново — отсюда и разнобой в размерах заголовка и в том, где стоят
 * кнопки. {глаз, что, метки:[], действия:[], подпись}
 */
export function head(o = {}) {
  // «Метки» — это ТЕКСТ: строка экранируется и сама одевается в метку. Готовая разметка идёт в
  // «действия». Перепутать легко (28.10 я перепутал дважды за один заход), а расплата некрасивая:
  // на экран выезжает «<span class="chip">…» буквами. Поэтому не молчим.
  const метки = (o["метки"] || []).filter(Boolean)
    .map((м) => {
      if (typeof м !== "string") return м;
      if (м.indexOf("<") >= 0) {
        console.warn("[head] в «метках» пришла разметка — она будет показана текстом: " + м.slice(0, 60));
      }
      return `<span style="${S_TAG}">${esc(м)}</span>`;
    }).join("");
  const действия = (o["действия"] || []).filter(Boolean).join("");
  return `<div style="display:flex;flex-direction:column;gap:6px;min-width:0">
    ${o["глаз"] ? `<span style="${S_EYE}">${esc(o["глаз"])}</span>` : ""}
    <div style="display:flex;align-items:center;gap:11px;flex-wrap:wrap;min-width:0">
      <h2 style="margin:0;font-size:20px;font-weight:800;letter-spacing:-.5px">${esc(o["что"] || "")}</h2>
      ${метки}
      ${действия ? `<span style="margin-left:auto;display:flex;gap:7px;align-items:center;flex-wrap:wrap">${действия}</span>` : ""}
    </div>
    ${o["подпись"] ? `<div style="${S_ORIGIN}">${esc(o["подпись"])}</div>` : ""}
  </div>`;
}

// Строка происхождения — наше отличие от продуктов, которые показывают только результат. Должна
// быть видна так же, как видны часы и рубли: не в подсказке и не под катом.
export function origin(текст) {
  return текст ? `<div style="${S_ORIGIN}">${esc(текст)}</div>` : "";
}

/**
 * Карточка. {что, класс, состояние, справа, суть, числа:[[число,подпись]], происхождение, действия}
 *
 * Карточка с числами и БЕЗ происхождения не притворяется полной: на экран уходит явная пометка,
 * что число ничем не объяснено. Это и делает правило числа механизмом, а не пожеланием.
 */
// Тон карточки: по умолчанию обычная панель, «ждёт» — тёплая подложка ожидания. Это не
// украшение: карточка, которая держит необратимое действие, должна отличаться от сводки с
// первого взгляда, а не читаться одинаково со всем остальным списком.
// Тон карточки — класс темы, а не набор инлайновых цветов: в макетах это «.card.top» (требует
// человека), «.card.warn» (ждёт) и спокойная «.card.calm». Цвет, подобранный в примитиве, стал бы
// четвёртым определением того же состояния.
const КЛАСС_ТОНА = {
  "ждёт": "warn",
  "опасно": "warn",
  "готово": "calm",
  "спокойно": "calm",
  "важное": "top",
};

export function card(o = {}) {
  const ч = Array.isArray(o["числа"]) ? o["числа"].filter((x) => Array.isArray(x) && x.length) : [];
  const нет_происхождения = ч.length > 0 && !o["происхождение"];
  // Разметка — из макетов (28.10.2026): шапка карточки с метками, объяснение, доказательство,
  // ряд действий. Рецепты живут в теме (`.card`, `.card-head`, `.why`, `.ev`, `.acts`), а здесь
  // только подстановка данных. Пока карточку собирает один примитив, экраны разойтись не могут.
  // Тяжесть красит метку: в макете «высокая» — красная, «средняя» — жёлтая. Тон приходит полем,
  // а не угадывается по тексту: слова у вертикалей свои, и разбирать их в ядре значит знать их.
  const метки = [[o["класс"], o["класс_тон"]], [o["состояние"], ""]]
    .filter(([т]) => т)
    .map(([т, тон]) => `<span class="tag${тон ? " " + тон : ""}">${esc(т)}</span>`).join("");
  const действия = (o["действия"] || []).filter(Boolean).join("");
  const тон = КЛАСС_ТОНА[String(o["тон"] || "")] || "";
  return `<div class="card${тон ? " " + тон : ""}">
    <div class="card-head">
      ${метки}
      <span class="t">${esc(o["что"] || "")}</span>
      ${o["справа"] ? `<span class="tag right">${esc(o["справа"])}</span>` : ""}
    </div>
    ${o["суть"] ? `<div class="why">${esc(o["суть"])}</div>` : ""}
    ${o["почему"] ? `<div class="why"><b>${esc(o["почему_лид"] || "Почему это здесь:")}</b> ${esc(o["почему"])}</div>` : ""}
    ${o["доказательство"] ? `<div class="ev">${esc(o["доказательство"])}</div>` : ""}
    ${ч.length ? `<div style="display:flex;gap:var(--gap-group);flex-wrap:wrap">${ч.map(([n, p]) => tile(n, p)).join("")}</div>` : ""}
    ${origin(o["происхождение"])}
    ${нет_происхождения ? `<div class="origin" style="color:var(--warn-ink)">число без происхождения — карточка собрана неполно</div>` : ""}
    ${действия ? `<div class="acts">${действия}</div>` : ""}
  </div>`;
}
