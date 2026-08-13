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
      'Nothing records which artifact this number is about'
        + `${why === 'not_declared' ? ', because the task declares no eval.metric.subject'
             : why ? `: the declared subject is ${why}` : ''}`
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

export function reportStepIdentity(operator, theme) {
  const op = String(operator || 'unknown operator').trim()
  const th = String(theme || '').trim()
  return th && th !== op ? `${op} · ${th}` : op
}
