import { buildingMarkers, evalStageFor, evalStageLabel,
  evalStageShortLabel } from './buildingModel.js'

// The public activity vocabulary is deliberately independent of the node's terminal-status enum.
// `pending` alone does not establish ownership by either the build or evaluation lane.
export const NODE_ACTIVITY = Object.freeze({
  BUILDING: 'building',
  QUEUED: 'queued',
  EVALUATING: 'evaluating',
  PENDING: 'pending',
  EVALUATED: 'evaluated',
  FAILED: 'failed',
})

const ACTIVITY_VALUES = new Set(Object.values(NODE_ACTIVITY))

const integer = value => typeof value !== 'boolean' && value !== null && value !== ''
  && Number.isInteger(Number(value))

function markerFor(state, node) {
  if (!node || !integer(node.id)) return null
  return buildingMarkers(state).find(marker => {
    if (Number(marker.node_id) !== Number(node.id)) return false
    // A marker without a generation is the legacy shape. When both sides carry one, refuse a stale
    // reset marker rather than claiming the new lifecycle is still being built.
    if (!Object.hasOwn(marker, 'generation') || !integer(node.attempt)) return true
    return Number(marker.generation) === Number(node.attempt)
  }) || null
}

export function nodeActivityStatus(node, state = null) {
  if (!node) return NODE_ACTIVITY.PENDING
  if (markerFor(state, node)) return NODE_ACTIVITY.BUILDING

  const activity = node.activity
  const recorded = activity && ACTIVITY_VALUES.has(activity.status) ? activity.status : null
  const sameGeneration = recorded && integer(activity?.generation) && integer(node.attempt)
    && Number(activity.generation) === Number(node.attempt)
  if (recorded && sameGeneration) return recorded

  // Compatibility with older servers and synthetic build nodes. Crucially, an old `pending` node
  // stays UNKNOWN: without the creator's boundary promise, silence is not evidence that it is queued.
  if (node.status === 'building' || node.building === true) return NODE_ACTIVITY.BUILDING
  if (node.status === 'evaluated') return NODE_ACTIVITY.EVALUATED
  if (node.status === 'failed') return NODE_ACTIVITY.FAILED
  return NODE_ACTIVITY.PENDING
}

const runStopped = state => !!state && (state.finished || state.stop_requested
  || state.phase === 'finalizing' || state.engine_running === false)

const stopLabel = (state, subject) => {
  if (state?.engine_running === false) return `${subject} interrupted · engine stopped`
  if (state?.stop_requested) return `${subject} stopping · stop requested`
  if (state?.phase === 'finalizing') return `${subject} no longer running · run is finalizing`
  return `${subject} no longer running · run finished`
}

const runCanWork = state => !!state && !runStopped(state) && !state.paused
  // Historical state explicitly carries null. An omitted field is an older server and preserves the
  // pre-existing live behaviour; null is a known snapshot and must never pulse as work happening now.
  && state.engine_running !== null

export function nodeActivityView(node, state = null, log = null) {
  const status = nodeActivityStatus(node, state)
  const stopped = runStopped(state)
  const paused = !!state?.paused
  const historical = state?.engine_running === null
  const active = runCanWork(state)

  if (status === NODE_ACTIVITY.BUILDING) {
    if (historical) return { status, tone: 'historical', active: false,
      label: 'Building at this point', shortLabel: 'building' }
    if (stopped) return { status, tone: 'interrupted', active: false,
      label: stopLabel(state, 'Build'), shortLabel: state?.stop_requested ? 'build stopping' : 'build interrupted' }
    if (paused) return { status, tone: 'paused', active: false,
      label: 'Build paused', shortLabel: 'build paused' }
    return { status, tone: 'building', active, label: 'Building code', shortLabel: 'building' }
  }
  if (status === NODE_ACTIVITY.EVALUATING) {
    if (historical) return { status, tone: 'historical', active: false,
      label: 'Evaluating at this point', shortLabel: 'evaluating' }
    if (stopped) return { status, tone: 'interrupted', active: false,
      label: stopLabel(state, 'Evaluation'), shortLabel: state?.stop_requested ? 'eval stopping' : 'eval interrupted' }
    if (paused) return { status, tone: 'paused', active: false,
      label: 'Evaluation paused', shortLabel: 'eval paused' }
    // WHICH STEP of the evaluation, when the run's own cursor says. `Training / evaluating` was one
    // label for an entire multi-hour pipeline, and on `mine` -> `train` -> `score` it is false for
    // two thirds of it — the flat word is what made "which node is training?" unanswerable from any
    // screen. `stage` rides on the view so a surface can render the step without re-decoding the
    // log, and `label`/`shortLabel` stay the ONE place the words are chosen.
    //
    // The fallback is unchanged and stays deliberately vague: with no beacon (an older engine, a
    // windowed log whose `started` scrolled out, a task that runs no staged eval) the only honest
    // answer is still that evaluation owns the node.
    const stage = log ? evalStageFor(node, log) : null
    if (stage) return { status, tone: 'evaluating', active, stage,
      label: evalStageLabel(stage), shortLabel: evalStageShortLabel(stage) }
    return { status, tone: 'evaluating', active, stage: null,
      label: 'Training / evaluating', shortLabel: 'training / eval' }
  }
  if (status === NODE_ACTIVITY.QUEUED) {
    let suffix = ''
    if (historical) suffix = ' at this point'
    else if (state?.engine_running === false) suffix = ' · engine stopped'
    else if (state?.stop_requested) suffix = ' · stop requested'
    else if (state?.phase === 'finalizing') suffix = ' · run finalizing'
    else if (state?.finished) suffix = ' · run finished'
    else if (paused) suffix = ' · run paused'
    return { status, tone: 'queued', active: false,
      label: `Waiting for evaluation slot${suffix}`, shortLabel: 'waiting for slot' }
  }
  if (status === NODE_ACTIVITY.PENDING) {
    return { status, tone: 'unknown', active: false,
      label: 'Pending · evaluation start unknown', shortLabel: 'start unknown' }
  }
  if (status === NODE_ACTIVITY.EVALUATED) {
    return { status, tone: 'evaluated', active: false,
      label: 'Evaluated', shortLabel: 'evaluated' }
  }
  return { status, tone: 'failed', active: false, label: 'Failed', shortLabel: 'failed' }
}

export function workingNodeIds(state) {
  const ids = new Set()
  for (const node of Object.values(state?.nodes || {})) {
    if (nodeActivityView(node, state).active) ids.add(Number(node.id))
  }
  return ids
}

export function partitionNodeWork(state) {
  const buckets = { building: [], evaluating: [], queued: [], unknown: [] }
  for (const node of Object.values(state?.nodes || {})) {
    const status = nodeActivityStatus(node, state)
    if (status === NODE_ACTIVITY.BUILDING) buckets.building.push(node)
    else if (status === NODE_ACTIVITY.EVALUATING) buckets.evaluating.push(node)
    else if (status === NODE_ACTIVITY.QUEUED) buckets.queued.push(node)
    else if (node?.status === 'pending') buckets.unknown.push(node)
  }
  for (const nodes of Object.values(buckets)) nodes.sort((a, b) => Number(a.id) - Number(b.id))
  return buckets
}
