// Doc 25 UI-05, the second recovery saga. Forking an Assistant chat is a DURABLE server action with
// a browser-side idempotency identity: the request id is saved before the POST leaves, so a reload,
// a navigation or a lost response can ask "did my exact fork happen?" instead of forking twice. That
// makes the saga four decisions and a lot of choreography, and until now all four were written as
// branch ladders inside a 4,400-line component, where nothing could reach them:
//
//   1. may a fork START at all — six facts, in an order that matters (a SAVED recovery outweighs a
//      busy turn, because "check fork" is how an interrupted fork is resolved and refusing it while
//      the chat looks busy would strand the exact request the recovery record exists to finish);
//   2. what one STATUS POLL's error says — which codes are terminal, which mean "still working",
//      and which are merely ambiguous transport and must be asked again;
//   3. what the SUBMIT's error means — forget the record, reconcile it, adopt ANOTHER TAB's
//      in-flight request, or refuse to touch it;
//   4. what each outcome SETTLES to — whether the record is forgotten, whether the chat list is
//      re-read, and what the operator is told.
//
// They are stated here with no React, no refs and no I/O, so each can be driven over its own truth
// table (`ui/test/assistantForkModel.test.js`); `useAssistantFork.js` is the React half that awaits,
// stores and flashes. Two properties are the reason this is worth stating rather than reading:
//
//   * AMBIGUITY IS NOT FAILURE. A timeout, a 5xx, a null status or an abort says nothing about
//     whether the server forked; every one of them routes to reconciliation, never to "could not
//     fork". Only an authoritative code or a 404 may forget a saved request.
//   * ANOTHER TAB'S REQUEST IS ADOPTED ONLY WHEN IT IS PROVABLY THE SAME SNAPSHOT. The server names
//     the in-flight action id and the message count it was taken over; a mismatch in either is a
//     DIFFERENT fork, and adopting it would hand this tab a child of a transcript it never saw.
const FORK_ACTION_RE = /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/

// Transport-ambiguous: the request may or may not have reached the server. Shared by both the submit
// and the status poll, because "ask again" is the only safe reading of each of them in both places.
const ambiguousForkError = error => {
  const status = Number(error?.status)
  return error?.name === 'TimeoutError' || error?.name === 'AbortError' || error?.status == null
    || (Number.isFinite(status) && (status >= 500 || [408, 425, 429].includes(status)))
}

// ── 1. may a fork start ───────────────────────────────────────────────────────────────────────────
// Returns null when it may, else the ONE message the operator gets. Order is the rule: a stored
// recovery record beats the busy-turn gate, so "check fork" stays reachable on a chat whose turn was
// interrupted by the very fork being recovered.
export function forkStartBlock({
  actionActive = false, hasStoredRecovery = false, turnBusy = false,
  deletingSession = false, shareActionActive = false,
} = {}) {
  if (actionActive) return { code: 'fork_active', message: 'Another Assistant fork is still in progress' }
  if (!hasStoredRecovery && turnBusy) {
    return { code: 'turn_incomplete',
      message: 'Wait for a complete Assistant reply before forking this chat' }
  }
  if (deletingSession) return { code: 'deleting', message: 'This chat is being deleted' }
  if (shareActionActive) {
    return { code: 'share_active',
      message: 'Wait for the current share action before forking this chat' }
  }
  return null
}

// ── 2. one status poll ────────────────────────────────────────────────────────────────────────────
// `retry` means ask again; `kind` is what to report if the attempts run out. `pending` is the
// server's own "still working on THIS action" and is the only retry that changes the exhausted
// answer — an ambiguous transport error leaves the outcome genuinely unknown.
export function forkStatusVerdict(error, actionId) {
  const reportedAction = String(error?.detail?.action_id || '')
  if (error?.code === 'assistant_fork_in_progress') {
    // Someone's fork is running. Ours (or an unnamed one) means wait; a NAMED other action means
    // this session is busy with a request that is not the one we are asking about.
    return !reportedAction || reportedAction === actionId
      ? { kind: 'pending', retry: true }
      : { kind: 'blocked', retry: false }
  }
  if (error?.code === 'assistant_fork_deleted') return { kind: 'deleted', retry: false }
  if (error?.code === 'assistant_fork_action_conflict') return { kind: 'conflict', retry: false }
  if (error?.code === 'assistant_fork_deleting') return { kind: 'deleting', retry: false }
  if (error?.status === 404) return { kind: 'absent', retry: false }
  if (ambiguousForkError(error)) return { kind: 'transient', retry: true }
  return { kind: 'unknown', retry: false }
}

// What the poll answers when its attempts are spent. Separate from the loop because it is the whole
// difference between "the server said it is still working" and "we never got an answer".
export const exhaustedForkStatus = sawPending => (sawPending ? 'pending' : 'unknown')

// ── 3. the submit's error ─────────────────────────────────────────────────────────────────────────
// `kind` is what to DO: 'forget' drops the saved request (it can never succeed), 'reconcile' asks the
// server what happened, 'adopt' takes over another tab's provably-identical request, and 'refuse'
// leaves everything exactly as it is. Only 'adopt' carries `adopted`.
export function forkSubmitVerdict(error, recovery = {}) {
  const forget = (message, { refresh = false } = {}) =>
    ({ kind: 'forget', message, refresh, adopted: null })

  if (error?.code === 'assistant_fork_session_deleting') {
    return forget('This chat is being deleted')
  }
  if (error?.code === 'assistant_fork_deleted') {
    return forget('That fork was deleted · fork again to create a new copy', { refresh: true })
  }
  if (error?.code === 'assistant_fork_action_conflict') {
    return forget('This saved fork request no longer matches the chat · review it and fork again',
      { refresh: true })
  }
  if (error?.code === 'assistant_fork_source_changed') {
    return forget('This chat changed before the fork started · review it and fork again',
      { refresh: true })
  }
  if (['assistant_fork_turn_active', 'assistant_fork_turn_incomplete'].includes(error?.code)) {
    return forget('Wait for the current Assistant reply to finish or recover it before forking')
  }
  if (error?.status === 404) return forget('This Assistant chat no longer exists')

  if (error?.code === 'assistant_fork_in_progress'
      && error?.detail?.action_id && error.detail.action_id !== recovery.actionId) {
    // Another tab got there first. Adopt its request ONLY if the server's own description of it
    // matches the snapshot this tab was forking; otherwise the two tabs are forking different
    // transcripts and adopting would present a child of a conversation this tab never saw.
    const actionId = String(error.detail.action_id).toLowerCase()
    const expectedMessages = typeof error.detail.expected_messages === 'number'
      ? error.detail.expected_messages : Number.NaN
    if (!FORK_ACTION_RE.test(actionId) || !Number.isSafeInteger(expectedMessages)
        || expectedMessages < 0 || expectedMessages !== recovery.expectedMessages) {
      return { kind: 'refuse', adopted: null, refresh: false,
        message: 'Another tab is forking a different chat snapshot · refresh before retrying' }
    }
    return { kind: 'adopt', adopted: { actionId, expectedMessages }, refresh: false, message: null,
      // If the adopted identity cannot be saved, this tab cannot recover it later either.
      unstorableMessage: 'Another fork is in progress · refresh the chat list before retrying' }
  }
  if (error?.code === 'assistant_fork_in_progress' || error?.code === 'assistant_fork_deleting'
      || error?.code === 'assistant_fork_child_deleting' || ambiguousForkError(error)) {
    return { kind: 'reconcile', adopted: null, refresh: false, message: null }
  }
  return forget('Could not fork this Assistant chat')
}

// ── 4. settling an outcome ────────────────────────────────────────────────────────────────────────
// `presented` is whether the child was actually shown (listed, and opened or deliberately kept
// behind a newer selection). A presented 'created' is the only outcome with nothing to say.
export function forkSettlement(kind, { presented = false } = {}) {
  if (kind === 'created') {
    return presented ? null : { forget: false, refresh: true,
      message: 'Fork result is uncertain · check the chat list before retrying' }
  }
  if (kind === 'deleted') {
    return { forget: true, refresh: true,
      message: 'That fork was deleted · fork again to create a new copy' }
  }
  if (kind === 'conflict') {
    return { forget: true, refresh: true,
      message: 'This saved fork request no longer matches the chat · review it and fork again' }
  }
  if (kind === 'deleting') {
    return { forget: false, refresh: true,
      message: 'That fork is being deleted · check again before retrying' }
  }
  // `absent` and `blocked` deliberately do NOT re-read the list: neither says anything new about the
  // sessions, and both keep the saved request — "check fork" is what resolves them.
  if (kind === 'absent') {
    return { forget: false, refresh: false,
      message: 'Fork is not published · check fork retries this exact request' }
  }
  if (kind === 'blocked') {
    return { forget: false, refresh: false,
      message: 'Another fork is in progress · check fork keeps this request safe' }
  }
  return { forget: false, refresh: true, message: kind === 'pending'
    ? 'Fork is still finishing · use check fork to recover this exact request'
    : 'Fork status is uncertain · use check fork before starting another' }
}
