// Модуль «Распознать» (OCR) — локально (RapidOCR), под правами пользователя.
// Перетащи/выбери картинку или скан → текст → «Добавить в чат» (текст попадает в контекст чата). Вёрстка по ДС.
import { LBL, CARD, head } from "../../core/cards.js";
// Значки — из общего набора ядра: панель не заводит своих символов.
import { icon } from "../../core/icons.js";
// Стили — из ядра: шесть разных карточек и пять разных подписей в панелях были
// расхождением, а не выбором (DIZAJN, раздел 2).
const O = "/api/modules/ocr";
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export async function mount(root, ctx) {
  const { api, mascot, toast, humanError } = ctx;
  let st = {}; try { st = await api(O + "/status"); } catch { st = {}; }
  let lastName = "скан";
  const eng = st.ok ? `RapidOCR · ${st.lang}` : "движок недоступен";

  root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
    <div style="max-width:900px;margin:0 auto;display:flex;flex-direction:column;gap:20px;animation:ape-in .35s ease-out">
      ${head({ "глаз": "страница в текст", "что": "Распознать текст",
               // Строка в «метках» экранируется и сама одевается в метку — отдаём текст, не разметку.
               "метки": [eng],
               "подпись": "Картинка или скан → текст. Локально. Ничего не уходит наружу." })}

      <div id="drop" style="${CARD};align-items:center;justify-content:center;gap:12px;min-height:200px;border:2px dashed var(--line-2);cursor:pointer;text-align:center">
        ${mascot("scan", 52)}
        <div style="font-size:14px;font-weight:600;color:var(--ink)">Перетащите изображение или нажмите</div>
        <div style="font-size:12px;color:var(--ink-3)">PNG · JPG · BMP · скан документа → текст попадёт в контекст чата</div>
        <input type="file" id="imgIn" accept="image/*" style="display:none"/>
      </div>

      <div id="preview"></div>
      <div id="result"></div>
    </div></div>`;
  const $ = (id) => root.querySelector("#" + id);

  async function recognizeDataUrl(dataUrl, previewSrc) {
    $("preview").innerHTML = `<div style="${CARD};gap:10px"><span style="${LBL}">исходное изображение</span><img src="${previewSrc}" style="max-width:100%;max-height:260px;border-radius:11px;border:1px solid var(--line)"/></div>`;
    $("result").innerHTML = `<div style="${CARD}"><span style="display:inline-flex;gap:12px;align-items:center">${mascot("thinking", 26)}<span style="color:var(--ink-2)">распознаю…</span></span></div>`;
    let r; try { r = await api(O + "/recognize", { method: "POST", body: JSON.stringify({ data_url: dataUrl }) }); } catch (e) { r = { ok: false, error: humanError(e) }; }
    if (!r.ok) { $("result").innerHTML = `<div style="${CARD}"><span class="danger-ink">Не удалось распознать: ${esc(r.error === "no_input" ? "нет изображения" : r.error)}</span></div>`; return; }
    const note = r.note ? `<span class="chip">${esc(r.note)}</span>` : "";
    $("result").innerHTML = `<div style="${CARD};gap:10px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><span style="${LBL};flex:1">распознанный текст · ${r.chars} симв.</span>${note}
        ${r.text ? `<button id="toChat" class="btn primary sm">${icon("chat")} Добавить в чат</button>` : ""}<button id="copyTxt" class="btn sm">${icon("copy")} копировать</button></div>
      <div style="white-space:pre-wrap;font-size:13px;line-height:1.6;color:var(--ink);max-height:340px;overflow:auto;background:var(--field);border:1px solid var(--line);border-radius:11px;padding:12px">${esc(r.text) || '<span style="color:var(--ink-3)">текст не найден</span>'}</div></div>`;
    if ($("copyTxt")) $("copyTxt").onclick = (e) => ctx.copy(r.text || "", e.currentTarget, "распознанный текст");
    if ($("toChat")) $("toChat").onclick = () => ctx.open("chat", { attach: { name: lastName, text: r.text } });
  }

  function handleFile(f) {
    if (!f || !/^image\//.test(f.type)) { toast("Нужна картинка: PNG, JPG или BMP", "warn"); return; }
    lastName = f.name || "скан";
    const rd = new FileReader();
    rd.onload = () => recognizeDataUrl(rd.result, rd.result);
    rd.readAsDataURL(f);
  }

  $("drop").onclick = () => $("imgIn").click();
  $("imgIn").onchange = (e) => { handleFile(e.target.files[0]); e.target.value = ""; };
  $("drop").addEventListener("dragover", (e) => { e.preventDefault(); $("drop").style.borderColor = "var(--accent-2)"; $("drop").style.background = "var(--accent-bg)"; });
  $("drop").addEventListener("dragleave", () => { $("drop").style.borderColor = "var(--line-2)"; $("drop").style.background = "var(--panel)"; });
  $("drop").addEventListener("drop", (e) => { e.preventDefault(); $("drop").style.borderColor = "var(--line-2)"; $("drop").style.background = "var(--panel)"; handleFile((e.dataTransfer.files || [])[0]); });
}
