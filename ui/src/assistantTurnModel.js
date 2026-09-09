// Doc 25 UI-05. `runLLM` — one Assistant turn, from the click to the durable reply — is the longest
// function in the UI, and the finding named it as the next target after the layout split. Its LENGTH
// is not the interesting part: the same 350 lines contain a seven-fact send gate, a second gate that
// re-checks everything after an awaited cancel, an SSE stream with two concurrent fallback poll
// loops, a terminal-frame reconciliation and a draft-restoration rule — and every one of those was
// an inline branch ladder no test could reach without mounting the component and driving a stream.
//
// The decisions live here, with no React and no I/O, so each has a truth table
// (`ui/test/assistantTurnModel.test.js`). The component keeps what genuinely needs it: the refs, the
// AbortController, the two poll loops and the setState calls.
//
// Three of these encode a property worth stating outright:
//
//   * THE SEND GATE FAILS CLOSED ON UNKNOWN PUBLIC-LINK STATE. A chat whose live-share status cannot
//     be established does not send — it says so, keeps the draft, and asks for a verification. A
//     turn sent into a live public link publishes the operator's next words.
//   * A CANCEL IS AWAITED, SO EVERYTHING IS RE-CHECKED AFTER IT. Between the click and the send, a
//     run command may have claimed the run, the route may have gone read-only and the operator may
//     have switched chats. `sendAbandonReason` is that second gate, and the two facts it can NAME
//     are the two the operator must be told about rather than left to guess.
//   * THE PROGRESS POLL ONLY FILLS A GAP. Behind a buffering proxy the SSE tokens arrive batched at
//     the end, so the mirrored answer-so-far is surfaced while it is LONGER than what the stream has
//     produced — and never once the stream overtakes it. That comparison is the whole rule, and
//     inverted it would let a stale poll overwrite live tokens.
import { assistantTurnIndex, completedAssistantReply } from './assistantRecovery.js'

// ── 1. may this turn be sent ──────────────────────────────────────────────────────────────────────
// Returns null when it may, else `{code, message, verifyShare}`. `verifyShare` asks the caller to
// start a public-link status read: the refusal is not a dead end, it is what schedules the check
// that lifts it.
export function sendTurnBlock({
  historical = false, readOnlyMessage = '', sessionOpening = false, forkingSession = false,
  shareActionActive = false, shareVerifying = false, shareUnknown = false, turnActive = false,
} = {}) {
  const block = (code, message, verifyShare = false) => ({ code, message, verifyShare })
  if (historical) return block('read_only', readOnlyMessage)
  if (sessionOpening) return block('opening', 'Wait for the selected Assistant chat to finish opening')
  if (forkingSession) {
    return block('forking', 'Wait for this Assistant chat to finish forking before sending')
  }
  if (shareActionActive) {
    return block('share_action', 'Wait for the current public-link action before sending another turn')
  }
  if (shareVerifying) return block('share_verifying', 'Still checking public-link status · nothing sent')
  // Fail closed, and schedule the read that can open the gate again.
  if (shareUnknown) return block('share_unknown', 'Nothing sent · checking public-link status', true)
  if (turnActive) return block('turn_active', 'Assistant or a run command is already starting')
  return null
}

// ── 2. the second gate, after the awaited cancel ──────────────────────────────────────────────────
// Returns null to proceed, else `{code, message}` — `message` is null for the shapes there is nobody
// to tell (an unmounted bar, a superseded attempt, a chat the operator switched away from).
export function sendAbandonReason({
  mounted = true, attemptCurrent = true, sessionCurrent = true, sessionOpening = false,
  directCapture = false, commandClaimed = false, readOnly = false,
} = {}) {
  // The two the operator MUST hear come first: they are the only ones where the draft was kept for a
  // reason the operator can act on, and both mean somebody else now owns this run.
  if (commandClaimed) {
    return { code: 'command_claimed', message: 'Draft not sent · a run command claimed this run first' }
  }
  if (readOnly) return { code: 'read_only', message: 'Draft not sent · run access became read-only' }
  if (!mounted) return { code: 'unmounted', message: null }
  if (!attemptCurrent) return { code: 'superseded', message: null }
  if (!sessionCurrent) return { code: 'session_changed', message: null }
  if (sessionOpening) return { code: 'opening', message: null }
  if (directCapture) return { code: 'direct_command', message: null }
  return null
}

// ── 3. the SSE fallback ───────────────────────────────────────────────────────────────────────────
// The /progress mirror fills the gap a buffering proxy leaves; it never fights a working stream.
export const shouldSurfaceProgress = (streamed, progress) => !!progress && progress.active === true
  && String(streamed || '').length < String(progress.text || '').length

// ── 4. the terminal frame ─────────────────────────────────────────────────────────────────────────
// A terminal SSE error is not a completed turn, and the transcript is the authority on what actually
// happened. Three outcomes, and the ORDER is the rule: a durable reply outranks everything (the turn
// finished, the frame was about the transport), a user turn that is the LAST message is a staged
// turn with no reply (retryable exactly once), and anything else is unknown — the optimistic bubble
// stays, marked as needing recovery, and nothing is re-POSTed.
export function terminalTurnOutcome(messages, prior, priorLen) {
  if (!Array.isArray(messages)) return { kind: 'invalid' }
  const reply = messages.length >= priorLen ? completedAssistantReply(messages, prior) : null
  if (reply?.content) return { kind: 'reply', reply }
  const turnIndex = assistantTurnIndex(messages, prior)
  if (turnIndex >= 0 && turnIndex === messages.length - 1) return { kind: 'staged', turnIndex }
  return { kind: 'unknown' }
}

// ── 5. what the bubble finally says ───────────────────────────────────────────────────────────────
// A streamed failure wins over a reply field, which wins over the accumulated tokens; `(no reply)` is
// the honest last resort and is never an empty bubble.
export function finalReplyText({ streamedFailure = '', result = null, streamed = '' } = {}) {
  return streamedFailure || (result && result.reply) || streamed
    || (result && result.ok === false && result.error ? `Assistant error: ${result.error}` : '(no reply)')
}

// ── 6. restoring a refused draft ──────────────────────────────────────────────────────────────────
// The turn was refused before the server staged anything, so the composer gets its text back. What
// the operator typed WHILE the request was in flight is theirs and is never discarded: the two are
// joined, in the order they were written, and an unchanged composer is left alone rather than
// doubled.
export function restoredComposerInput(inputAtSend, currentInput) {
  if (!currentInput) return inputAtSend
  if (currentInput === inputAtSend || currentInput.startsWith(`${inputAtSend}\n\n`)) return currentInput
  return `${inputAtSend}\n\n${currentInput}`
}
