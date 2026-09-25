// Маскот Эйп и лого — 1:1 из дизайн-системы (Ape.dc.html / APE Desktop.dc.html).
// Состояния из APE_STATES макета. Анимации ab-dot/ab-scan — в theme.css.

const APE_STATES = {
  idle:     { body: "#6366f1", leg: "#4f46e5", visor: "#dbeafe", beacon: "#34d399" },
  thinking: { body: "#6366f1", leg: "#4f46e5", visor: "#dbeafe", beacon: "#34d399" },
  scan:     { body: "#6366f1", leg: "#4f46e5", visor: "#0f172a", beacon: "#34d399" },
  hitl:     { body: "#f59e0b", leg: "#b45309", visor: "#fffbeb", beacon: "#fbbf24" },
  stop:     { body: "#ef4444", leg: "#991b1b", visor: "#fef2f2", beacon: "#fca5a5" },
  done:     { body: "#10b981", leg: "#065f46", visor: "#ecfdf5", beacon: "#6ee7b7" },
};

// SVG-маскот; state: idle|thinking|scan|hitl|stop|done, size — ширина px.
export function apeMascot(state = "idle", size = 32) {
  const c = APE_STATES[state] || APE_STATES.idle;
  const w = Number(size), h = Math.round(w * 106 / 80);
  let ov = "";
  if (state === "idle") ov = `<circle cx="33" cy="42" r="3.6" fill="#1e1b4b"/><circle cx="47" cy="42" r="3.6" fill="#1e1b4b"/>`;
  else if (state === "thinking") ov = `<circle cx="30" cy="42" r="3.2" fill="#4338ca" style="animation:ab-dot 1.05s ease-in-out infinite"/><circle cx="40" cy="42" r="3.2" fill="#4338ca" style="animation:ab-dot 1.05s ease-in-out .16s infinite"/><circle cx="50" cy="42" r="3.2" fill="#4338ca" style="animation:ab-dot 1.05s ease-in-out .32s infinite"/>`;
  else if (state === "hitl") ov = `<rect x="38" y="35" width="4" height="10" rx="2" fill="#92400e"/><circle cx="40" cy="49" r="2.4" fill="#92400e"/>`;
  else if (state === "stop") ov = `<g stroke="#991b1b" stroke-width="3.4" stroke-linecap="round"><path d="M29 38l8 8M37 38l-8 8M43 38l8 8M51 38l-8 8"/></g>`;
  else if (state === "done") ov = `<path d="M30 43l6 6 13-13" stroke="#065f46" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" fill="none"/>`;
  else if (state === "scan") ov = `<rect x="18" y="30" width="44" height="24" rx="12" fill="#0f172a"/><rect x="20" y="40" width="40" height="4" fill="#34d399" opacity="0.85" style="animation:ab-scan 1.7s ease-in-out infinite"/>`;
  // Маячок: в «думает» циклит по стадиям жёлтый→синий→зелёный + пульс-ореол (живее воспринимается).
  const beacon = state === "thinking"
    ? `<circle cx="40" cy="6" r="8" fill="#818cf8" opacity=".28" style="animation:ab-pulse 1.4s ease-in-out infinite;transform-box:fill-box;transform-origin:center"/><circle cx="40" cy="6" r="4.5" style="animation:ape-stage 1.8s ease-in-out infinite"/>`
    : `<circle cx="40" cy="6" r="4.5" fill="${c.beacon}"/>`;
  return `<svg width="${w}" height="${h}" viewBox="0 -6 80 106" fill="none" style="display:block;flex:none">
    <rect x="16" y="76" width="18" height="14" rx="6" fill="${c.leg}"/>
    <rect x="46" y="76" width="18" height="14" rx="6" fill="${c.leg}"/>
    <path d="M8 44a32 32 0 0 1 64 0v26a12 12 0 0 1-12 12H20A12 12 0 0 1 8 70Z" fill="${c.body}"/>
    <path d="M8 44a32 32 0 0 1 32-32v70H20A12 12 0 0 1 8 70Z" fill="#ffffff" opacity="0.08"/>
    <rect x="37" y="6" width="6" height="12" rx="3" fill="${c.leg}"/>
    <circle cx="40" cy="6" r="4.5" fill="${c.beacon}"/>
    <rect x="18" y="30" width="44" height="24" rx="12" fill="${c.visor}"/>
    ${ov}
  </svg>`;
}

// Лого в шапке (1:1 из header макета).
export function apeLogo(size = 30) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 72 72" fill="none" style="flex:none">
    <defs>
      <linearGradient id="apeMark" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8b5cf6"/><stop offset=".55" stop-color="#6366f1"/><stop offset="1" stop-color="#4338ca"/></linearGradient>
      <linearGradient id="apeVisor" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1e1b4b"/><stop offset="1" stop-color="#3730a3"/></linearGradient>
    </defs>
    <rect width="72" height="72" rx="20" fill="url(#apeMark)"/>
    <g transform="translate(3.45 5.85) scale(0.84)">
      <rect x="34.4" y="9" width="3.2" height="8" rx="1.6" fill="#ffffff" opacity=".8"/>
      <circle cx="36" cy="7.4" r="3.6" fill="#34d399"/>
      <rect x="53" y="37" width="7.5" height="17" rx="3.75" fill="#c7d2fe"/>
      <path d="M17 38a19 19 0 0 1 38 0v16a9 9 0 0 1-9 9H26a9 9 0 0 1-9-9Z" fill="#ffffff"/>
      <rect x="24" y="60" width="11" height="8" rx="4" fill="#ffffff"/>
      <rect x="38" y="60" width="11" height="8" rx="4" fill="#ffffff"/>
      <rect x="23.5" y="33" width="25" height="14" rx="7" fill="url(#apeVisor)"/>
    </g>
  </svg>`;
}
