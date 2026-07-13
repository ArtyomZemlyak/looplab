// Assistant turn recovery is an identity operation, not a new chat send.  A persisted user turn may
// carry model-only context in `raw` (open run, experiment refs, attachment contents) and always pins
// the permission mode that governed the original attempt.  Reconstructing either value from the
// visible bubble/current selector can change the intent or silently widen permissions.

const RECOVERY_MODES = new Set(['plan', 'default', 'acceptEdits', 'auto'])

export function danglingAssistantTurn(messages) {
  const trailing = Array.isArray(messages) ? messages[messages.length - 1] : null
  return trailing && trailing.role === 'user' && typeof trailing.turn_id === 'string'
    && trailing.turn_id.length > 0 ? trailing : null
}

export function assistantRecoveryPayload(turn) {
  if (!turn || turn.role !== 'user' || typeof turn.turn_id !== 'string' || !turn.turn_id) return null
  const display = typeof turn.content === 'string' ? turn.content : null
  const instruction = turn.raw == null || turn.raw === '' ? display : turn.raw
  const mode = typeof turn.mode === 'string' ? turn.mode : null
  if (!display || typeof instruction !== 'string' || !instruction || !RECOVERY_MODES.has(mode)) return null
  return { instruction, display, mode }
}

export function assistantReplyCompletesTurn(messages, prior) {
  if (!Array.isArray(messages) || !prior || prior.role !== 'user') return false
  const payload = prior.retryPayload || {}
  let userIndex = -1
  if (typeof prior.turn_id === 'string' && prior.turn_id) {
    userIndex = messages.findIndex(message => message?.role === 'user'
      && message.turn_id === prior.turn_id)
  } else if (Number.isInteger(payload.historyLength) && payload.historyLength >= 0) {
    userIndex = payload.historyLength
  }
  const durableUser = messages[userIndex]
  const durableReply = messages[userIndex + 1]
  if (!durableUser || durableUser.role !== 'user' || durableReply?.role !== 'assistant') return false
  if (typeof prior.turn_id === 'string' && prior.turn_id) return durableUser.turn_id === prior.turn_id
  const durableRaw = durableUser.raw || durableUser.content
  return durableUser.content === prior.content && durableRaw === payload.raw && durableUser.mode === payload.mode
}

export const unavailableAssistantRecovery = Object.freeze({
  blocked: true,
  message: '(saved turn recovery is blocked: its durable instruction or permission mode is unavailable. Start a new chat to continue safely.)',
  notice: 'The saved Assistant turn cannot be recovered safely; start a new chat',
})

// Only authoritative HTTP failures terminate polling. A lost response/5xx is ambiguous: the exact
// recovery POST may already own the server turn, so the UI keeps observing the transcript instead of
// issuing another logical turn. A plain 409 means an existing worker won the race and is also observed.
export function assistantRecoveryFailure(error) {
  if (!error || error.name === 'AbortError' || !Number.isFinite(Number(error.status))) return null
  if (error.status >= 500 || [408, 425, 429].includes(error.status)) return null
  if (error.status === 409 && ![
    'assistant_turn_recovery_mismatch', 'assistant_turn_recovery_required',
  ].includes(error.code)) return null
  if (error.code === 'assistant_turn_recovery_mismatch') return {
    blocked: true,
    message: '(saved turn recovery was blocked: its instruction or permission mode no longer matches the durable turn. Start a new chat to continue safely.)',
    notice: 'Saved Assistant turn no longer matches its durable recovery identity',
  }
  if (error.code === 'assistant_turn_recovery_required') return {
    blocked: true,
    message: '(saved turn recovery was blocked because the server requires the exact prior turn. Start a new chat to continue safely.)',
    notice: 'The server rejected a changed Assistant turn; start a new chat',
  }
  if (error.status === 404) return {
    blocked: true,
    message: '(saved turn recovery was blocked because this Assistant session no longer exists. Start a new chat.)',
    notice: 'This Assistant session no longer exists',
  }
  return {
    blocked: true,
    message: '(saved turn recovery was rejected. Restore access or start a new chat to continue safely.)',
    notice: 'The saved Assistant turn could not be recovered',
  }
}
