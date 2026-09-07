// ABOP UI-примитивы (buildless React) в дизайн-языке v3: стекло, индиго-градиент,
// семафор, mono-лейблы, маскот Эйп. Цвета — из tokens.css (совпадают с дизайн-китом).

// Микро-лейбл секции: mono, uppercase, letter-spacing (ABOP_DESIGN_KIT §6.2).
var Rub = function ({ children, style }) {
  return <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-11)", textTransform: "uppercase",
    letterSpacing: "var(--tracking-caps)", color: "var(--text-3)", fontWeight: "var(--fw-semibold)", ...style }}>{children}</span>;
};

// Стеклянная панель — базовый блок ABOP.
var Glass = function ({ children, style, pad = true }) {
  return <div className="glass" style={{ padding: pad ? "var(--s-4)" : 0, boxShadow: "var(--shadow-panel)", ...style }}>{children}</div>;
};

// Кнопка: primary (бренд-градиент) / secondary (стекло) / ghost / danger. §6.4.
var Button = function ({ variant = "secondary", size = "md", disabled, loading, children, onClick, style }) {
  var skins = {
    primary: { background: "var(--brand-grad)", color: "#fff", border: "1px solid transparent" },
    secondary: { background: "var(--surface)", color: "#fff", border: "1px solid var(--surface-line)" },
    ghost: { background: "transparent", color: "var(--text-2)", border: "1px solid transparent" },
    danger: { background: "var(--crit-bg)", color: "var(--crit)", border: "1px solid var(--crit-line)" },
  };
  return (
    <button type="button" disabled={disabled || loading} onClick={onClick}
      style={{ display: "inline-flex", alignItems: "center", gap: "var(--s-2)",
        height: size === "sm" ? 32 : "var(--control-h)", padding: size === "sm" ? "0 12px" : "0 20px",
        borderRadius: "var(--r-md)", fontFamily: "var(--font-sans)", fontSize: "var(--fs-13)",
        fontWeight: "var(--fw-semibold)", cursor: (disabled || loading) ? "not-allowed" : "pointer",
        opacity: (disabled || loading) ? 0.5 : 1, transition: "transform 120ms ease, filter 120ms ease",
        ...(skins[variant] || skins.secondary), ...style }}
      onMouseDown={function (e) { if (!disabled && !loading) e.currentTarget.style.transform = "scale(.98)"; }}
      onMouseUp={function (e) { e.currentTarget.style.transform = "none"; }}
      onMouseLeave={function (e) { e.currentTarget.style.transform = "none"; }}>
      {loading ? <span className="ab-ring" style={{ width: 14, height: 14 }} /> : null}
      {children}
    </button>
  );
};

// Семафор-чип: статусная пилюля (ok/warn/crit/brand/neutral). §6.8.
var Chip = function ({ tone = "neutral", mono = true, children, style }) {
  var t = {
    ok: ["var(--ok-bg)", "var(--ok)", "var(--ok-line)"],
    warn: ["var(--warn-bg)", "var(--warn)", "var(--warn-line)"],
    crit: ["var(--crit-bg)", "var(--crit)", "var(--crit-line)"],
    brand: ["var(--brand-050)", "var(--brand-300)", "var(--brand-line)"],
    neutral: ["var(--surface)", "var(--text-2)", "var(--surface-line)"],
  }[tone] || ["var(--surface)", "var(--text-2)", "var(--surface-line)"];
  return <span style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "3px 10px",
    borderRadius: "var(--r-chip)", background: t[0], border: "1px solid " + t[2], color: t[1],
    fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)", fontSize: "var(--fs-11)",
    lineHeight: "var(--lh-tight)", whiteSpace: "nowrap", ...style }}>{children}</span>;
};

var Dot = function ({ color, size = 7 }) {
  return <span style={{ width: size, height: size, borderRadius: 9999, background: color, flex: "none", display: "inline-block" }} />;
};

// Маскот Эйп — купол + визор + маячок конверта. Состояние = статус (idle/thinking/hitl/stop/dod). §6.7.
var APE_BEACON = { idle: "#34d399", thinking: "#818cf8", hitl: "#f59e0b", stop: "#ef4444", dod: "#34d399" };
var Ape = function ({ state = "idle", size = 40 }) {
  var beacon = APE_BEACON[state] || APE_BEACON.idle;
  var visor = state === "stop" ? "#ef4444" : state === "hitl" ? "#f59e0b" : "#0f172a";
  return (
    <svg width={size} height={size * 1.06} viewBox="0 0 40 42" aria-hidden="true" style={{ flex: "none" }}>
      <defs><linearGradient id="apeg" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stopColor="#6366f1" /><stop offset="1" stopColor="#8b5cf6" /></linearGradient></defs>
      <circle cx="20" cy="6" r="3" fill={beacon} />
      <rect x="20.2" y="8" width="0" height="0" />
      <path d="M20 8 L20 12" stroke={beacon} strokeWidth="1.5" strokeLinecap="round" opacity=".7" />
      <rect x="6" y="12" width="28" height="24" rx="10" fill="url(#apeg)" />
      <rect x="11" y="18" width="18" height="10" rx="5" fill={visor} opacity={state === "idle" || state === "dod" ? 1 : .92} />
      {state === "hitl" ? <text x="20" y="26" textAnchor="middle" fontSize="9" fill="#fff" fontWeight="700">!</text>
        : state === "dod" ? <path d="M16 23 l3 3 l6 -6" stroke="#34d399" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        : <g fill="#c7d2fe"><circle cx="16" cy="23" r="1.6" /><circle cx="24" cy="23" r="1.6" /></g>}
    </svg>
  );
};

var TH = { textAlign: "left", padding: "0 12px 8px 0", borderBottom: "2px solid var(--surface-line-2)",
  fontFamily: "var(--font-mono)", fontSize: "var(--fs-11)", fontWeight: "var(--fw-semibold)",
  letterSpacing: "var(--tracking-caps)", textTransform: "uppercase", color: "var(--text-mute)", whiteSpace: "nowrap" };
var TD = { padding: "10px 12px 10px 0", borderBottom: "1px solid var(--surface-line)", verticalAlign: "top" };
