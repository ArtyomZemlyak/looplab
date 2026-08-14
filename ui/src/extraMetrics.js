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
// Measured over the preserved runs, `declared` produced 0 of 12 extra metrics and `auto` produced
// 12 of 12 — including `speculation_cuda_probe_v` (a schema VERSION number) rendered in the same
// table as the protected objective. So a UI that shows them identically is not neutral; it states
// something false about the weaker one.
//
// `unknown` is a value from a log written before the channel was recorded. It is deliberately NOT
// treated as `declared`: on every preserved row where we can check, the truth was `auto`. Readers
// group it WITH auto for trust and label it distinctly, so an operator can tell "the candidate
// printed this" from "nobody wrote down where this came from".
export const EXTRA_METRIC_DECLARED = 'declared'
export const EXTRA_METRIC_AUTO = 'auto'
export const EXTRA_METRIC_UNKNOWN = 'unknown'

export const EXTRA_METRIC_CHANNEL_LABEL = {
  [EXTRA_METRIC_DECLARED]: 'declared',
  [EXTRA_METRIC_AUTO]: 'self-reported',
  [EXTRA_METRIC_UNKNOWN]: 'provenance unknown',
}

export const EXTRA_METRIC_CHANNEL_HELP = {
  [EXTRA_METRIC_DECLARED]: "Read by the operator's own metric reader spec — the same guarded channel as the objective.",
  [EXTRA_METRIC_AUTO]: "Taken from the experiment's own stdout. Nothing declared it and nothing checked it; the code that produced the number also chose to print it.",
  [EXTRA_METRIC_UNKNOWN]: 'This run was recorded before the source of a secondary metric was written down. Treat it as self-reported — every such value we can check was.',
}

// The channel for one key of one node. Total: an absent map, a non-object map, a missing key or an
// unrecognised value all answer `unknown` rather than guessing the guarded channel.
export function extraMetricChannel(node, key) {
  const map = node && node.extra_metrics_provenance
  if (map && typeof map === 'object' && !Array.isArray(map)) {
    const found = map[key]
    if (found === EXTRA_METRIC_DECLARED || found === EXTRA_METRIC_AUTO) return found
  }
  return EXTRA_METRIC_UNKNOWN
}

// Is this value one an operator may read as a measurement? Only the declared channel is.
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
