import { useEffect, useRef, useState } from 'react'

import { assistantFork, assistantForkStatus, createIdempotencyKey } from './api.js'
import { boundedRequest } from './requestDeadline.js'
import { storageGet, storageRemove, storageSet } from './util.js'
import {
  exhaustedForkStatus, forkSettlement, forkStartBlock, forkStatusVerdict, forkSubmitVerdict,
} from './assistantForkModel.js'

// The React half of the Assistant fork saga (doc 25 UI-05); the decisions are in
// ./assistantForkModel.js. What lives here is everything that needs a component: the durable
// recovery record (sessionStorage + an in-memory map + the one piece of render state the button
// reads), the single-flight action ref that four other places in AssistantBar consult, the bounded
// requests, and the reconciliation loop's sleeps.
//
// It is a hook and not a pure move because the saga's ORDER against the rest of the component is
// part of the contract: `forkActionSessionRef` is read by the send gate, the delete gate and the
// share gate, and the recovery record has to survive a reload, which is why it is written BEFORE the
// POST and cleared only on an authoritative answer.
//
// The one rule this file states that the model cannot: a fork request is saved before it is sent and
// forgotten only when the server has said something authoritative about it. Everything ambiguous —
// a timeout, a 5xx, a lost response, an abort — routes to `reconcileFork`, which ASKS. That is why
// the button says "check fork" while a record exists: the record is the operator's handle on a paid
// action whose outcome the browser could not see.

const ASSISTANT_FORK_ACTION_RE = /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/
const ASSISTANT_SESSION_RE = /^[\da-f]{16}$/
const sleep = (ms) => new Promise(r => setTimeout(r, ms))

const assistantForkRecoveryKey = sid =>
  `ll.assistant-fork-recovery.${encodeURIComponent(String(sid || ''))}`
const validAssistantForkRecovery = value => value && typeof value === 'object'
  && !Array.isArray(value) && Object.keys(value).length === 2
  && typeof value.actionId === 'string' && ASSISTANT_FORK_ACTION_RE.test(value.actionId)
  && Number.isSafeInteger(value.expectedMessages) && value.expectedMessages >= 0
export const loadAssistantForkRecovery = sid => {
  if (!ASSISTANT_SESSION_RE.test(String(sid || ''))) return null
  const key = assistantForkRecoveryKey(sid)
  const raw = storageGet(key)
  if (typeof raw !== 'string') return null
  if (raw.length > 256) { storageRemove(key); return null }
  try {
    const parsed = JSON.parse(raw)
    if (validAssistantForkRecovery(parsed)) return parsed
  } catch { /* invalid optional recovery state is discarded below */ }
  storageRemove(key)
  return null
}
const saveAssistantForkRecovery = (sid, recovery) => ASSISTANT_SESSION_RE.test(String(sid || ''))
  && validAssistantForkRecovery(recovery)
  && storageSet(assistantForkRecoveryKey(sid), JSON.stringify(recovery))
const clearAssistantForkRecovery = (sid, actionId) => {
  const current = loadAssistantForkRecovery(sid)
  if (current?.actionId !== actionId) return false
  return storageRemove(assistantForkRecoveryKey(sid))
}

export function useAssistantFork({
  sid, sidRef, mountedRef, openSessionSeqRef, deletingSessionsRef, shareActionSessionRef,
  readTurnState, flash, refreshSessions, openSession,
}) {
  const [forkBusySid, setForkBusySid] = useState(null)
  const [forkRecovery, setForkRecovery] = useState(null)
  // The single-flight owner. A REF and not state on purpose: the send, delete and share gates read
  // it synchronously in the same event turn a fork starts, before React can publish anything.
  const forkActionSessionRef = useRef(null)
  const forkRecoveryRef = useRef(new Map())
  // `readTurnState` is a FUNCTION, not two values, and that is deliberate: the saga's own gate read
  // `runningRef.current` / `turnCaptureRef.current` / the message count at the moment of the CLICK,
  // and half of those are refs that move without a render. Passing values would have silently made
  // the gate one render stale — and `messageCount` is the snapshot identity the server checks a
  // saved request against, so a stale one resumes or adopts a fork of a different transcript.
  const readTurnStateRef = useRef(readTurnState)
  readTurnStateRef.current = readTurnState

  useEffect(() => {
    if (!sid) { setForkRecovery(null); return }
    const recovery = forkRecoveryRef.current.get(sid) || loadAssistantForkRecovery(sid)
    if (recovery) forkRecoveryRef.current.set(sid, recovery)
    setForkRecovery(recovery ? { sid, ...recovery } : null)
  }, [sid])

  const rememberForkRecovery = (sourceSid, recovery) => {
    const stored = loadAssistantForkRecovery(sourceSid)
    const durable = stored?.actionId === recovery.actionId
      && stored.expectedMessages === recovery.expectedMessages
      ? true : saveAssistantForkRecovery(sourceSid, recovery)
    if (!durable) return false
    forkRecoveryRef.current.set(sourceSid, recovery)
    if (mountedRef.current && (sidRef.current || sid) === sourceSid) {
      setForkRecovery({ sid: sourceSid, ...recovery })
    }
    return true
  }
  const forgetForkRecovery = (sourceSid, actionId) => {
    if (forkRecoveryRef.current.get(sourceSid)?.actionId === actionId) {
      forkRecoveryRef.current.delete(sourceSid)
    }
    clearAssistantForkRecovery(sourceSid, actionId)
    if (mountedRef.current) {
      setForkRecovery(current => current?.sid === sourceSid && current.actionId === actionId
        ? null : current)
    }
  }
  const presentForkChild = async (child, sourceSid, recovery, sourceChoiceSeq) => {
    if (!mountedRef.current) return false
    const childId = typeof child?.id === 'string' && ASSISTANT_SESSION_RE.test(child.id)
      && child.parent === sourceSid && child.fork_action_id === recovery.actionId ? child.id : null
    if (!childId) return false
    const listed = await refreshSessions()
    if (!mountedRef.current) return false
    let confirmed = Array.isArray(listed) && listed.some(session => session?.id === childId)
    if (sidRef.current === sourceSid && openSessionSeqRef.current === sourceChoiceSeq) {
      const opened = await openSession(childId)
      confirmed = confirmed || !!opened?.ok
    } else if (confirmed) {
      flash('Fork created · kept your newer chat selection')
    }
    if (!confirmed) return false
    forgetForkRecovery(sourceSid, recovery.actionId)
    return true
  }
  const reconcileFork = async (sourceSid, recovery) => {
    const actionId = recovery.actionId
    let sawPending = false
    for (let attempt = 0; attempt < 5 && mountedRef.current; attempt++) {
      if (attempt > 0) await sleep(350 * attempt)
      try {
        const child = await boundedRequest(
          signal => assistantForkStatus(sourceSid, actionId, {
            expectedMessages: recovery.expectedMessages, signal,
          }), 4000)
        return { kind: 'created', child }
      } catch (error) {
        const verdict = forkStatusVerdict(error, actionId)
        if (verdict.retry) {
          if (verdict.kind === 'pending') sawPending = true
          continue
        }
        return { kind: verdict.kind, error }
      }
    }
    return { kind: exhaustedForkStatus(sawPending) }
  }
  const settleForkReconciliation = async (outcome, sourceSid, recovery, sourceChoiceSeq) => {
    if (!mountedRef.current) return
    const presented = outcome.kind === 'created'
      && await presentForkChild(outcome.child, sourceSid, recovery, sourceChoiceSeq)
    const settlement = forkSettlement(outcome.kind, { presented })
    if (!settlement) return
    if (settlement.forget) forgetForkRecovery(sourceSid, recovery.actionId)
    // A settlement that re-reads the list says its sentence AFTER the read and only if this bar is
    // still mounted — the list is what the sentence is about. One that reads nothing says it now.
    if (!settlement.refresh) { flash(settlement.message); return }
    await refreshSessions()
    if (mountedRef.current) flash(settlement.message)
  }
  const forkCurrentSession = async () => {
    const forkSid = sidRef.current || sid
    if (!forkSid) return
    const storedRecovery = forkRecoveryRef.current.get(forkSid) || loadAssistantForkRecovery(forkSid)
    const turn = readTurnStateRef.current()
    const blocked = forkStartBlock({
      actionActive: !!forkActionSessionRef.current,
      hasStoredRecovery: !!storedRecovery,
      turnBusy: !!turn.busy,
      deletingSession: deletingSessionsRef.current.has(forkSid),
      shareActionActive: !!shareActionSessionRef.current,
    })
    if (blocked) { flash(blocked.message); return }
    const recovery = storedRecovery || {
      actionId: createIdempotencyKey().toLowerCase(), expectedMessages: turn.messageCount,
    }
    if (!rememberForkRecovery(forkSid, recovery)) {
      flash('Browser recovery storage is unavailable · enable it before forking safely')
      return
    }
    const sourceChoiceSeq = openSessionSeqRef.current
    forkActionSessionRef.current = forkSid
    setForkBusySid(forkSid)
    try {
      const child = await boundedRequest(
        signal => assistantFork(forkSid, recovery, { signal }), 12000)
      if (!await presentForkChild(child, forkSid, recovery, sourceChoiceSeq)) {
        const outcome = await reconcileFork(forkSid, recovery)
        await settleForkReconciliation(outcome, forkSid, recovery, sourceChoiceSeq)
      }
    } catch (error) {
      if (!mountedRef.current) return
      const verdict = forkSubmitVerdict(error, recovery)
      if (verdict.kind === 'reconcile') {
        const outcome = await reconcileFork(forkSid, recovery)
        await settleForkReconciliation(outcome, forkSid, recovery, sourceChoiceSeq)
      } else if (verdict.kind === 'adopt') {
        // Another tab owns an identical request. Drop ours, take theirs, and reconcile THAT one — so
        // both tabs converge on one child instead of racing to create two.
        forgetForkRecovery(forkSid, recovery.actionId)
        if (!rememberForkRecovery(forkSid, verdict.adopted)) flash(verdict.unstorableMessage)
        else {
          const outcome = await reconcileFork(forkSid, verdict.adopted)
          await settleForkReconciliation(outcome, forkSid, verdict.adopted, sourceChoiceSeq)
        }
      } else {
        if (verdict.kind === 'forget') forgetForkRecovery(forkSid, recovery.actionId)
        if (verdict.refresh) refreshSessions()
        flash(verdict.message)
      }
    } finally {
      if (forkActionSessionRef.current === forkSid) forkActionSessionRef.current = null
      if (mountedRef.current) setForkBusySid(current => current === forkSid ? null : current)
    }
  }

  return { forkBusySid, forkRecovery, forkActionSessionRef, forkCurrentSession }
}
