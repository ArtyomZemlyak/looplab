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

export function nodeFeasibilityStatus(node) {
  if ((node?.violations || []).length || node?.feasible === false) return result(
    'alarm',
    'Constraint violation',
    'This result is infeasible and excluded from winner selection.',
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
