// Doc 25 UI-05. Public-link mutations were written twice and in two different PLACES: `revokeCurrentShares`
// as a named handler, and the snapshot mint as a ~68-line async function living inside a JSX `onClick`
// prop — refs, clipboard fallbacks and a tombstone check inside the render tree. The two halves agree
// on every rule that matters and disagree only in copy, which is exactly the drift this module removes.
// The decisions are stated here ONCE, with no React and no I/O, so both halves can be driven directly.
//
// Two rules are load-bearing and are what the tests drive:
//
//   * A snapshot may only be minted over a COMPLETE turn; a revoke is safe at any time. A snapshot is
//     a durable public artifact, so freezing a half-streamed turn would publish text the model has not
//     finished saying. Revoking only ever takes access away, so it needs no such gate.
//   * A 4xx is authoritative about THIS request and nothing else is. Only the server can say "no link
//     was minted / nothing was revoked"; a timeout, a 5xx or a response lost to navigation leaves a
//     public link that may or may not exist, and it must be treated as existing until an authoritative
//     read says otherwise. That asymmetry is why the uncertain branch marks the session unknown and
//     re-reads the session list instead of only flashing a message.

// A closed vocabulary: an unrecognised action is refused rather than allowed through with a default,
// because the default here would be "mint a public link".
const SHARE_ACTION_BLOCKS = Object.freeze({
  snapshot: Object.freeze({
    fork: 'Wait for this chat to finish forking before creating a snapshot',
    turn: 'Wait for the Assistant reply to finish before creating a snapshot',
  }),
  revoke: Object.freeze({
    fork: 'Wait for this chat to finish forking before changing its public links',
    turn: null,   // revoking only removes access, so it never waits on a turn
  }),
})

export const SHARE_ACTIONS = Object.freeze(Object.keys(SHARE_ACTION_BLOCKS))

// The server refuses a snapshot whose last turn is not durably complete. Four codes mean the same
// thing to the operator; they are listed rather than pattern-matched so a new one has to be adopted
// deliberately instead of falling into the generic "Share failed".
const SNAPSHOT_TURN_CODES = Object.freeze([
  'assistant_share_in_progress', 'assistant_share_incomplete',
  'assistant_share_turn_active', 'assistant_share_turn_incomplete',
])

export function shareActionBlock(action, {
  shareActionActive = false, forkingSession = false, deletingSession = false, turnIncomplete = false,
} = {}) {
  // `Object.hasOwn`, not a plain lookup: `SHARE_ACTION_BLOCKS['toString']` inherits a truthy function
  // from Object.prototype, and the fail-closed branch below would never run for it.
  const copy = typeof action === 'string' && Object.hasOwn(SHARE_ACTION_BLOCKS, action)
    ? SHARE_ACTION_BLOCKS[action] : null
  if (!copy) return { message: 'Unknown share action' }
  if (shareActionActive) return { message: 'Another share action is still in progress' }
  if (forkingSession) return { message: copy.fork }
  if (turnIncomplete && copy.turn) return { message: copy.turn }
  if (deletingSession) return { message: 'This chat is being deleted' }
  return null
}

// `uncertain` is the fact the caller acts on: it means "mark this chat's public-link state unknown and
// re-read the list", not merely "show a different string". Keeping it beside the notice is what stops
// a copy edit from silently converting an uncertain mutation into a confidently wrong one. The notice
// is always OUR vocabulary — the thrown error's own message never reaches the operator, which is why
// this field is spelled `notice` the way `assistantRecovery.js` spells its own.
export function shareActionFailure(action, error) {
  const authoritative = error?.status >= 400 && error.status < 500
  if (action === 'revoke') {
    return authoritative
      ? { uncertain: false, notice: 'Revoke failed' }
      : { uncertain: true, notice: 'Revoke uncertain · retry to confirm' }
  }
  if (!authoritative) return { uncertain: true, notice: 'Share uncertain · revoke before retrying' }
  return {
    uncertain: false,
    notice: error?.code === 'assistant_share_snapshot_too_large'
      ? 'This chat is too large for a complete snapshot · fork or share a shorter chat'
      : SNAPSHOT_TURN_CODES.includes(error?.code)
        ? 'Wait for a complete Assistant reply before sharing' : 'Share failed',
  }
}

// A mint that lands after the user has moved on is still a real public link — the session list and the
// visible fallback have to record it — but its clipboard copy and its "copied" toast belong to the chat
// the operator is actually looking at. Naming the three outcomes keeps that distinction statable.
export function shareSnapshotAudience(mountedNow, visibleSessionId, mintedSessionId) {
  if (!mountedNow) return 'gone'
  return String(visibleSessionId || '') === String(mintedSessionId || '') ? 'current' : 'departed'
}
