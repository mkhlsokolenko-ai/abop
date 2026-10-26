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

export const S_CARD = "display:flex;flex-direction:column;gap:var(--gap-item);padding:var(--pad-card) var(--pad-card-2);border-radius:14px;border:1px solid var(--line);background:var(--panel)";
export const S_LBL = "font-family:var(--mono);font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-3)";
export const S_NUM = "font-family:var(--mono);font-size:17px;font-weight:700;line-height:1.1";
export const S_ORIGIN = "font-size:11px;color:var(--ink-3);line-height:1.45";

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
export const INP = "padding:7px 10px;border-radius:9px;border:1px solid var(--line);background:var(--field);color:var(--ink);font-size:12.5px";
export const BTN = "padding:7px 13px;border-radius:9px;border:1px solid var(--line-2);background:var(--panel-2);color:var(--ink);font-size:12.5px;font-weight:600;cursor:pointer";
// Утвердительное действие — заливка, а не подкрашенная рамка.
//
// `BTN_OK` отличался от обычной кнопки ОДНИМ оттенком рамки, и на тёмном фоне «Отправить» в
// решениях выглядело как серый текст: владелец искал на экране кнопку подтверждения и не нашёл.
// Необратимое действие обязано быть самым заметным элементом карточки — иначе человек либо не
// найдёт его, либо нажмёт соседнее. Цвет берётся из токенов темы (`--ok-solid`), а не литералом:
// в светлой теме это тёмно-зелёный с белым текстом, и оба проходят AA (tests/test_desktop_contrast).
export const BTN_OK = "padding:7px 13px;border-radius:9px;border:1px solid transparent;background:var(--ok-solid);color:var(--ok-solid-ink);font-size:12.5px;font-weight:700;cursor:pointer;box-shadow:0 10px 26px var(--ok-glow)";
// Строка списка: высота из токена, а не из воздуха вокруг текста. Двенадцать строк на экране
// 1366×768 вместо восьми — критерий из раздела 9 того же документа.
export const ROW = "display:flex;align-items:center;gap:10px;min-height:var(--row-h);padding:0 var(--pad-card);font-size:12.5px;border-bottom:1px solid var(--line)";

// Плитка числа: само число и подпись, что оно значит. Числа моноширинные — колонки выравниваются
// по разряду, и «1 240» не выглядит длиннее «840».
export function tile(число, подпись) {
  return `<div style="display:flex;flex-direction:column;gap:2px;min-width:92px">
    <span style="${S_NUM}">${esc(число)}</span><span style="${S_LBL}">${esc(подпись)}</span></div>`;
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
const ТОН = {
  "ждёт": "border-color:var(--warn-line);background:var(--warn-bg)",
  "опасно": "border-color:var(--danger-line);background:var(--danger-bg)",
  "готово": "border-color:var(--ok-line);background:var(--ok-bg)",
};

export function card(o = {}) {
  const ч = Array.isArray(o["числа"]) ? o["числа"].filter((x) => Array.isArray(x) && x.length) : [];
  const нет_происхождения = ч.length > 0 && !o["происхождение"];
  const метки = [o["класс"], o["состояние"]].filter(Boolean)
    .map((t) => `<span style="${S_LBL};padding:2px 7px;border:1px solid var(--line);border-radius:999px">${esc(t)}</span>`).join("");
  const действия = (o["действия"] || []).filter(Boolean).join("");
  const тон = ТОН[String(o["тон"] || "")] || "";
  return `<div style="${S_CARD}${тон ? ";" + тон : ""}">
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <span style="font-size:13.5px;font-weight:650">${esc(o["что"] || "")}</span>
      ${метки}
      ${o["справа"] ? `<span style="${S_LBL};margin-left:auto">${esc(o["справа"])}</span>` : ""}
    </div>
    ${o["суть"] ? `<div style="font-size:12.5px;line-height:1.45;color:var(--ink-2)">${esc(o["суть"])}</div>` : ""}
    ${ч.length ? `<div style="display:flex;gap:var(--gap-group);flex-wrap:wrap">${ч.map(([n, p]) => tile(n, p)).join("")}</div>` : ""}
    ${origin(o["происхождение"])}
    ${нет_происхождения ? `<div style="${S_ORIGIN};color:var(--warn-ink)">число без происхождения — карточка собрана неполно</div>` : ""}
    ${действия ? `<div style="display:flex;gap:var(--gap-item);flex-wrap:wrap;align-items:center">${действия}</div>` : ""}
  </div>`;
}
