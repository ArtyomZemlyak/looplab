// Pure value formatters: metric numbers, byte sizes, epoch-seconds timestamps, and the caption
// font-size fitter. Split out of util.js (mega-refactor P5.2 — bodies verbatim); util.js re-exports
// everything, so importers are unchanged.

export function fmt(v, p = 4) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  if (typeof v !== 'number') return String(v)
  const a = Math.abs(v)
  if (a !== 0 && (a < 1e-3 || a >= 1e6)) return v.toExponential(2)
  return Number(v.toPrecision(p)).toString()
}

export function fmtInt(v) {
  if (v === null || v === undefined) return '—'
  return Number(v).toLocaleString()
}

// Dynamic font size for a node card's one-line caption ("what this node did"). The chip is a fixed
// width (~168px) and single line, so a long param-diff / change-summary used to hit the hard ellipsis
// almost immediately. Instead of clipping, shrink the font as the text grows so MORE of the caption
// stays legible in the same footprint — a short "baseline" stays a comfortable 11px, a long
// "lr: 0.01 → 0.003, depth: 4 → 8, subsample: …" scales down toward an 8px floor before ellipsizing.
// Pure + deterministic (length-based, ~0.56em/char) so it never reflows or measures the DOM.
export function chipFontSize(text, { max = 11, min = 8, width = 168 } = {}) {
  const len = String(text || '').length
  if (!len) return max
  const fit = width / (len * 0.56)   // approx glyph advance ≈ 0.56em at this weight
  return Math.max(min, Math.min(max, Math.round(fit * 2) / 2))   // clamp to [min,max] in 0.5px steps
}

// Human-readable byte size (file listings, etc.).
export function fmtBytes(n) {
  if (n == null) return ''
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}

// Epoch-SECONDS timestamp helpers (run mtime/created come from os.stat → seconds, not ms).
export function fmtDate(sec, withTime = true) {
  if (!sec) return '—'
  return new Date(sec * 1000).toLocaleString(undefined, withTime
    ? { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
    : { year: 'numeric', month: 'short', day: 'numeric' })
}
export function fmtAgo(sec) {
  if (!sec) return ''
  const d = Date.now() / 1000 - sec
  if (d < 60) return 'just now'
  if (d < 3600) return Math.floor(d / 60) + 'm ago'
  if (d < 86400) return Math.floor(d / 3600) + 'h ago'
  if (d < 7 * 86400) return Math.floor(d / 86400) + 'd ago'
  return new Date(sec * 1000).toLocaleDateString()
}
