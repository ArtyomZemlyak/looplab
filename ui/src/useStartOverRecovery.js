import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import { createIdempotencyKey, resetRun } from './util.js'
import {
  clearRunStartOverIntent, createRunStartOverIntent, loadRunStartOverIntent,
  saveRunStartOverIntent,
} from './runStartOverRecovery.js'

// Doc 25 UI-03. Start over ARCHIVES the finished generation and re-spawns the same run id — the
// one destructive operation the run route owns, and why RunView carried a recovery saga at all.
// It is crash-recoverable: the exact operation identity is written to session storage BEFORE the
// POST leaves, so a reload, a Back, or a response lost to navigation rejoins THAT operation instead
// of archiving a second time. Three properties are load-bearing and are what the tests drive:
//
//   * "rejected" and "outcome unknown" are different states. Only the ORIGINAL response can prove a
//     pre-mutation rejection; anything else keeps the envelope and the run-mutation lock.
//   * A response may commit only while it is still the request this component is waiting for
//     (`startOverRequestRef`), and a phase write may only land on the operation id it was staged
//     against — a competing tab's envelope is adopted, never overwritten.
//   * The replacement generation must be VERIFIED by a matching operation receipt before the route
//     is handed to it. A different generation appearing is not proof of anything on its own.
//
// The saga deliberately lives in TWO hooks, and the boundary is effect ORDER rather than cohesion.
// `useStartOverRecovery` owns the durable state and is called near the top of RunView, because
// `startOverMutationBlocked` gates the published run access, the panel allow-list and every node
// mutation. `useStartOverCoordination` owns the four coordinating effects and must stay at the
// position they occupied in the component body: the handoff is a LAYOUT effect that clears
// `largeOverviewAppliedRef`, and RunView's large-run overview layout effect reads that same ref in
// the same commit. Declaring the handoff first would let a replacement generation re-decide its
// canvas one commit earlier than it does today.
export const START_OVER_REQUEST_TIMEOUT_MS = 15_000
export const START_OVER_AUTO_RETRY_LIMIT = 3

const restoredStartOverState = runId => {
  const restored = loadRunStartOverIntent(runId)
  if (restored.kind !== 'active' || restored.intent.phase !== 'submitting') return restored
  // A submitting marker can only outlive its component when the response was lost to navigation or
  // reload. Keep the same operation identity so an explicit retry rejoins instead of duplicating it.
  return { ...restored, intent: { ...restored.intent, phase: 'unknown' } }
}

export function useStartOverRecovery({ runId, generation, reviewMode }) {
  const [startOverRecovery, setStartOverRecovery] = useState(
    () => reviewMode ? { kind: 'none', intent: null } : restoredStartOverState(runId))
  const [startOverRouteSyncAttempt, setStartOverRouteSyncAttempt] = useState(0)
  const [startOverRouteSyncFailed, setStartOverRouteSyncFailed] = useState(false)
  const startOverRequestRef = useRef(null)
  const startOverAutoRetryRef = useRef({ operationId: null, count: 0 })
  const startOverNoticeRef = useRef(null)
  useEffect(() => {
    startOverRequestRef.current?.controller?.abort()
    startOverRequestRef.current = null
    startOverAutoRetryRef.current = { operationId: null, count: 0 }
    setStartOverRecovery(reviewMode
      ? { kind: 'none', intent: null }
      : restoredStartOverState(runId))
    setStartOverRouteSyncAttempt(0)
    setStartOverRouteSyncFailed(false)
    return () => {
      startOverRequestRef.current?.controller?.abort()
      startOverRequestRef.current = null
    }
  }, [runId, reviewMode])
  const startOverIntent = startOverRecovery.kind === 'active'
    ? startOverRecovery.intent : null
  const startOverRequestPending = !!startOverRequestRef.current
  const startOverHandoff = !!(startOverIntent?.replacementGeneration && generation
    && generation === startOverIntent.replacementGeneration)
  // A verified replacement can itself be superseded by a later Start over in another tab. The
  // saved operation is resolved at that point, so keeping its envelope as an eternal mutation lock
  // would make the current generation impossible to open. Do not confuse the still-visible source
  // generation with that case: it is expected while the verified replacement is starting.
  const startOverReplacementSuperseded = !!(startOverIntent?.replacementGeneration && generation
    && generation !== startOverIntent.replacementGeneration
    && generation !== startOverIntent.expectedGeneration)
  return {
    startOverRecovery, setStartOverRecovery,
    startOverRouteSyncAttempt, setStartOverRouteSyncAttempt,
    startOverRouteSyncFailed, setStartOverRouteSyncFailed,
    startOverRequestRef, startOverAutoRetryRef, startOverNoticeRef,
    startOverIntent, startOverRequestPending, startOverHandoff, startOverReplacementSuperseded,
    startOverMutationBlocked: startOverRecovery.kind === 'active'
      || startOverRecovery.kind === 'corrupt',
  }
}

export function useStartOverCoordination(state, {
  runId, generation, reviewMode, runStatus, route, retryRunRef, routeMainRef, showToast,
  resetWorkspace,
}) {
  const {
    startOverRecovery, setStartOverRecovery,
    startOverRouteSyncAttempt, setStartOverRouteSyncAttempt, setStartOverRouteSyncFailed,
    startOverRequestRef, startOverAutoRetryRef, startOverNoticeRef,
    startOverIntent, startOverHandoff, startOverReplacementSuperseded,
  } = state
  const persistStartOverPhase = (
    intent, phase, replacementGeneration = intent.replacementGeneration,
  ) => {
    const next = createRunStartOverIntent(
      intent.runId, intent.expectedGeneration, intent.operationId, phase, Date.now(),
      replacementGeneration)
    if (!next) return null
    const stored = saveRunStartOverIntent(next, undefined, intent.operationId)
    if (!stored) {
      const restored = loadRunStartOverIntent(intent.runId)
      if (restored.kind === 'active'
          && restored.intent.operationId !== intent.operationId) {
        setStartOverRecovery(restored)
        return null
      }
    }
    setStartOverRecovery({
      kind: 'active', intent: next, storageUnavailable: !stored,
    })
    return next
  }
  const clearStartOverRecovery = (intent) => {
    if (clearRunStartOverIntent(intent.runId, undefined, intent.operationId)) {
      setStartOverRouteSyncFailed(false)
      setStartOverRecovery({ kind: 'none', intent: null })
      return true
    }
    const restored = loadRunStartOverIntent(intent.runId)
    setStartOverRecovery(restored.kind === 'none'
      ? { kind: 'corrupt', intent: null }
      : restored)
    return false
  }
  const executeStartOver = async (intent, { initialRequest = false } = {}) => {
    if (!intent || startOverRequestRef.current) return
    if (!persistStartOverPhase(intent, 'submitting')) {
      showToast('The saved Start over identity changed. No request was submitted.')
      return
    }
    const controller = new AbortController()
    const request = {
      runId: intent.runId,
      expectedGeneration: intent.expectedGeneration,
      operationId: intent.operationId,
      controller,
    }
    startOverRequestRef.current = request
    const timeout = setTimeout(() => controller.abort(), START_OVER_REQUEST_TIMEOUT_MS)
    try {
      const result = await resetRun(
        intent.runId, intent.expectedGeneration, intent.operationId, { signal: controller.signal })
      if (startOverRequestRef.current !== request) return
      const replacementGeneration = String(result?.generation || '')
      if (!result || result.ok !== true || result.operation_id !== intent.operationId
          || result.expected_generation !== intent.expectedGeneration
          || !/^[0-9a-f]{64}$/.test(replacementGeneration)
          || replacementGeneration === intent.expectedGeneration
          || (intent.replacementGeneration
            && replacementGeneration !== intent.replacementGeneration)) {
        const error = new Error('The server returned an invalid Start over response.')
        error.code = 'start_over_protocol_error'
        throw error
      }
      startOverRequestRef.current = null
      startOverAutoRetryRef.current = { operationId: intent.operationId, count: 0 }
      persistStartOverPhase(intent, 'accepted', replacementGeneration)
      showToast('Start over accepted. Opening the new run generation…')
      retryRunRef.current()
    } catch (error) {
      if (startOverRequestRef.current !== request) return
      startOverRequestRef.current = null
      const status = Number(error?.status)
      const generationChanged = error?.code === 'run_generation_changed'
      const foreignResetConflict = error?.code === 'reset_operation_conflict'
        && String(error?.detail?.operation_id || '')
        && String(error.detail.operation_id) !== intent.operationId
      const pendingReceipt = status === 425 && error?.code === 'reset_pending'
        && error?.detail?.operation_id === intent.operationId
        && error?.detail?.expected_generation === intent.expectedGeneration
        && ['pending', 'accepted'].includes(error?.detail?.status)
      if (pendingReceipt) {
        persistStartOverPhase(intent, 'pending')
        showToast('The server saved this exact Start over request. Checking its outcome…')
        retryRunRef.current()
        return
      }
      // Only the original response can prove a pre-mutation rejection. A later retry receiving a
      // 4xx does not disprove that the first request is still finishing or already crossed archive.
      const authoritativeRejection = initialRequest && (
        [400, 401, 403, 422, 428].includes(status)
        || generationChanged
        || foreignResetConflict
        || error?.code === 'replay_task_invalid'
        || error?.code === 'replay_config_invalid'
        || error?.code === 'replay_config_unavailable')
      if (authoritativeRejection && clearStartOverRecovery(intent)) {
        showToast(foreignResetConflict
          ? 'Another Start over operation already owns this run. This request was not submitted.'
          : error?.message || 'Start over was rejected; the run was not changed.')
        retryRunRef.current()
        return
      }
      persistStartOverPhase(intent, 'unknown')
      showToast(generationChanged
        ? 'The run changed, but this operation is not yet verified. Retry the exact request.'
        : 'Start-over outcome is not confirmed. Retry this exact request before doing anything else.')
      retryRunRef.current()
    } finally {
      clearTimeout(timeout)
    }
  }
  // The caller owns the "is it safe to start one" preflight over its own retained work; this is the
  // durable operation itself, from the pre-POST envelope onwards.
  const beginStartOver = (confirmedGeneration) => {
    if (reviewMode || startOverRecovery.kind !== 'none'
        || confirmedGeneration !== generation
        || !/^[0-9a-f]{64}$/.test(confirmedGeneration || '')) {
      showToast('Start over was not submitted because the run changed before confirmation.')
      return
    }
    const intent = createRunStartOverIntent(
      runId, confirmedGeneration, createIdempotencyKey().toLowerCase())
    if (!intent || !saveRunStartOverIntent(intent)) {
      setStartOverRecovery({ kind: 'unavailable', intent: null })
      showToast('Start over was not submitted because recovery storage is unavailable.')
      return
    }
    setStartOverRouteSyncFailed(false)
    startOverAutoRetryRef.current = { operationId: intent.operationId, count: 0 }
    setStartOverRecovery({ kind: 'active', intent })
    executeStartOver(intent, { initialRequest: true })
  }
  const finishStartOverHandoff = (intent, { superseded = false } = {}) => {
    if (!intent) return false
    startOverRequestRef.current?.controller?.abort()
    startOverRequestRef.current = null
    if (!route.openCurrentGeneration({ mode: 'replace' })) {
      setStartOverRouteSyncFailed(true)
      showToast(superseded
        ? 'Start over is verified, but the current run address could not be opened. Retry.'
        : 'The new run is ready, but its address could not be updated. Retry opening it.')
      return false
    }
    setStartOverRouteSyncFailed(false)
    resetWorkspace()
    if (clearStartOverRecovery(intent)) {
      showToast(superseded
        ? 'Start over completed, and the run changed again. The current generation is open.'
        : 'Previous run generation archived. The new run is open.')
    } else {
      showToast('The current run is open, but saved recovery evidence could not be cleared safely.')
    }
    requestAnimationFrame(() => {
      ;(routeMainRef.current || document.querySelector('[data-route-main]'))
        ?.focus?.({ preventScroll: true })
    })
    return true
  }
  const retryStartOver = () => {
    if (!startOverIntent || startOverRequestRef.current) return
    if (startOverReplacementSuperseded) {
      finishStartOverHandoff(startOverIntent, { superseded: true })
      return
    }
    if (startOverHandoff) {
      setStartOverRouteSyncAttempt(value => value + 1)
      return
    }
    executeStartOver(startOverIntent)
  }
  const retryStartOverStorage = () => {
    const restored = loadRunStartOverIntent(runId)
    setStartOverRecovery(restored)
    showToast(restored.kind === 'unavailable'
      ? 'Recovery storage is still unavailable in this tab.'
      : 'Recovery storage is available again.')
    if (restored.kind === 'none') {
      requestAnimationFrame(() => {
        ;(routeMainRef.current || document.querySelector('[data-route-main]'))
          ?.focus?.({ preventScroll: true })
      })
    }
  }
  useLayoutEffect(() => {
    if (!startOverIntent?.replacementGeneration || !generation
        || generation !== startOverIntent.replacementGeneration) return
    // This mismatch belongs to the exact destructive intent saved before POST, so replacing its
    // stale diagnostic URL is safe. Unrelated stale links still stop at the normal generation fence.
    finishStartOverHandoff(startOverIntent)
  }, [runId, generation, startOverIntent?.expectedGeneration,
    startOverIntent?.operationId, startOverIntent?.replacementGeneration,
    startOverIntent?.phase, startOverRouteSyncAttempt])
  useEffect(() => {
    if (!startOverIntent || startOverIntent.phase !== 'pending') return undefined
    const tracker = startOverAutoRetryRef.current
    if (tracker.operationId !== startOverIntent.operationId) {
      tracker.operationId = startOverIntent.operationId
      tracker.count = 0
    }
    if (tracker.count >= START_OVER_AUTO_RETRY_LIMIT) {
      persistStartOverPhase(startOverIntent, 'unknown')
      showToast('Start over is still unresolved. Use Retry exact request to check it again.')
      return undefined
    }
    tracker.count += 1
    const timer = setTimeout(
      () => executeStartOver(startOverIntent), 900 + tracker.count * 450)
    return () => clearTimeout(timer)
  }, [startOverIntent?.operationId, startOverIntent?.phase, startOverIntent?.updatedAt])
  useEffect(() => {
    if (!startOverIntent || startOverIntent.phase !== 'accepted'
        || (generation && generation !== startOverIntent.expectedGeneration)) return
    const age = Date.now() - startOverIntent.updatedAt
    if (age >= 45_000) {
      showToast('Start over is verified, but the replacement run is taking longer than expected to open.')
      return
    }
    const timer = runStatus === 'loading'
      ? setTimeout(() => {
          showToast('Start over is verified, but the replacement run is taking longer than expected to open.')
        }, Math.max(1000, 45_000 - age))
      : setTimeout(() => retryRunRef.current(), 900)
    return () => clearTimeout(timer)
  }, [runStatus, generation, startOverIntent?.operationId,
    startOverIntent?.expectedGeneration, startOverIntent?.replacementGeneration,
    startOverIntent?.phase, startOverIntent?.updatedAt])
  useEffect(() => {
    if (startOverRecovery.kind === 'none') return undefined
    const frame = requestAnimationFrame(() => {
      if (!document.querySelector('[aria-modal="true"]')) {
        startOverNoticeRef.current?.focus?.({ preventScroll: true })
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [startOverRecovery.kind])
  return { beginStartOver, retryStartOver, retryStartOverStorage }
}
