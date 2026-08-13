import { COMMAND_FAILED, COMMAND_PENDING, isTransientCommandReadError } from './api.js'

// The RULES of the durable run-command lifecycle, shared by the two surfaces that own one: Dock's run
// transport ('dock') and the Assistant's direct /commands ('assistant'). api.js already parameterizes
// the STORAGE beneath both (saveCommandTransport/loadCommandTransport/the per-run lock take a source);
// what lived twice was the rule layer above it — the observation classifier, the identity fence
// between the two surfaces, and the status-poll cadence.
//
// This module is deliberately NOT the whole state machine (doc 25 UI-01 recommended extracting one
// `useDurableRunCommand` hook). Measured on the tree, only 71 of Dock's 252 machine lines are
// identical to the Assistant's after α-renaming every paired identifier, and the two restore-on-mount
// effects share 7 lines of which all 7 are closing braces. The surfaces genuinely differ — the
// Assistant redacts server text out of its failure records, fences every setState on mount + the open
// run, and auto-resubmits an id-less recovery, none of which Dock does. So what moved here is the
// subset that is provably the same rule on both sides; each divergence stays visible at its call site.

const COMMAND_ID_RE = /^cmd_[0-9a-f]{32}$/

// Status reads classify into one vocabulary. 'transport'/'access'/'protocol' mean "the OUTCOME is
// unknown" — the durable intent must be preserved and re-observed. 'missing'/'request' are
// authoritative for this client and settle the command as failed.
export function observeCommandError(error) {
  if (error?.status === 401 || error?.status === 403) return 'access'
  if (error?.code === 'COMMAND_PROTOCOL_ERROR') return 'protocol'
  if (error?.status === 404) return 'missing'
  return isTransientCommandReadError(error) ? 'transport' : 'request'
}

// The three observation kinds that must NEVER be collapsed into "failed": the command may well have
// been accepted, so the surface keeps the same idempotency key + action and offers "Check command".
export const INTENT_PRESERVING_OBSERVATIONS = Object.freeze(['transport', 'access', 'protocol'])
export const commandIntentPreserved = kind => INTENT_PRESERVING_OBSERVATIONS.includes(kind)

// A response that failed identity verification keeps only a well-formed command id, never the rest of
// the record: the id is what lets the operator check the same durable command, and anything else in an
// unverifiable response is exactly what must not be trusted.
export function protocolCommandRecord(record) {
  const commandId = COMMAND_ID_RE.test(String(record?.id || '')) ? String(record.id) : ''
  return { commandId, record: commandId ? { id: commandId, status: 'accepted' } : { status: 'submitting' } }
}

// A pre-POST storage failure is a REJECTION, not an unknown outcome: nothing was sent, so the record is
// client-authored and explicitly not retryable.
export const commandStorageUnavailableRecord = () => ({ status: 'rejected', error: {
  code: 'command_storage_unavailable',
  message: 'The command was not sent because durable tab storage is unavailable.',
  remediation: 'Enable session storage or free browser storage, then try again.', retryable: false,
} })

// The exact-identity filter `clearRunCommandLock` needs. Passing anything less lets an older render
// unlock a newer accepted command (see the commandId rule in api.js::clearRunCommandLock).
export const commandLockIdentity = (source, action, entry) => ({
  source, idempotencyKey: entry?.idempotencyKey, action,
  expectedGeneration: entry?.expectedGeneration, commandId: entry?.record?.id || '',
})

// Dock and Assistant share ONE per-run lock. A lock owned by the other surface means this surface's
// stored envelope is stale — it must not be replayed, checked, or presented as this tab's pending work.
export const foreignCommandLock = (lock, source) => !!lock && lock.source !== source

// ...and a lock owned by this surface that does not match the stored envelope is the same hazard from
// the other direction: the durable record and the active lock disagree about WHICH command is in
// flight, so neither may be resubmitted. `commandId` is compared only when both sides carry one — an
// envelope staged before the POST returned legitimately has none yet.
export function commandLockMismatch(lock, source, identity) {
  return !!identity && lock?.source === source && !!(
    lock.idempotencyKey !== identity.idempotencyKey || lock.action !== identity.action
    || lock.expectedGeneration !== identity.expectedGeneration
    || (lock.commandId && identity.commandId && lock.commandId !== identity.commandId)
  )
}

// A reload during a check/retry is the ambiguous case: the request left this tab and its answer was
// lost with the page. The reloaded surface must re-observe rather than trust the pre-reload state.
export const interruptedCommandRecovery = saved => !!saved?.retrying || !!saved?.checking

// The ONE rule that separates "failed" from "outcome unknown" across a reload. A stored terminal
// failure settles into the actionable failure UI only when nothing about that envelope was in doubt
// when it was written; otherwise it stays pending and observable. Getting this wrong in the permissive
// direction shows a Retry button for a command that may still be executing.
export const settledCommandFailure = (saved, record) => COMMAND_FAILED.has(record?.status)
  && !saved?.statusUnavailable && !interruptedCommandRecovery(saved)

// Rebuild the in-flight record from a restored envelope. Both fallbacks are defensive: api.js's loader
// already refuses an envelope whose `commandId` and `record.id` disagree about existing, so neither
// branch can fire on a strictly-loaded record — which is why the two surfaces can share one spelling.
export function restoredCommandRecord(saved) {
  const stored = saved?.record || (saved?.commandId
    ? { id: saved.commandId, status: 'accepted' }
    : { status: 'submitting' })
  return saved?.commandId && !stored.id ? { ...stored, id: saved.commandId } : stored
}

// A PENDING COMMAND MUST NEVER READ AS FROZEN.
//
// On 2026-08-11 an operator watched `Stop requested…` for twenty minutes, then for a day. The
// server side of that is fixed — a pause now succeeds when the pause FOLDS, which is the effect the
// button asked for — but the chip itself was the other half of the report: a bare label with no
// elapsed time, no bound and no move. Anything the operator cannot act on and cannot see the end of
// is indistinguishable from a hang, which is how this got debugged as one.
//
// The bound comes from the SERVER's own record (`absolute_deadline_at`), not a client timer: the
// command monitor terminalizes against that value, so it is the real answer to "when does this stop
// waiting", and it survives a reload, a second tab, and a page that was closed for an hour. A record
// without one (a pre-upgrade server) is reported as unbounded rather than guessed at — `boundedMs`
// is null and the surface says the wait is not observable, which is itself actionable.
export const COMMAND_STALL_NOTICE_MS = 15000

// What a still-pending command owes the operator once it stops looking instantaneous. `null` while
// it still does: a stop that lands in 300 ms must not flash a stall notice.
//
// `waitingFor` is per ACTION because the honest answer differs: a stop waits on the fold (immediate)
// and then on the engine finishing the node it is on, a finalize waits on the whole wrap-up, a
// resume on the engine's acknowledgement. Naming what is being waited for is what turns "frozen"
// into "working".
export function pendingCommandRemedy(record, action, nowMs = Date.now()) {
  if (!record || !COMMAND_PENDING.has(record.status)) return null
  const createdSec = Number(record.created_at)
  if (!Number.isFinite(createdSec) || createdSec <= 0) return null
  const elapsedMs = Math.max(0, nowMs - createdSec * 1000)
  if (elapsedMs < COMMAND_STALL_NOTICE_MS) return null
  const absoluteSec = Number(record.absolute_deadline_at)
  const boundedMs = Number.isFinite(absoluteSec) && absoluteSec > 0
    ? Math.max(0, absoluteSec * 1000 - nowMs) : null
  const waitingFor = {
    stop: 'The pause is recorded as soon as it is written; the engine then finishes the node it is evaluating before it releases the run.',
    finalize: 'The engine is completing the report, lessons and cost write-out.',
    resume: 'Waiting for the engine to acknowledge the resume.',
  }[action] || 'Waiting for the engine to acknowledge this command.'
  return {
    elapsedMs,
    boundedMs,
    waitingFor,
    // The move that is ALWAYS available while a command is pending: force an observation. It is the
    // same request the poller makes, offered as an explicit control so that a poller stalled behind
    // a lost tab, a suspended laptop or a dropped connection is not the end of the operator's
    // options.
    canCheck: true,
  }
}

// Status-poll cadence, identical on both surfaces: first read one second after the command is
// accepted, then every 1.5s while it is still accepted/executing. Transport failures get a short
// budget of retries before the surface gives up and reports the outcome as unknown — three strikes,
// because a status read is cheap but a "status unavailable" banner the operator cannot dismiss is not.
export const COMMAND_POLL_START_MS = 1000
export const COMMAND_POLL_REPEAT_MS = 1500
export const COMMAND_POLL_MAX_TRANSIENT = 3
// Exponential backoff capped at 6s, but never faster than a Retry-After the server asked for.
export const commandPollBackoffMs = (error, transientFailures) =>
  Math.max(Number(error?.retryAfterMs) || 0, Math.min(6000, 750 * (2 ** transientFailures)))
