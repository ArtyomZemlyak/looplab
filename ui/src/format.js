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

// Compact elapsed-time label for run summaries. Whole-second rounding keeps the existing
// at-a-glance display for ordinary evaluations, while a positive sub-second evaluation must not
// look like no work happened. Invalid and negative durations are not meaningful measurements.
export function fmtElapsedSeconds(v) {
  if (typeof v !== 'number' || !Number.isFinite(v) || v < 0) return '—'
  if (v === 0) return '0s'
  if (v < 1) return '<1s'
  return `${Math.round(v)}s`
}

// UNPRICED IS NOT FREE. `llm_cost.cost` is the sum of the amounts providers actually stated, and a
// provider that states nothing contributes 0 — so `$0` was shown for runs nobody ever priced
// (measured: `rubert-dr-0805`, 354 calls / 11,616,993 tokens / "$0"), and a partial total was shown
// as if it were the whole invoice (`rubert-dr-0804`: 209 of 313 calls priced, "$8.26" over a third
// of its tokens unpriced). `priced_calls` is what settles it; the fold backfills it for logs written
// before the counter existed (`events/replay.py::_row_priced_calls`).
//
// Returns { text, priced, partial, title } — `text` is what to show, `title` the hover explanation.
export function costPricing(c) {
  if (!c || typeof c !== 'object') {
    return { text: '—', priced: false, partial: false,
             title: 'No LLM cost roll-up exists for this run yet.' }
  }
  const cost = typeof c.cost === 'number' && Number.isFinite(c.cost) ? c.cost : null
  const calls = typeof c.calls === 'number' && Number.isFinite(c.calls) ? c.calls : 0
  // ABSENT is not ZERO, and this is the same distinction the whole function exists for, one level
  // up. The FOLDED total always carries `priced_calls` (the fold backfills it), but a RAW
  // `llm_cost` event written before it travelled with the payload does not — and the timeline reads
  // raw events. Defaulting a missing key to 0 made this print a confident "unpriced" over a run
  // that really spent $1.96 across 156 priced calls, while the Inspector showed the money from the
  // folded state on the same run at the same moment. `null` = "this payload does not say".
  const priced = typeof c.priced_calls === 'number' && Number.isFinite(c.priced_calls)
    ? c.priced_calls : null
  if (cost === null) {
    return { text: '—', priced: false, partial: false, title: 'No cost was recorded.' }
  }
  // No provider call at all (offline/toy run): nothing was spent and nothing is unknown.
  if (calls <= 0) {
    return { text: '$0', priced: true, partial: false,
             title: 'No model calls were made, so nothing was spent.' }
  }
  // The payload does not carry the counter: show the cost it DOES carry, and say the split is
  // unknown rather than asserting either "unpriced" or "all priced".
  if (priced === null) {
    return { text: `$${fmt(cost)}`, priced: true, partial: false,
             title: `This roll-up does not record how many of its ${calls.toLocaleString()} calls the`
               + ' provider priced, so the figure may be a floor. Open the run to see the split.' }
  }
  if (priced <= 0) {
    return { text: 'unpriced', priced: false, partial: false,
             title: `This run's provider reported no price for any of its ${calls.toLocaleString()}`
               + ' calls. Spend is unknown, not zero — read the token counts instead.' }
  }
  const label = `$${fmt(cost)}`
  if (priced < calls) {
    return { text: `${label}+`, priced: true, partial: true,
             title: `Only ${priced.toLocaleString()} of ${calls.toLocaleString()} calls were priced`
               + ` by the provider, so ${label} is a floor — the other`
               + ` ${(calls - priced).toLocaleString()} cost an unknown amount.` }
  }
  return { text: label, priced: true, partial: false,
           title: `All ${calls.toLocaleString()} calls were priced by the provider.` }
}

// Convenience wrapper for the one-line "N tokens · $X" summaries.
export function fmtCost(c) { return costPricing(c).text }

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
