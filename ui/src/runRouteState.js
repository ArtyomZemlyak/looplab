// Canonical, fragment-only diagnostic state for a run workspace. Keeping both owner state and the
// review bearer after `#` prevents node ids, filters, and capability material from reaching HTTP
// request targets, referrers, proxy logs, or server analytics.

export const RUN_ROUTE_TABS = ['Overview', 'Comments', 'Trials', 'Trace', 'Code', 'Metrics', 'Trust', 'Cost']
export const RUN_ROUTE_PANELS = [
  'overview', 'queue', 'hypotheses', 'research', 'failures', 'trust', 'pareto', 'data',
  'compare', 'sensitivity', 'importance', 'crossrun', 'artifacts', 'registry', 'memory',
  'collab', 'authoring', 'events', 'gpu', 'config',
]
export const TIMELINE_KIND_ORDER = [
  'proposal', 'eval', 'decision', 'research', 'report', 'trust', 'control', 'lifecycle',
]
export const REVIEW_SUMMARY_TABS = ['Overview', 'Comments', 'Trust', 'Cost']
export const REVIEW_EVIDENCE_TABS = ['Overview', 'Comments', 'Code', 'Trust', 'Cost']
export const REVIEW_SAFE_PANEL_NAMES = [
  'overview', 'trust', 'sensitivity', 'importance', 'failures', 'pareto', 'data', 'compare', 'collab',
]

const TAB_FROM_WIRE = new Map(RUN_ROUTE_TABS.map(tab => [tab.toLowerCase(), tab]))
const TAB_TO_WIRE = new Map(RUN_ROUTE_TABS.map(tab => [tab, tab.toLowerCase()]))
const PANEL_SET = new Set(RUN_ROUTE_PANELS)
const REVIEW_PANEL_SET = new Set(REVIEW_SAFE_PANEL_NAMES)
const KIND_SET = new Set(TIMELINE_KIND_ORDER)
const KNOWN_KEYS = new Set(['gen', 'view', 'node', 'attempt', 'tab', 'comment', 'panel', 'focus', 'seq', 'q', 'kinds'])
const GENERATION_RE = /^[0-9a-f]{64}$/
const COMMENT_ID_RE = /^[A-Za-z0-9_-]{8,160}$/
const INTEGER_RE = /^(0|[1-9][0-9]*)$/
const CONTROL_RE = /[\u0000-\u001f\u007f]/
const MAX_FOCUS_CHARS = 160
const MAX_FILTER_CHARS = 500

export const emptyRunRouteState = () => ({
  generation: null,
  view: 'dag',
  nodeId: null,
  nodeGeneration: null,
  inspectTab: 'Overview',
  commentId: null,
  panel: null,
  directionFilter: null,
  sequence: null,
  timelineFilter: '',
  timelineKinds: [],
})

export function splitRouteHash(hash = '') {
  const raw = String(hash || '').replace(/^#/, '')
  const marker = raw.indexOf('?')
  return marker < 0
    ? { path: raw, query: '' }
    : { path: raw.slice(0, marker), query: raw.slice(marker + 1) }
}

export function routeHashPath(hash = '') {
  const path = splitRouteHash(hash).path
  return path ? `#${path}` : ''
}

function single(params, key, issues) {
  const values = params.getAll(key)
  if (values.length > 1) {
    issues.push(`Duplicate “${key}” was ignored.`)
    return null
  }
  return values.length === 1 ? values[0] : null
}

function integer(value, key, issues) {
  if (value == null || value === '') return null
  if (!INTEGER_RE.test(value)) {
    issues.push(`Invalid ${key} was ignored.`)
    return null
  }
  const parsed = Number(value)
  if (!Number.isSafeInteger(parsed)) {
    issues.push(`${key} is outside the supported range and was ignored.`)
    return null
  }
  return parsed
}

function boundedText(value, key, max, issues) {
  if (value == null || value === '') return null
  if (value.length > max || CONTROL_RE.test(value)) {
    issues.push(`${key} is not a safe URL value and was ignored.`)
    return null
  }
  return value
}

function normalizeKinds(value, issues) {
  if (value == null || value === '') return []
  const raw = value.split(',')
  if (raw.some(kind => !KIND_SET.has(kind)) || new Set(raw).size !== raw.length) {
    issues.push('Unknown or duplicate timeline kinds were ignored.')
    return []
  }
  const selected = new Set(raw)
  return TIMELINE_KIND_ORDER.filter(kind => selected.has(kind))
}

export function sanitizeRunRouteState(input = {}, { reviewMode = false } = {}) {
  const state = emptyRunRouteState()
  if (GENERATION_RE.test(String(input.generation || ''))) state.generation = String(input.generation)
  if (input.view === 'report' || (!reviewMode && input.view === 'concepts')) state.view = input.view
  if (Number.isSafeInteger(input.nodeId) && input.nodeId >= 0) state.nodeId = input.nodeId
  const nodeGeneration = state.nodeId != null && Number.isSafeInteger(input.nodeGeneration)
    && input.nodeGeneration >= 0 ? input.nodeGeneration : null
  if (state.nodeId != null && RUN_ROUTE_TABS.includes(input.inspectTab)) state.inspectTab = input.inspectTab
  if (state.nodeId != null && nodeGeneration != null && state.inspectTab === 'Comments'
      && typeof input.commentId === 'string' && COMMENT_ID_RE.test(input.commentId)) {
    state.nodeGeneration = nodeGeneration
    state.commentId = input.commentId
  }
  if (PANEL_SET.has(input.panel)) state.panel = input.panel
  if (typeof input.directionFilter === 'string' && input.directionFilter.length > 0
      && input.directionFilter.length <= MAX_FOCUS_CHARS && !CONTROL_RE.test(input.directionFilter)) {
    state.directionFilter = input.directionFilter
  }
  if (!reviewMode && Number.isSafeInteger(input.sequence) && input.sequence >= 0 && state.generation) {
    state.sequence = input.sequence
  }
  if (!reviewMode && typeof input.timelineFilter === 'string'
      && input.timelineFilter.length <= MAX_FILTER_CHARS && !CONTROL_RE.test(input.timelineFilter)) {
    // Do NOT trim here: this value is bound directly to the Dock filter's controlled <input>, so
    // trimming on every keystroke drops the trailing space of a multi-word filter ("node failed"
    // would collapse to "nodefailed"). The feed filters on filter.trim() at the use site, and the
    // URL parse path (parseRunRoute) trims persisted values, so interior/trailing spaces are only
    // preserved live while typing. Mirrors directionFilter above, which was never trimmed.
    state.timelineFilter = input.timelineFilter
  }
  if (!reviewMode && Array.isArray(input.timelineKinds)) {
    const selected = new Set(input.timelineKinds.filter(kind => KIND_SET.has(kind)))
    state.timelineKinds = TIMELINE_KIND_ORDER.filter(kind => selected.has(kind))
  }
  return state
}

export function reviewInspectorTabs(evidence = false) {
  return evidence ? REVIEW_EVIDENCE_TABS : REVIEW_SUMMARY_TABS
}

export function reviewPanelAllowed(panel, evidence = false) {
  return REVIEW_PANEL_SET.has(panel) && (panel !== 'compare' || evidence)
}

export function reviewRouteStateForScope(input, { evidence = false } = {}) {
  const state = sanitizeRunRouteState(input, { reviewMode: true })
  const tabs = reviewInspectorTabs(evidence)
  if (!tabs.includes(state.inspectTab)) state.inspectTab = 'Overview'
  if (state.panel && !reviewPanelAllowed(state.panel, evidence)) state.panel = null
  return state
}

export function parseRunRouteState(hash = '', { reviewMode = false } = {}) {
  const { query } = splitRouteHash(hash)
  const params = new URLSearchParams(query)
  const issues = []
  for (const key of new Set(params.keys())) {
    if (!KNOWN_KEYS.has(key)) issues.push(`Unknown “${key}” link state was ignored.`)
  }

  const state = emptyRunRouteState()
  const generation = single(params, 'gen', issues)
  if (generation != null) {
    if (GENERATION_RE.test(generation)) state.generation = generation
    else issues.push('Invalid run generation was ignored.')
  }
  const view = single(params, 'view', issues)
  if (view != null && view !== '') {
    if (view === 'report') state.view = 'report'
    else if (view === 'concepts') {
      if (reviewMode) issues.push('Concept view is unavailable in review links.')
      else state.view = 'concepts'
    }
    else if (view !== 'dag') issues.push('Unknown workspace view was ignored.')
  }
  state.nodeId = integer(single(params, 'node', issues), 'node id', issues)
  const attempt = integer(single(params, 'attempt', issues), 'node attempt', issues)
  if (attempt != null) {
    if (state.nodeId == null) issues.push('Node attempt without a node was ignored.')
    else state.nodeGeneration = attempt
  }
  const tab = single(params, 'tab', issues)
  if (tab != null && tab !== '') {
    const decoded = TAB_FROM_WIRE.get(tab)
    if (!decoded) issues.push('Unknown Inspector tab was ignored.')
    else if (state.nodeId == null) issues.push('Inspector tab without a node was ignored.')
    else state.inspectTab = decoded
  }
  const comment = single(params, 'comment', issues)
  if (comment != null && comment !== '') {
    if (!COMMENT_ID_RE.test(comment)) issues.push('Invalid comment target was ignored.')
    else if (state.nodeId == null || state.nodeGeneration == null
        || state.inspectTab !== 'Comments') {
      issues.push('Comment target without the matching node attempt and Comments tab was ignored.')
    } else state.commentId = comment
  }
  if (state.nodeGeneration != null && state.commentId == null) {
    issues.push('Node attempt without a valid comment target was ignored.')
    state.nodeGeneration = null
  }
  const panel = single(params, 'panel', issues)
  if (panel != null && panel !== '') {
    if (PANEL_SET.has(panel)) state.panel = panel
    else issues.push('Unknown panel was ignored.')
  }
  state.directionFilter = boundedText(single(params, 'focus', issues), 'direction filter', MAX_FOCUS_CHARS, issues)

  const rawSequence = single(params, 'seq', issues)
  const rawFilter = single(params, 'q', issues)
  const rawKinds = single(params, 'kinds', issues)
  if (reviewMode) {
    if (rawSequence != null || rawFilter != null || rawKinds != null) {
      issues.push('Timeline history and raw-event filters are unavailable in review links.')
    }
  } else {
    const sequence = integer(rawSequence, 'sequence', issues)
    if (sequence != null && !state.generation) {
      issues.push('Historical sequence without a generation fence was ignored.')
    } else state.sequence = sequence
    state.timelineFilter = boundedText(rawFilter, 'timeline filter', MAX_FILTER_CHARS, issues)?.trim() || ''
    state.timelineKinds = normalizeKinds(rawKinds, issues)
  }
  const sanitized = sanitizeRunRouteState(state, { reviewMode })
  if (!sanitized.generation && runRouteStateHasTarget(sanitized, { reviewMode })) {
    issues.push('Diagnostic state without a generation fence was ignored.')
    return { state: emptyRunRouteState(), issues, hadState: query.length > 0 }
  }
  return { state: sanitized, issues, hadState: query.length > 0 }
}

export function runRouteStateHasTarget(state, { reviewMode = false } = {}) {
  const value = sanitizeRunRouteState(state, { reviewMode })
  return value.view !== 'dag' || value.nodeId != null || value.panel != null
    || value.directionFilter != null || value.sequence != null
    || (!reviewMode && Number.isSafeInteger(state?.sequence) && state.sequence >= 0)
    || value.timelineFilter.trim() !== '' || value.timelineKinds.length > 0
}

export function encodeRunRouteState(input, { reviewMode = false, forceGeneration = false } = {}) {
  const state = sanitizeRunRouteState(input, { reviewMode })
  const params = new URLSearchParams()
  if (state.generation && (forceGeneration || runRouteStateHasTarget(state, { reviewMode }))) {
    params.set('gen', state.generation)
  }
  if (state.view === 'report' || state.view === 'concepts') params.set('view', state.view)
  if (state.nodeId != null) {
    params.set('node', String(state.nodeId))
    if (state.nodeGeneration != null) params.set('attempt', String(state.nodeGeneration))
    if (state.inspectTab !== 'Overview') params.set('tab', TAB_TO_WIRE.get(state.inspectTab))
    if (state.inspectTab === 'Comments' && state.commentId) params.set('comment', state.commentId)
  }
  if (state.panel) params.set('panel', state.panel)
  if (state.directionFilter) params.set('focus', state.directionFilter)
  if (!reviewMode && state.sequence != null && state.generation) params.set('seq', String(state.sequence))
  // Trim only when writing the canonical URL: the live state keeps interior/trailing spaces so the
  // Dock filter input can be typed left-to-right, but the shareable link stays canonical (and the
  // parse path also trims, so a copied link round-trips identically).
  const canonicalFilter = state.timelineFilter.trim()
  if (!reviewMode && canonicalFilter) params.set('q', canonicalFilter)
  if (!reviewMode && state.timelineKinds.length) params.set('kinds', state.timelineKinds.join(','))
  return params.toString()
}

export function hashWithRunRouteState(hash, state, options = {}) {
  const { path } = splitRouteHash(hash)
  const query = encodeRunRouteState(state, options)
  return `#${path}${query ? `?${query}` : ''}`
}

export function hrefWithRunRouteState(locationLike, state, options = {}) {
  const hash = hashWithRunRouteState(locationLike.hash || '', state, options)
  return `${locationLike.pathname || ''}${locationLike.search || ''}${hash}`
}

export function sameRunRouteState(left, right) {
  return encodeRunRouteState(left, { forceGeneration: true })
    === encodeRunRouteState(right, { forceGeneration: true })
}

export function reconcileRunRouteStateUpdate(current, raw, {
  generation = null, reviewMode = false,
} = {}) {
  let candidate = { ...emptyRunRouteState(), ...raw }
  if (!candidate.generation && generation && runRouteStateHasTarget(candidate, { reviewMode })) {
    candidate.generation = generation
  }
  candidate = sanitizeRunRouteState(candidate, { reviewMode })
  // An explicit `?gen=A` with otherwise-default state is meaningful. A click on the already-active
  // Search view (or any other semantic no-op) must not silently turn that exact link into a live alias.
  if (sameRunRouteState(current, candidate)) return current
  if (!runRouteStateHasTarget(candidate, { reviewMode })) candidate.generation = null
  return sameRunRouteState(current, candidate) ? current : candidate
}
