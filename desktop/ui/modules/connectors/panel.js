// Модуль «Источники» — тонкий клиент ABOP: каталог коннекторов и рецептов Data Plane из ABOP
// (что реально видит агент, ABAC по отделу) + локальные файлы под правами пользователя ОС.
import { LBL, CARD, head } from "../../core/cards.js";
// Значки — из общего набора ядра: панель не заводит своих символов.
import { icon } from "../../core/icons.js";
// Стили — из ядра: шесть разных карточек и пять разных подписей в панелях были
// расхождением, а не выбором (DIZAJN, раздел 2).
const K = "/api/modules/connectors";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function row(icon, title, note, right) {
  return `<div style="display:flex;align-items:center;gap:12px;padding:11px 13px;border-radius:11px;background:var(--hover);border:1px solid var(--line)">
    <span style="font-size:15px">${icon}</span>
    <span style="flex:1;min-width:0;display:flex;flex-direction:column;gap:2px"><span style="font-size:12.5px;font-weight:600">${esc(title)}</span><span style="font-size:11px;color:var(--ink-3)">${esc(note)}</span></span>
    ${right || ""}</div>`;
}

export async function mount(root, ctx) {
  const { api, toast, humanError } = ctx;
  let cat = {}; try { cat = await api(K + "/list"); } catch (e) { cat = { abop_error: humanError(e) }; }
  // Здоровье связей: вопрос «откуда это число» имеет право на короткий ответ. Свежесть берётся
  // из происхождения записей, а не из журнала прогонов: журнал говорит, что рецепт запускали, а
  // происхождение — что данные доехали.
  let здоровье = null, ошибкаЗдоровья = "";
  try { здоровье = await api(K + "/health"); } catch (e) { ошибкаЗдоровья = humanError(e); }

  const свежесть = Object.entries((здоровье || {})["данные"] || {}).map(([сущность, д]) => {
    const часов = Number(д["часов_назад"]);
    const давно = isFinite(часов) && часов > 24;
    const правый = `<span class="chip${давно ? "" : " on"}" title="${esc(д["рецепты"] || "")}">${
      д["последняя_загрузка"] ? esc(д["последняя_загрузка"]) : "без метки"}</span>`;
    return row(давно ? "🕰" : "🟢", сущность,
      `${д["записей"]} записей${isFinite(часов) ? ` · ${часов} ч назад` : ""}${
        д["рецепты"] ? " · " + д["рецепты"] : ""}`, правый);
  }).join("");

  const заглушки = Object.entries((здоровье || {})["заглушки"] || {}).map(([сущность, сколько]) =>
    row("🧪", сущность, `${сколько} записей помечены источником «заглушка» — решение по ним принимать нельзя`,
      `<span class="chip">заглушка</span>`)).join("");

  const системы = ((здоровье || {})["системы"] || []).map((c) =>
    row(c["заглушка"] === "да" ? "🧪" : "🔌", c["система"],
      [c["вид"], c["роль"], c["адрес"], "ключ: " + c["ключ"],
       "отделы: " + c["доступна_отделам"]].filter(Boolean).join(" · "),
      `<span class="chip${c["заглушка"] === "да" ? "" : " on"}">${
        c["заглушка"] === "да" ? "заглушка" : c["egress"] || "—"}</span>`)).join("");

  const abopConns = (cat.connectors || []).map((c) => {
    const ok = c.status !== "blocked";
    const right = ok ? `<span class="chip on">агент видит</span>`
      : `<span class="chip" title="${esc(c.access_reason || "")}">нет доступа</span>`;
    return row("🗄", c.title, c.note, right);
  }).join("");

  const abopRecipes = (cat.recipes || []).map((r) =>
    row("🧬", r.title, [r.entity, r.note].filter(Boolean).join(" · "), `<span class="chip on">рецепт</span>`)
  ).join("");

  const localConns = (cat.local || []).map((c) => row("📄", c.title, c.note, `<span class="chip on">готов</span>`)).join("");

  const abopBlock = cat.abop_error
    ? `<div style="${CARD};gap:8px;border-color:var(--danger-line,rgba(239,68,68,.4))"><span style="${LBL}">ABOP</span><span style="font-size:12.5px;color:var(--danger-ink)">Не удалось получить источники из ABOP: ${esc(cat.abop_error)}</span></div>`
    : `${abopConns ? `<div style="${CARD};gap:12px"><span style="${LBL}">источники ABOP</span>${abopConns}</div>` : ""}
       ${abopRecipes ? `<div style="${CARD};gap:12px"><span style="${LBL}">рецепты ABOP · источник → entity</span>${abopRecipes}</div>` : ""}`;

  root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
    <div style="max-width:1020px;margin:0 auto;display:flex;flex-direction:column;gap:20px;animation:ape-in .35s ease-out">
      ${head({ "глаз": "данные", "что": "Источники",
               "подпись": "Источники и наборы данных приходят из ABOP — это ровно то, что видит агент под вашими правами. Локальные файлы читаются под правами вашей учётной записи в системе." })}

      ${abopBlock || `<div style="${CARD}"><span style="${LBL}">коннекторы</span><span style="font-size:12.5px;color:var(--ink-3)">В ABOP пока нет подключённых коннекторов.</span></div>`}

      ${ошибкаЗдоровья
        ? `<div style="${CARD};gap:8px"><span style="${LBL}">здоровье связей</span><span style="font-size:12.5px;color:var(--ink-3)">Не удалось получить: ${esc(ошибкаЗдоровья)}</span></div>`
        : `${системы ? `<div style="${CARD};gap:12px"><span style="${LBL}">системы установки · роль и доступ</span>${системы}</div>` : ""}
           ${свежесть ? `<div style="${CARD};gap:12px"><span style="${LBL}">когда данные доехали · по происхождению записей, а не по журналу прогонов</span>${свежесть}</div>` : ""}
           ${заглушки ? `<div style="${CARD};gap:12px;border-color:var(--warn-line,rgba(251,191,36,.4))"><span style="${LBL}">вместо источника — заглушка</span>${заглушки}</div>` : ""}`}

      <div style="${CARD};gap:12px"><span style="${LBL}">локальные источники (права ОС)</span>${localConns}</div>

      <div style="${CARD};gap:12px">
        <span style="${LBL}">добавить локальный файл</span>
        <p style="margin:0;font-size:12.5px;color:var(--ink-2)">docx · xlsx · csv · txt · json · md → распознаём и одной кнопкой кладём в контекст чата.</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn primary" id="pickFile">Выбрать файл…</button>
          <input type="file" id="fileIn" accept=".txt,.md,.csv,.json,.docx,.xlsx" style="display:none"/>
        </div>
        <div id="ingRes" style="font-size:12.5px;color:var(--ink-3)"></div>
      </div>

      <div style="${CARD};gap:10px">
        <span style="${LBL}">последние рабочие файлы</span>
        <div id="recent" style="font-size:12.5px;color:var(--ink-3)">Загрузка…</div>
      </div>

      <div style="font-size:12px;color:var(--ink-3)">Внешние системы (CRM, учётные системы, почта) подключает администратор в ABOP, а агент обращается к ним под вашими правами.</div>
    </div></div>`;
  const $ = (id) => root.querySelector("#" + id);

  function showIngested(name, text) {
    $("ingRes").innerHTML = `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><span class="ok-ink">✓ «${esc(name)}» распознан (${text.length} симв.)</span>
      <button class="btn primary sm" id="toChat">${icon("chat")} Добавить в чат</button><button class="btn sm" id="copyTxt">${icon("copy")} копировать</button></div>`;
    $("toChat").onclick = () => ctx.open("chat", { attach: { name, text } });
    $("copyTxt").onclick = (e) => ctx.copy(text, e.currentTarget, "команду");
  }
  async function ingestPath(path) {
    $("ingRes").textContent = "Читаю…";
    let r; try { r = await api(K + "/ingest", { method: "POST", body: JSON.stringify({ path }) }); } catch (e) { r = { ok: false, error: humanError(e) }; }
    if (r.ok) showIngested(r.name, r.text || "");
    else $("ingRes").innerHTML = `<span class="danger-ink">Не удалось: ${esc(r.error === "not_found" ? "файл не найден" : r.error === "empty" ? "в файле нет текста" : r.error)}</span>`;
  }

  // выбор файла: Electron file input даёт .path (полный путь) — читаем под правами ОС
  $("pickFile").onclick = () => $("fileIn").click();
  $("fileIn").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    if (f.path) { await ingestPath(f.path); }
    else { const text = await f.text(); showIngested(f.name, text); }
    e.target.value = "";
  };

  try {
    const rec = await api(K + "/recent");
    $("recent").innerHTML = (rec.files || []).length
      ? rec.files.map((f) => `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)">
          <span style="flex:1;min-width:0;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.name)}</span>
          <button class="rec btn sm" data-p="${esc(f.path)}">Прочитать</button></div>`).join("")
      : `<span>Нет недавних файлов в Documents/Downloads/Desktop.</span>`;
    $("recent").querySelectorAll(".rec").forEach((b) => b.onclick = () => ingestPath(b.dataset.p));
  } catch { $("recent").textContent = "Не удалось получить список."; }
}
