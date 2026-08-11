// Canonical, fragment-only diagnostic state for a run workspace. Keeping both owner state and the
// review bearer after `#` prevents node ids, filters, and capability material from reaching HTTP
// request targets, referrers, proxy logs, or server analytics.

export const RUN_ROUTE_TABS = ['Overview', 'Comments', 'Trials', 'Trace', 'Code', 'Metrics', 'Trust', 'Cost']
export const RUN_ROUTE_PANELS = [
  'overview', 'queue', 'research', 'operations', 'failures', 'trust', 'pareto', 'data',
  'compare', 'sensitivity', 'importance', 'crossrun', 'artifacts', 'registry', 'memory',
  'collab', 'authoring', 'events', 'gpu', 'config',
]
// The Card board is no longer a panel: it is the `cards` workspace view. `?panel=hypotheses` was a
// live deep link (and the run menu's own spelling), so it is MIGRATED rather than rejected —
// dropping it from RUN_ROUTE_PANELS alone would make every saved link report "Unknown panel was
// ignored" and land on the graph. This is the one legacy panel name with a view successor; see
// `sanitizeRunRouteState`, which is where the translation happens so that BOTH the parse path and
// an in-memory `route.update({ panel: 'hypotheses' })` from stale code reach the same place.
export const LEGACY_PANEL_VIEWS = { hypotheses: 'cards' }
export const RUN_ROUTE_VIEWS = ['dag', 'cards', 'concepts', 'report']
// Views a review bearer may reach. `cards` joins `concepts` on the owner-only side: the Card board
// is an operator CONTROL surface (edit/priority/resources/drop/abandon) and `hypotheses` was never
// in REVIEW_SAFE_PANEL_NAMES, so promoting it to a view must not quietly widen the review scope.
export const REVIEW_SAFE_VIEWS = ['dag', 'report']
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
const KNOWN_KEYS = new Set(
  ['gen', 'view', 'card', 'node', 'attempt', 'tab', 'comment', 'panel', 'focus', 'seq', 'q', 'kinds'])
const VIEW_SET = new Set(RUN_ROUTE_VIEWS)
const REVIEW_VIEW_SET = new Set(REVIEW_SAFE_VIEWS)
const GENERATION_RE = /^[0-9a-f]{64}$/
const COMMENT_ID_RE = /^[A-Za-z0-9_-]{8,160}$/
const INTEGER_RE = /^(0|[1-9][0-9]*)$/
const CONTROL_RE = /[\u0000-\u001f\u007f]/
const MAX_FOCUS_CHARS = 160
const MAX_FILTER_CHARS = 500

export const emptyRunRouteState = () => ({
  generation: null,
  view: 'dag',
  cardId: null,
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

// A Card id as the wire actually bounds it: `core/models.py::_read_bounded_card_id` admits a
// non-empty printable string of at most 256 chars that equals its own trim, and the UI already
// spells the same rule as `CardBoard.jsx::canonicalHypothesisId`. Kept as a positive shape test
// rather than a charset allow-list because card ids are minted server-side (`card-N` today) and a
// narrower client rule would make a legitimate future id silently unlinkable.
export function canonicalCardId(value) {
  return typeof value === 'string' && value === value.trim() && value.length > 0
    && [...value].length <= 256 && !/\p{C}/u.test(value)
}

export function sanitizeRunRouteState(input = {}, { reviewMode = false } = {}) {
  const state = emptyRunRouteState()
  if (GENERATION_RE.test(String(input.generation || ''))) state.generation = String(input.generation)
  // The ONE place `?panel=hypotheses` becomes `view=cards`, so the parse path and any in-memory
  // `route.update({ panel: 'hypotheses' })` left in older code reach the same successor instead of
  // one of them silently landing on the graph. Applied before the view is read so an explicit
  // `?view=…` alongside the legacy panel still wins — the panel is the weaker, deprecated signal.
  // `Object.hasOwn`, never a bare index: `input.panel` is untrusted route text, and `constructor`
  // /`toString`/`__proto__` all resolve to a truthy inherited member that would pass the
  // `if (requestedView …)` test below and be assigned as `state.view` — the same guard the parse
  // path already applies to this exact table.
  const migratedView = !reviewMode && Object.hasOwn(LEGACY_PANEL_VIEWS, String(input.panel))
    ? LEGACY_PANEL_VIEWS[String(input.panel)] : undefined
  const requestedView = VIEW_SET.has(input.view) && input.view !== 'dag' ? input.view : migratedView
  if (requestedView && (!reviewMode || REVIEW_VIEW_SET.has(requestedView))) state.view = requestedView
  if (state.view === 'cards' && canonicalCardId(input.cardId)) state.cardId = input.cardId
  if (Number.isSafeInteger(input.nodeId) && input.nodeId >= 0) state.nodeId = input.nodeId
  const nodeGeneration = state.nodeId != null && Number.isSafeInteger(input.nodeGeneration)
    && input.nodeGeneration >= 0 ? input.nodeGeneration : null
  if (nodeGeneration != null) state.nodeGeneration = nodeGeneration
  if (state.nodeId != null && RUN_ROUTE_TABS.includes(input.inspectTab)) state.inspectTab = input.inspectTab
  if (state.nodeId != null && state.nodeGeneration != null && state.inspectTab === 'Comments'
      && typeof input.commentId === 'string' && COMMENT_ID_RE.test(input.commentId)) {
    state.commentId = input.commentId
  }
  if (PANEL_SET.has(input.panel)) state.panel = input.panel
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
  // Whether the URL actually STATED a view, which `sanitizeRunRouteState` cannot recover: its input
  // is `{...emptyRunRouteState(), ...raw}` on the in-memory path, so a defaulted `view: 'dag'` and an
  // explicit `?view=dag` reach it identically. That is why the legacy-panel migration below is
  // resolved HERE — the alternative (`input.view !== 'dag'` standing in for "a view was requested")
  // silently turns `?view=dag&panel=hypotheses` into the Card board.
  let explicitView = false
  if (view != null && view !== '') {
    if (!VIEW_SET.has(view)) issues.push('Unknown workspace view was ignored.')
    else if (reviewMode && !REVIEW_VIEW_SET.has(view)) {
      // Name the refused view rather than a generic message: a reviewer handed a `#…?view=cards`
      // link needs to know the board is owner-only, not that their link was malformed.
      issues.push(`The ${view === 'cards' ? 'Card board' : 'Concept'} view is unavailable in review links.`)
    } else { state.view = view; explicitView = true }
  }
  const card = single(params, 'card', issues)
  if (card != null && card !== '') {
    if (!canonicalCardId(card)) issues.push('Invalid Card target was ignored.')
    // A Card target is meaningless outside the board, and silently switching the view for it would
    // let `?view=report&card=…` yank the operator off the report. Refuse rather than redirect.
    else if (state.view !== 'cards') issues.push('Card target without the Card board view was ignored.')
    else state.cardId = card
  }
  state.nodeId = integer(single(params, 'node', issues), 'node id', issues)
  const attemptSupplied = params.has('attempt')
  const attempt = integer(single(params, 'attempt', issues), 'node attempt', issues)
  if (attemptSupplied && attempt == null) {
    issues.push(state.nodeId == null
      ? 'Invalid node attempt was ignored.'
      : 'The node target had an invalid attempt and was not opened.')
    state.nodeId = null
  } else if (attempt != null) {
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
  // `attempt` is now also a standalone exact node-lifecycle fence. A supplied comment is an exact
  // compound target, however, so rejecting any part of it (including a missing attempt) must reject
  // the node too instead of silently opening that numeric id's current lifecycle.
  if (params.has('comment') && state.commentId == null) {
    issues.push('The comment target was invalid and its experiment was not opened.')
    state.nodeId = null
    state.nodeGeneration = null
    state.inspectTab = 'Overview'
    state.commentId = null
  }
  const panel = single(params, 'panel', issues)
  if (panel != null && panel !== '') {
    if (PANEL_SET.has(panel)) state.panel = panel
    // `?panel=hypotheses` is a live link shape (it was the run menu's own spelling until the board
    // became a view). It is carried through as the panel value so `sanitizeRunRouteState` — the ONE
    // owner of the translation — turns it into `view=cards`, and it must NOT report "Unknown panel"
    // on the way: a migrated link is honoured, not diagnosed. An explicit `?view=` still wins there.
    // Carried only when no explicit `?view=` was stated: the panel is the weaker, deprecated signal,
    // and `?view=dag&panel=hypotheses` must land on the graph the link asked for. Dropping it here
    // is not a diagnostic — the link is honoured, its migration is simply superseded.
    else if (Object.hasOwn(LEGACY_PANEL_VIEWS, panel)) { if (!explicitView) state.panel = panel }
    else issues.push('Unknown panel was ignored.')
  }
  const legacyFocus = boundedText(single(params, 'focus', issues), 'legacy Direction focus', MAX_FOCUS_CHARS, issues)
  if (legacyFocus) {
    issues.push('Legacy Direction focus is no longer supported; use the Concepts filter instead.')
  }

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
    || value.sequence != null
    || (!reviewMode && Number.isSafeInteger(state?.sequence) && state.sequence >= 0)
    || value.timelineFilter.trim() !== '' || value.timelineKinds.length > 0
}

export function encodeRunRouteState(input, { reviewMode = false, forceGeneration = false } = {}) {
  const state = sanitizeRunRouteState(input, { reviewMode })
  const params = new URLSearchParams()
  if (state.generation && (forceGeneration || runRouteStateHasTarget(state, { reviewMode }))) {
    params.set('gen', state.generation)
  }
  if (state.view !== 'dag') params.set('view', state.view)
  if (state.cardId) params.set('card', state.cardId)
  if (state.nodeId != null) {
    params.set('node', String(state.nodeId))
    if (state.nodeGeneration != null) params.set('attempt', String(state.nodeGeneration))
    if (state.inspectTab !== 'Overview') params.set('tab', TAB_TO_WIRE.get(state.inspectTab))
    if (state.inspectTab === 'Comments' && state.commentId) params.set('comment', state.commentId)
  }
  if (state.panel) params.set('panel', state.panel)
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
  generation = null, reviewMode = false, forceGeneration = false,
} = {}) {
  let candidate = { ...emptyRunRouteState(), ...raw }
  if (!candidate.generation && generation && runRouteStateHasTarget(candidate, { reviewMode })) {
    candidate.generation = generation
  }
  candidate = sanitizeRunRouteState(candidate, { reviewMode })
  // An explicit `?gen=A` with otherwise-default state is meaningful. A click on the already-active
  // Search view (or any other semantic no-op) must not silently turn that exact link into a live alias.
  if (sameRunRouteState(current, candidate)) return current
  if (!forceGeneration && !runRouteStateHasTarget(candidate, { reviewMode })) candidate.generation = null
  return sameRunRouteState(current, candidate) ? current : candidate
}
