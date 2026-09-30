import React, { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createIdempotencyKey, llmHealth } from './util.js'
import { OpIcon } from './icons.jsx'
import { deadlineRequest } from './requestDeadline.js'
import { publishModelCheck } from './modelConnection.js'
import './llm-health.css'

const countLabel = (count, singular, plural = `${singular}s`) => `${count} ${count === 1 ? singular : plural}`
// Outlive the server's 60s provider wall plus bounded teardown. The interaction
// test imports this deadline so it cannot silently stop exercising timeout recovery.
export const LLM_HEALTH_TIMEOUT_MS = 70_000
export const unknownTransport = error => !Number.isInteger(error?.status)
  || error.status >= 500 || [408, 425, 429].includes(error.status)

const releaseHealthRequest = active => {
  if (!active || active.released) return
  active.released = true
  active.finishAction?.(active.mutation)
}

const LLM_HEALTH_RECOVERY_KEY = 'looplab.llm-health-recovery.v1'
const LLM_HEALTH_OPERATION_RE = /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i
let volatileHealthRecovery = null
const healthRecoveryScope = () => typeof location === 'undefined'
  ? '' : `${location.origin}${location.pathname}`
const validHealthRecovery = value => !!value && value.scope === healthRecoveryScope()
  && LLM_HEALTH_OPERATION_RE.test(value.operationId || '')
  && typeof value.settingsRevision === 'string' && value.settingsRevision.length <= 256
  && typeof value.secretRevision === 'string' && value.secretRevision.length <= 256
  && ['reconcile', 'terminal-unknown'].includes(value.mode)
  && Number.isFinite(value.createdAt) && value.createdAt > 0
export const readHealthRecovery = () => {
  if (validHealthRecovery(volatileHealthRecovery)) return volatileHealthRecovery
  let value = null
  try {
    value = JSON.parse(window.sessionStorage.getItem(LLM_HEALTH_RECOVERY_KEY) || 'null')
  } catch { /* fall back to this page's volatile recovery fence */ }
  return validHealthRecovery(value) ? value : null
}
const writeHealthRecovery = value => {
  const record = {
    scope: healthRecoveryScope(),
    operationId: value.operationId,
    settingsRevision: value.settingsRevision,
    secretRevision: value.secretRevision,
    mode: value.mode,
    createdAt: value.createdAt || Date.now(),
  }
  volatileHealthRecovery = record
  try {
    window.sessionStorage.setItem(LLM_HEALTH_RECOVERY_KEY, JSON.stringify(record))
    return true
  } catch { return false }
}
const clearHealthRecovery = operationId => {
  try {
    const current = readHealthRecovery()
    if (!operationId || !current || current.operationId === operationId) {
      volatileHealthRecovery = null
      window.sessionStorage.removeItem(LLM_HEALTH_RECOVERY_KEY)
    }
  } catch { volatileHealthRecovery = null }
}

// LLM endpoint self-test (the UI equivalent of `LoopLab smoke`): pings the configured model so the
// user knows it is reachable before launching a run against it.
export function LlmHealth({
  savedSettingsRevision,
  savedSecretRevision,
  unsavedCount = 0,
  actionBlocked = false,
  actionKind = '',
  beginAction,
  finishAction,
  reloadSavedSettings,
  onRecoveryChange,
  providerBlockedReason = '',
}) {
  const noteId = useId()
  const actionNoteId = `${noteId}-action`
  const blockNoteId = `${noteId}-block`
  const draftNoteId = `${noteId}-draft`
  const [status, setStatus] = useState(null)
  const [busyContext, setBusyContext] = useState(null)
  const requestRef = useRef(null)
  const identityRef = useRef(null)
  const previousIdentity = identityRef.current
  const identityChanged = !previousIdentity
    || previousIdentity.savedSettingsRevision !== savedSettingsRevision
    || previousIdentity.savedSecretRevision !== savedSecretRevision
    || previousIdentity.providerBlockedReason !== providerBlockedReason
  if (identityChanged) {
    identityRef.current = {
      savedSettingsRevision,
      savedSecretRevision,
      providerBlockedReason,
      version: (previousIdentity?.version || 0) + 1,
    }
  }
  const contextVersion = identityRef.current.version
  const revisionsReady = typeof savedSettingsRevision === 'string' && savedSettingsRevision.length > 0
    && typeof savedSecretRevision === 'string' && savedSecretRevision.length > 0
  const busy = busyContext === contextVersion && requestRef.current?.contextVersion === contextVersion
  const reloading = actionKind === 'reloading settings'
  const visibleStatus = status?.contextVersion === contextVersion ? status.value : null

  // Effects run after render. The version above hides and fences an old result synchronously; this
  // layout effect then cancels transport work before paint and releases the shared settings action.
  useLayoutEffect(() => {
    const active = requestRef.current
    if (active && active.contextVersion !== contextVersion) {
      requestRef.current = null
      active.timed.controller.abort()
      releaseHealthRequest(active)
    }
    setStatus(current => {
      if (current?.contextVersion === contextVersion) return current
      if (!revisionsReady) return null
      const recovery = readHealthRecovery()
      if (!recovery) return null
      const revisionChanged = recovery.settingsRevision !== savedSettingsRevision
        || recovery.secretRevision !== savedSecretRevision
      const terminalUnknown = recovery.mode === 'terminal-unknown'
      return { contextVersion, value: {
        ok: false,
        unresolved: true,
        reconcilable: !terminalUnknown,
        terminalUnknown,
        revisionChanged,
        previousConfiguration: revisionChanged,
        operationId: recovery.operationId,
        error: revisionChanged
          ? terminalUnknown
            ? 'A provider outcome from the previous saved configuration is unresolved and may have been billed. Acknowledge it before starting another check.'
            : 'A check from the previous saved configuration has no verified result. Check that previous result without starting a new provider call.'
          : terminalUnknown
          ? 'The previous provider outcome is unresolved and may have been billed. Starting another check may bill again.'
          : 'A previous browser request has no verified result. Check the previous result to reconcile it without starting a new provider call.',
      } }
})

    setBusyContext(current => current === contextVersion ? current : null)
  }, [contextVersion, revisionsReady, savedSettingsRevision, savedSecretRevision])

  useEffect(() => {
    onRecoveryChange?.(visibleStatus?.unresolved === true)
  }, [visibleStatus?.unresolved, onRecoveryChange])

  useEffect(() => () => {
    const active = requestRef.current
    requestRef.current = null
    active?.timed.controller.abort()
    releaseHealthRequest(active)
  }, [])

  const startCheck = (replayOnly = false) => {
    if (actionBlocked || requestRef.current || !revisionsReady
        || (providerBlockedReason && !replayOnly)) return
    const mutation = beginAction?.('testing-llm')
    if (!mutation) return
    const requestedContext = contextVersion
    const operationId = replayOnly && visibleStatus?.reconcilable && visibleStatus.operationId
      ? visibleStatus.operationId : createIdempotencyKey()
    const previousRecovery = readHealthRecovery()
    const replayRecovery = replayOnly && previousRecovery?.operationId === operationId
      ? previousRecovery : null
    const requestSettingsRevision = replayRecovery?.settingsRevision || savedSettingsRevision
    const requestSecretRevision = replayRecovery?.secretRevision || savedSecretRevision
    const previousConfiguration = requestSettingsRevision !== savedSettingsRevision
      || requestSecretRevision !== savedSecretRevision
    const recoveryCreatedAt = previousRecovery?.operationId === operationId
      ? previousRecovery.createdAt : Date.now()
    // Persist the non-secret recovery fence before POST. If the tab unloads after submission, the
    // next Settings mount can only issue a replay-only lookup for this UUID.
    const recoveryPersisted = writeHealthRecovery({
      operationId,
      settingsRevision: requestSettingsRevision,
      secretRevision: requestSecretRevision,
      mode: 'reconcile',
      createdAt: recoveryCreatedAt,
    })
    if (!recoveryPersisted) {
      // A health operation is unsafe to hand off across reload unless its UUID survives. Restore
      // any older fence and stop before constructing the request; no provider was contacted here.
      if (previousRecovery) writeHealthRecovery(previousRecovery)
      else clearHealthRecovery(operationId)
      finishAction?.(mutation)
      setStatus({ contextVersion: requestedContext, value: previousRecovery
        ? {
            ...(visibleStatus || {}),
            ok: false,
            unresolved: true,
            reconcilable: previousRecovery.mode === 'reconcile',
            terminalUnknown: previousRecovery.mode === 'terminal-unknown',
            operationId: previousRecovery.operationId,
            error: 'Browser recovery storage is unavailable. No provider request was sent; the previous outcome remains unresolved.',
          }
        : {
            ok: false,
            notStarted: true,
            storageUnavailable: true,
            error: 'Browser recovery storage is unavailable, so the check was not started and no provider request was sent.',
          } })
      return
    }
    const timed = deadlineRequest(signal => llmHealth(
      requestSettingsRevision, requestSecretRevision, operationId, { signal, replayOnly },
    ), LLM_HEALTH_TIMEOUT_MS)
    const active = {
      timed,
      contextVersion: requestedContext,
      operationId,
      replayOnly,
      requestSettingsRevision,
      requestSecretRevision,
      previousConfiguration,
      recoveryCreatedAt,
      mutation,
      finishAction,
      released: false,
    }
    requestRef.current = active
    setStatus(null)
    setBusyContext(requestedContext)
    timed.promise.then(value => {
      if (timed.controller.signal.aborted || requestRef.current !== active
          || identityRef.current.version !== requestedContext) return
      const providerAttempted = value.provider_attempted === true
      const terminalUnknown = value.ok !== true
        && (value.outcome_unknown === true || value.ambiguous === true)
      if (terminalUnknown) {
        writeHealthRecovery({ operationId, settingsRevision: active.requestSettingsRevision,
          secretRevision: active.requestSecretRevision, mode: 'terminal-unknown',
          createdAt: active.recoveryCreatedAt })
      } else clearHealthRecovery(operationId)
      if (value.ok === true || terminalUnknown || providerAttempted) {
        publishModelCheck(active.requestSettingsRevision, active.requestSecretRevision,
          value.ok === true ? 'passed' : terminalUnknown ? 'unknown' : 'failed')
      }
      setStatus({ contextVersion: requestedContext, value: value.ok === true
        ? { ok: true, previousConfiguration: active.previousConfiguration }
        : terminalUnknown
          ? {
              ok: false,
              unresolved: true,
              terminalUnknown: true,
              previousConfiguration: active.previousConfiguration,
              operationId,
              error: 'The server recorded an unresolved provider outcome that may have been billed. Starting another check may bill again.',
            }
          : {
              ok: false,
              previousConfiguration: active.previousConfiguration,
              notStarted: !providerAttempted,
              error: value.message || value.error
                || 'The active LLM check did not complete successfully.',
            } })
    }).catch(error => {
      if (error?.name === 'AbortError' || requestRef.current !== active
          || identityRef.current.version !== requestedContext) return
      const detail = error?.detail && typeof error.detail === 'object'
        && !Array.isArray(error.detail) ? error.detail : {}
      const configurationChanged = error?.code === 'llm_configuration_changed'
      const attemptContractKnown = typeof detail.provider_attempted === 'boolean'
        && typeof detail.outcome_unknown === 'boolean'
      const postAttempt = error?.code === 'llm_configuration_changed_after_attempt'
        || error?.code === 'llm_health_outcome_unverifiable_after_attempt'
      const replayUnavailable = error?.code === 'llm_health_replay_unavailable'
      const replayConflict = replayOnly && error?.code === 'llm_health_operation_conflict'
      const protocolUncertain = error?.code === 'llm_health_identity_protocol_error'
      const terminalUnknown = !protocolUncertain && (postAttempt || replayUnavailable || replayConflict
        || detail.outcome_unknown === true || detail.ambiguous === true)
      const reconcilable = !terminalUnknown && (error?.name === 'TimeoutError'
        || protocolUncertain || (!attemptContractKnown && unknownTransport(error)))
      const unresolved = terminalUnknown || reconcilable
      const anotherCheckBusy = error?.code === 'llm_health_in_progress'
      if (terminalUnknown) {
        writeHealthRecovery({ operationId, settingsRevision: active.requestSettingsRevision,
          secretRevision: active.requestSecretRevision, mode: 'terminal-unknown',
          createdAt: active.recoveryCreatedAt })
      } else if (!reconcilable) clearHealthRecovery(operationId)
      if (unresolved || (attemptContractKnown && detail.provider_attempted && !configurationChanged)) {
        publishModelCheck(active.requestSettingsRevision, active.requestSecretRevision,
          unresolved ? 'unknown' : 'failed')
      }
      setStatus({ contextVersion: requestedContext, value: {
        ok: false,
        configurationChanged,
        unresolved,
        operationId: unresolved ? operationId : undefined,
        reconcilable,
        terminalUnknown,
        replayUnavailable,
        replayConflict,
        anotherCheckBusy,
        previousConfiguration: active.previousConfiguration,
        notStarted: !unresolved && !configurationChanged,
        error: configurationChanged
          ? 'The active LLM configuration changed before the provider was contacted. Reload saved settings before testing again.'
          : terminalUnknown
            ? replayUnavailable
              ? 'The previous result is no longer available. No new provider call was made; its outcome remains unknown.'
              : replayConflict
                ? 'The recovery ID belongs to a different server receipt. No new provider call was made; the prior outcome remains unknown.'
              : 'The server recorded an unresolved provider outcome that may have been billed. Starting another check may bill again.'
            : reconcilable
              ? 'The browser has no verified result. Check the previous result to reuse this operation without starting a new provider call.'
            : anotherCheckBusy
              ? 'Another active LLM check is already running. This request did not contact the provider; wait, then try again.'
              : detail.message || error?.message
                || 'The active LLM check could not start; no provider result was confirmed.',
      } })
    }).finally(() => {
      if (requestRef.current === active) requestRef.current = null
      releaseHealthRequest(active)
      if (identityRef.current.version === requestedContext) {
        setBusyContext(current => current === requestedContext ? null : current)
      }
    })
  }

  const check = () => {
    if (visibleStatus?.configurationChanged) {
      Promise.resolve(reloadSavedSettings?.()).then(reloaded => {
        if (reloaded !== false) setStatus(null)
      })
      return
    }
    if (visibleStatus?.terminalUnknown) return
    startCheck(visibleStatus?.reconcilable === true)
  }
  const startNewAfterUnknown = () => {
    if (actionBlocked || providerBlockedReason || requestRef.current
        || !revisionsReady || !visibleStatus?.terminalUnknown) return
    if (!window.confirm('The previous provider outcome is unresolved and may already be billed. Start a new active LLM check that may bill again?')) return
    // `startCheck` replaces the old recovery record only after it owns the shared mutation token.
    // If another same-tick action won that token, keep the terminal warning and its UUID intact.
    startCheck(false)
  }
  const dismissRecovery = () => {
    if (actionBlocked || requestRef.current || !visibleStatus?.unresolved) return
    if (!window.confirm('Acknowledge and dismiss this unresolved provider outcome? A later Test active LLM action will create a new provider operation and may bill again.')) return
    clearHealthRecovery(visibleStatus.operationId)
    setStatus(null)
  }

  const buttonTitle = busy
    ? 'An active provider check is in progress. Leaving may not stop provider work or billing.'
    : reloading
      ? 'Reloading the saved configuration without contacting the provider.'
      : visibleStatus?.configurationChanged
        ? 'Reload the server-resolved active LLM configuration before checking again.'
      : visibleStatus?.terminalUnknown
        ? 'The previous outcome is unresolved. A new check is available only as a separate confirmed action because it may bill again.'
        : visibleStatus?.reconcilable
          ? visibleStatus.previousConfiguration
            ? 'Requests only the operation result from the previous saved configuration. The current active LLM cannot be contacted by this action.'
            : 'Requests only the previous operation result. It cannot start a new provider call if that result expired or the server restarted.'
          : providerBlockedReason
            ? providerBlockedReason
            : revisionsReady
              ? 'Starts one explicit check of the server-resolved active LLM. Unsaved edits and typed API keys are excluded; environment or .env configuration may override the saved store. The browser does not auto-retry.'
              : 'Load saved settings before testing the active LLM.'
  const activeReplayOnly = busy && requestRef.current?.replayOnly === true
  const providerActionBlocked = !!providerBlockedReason
    && !visibleStatus?.configurationChanged && !visibleStatus?.reconcilable
  const healthActionNote = reloading || visibleStatus?.configurationChanged
    ? 'Reload only · no provider request'
    : activeReplayOnly || visibleStatus?.reconcilable
      ? 'Replay only · no new provider request'
      : visibleStatus?.terminalUnknown
        ? 'A new check requires confirmation and may bill again'
        : busy
          ? 'Provider check in progress and may be billed'
          : providerBlockedReason
            ? 'Provider test blocked by credential state'
            : 'One provider request may be billed'
  const healthDescription = [actionNoteId,
    providerBlockedReason ? blockNoteId : '',
    unsavedCount > 0 ? draftNoteId : ''].filter(Boolean).join(' ')
  return <span className="llm-health">
    <button type="button" className="btn sm"
            disabled={actionBlocked || busy || !revisionsReady || visibleStatus?.terminalUnknown
              || providerActionBlocked}
            onClick={check} title={buttonTitle}
            aria-describedby={healthDescription}>
      {busy ? (activeReplayOnly ? 'Checking previous result…' : 'Testing active LLM…')
        : reloading ? 'Reloading settings…'
        : visibleStatus?.configurationChanged ? 'Reload saved settings'
        : visibleStatus?.terminalUnknown ? 'Outcome unresolved'
        : visibleStatus?.reconcilable ? 'Check previous result'
        : <><OpIcon name="bolt" className="t-ic" /> Test active LLM</>}
    </button>
    {visibleStatus?.terminalUnknown && <button type="button" className="btn sm warn"
      disabled={actionBlocked || !!providerBlockedReason || busy || !revisionsReady}
      onClick={startNewAfterUnknown}
      aria-describedby={actionNoteId}
      title="Requires confirmation because this creates a new provider operation that may be billed.">
      Start new check (may bill)
    </button>}
    {visibleStatus?.unresolved && <button type="button" className="btn sm ghost"
      disabled={actionBlocked || busy} onClick={dismissRecovery}
      title="Acknowledge the unknown outcome and remove its recovery gate without contacting the provider.">
      Dismiss warning
    </button>}
    <span id={actionNoteId} className="llm-health-note">{healthActionNote}</span>
    {providerBlockedReason && <span id={blockNoteId} className="llm-health-note is-blocked">
      {providerBlockedReason}
    </span>}
    {unsavedCount > 0 && <span id={draftNoteId} className="llm-health-note">
      {countLabel(unsavedCount, 'draft change')} excluded
    </span>}
    {visibleStatus && <span className="llm-health-result" role="status" aria-live="polite">
      <span className={'chip llm-health-status ' + (visibleStatus.ok ? 'ok'
        : visibleStatus.unresolved || visibleStatus.configurationChanged || visibleStatus.anotherCheckBusy
          ? 'warn' : 'alarm')}
                       title={visibleStatus.ok
                         ? visibleStatus.previousConfiguration
                           ? 'The previous saved LLM configuration responded successfully; the current active configuration was not contacted.'
                           : 'The server-resolved active LLM responded successfully.'
                         : visibleStatus.error || 'Check the active provider configuration and network access.'}>
        {visibleStatus.ok ? '✓' : visibleStatus.unresolved || visibleStatus.configurationChanged
          || visibleStatus.anotherCheckBusy ? '!' : '×'} {visibleStatus.configurationChanged
          ? 'Reload saved settings'
          : visibleStatus.replayUnavailable ? 'Previous result unavailable'
          : visibleStatus.terminalUnknown ? 'Provider outcome unresolved'
          : visibleStatus.reconcilable ? 'Previous result pending'
          : visibleStatus.anotherCheckBusy ? 'Another check is running'
          : visibleStatus.ok ? visibleStatus.previousConfiguration
            ? 'Previous LLM responded' : 'Active LLM responded'
          : visibleStatus.notStarted ? 'Check not started'
          : visibleStatus.previousConfiguration ? 'Previous LLM failed' : 'Active LLM failed'}
      </span>
      {visibleStatus.previousConfiguration && <span className="llm-health-detail">
        Result belongs to the previous saved configuration; the current active LLM was not contacted.
      </span>}
      {!visibleStatus.ok && visibleStatus.error && <span className="llm-health-detail">{visibleStatus.error}</span>}
    </span>}
  </span>
}

