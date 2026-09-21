// Модуль «Распознать» (OCR) — локально (RapidOCR), под правами пользователя.
// Перетащи/выбери картинку или скан → текст → опционально в знания треда (RAG). Вёрстка по ДС.
const O = "/api/modules/ocr";
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const LBL = "font-family:var(--mono);font-size:9.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3)";
const CARD = "padding:20px;border-radius:16px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:14px";

export async function mount(root, ctx) {
  const { api, mascot } = ctx;
  let st = {}; try { st = await api(O + "/status"); } catch {}
  const eng = st.ok ? `RapidOCR · ${st.lang}` : "движок недоступен";

  root.innerHTML = `<div style="flex:1;min-width:0;overflow-y:auto;padding:26px 30px">
    <div style="max-width:900px;margin:0 auto;display:flex;flex-direction:column;gap:20px;animation:ape-in .35s ease-out">
      <div style="display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap">
        <div style="flex:1 1 340px;display:flex;flex-direction:column;gap:6px">
          <h1 style="margin:0;font-size:26px;font-weight:800;letter-spacing:-.7px">Распознать текст</h1>
          <p style="margin:0;font-size:13px;color:var(--ink-2)">Картинка или скан → текст. Локально, ${eng}. Ничего не уходит наружу.</p>
        </div>
        <label style="display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--ink-2)"><input type="checkbox" id="toKb" checked/> добавить в знания треда</label>
      </div>

      <div id="drop" style="${CARD};align-items:center;justify-content:center;gap:12px;min-height:200px;border:2px dashed var(--line-2);cursor:pointer;text-align:center">
        ${mascot("scan", 52)}
        <div style="font-size:14px;font-weight:600;color:var(--ink)">Перетащите изображение или нажмите</div>
        <div style="font-size:12px;color:var(--ink-3)">PNG · JPG · BMP · скан документа</div>
        <input type="file" id="imgIn" accept="image/*" style="display:none"/>
      </div>

      <div id="preview"></div>
      <div id="result"></div>
    </div></div>`;
  const $ = (id) => root.querySelector("#" + id);

  async function recognizeDataUrl(dataUrl, previewSrc) {
    $("preview").innerHTML = `<div style="${CARD};gap:10px"><span style="${LBL}">исходное изображение</span><img src="${previewSrc}" style="max-width:100%;max-height:260px;border-radius:11px;border:1px solid var(--line)"/></div>`;
    $("result").innerHTML = `<div style="${CARD}"><span style="display:inline-flex;gap:12px;align-items:center">${mascot("thinking", 26)}<span style="color:var(--ink-2)">распознаю…</span></span></div>`;
    const r = await api(O + "/recognize", { method: "POST", body: JSON.stringify({ data_url: dataUrl, to_knowledge: $("toKb").checked }) });
    if (!r.ok) { $("result").innerHTML = `<div style="${CARD};color:var(--danger-ink)">Ошибка: ${esc(r.error)}</div>`; return; }
    const note = r.indexed ? `<span class="chip on">✓ в знаниях (${r.indexed} фр.)</span>` : (r.note ? `<span class="chip">${esc(r.note)}</span>` : "");
    $("result").innerHTML = `<div style="${CARD};gap:10px">
      <div style="display:flex;align-items:center;gap:10px"><span style="${LBL};flex:1">распознанный текст · ${r.chars} симв.</span>${note}
        <button id="copyTxt" class="btn sm">⧉ копировать</button></div>
      <div style="white-space:pre-wrap;font-size:13px;line-height:1.6;color:var(--ink);max-height:340px;overflow:auto;background:var(--field);border:1px solid var(--line);border-radius:11px;padding:12px">${esc(r.text) || '<span style="color:var(--ink-3)">текст не найден</span>'}</div></div>`;
    if ($("copyTxt")) $("copyTxt").onclick = () => { navigator.clipboard.writeText(r.text); $("copyTxt").textContent = "✓"; };
  }

  function handleFile(f) {
    if (!f || !/^image\//.test(f.type)) return;
    const rd = new FileReader();
    rd.onload = () => recognizeDataUrl(rd.result, rd.result);
    rd.readAsDataURL(f);
  }

  $("drop").onclick = () => $("imgIn").click();
  $("imgIn").onchange = (e) => { handleFile(e.target.files[0]); e.target.value = ""; };
  $("drop").addEventListener("dragover", (e) => { e.preventDefault(); $("drop").style.borderColor = "#818cf8"; $("drop").style.background = "var(--accent-bg)"; });
  $("drop").addEventListener("dragleave", () => { $("drop").style.borderColor = "var(--line-2)"; $("drop").style.background = "var(--panel)"; });
  $("drop").addEventListener("drop", (e) => { e.preventDefault(); $("drop").style.borderColor = "var(--line-2)"; $("drop").style.background = "var(--panel)"; handleFile((e.dataTransfer.files || [])[0]); });
}
