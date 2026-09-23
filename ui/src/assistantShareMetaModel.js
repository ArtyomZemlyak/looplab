// What this browser accepts as an Assistant chat's PUBLIC-LINK metadata (review 2026-09-22, UI-06):
// the share-id and share-URL grammars, the bounded id lists, the internal consistency a session's
// `shared` / `share_count` / `share_live` / `share_expires_at` fields must have before any of them is
// shown, the conflict codes that pause a turn over a changed live-link set, and the clipboard
// fallback a minted link may be kept as. Moved verbatim out of `AssistantBar.jsx`, where it was
// module-private; `test/assistantShareMetaModel.test.js` drives it. Pure apart from reading `location`
// (the deployment a link belongs to) at call time.

export const ASSISTANT_SHARE_ID_RE = /^[0-9a-f]{32}$/
export const ASSISTANT_SHARE_URL_RE = /^#\/assistant\/shared\/([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$/
export const ASSISTANT_SHARE_IDS_MAX = 4096
export const validAssistantShareId = value => typeof value === 'string' && ASSISTANT_SHARE_ID_RE.test(value)
export const boundedAssistantShareIds = value => {
  if (!Array.isArray(value) || value.length > ASSISTANT_SHARE_IDS_MAX) return null
  const ids = []
  const seen = new Set()
  for (const id of value) {
    if (!validAssistantShareId(id) || seen.has(id)) return null
    seen.add(id); ids.push(id)
  }
  return ids
}
export const assistantShareIds = meta => boundedAssistantShareIds(meta?.share_ids)
export const assistantLiveShareIds = meta => boundedAssistantShareIds(meta?.live_share_ids)
export const validAssistantShareMeta = meta => {
  const ids = assistantShareIds(meta)
  const liveIds = assistantLiveShareIds(meta)
  const shareSet = ids == null ? null : new Set(ids)
  if (ids == null || liveIds == null
      || liveIds.some(id => !shareSet.has(id))
      || typeof meta.shared !== 'boolean' || meta.shared !== (ids.length > 0)
      || !Number.isInteger(meta.share_count) || meta.share_count !== ids.length
      || typeof meta.share_live !== 'boolean' || meta.share_live !== (liveIds.length > 0)) return false
  return ids.length
    ? Number.isFinite(meta.share_expires_at) && meta.share_expires_at > 0
    : meta.share_expires_at == null
}
export const assistantLiveShareAckRequired = error => error?.status === 409
  && error?.code === 'assistant_live_share_ack_required'
export const assistantLiveShareRecoveryFailure = {
  message: 'Saved Assistant turn paused because the live public-link set changed. Verify its status, then retry this exact turn.',
  notice: 'Saved Assistant turn paused · live public-link status changed',
  blocked: false,
}
// A saved create identity belongs to ONE deployment served from ONE path: two LoopLabs open in the
// same tab must not read each other's envelopes, and a chat id is only unique within a deployment.
// (`assistantShareReceipt` used to live here; the receipt is now checked against what this browser
// DERIVES rather than against the shape of the answer — `assistantShareRecovery.js`.)
export const shareRecoveryScope = () => `${location.origin}${location.pathname}`
export const validAssistantShareFallback = value => {
  if (!value || !validAssistantShareId(value.shareId) || !Number.isFinite(value.expiresAt)
      || value.expiresAt <= 0 || typeof value.url !== 'string') return false
  try {
    const parsed = new URL(value.url)
    const match = ASSISTANT_SHARE_URL_RE.exec(parsed.hash)
    return parsed.origin === location.origin && parsed.pathname === location.pathname
      && !parsed.search && match?.[1] === value.shareId
  } catch { return false }
}
