// WHERE A NODE'S SECONDARY METRIC CAME FROM — the browser half of
// `looplab/core/models.py`'s extra-metric CHANNEL vocabulary, and the one place the UI decides how
// to SAY it.
//
// Two channels fill `Node.extra_metrics` and until 2026-08-14 the record could not tell them apart:
//
//   declared — read by an operator-owned `eval.metrics` reader spec. That channel refuses
//              agent-authored reader code, for the same reason the drift cross-check does.
//   auto     — every OTHER numeric key on the experiment's own stdout JSON line. No declaration,
//              no reader spec, no gate: the code that produced the number also chose to print it.
//
// Measured over the whole preserved corpus (238 logs, 1,642 values), `declared` produced 0 and
// `auto` produced all of them — including `speculation_cuda_probe_v` (a schema VERSION number)
// rendered in the same table as the protected objective. So a UI that shows them identically is not
// neutral; it states something false about the weaker one.
//
//   engine   — the third channel, added 2026-08-14. The engine SPLICES its own source into some
//              artifacts (the speculation calibration's CUDA probe), and the numbers that source
//              prints are diagnostics it wrote and independently authenticates: a schema version, a
//              device count, two request constants. 1,636 of those 1,642 values are these four keys.
//              They are trustworthy — and they are NOT measurements of the experiment, which is the
//              distinction this label exists to draw. `auto` was a false statement about them
//              ("nobody checked it"); showing them as `declared` would be the opposite false
//              statement ("the operator is reading a result").
//
// `unknown` is a value from a log written before the channel was recorded. It is deliberately NOT
// treated as `declared`: on every preserved row where we can check, the truth was `auto`. Readers
// group it WITH auto for trust and label it distinctly, so an operator can tell "the candidate
// printed this" from "nobody wrote down where this came from".
export const EXTRA_METRIC_DECLARED = 'declared'
export const EXTRA_METRIC_AUTO = 'auto'
export const EXTRA_METRIC_ENGINE = 'engine'
export const EXTRA_METRIC_UNKNOWN = 'unknown'

export const EXTRA_METRIC_CHANNEL_LABEL = {
  [EXTRA_METRIC_DECLARED]: 'declared',
  [EXTRA_METRIC_AUTO]: 'self-reported',
  [EXTRA_METRIC_ENGINE]: 'engine diagnostic',
  [EXTRA_METRIC_UNKNOWN]: 'provenance unknown',
}

export const EXTRA_METRIC_CHANNEL_HELP = {
  [EXTRA_METRIC_DECLARED]: "Read by the operator's own metric reader spec — the same guarded channel as the objective.",
  [EXTRA_METRIC_AUTO]: "Taken from the experiment's own stdout. Nothing declared it and nothing checked it; the code that produced the number also chose to print it.",
  [EXTRA_METRIC_ENGINE]: "Printed by LoopLab's own instrumentation spliced into this artifact, and verified against it. A diagnostic about the machine or the harness — not a measurement of this experiment.",
  [EXTRA_METRIC_UNKNOWN]: 'This run was recorded before the source of a secondary metric was written down. Treat it as self-reported — every such value we can check was.',
}

// The channel for one key of one node. Total: an absent map, a non-object map, a missing key or an
// unrecognised value all answer `unknown` rather than guessing the guarded channel.
export function extraMetricChannel(node, key) {
  const map = node && node.extra_metrics_provenance
  if (map && typeof map === 'object' && !Array.isArray(map)) {
    const found = map[key]
    if (found === EXTRA_METRIC_DECLARED || found === EXTRA_METRIC_AUTO
        || found === EXTRA_METRIC_ENGINE) return found
  }
  return EXTRA_METRIC_UNKNOWN
}

// Is this value one an operator may read as a measurement OF THIS EXPERIMENT? Only the declared
// channel is — and `engine` is deliberately excluded even though it is the better-authenticated of
// the two non-declared channels. Trust and subject are different questions: the CUDA probe's
// `speculation_cuda_probe_v` is a number nobody can forge and still nothing anyone should read off
// a results table. Caveating it is the point; the LABEL is what stops the caveat from being wrong
// about why.
export function extraMetricIsDeclared(node, key) {
  return extraMetricChannel(node, key) === EXTRA_METRIC_DECLARED
}

// Which of a set of keys are NOT declared, across a population of nodes — what a surface that shows
// several nodes' extras at once (the Pareto table) needs in order to caveat the right columns.
// A key is unverified if ANY node reporting it reports it through a non-declared channel: the
// column is one heading over many rows, and a heading cannot be half-trustworthy.
export function unverifiedExtraMetricKeys(nodes, keys) {
  const out = new Set()
  for (const key of keys || []) {
    for (const node of nodes || []) {
      if (node && node.extra_metrics && node.extra_metrics[key] != null
          && !extraMetricIsDeclared(node, key)) { out.add(key); break }
    }
  }
  return out
}

// WAS IT MEASURED, OR RECONSTRUCTED AFTERWARDS — the browser half of
// `looplab/core/models.py::extra_metric_is_backfilled`, and a question ORTHOGONAL to the channel
// above rather than a fourth value of it.
//
// `maintenance/backfill_score_metrics.py` recovers objectives the score stage printed and the run
// threw away (36 numbers computed, one kept). It writes them through the `declared` channel, which
// is the honest one of the three — the operator's own scoring program printed them, so `auto` and
// `engine` are both false — and until 2026-09-02 that was the WHOLE record, so a reconstruction
// rendered here exactly like a measurement taken while the run was happening.
//
// THE COST IS ABOUT PRECISION. The recovered suite is printed to TWO decimals while the primary is
// read at six, so neighbouring nodes tie: `e5small-dr-unified-v4` nodes 0 and 1 differ by 0.006 on
// recall@100 and are identical on every recovered metric. "These two nodes are equal on nDCG" and
// "the print statement cannot tell them apart" are different claims, and a table that renders the
// second as the first is stating something false.
//
// NOT PER KEY, because the writer cannot produce a partially-backfilled node: the fold declines a
// node that already carries ANY extra metric, so a backfilled map is backfilled entirely. The
// `node` argument is still taken per call so the two readers below have the same shape as the
// channel ones and a caller cannot pass the wrong object silently.
export const EXTRA_METRIC_RECONSTRUCTED_LABEL = 'reconstructed'
export const EXTRA_METRIC_RECONSTRUCTED_HELP =
  'Recovered from the preserved score log after the run, not recorded while it was happening. The '
  + 'operator\'s own scoring program printed it — but at the precision it chose to print, which is '
  + 'coarser than the objective, so two nodes equal here are not known to be equal.'

// Absent means MEASURED, and that is the safe direction here — the opposite of the channel map's.
// An absent channel means "nobody wrote down where this came from" and must not read as the
// guarded one; an absent backfill marker means the fold never applied a reconstruction to this
// node, which no log written before the backfill tool existed can contradict.
export function extraMetricIsBackfilled(node) {
  const record = node && node.extra_metrics_backfill
  return !!(record && typeof record === 'object' && !Array.isArray(record) && record.backfilled)
}

// How many decimals this value was PRINTED to, or null. `null` is not "full precision" — it is
// "nobody wrote it down", which is the answer for every live measurement and for a reconstruction
// whose log did not say. A surface rendering a tie must say which of the three it is looking at.
export function extraMetricPrecision(node, key) {
  const record = node && node.extra_metrics_backfill
  if (!record || typeof record !== 'object' || Array.isArray(record)) return null
  const decimals = record.precision_decimals
  if (!decimals || typeof decimals !== 'object' || Array.isArray(decimals)) return null
  const found = decimals[key]
  return Number.isInteger(found) && found >= 0 ? found : null
}

// The source cell's full sentence for one key: the channel, plus the reconstruction note when there
// is one. ONE function so the label and its tooltip cannot come apart, and so a surface cannot
// print "declared" with no hint that the number was recovered from a log.
export function extraMetricSourceLabel(node, key) {
  const channel = extraMetricChannel(node, key)
  if (!extraMetricIsBackfilled(node)) return EXTRA_METRIC_CHANNEL_LABEL[channel]
  return `${EXTRA_METRIC_CHANNEL_LABEL[channel]} · ${EXTRA_METRIC_RECONSTRUCTED_LABEL}`
}

export function extraMetricSourceHelp(node, key) {
  const channel = extraMetricChannel(node, key)
  if (!extraMetricIsBackfilled(node)) return EXTRA_METRIC_CHANNEL_HELP[channel]
  const decimals = extraMetricPrecision(node, key)
  const precision = decimals == null ? '' : ` Printed to ${decimals} decimal place(s).`
  return `${EXTRA_METRIC_CHANNEL_HELP[channel]} ${EXTRA_METRIC_RECONSTRUCTED_HELP}${precision}`
}

// A value that is caveated for EITHER reason — the channel is not the guarded one, or the whole map
// is a reconstruction. This is what a source cell decides its `warn` class on: `declared ·
// reconstructed` is still a caveat, and rendering it unmarked because the channel is `declared` is
// the inversion this whole block exists to close.
export function extraMetricCaveated(node, key) {
  return !extraMetricIsDeclared(node, key) || extraMetricIsBackfilled(node)
}

