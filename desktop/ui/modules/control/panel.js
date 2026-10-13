// Раздел «Лента контроля» — рабочее место руководителя. Смотреть свершившееся и управлять
// полномочиями; подтверждать чужие действия здесь НЕЛЬЗЯ, и это не недоделка.
//
// Четыре решения экрана (PRD 5.1, DIZAJN 5.5):
//
// 1. **Аномалии первыми.** Не «всё, что было», а то, на что стоит посмотреть: новый адресат,
//    нерабочее время, всплеск объёма, решение не адресатом. Пустой список — нормальное состояние,
//    и он так и подписан, иначе человек решит, что данные не пришли.
// 2. **Группировка по людям, а не по времени.** Вопрос руководителя звучит «что делают мои люди»,
//    а не «что было в 14:12».
// 3. **Единственное действие — снять разрешение.** Кнопка «отозвать письмо» была бы ложью:
//    отправленное не вернуть. Подпись к экрану говорит это прямо.
// 4. **Снятие объяснено последствием.** «Человек сможет привязать канал снова» — это не запрет
//    навсегда, и прятать разницу значит обещать больше, чем происходит.
import { LBL, BTN } from "../../core/cards.js";
// Стили — из ядра: шесть разных карточек и пять разных подписей в панелях были
// расхождением, а не выбором (DIZAJN, раздел 2).
const M = "/api/modules/control";

// Вид аномалии — словом и знаком, не цветом: «нерабочее время» не ошибка, а повод посмотреть.
const ЗНАК = { "новый адресат": "◆", "нерабочее время": "◷", "всплеск объёма": "▲",
               "решил не адресат": "◇" };

export async function mount(root, ctx) {
  const { api, esc, toast, humanError, card, origin, confirm } = ctx;
  let данные = null, err = "", loading = true, дней = 7, busy = "";

  async function load() {
    loading = true; render();
    try { данные = await api(M + "/feed?days=" + дней); err = ""; }
    catch (e) { err = humanError(e); данные = null; }
    loading = false; render();
  }

  async function снять(id, кто) {
    const ок = await confirm({
      title: "Снять разрешение?",
      kicker: кто || "",
      text: "Привязка канала будет снята: агенты этого человека перестанут запускаться из "
            + "переписки. Это не запрет навсегда — он сможет привязать канал снова.",
      okLabel: "Снять", danger: true,
    });
    if (!ок) return;
    busy = id; render();
    try {
      const r = await api(M + "/permissions/" + encodeURIComponent(id), { method: "DELETE" });
      toast(r["что_дальше"] || "Разрешение снято", "ok");
      await load();
    } catch (e) { toast(humanError(e), "err"); }
    finally { busy = ""; render(); }
  }

  function когда(x) {
    if (!x) return "—";
    const t = Date.parse(x);
    if (Number.isNaN(t)) return String(x).slice(0, 16).replace("T", " ");
    return new Date(t).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
  }

  function аномалииHTML() {
    const a = (данные || {})["аномалии"] || [];
    if (!a.length) {
      return card({
        "что": "Аномалий нет",
        "суть": "За выбранный период ничего не выбивается из обычной работы людей.",
        "происхождение": (данные || {})["чем_посчитано"] || "",
      });
    }
    const строки = a.slice(0, 20).map((x) => `<div style="display:flex;gap:9px;align-items:baseline;padding:4px 0;border-bottom:1px solid var(--line)">
      <span style="width:14px;color:var(--warn-ink)">${esc(ЗНАК[x["вид"]] || "•")}</span>
      <span style="${LBL};min-width:130px">${esc(x["вид"])}</span>
      <b style="font-weight:650;min-width:110px">${esc(x["кто"] || "—")}</b>
      <span style="font-size:12.5px;color:var(--ink-2);flex:1">${esc(x["что"] || "")}</span>
      <span style="font-size:11.5px;color:var(--ink-3)">${esc(когда(x["когда"]))}</span>
    </div>`).join("");
    return card({
      "что": "Требует внимания", "состояние": a.length + " из обычного ряда",
      "суть": "Это не нарушения, а отклонения: посмотреть стоит с них.",
      "происхождение": (данные || {})["чем_посчитано"] || "",
      "действия": [`<div style="width:100%">${строки}</div>`],
    });
  }

  function человекHTML(г) {
    const строки = (г["строки"] || []).map((с) => `<div style="display:flex;gap:9px;align-items:baseline;font-size:12px;padding:3px 0">
      <span style="color:var(--ink-3);width:14px">→</span>
      <span style="flex:1">${esc(с["кому"] || "—")}</span>
      <span style="color:var(--ink-3)">${esc(с["канал"] || "")}${с["уровень"] ? " · У" + esc(с["уровень"]) : ""}</span>
      <span style="color:var(--ink-2)">${esc(с["агент"] || "")}</span>
      <span style="color:var(--ink-3);font-family:var(--mono)">${esc(когда(с["когда"]))}</span>
    </div>`).join("");
    return card({
      "что": г["кто"], "состояние": "ушло наружу: " + г["всего"],
      "справа": г["адресатов"] + " адресатов",
      "суть": "Подтвердил сам — в рамках своих обязанностей.",
      "происхождение": "строки журнала решений; отправленное не вернуть",
      "действия": [`<div style="width:100%">${строки}</div>`],
    });
  }

  function разрешенияHTML() {
    const р = (данные || {})["разрешения"] || [];
    const строки = р.map((x) => `<div style="display:flex;gap:10px;align-items:center;font-size:12.5px;padding:5px 0;border-bottom:1px solid var(--line)">
      <b style="font-weight:650;min-width:120px">${esc(x["кто"] || "—")}</b>
      <span style="${LBL}">${esc(x["канал"] || "")}</span>
      <span style="color:var(--ink-3)">выдано ${esc(когда(x["создана"]))}</span>
      <span style="color:var(--ink-3)">последнее ${esc(когда(x["последнее"]))}</span>
      <button data-off="${esc(x.id)}" data-кто="${esc(x["кто"] || "")}" style="${BTN};margin-left:auto" ${busy === x.id ? "disabled" : ""}>Снять разрешение</button>
    </div>`).join("");
    return card({
      "что": "Постоянные разрешения", "состояние": р.length ? р.length + " действует" : "нет",
      "суть": р.length
        ? "Привязки каналов: после них агенты человека запускаются из переписки и готовят письма коллегам."
        : "Никто не выдавал себе постоянных разрешений.",
      "происхождение": (данные || {})["авто_правил_нет"] || "",
      "действия": строки ? [`<div style="width:100%">${строки}</div>`] : [],
    });
  }

  function недошлоHTML() {
    const н = (данные || {})["не_дошло"] || [];
    if (!н.length) {
      return card({ "что": "Всё дошло", "суть": "Отказов и просроченных решений за период нет.",
                    "происхождение": "журнал решений" });
    }
    const строки = н.slice(0, 25).map((с) => `<div style="display:flex;gap:9px;align-items:baseline;font-size:12px;padding:4px 0;border-bottom:1px solid var(--line)">
      <span style="${LBL};min-width:96px">${esc(с["состояние"])}</span>
      <span style="min-width:150px">${esc(с["кому"] || "—")}</span>
      <span style="color:var(--ink-2);min-width:110px">${esc(с["чей"] || "—")}</span>
      <span style="color:var(--ink-3);flex:1">${esc(с["почему"] || "")}</span>
      <span style="color:var(--ink-3);font-family:var(--mono)">${esc(когда(с["когда"]))}</span>
    </div>`).join("");
    return card({
      "что": "Не дошло", "состояние": н.length + " строк",
      "суть": "Где работа встала: отказы и решения, не принятые в срок.",
      "происхождение": "черновики при этом не потеряны — они лежат в «Результатах» 30 дней",
      "действия": [`<div style="width:100%">${строки}</div>`],
    });
  }

  function render() {
    const d = данные || {};
    const окна = [7, 14, 30].map((n) => `<button data-days="${n}" style="${BTN}${n === дней ? ";border-color:var(--line-2);font-weight:650" : ""}">${n} дн.</button>`).join("");
    root.innerHTML = `<div style="display:flex;flex-direction:column;gap:var(--gap-group);max-width:980px;padding:24px 26px;overflow:auto">
      <div style="display:flex;flex-direction:column;gap:5px">
        <h2 style="margin:0;font-size:20px;font-weight:700">Что происходит в отделе</h2>
        ${origin(d["подпись"] || "")}
      </div>
      <div style="display:flex;gap:7px;align-items:center">${окна}
        <span style="${LBL};margin-left:auto">отдел: ${esc(d["отдел"] || "—")}</span></div>
      ${err ? `<div style="font-size:12.5px;color:var(--danger-ink)">${esc(err)}</div>` : ""}
      ${loading && !данные ? `<div style="${LBL}">читаю журнал…</div>` : ""}
      ${данные ? аномалииHTML() : ""}
      ${данные ? недошлоHTML() : ""}
      ${данные ? ((d["наружу"] || []).length
        ? (d["наружу"] || []).map(человекHTML).join("")
        : card({ "что": "Наружу ничего не уходило", "суть": "За период внешних отправок не было.",
                 "происхождение": "журнал решений" })) : ""}
      ${данные ? разрешенияHTML() : ""}
    </div>`;
    root.querySelectorAll("[data-days]").forEach((b) => b.onclick = () => { дней = Number(b.dataset.days); load(); });
    root.querySelectorAll("[data-off]").forEach((b) => b.onclick = () => снять(b.dataset.off, b.dataset["кто"]));
  }

  render();
  await load();
  return { refresh: load };
}
