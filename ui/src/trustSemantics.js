// Pure trust-state wording shared by the run-wide Trust panel and the node Inspector.
// Absence of a recorded flag is deliberately NOT treated as proof that a detector ran.

const result = (tone, label, detail) => ({ tone, label, detail })

export function leakageStatus(leakage) {
  if (!leakage) return result(
    'unknown',
    'Not scanned',
    'No leakage scan is recorded for this run. This is unknown coverage, not a clean result.',
  )
  if (leakage.leak) return result(
    'alarm',
    'Leakage detected',
    'At least one recorded leakage detector flagged the run.',
  )
  if ((leakage.verdicts || []).length) return result(
    'ok',
    'Recorded scan passed',
    'The recorded leakage detectors completed without a flag.',
  )
  return result(
    'unknown',
    'No applicable evidence',
    'A scan event exists, but it contains no detector verdicts for this task.',
  )
}

export function driftStatus(drifts, config, evaluatedCount = 0) {
  if ((drifts || []).length) return result(
    'alarm',
    `${drifts.length} divergence${drifts.length === 1 ? '' : 's'} recorded`,
    'The independent metric cross-check disagreed with the primary metric.',
  )
  if (!config) return result(
    'unknown',
    'Coverage unknown',
    'No drift flags are recorded, but the cross-check configuration could not be verified.',
  )
  if (config.eval_trust_mode !== 'ratify_freeze_drift') return result(
    'unknown',
    'Cross-check not enabled',
    `Eval trust mode is ${config.eval_trust_mode || 'unspecified'}; no independent drift check is claimed.`,
  )
  if (!evaluatedCount) return result(
    'unknown',
    'Waiting for an evaluation',
    'The drift cross-check is enabled, but there are no evaluated nodes yet.',
  )
  return result(
    'unknown',
    'No divergence flags recorded',
    'The cross-check mode is enabled and no divergence event is recorded. Per-node coverage is not asserted here.',
  )
}

export function rewardHackStatus(hacks, config, evaluatedCount = 0) {
  if ((hacks || []).length) return result(
    'alarm',
    `${hacks.length} suspicious node${hacks.length === 1 ? '' : 's'} flagged`,
    'Review the recorded signals before trusting or promoting the result.',
  )
  if (!config) return result(
    'unknown',
    'Detector state unknown',
    'No suspicious signals are recorded, but detector configuration is unavailable.',
  )
  if (!config.reward_hack_detect) return result(
    'unknown',
    'Detector off',
    'No suspicious signals can be claimed because reward-hack detection is disabled.',
  )
  if (!evaluatedCount) return result(
    'unknown',
    'Waiting for an evaluation',
    'The detector is enabled, but it has no evaluated node to inspect yet.',
  )
  return result(
    'ok',
    'No suspicious signals found',
    `The enabled detector inspected ${evaluatedCount} evaluated node${evaluatedCount === 1 ? '' : 's'} without recording a flag.`,
  )
}

export const SALVAGED_METRIC_VIOLATION = 'metric_salvaged'
export const isSalvagedMetricViolation = v => v?.name === SALVAGED_METRIC_VIOLATION
// An UNBOUND metric rides the SAME `metric_salvaged` row on purpose (a second exclusion vocabulary
// would still exclude the node while silently ceasing to be recognised by every existing reader),
// so the row NAME cannot tell the two apart — only its `salvage.condition` can. Without this, an
// unbound node is labelled "Metric salvaged, not measured … recovered with its own declared
// reader", which is false in both halves: nothing failed and nothing was recovered. That is exactly
// the false accusation the comment below exists to prevent, one condition over.
export const UNBOUND_SUBJECT_CONDITION = 'metric_subject_unbound'
export const isUnboundSubjectViolation = v =>
  isSalvagedMetricViolation(v) && v?.salvage?.condition === UNBOUND_SUBJECT_CONDITION
// The node's own folded `metric_provenance`, which is the ONLY record of a salvage under
// `metric_salvage: "select"`. Everything the salvage UI knew used to hang off the violation ROW, and
// `select` is precisely the rung that has no row — so the operator who opted a salvaged metric INTO
// winner selection was the one shown nothing at all, on the node where it matters most. The
// commit's own argument ("a provenance field alone would be 'can tell' and not 'does'") applies to
// its own permissive rung: the field is folded and served, so read it.
export const salvagedProvenance = node =>
  (node?.metric_provenance?.salvaged ? node.metric_provenance : null)

// WHY AN UNBOUND METRIC IS UNBOUND, as one clause, spelled ONCE. Both surfaces that say it — the
// Trust panel's feasibility row and the Metrics tab's objective source — must name the same fix, and
// a second hand-written copy is how they come to name different ones. `not_declared` is the state 82
// of 83 corpus metrics are in, so it gets the sentence that tells the operator what to write; every
// other slug is a fact about a subject that WAS declared and reads as such.
const unboundBecause = (why) =>
  why === 'not_declared' ? ', because the task declares no eval.metric.subject'
    : why ? `: the declared subject is ${why}` : ''

export function nodeFeasibilityStatus(node) {
  const violations = node?.violations || []
  const provenance = salvagedProvenance(node)
  // A SALVAGED metric is not a constraint violation and must not be shown as one. It rides on the
  // violations list because that is what `feasible = not violations` reads and therefore what keeps
  // an unmeasured value out of champion selection — but the operator reading "Constraint violation"
  // about a node whose experiment SUCCEEDED, and whose metric was recovered by the run's own
  // declared reader, is being told something false. The exclusion is real; the accusation is not.
  // Checked BEFORE the salvage branch, because an unbound row satisfies `isSalvagedMetricViolation`
  // too — it IS one of those rows. The number here was measured by the scoring path and the eval
  // succeeded; what is missing is any record of WHAT it is about.
  if (violations.length && violations.every(isUnboundSubjectViolation)) {
    const why = violations.find(isUnboundSubjectViolation)?.salvage?.unbound_reason
    return result(
      'warn',
      'Metric bound to no subject',
      'Nothing records which artifact this number is about' + unboundBecause(why)
        + ', so the claim cannot be checked and the node is excluded from winner selection.',
    )
  }
  if (violations.length && violations.every(isSalvagedMetricViolation)) {
    const source = violations.find(isSalvagedMetricViolation)?.salvage
    return result(
      'warn',
      'Metric salvaged, not measured',
      'The experiment produced this metric and the run recovered it with its own declared reader'
        + `${source?.stage ? ` after stage “${source.stage}” failed its contract` : ''}`
        + '. It is excluded from winner selection until metric_salvage is set to “select”.',
    )
  }
  if (violations.length || node?.feasible === false) return result(
    'alarm',
    'Constraint violation',
    'This result is infeasible and excluded from winner selection.'
      + (provenance ? ' Its metric was also SALVAGED rather than measured.' : ''),
  )
  // ADMITTED, and still not measured. `select` removes the exclusion, not the fact — this node
  // competes for champion on a number recovered from an eval that failed, and a row reading plain
  // "Feasible" is the same silence the violation row was added to break.
  if (provenance) return result(
    'warn',
    'Metric salvaged, admitted for selection',
    'The run recovered this metric with its own declared reader'
      + `${provenance.stage ? ` after stage “${provenance.stage}” failed its contract` : ''}`
      + '. metric_salvage is set to “select”, so it competes for champion like a measured result.',
  )
  if (node?.status === 'evaluated' && node?.feasible === true) return result(
    'ok',
    'Feasible',
    'Evaluation completed with no recorded constraint violation.',
  )
  return result(
    'unknown',
    'Not established',
    'Feasibility is only established after a successful evaluation.',
  )
}

// WHAT THE OBJECTIVE NUMBER IS — the Metrics tab's ★ row, and the browser half of the engine's
// salvage/subject vocabulary applied to the PRIMARY metric.
//
// The Metrics tab is the surface an operator opens to READ NUMBERS, and it used to print a hardcoded
// `measured` with the tooltip "read by the operator's own metric spec on the protected score stage"
// for every node — including the ones this very file already describes to the Trust tab as "Metric
// salvaged, not measured". Two tabs about one record, disagreeing about whether anybody measured it.
//
// THE DECISION LIVES HERE, beside `nodeFeasibilityStatus`, and not inline in `Inspector.jsx`, for
// the reason the disagreement happened at all: a vocabulary with two homes drifts, and the JSX home
// was unreachable by any test. Reading the same three record facts — the violation ROWS, their
// `salvage.condition`, and the folded `metric_provenance` — is what keeps the two rows one answer.
//
// The vocabulary is THREE channels and they are deliberately not two:
//
//   measured             — nothing in the record says otherwise. The historical sentence, kept
//                          verbatim for the node it was always true about.
//   salvaged             — the eval FAILED and the number was recovered by the run's own declared
//                          reader. It says the engine's own word, not a hedge. It is the SAME word
//                          whether the node is excluded (`audit`) or ADMITTED (`select`): `select`
//                          removes the exclusion, not the fact, and the admitted rung is the one
//                          where the number competes for champion, i.e. where the label matters
//                          most. The tooltip is what carries which rung it is.
//   measured, no subject — the eval SUCCEEDED, the protected scoring path read the number, and
//                          nothing records WHICH ARTIFACT it is about. NOT the same word as
//                          salvaged, for the reason `isUnboundSubjectViolation` above states:
//                          nothing failed and nothing was recovered, so "salvaged" is a false
//                          accusation one condition over. Both halves of the label are true and
//                          neither can be read as the other.
//
// WHAT THIS DELIBERATELY DOES NOT LABEL, both derived from the record rather than from taste:
//
//   * a CONSTRAINT violation. `latency_ms > 500` is a fact about a bound, and the metric beside it
//     was measured. Feasibility asks "why is this node excluded" — an EVERY-row question, which is
//     why `nodeFeasibilityStatus` uses `every` — while this asks "what is this number", an ANY-row
//     question. A node with both a breached bound and a salvage row is salvaged HERE and a
//     constraint violation THERE, and both statements are true.
//   * an unbound subject under the `audit`/`off` rungs, which record without enforcing and mint no
//     row. That is 82 of 83 preserved corpus metrics, so a label there would fire on the rule rather
//     than on the finding — and it would put this tab back in disagreement with the Trust tab, which
//     reads the same non-enforcing record as "Feasible". Stated, not patched.
export const OBJECTIVE_MEASURED = 'measured'
export const OBJECTIVE_SALVAGED = 'salvaged'
export const OBJECTIVE_SUBJECT_UNBOUND = 'subject_unbound'

// Short enough for a table cell, and shaped like the extras' own labels beside it (`declared` /
// `self-reported` / `engine diagnostic` / `provenance unknown`) so one column reads as one column.
export const OBJECTIVE_SOURCE_LABEL = {
  [OBJECTIVE_MEASURED]: 'measured',
  [OBJECTIVE_SALVAGED]: 'salvaged',
  [OBJECTIVE_SUBJECT_UNBOUND]: 'measured, no subject',
}

// The base sentence per channel. The record-specific half (which stage failed, which subject rule,
// which selection rung) is appended by `objectiveSourceHelp` — the tooltip carries the detail, as
// the historical one did.
export const OBJECTIVE_SOURCE_HELP = {
  [OBJECTIVE_MEASURED]:
    "The run's objective: read by the operator's own metric spec on the protected score stage.",
  [OBJECTIVE_SALVAGED]:
    'NOT measured: the experiment produced this number and the run recovered it with its own '
    + 'declared reader',
  [OBJECTIVE_SUBJECT_UNBOUND]:
    'This number was measured by the protected scoring path, but nothing records which artifact it '
    + 'is about',
}

// The channel + the facts the tooltip needs, read from ONE node record. Total over junk: a
// hand-edited log, a `violations` list of strings, a string `metric_provenance` — anything that is
// not a recognised row or record answers `measured`, because a caveat nobody recorded must not be
// invented, and equally an absent detector is never proof of a clean result (this file's first rule).
export function objectiveMetricSource(node) {
  const violations = Array.isArray(node?.violations) ? node.violations : []
  const salvageRows = violations.filter(isSalvagedMetricViolation)
  const unboundRows = salvageRows.filter(isUnboundSubjectViolation)
  // A salvage-PROPER row is a `metric_salvaged` row that is not the unbound one riding on the same
  // name. The two are told apart by `salvage.condition` and by nothing else — see the comment on
  // `UNBOUND_SUBJECT_CONDITION`.
  const properRows = salvageRows.filter(v => !isUnboundSubjectViolation(v))
  const provenance = salvagedProvenance(node)
  if (properRows.length || provenance) {
    const account = properRows[0]?.salvage || provenance || {}
    return {
      channel: OBJECTIVE_SALVAGED,
      stage: account.stage || '',
      // ADMITTED is exactly "the record minted no row": `metric_salvage: "select"` over
      // operator-produced output is the only rung that mints none, and any row at all — a salvage
      // row, an unbound row, a breached bound — keeps the node out of `feasible_nodes`. Deriving it
      // from the rows rather than from `feasible` is what stops a node excluded for a DIFFERENT
      // reason from being told it competes for champion.
      admitted: violations.length === 0,
    }
  }
  if (unboundRows.length) {
    return {
      channel: OBJECTIVE_SUBJECT_UNBOUND,
      unboundReason: unboundRows[0]?.salvage?.unbound_reason || '',
    }
  }
  // A repaired DECLARATION is `metric_provenance` for a MEASURED metric — the artifact contract was
  // re-checked and PASSED, and `engine/metric_salvage.py::declaration_repair_provenance` spells
  // `salvaged: False` out so "a False here reads as measured everywhere". So the LABEL does not
  // move: softening a re-checked measurement is the same error as hardening an unbound one, one
  // direction over. The tooltip carries the one thing this record uniquely proves — that the node's
  // recorded code is not byte-for-byte what produced its recorded metric.
  const repaired = node?.metric_provenance
  return {
    channel: OBJECTIVE_MEASURED,
    declarationRepaired: !!(repaired && typeof repaired === 'object' && !Array.isArray(repaired)
      && repaired.declaration_repaired === true && !repaired.salvaged),
  }
}

// The full tooltip for one `objectiveMetricSource` record. Kept apart from the model so the channel
// (which decides the LABEL and the warn styling) and the sentence (which the operator hovers for)
// cannot come to disagree about the same node.
export function objectiveSourceHelp(source) {
  const base = OBJECTIVE_SOURCE_HELP[source?.channel] || OBJECTIVE_SOURCE_HELP[OBJECTIVE_MEASURED]
  if (source?.channel === OBJECTIVE_SALVAGED) {
    return base
      + `${source.stage ? ` after stage “${source.stage}” failed its contract` : ''}`
      + (source.admitted
        ? '. metric_salvage is set to “select”, so it competes for champion like a measured result.'
        : '. It is excluded from winner selection until metric_salvage is set to “select”.')
  }
  if (source?.channel === OBJECTIVE_SUBJECT_UNBOUND) {
    return base + unboundBecause(source.unboundReason)
      + ', so the claim cannot be checked and the node is excluded from winner selection.'
  }
  return base + (source?.declarationRepaired
    ? ' Its declaration was repaired after the fact and the artifact contract was then re-checked'
      + " and passed, so this node's recorded code is not byte-for-byte what produced this number."
    : '')
}

// IS THIS OBJECTIVE MARKED? ONE predicate, hoisted out of `Inspector.jsx::Metrics` where it was the
// inline expression `objective.channel !== OBJECTIVE_MEASURED`. A second surface copying that
// expression is precisely how the two homes this file exists to prevent get rebuilt one boolean at a
// time — and the second surface is a SELECTION display (`panels.jsx::ParetoPanel`), so the two must
// mark the SAME nodes or the run's champion is caveated on the tab that reads numbers and unmarked
// on the chart that picks them. Absent or unrecognised input answers FALSE, the same rule
// `objectiveMetricSource` is total under: a caveat nobody recorded must not be invented.
export const objectiveSourceCaveated = source =>
  !!source && !!source.channel && source.channel !== OBJECTIVE_MEASURED

export function reportStepIdentity(operator, theme) {
  const op = String(operator || 'unknown operator').trim()
  const th = String(theme || '').trim()
  return th && th !== op ? `${op} · ${th}` : op
}
