// ABOP Shell — канва-центр (ADR-030): конверт-рамка сверху, палитра-навигация слева,
// центр = экран/канва. Вход — «Загрузка из Люды» (не Обзор), пока контракт не принят.

var NAV = [
  { id: "ingress", label: "Загрузка из Люды", always: true },
  { id: "canvas", label: "Канва · Строю", needsContract: true },
  { id: "runs", label: "Пульт · Запускаю", needsContract: true },
  { id: "ops", label: "Эксплуатация", needsContract: true },
];

var EnvelopeFrame = function ({ contract }) {
  // Конверт всегда виден (ABOP_SCREENS §0): потолок автономии ≤ контракт, egress, HITL, статус.
  var ok = !!contract;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "var(--s-3)", padding: "8px 20px",
      borderBottom: "1px solid var(--surface-line)", background: "var(--surface)", flexWrap: "wrap",
      fontFamily: "var(--font-mono)", fontSize: "var(--fs-11)" }}>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6, color: ok ? "var(--ok)" : "var(--text-3)" }}>
        <Dot color={ok ? "var(--ok)" : "var(--text-mute)"} /> конверт
      </span>
      <span style={{ color: "var(--text-2)" }}>автономия ≤ {contract ? contract.autonomy : "—"}</span>
      <span style={{ color: "var(--text-3)" }}>· egress: {contract ? "по контракту" : "—"}</span>
      <span style={{ color: "var(--text-3)" }}>· HITL: {contract ? (contract.hitl || 0) : "—"}</span>
      <span style={{ marginLeft: "auto", color: ok ? "var(--ok)" : "var(--text-mute)" }}>{ok ? "🟢 контракт LUDA принят" : "ожидает контракт LUDA"}</span>
    </div>
  );
};

var App = function () {
  var [view, setView] = React.useState("ingress");
  var [contract, setContract] = React.useState(null);  // {audit_id, autonomy, hitl, skills}

  var openContract = function (audit_id) {
    fetch("/api/contracts/" + encodeURIComponent(audit_id)).then(function (r) { return r.json(); })
      .then(function (cs) {
        var ci = cs.intake || {};
        setContract({ audit_id: cs.audit_id, autonomy: cs.autonomy || ci.autonomy_ceiling,
          hitl: (ci.hitl_points || []).length, skills: ci.skills || [] });
        setView("canvas");
      }).catch(function () {});
  };

  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}>
      <EnvelopeFrame contract={contract} />
      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        {/* Палитра-навигация */}
        <nav style={{ width: 216, flex: "none", borderRight: "1px solid var(--surface-line)",
          padding: "var(--s-4) var(--s-3)", display: "flex", flexDirection: "column", gap: 4 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "0 8px var(--s-4)" }}>
            <span style={{ width: 22, height: 22, borderRadius: "22.5%", background: "var(--brand-grad)", display: "inline-block" }} />
            <span style={{ fontWeight: "var(--fw-bold)", letterSpacing: "var(--tracking-tight)" }}>ABOP</span>
          </div>
          {NAV.map(function (n) {
            var locked = n.needsContract && !contract;
            var active = view === n.id;
            return (
              <button key={n.id} disabled={locked} onClick={function () { if (!locked) setView(n.id); }}
                style={{ textAlign: "left", padding: "9px 12px", borderRadius: "var(--r-md)", border: "none",
                  cursor: locked ? "not-allowed" : "pointer", fontSize: "var(--fs-13)", fontWeight: "var(--fw-medium)",
                  background: active ? "var(--brand-050)" : "transparent",
                  color: active ? "#fff" : locked ? "var(--text-mute)" : "var(--text-2)",
                  borderLeft: active ? "2px solid var(--brand)" : "2px solid transparent" }}>
                {n.label}{locked ? " 🔒" : ""}
              </button>
            );
          })}
          <div style={{ marginTop: "auto", padding: "var(--s-3) 8px 0", display: "flex", alignItems: "center", gap: 8 }}>
            <Ape state={contract ? "idle" : "idle"} size={28} />
            <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-11)", color: "var(--text-3)" }}>Эйп · оператор</span>
          </div>
        </nav>

        {/* Центр */}
        <main style={{ flex: 1, minWidth: 0, overflow: "auto", padding: "var(--s-6) var(--s-5)" }}>
          {view === "ingress" ? <Ingress onLoaded={openContract} />
            : view === "canvas" ? <Canvas auditId={contract.audit_id} />
            : <div style={{ maxWidth: 960, margin: "0 auto", color: "var(--text-3)" }}>
                <Glass>Экран «{(NAV.find(function (n) { return n.id === view; }) || {}).label}» — перенос из дизайна v3 (следующий шаг).</Glass>
              </div>}
        </main>
      </div>
    </div>
  );
};

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
