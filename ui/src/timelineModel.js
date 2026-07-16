// Pure state transitions for the bounded event timeline.  The server cursor is deliberately opaque:
// the browser never derives byte offsets or trusts event sequence numbers as pagination tokens.

export const TIMELINE_PAGE_SIZE = 200
export const TIMELINE_RETAIN_LIMIT = 5_000
export const TIMELINE_CATCHUP_DELAYS = Object.freeze([100, 200, 400])
export const TIMELINE_DIRECTIONS = new Set(['tail', 'older', 'newer', 'around'])

const finiteSeq = value => value !== null && value !== undefined && value !== ''
  && Number.isSafeInteger(Number(value)) && Number(value) >= 0
const validEventCount = value => typeof value === 'number'
  && Number.isSafeInteger(value) && value >= 0 ? value : null
const GENERATION_RE = /^[0-9a-f]{64}$/
const generationValue = value => {
  if (value == null || value === '') return null
  const generation = String(value)
  if (!GENERATION_RE.test(generation)) throw new TypeError('Timeline generation must be 64 lowercase hex characters.')
  return generation
}

export const timelineConflictMatchesGeneration = (error, displayedGeneration) =>
  GENERATION_RE.test(String(displayedGeneration || ''))
  && error?.status === 409
  && String(error?.detail?.actual_generation || '') === String(displayedGeneration)

export function timelineEventKey(event) {
  if (finiteSeq(event?.seq)) return `seq:${Number(event.seq)}`
  if (typeof event?.id === 'string' && event.id) return `id:${event.id}`
  // Malformed rows are not merged together.  They remain visible for diagnosis, but page-local
  // position is intentionally part of the fallback key rather than pretending they have a seq.
  return null
}

export function createTimelineState({ retainLimit = TIMELINE_RETAIN_LIMIT } = {}) {
  return {
    rows: [], segments: [], generation: null,
    cursors: { older: null, newer: null },
    hasMore: { older: false, newer: false },
    status: 'idle', errors: { tail: null, older: null, newer: null, around: null },
    loading: { tail: false, older: false, newer: false, around: false },
    followingTail: true, windowAtTail: false, unread: 0, unreadUnknown: false,
    parkedFrontier: null, parkedTotalEvents: null, parkedEventCount: null,
    liveEventCount: null, tornTail: false,
    totalEvents: null, range: null, sourceTailLimited: false,
    requestedAnchor: null, matchedSeq: null,
    // One server page is at most 500 rows. Retaining whole pages keeps every boundary paired with
    // the exact opaque cursor that produced it; partial-page eviction would make that cursor unsafe.
    retainLimit: Math.max(500, Number(retainLimit) || TIMELINE_RETAIN_LIMIT),
    revision: 0, generationChanged: false,
  }
}

const cursorValue = value => value == null || value === '' ? null : String(value)
const booleanValue = (primary, fallback = false) => primary == null ? !!fallback : primary === true

// Normalize both the nested contract and the transitional top-level spelling.  Keeping this adapter
// here prevents Dock and EventExplorer from inventing subtly different pagination semantics.
export function normalizeTimelinePage(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new TypeError('Timeline page must be an object.')
  }
  const rows = payload.events ?? payload.rows
  if (!Array.isArray(rows)) throw new TypeError('Timeline page events must be an array.')
  let previousSeq = -1
  for (const row of rows) {
    if (!row || typeof row !== 'object' || Array.isArray(row) || !finiteSeq(row.seq)) {
      throw new TypeError('Every timeline event must be an object with a non-negative integer seq.')
    }
    const seq = Number(row.seq)
    if (seq <= previousSeq) throw new TypeError('Timeline page seq values must be strictly increasing.')
    previousSeq = seq
  }
  const cursors = payload.cursors || {}
  const hasMore = payload.has_more || payload.hasMore || {}
  const generation = generationValue(payload.generation)
  const totalEvents = payload.total_events == null ? null : Number(payload.total_events)
  if (totalEvents != null && (!Number.isSafeInteger(totalEvents) || totalEvents < 0)) {
    throw new TypeError('Timeline total_events must be a non-negative integer.')
  }
  return {
    rows,
    generation,
    cursors: {
      older: cursorValue(cursors.older ?? payload.older_cursor ?? payload.cursor_older),
      newer: cursorValue(cursors.newer ?? payload.newer_cursor ?? payload.cursor_newer),
    },
    hasMore: {
      older: booleanValue(hasMore.older ?? payload.has_older),
      newer: booleanValue(hasMore.newer ?? payload.has_newer),
    },
    tornTail: payload.torn_tail === true || payload.tornTail === true,
    sourceTailLimited: payload.source_tail_limited === true,
    totalEvents,
    range: payload.range && typeof payload.range === 'object' ? payload.range : null,
    anchorSeq: finiteSeq(payload.anchor_seq) ? Number(payload.anchor_seq) : null,
    matchedSeq: finiteSeq(payload.matched_seq) ? Number(payload.matched_seq) : null,
  }
}

export function timelineRequestStarted(state, direction) {
  if (!TIMELINE_DIRECTIONS.has(direction)) throw new TypeError(`Unknown timeline direction: ${direction}`)
  return {
    ...state,
    status: state.rows.length ? 'ready' : 'loading',
    // The hook serializes requests with one AbortController. Starting a new direction cancels the
    // previous one, so no stale loading flag may survive that hand-off and freeze live polling.
    loading: { tail: false, older: false, newer: false, around: false, [direction]: true },
    errors: { ...state.errors, [direction]: null },
  }
}

export function timelineRequestFailed(state, direction, error) {
  if (!TIMELINE_DIRECTIONS.has(direction)) throw new TypeError(`Unknown timeline direction: ${direction}`)
  return {
    ...state,
    status: state.rows.length ? 'ready' : 'error',
    loading: { ...state.loading, [direction]: false },
    errors: { ...state.errors, [direction]: error?.status === 401 || error?.status === 403
      ? 'Owner access is required to load timeline events.'
      : direction === 'older' ? 'Older timeline events could not be loaded.'
        : direction === 'newer' ? 'New timeline events could not be loaded.'
          : direction === 'around' ? 'Replay timeline events could not be loaded.'
            : 'Timeline events could not be loaded.' },
    windowAtTail: direction === 'tail' || direction === 'newer' ? false : state.windowAtTail,
  }
}

// Prefer the generation-scoped folded-state count over seq. Sequence numbers may legitimately contain
// gaps after repair, while legacy servers without event_count still need the old wake-up fallback.
export function timelineBehindLive(state, liveSeq, liveEventCount) {
  const observedCount = validEventCount(liveEventCount)
  const loadedCount = validEventCount(state?.totalEvents)
  if (observedCount != null && loadedCount != null) return observedCount > loadedCount
  if (!finiteSeq(liveSeq)) return false
  const loadedSeq = state?.rows?.reduce(
    (max, row) => finiteSeq(row?.seq) ? Math.max(max, Number(row.seq)) : max, -1) ?? -1
  return Number(liveSeq) > loadedSeq
}

const orderedRows = (previous, incoming) => {
  const merged = new Map()
  const malformed = []
  for (const row of [...previous, ...incoming]) {
    const key = timelineEventKey(row)
    if (key == null) malformed.push(row)
    else merged.set(key, row)
  }
  const rows = [...merged.values()]
  rows.sort((a, b) => Number(a.seq) - Number(b.seq))
  return rows.concat(malformed)
}

const knownKeys = rows => new Set(rows.map(timelineEventKey).filter(Boolean))

function flattenSegments(segments) {
  const rows = []
  let lastSeq = -1
  for (const segment of segments) {
    const firstSeq = segment.rows.length ? Number(segment.rows[0].seq) : null
    if (firstSeq != null && firstSeq <= lastSeq) {
      return orderedRows([], segments.flatMap(item => item.rows))
    }
    rows.push(...segment.rows)
    if (segment.rows.length) lastSeq = Number(segment.rows.at(-1).seq)
  }
  return rows
}

const pageSegment = page => ({ rows: page.rows, cursors: page.cursors, hasMore: page.hasMore })
const segmentRowCount = segments => segments.reduce((count, segment) => count + segment.rows.length, 0)

function retainedSegments(state, page, direction, replace) {
  let segments = state.segments?.length ? state.segments : (state.rows.length
    ? [{ rows: state.rows, cursors: state.cursors, hasMore: state.hasMore }] : [])
  const incoming = pageSegment(page)
  if (replace) segments = [incoming]
  else if (page.rows.length > 0) {
    segments = direction === 'older' ? [incoming, ...segments] : [...segments, incoming]
  } else if (segments.length > 0) {
    // An empty poll refreshes only its OUTER edge. Do not append an empty segment which could later
    // become a false boundary after retention evicts adjacent pages.
    if (direction === 'older') {
      const first = segments[0]
      segments = [{ ...first, cursors: { ...first.cursors, older: page.cursors.older },
        hasMore: { ...first.hasMore, older: page.hasMore.older } }, ...segments.slice(1)]
    } else {
      const last = segments.at(-1)
      segments = [...segments.slice(0, -1), { ...last,
        cursors: { ...last.cursors, newer: page.cursors.newer },
        hasMore: { ...last.hasMore, newer: page.hasMore.newer } }]
    }
  } else segments = [incoming]

  // Evict only complete server pages. Their boundary cursors travel with them, preventing both
  // skipped rows and pagination loops when the reader reverses direction later.
  while (segments.length > 1 && segmentRowCount(segments) > state.retainLimit) {
    if (direction === 'older') segments.pop()
    else segments.shift()
  }
  // Conforming pages are <=500 rows, so only a hostile response or the 50k stress fixture can enter
  // this branch. Keep memory bounded; normal cursor alignment is unaffected.
  if (segments.length === 1 && segments[0].rows.length > state.retainLimit) {
    const segment = segments[0]
    segments = [{ ...segment, rows: direction === 'older'
      ? segment.rows.slice(0, state.retainLimit) : segment.rows.slice(-state.retainLimit) }]
  }
  return segments
}

export function mergeTimelinePage(state, payload, { direction = 'tail' } = {}) {
  if (!TIMELINE_DIRECTIONS.has(direction)) throw new TypeError(`Unknown timeline direction: ${direction}`)
  const page = normalizeTimelinePage(payload)
  const changedGeneration = !!state.generation && !!page.generation && state.generation !== page.generation
  if (changedGeneration) {
    // A delta from another generation may begin in the middle of a rewritten file. It is never a
    // replacement snapshot: clear the fenced view and let the hook obtain a fresh tail.
    return {
      ...createTimelineState({ retainLimit: state.retainLimit }),
      status: 'loading', generationChanged: true, followingTail: state.followingTail,
    }
  }
  const replace = direction === 'tail' || direction === 'around'
  const previous = replace ? [] : state.rows
  const incomingFirst = page.rows.length ? Number(page.rows[0].seq) : null
  const incomingLast = page.rows.length ? Number(page.rows.at(-1).seq) : null
  const previousFirst = previous.length ? Number(previous[0].seq) : null
  const previousLast = previous.length ? Number(previous.at(-1).seq) : null
  const disjoint = previous.length === 0 || page.rows.length === 0
    || (direction === 'older' && incomingLast < previousFirst)
    || (direction === 'newer' && incomingFirst > previousLast)
  const before = disjoint ? null : knownKeys(previous)
  const added = disjoint ? page.rows.length : page.rows.reduce((count, row) => {
    const key = timelineEventKey(row)
    return count + (key != null && !before.has(key) ? 1 : 0)
  }, 0)
  const segments = retainedSegments(state, page, direction, replace)
  const rows = flattenSegments(segments)
  const first = segments[0] || pageSegment(page)
  const last = segments.at(-1) || first
  const cursors = { older: first.cursors.older, newer: last.cursors.newer }
  const hasMore = { older: first.hasMore.older, newer: last.hasMore.newer }
  const nextGeneration = page.generation ?? state.generation
  const followingTail = state.followingTail
  const windowAtTail = !hasMore.newer
  const unreadAdded = direction === 'newer' && !followingTail ? added : 0
  const nextTotalEvents = page.totalEvents ?? state.totalEvents
  const totalUnread = !followingTail && state.parkedTotalEvents != null && nextTotalEvents != null
    ? Math.max(0, nextTotalEvents - state.parkedTotalEvents) : null
  const observedEventCount = validEventCount(state.liveEventCount)
  const parkedEventCount = validEventCount(state.parkedEventCount)
  // A non-monotonic/corrupt tail is intentionally excluded by the pager but may still be accepted
  // by the folded projection. Do not present a reconciled exact timeline count across that boundary.
  const tornCountMismatch = page.tornTail && observedEventCount != null && nextTotalEvents != null
    && observedEventCount > nextTotalEvents
  const countUnread = !followingTail && !tornCountMismatch
    && parkedEventCount != null && observedEventCount != null
    ? Math.max(0, observedEventCount - parkedEventCount) : null
  const exactUnread = countUnread ?? (tornCountMismatch ? null : totalUnread)
  return {
    ...state,
    rows, segments,
    generation: nextGeneration,
    cursors,
    hasMore,
    status: 'ready',
    loading: { ...state.loading, [direction]: false },
    errors: replace
      ? { tail: null, older: null, newer: null, around: null }
      : { ...state.errors, [direction]: null },
    tornTail: page.tornTail,
    sourceTailLimited: page.sourceTailLimited,
    totalEvents: nextTotalEvents,
    range: page.range ?? state.range,
    requestedAnchor: direction === 'around' ? page.anchorSeq : replace ? null : state.requestedAnchor,
    matchedSeq: direction === 'around' ? page.matchedSeq : replace ? null : state.matchedSeq,
    windowAtTail,
    followingTail,
    unread: followingTail ? 0 : exactUnread ?? (state.unread + unreadAdded),
    unreadUnknown: followingTail ? false : tornCountMismatch ? true
      : exactUnread != null ? false : state.unreadUnknown,
    revision: state.revision + 1,
    generationChanged: false,
  }
}

export function setTimelineFollowing(state, following) {
  const value = following === true
  if (state.followingTail === value && (!value || state.unread === 0)) return state
  return { ...state, followingTail: value, unread: value ? 0 : state.unread,
    unreadUnknown: value ? false : state.unreadUnknown,
    parkedFrontier: value ? null : state.parkedFrontier,
    parkedTotalEvents: value ? null : state.parkedTotalEvents,
    parkedEventCount: value ? null : state.parkedEventCount,
    liveEventCount: value ? null : state.liveEventCount }
}

export function parkTimeline(state, liveSeq = null, liveEventCount = null) {
  const frontier = finiteSeq(liveSeq) ? Number(liveSeq) : state.rows.reduce((max, row) => Math.max(max, Number(row.seq)), -1)
  if (!state.followingTail && (state.parkedFrontier != null || state.parkedEventCount != null
      || state.parkedTotalEvents != null)) return state
  const eventCount = validEventCount(liveEventCount)
  return { ...state, followingTail: false, unread: 0, unreadUnknown: false,
    parkedFrontier: frontier >= 0 ? frontier : null, parkedTotalEvents: state.totalEvents,
    parkedEventCount: eventCount, liveEventCount: eventCount }
}

// SSE tells us a newer frontier exists even when the reader is parked in an around/history page.
// This is an estimate based on monotonic event seq and is replaced by exact dedupe counts whenever a
// newer page is fetched.
export function noticeTimelineFrontier(state, liveSeq, liveEventCount = null) {
  const eventCount = validEventCount(liveEventCount)
  if (state.followingTail || (!finiteSeq(liveSeq) && eventCount == null)) return state
  const parked = finiteSeq(state.parkedFrontier) ? Number(state.parkedFrontier)
    : finiteSeq(liveSeq) ? Number(liveSeq) : null
  const baseline = validEventCount(state.parkedEventCount)
  // Count growth without any sequence advance cannot be a canonical append. It is the earliest
  // observable signal of a duplicate/decreasing tail, before a page read can publish torn_tail.
  const nonAdvancingCount = baseline != null && eventCount != null && eventCount > baseline
    && finiteSeq(state.parkedFrontier) && finiteSeq(liveSeq)
    && Number(liveSeq) <= Number(state.parkedFrontier)
  const inferredTornTail = state.tornTail || nonAdvancingCount
  const tornCountMismatch = inferredTornTail && eventCount != null && state.totalEvents != null
    && eventCount > state.totalEvents
  const exactUnread = !tornCountMismatch && !nonAdvancingCount && baseline != null && eventCount != null
    ? Math.max(0, eventCount - baseline) : null
  return { ...state, parkedFrontier: parked,
    liveEventCount: eventCount ?? state.liveEventCount, windowAtTail: false,
    tornTail: inferredTornTail,
    unread: exactUnread ?? state.unread, unreadUnknown: exactUnread == null }
}

export function timelineContainsSeq(state, seq) {
  if (!finiteSeq(seq) || state.rows.length === 0) return false
  const target = Number(seq)
  return state.rows.some(row => Number(row?.seq) === target)
}

export function timelinePagePath(runId, {
  direction = 'tail', cursor = null, generation = null, anchorSeq = null,
  limit = TIMELINE_PAGE_SIZE, byteLimit = null,
} = {}) {
  if (!TIMELINE_DIRECTIONS.has(direction)) throw new TypeError(`Unknown timeline direction: ${direction}`)
  const params = new URLSearchParams({ direction, limit: String(Math.max(1, Math.min(500, Number(limit) || TIMELINE_PAGE_SIZE))) })
  if (cursor) params.set('cursor', String(cursor))
  if (generation) params.set('generation', String(generation))
  if (byteLimit != null) params.set('byte_limit', String(Math.max(1_024, Math.min(524_288, Number(byteLimit) || 262_144))))
  if (direction === 'around') {
    if (!finiteSeq(anchorSeq)) throw new TypeError('around timeline requests require anchorSeq')
    params.set('anchor_seq', String(Number(anchorSeq)))
  }
  return `/api/runs/${encodeURIComponent(String(runId))}/log-page?${params}`
}
