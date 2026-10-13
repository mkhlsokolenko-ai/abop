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
export function card(o = {}) {
  const ч = Array.isArray(o["числа"]) ? o["числа"].filter((x) => Array.isArray(x) && x.length) : [];
  const нет_происхождения = ч.length > 0 && !o["происхождение"];
  const метки = [o["класс"], o["состояние"]].filter(Boolean)
    .map((t) => `<span style="${S_LBL};padding:2px 7px;border:1px solid var(--line);border-radius:999px">${esc(t)}</span>`).join("");
  const действия = (o["действия"] || []).filter(Boolean).join("");
  return `<div style="${S_CARD}">
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
