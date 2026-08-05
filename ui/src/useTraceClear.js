import { useCallback, useEffect, useRef, useState } from 'react'

import { clearNodeTrace } from './api.js'
import { deadlineRequest } from './requestDeadline.js'
import {
  classifyTraceClearFailure, initialTraceClearMessage, traceClearAvailability,
} from './traceClearModel.js'

// The React half of the trace-clear recovery (doc 25 UI-09); the decisions are in
// ./traceClearModel.js. What lives here is everything that needs a component: the confirm → busy →
// blocked/ambiguous phase state, the durable recovery record written to the store ABOVE the
// conditionally mounted Inspector, the cross-surface recovery signal, the request itself, and the
// focus choreography that keeps a keyboard operator on a control that still exists after each phase
// change. `Inspector.jsx::Trace` keeps only the view toggle and the render.
//
// Splitting it out is not cosmetic: the four effects below are ordered against each other (a restored
// `reconcile` must start its detail read one frame after mount, before the phase effect can move
// focus), and reading that order used to mean reading past 200 lines of JSX-adjacent code.

export const TRACE_CLEAR_REQUEST_TIMEOUT_MS = 15000

// Every clear carries a fresh operation id so an interrupted request can be VERIFIED rather than
// re-submitted. Without a secure random source there is no such identity, and a clear that cannot be
// verified must not be sent at all.
export const newTraceClearOperationId = () => {
  const token = globalThis.crypto?.randomUUID?.().replace(/-/g, '').toLowerCase()
  if (!/^[0-9a-f]{32}$/.test(token || '')) {
    const error = new Error('Secure operation identity is unavailable.')
    error.code = 'trace_clear_operation_unavailable'
    throw error
  }
  return `tc_${token}`
}

export function useTraceClear({ nodeId, nodeStatus, nodeGeneration, runId, expectedGeneration,
  expectedTraceRevision, live, detailStatus = 'ready', reloadPending = false, unavailable = false,
  onReload, clearScope, clearRecoveryStore, recoverClearState = null, clearRecoverySignal = null,
  publishClearRecovery, bodyRef, onCleared }) {
  const initialClearRecovery = useRef(recoverClearState).current
  const [clearing, setClearing] = useState(
    initialClearRecovery?.phase === 'busy' ? 'busy' : '') // '' | 'confirm' | 'busy'
  const initialOnReload = useRef(onReload).current
  const handledClearRecoveryRevisionRef = useRef(clearRecoverySignal?.revision || 0)
  const initialClearMessage = useRef(initialTraceClearMessage(initialClearRecovery)).current
  const [clearMessage, setClearMessage] = useState(initialClearMessage)
  const clearTriggerRef = useRef(null)
  const clearConfirmRef = useRef(null)
  const clearRefreshRef = useRef(null)
  const { available: clearAvailable, unavailableText: clearUnavailableText } = traceClearAvailability(
    { nodeStatus, nodeGeneration, expectedGeneration, expectedTraceRevision, detailStatus,
      reloadPending, unavailable, live })
  const storeClearRecovery = useCallback(next => {
    const store = clearRecoveryStore?.current
    if (!store || !clearScope) return
    if (!next) {
      store.delete(clearScope)
      return
    }
    store.delete(clearScope)
    store.set(clearScope, next)
    while (store.size > 64) store.delete(store.keys().next().value)
  }, [clearRecoveryStore, clearScope])
  const setClearPhase = phase => {
    storeClearRecovery(phase ? { phase } : null)
    setClearing(phase)
  }
  const shouldRestoreClearFocus = () => {
    const active = document.activeElement
    return active === document.body || !active?.isConnected
      || active === clearConfirmRef.current
      || (active === clearTriggerRef.current && clearTriggerRef.current?.disabled)
  }
  useEffect(() => {
    if (!initialClearRecovery) return
    if (initialClearRecovery.phase === 'reconcile') {
      // Child effects run before the parent Inspector's request owner is installed on a full
      // remount. Defer one frame so this always starts (and supersedes) a post-POST detail read.
      const frame = requestAnimationFrame(() => {
        initialOnReload?.('trace-cleared')
        clearRefreshRef.current?.focus({ preventScroll: true })
      })
      return () => cancelAnimationFrame(frame)
    }
    if (initialClearRecovery.phase === 'busy') {
      const frame = requestAnimationFrame(
        () => clearConfirmRef.current?.focus({ preventScroll: true }))
      return () => cancelAnimationFrame(frame)
    }
    if (initialClearRecovery.phase === 'confirm') {
      storeClearRecovery(null)
      const frame = requestAnimationFrame(
        () => clearTriggerRef.current?.focus({ preventScroll: true }))
      return () => cancelAnimationFrame(frame)
    }
    if (!initialClearMessage) return
    if (initialClearRecovery.phase !== 'ambiguous') {
      storeClearRecovery({ phase: 'blocked', message: initialClearMessage })
    }
    const frame = requestAnimationFrame(() => clearRefreshRef.current?.focus({ preventScroll: true }))
    return () => cancelAnimationFrame(frame)
  }, [initialClearRecovery, initialClearMessage, initialOnReload, storeClearRecovery])
  useEffect(() => {
    const revision = clearRecoverySignal?.revision || 0
    if (clearRecoverySignal?.scope !== clearScope
        || revision <= handledClearRecoveryRevisionRef.current) return
    handledClearRecoveryRevisionRef.current = revision
    const recovery = clearRecoveryStore?.current?.get(clearScope)
    if (clearRecoverySignal.kind === 'clear-succeeded') {
      const restoreFocus = shouldRestoreClearFocus()
      const message = recovery?.message || {
        kind: 'success',
        blocking: true,
        text: 'Trace was cleared. Refreshing experiment details before another clear is allowed.',
      }
      setClearing('')
      setClearMessage(message)
      onReload?.('trace-cleared')
      if (restoreFocus) {
        requestAnimationFrame(() => clearRefreshRef.current?.focus({ preventScroll: true }))
      }
      return
    }
    if (['clear-failed', 'refresh-failed'].includes(clearRecoverySignal.kind)) {
      const restoreFocus = shouldRestoreClearFocus()
      const message = recovery?.message || {
        kind: 'error',
        blocking: true,
        text: clearRecoverySignal.kind === 'clear-failed'
          ? 'Trace clear did not complete. Refresh this experiment before trying again.'
          : 'Experiment details could not be refreshed. Trace clear remains unavailable until a refresh succeeds.',
      }
      setClearing('')
      setClearMessage(message)
      if (restoreFocus) {
        requestAnimationFrame(() => clearRefreshRef.current?.focus({ preventScroll: true }))
      }
      return
    }
    if (clearRecoverySignal.kind !== 'refresh-succeeded' || !clearMessage?.blocking) return
    const active = document.activeElement
    const restoreFocus = active === clearRefreshRef.current
      || active === document.body || !active?.isConnected
    storeClearRecovery(null)
    setClearMessage(null)
    if (restoreFocus) {
      requestAnimationFrame(() => {
        const trigger = clearTriggerRef.current
        const target = trigger && !trigger.disabled
          ? trigger : bodyRef.current?.closest('.insp-body')
        target?.focus({ preventScroll: true })
      })
    }
  }, [clearRecoverySignal, clearRecoveryStore, clearScope, clearMessage?.blocking, onReload,
    storeClearRecovery])
  useEffect(() => {
    if (!['confirm', 'busy'].includes(clearing)) return
    const frame = requestAnimationFrame(() => {
      if (clearing === 'confirm') {
        clearConfirmRef.current?.focus({ preventScroll: true })
        return
      }
      const active = document.activeElement
      if (active === document.body || !active?.isConnected) {
        clearConfirmRef.current?.focus({ preventScroll: true })
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [clearing])
  useEffect(() => {
    if (clearMessage?.kind !== 'success' || clearMessage.blocking) return
    const timer = setTimeout(() => setClearMessage(null), 4000)
    return () => clearTimeout(timer)
  }, [clearMessage])
  const finishClear = (message, recovery = null) => {
    setClearing('')
    storeClearRecovery(recovery || (message?.blocking ? { phase: 'blocked', message } : null))
    setClearMessage(message)
    if (message?.blocking) publishClearRecovery?.(clearScope, 'clear-failed')
    requestAnimationFrame(() => {
      const active = document.activeElement
      if (active === document.body || !active?.isConnected) {
        const target = message?.blocking
          ? clearRefreshRef.current
          : clearTriggerRef.current && !clearTriggerRef.current.disabled
            ? clearTriggerRef.current : bodyRef.current?.closest('.insp-body')
        target?.focus({ preventScroll: true })
      }
    })
  }
  const refreshClearScope = () => {
    if (reloadPending || clearMessage?.refreshing) return
    const recovery = clearRecoveryStore?.current?.get(clearScope)
    if (clearMessage?.verifyOperation && recovery?.operation) {
      void submitClear(recovery.operation, true)
      return
    }
    setClearMessage(message => message ? { ...message, refreshing: true } : message)
    if (!onReload?.('trace-clear-recovery')) {
      const message = {
        kind: 'error',
        blocking: true,
        text: 'Experiment refresh could not start. Trace clear remains unavailable; use the experiment retry notice before trying again.',
      }
      storeClearRecovery({ phase: 'blocked', message })
      setClearMessage(message)
    }
  }
  const submitClear = async (operation, verifying = false) => {
    const pendingMessage = verifying ? {
      kind: 'status',
      blocking: true,
      pending: true,
      verifyOperation: true,
      text: 'Checking whether the original trace clear completed…',
    } : null
    storeClearRecovery({
      phase: 'busy',
      operation,
      mode: verifying ? 'verify' : 'clear',
      ...(pendingMessage ? { message: pendingMessage } : {}),
    })
    setClearing('busy')
    setClearMessage(pendingMessage)
    try {
      const timed = deadlineRequest(signal => clearNodeTrace(runId, nodeId, {
        expectedGeneration: operation.expectedGeneration,
        expectedTraceRevision: operation.expectedTraceRevision,
        nodeGeneration: operation.nodeGeneration,
        operationId: operation.operationId,
        signal,
      }), TRACE_CLEAR_REQUEST_TIMEOUT_MS)
      const result = await timed.promise
      if (result?.status !== 'succeeded'
          || result?.operation_id !== operation.operationId) {
        const error = new Error('Trace clear returned an invalid operation receipt.')
        error.code = 'trace_clear_protocol_error'
        throw error
      }
      onCleared?.()
      // Persist the acknowledged mutation above the conditionally mounted Inspector. Only a detail
      // read started after this POST settles may clear this fence and permit another mutation.
      const message = {
        kind: 'success',
        blocking: true,
        text: `Trace cleared for #${nodeId} · attempt ${operation.nodeGeneration}. Refreshing experiment details…`,
      }
      storeClearRecovery({ phase: 'reconcile', message })
      setClearing('')
      setClearMessage(message)
      publishClearRecovery?.(clearScope, 'clear-succeeded')
    } catch (e) {
      const { message, recovery } = classifyTraceClearFailure(e, { verifying, operation, nodeId })
      finishClear(message, recovery)
    }
  }
  const doClear = () => {
    try {
      return submitClear({
        expectedGeneration,
        expectedTraceRevision,
        nodeGeneration,
        operationId: newTraceClearOperationId(),
      })
    } catch (error) {
      finishClear({
        kind: 'error',
        blocking: true,
        text: 'A secure trace clear operation could not be created. Reload the run before trying again.',
      })
      return false
    }
  }
  const beginClear = () => { setClearMessage(null); setClearPhase('confirm') }
  const cancelClear = () => {
    setClearPhase('')
    requestAnimationFrame(() => {
      const trigger = clearTriggerRef.current
      const target = trigger && !trigger.disabled
        ? trigger : bodyRef.current?.closest('.insp-body')
      target?.focus({ preventScroll: true })
    })
  }
  const clearPrimaryBusy = clearing === 'busy'
  const clearPrimaryVerifying = clearPrimaryBusy && clearMessage?.verifyOperation
  const clearPrimaryConfirm = clearing === 'confirm'
  const storedClearPhase = clearRecoveryStore?.current?.get(clearScope)?.phase
  const clearFenced = !!clearMessage?.blocking
    || ['busy', 'reconcile', 'blocked', 'ambiguous'].includes(storedClearPhase)
  return {
    clearing, clearMessage, clearAvailable, clearUnavailableText,
    clearPrimaryBusy, clearPrimaryVerifying, clearPrimaryConfirm, clearFenced,
    clearTriggerRef, clearConfirmRef, clearRefreshRef,
    beginClear, cancelClear, doClear, refreshClearScope,
  }
}
