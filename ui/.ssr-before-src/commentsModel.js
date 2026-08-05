const RUN_GENERATION_RE = /^[0-9a-f]{64}$/
const COMMENT_ID_RE = /^[A-Za-z0-9_-]{8,160}$/
const CONTROL_RE = /[\u0000-\u001f\u007f]/

export const COMMENT_MAX_BYTES = 8 * 1024

const ACTOR_LABELS = Object.freeze({
  deployment_owner: 'Deployment owner',
  local_operator: 'Local operator',
  legacy_unknown: 'Legacy note',
})

const safeInteger = value => Number.isSafeInteger(value) && value >= 0 ? value : null
// Date#toISOString throws outside the ECMAScript TimeClip range (Â±8.64e15 ms). Event logs are
// locally editable, so treat an oversized timestamp as a malformed authoritative page instead of
// allowing one row to crash the whole comments surface during render.
const safeTime = value => typeof value === 'number' && Number.isFinite(value)
  && value >= 0 && value <= 8.64e12
  ? value : null
const safeCursor = value => typeof value === 'string' && value.length > 0 && value.length <= 512
  && !CONTROL_RE.test(value) ? value : null

export function utf8Bytes(value) {
  const text = typeof value === 'string' ? value : ''
  if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(text).length
  // Node/browser fallback for the pure model tests and older embedded WebViews.
  try { return unescape(encodeURIComponent(text)).length } // eslint-disable-line no-undef
  catch { return Number.POSITIVE_INFINITY }
}

export function validUnicode(value) {
  if (typeof value !== 'string') return false
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index)
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(index + 1)
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false
      index += 1
    } else if (unit >= 0xdc00 && unit <= 0xdfff) return false
  }
  return true
}

export function commentIdValid(value) {
  return typeof value === 'string' && COMMENT_ID_RE.test(value)
}

export function commentDraftState(value) {
  const text = typeof value === 'string' ? value : ''
  const bytes = utf8Bytes(text)
  const invalidUnicode = !validUnicode(text)
  return {
    text,
    bytes,
    remaining: COMMENT_MAX_BYTES - bytes,
    empty: text.trim().length === 0,
    invalidUnicode,
    tooLarge: bytes > COMMENT_MAX_BYTES,
    valid: text.trim().length > 0 && !invalidUnicode && bytes <= COMMENT_MAX_BYTES,
  }
}

function actorKind(value, legacy) {
  if (legacy) return 'legacy_unknown'
  return Object.hasOwn(ACTOR_LABELS, value) && value !== 'legacy_unknown' ? value : null
}

export function normalizeComment(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const id = raw.comment_id ?? raw.id
  const nodeId = safeInteger(raw.node_id ?? raw.nodeId)
  const nodeGeneration = safeInteger(raw.node_generation ?? raw.nodeGeneration)
  const version = safeInteger(raw.version)
  const createdAt = safeTime(raw.created_at ?? raw.createdAt)
  const updatedAt = safeTime(raw.updated_at ?? raw.updatedAt)
  const legacy = raw.legacy === true
  const text = raw.text
  if (!commentIdValid(id) || nodeId == null || (!legacy && nodeGeneration == null)
      || version == null || version < 1
      || createdAt == null || updatedAt == null || updatedAt < createdAt
      || typeof text !== 'string' || !validUnicode(text) || utf8Bytes(text) > COMMENT_MAX_BYTES
      || typeof raw.resolved !== 'boolean' || typeof raw.editable !== 'boolean') return null
  const kind = actorKind(raw.actor_kind ?? raw.actorKind, legacy)
  if (!kind) return null
  return Object.freeze({
    id,
    nodeId,
    nodeGeneration,
    text,
    actorKind: kind,
    actorLabel: ACTOR_LABELS[kind],
    version,
    resolved: raw.resolved,
    createdAt,
    updatedAt,
    legacy,
    editable: raw.editable && !legacy,
  })
}

export function normalizeCommentsPage(payload, expectedGeneration = null) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)
      || !Array.isArray(payload.comments)
      || !RUN_GENERATION_RE.test(payload.run_generation || '')
      || typeof payload.has_more !== 'boolean') return null
  if (expectedGeneration && payload.run_generation !== expectedGeneration) return null
  const comments = payload.comments.map(normalizeComment)
  // One malformed row makes the whole page non-authoritative. Callers retain their last-safe page.
  if (comments.some(comment => comment == null)) return null
  const nextCursor = payload.next_cursor == null ? null : safeCursor(payload.next_cursor)
  if ((payload.has_more && !nextCursor) || (!payload.has_more && payload.next_cursor != null)) return null
  return Object.freeze({
    comments,
    runGeneration: payload.run_generation,
    hasMore: payload.has_more,
    nextCursor,
  })
}

export function normalizeCommentVersion(raw, current) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || !current) return null
  const version = safeInteger(raw.version)
  const updatedAt = safeTime(raw.updated_at ?? raw.created_at ?? raw.ts)
  const text = raw.text ?? current.text
  const resolved = raw.resolved ?? current.resolved
  const legacy = raw.legacy === true || current.legacy
  if (version == null || version < 1 || updatedAt == null || typeof text !== 'string'
      || !validUnicode(text) || utf8Bytes(text) > COMMENT_MAX_BYTES
      || typeof resolved !== 'boolean') return null
  const kind = actorKind(raw.actor_kind ?? raw.actorKind ?? current.actorKind, legacy)
  if (!kind) return null
  const action = ['created', 'edited', 'resolved', 'reopened', 'legacy'].includes(raw.action)
    ? raw.action : version === 1 ? 'created' : 'edited'
  return Object.freeze({
    version,
    text,
    resolved,
    updatedAt,
    actorKind: kind,
    actorLabel: ACTOR_LABELS[kind],
    action,
  })
}

export function normalizeCommentHistory(payload, current, expectedGeneration = null) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)
      || payload.comment_id !== current?.id || !Array.isArray(payload.versions)
      || !RUN_GENERATION_RE.test(payload.run_generation || '')
      || typeof payload.has_more !== 'boolean') return null
  if (expectedGeneration && payload.run_generation !== expectedGeneration) return null
  const versions = payload.versions.map(version => normalizeCommentVersion(version, current))
  if (versions.some(version => version == null)) return null
  const nextCursor = payload.next_cursor == null ? null : safeCursor(payload.next_cursor)
  if ((payload.has_more && !nextCursor) || (!payload.has_more && payload.next_cursor != null)) return null
  return Object.freeze({
    versions,
    runGeneration: payload.run_generation,
    hasMore: payload.has_more,
    nextCursor,
  })
}

export function mergeCommentPages(pages) {
  const byId = new Map()
  for (const page of pages || []) {
    for (const comment of page || []) if (comment?.id) byId.set(comment.id, comment)
  }
  return [...byId.values()]
}

export function commentMatchesSubject(comment, nodeId, nodeGeneration) {
  if (nodeId == null) return true
  return Number.isSafeInteger(nodeId) && nodeId >= 0
    && Number.isSafeInteger(nodeGeneration) && nodeGeneration >= 0
    && comment?.legacy === false
    && comment.nodeId === nodeId
    && comment.nodeGeneration === nodeGeneration
}

export function filterComments(comments, filter = 'open') {
  const list = Array.isArray(comments) ? comments : []
  if (filter === 'resolved') return list.filter(comment => comment.resolved)
  if (filter === 'all') return list
  return list.filter(comment => !comment.resolved)
}

export function commentConflict(error) {
  return [
    'comment_version_conflict', 'comment_version_changed', 'comment_revision_changed', 'comment_conflict',
  ].includes(error?.code) && (error?.status == null || error.status === 409)
}

export function commentMutationError(error, fallback = 'Comment could not be saved.') {
  if (commentConflict(error)) {
    return 'This comment changed in another tab. Your draft is preserved.'
  }
  if (error?.code === 'run_generation_changed') {
    return 'This link targets an earlier run generation. Your draft is preserved.'
  }
  if (error?.code === 'command_in_progress') {
    return 'Another run command is still in progress. Your draft is preserved.'
  }
  if (error?.code === 'comment_subject_changed' || error?.code === 'node_generation_changed') {
    return 'This experiment attempt changed. Your draft is preserved; return to the current attempt.'
  }
  if (['collaboration_concurrency_busy', 'comment_concurrency_busy'].includes(error?.code)) {
    return 'The comment is changing too quickly to update safely. Your draft is preserved; refresh and retry.'
  }
  if (error?.code === 'event_lock_unavailable') {
    return 'Comment writes are temporarily unavailable. Your draft is preserved; retry shortly.'
  }
  if (error?.commandUnknown === true
      || ['accepted', 'executing'].includes(error?.commandRecord?.status)) {
    return 'The command outcome is not known yet. Your draft is preserved; refresh comments before creating another intent.'
  }
  return fallback
}
