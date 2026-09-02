// Pure value formatters: metric numbers, byte sizes, epoch-seconds timestamps, and the caption
// font-size fitter. Split out of util.js (mega-refactor P5.2 — bodies verbatim); util.js re-exports
// everything, so importers are unchanged.

// OPEN[ranked-metrics-print-fewer-digits-than-they-rank] the cross-run table ranks at full precision
// and renders through this four-significant-figure formatter, so the two best numbers on the corpus
// (0.793426 and 0.793411) both print 0.7934 under ranks 1 and 2 with no tie marker, while the claim
// sentence beside the table prints the unrounded value. Driven: `Number(v.toPrecision(4)).toString()`
// returns the same string for both. No caller anywhere in `ui/src` asks for more than 4, so the
// remedy is a formatter that widens until the ranked values it is given render distinctly, used
// wherever a rank is shown.
// proof:absent:distinctMetricFormatter@ui/src/format.js
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

// HOW LONG, IN ONE PLACE — the tiered duration label every surface that prints an interval shares.
//
// Three tiers and TWO boundaries, named rather than spelled inline, because three surfaces had each
// re-derived the same two numbers and the top tier had already drifted: for one and the same
// interval the run's live-status age printed `3h 25m` (narration.js), a node's episode picker `3.4h`
// (traceEpisodeModel.js) and the standing-watch strip `3h` (assistantWatchModel.js). An operator
// reads the first two side by side — the episode picker sits inside the run screen the status strip
// captions — so one interval was labelled two ways on one screen. Nothing anywhere stated a reason
// for the three to differ, so they are now one rendering; what stays at each call site is only its
// own wording around the number (`in 3h 26m`, `3h 26m`).
//
// It lives HERE and not in narration.js, which is where the first of the three was: narration.js
// reaches `markdown.jsx`, so plain `node --test` cannot import it, and the two pure models that need
// this are driven directly by `node --test` (the house pattern). format.js imports nothing, which is
// what lets all three reach one definition. `util.js` re-exports it, so narration.js pulls it in
// beside `fmt` exactly as before.
export const DURATION_SECOND_TIER_MAX_S = 90     // below this, bare seconds: `89s` beats `1m 29s`
export const DURATION_MINUTE_TIER_MAX_S = 5400   // below this, whole minutes — up to `90m`

export function durationLabel(v) {
  // Not `Number(v)`: `Number(null)` and `Number('')` are 0, so a coercing guard would render an
  // ABSENT duration as `0s`. Only a real number is a measurement.
  if (typeof v !== 'number' || !Number.isFinite(v) || v < 0) return ''
  if (v < DURATION_SECOND_TIER_MAX_S) return `${Math.round(v)}s`
  if (v < DURATION_MINUTE_TIER_MAX_S) return `${Math.round(v / 60)}m`
  const hours = Math.floor(v / 3600)
  const minutes = Math.round((v % 3600) / 60)
  // 7,199 s is 1 h 59.98 m, and rounding the remainder on its own printed `1h 60m` — carry it into
  // the hour instead. The narration copy this consolidates had that bug; it was simply unreachable
  // in its own tests, which is the other half of why three copies is worse than one.
  if (minutes >= 60) return `${hours + 1}h`
  return minutes ? `${hours}h ${minutes}m` : `${hours}h`
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
  // A body whose roll-up was never written says so. `routers/runs.py::run_cost` and
  // `routers/reviews.py::_review_cost` zero-fill their defaults and stamp `recorded: false` for
  // exactly this case, naming this function as the consumer — and until this branch existed the
  // flag was dead payload, so an unmeasured run still reached `calls <= 0` below and rendered as a
  // measured "$0 — nothing was spent". Absent means the writer does not state it; only an explicit
  // `false` is a claim.
  if (c.recorded === false) {
    return { text: '—', priced: false, partial: false,
             title: 'No LLM cost roll-up exists for this run yet.' }
  }
  const cost = typeof c.cost === 'number' && Number.isFinite(c.cost) ? c.cost : null
  // `null`, not `0` — the same rule as `priced_calls` below, for the same reason. `narration.js`
  // renders RAW `llm_cost` events and its `validate` requires only `total_tokens` and `cost`, so a
  // payload carrying `cost: 8.26` and no `calls` key is reachable; defaulting it to 0 put that
  // payload in the "$0, nothing was spent" branch BEFORE its cost was ever read.
  const calls = typeof c.calls === 'number' && Number.isFinite(c.calls) ? c.calls : null
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
  // No provider call at all (offline/toy run): nothing was spent and nothing is unknown. Only when
  // the payload STATES the count — see `calls` above.
  if (calls !== null && calls <= 0) {
    return { text: '$0', priced: true, partial: false,
             title: 'No model calls were made, so nothing was spent.' }
  }
  // A cost with no call count. Show the money it does state and say what it does not: claiming
  // either "nothing was spent" or "all of it is priced" would be inventing the missing half.
  if (calls === null) {
    return { text: `$${fmt(cost)}`, priced: cost > 0, partial: false,
             title: 'This roll-up does not record how many calls it covers, so the figure may be a'
               + ' floor. Open the run to see the split.' }
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
