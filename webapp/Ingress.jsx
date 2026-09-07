// Экран «Загрузка данных из Люды» — вход в ABOP (Contract Ingress, ADR-029).
// Принять handoff-бандл LUDA (JSON) → POST /api/contracts/ingest → ContractSet.
// Экрана нет в дизайне v3 — построен в его языке (стекло/индиго/mono/маскот).

var AUTONOMY_HUMAN = {
  A0: "минимальная — AI только показывает",
  A1: "низкая — AI предлагает, решаете вы",
  A2: "средняя — AI готовит, критичное подтверждаете",
  A3: "высокая — AI действует, вы контролируете исключения",
  A4: "полная — AI ведёт процесс автономно в рамках контракта",
};

var Ingress = function ({ onLoaded }) {
  var [busy, setBusy] = React.useState(false);
  var [drag, setDrag] = React.useState(false);
  var [result, setResult] = React.useState(null);   // {accepted, audit_id, intake, warnings} | {accepted:false, errors}
  var [recent, setRecent] = React.useState([]);
  var fileRef = React.useRef(null);

  var loadRecent = React.useCallback(function () {
    fetch("/api/contracts").then(function (r) { return r.json(); })
      .then(function (d) { setRecent(d.contracts || []); }).catch(function () {});
  }, []);
  React.useEffect(function () { loadRecent(); }, [loadRecent]);

  var ingest = function (bundle) {
    setBusy(true); setResult(null);
    fetch("/api/contracts/ingest", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(bundle) })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, j: j }; }); })
      .then(function (x) { setResult(x.j); if (x.j.accepted) loadRecent(); })
      .catch(function (e) { setResult({ accepted: false, errors: ["Сеть/сервер: " + String(e.message || e)] }); })
      .finally(function () { setBusy(false); });
  };

  var readFile = function (file) {
    if (!file) return;
    var rd = new FileReader();
    rd.onload = function () {
      try { ingest(JSON.parse(String(rd.result))); }
      catch (e) { setResult({ accepted: false, errors: ["Файл не является валидным JSON: " + String(e.message || e)] }); }
    };
    rd.readAsText(file);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--s-5)", maxWidth: 960, margin: "0 auto" }}>

      {/* Заголовок */}
      <div style={{ display: "flex", alignItems: "center", gap: "var(--s-4)" }}>
        <Ape state={result && result.accepted ? "dod" : busy ? "thinking" : "idle"} size={48} />
        <div>
          <Rub>ABOP · вход</Rub>
          <div style={{ fontSize: "var(--fs-22)", fontWeight: "var(--fw-bold)", letterSpacing: "var(--tracking-tight)" }}>
            Загрузка данных из Люды
          </div>
          <div style={{ fontSize: "var(--fs-13)", color: "var(--text-2)", marginTop: 2 }}>
            Приём аудита LUDA: три контракта + свидетельства. Валидация схем <span style={{ fontFamily: "var(--font-mono)" }}>luda.*/1.0</span> на границе — ABOP не доверяет вслепую.
          </div>
        </div>
      </div>

      {/* Drop-zone */}
      <Glass pad={false} style={{ overflow: "hidden" }}>
        <div
          onDragOver={function (e) { e.preventDefault(); setDrag(true); }}
          onDragLeave={function () { setDrag(false); }}
          onDrop={function (e) { e.preventDefault(); setDrag(false); readFile(e.dataTransfer.files[0]); }}
          onClick={function () { fileRef.current && fileRef.current.click(); }}
          style={{ cursor: "pointer", padding: "var(--s-8) var(--s-5)", textAlign: "center",
            background: drag ? "var(--brand-050)" : "transparent",
            border: "2px dashed " + (drag ? "var(--brand)" : "var(--surface-line-2)"), borderRadius: "var(--r-lg)", margin: "var(--s-3)" }}>
          <input ref={fileRef} type="file" accept="application/json,.json" style={{ display: "none" }}
            onChange={function (e) { readFile(e.target.files[0]); }} />
          {busy
            ? <div style={{ display: "inline-flex", alignItems: "center", gap: 10, color: "var(--text-2)" }}>
                <span className="ab-ring" style={{ width: 18, height: 18 }} /> Проверяю и принимаю бандл…</div>
            : <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "center" }}>
                <div style={{ fontSize: "var(--fs-16)", fontWeight: "var(--fw-semibold)" }}>Перетащите JSON-бандл LUDA сюда</div>
                <div style={{ fontSize: "var(--fs-12)", color: "var(--text-3)" }}>
                  или нажмите, чтобы выбрать файл · экспорт «Скачать JSON» из экрана «Решение и пилот» LUDA
                </div>
              </div>}
        </div>
      </Glass>

      {/* Результат приёма */}
      {result ? (result.accepted
        ? <ContractSetCard result={result} onLoaded={onLoaded} />
        : <RejectCard errors={result.errors} warnings={result.warnings} />) : null}

      {/* Уже принятые */}
      {recent.length ? (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--s-3)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)" }}>
            <Rub>принятые контракты</Rub>
            <Chip tone="neutral">{recent.length}</Chip>
            <span style={{ flex: 1, height: 1, background: "var(--surface-line)" }} />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(260px,1fr))", gap: "var(--s-3)" }}>
            {recent.map(function (c) {
              return (
                <Glass key={c.audit_id} style={{ display: "flex", flexDirection: "column", gap: "var(--s-2)" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)" }}>
                    <Chip tone="brand">{c.autonomy || "A?"}</Chip>
                    <span style={{ fontSize: "var(--fs-13)", fontWeight: "var(--fw-semibold)" }}>{c.family || "—"}</span>
                  </div>
                  <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-11)", color: "var(--text-3)" }}>{c.audit_id}</div>
                  <div style={{ fontSize: "var(--fs-12)", color: "var(--text-2)" }}>{(c.skills || []).length} навыков · сегмент {c.segment || "—"}</div>
                  <Button size="sm" variant="secondary" onClick={function () { onLoaded && onLoaded(c.audit_id); }}>Открыть в канве →</Button>
                </Glass>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
};

// Карточка принятого ContractSet (семя Project Graph).
var ContractSetCard = function ({ result, onLoaded }) {
  var ci = result.intake || {};
  var resid = ci.residency || {};
  var residIn = /on_prem|internal|перим|local/i.test(String(resid.content || ""));
  return (
    <Glass style={{ display: "flex", flexDirection: "column", gap: "var(--s-4)", borderColor: "var(--ok-line)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)", flexWrap: "wrap" }}>
        <Dot color="var(--ok)" /><Rub style={{ color: "var(--ok)" }}>бандл принят</Rub>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-12)", color: "var(--text-3)", marginLeft: "auto" }}>{ci.audit_id}</span>
      </div>

      <div style={{ display: "flex", alignItems: "baseline", gap: "var(--s-2)", flexWrap: "wrap" }}>
        <Chip tone="brand" style={{ fontSize: "var(--fs-13)", padding: "4px 12px" }}>{ci.autonomy_ceiling}</Chip>
        <span style={{ fontSize: "var(--fs-18)", fontWeight: "var(--fw-semibold)" }}>Автономия — потолок из контракта</span>
      </div>
      <div style={{ fontSize: "var(--fs-13)", color: "var(--text-2)", marginTop: "calc(-1 * var(--s-2))" }}>
        {AUTONOMY_HUMAN[ci.autonomy_ceiling] || ""}. Сборка агента не поднимет автономию выше (ADR-013).
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(180px,1fr))", gap: "var(--s-3)" }}>
        <Field k="семейство" v={ci.family} />
        <Field k="сегмент" v={ci.segment} />
        <Field k="критичность" v={ci.criticality} />
        <Field k="свидетельств" v={String(ci.evidence_count)} />
      </div>

      <div>
        <Rub style={{ display: "block", marginBottom: "var(--s-2)" }}>навыки ({(ci.skills || []).length})</Rub>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {(ci.skills || []).map(function (s) { return <Chip key={s} tone="brand">{s}</Chip>; })}
        </div>
      </div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        <Chip tone={(ci.hitl_points || []).length ? "warn" : "neutral"}>
          {(ci.hitl_points || []).length ? "HITL: " + ci.hitl_points.length + " точек" : "без HITL"}</Chip>
        <Chip tone={residIn ? "ok" : "neutral"}>{residIn ? "данные в периметре" : "резидентность: " + (resid.content || "—")}</Chip>
        {ci.limiting_axis ? <Chip tone="neutral">огранич. {ci.limiting_axis}</Chip> : null}
      </div>

      {(result.warnings || []).length ? (
        <div style={{ background: "var(--warn-bg)", border: "1px solid var(--warn-line)", borderRadius: "var(--r-md)", padding: "var(--s-3)" }}>
          <Rub style={{ color: "var(--warn)" }}>предупреждения</Rub>
          {result.warnings.map(function (w, i) { return <div key={i} style={{ fontSize: "var(--fs-12)", color: "var(--text-2)", marginTop: 4 }}>{w}</div>; })}
        </div>
      ) : null}

      <div style={{ display: "flex", alignItems: "center", gap: "var(--s-3)" }}>
        <Button variant="primary" onClick={function () { onLoaded && onLoaded(ci.audit_id); }}>Строить процесс в канве →</Button>
        <span style={{ fontSize: "var(--fs-12)", color: "var(--text-3)" }}>Канва посеется навыками и потолком автономии из контракта.</span>
      </div>
    </Glass>
  );
};

var RejectCard = function ({ errors, warnings }) {
  return (
    <Glass style={{ borderColor: "var(--crit-line)", background: "var(--crit-bg)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--s-2)" }}>
        <Dot color="var(--crit)" /><Rub style={{ color: "var(--crit)" }}>бандл не принят</Rub>
      </div>
      <div style={{ fontSize: "var(--fs-13)", color: "var(--text-2)", margin: "var(--s-2) 0" }}>
        ABOP валидирует схему и версию (ADR-025) и не заводит невалидный контракт. Исправьте и загрузите снова:
      </div>
      {(errors || []).map(function (e, i) {
        return <div key={i} style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-12)", color: "var(--crit)", padding: "3px 0" }}>· {e}</div>;
      })}
    </Glass>
  );
};

var Field = function ({ k, v }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <Rub>{k}</Rub>
      <span style={{ fontSize: "var(--fs-14)", fontWeight: "var(--fw-medium)" }}>{v || "—"}</span>
    </div>
  );
};
