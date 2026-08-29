import { emptyRunRouteState, encodeRunRouteState } from './runRouteState.js'

const RUN_ID_RE = /^[0-9a-f]{64}$/
const GENERATION_RE = /^[0-9a-f]{64}$/
const PERMISSION_ID_RE = /^[0-9a-f]{16}$/
const SESSION_ID_RE = /^[0-9a-f]{16}$/
const CONTROL_RE = /[\u0000-\u001f\u007f]/

export const ATTENTION_KINDS = new Set([
  'approval', 'approval_incomplete', 'spec_approval', 'failure_spike', 'run_failed',
  'budget_exhausted', 'finished', 'stopped', 'finalization_stalled', 'stalled', 'train_monitor',
  // A stage heading past the wall that will kill it, by more than the deadline grace can absorb.
  // A SEPARATE kind from `train_monitor` because it is a separate question: that one asks whether
  // the training is broken, this one asks whether it will finish, and a node can be perfectly
  // healthy and doomed at the same time (node 6 burned 7.78 GPU-hours reading `healthy` throughout).
  'train_overrun', 'asha',
])
const SEVERITIES = new Set(['action', 'warning', 'danger', 'success'])
const SEVERITY_PRIORITY = Object.freeze({ danger: 4, action: 3, warning: 2, success: 1 })
const NEEDS_ACTION = new Set([
  'approval', 'approval_incomplete', 'spec_approval', 'failure_spike', 'run_failed',
  'finalization_stalled', 'stalled', 'assistant_permission', 'train_monitor',
  // NEEDS_ACTION, and the action is time-critical in a way the others are not: the window closes
  // when the wall arrives, and after that there is nothing to decide.
  'train_overrun',
])

const COPY = Object.freeze({
  approval: ['Experiment approval needed', 'Review the exact pending experiment lifecycle.', 'Review run'],
  approval_incomplete: ['Approval state needs inspection', 'No safe approval target is available. Inspect Events.', 'Open Events'],
  spec_approval: ['Evaluation spec approval needed', 'Review the pending evaluation specification.', 'Review spec'],
  failure_spike: ['Experiment failures need attention', 'Several current experiments failed. Inspect the failure evidence.', 'Inspect failures'],
  run_failed: ['Run failure needs attention', 'Open the run for the failure evidence and recovery options.', 'Inspect run'],
  budget_exhausted: ['Run budget reached', 'The run completed after reaching a configured budget.', 'View report'],
  finished: ['Run finished', 'The final report and durable wrap-up are ready.', 'View report'],
  stopped: ['Run finalized', 'The run was intentionally stopped and its durable wrap-up is ready.', 'View report'],
  finalization_stalled: ['Finalization needs recovery', 'The engine stopped before durable wrap-up completed.', 'Open Events'],
  stalled: ['Run engine stopped', 'No engine process is advancing this run.', 'Open Events'],
  train_monitor: ['Training looks broken', 'The live-log monitor judged this training likely wasted. Open the run to inspect the log and verdict.', 'Inspect training'],
  // THE SERVER'S MEASURED DETAIL IS NOW USED, and the marker that stood here prescribed the wrong
  // fix (corrected 2026-08-29). It said to publish "two bounded NUMERIC hour fields and format them
  // here" — but `serve/attention.py` ALREADY computes the sentence from its own measurements
  // ("projected to overrun by 4.2h beyond the deadline grace against a 10.0h wall"), and what threw
  // it away was this module's `const [title, detail, actionLabel] = COPY[kind]`, which reads the
  // table and never the payload. Adding numeric fields would have duplicated arithmetic the server
  // had already done; `MEASURED_DETAIL_KINDS` below reads what it sent.
  // THE UNTRUSTED-PROSE BOUNDARY IS KEPT AND IS THE WHOLE REASON THIS IS AN ALLOW-LIST. The server
  // states the rule at the `train_overrun` row: its numbers are "the engine's OWN measurement of
  // its own stage — a span, an ETA and a declared wall — not model-authored log prose, so they may
  // ride in the envelope where the health family's verdict text deliberately may not". `train_monitor`
  // is exactly that other family: its detail quotes an LLM's verdict about a candidate's own log, so
  // it stays on the COPY table and no server string may replace it.
  // The row below survives as the FALLBACK for a payload that carries no detail (an older server,
  // or a row whose detail failed sanitisation).
  train_overrun: ['Experiment will miss its wall', 'This experiment is projected to be killed by its own deadline before it finishes. Raise the wall or stop it.', 'Inspect training'],
  asha: ['ASHA rank warning', 'Inspect the live curve. Automatic stopping requires peers at the same declared progress.', 'Inspect experiment'],
  assistant_permission: ['Assistant approval needed', 'Open Assistant to review the exact action and scope.', 'Open Assistant'],
})

const safeRunId = value => typeof value === 'string' && value.length > 0 && value.length <= 255
  && !CONTROL_RE.test(value) ? value : ''
// WHICH KINDS MAY SPEAK FOR THEMSELVES. Membership means the server's `detail` for that kind is
// built from the ENGINE's own measurements, never from model-authored text. Adding a kind here is a
// trust decision, not a formatting one — `train_monitor` is deliberately absent because its detail
// quotes an LLM verdict about a candidate's own log.
export const MEASURED_DETAIL_KINDS = new Set(['train_overrun'])

// A server detail is untrusted TEXT even when its numbers are trusted: bounded, single-line, and
// control-free, on the same rule `safeContextText` applies one line down. It is deliberately more
// generous in length (a measured sentence is longer than a label) and returns '' on anything it
// cannot vouch for, which sends the caller back to the COPY table.
const safeDetailText = value => typeof value === 'string' && value.trim().length > 0
  && value.trim().length <= 400 && !CONTROL_RE.test(value) ? value.trim() : ''

const safeContextText = value => typeof value === 'string' && value.trim().length > 0
  && value.trim().length <= 160 && !CONTROL_RE.test(value) ? value.trim() : ''
const safeInteger = value => Number.isSafeInteger(value) && value >= 0 ? value : null
const safeTime = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0

export function attentionHref(item) {
  if (!item || item.source !== 'run' || !safeRunId(item.runId)
      || !GENERATION_RE.test(item.generation || '')) return null
  const state = { ...emptyRunRouteState(), generation: item.generation }
  const exactNodeId = safeInteger(item.nodeId)
  const exactNodeAttempt = safeInteger(item.nodeGeneration)
  const hasExactNode = exactNodeId != null && exactNodeAttempt != null
  if (item.kind === 'approval' && hasExactNode) state.nodeId = exactNodeId
  else if (item.kind === 'approval') state.panel = 'events'
  else if (item.kind === 'finished' || item.kind === 'budget_exhausted'
      || item.kind === 'stopped') state.view = 'report'
  else if (item.kind === 'failure_spike') {
    state.panel = 'failures'
    if (hasExactNode) state.nodeId = exactNodeId
  } else if (item.kind === 'run_failed') {
    state.panel = 'failures'
    if (hasExactNode) state.nodeId = exactNodeId
  } else if ((item.kind === 'train_monitor' || item.kind === 'train_overrun'
              || item.kind === 'asha') && hasExactNode) {
    state.nodeId = exactNodeId          // deep-link to the evaluating node (its live training curve)
  } else state.panel = 'events'
  // A run generation can contain several lifecycles for the same numeric node after a reset/retry.
  // Target a node only when the feed carries both halves of its lifecycle identity; incomplete or
  // future-schema notifications degrade to a safe run-level panel rather than the current attempt.
  if (state.nodeId != null) state.nodeGeneration = exactNodeAttempt
  const query = encodeRunRouteState(state, { forceGeneration: true })
  return `#/run/${encodeURIComponent(item.runId)}${query ? `?${query}` : ''}`
}

export function normalizeRunAttention(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const id = typeof raw.id === 'string' && RUN_ID_RE.test(raw.id) ? raw.id : ''
  const kind = typeof raw.kind === 'string' && ATTENTION_KINDS.has(raw.kind) ? raw.kind : ''
  const severity = typeof raw.severity === 'string' && SEVERITIES.has(raw.severity)
    ? raw.severity : ''
  const runId = safeRunId(raw.run_id)
  const generation = typeof raw.generation === 'string' && GENERATION_RE.test(raw.generation)
    ? raw.generation : ''
  const seq = safeInteger(raw.seq)
  if (!id || !kind || !severity || !runId || !generation || seq == null
      || typeof raw.active !== 'boolean' || typeof raw.browser !== 'boolean'
      || typeof raw.derived !== 'boolean'
      || (raw.stale !== undefined && typeof raw.stale !== 'boolean')) return null
  const nodeId = raw.node_id == null ? null : safeInteger(raw.node_id)
  const nodeGeneration = raw.node_generation == null ? null : safeInteger(raw.node_generation)
  if ((raw.node_id != null && nodeId == null) || (raw.node_generation != null && nodeGeneration == null)) return null
  if (kind === 'approval' && (nodeId == null || nodeGeneration == null)) return null
  const [title, fallbackDetail, actionLabel] = COPY[kind]
  // The server's sentence when this kind is allowed to speak and actually said something;
  // the table otherwise. Neither branch can produce an empty detail.
  const measured = MEASURED_DETAIL_KINDS.has(kind) ? safeDetailText(raw.detail) : ''
  const detail = measured || fallbackDetail
  const runLabel = safeContextText(raw.run_label)
  const taskId = safeContextText(raw.task_id)
  const item = {
    id, source: 'run', kind, severity, title, detail, actionLabel,
    runId, generation, seq, created: safeTime(raw.created), active: raw.active,
    notifyEligible: raw.browser === true && raw.derived === false && raw.stale !== true,
    derived: raw.derived, stale: raw.stale === true, nodeId, nodeGeneration,
    runLabel, taskId, contextLabel: runLabel || runId,
  }
  return { ...item, href: attentionHref(item), needsAction: NEEDS_ACTION.has(kind) }
}

export function normalizePermissionAttention(raw, now = Date.now()) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const requestId = typeof raw.id === 'string' && PERMISSION_ID_RE.test(raw.id) ? raw.id : ''
  const session = typeof raw.session === 'string' && SESSION_ID_RE.test(raw.session) ? raw.session : ''
  const created = safeTime(raw.created)
  const expires = safeTime(raw.expires_at)
  if (!requestId || !session || !created || !expires || expires * 1000 <= now) return null
  const [title, detail, actionLabel] = COPY.assistant_permission
  return {
    id: `perm_${requestId}`, requestId, session, source: 'permission',
    kind: 'assistant_permission', severity: 'action', title, detail, actionLabel,
    created, expiresAt: expires, active: true, notifyEligible: true, derived: false,
    href: null, needsAction: true,
  }
}

export function sortAttentionItems(items) {
  const unique = new Map()
  for (const item of items || []) if (item?.id) unique.set(item.id, item)
  return [...unique.values()].sort((left, right) => {
    if (left.needsAction !== right.needsAction) return left.needsAction ? -1 : 1
    if (left.active !== right.active) return left.active ? -1 : 1
    const severity = (SEVERITY_PRIORITY[right.severity] || 0)
      - (SEVERITY_PRIORITY[left.severity] || 0)
    const leftSeq = Number.isSafeInteger(left.seq) ? left.seq : -1
    const rightSeq = Number.isSafeInteger(right.seq) ? right.seq : -1
    return severity || right.created - left.created || rightSeq - leftSeq
      || right.id.localeCompare(left.id)
  })
}

export function normalizeAttentionSources(attentionPayload, permissionsPayload, now = Date.now()) {
  const runs = Array.isArray(attentionPayload?.items)
    ? attentionPayload.items.map(normalizeRunAttention).filter(Boolean) : []
  const permissions = Array.isArray(permissionsPayload?.pending)
    ? permissionsPayload.pending.map(item => normalizePermissionAttention(item, now)).filter(Boolean) : []
  return sortAttentionItems([...runs, ...permissions])
}

export const attentionIdValid = value => (typeof value === 'string'
  && (RUN_ID_RE.test(value) || /^perm_[0-9a-f]{16}$/.test(value)))
