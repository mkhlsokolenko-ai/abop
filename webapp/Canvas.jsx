// Канва «Строю» — посев из принятого ContractSet (ADR-030, SDD §4.4).
// Узлы-навыки из CapabilityRequest; потолок автономии из DeploymentContract (ADR-013,
// жёсткий инвариант — слайдер заперт выше потолка); требуемый навык без покрытия
// каталогом ABOP = пробел-предупреждение. Данные: /api/contracts/{id} + /api/skills.

var A_LEVELS = ["A0", "A1", "A2", "A3", "A4"];
var A_IDX = function (a) { return Math.max(0, A_LEVELS.indexOf(a)); };

// цвет узла по egress навыка (permission-scoping видимо, DESIGN_KIT §6.8)
var egressTone = function (safety) {
  if (!safety) return "neutral";
  if (safety.egress === "external") return "warn";      // наружу → HITL
  if (safety.mode === "action") return "warn";
  return "brand";
};

var Canvas = function ({ auditId }) {
  var [cs, setCs] = React.useState(null);        // полный ContractSet
  var [catalog, setCatalog] = React.useState(null); // id → skill (safety/scope)
  var [nodes, setNodes] = React.useState([]);     // посеянные узлы
  var [sel, setSel] = React.useState(null);       // выбранный узел id
  var [err, setErr] = React.useState(null);

  React.useEffect(function () {
    Promise.all([
      fetch("/api/contracts/" + encodeURIComponent(auditId)).then(function (r) { if (!r.ok) throw new Error("контракт не найден"); return r.json(); }),
      fetch("/api/skills").then(function (r) { return r.json(); }),
    ]).then(function (res) {
      var contractSet = res[0], cat = {};
      (res[1].skills || []).forEach(function (s) { cat[s.id] = s; });
      setCs(contractSet); setCatalog(cat);
      setNodes(seed(contractSet, cat));
    }).catch(function (e) { setErr(String(e.message || e)); });
  }, [auditId]);

  // Посев: источник → узлы-навыки (покрытые/пробелы) → выход. Потолок = autonomy_ceiling.
  function seed(contractSet, cat) {
    var ci = contractSet.intake || {};
    var ceiling = ci.autonomy_ceiling || "A0";
    var out = [];
    var mod = (ci.modules || [])[0];
    out.push({ id: "src", kind: "source", title: mod ? mod.id : "Источник данных",
      sub: mod ? ("доступ: " + (mod.access || "read_only")) : "из CapabilityRequest.modules", col: 0 });
    (ci.skills || []).forEach(function (sid, i) {
      var hit = cat[sid];
      out.push({ id: "sk:" + sid, kind: hit ? "skill" : "gap", title: sid,
        safety: hit ? hit.safety : null, catalogTitle: hit ? hit.title : null,
        autonomy: A_LEVELS[Math.min(1, A_IDX(ceiling))], // старт: A1 или потолок, что меньше
        col: 1 + Math.floor(i / 3) });
    });
    var maxCol = out.reduce(function (m, n) { return Math.max(m, n.col); }, 1);
    out.push({ id: "out", kind: "output", title: "Результат", sub: "выход процесса", col: maxCol + 1 });
    return out;
  }

  if (err) return <div style={{ maxWidth: 960, margin: "0 auto" }}><Glass style={{ borderColor: "var(--crit-line)" }}>{err}</Glass></div>;
  if (!cs) return <div style={{ color: "var(--text-3)", padding: "var(--s-6)" }}><span className="ab-ring" style={{ width: 18, height: 18 }} /> посев канвы…</div>;

  var ci = cs.intake || {};
  var ceiling = ci.autonomy_ceiling || "A0";
  var reqSkills = (ci.skills || []);
  var covered = reqSkills.filter(function (s) { return catalog[s]; }).length;
  var gaps = reqSkills.length - covered;
  var cols = {};
  nodes.forEach(function (n) { (cols[n.col] = cols[n.col] || []).push(n); });
  var colKeys = Object.keys(cols).map(Number).sort(function (a, b) { return a - b; });
  var selNode = nodes.find(function (n) { return n.id === sel; });

  return (
    <div style={{ display: "flex", gap: "var(--s-4)", height: "100%", minHeight: 0 }}>

      {/* Палитра — каталог навыков ABOP (источник для добавления в граф) */}
      <aside style={{ width: 210, flex: "none", display: "flex", flexDirection: "column", gap: "var(--s-2)", overflow: "auto" }}>
        <Rub>палитра · навыки ABOP</Rub>
        {Object.keys(catalog).sort().map(function (sid) {
          var s = catalog[sid];
          var on = nodes.some(function (n) { return n.id === "sk:" + sid; });
          return (
            <button key={sid} disabled={on} onClick={function () { addSkill(sid); }}
              title={s.short || ""}
              style={{ textAlign: "left", padding: "7px 10px", borderRadius: "var(--r-md)", cursor: on ? "default" : "pointer",
                background: on ? "var(--brand-050)" : "var(--surface)", border: "1px solid var(--surface-line)",
                color: on ? "var(--text-mute)" : "#fff", fontFamily: "var(--font-mono)", fontSize: "var(--fs-11)" }}>
              {on ? "✓ " : "+ "}{sid}
            </button>
          );
        })}
      </aside>

      {/* Канва — посеянный граф */}
      <section style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: "var(--s-3)" }}>
        <Glass style={{ display: "flex", alignItems: "center", gap: "var(--s-3)", flexWrap: "wrap" }}>
          <Chip tone="brand" style={{ padding: "4px 12px", fontSize: "var(--fs-13)" }}>{ceiling}</Chip>
          <span style={{ fontSize: "var(--fs-13)", color: "var(--text-2)" }}>потолок автономии из контракта · <span style={{ fontFamily: "var(--font-mono)" }}>{ci.audit_id}</span></span>
          <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
            <Chip tone="ok">{covered} покрыто</Chip>
            {gaps ? <Chip tone="warn">{gaps} пробел{gaps > 1 ? "ов" : ""}</Chip> : null}
            <Chip tone={(ci.hitl_points || []).length ? "warn" : "neutral"}>HITL: {(ci.hitl_points || []).length}</Chip>
          </span>
        </Glass>

        <div style={{ flex: 1, overflow: "auto", padding: "var(--s-4)", borderRadius: "var(--r-lg)",
          border: "1px solid var(--surface-line)", background: "var(--surface-sunken)" }}>
          <div style={{ display: "flex", alignItems: "flex-start", gap: 0, minWidth: "min-content" }}>
            {colKeys.map(function (ck, ciX) {
              return (
                <React.Fragment key={ck}>
                  <div style={{ display: "flex", flexDirection: "column", gap: "var(--s-3)", minWidth: 190 }}>
                    {cols[ck].map(function (n) { return <NodeCard key={n.id} n={n} ceiling={ceiling}
                      selected={sel === n.id} onSelect={function () { setSel(n.id); }} />; })}
                  </div>
                  {ciX < colKeys.length - 1 ? <Connector /> : null}
                </React.Fragment>
              );
            })}
          </div>
        </div>
      </section>

      {/* Инспектор — узел + автономия ≤ потолок */}
      <aside style={{ width: 260, flex: "none", overflow: "auto" }}>
        {selNode ? <Inspector n={selNode} ceiling={ceiling} onAutonomy={setAutonomy} contract={cs} />
          : <Glass><Rub>инспектор</Rub><div style={{ fontSize: "var(--fs-12)", color: "var(--text-3)", marginTop: 8 }}>Выберите узел на канве.</div></Glass>}
      </aside>
    </div>
  );

  function addSkill(sid) {
    var s = catalog[sid];
    setNodes(function (prev) {
      if (prev.some(function (n) { return n.id === "sk:" + sid; })) return prev;
      var outCol = prev.reduce(function (m, n) { return n.kind === "output" ? n.col : m; }, 2);
      var node = { id: "sk:" + sid, kind: "skill", title: sid, safety: s ? s.safety : null,
        catalogTitle: s ? s.title : null, autonomy: "A1", col: Math.max(1, outCol - 1), added: true };
      return prev.slice(0, -1).concat([node, prev[prev.length - 1]]);
    });
    setSel("sk:" + sid);
  }
  function setAutonomy(nodeId, a) {
    setNodes(function (prev) { return prev.map(function (n) { return n.id === nodeId ? Object.assign({}, n, { autonomy: a }) : n; }); });
  }
};

var NodeCard = function ({ n, ceiling, selected, onSelect }) {
  if (n.kind === "source" || n.kind === "output") {
    return (
      <div onClick={onSelect} style={{ cursor: "pointer", padding: "var(--s-3)", borderRadius: "var(--r-md)",
        background: "var(--surface)", border: "1px dashed var(--surface-line-2)", textAlign: "center" }}>
        <Rub>{n.kind === "source" ? "источник" : "выход"}</Rub>
        <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-12)", marginTop: 4 }}>{n.title}</div>
        {n.sub ? <div style={{ fontSize: "var(--fs-11)", color: "var(--text-3)", marginTop: 2 }}>{n.sub}</div> : null}
      </div>
    );
  }
  var gap = n.kind === "gap";
  var tone = gap ? "crit" : egressTone(n.safety);
  var bd = gap ? "var(--crit-line)" : selected ? "var(--brand)" : "var(--surface-line)";
  return (
    <div onClick={onSelect} style={{ cursor: "pointer", padding: "var(--s-3)", borderRadius: "var(--r-md)",
      background: selected ? "var(--brand-050)" : "var(--surface)", border: "1px solid " + bd,
      boxShadow: selected ? "0 0 0 1px var(--brand)" : "none", display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-12)", fontWeight: "var(--fw-semibold)", color: "#fff", flex: 1, wordBreak: "break-all" }}>{n.title}</span>
        <Chip tone="brand" style={{ padding: "1px 7px" }}>{n.autonomy}</Chip>
      </div>
      {gap ? (
        <Chip tone="crit">нет в каталоге ABOP</Chip>
      ) : (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          <Chip tone={n.safety && n.safety.mode === "action" ? "warn" : "neutral"}>{(n.safety && n.safety.mode) || "read"}</Chip>
          <Chip tone={n.safety && n.safety.egress === "external" ? "warn" : "neutral"}>egress:{(n.safety && n.safety.egress) || "internal"}</Chip>
          {n.safety && n.safety.cite ? <Chip tone="ok">cite</Chip> : null}
        </div>
      )}
      {n.safety && n.safety.egress === "external" ? <Chip tone="warn">⚠ HITL перед действием</Chip> : null}
    </div>
  );
};

var Connector = function () {
  return (
    <div style={{ display: "flex", alignItems: "center", padding: "0 4px", minWidth: 34, alignSelf: "center" }}>
      <svg width="34" height="14" viewBox="0 0 34 14" aria-hidden="true">
        <path d="M0 7 H27" stroke="var(--brand-400)" strokeWidth="1.5" />
        <path d="M27 3 L33 7 L27 11" fill="none" stroke="var(--brand-400)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );
};

var Inspector = function ({ n, ceiling, onAutonomy, contract }) {
  var ceilIdx = A_IDX(ceiling);
  var isNode = n.kind === "skill" || n.kind === "gap";
  return (
    <Glass style={{ display: "flex", flexDirection: "column", gap: "var(--s-3)" }}>
      <Rub>инспектор узла</Rub>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-13)", fontWeight: "var(--fw-semibold)", wordBreak: "break-all" }}>{n.title}</div>
      {n.catalogTitle ? <div style={{ fontSize: "var(--fs-12)", color: "var(--text-2)" }}>{n.catalogTitle}</div> : null}
      {n.kind === "gap" ? (
        <div style={{ background: "var(--crit-bg)", border: "1px solid var(--crit-line)", borderRadius: "var(--r-md)", padding: "var(--s-2)", fontSize: "var(--fs-12)", color: "var(--crit)" }}>
          Требуется контрактом, но нет в каталоге навыков ABOP. Создайте навык или замените — иначе сборка неполна (SDD §4.4).
        </div>
      ) : null}

      {isNode && n.kind !== "gap" && n.safety ? (
        <div>
          <Rub style={{ display: "block", marginBottom: 6 }}>безопасность</Rub>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            <Chip tone={n.safety.mode === "action" ? "warn" : "neutral"}>mode: {n.safety.mode}</Chip>
            <Chip tone={n.safety.egress === "external" ? "warn" : "neutral"}>egress: {n.safety.egress}</Chip>
            <Chip tone={n.safety.cite ? "ok" : "neutral"}>cite: {String(n.safety.cite)}</Chip>
          </div>
        </div>
      ) : null}

      {isNode ? (
        <div>
          <Rub style={{ display: "block", marginBottom: 6 }}>автономия узла (≤ {ceiling})</Rub>
          <div style={{ display: "flex", gap: 4 }}>
            {A_LEVELS.map(function (a, i) {
              var locked = i > ceilIdx;
              var active = n.autonomy === a;
              return (
                <button key={a} disabled={locked} title={locked ? "выше потолка контракта (ADR-013)" : ""}
                  onClick={function () { onAutonomy(n.id, a); }}
                  style={{ flex: 1, padding: "6px 0", borderRadius: "var(--r-sm)", cursor: locked ? "not-allowed" : "pointer",
                    fontFamily: "var(--font-mono)", fontSize: "var(--fs-11)",
                    background: active ? "var(--brand-grad)" : locked ? "transparent" : "var(--surface)",
                    color: active ? "#fff" : locked ? "var(--text-mute)" : "var(--text-2)",
                    border: "1px solid " + (active ? "transparent" : "var(--surface-line)"), opacity: locked ? 0.5 : 1 }}>
                  {locked ? "🔒" : a}
                </button>
              );
            })}
          </div>
          <div style={{ fontSize: "var(--fs-11)", color: "var(--text-3)", marginTop: 6 }}>
            Потолок задан LUDA в DeploymentContract — локально не поднять (это переаудит в LUDA).
          </div>
        </div>
      ) : (
        <div style={{ fontSize: "var(--fs-12)", color: "var(--text-3)" }}>Служебный узел процесса ({n.kind}).</div>
      )}
    </Glass>
  );
};
