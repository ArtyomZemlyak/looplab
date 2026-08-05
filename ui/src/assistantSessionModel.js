// Doc 25 UI-05. `openSession` is a ~260-line async operation whose whole job is to decide, three
// separate times, whether a network read that has just come back is still allowed to replace what the
// operator is looking at. Those three checks were written out longhand at each await, differing in
// exactly one conjunct, and the one that differs is the load-bearing one. The decisions live here now:
// no React, no refs, no I/O, so each can be driven over its own truth table.
//
// The same file states the two other pure rules the session lifecycle turns on — which fact refuses a
// deletion, and what an unconfirmed deletion means — for the same reason: both were nested ternaries
// that had to stay in agreement with a second nested ternary written a few lines away.

// ── the late-read fence ───────────────────────────────────────────────────────────────────────────
//
// A session read may commit only while every fact it was issued under is still true. The reasons are
// ordered most-authoritative first, so the answer names the fact that actually ended the read rather
// than whichever check happened to be written first:
//
//   unmounted    — there is no longer a component to commit into.
//   newer-choice — the operator has chosen a different chat (or a new one) since this read started.
//                  `seq` is claimed BEFORE the request leaves; a purely observational read of the live
//                  session deliberately reuses the current epoch instead of burning a new one.
//   deleted      — a deletion tombstone for this id already exists, so its transcript must never be
//                  committed even if the read itself succeeded and the epoch never moved.
//   not-visible  — only checked where the caller passes `requireVisible`. The transcript read has
//                  already committed `sid` by then, so a later await must additionally prove the same
//                  chat is still the visible one; before that commit there is nothing to compare to.
//   replaced     — a newer read for the SAME id took the pending-open slot. A read that never claimed
//                  the slot (the observational one, `token === null`) is exempt: it cannot be replaced
//                  because it never owned anything.
export function sessionReadSuperseded({
  mounted, sessionId, seq, choiceSeq, token = null, pendingToken = null, deleted = false,
  visibleSessionId = null, requireVisible = false,
} = {}) {
  if (!mounted) return 'unmounted'
  if (seq !== choiceSeq) return 'newer-choice'
  if (deleted) return 'deleted'
  if (requireVisible && visibleSessionId !== sessionId) return 'not-visible'
  if (token && pendingToken !== token) return 'replaced'
  return null
}

// ── refusing a deletion ───────────────────────────────────────────────────────────────────────────
//
// Deleting a chat is irreversible and cancels live work, so every reason to wait is checked before it
// starts. The ORDER is the property: the operator is told about the innermost cause, the one they can
// actually act on. `duplicate` is not a refusal the operator sees — a second click on a deletion that
// is already running is simply not a second deletion.
export function sessionDeleteBlock({
  launchRecovery = null, isCurrentSession = false, shareActionOwned = false, forkOwned = false,
  openPending = false, turnActive = false, publiclyShared = false, alreadyDeleting = false,
} = {}) {
  if (launchRecovery) {
    return {
      message: `Check or release startup recovery${launchRecovery.runId ? ` for “${launchRecovery.runId}”` : ''} before deleting this chat`,
      // The recovery control lives in the side view, so offering the advice is only useful when the
      // chat holding that unresolved startup IS the one on screen.
      revealRecovery: isCurrentSession,
    }
  }
  if (shareActionOwned) return { message: 'Wait for the current share action before deleting this chat' }
  if (forkOwned) return { message: 'Wait for this chat to finish forking before deleting it' }
  if (openPending) return { message: 'Wait for this chat to finish opening before deleting it' }
  if (turnActive) return { message: 'Stop or finish the current Assistant turn before deleting this chat' }
  // A public link is a separate secret that outlives the transcript. Deleting the chat under it would
  // orphan the link instead of revoking it, so the revocation has to be an explicit, observed act.
  if (publiclyShared) return { message: 'Unshare this chat before deleting it so every public link is explicitly revoked' }
  if (alreadyDeleting) return { duplicate: true }
  return null
}

// ── an unconfirmed deletion ───────────────────────────────────────────────────────────────────────
//
// The dialog copy and the toast were two nested ternaries a dozen lines apart, walking the same four
// codes in the same order and expected to agree. They are one table now, so a row cannot gain a
// detail without a notice: an operator told "wait for the fork" in the dialog and "could not confirm"
// in the toast is being given two different stories about the same durable outcome.
const DELETE_FAILURES = Object.freeze({
  assistant_delete_shared: Object.freeze({
    notice: 'Unshare this chat before deleting it',
    detail: 'This chat still has an active public link. Unshare it first, then delete the chat.',
  }),
  assistant_delete_fork_in_progress: Object.freeze({
    notice: 'Wait for this chat to finish forking before deleting it',
    detail: 'This chat is still being forked. It is shown again; try deleting it after the fork finishes.',
  }),
  assistant_delete_busy: Object.freeze({
    notice: 'Assistant work is still stopping',
    detail: 'The live Assistant turn is still stopping. The chat is shown again; try deleting it again shortly.',
  }),
  assistant_delete_incomplete: Object.freeze({
    notice: 'Assistant chat was not completely removed',
    detail: 'The server could not completely remove the chat. Close anything using its files and try again.',
  }),
})

export const DELETE_FAILURE_CODES = Object.freeze(Object.keys(DELETE_FAILURES))

// `listedPresence` is deliberately three-valued. `true` means an authoritative list still showed the
// chat; `null` means the list could not be read, which is NOT evidence of absence — the list
// projection intentionally skips unreadable or partially deleted session metadata, so only DELETE can
// confirm removal. Collapsing the two would promise the operator a retry is safe when nothing is known.
export function sessionDeleteFailure({ code = null, ambiguous = false, listedPresence = null } = {}) {
  if (typeof code === 'string' && Object.hasOwn(DELETE_FAILURES, code)) return DELETE_FAILURES[code]
  return {
    notice: 'Could not confirm chat deletion',
    detail: ambiguous && listedPresence === true
      ? 'Neither bounded deletion attempt was confirmed, and the chat still appears available. You can try again.'
      : ambiguous
        ? 'Neither bounded deletion attempt was confirmed. The chat is shown again for safety; retry or refresh.'
        : 'The chat was not deleted. It is shown again; you can cancel or try again.',
  }
}
