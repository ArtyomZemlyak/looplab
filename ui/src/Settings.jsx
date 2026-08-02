import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createIdempotencyKey, deadlineGet, saveSettings, saveSecret, llmHealth } from './util.js'
import {
  toForm, fromForm, settingsSavePayload, settingsValidationErrors, loadSettingsSchema,
} from './settingsSchema.js'
import {
  filterSettingsGroups, reconcileAcceptedRecord, reconcileUnknownRecord, settingsViewStats,
  validateSecretSaveAck, validateSettingsResource, validateSettingsSaveAck,
} from './settingsModel.js'
import SettingsForm from './SettingsForm.jsx'
import { OpIcon } from './icons.jsx'
import { deadlineRequest } from './requestDeadline.js'
import { installNavigationLossGuard } from './navigationLossGuard.js'
import { publish as publishSettingsLaunchGuard } from './settingsLaunchGuard.js'

const countLabel = (count, singular, plural = `${singular}s`) => `${count} ${count === 1 ? singular : plural}`
const SETTINGS_READ_TIMEOUT_MS = 15_000
const SETTINGS_WRITE_TIMEOUT_MS = 15_000
// The server permits a 60s provider wall limit plus bounded teardown. The browser must keep the
// same operation alive long enough to receive that authoritative result instead of timing out first.
const LLM_HEALTH_TIMEOUT_MS = 70_000
const unknownTransport = error => !Number.isInteger(error?.status)
  || error.status >= 500 || [408, 425, 429].includes(error.status)
const publicSubmittedForm = form => ({ ...(form || {}), llm_api_key: '' })
const boundedSettingsWrite = work =>
  deadlineRequest(signal => work(signal), SETTINGS_WRITE_TIMEOUT_MS).promise
const navigationWarning = (busy, unknown = false, healthRecovery = false) => busy === 'testing-llm'
  ? 'An LLM provider check is still in flight and may complete or be billed after you leave. Leave this page anyway?'
  : busy
    ? 'A settings action is still in flight and may finish after you leave. Leave this page anyway?'
    : unknown
      ? 'A settings action has an unresolved server outcome. Leave this page anyway?'
      : healthRecovery
        ? 'An LLM provider outcome is unresolved. Leaving may discard the visible recovery warning; leave anyway?'
      : 'Discard unsaved settings changes and leave this page?'

const launchGuardState = ({
  loaded, loadError, invalidCount, unsaved, mutationBusy, mutationUnknown,
  healthRecoveryBlocked,
}) => {
  if (mutationBusy === 'saving') return {
    blocked: true,
    status: 'saving',
    reason: 'Settings are being saved. Wait for the server acknowledgement before starting a run.',
  }
  if (mutationBusy === 'reconciling' || mutationBusy === 'reloading settings') return {
    blocked: true,
    status: 'recovering',
    reason: 'Saved settings are being refreshed. Wait for authoritative server state before starting a run.',
  }
  if (mutationBusy === 'clearing secret') return {
    blocked: true,
    status: 'saving',
    reason: 'A saved credential change is in progress. Wait for its server acknowledgement before starting a run.',
  }
  if (mutationBusy === 'testing-llm') return {
    blocked: true,
    status: 'recovering',
    reason: 'The active LLM check is still in progress. Wait for its verified outcome before starting a run.',
  }
  if (mutationBusy) return {
    blocked: true,
    status: 'saving',
    reason: 'A Settings operation is still in progress. Wait for it to finish before starting a run.',
  }
  if (mutationUnknown) return {
    blocked: true,
    status: 'unknown',
    reason: 'The outcome of a Settings update is unknown. Refresh server state before starting a run.',
  }
  if (healthRecoveryBlocked) return {
    blocked: true,
    status: 'unknown',
    reason: 'An active LLM provider outcome is unresolved. Resolve or acknowledge it before starting a run.',
  }
  if (loadError) return {
    blocked: true,
    status: 'load-error',
    reason: 'Settings could not be loaded or refreshed. Retry before starting a run.',
  }
  if (!loaded) return {
    blocked: true,
    status: 'loading',
    reason: 'Settings are still loading. Wait for saved defaults before starting a run.',
  }
  if (invalidCount > 0) return {
    blocked: true,
    status: 'invalid',
    reason: `${countLabel(invalidCount, 'invalid setting')} must be fixed before starting a run.`,
  }
  if (unsaved) return {
    blocked: true,
    status: 'unsaved',
    reason: 'Save or discard the Settings draft before starting a run.',
  }
  return {
    blocked: false,
    status: 'ready',
    reason: 'Settings are saved and ready for a new run.',
  }
}

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
const readHealthRecovery = () => {
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
}) {
  const [status, setStatus] = useState(null)
  const [busyContext, setBusyContext] = useState(null)
  const requestRef = useRef(null)
  const identityRef = useRef(null)
  const previousIdentity = identityRef.current
  const identityChanged = !previousIdentity
    || previousIdentity.savedSettingsRevision !== savedSettingsRevision
    || previousIdentity.savedSecretRevision !== savedSecretRevision
  if (identityChanged) {
    identityRef.current = {
      savedSettingsRevision,
      savedSecretRevision,
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
    if (actionBlocked || requestRef.current || !revisionsReady) return
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
    if (actionBlocked || requestRef.current || !revisionsReady || !visibleStatus?.terminalUnknown) return
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
    : revisionsReady
      ? 'Starts one explicit check of the server-resolved active LLM. Unsaved edits and typed API keys are excluded; environment or .env configuration may override the saved store. The browser does not auto-retry.'
      : 'Load saved settings before testing the active LLM.'
  const activeReplayOnly = busy && requestRef.current?.replayOnly === true
  const healthActionNote = reloading || visibleStatus?.configurationChanged
    ? 'Reload only · no provider request'
    : activeReplayOnly || visibleStatus?.reconcilable
      ? 'Replay only · no new provider request'
      : visibleStatus?.terminalUnknown
        ? 'A new check requires confirmation and may bill again'
        : busy
          ? 'Provider check in progress and may be billed'
          : 'One provider request may be billed'
  const healthDescription = ['llm-health-action-note',
    unsavedCount > 0 ? 'llm-health-draft-note' : ''].filter(Boolean).join(' ')
  return <span className="llm-health">
    <button type="button" className="btn sm"
            disabled={actionBlocked || busy || !revisionsReady || visibleStatus?.terminalUnknown}
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
      disabled={actionBlocked || busy || !revisionsReady} onClick={startNewAfterUnknown}
      aria-describedby="llm-health-action-note"
      title="Requires confirmation because this creates a new provider operation that may be billed.">
      Start new check (may bill)
    </button>}
    {visibleStatus?.unresolved && <button type="button" className="btn sm ghost"
      disabled={actionBlocked || busy} onClick={dismissRecovery}
      title="Acknowledge the unknown outcome and remove its recovery gate without contacting the provider.">
      Dismiss warning
    </button>}
    <span id="llm-health-action-note" className="llm-health-note">{healthActionNote}</span>
    {unsavedCount > 0 && <span id="llm-health-draft-note" className="llm-health-note">
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

// Full-page editor for the engine defaults used by every new run. Per-run overrides remain in each
// run's Settings panel; this page deliberately starts with the small set most people need.
export default function Settings({ onBack }) {
  const [defaults, setDefaults] = useState(null)
  const [schema, setSchema] = useState(null)
  const [form, setForm] = useState(null)
  const [saved, setSaved] = useState(null)
  const [agentControl, setAgentControl] = useState({})
  const [savedAC, setSavedAC] = useState({})
  const [secretState, setSecretState] = useState({})
  const [revisions, setRevisions] = useState({ settings: '', secret: '' })
  const [loadError, setLoadError] = useState('')
  const [toast, setToast] = useState(null)
  const [mode, setMode] = useState('essential')
  const [query, setQuery] = useState('')
  const [mutationBusy, setMutationBusy] = useState('')
  const [mutationUnknown, setMutationUnknown] = useState(null)
  const [healthRecoveryActive, setHealthRecoveryActive] = useState(false)
  const [invalidFocus, setInvalidFocus] = useState({ key: '', request: 0 })
  const mutationRef = useRef(null)
  const toastTimer = useRef(null)
  const loadRef = useRef(0)
  const loadControllerRef = useRef(null)
  const allowNavigationRef = useRef(false)
  const settingsHashRef = useRef(typeof location === 'undefined' ? '#/settings' : location.hash)

  const load = (reloadSchema = false, preserveExisting = false) => {
    const owner = ++loadRef.current
    loadControllerRef.current?.abort()
    const timed = deadlineGet('/api/settings', SETTINGS_READ_TIMEOUT_MS)
    loadControllerRef.current = timed.controller
    setLoadError('')
    return Promise.all([timed.promise, loadSettingsSchema({ reload: reloadSchema })]).then(([data, nextSchema]) => {
      if (loadRef.current !== owner) return false
      validateSettingsResource(data, nextSchema)
      const settings = data.settings || {}
      const nextForm = toForm(settings, nextSchema)
      setDefaults(data.defaults)
      setSchema(nextSchema)
      setForm(nextForm)
      setSaved(nextForm)
      const control = settings.agent_control || {}
      setAgentControl(control)
      setSavedAC(control)
      setSecretState({ llm_api_key: !!settings.llm_api_key })
      setRevisions({ settings: data.settings_revision, secret: data.secret_revision })
      return true
    }).catch(() => {
      if (loadRef.current === owner) {
        if (!preserveExisting) setSchema(null)
        setLoadError('Settings or their editor schema could not be loaded.')
      }
      return false
    }).finally(() => {
      if (loadRef.current === owner && loadControllerRef.current === timed.controller) {
        loadControllerRef.current = null
      }
    })
  }
  useEffect(() => {
    load()
    // The toast timer is torn down here too: it outlived the component otherwise, and firing
    // setToast on an unmounted Settings view is a wasted render at best.
    return () => {
      loadRef.current += 1
      loadControllerRef.current?.abort()
      if (toastTimer.current) clearTimeout(toastTimer.current)
    }
  }, [])

  // Values that differ from engine defaults stay marked after Save.
  const dirty = useMemo(() => {
    if (!form || !defaults || !schema) return new Set()
    const current = fromForm(form, schema)
    const changed = new Set()
    for (const key of Object.keys(schema.fieldByKey)) {
      if (schema.fieldByKey[key].type === 'secret') continue
      const defaultValue = defaults[key] ?? (schema.fieldByKey[key].type === 'list' ? [] : null)
      if (JSON.stringify(current[key]) !== JSON.stringify(defaultValue ?? null)) changed.add(key)
    }
    return changed
  }, [form, defaults, schema])

  const validationErrors = useMemo(() => form && schema
    ? settingsValidationErrors(form, schema) : {}, [form, schema])

  // Valid form values compare in their persisted/coerced shape (an input's "8" is the saved number 8).
  // Invalid transitional input stays unsaved in its raw shape; secrets are write-only and stay raw too.
  const unsavedKeys = useMemo(() => {
    const changed = new Set()
    if (form && saved && schema) {
      const currentValues = fromForm(form, schema)
      const savedValues = fromForm(saved, schema)
      for (const key of Object.keys(form)) {
        const field = schema.fieldByKey?.[key]
        if (field?.type === 'secret') {
          if (JSON.stringify(form[key]) !== JSON.stringify(saved[key])) changed.add(key)
        } else if (validationErrors[key]
          || JSON.stringify(currentValues[key]) !== JSON.stringify(savedValues[key])) {
          changed.add(key)
        }
      }
    }
    const controlKeys = new Set([...Object.keys(agentControl || {}), ...Object.keys(savedAC || {})])
    for (const key of controlKeys) {
      if (JSON.stringify((agentControl || {})[key] || []) !== JSON.stringify((savedAC || {})[key] || [])) changed.add(key)
    }
    return changed
  }, [form, saved, schema, validationErrors, agentControl, savedAC])
  const unsaved = unsavedKeys.size > 0
  const invalidCount = Object.keys(validationErrors).length
  // The storage fence is written synchronously before a provider POST, while the child-to-parent
  // status callback lands in an effect. Read both so a same-tick Save/clear/navigation cannot slip
  // through that render gap.
  const healthRecoveryBlocked = healthRecoveryActive || !!readHealthRecovery()
  const navigationUnsafe = unsaved || !!mutationBusy || !!mutationUnknown || healthRecoveryBlocked
  const launchDefaultsLoaded = !!form && !!schema && !!defaults && !!saved
    && typeof revisions.settings === 'string' && revisions.settings.length > 0
    && typeof revisions.secret === 'string' && revisions.secret.length > 0

  // The persistent Assistant lives beside this route. Publish before paint so its paid launch CTA
  // cannot observe one permissive frame while Settings is mounting or changing state.
  useLayoutEffect(() => () => {
    publishSettingsLaunchGuard({ active: false })
  }, [])
  useLayoutEffect(() => {
    publishSettingsLaunchGuard({
      active: true,
      ...launchGuardState({
        loaded: launchDefaultsLoaded,
        loadError,
        invalidCount,
        unsaved,
        mutationBusy,
        mutationUnknown,
        healthRecoveryBlocked,
      }),
    })
  }, [healthRecoveryBlocked, invalidCount, launchDefaultsLoaded, loadError,
    mutationBusy, mutationUnknown, unsaved])

  useEffect(() => {
    if (!navigationUnsafe) return undefined
    // Capture listeners run before App's ordinary route listeners. Restore Settings before App
    // reads location so a cancelled browser/hash navigation cannot unmount the draft.
    return installNavigationLossGuard({
      allowRef: allowNavigationRef,
      guardedHash: settingsHashRef.current,
      message: () => navigationWarning(mutationBusy, !!mutationUnknown, healthRecoveryBlocked),
    })
  }, [navigationUnsafe, mutationBusy, mutationUnknown, healthRecoveryBlocked])

  const visibleGroups = useMemo(() => schema
    ? filterSettingsGroups(schema.groups, { mode, query }) : [], [mode, query, schema])
  const visibleStats = useMemo(() => settingsViewStats(visibleGroups), [visibleGroups])
  const hiddenUnsaved = [...unsavedKeys].filter(key => !visibleStats.keys.has(key)).length
  const searching = !!query.trim()
  const catalogueSummary = searching
    ? `${countLabel(visibleStats.fields, 'match', 'matches')} across all settings`
    : mode === 'essential'
      ? countLabel(visibleStats.fields, 'essential setting')
      : `${countLabel(visibleStats.fields, 'setting')} in ${countLabel(visibleStats.groups, 'section')}`

  const onChange = (key, value) => {
    if (mutationRef.current?.kind === 'reloading settings') return
    setForm(current => ({ ...current, [key]: value }))
  }
  const onToggleAgent = (key, role) => setAgentControl(current => {
    if (mutationRef.current?.kind === 'reloading settings') return current
    const roles = new Set(current[key] || [])
    roles.has(role) ? roles.delete(role) : roles.add(role)
    return { ...current, [key]: [...roles] }
  })
  const show = message => {
    // Clear the previous timer so a second toast isn't hidden early by the first one's timeout —
    // the same fix RunView.jsx::showToast carries. Two saves within 2.5s used to leave the second
    // confirmation on screen for whatever was left of the first one's window.
    if (toastTimer.current) clearTimeout(toastTimer.current)
    setToast(message)
    toastTimer.current = setTimeout(() => setToast(null), 2500)
  }
  // State-driven `disabled` attributes render one tick after a click. The token closes that gap so
  // save and secret-clear can never issue overlapping writes, even under a same-tick double click.
  const beginMutation = kind => {
    if (mutationRef.current || (mutationUnknown && kind !== 'reconciling')) return null
    const token = { kind }
    mutationRef.current = token
    setMutationBusy(kind)
    return token
  }
  const finishMutation = token => {
    if (mutationRef.current !== token) return
    mutationRef.current = null
    setMutationBusy('')
  }
  const rememberUnknown = (stage, submittedForm, submittedControl = {},
    uncertainKeys = [], uncertainControlKeys = []) => {
    // Never copy a credential into recovery metadata, logs or error UI. The controlled password
    // field already owns the in-memory draft; recovery retains only its non-secret comparison shape.
    const normalizedSubmitted = schema
      ? toForm(fromForm(submittedForm, schema), schema) : submittedForm
    setMutationUnknown({ stage, submittedForm: publicSubmittedForm(normalizedSubmitted),
      submittedControl, uncertainKeys, uncertainControlKeys,
      preserveSecret: stage === 'secret-set' || stage === 'secret-conflict' })
  }
  const focusFirstInvalid = () => {
    const first = Object.keys(validationErrors)[0]
    if (!first) return
    setQuery(first)
    setMode('all')
    setInvalidFocus(previous => ({ key: first, request: previous.request + 1 }))
  }
  const reconcileUnknown = async () => {
    const recovery = mutationUnknown
    if (!recovery) return
    const mutation = beginMutation('reconciling')
    if (!mutation) return
    const timed = deadlineGet('/api/settings', SETTINGS_READ_TIMEOUT_MS)
    try {
      const data = await timed.promise
      validateSettingsResource(data, schema)
      const settings = data.settings || {}
      const acceptedForm = toForm(settings, schema)
      const acceptedControl = settings.agent_control || {}
      setDefaults(data.defaults)
      setSaved(acceptedForm); setSavedAC(acceptedControl)
      setForm(current => {
        const next = reconcileUnknownRecord(
          current, recovery.submittedForm, acceptedForm, recovery.uncertainKeys,
        )
        // GET can report only that a credential exists, never which replacement won. Retain the
        // password-box draft for deliberate review; Test active LLM never sends it. The server
        // resolves the active credential, including any environment/.env override.
        if (recovery.preserveSecret) next.llm_api_key = current?.llm_api_key || ''
        return next
      })
      setAgentControl(current => reconcileUnknownRecord(
        current, recovery.submittedControl, acceptedControl, recovery.uncertainControlKeys))
      setSecretState({ llm_api_key: !!settings.llm_api_key })
      setRevisions({ settings: data.settings_revision, secret: data.secret_revision })
      setMutationUnknown(null)
      show(recovery.preserveSecret
        ? 'Server state refreshed; the typed API-key draft was not sent, and Test active LLM uses the server-resolved credential'
        : recovery.stage.endsWith('-conflict')
        ? 'Current server settings loaded; review the retained draft before saving again'
        : 'Settings refreshed from the server; the unknown write was not replayed')
    } catch {
      show('Could not refresh authoritative settings; the previous outcome is still unknown')
    } finally {
      finishMutation(mutation)
    }
  }
  const onSave = async () => {
    if (healthRecoveryBlocked) {
      show('Acknowledge or resolve the LLM provider warning before saving a new configuration')
      return
    }
    if (invalidCount) {
      show(`Fix ${countLabel(invalidCount, 'invalid setting')} before saving`)
      focusFirstInvalid()
      return
    }
    const mutation = beginMutation('saving')
    if (!mutation) { show('A settings update is already in progress'); return }
    const submittedForm = form
    const submittedControl = agentControl
    const submittedSettingsRevision = revisions.settings
    const submittedSecretRevision = revisions.secret
    const apiKey = (submittedForm.llm_api_key || '').trim()
    let settingsPatch = null
    try {
      settingsPatch = settingsSavePayload(submittedForm, submittedControl, saved, savedAC, schema)
      const settingsChanged = Object.keys(settingsPatch).length > 0
      // PATCH only edits since this tab's baseline; replaying the full stale form
      // would overwrite disjoint settings saved by another tab after this one loaded.
      const result = validateSettingsSaveAck(await boundedSettingsWrite(
        signal => saveSettings(settingsPatch, {
          signal, expectedRevision: submittedSettingsRevision,
        })), schema)
      const acceptedForm = toForm(result.settings, schema)
      const acceptedControl = result.settings.agent_control || {}

      // Commit the ordinary-settings ACK immediately. If the independent secret write fails, the
      // accepted baseline must not be resent, while the API-key input remains available to retry.
      setSaved(acceptedForm)
      setSavedAC(acceptedControl)
      setRevisions(current => ({ ...current, settings: result.settings_revision }))
      const formBeforeSecretAck = apiKey
        ? { ...acceptedForm, llm_api_key: submittedForm.llm_api_key }
        : acceptedForm
      setForm(current => reconcileAcceptedRecord(current, submittedForm, formBeforeSecretAck))
      setAgentControl(current => reconcileAcceptedRecord(current, submittedControl, acceptedControl))
      if (apiKey) {
        let resultSecret
        try {
          resultSecret = validateSecretSaveAck(await boundedSettingsWrite(
            signal => saveSecret('llm_api_key', apiKey, {
              signal, expectedRevision: submittedSecretRevision,
            })), 'llm_api_key')
        } catch (error) {
          if (error?.code === 'secret_revision_conflict') {
            rememberUnknown('secret-conflict', submittedForm, submittedControl)
            return
          }
          if (unknownTransport(error)) {
            rememberUnknown('secret-set', submittedForm, submittedControl)
            return
          }
          const prefix = settingsChanged
            ? 'Settings saved, but the API key was not stored: '
            : 'API key was not stored: '
          show(prefix + (error.message || error))
          return
        }
        setSecretState(current => ({ ...current, llm_api_key: resultSecret.set === true }))
        setRevisions(current => ({ ...current, secret: resultSecret.secret_revision }))
        if (resultSecret.set !== true) {
          show(settingsChanged
            ? 'Settings saved, but the API key was not stored'
            : 'API key was not stored')
          return
        }
        // Clear only the submitted credential. A replacement typed while either request was in
        // flight remains an unsaved edit instead of being erased by the older acknowledgement.
        setForm(current => reconcileAcceptedRecord(current, submittedForm, acceptedForm))
      }
      const savedParts = [settingsChanged ? 'Submitted settings saved' : '', apiKey ? 'API key stored securely' : ''].filter(Boolean)
      show(`${savedParts.join(' · ') || 'No persisted changes'} — applied to new runs`)
    } catch (error) {
      if (settingsPatch && error?.code === 'settings_revision_conflict') {
        rememberUnknown(
          'settings-conflict', submittedForm, submittedControl,
          Object.keys(settingsPatch).filter(key => key !== 'agent_control'),
          Object.keys(settingsPatch.agent_control || {}),
        )
      }
      else if (settingsPatch && unknownTransport(error)) {
        rememberUnknown(
          'settings-save', submittedForm, submittedControl,
          Object.keys(settingsPatch).filter(key => key !== 'agent_control'),
          Object.keys(settingsPatch.agent_control || {}),
        )
      }
      else show('Save failed: ' + error.message)
    } finally {
      finishMutation(mutation)
    }
  }
  const onClearSecret = async key => {
    if (healthRecoveryBlocked) {
      show('Acknowledge or resolve the LLM provider warning before changing its credential')
      return
    }
    if (!window.confirm('Clear the stored API key now? This is immediate, separate from Save, and cannot be undone. Any typed replacement stays as an unsaved draft.')) return
    const mutation = beginMutation('clearing secret')
    if (!mutation) { show('A settings update is already in progress'); return }
    const submittedForm = form
    const submittedSecretRevision = revisions.secret
    try {
      const resultSecret = validateSecretSaveAck(await boundedSettingsWrite(
        signal => saveSecret(key, '', {
          signal, expectedRevision: submittedSecretRevision,
        })), key)
      setSecretState(current => ({ ...current, [key]: false }))
      setRevisions(current => ({ ...current, secret: resultSecret.secret_revision }))
      show(submittedForm?.[key]
        ? 'Stored API key cleared; the typed replacement remains an unsaved draft'
        : 'API key cleared')
    } catch (error) {
      if (error?.code === 'secret_revision_conflict') {
        rememberUnknown('secret-clear-conflict', submittedForm)
      }
      else if (unknownTransport(error)) rememberUnknown('secret-clear', submittedForm)
      else show('Clear failed: ' + error.message)
    } finally {
      finishMutation(mutation)
    }
  }
  const resetToDefaults = () => {
    if (mutationRef.current?.kind === 'reloading settings') return
    if (defaults) {
      setForm(toForm(defaults, schema))
      setAgentControl(defaults.agent_control || {})
    }
  }
  const revealChanges = () => {
    setQuery('')
    setMode('all')
  }
  const requestBack = () => {
    if (navigationUnsafe && !window.confirm(navigationWarning(
      mutationBusy, !!mutationUnknown, healthRecoveryBlocked))) return
    allowNavigationRef.current = true
    onBack()
  }

  return <div className="app">
    <div className="topbar">
      <span className="brand"><span className="dot">◉</span> LoopLab</span>
      <button className="btn sm ghost" onClick={requestBack}>← runs</button>
      <span className="ttl" style={{ fontWeight: 700, fontSize: 15 }}>Settings</span>
      <span className="muted">engine defaults for new runs</span>
      <span className="spacer" style={{ flex: 1 }} />
    </div>

    <main className="settings-page" data-route-main tabIndex={-1}>
      {!form || !schema ? (loadError
        ? <div className="notice resource-error" role="alert"><b>Could not load settings.</b><span>{loadError}</span><button className="btn sm primary" onClick={() => load(true)}>Retry</button></div>
        : <div className="notice" role="status">Loading settings…</div>) : <>
        <section className="settings-overview" aria-labelledby="settings-heading">
          <div className="settings-heading-row">
            <div>
              <h1 id="settings-heading">Engine defaults</h1>
              <p>Applied to every new run. A run can still override these values before launch.</p>
            </div>
            <details className="settings-help">
              <summary>How changes work</summary>
              <p><span className="sf-dot unsaved">●</span> Unsaved edits clear after Save.
                <span className="sf-dot fromdefault">●</span> Customized values differ from the engine default.</p>
              <p>R, S, and B control whether the Researcher, Strategist, or Boss may change a setting at runtime.</p>
            </details>
          </div>

          <div className="settings-toolbar">
            <div className="settings-mode-block">
              <span id="settings-mode-label" className="settings-control-label">Visible settings</span>
              <div className="settings-mode" role="group" aria-labelledby="settings-mode-label">
                <button type="button" className={mode === 'essential' ? 'active' : ''}
                        aria-pressed={mode === 'essential'} disabled={searching}
                        onClick={() => setMode('essential')}>Essential</button>
                <button type="button" className={mode === 'all' ? 'active' : ''}
                        aria-pressed={mode === 'all'} disabled={searching}
                        onClick={() => setMode('all')}>All</button>
              </div>
            </div>
            <div className="settings-search-block">
              <label className="settings-control-label" htmlFor="settings-search">Find a setting</label>
              <div className="settings-search-control">
                <OpIcon name="search" className="t-ic" />
                <input id="settings-search" type="search" value={query}
                       aria-describedby={searching ? 'settings-search-scope' : undefined}
                       placeholder="Name, key, option, or purpose…"
                       onChange={event => setQuery(event.target.value)} />
                {query && <button type="button" className="settings-search-clear"
                                  aria-label="Clear settings search" onClick={() => setQuery('')}>×</button>}
              </div>
              {searching && <span id="settings-search-scope" className="settings-search-scope">Search includes advanced settings.</span>}
            </div>
          </div>

          <div className="settings-summary" role="status" aria-live="polite">
            <span>{catalogueSummary}</span>
            <span className="settings-summary-divider" aria-hidden="true">·</span>
            <span className={unsaved ? 'is-unsaved' : ''}>{unsaved ? countLabel(unsavedKeys.size, 'unsaved change') : 'No unsaved changes'}</span>
            <span className="settings-summary-divider" aria-hidden="true">·</span>
            <span>{countLabel(dirty.size, 'customized value')}</span>
            {hiddenUnsaved > 0 && <button type="button" className="settings-summary-link" onClick={revealChanges}>
              Review {countLabel(hiddenUnsaved, 'hidden change')}
            </button>}
          </div>
        </section>

        {mutationUnknown && <div className="notice resource-error" role="alert">
          <b>{mutationUnknown.stage.endsWith('-conflict')
            ? 'Server state changed in another client.' : 'Update outcome unknown.'}</b>
          <span>{mutationUnknown.stage === 'settings-conflict'
            ? 'Your draft is retained. Refresh the current server state before deliberately saving it against the new revision.'
            : mutationUnknown.stage === 'secret-conflict'
              ? 'Ordinary settings were accepted, but another credential update won. The typed replacement is retained and is not sent by Test active LLM; the server resolves its active credential.'
            : mutationUnknown.stage === 'secret-clear-conflict'
              ? 'Another credential update won before this clear. Refresh before deciding whether to clear the current credential.'
            : mutationUnknown.stage === 'secret-set'
            ? 'Ordinary settings were accepted, but the API-key replacement could not be confirmed. The typed draft is retained and is not sent by Test active LLM; the server resolves its active credential.'
            : mutationUnknown.stage === 'secret-clear'
              ? 'The API-key clear may or may not have reached the server. Do not repeat it blindly.'
              : 'The settings save may or may not have reached the server. Current edits are kept and will not be replayed automatically.'}</span>
          <button className="btn sm primary" disabled={!!mutationBusy} onClick={reconcileUnknown}>
            {mutationBusy === 'reconciling' ? 'Refreshing…' : 'Refresh server state'}
          </button>
        </div>}

        <SettingsForm form={form} onChange={onChange} dirty={dirty} unsaved={unsavedKeys}
                      errors={validationErrors}
                      agentControl={agentControl} onToggleAgent={onToggleAgent}
                      secretState={secretState} onClearSecret={onClearSecret}
                      secretActionDisabled={!!mutationBusy || !!mutationUnknown || healthRecoveryBlocked}
                      interactionDisabled={mutationBusy === 'reloading settings'}
                      mode={mode} query={query} schema={schema}
                      focusKey={invalidFocus.key} focusRequest={invalidFocus.request} />
      </>}
    </main>

    {form && schema && <div className="settings-actions"><div className="sa-inner">
      <LlmHealth savedSettingsRevision={revisions.settings} savedSecretRevision={revisions.secret}
        unsavedCount={unsavedKeys.size}
        actionBlocked={!!mutationBusy || !!mutationUnknown}
        actionKind={mutationBusy}
        beginAction={beginMutation} finishAction={finishMutation}
        onRecoveryChange={setHealthRecoveryActive}
        reloadSavedSettings={async () => {
          if (unsaved && !window.confirm('Reload saved settings and discard the current draft changes?')) return false
          const mutation = beginMutation('reloading settings')
          if (!mutation) return false
          try {
            const reloaded = await load(false, true)
            if (!reloaded) show('Saved settings could not be reloaded; current values and the provider warning were kept')
            return reloaded
          }
          finally { finishMutation(mutation) }
        }} />
      <span className="spacer" style={{ flex: 1 }} />
      {invalidCount
        ? <button type="button" className="settings-summary-link settings-save-state is-invalid"
            onClick={focusFirstInvalid}>{countLabel(invalidCount, 'invalid setting')} — review</button>
        : <span className={'settings-save-state' + (unsaved ? ' is-unsaved' : '')}
            role="status" aria-live="polite">
          {unsaved ? countLabel(unsavedKeys.size, 'unsaved change') : 'All changes saved'}
        </span>}
      <button className="btn sm ghost" disabled={mutationBusy === 'reloading settings'} onClick={resetToDefaults}
              title="Reset every field to the engine default">↻ Defaults</button>
      <button className="btn sm primary" disabled={!unsaved || invalidCount > 0 || !!mutationBusy || !!mutationUnknown || healthRecoveryBlocked} onClick={onSave}>
        {mutationBusy === 'saving' ? 'Saving...' : 'Save'}
      </button>
    </div></div>}
    {toast && <div className="toast" role="status">{toast}</div>}
  </div>
}
