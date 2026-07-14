import { emptyRunRouteState, encodeRunRouteState } from './runRouteState.js'

const RUN_ID_RE = /^[0-9a-f]{64}$/
const GENERATION_RE = /^[0-9a-f]{64}$/
const PERMISSION_ID_RE = /^[0-9a-f]{16}$/
const SESSION_ID_RE = /^[0-9a-f]{16}$/
const CONTROL_RE = /[\u0000-\u001f\u007f]/

export const ATTENTION_KINDS = new Set([
  'approval', 'approval_incomplete', 'spec_approval', 'failure_spike', 'run_failed',
  'budget_exhausted', 'finished', 'stopped', 'finalization_stalled', 'stalled',
])
const SEVERITIES = new Set(['action', 'warning', 'danger', 'success'])
const NEEDS_ACTION = new Set([
  'approval', 'approval_incomplete', 'spec_approval', 'failure_spike', 'run_failed',
  'finalization_stalled', 'stalled', 'assistant_permission',
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
  assistant_permission: ['Assistant approval needed', 'Open Assistant to review the exact action and scope.', 'Open Assistant'],
})

const safeRunId = value => typeof value === 'string' && value.length > 0 && value.length <= 255
  && !CONTROL_RE.test(value) ? value : ''
const safeInteger = value => Number.isSafeInteger(value) && value >= 0 ? value : null
const safeTime = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0

export function attentionHref(item) {
  if (!item || item.source !== 'run' || !safeRunId(item.runId)
      || !GENERATION_RE.test(item.generation || '')) return null
  const state = { ...emptyRunRouteState(), generation: item.generation }
  if (item.kind === 'approval') state.nodeId = item.nodeId
  else if (item.kind === 'finished' || item.kind === 'budget_exhausted'
      || item.kind === 'stopped') state.view = 'report'
  else if (item.kind === 'failure_spike') {
    state.panel = 'failures'
    if (item.nodeId != null) state.nodeId = item.nodeId
  } else if (item.kind === 'run_failed' && item.nodeId != null) {
    state.panel = 'failures'; state.nodeId = item.nodeId
  } else state.panel = 'events'
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
  const [title, detail, actionLabel] = COPY[kind]
  const item = {
    id, source: 'run', kind, severity, title, detail, actionLabel,
    runId, generation, seq, created: safeTime(raw.created), active: raw.active,
    notifyEligible: raw.browser === true && raw.derived === false && raw.stale !== true,
    derived: raw.derived, stale: raw.stale === true, nodeId, nodeGeneration,
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
    return right.created - left.created || right.id.localeCompare(left.id)
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
