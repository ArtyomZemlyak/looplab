const DRAFT_FIELDS = Object.freeze([
  'proposal_id', 'run_id', 'source', 'task_file', 'task_json', 'settings_json', 'rationale', 'setup_steps',
])

const segment = value => encodeURIComponent(String(value == null || value === '' ? 'unsaved' : value))

// Keys never depend on editable run_id.  Stable proposal ids win; legacy proposals fall back to the
// owning Assistant message and ordinal, scoped by session so two chats cannot share a draft.
export function launchDraftKey({ sessionId, messageId, messageIndex, proposalId, proposalIndex }) {
  const owner = proposalId
    ? `proposal:${proposalId}`
    : `message:${messageId || messageIndex}:proposal:${proposalIndex}`
  return `${segment(sessionId)}::${segment(owner)}`
}

export function launchDraftSession(identity) {
  const value = String(identity || '')
  const separator = value.indexOf('::')
  if (separator < 1) return ''
  try {
    const sessionId = decodeURIComponent(value.slice(0, separator))
    return sessionId === 'unsaved' ? '' : sessionId
  } catch { return '' }
}

export function retainedLaunchDraft(draft) {
  if (!draft || typeof draft !== 'object' || Array.isArray(draft)) return null
  const clean = {}
  for (const field of DRAFT_FIELDS) {
    if (!(field in draft)) continue
    clean[field] = field === 'setup_steps'
      ? (Array.isArray(draft[field]) ? draft[field].map(value => String(value)) : [])
      : String(draft[field] == null ? '' : draft[field])
  }
  return clean
}

export function retainLaunchDraft(store, key, draft, limit = 50) {
  const clean = retainedLaunchDraft(draft)
  if (!key || !clean) return store || {}
  const next = { ...(store || {}), [key]: clean }
  const keys = Object.keys(next)
  for (const stale of keys.slice(0, Math.max(0, keys.length - limit))) delete next[stale]
  return next
}

export function removeLaunchDraft(store, key) {
  if (!store?.[key]) return store || {}
  const next = { ...store }
  delete next[key]
  return next
}

export function clearLaunchDraftSession(store, sessionId) {
  const prefix = `${segment(sessionId)}::`
  return Object.fromEntries(Object.entries(store || {}).filter(([key]) => !key.startsWith(prefix)))
}
