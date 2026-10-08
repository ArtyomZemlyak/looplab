import { uiText, uiMessage, uiPlural, useUILanguage } from './uiLanguage.js'
import { setUILanguage } from './uiLanguage.js'
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { createIdempotencyKey, deadlineGet, saveSettings, saveSecret } from './util.js'
import {
  toForm, fromForm, settingsSavePayload, settingsValidationErrors, loadSettingsSchema, sameAgentRoles,
} from './settingsSchema.js'
import {
  ambientVariables, filterSettingsGroups, mismatchDetail, reconcileAcceptedRecord,
  reconcileUnknownRecord, settingsViewStats, validateSecretSaveAck, validateSettingsResource,
  validateSettingsSaveAck,
} from './settingsModel.js'
import SettingsForm from './SettingsForm.jsx'
import GlobalMenu from './GlobalMenu.jsx'
import { OpIcon } from './icons.jsx'
import { deadlineRequest } from './requestDeadline.js'
import { installNavigationLossGuard } from './navigationLossGuard.js'
import {
  beginOperation as beginSettingsLaunchOperation,
  captureAuthoritativeRead as captureSettingsLaunchRead,
  claimPublisher as claimSettingsLaunchGuard,
  confirmAuthoritativeRead as confirmSettingsLaunchRead,
  getSnapshot as getSettingsLaunchGuard,
  publish as publishSettingsLaunchGuard,
  releasePublisher as releaseSettingsLaunchGuard,
  settleOperation as settleSettingsLaunchOperation,
  subscribe as subscribeSettingsLaunchGuard,
} from './settingsLaunchGuard.js'
import { DIALOG_PRIORITY, useDialogFocus } from './useDialogFocus.js'
import { useToast } from './useToast.js'
import { LlmHealth, readHealthRecovery, unknownTransport } from './LlmHealth.jsx'
export { LlmHealth, LLM_HEALTH_TIMEOUT_MS } from './LlmHealth.jsx'

const CREDENTIAL_SOURCE_LABELS = {
  stored: 'Stored secret',
  environment: 'Process environment',
  dotenv: '.env file',
  none: 'None',
}
const CREDENTIAL_STATUS_LABELS = {
  active: 'Matches base URL',
  missing: 'No shared key',
  incomplete: 'Incomplete pair',
  unbound: 'Unbound',
  endpoint_mismatch: 'Base URL mismatch',
  ambient_override: 'Ambient override',
}
const credentialBindingProblem = credential => credential?.status === 'incomplete'
  || credential?.status === 'unbound'
  || credential?.status === 'endpoint_mismatch'
const SETTINGS_READ_TIMEOUT_MS = 15_000
const SETTINGS_WRITE_TIMEOUT_MS = 15_000
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
  healthRecoveryBlocked, credentialBlockedReason,
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
  if (credentialBlockedReason) return {
    blocked: true,
    status: 'invalid',
    reason: `The saved LLM credential is not ready: ${credentialBlockedReason}`,
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
    reason: uiPlural(invalidCount, '{0} invalid setting must be fixed before starting a run.', '{0} invalid settings must be fixed before starting a run.'),
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

function CredentialState({
  credential, writeError = '', onRefresh, refreshing = false, refreshDisabled = false,
}) {
  useUILanguage()

  if (!credential) return null
  const ambient = credential.source === 'environment' || credential.source === 'dotenv'
    || credential.status === 'ambient_override'
  const ambientEffective = ambient && credential.effective
  const bindingProblem = credentialBindingProblem(credential)
  const bindingNotice = credential.status === 'endpoint_mismatch'
    ? {
        title: 'Shared-key base URL mismatch',
        text: `The shared key is bound to a different normalized base endpoint${mismatchDetail(credential)}. Shared-target actions will fail server preflight until the endpoint and key are saved as one verified pair. A profile with its own bound credential is validated separately.`,
      }
      : credential.status === 'unbound'
        ? {
          title: 'Shared credential is unbound',
          text: 'A shared key is available, but it has no verified binding to the base endpoint. Shared-target actions will fail server preflight until a verified pair is saved. Profile credentials are validated separately.',
        }
        : credential.status === 'incomplete'
          ? {
            title: 'Shared credential pair is incomplete',
            text: (credential.source === 'stored' ? uiText('The stored shared pair has an endpoint binding but no API key. Enter a key to complete it, or clear the incomplete pair for a local endpoint that needs no credential. Profile credentials are validated separately.') : uiMessage("The ambient shared source has an endpoint binding but no API key. Complete or remove that pair in the {0}. A key entered here is only a stored fallback; profile credentials are validated separately.", [CREDENTIAL_SOURCE_LABELS[credential.source] || 'ambient source'])),
          }
          : null
  const ambientNotice = ambient
    ? ambientEffective
      ? {
          title: 'Ambient credential is read-only',
          text: `The effective key comes from ${CREDENTIAL_SOURCE_LABELS[credential.source] || 'an ambient source'}${ambientVariables(credential)} and cannot be changed or cleared here. ${credential.stored
            ? 'Stored credential material remains only a fallback while this override exists.'
            : 'You can enter a key below to store a fallback without replacing this override.'}`,
        }
      : {
          title: 'Ambient source has no effective key',
          text: `${CREDENTIAL_SOURCE_LABELS[credential.source] || 'An ambient source'} controls credential resolution for this process, but it does not currently supply an API key. ${credential.stored
            ? 'Stored credential material remains inactive while this ambient source is selected.'
            : 'A key entered below will be stored only as a fallback and will not replace the ambient source.'}`,
        }
    : null
  return <>
    <dl className="settings-credential-state" aria-label={uiText("Shared credential store state")}>
      {[
        ['Stored material', credential.stored],
        ['Shared key', credential.effective],
        ['Matches base URL', credential.active],
      ].map(([label, value]) => <div key={label}>
        <dt>{uiText(label)}</dt>
        <dd className={value ? 'is-yes' : 'is-no'}>{((value ? uiText('Yes') : uiText('No')))}</dd>
      </div>)}
      <div className="is-wide">
        <dt>{uiText("Source")}</dt>
        <dd>{uiText(CREDENTIAL_SOURCE_LABELS[credential.source])}</dd>
      </div>
      <div className="is-wide">
        <dt>{uiText("Status")}</dt>
        <dd>{uiText(CREDENTIAL_STATUS_LABELS[credential.status])}</dd>
      </div>
    </dl>
    {writeError && <div id="settings-credential-write-warning"
      className="settings-credential-notice is-danger" role="alert">
      <div>
        <strong>{uiText("Replacement key not accepted")}</strong>
        <span>{writeError}{uiText(" The typed replacement is excluded; provider checks and launches continue to use the server-resolved saved/profile configuration.")}</span>
      </div>
      {onRefresh && <button type="button" className="btn sm ghost"
        disabled={refreshing || refreshDisabled} onClick={onRefresh}>
        {((refreshing ? uiText('Refreshing…') : uiText('Refresh server state')))}
      </button>}
    </div>}
    {bindingNotice && <div id="settings-credential-status-warning"
      className={'settings-credential-notice ' + (bindingProblem ? 'is-danger' : 'is-info')}
      role={bindingProblem ? 'alert' : 'note'}>
      <strong>{uiText(bindingNotice.title)}</strong>
      <span>{uiText(bindingNotice.text)}</span>
    </div>}
    {ambientNotice && <div id="settings-credential-ambient-note"
      className="settings-credential-notice is-info" role="note">
      <strong>{uiText(ambientNotice.title)}</strong>
      <span>{uiText(ambientNotice.text)}</span>
    </div>}
  </>
}

function ResetDefaultsDialog({ hasSecretDraft, onCancel, onConfirm }) {
  useUILanguage()

  const dialogRef = useRef(null)
  useDialogFocus(dialogRef, onCancel, true, { priority: DIALOG_PRIORITY.DESTRUCTIVE })
  return <div className="overlay settings-reset-overlay"
    onMouseDown={event => { if (event.target === event.currentTarget) onCancel() }}>
    <section ref={dialogRef} className="modal settings-reset-dialog" role="alertdialog"
      aria-modal="true" aria-labelledby="settings-reset-title"
      aria-describedby={`settings-reset-description${hasSecretDraft
        ? ' settings-reset-credential-warning' : ''} settings-reset-server-note`} tabIndex={-1}>
      <div className="modal-h"><b id="settings-reset-title">{uiText("Reset all draft settings?")}</b></div>
      <div className="modal-b">
        <p id="settings-reset-description" className="settings-reset-copy">{uiText("This loads engine defaults for ordinary settings and runtime access controls, including hidden advanced fields.")}</p>
        {hasSecretDraft && <p id="settings-reset-credential-warning" className="settings-reset-warning">{uiText("Typed credential drafts will be discarded. Stored server credentials are unchanged.")}</p>}
        <p id="settings-reset-server-note" className="settings-reset-note">{uiText("Nothing changes on the server until you choose Save.")}</p>
        <div className="modal-actions">
          <button type="button" className="btn sm" data-dialog-initial-focus onClick={onCancel}>{uiText("Cancel")}</button>
          <button type="button" className="btn sm danger" onClick={onConfirm}>{uiText("Reset draft")}</button>
        </div>
      </div>
    </section>
  </div>
}

// Full-page editor for the engine defaults used by every new run. Per-run overrides remain in each
// run's Settings panel; this page deliberately starts with the small set most people need.
export default function Settings({ onBack, initialSection = '' }) {
  

  const [, , localeRevision] = useUILanguage()

  const settingsLaunchSnapshot = useSyncExternalStore(
    subscribeSettingsLaunchGuard, getSettingsLaunchGuard, getSettingsLaunchGuard)
  const [defaults, setDefaults] = useState(null)
  const [schema, setSchema] = useState(null)
  const [form, setForm] = useState(null)
  const [saved, setSaved] = useState(null)
  const [agentControl, setAgentControl] = useState({})
  const [savedAC, setSavedAC] = useState({})
  const [credential, setCredential] = useState(null)
  const [credentialWriteError, setCredentialWriteError] = useState('')
  const [revisions, setRevisions] = useState({ settings: '', secret: '' })
  const [loadError, setLoadError] = useState('')
  const [toast, show] = useToast(2500)   // shared timer discipline (doc 25 UI-13)
  const [mode, setMode] = useState('essential')
  const [query, setQuery] = useState('')
  const [mutationBusy, setMutationBusy] = useState('')
  const [mutationUnknown, setMutationUnknown] = useState(null)
  const [healthRecoveryActive, setHealthRecoveryActive] = useState(false)
  const [invalidFocus, setInvalidFocus] = useState({ key: '', request: 0 })
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false)
  const mutationRef = useRef(null)
  const loadRef = useRef(0)
  const loadControllerRef = useRef(null)
  const saveButtonRef = useRef(null)
  const saveStateRef = useRef(null)
  const allowNavigationRef = useRef(false)
  const settingsHashRef = useRef(typeof location === 'undefined' ? '#/settings' : location.hash)
  const searchInputRef = useRef(null)
  const providerHeadingRef = useRef(null)
  const loadErrorHeadingRef = useRef(null)
  const recoveryHeadingRef = useRef(null)
  const mutationUnknownHeadingRef = useRef(null)
  const launchGuardOwnerRef = useRef(null)
  const launchGuardReleaseRef = useRef({ retain: false })

  const load = (reloadSchema = false, preserveExisting = false, explicitReconciliation = false) => {
    const owner = ++loadRef.current
    loadControllerRef.current?.abort()
    const timed = deadlineGet('/api/settings', SETTINGS_READ_TIMEOUT_MS)
    const launchRead = captureSettingsLaunchRead(launchGuardOwnerRef.current, {
      explicit: explicitReconciliation,
    })
    loadControllerRef.current = timed.controller
    // Keep a stale-data warning visible while an authoritative refresh is in flight. Clearing it
    // early briefly made launch look safe and removed the only progress affordance on this page.
    if (!preserveExisting) setLoadError('')
    return Promise.all([timed.promise, loadSettingsSchema({ reload: reloadSchema })]).then(([data, nextSchema]) => {
      if (loadRef.current !== owner) return { loaded: false, reconciled: false }
      validateSettingsResource(data, nextSchema)
      const reconciled = confirmSettingsLaunchRead(launchRead)
      const settings = data.settings || {}
      const nextForm = toForm(settings, nextSchema)
      setLoadError('')
      setDefaults(data.defaults)
      setSchema(nextSchema)
      setForm(nextForm)
      setSaved(nextForm)
      const control = settings.agent_control || {}
      setAgentControl(control)
      setSavedAC(control)
      setCredential(data.credential)
      setCredentialWriteError('')
      setRevisions({ settings: data.settings_revision, secret: data.secret_revision })
      return { loaded: true, reconciled }
    }).catch(() => {
      if (loadRef.current === owner) {
        if (!preserveExisting) setSchema(null)
        setLoadError('Settings or their editor schema could not be loaded.')
      }
      return { loaded: false, reconciled: false }
    }).finally(() => {
      if (loadRef.current === owner && loadControllerRef.current === timed.controller) {
        loadControllerRef.current = null
      }
    })
  }
  useEffect(() => {
    load()
    // The toast timer's teardown (it outlived the component otherwise, and firing setToast on an
    // unmounted Settings view is a wasted render at best) now lives in `useToast`.
    return () => {
      loadRef.current += 1
      loadControllerRef.current?.abort()
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
    const defaultControl = defaults.agent_control || {}
    const controlKeys = new Set([...Object.keys(agentControl || {}), ...Object.keys(defaultControl)])
    for (const key of controlKeys) {
      if (!sameAgentRoles((agentControl || {})[key] || [], defaultControl[key] || [])) changed.add(key)
    }
    return changed
  }, [form, defaults, schema, agentControl])
  const hasSecretDraft = useMemo(() => !!form && !!schema
    && Object.entries(schema.fieldByKey).some(([key, field]) => (
      field.type === 'secret' && String(form[key] ?? '').length > 0
    )), [form, schema])
  const canResetDefaults = dirty.size > 0 || hasSecretDraft

  const validationErrors = useMemo(() => form && schema
    ? settingsValidationErrors(form, schema) : {}, [form, schema])
  const savedValidationErrors = useMemo(() => saved && schema
    ? settingsValidationErrors(saved, schema) : {}, [saved, schema])

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
      if (!sameAgentRoles((agentControl || {})[key] || [], (savedAC || {})[key] || [])) changed.add(key)
    }
    return changed
  }, [form, saved, schema, validationErrors, agentControl, savedAC])
  const unsaved = unsavedKeys.size > 0
  const invalidCount = Object.keys(validationErrors).length
  const savedInvalidCount = Object.keys(savedValidationErrors).length
  // The storage fence is written synchronously before a provider POST, while the child-to-parent
  // status callback lands in an effect. Read both so a same-tick Save/clear/navigation cannot slip
  // through that render gap.
  const healthRecoveryBlocked = healthRecoveryActive || !!readHealthRecovery()
  // The card above describes only the shared fallback pair. Named profiles and role/stage targets
  // can use different bound credentials, so the server's target-aware health/start preflight is the
  // authority. Client-side blocking on the shared pair would reject valid profiled configurations.
  const credentialBlockedReason = ''
  const navigationUnsafe = unsaved || !!mutationBusy || !!mutationUnknown || healthRecoveryBlocked
  const launchDefaultsLoaded = !!form && !!schema && !!defaults && !!saved
    && typeof revisions.settings === 'string' && revisions.settings.length > 0
    && typeof revisions.secret === 'string' && revisions.secret.length > 0
  const currentLaunchGuard = launchGuardState({
    loaded: launchDefaultsLoaded,
    loadError,
    invalidCount,
    unsaved,
    mutationBusy,
    mutationUnknown,
    healthRecoveryBlocked,
    credentialBlockedReason: '',
  })
  const launchGuardRetainable = !!loadError || !!mutationBusy || !!mutationUnknown
    || healthRecoveryBlocked || savedInvalidCount > 0
  const launchGuardResolved = launchDefaultsLoaded && !loadError && !mutationBusy
    && !mutationUnknown && !healthRecoveryBlocked
  const launchGuardExternalPending = settingsLaunchSnapshot.status === 'operation-pending'
  const launchGuardExternalRecovery = launchGuardExternalPending
    || settingsLaunchSnapshot.status === 'reconcile-required'
  const settingsActionRecoveryBlocked = launchGuardExternalRecovery || !!loadError
  launchGuardReleaseRef.current = {
    retain: launchGuardRetainable || launchGuardExternalRecovery,
  }

  // The persistent Assistant lives beside this route. Publish before paint so its paid launch CTA
  // cannot observe one permissive frame while Settings is mounting or changing state.
  useLayoutEffect(() => {
    const owner = claimSettingsLaunchGuard()
    launchGuardOwnerRef.current = owner
    return () => {
      // React state may not have rendered yet when navigation follows a click in the same tick. The
      // synchronous mutation token and durable provider-recovery record close that final gap.
      const retain = launchGuardReleaseRef.current.retain
        || !!mutationRef.current || !!readHealthRecovery()
      releaseSettingsLaunchGuard(owner, { retain })
      if (launchGuardOwnerRef.current === owner) launchGuardOwnerRef.current = null
    }
  }, [])
  useLayoutEffect(() => {
    publishSettingsLaunchGuard(launchGuardOwnerRef.current, {
      active: true,
      ...currentLaunchGuard,
      retainable: launchGuardRetainable,
      requiresReconciliation: !!loadError || !!mutationUnknown || healthRecoveryBlocked,
      resolved: launchGuardResolved,
    })
  }, [healthRecoveryBlocked, invalidCount, launchDefaultsLoaded, loadError,
    launchGuardResolved, launchGuardRetainable, mutationBusy, mutationUnknown, unsaved])

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
    ? filterSettingsGroups(schema.groups, { mode, query }) : [], [mode, query, schema, localeRevision])
  const visibleStats = useMemo(() => settingsViewStats(visibleGroups), [visibleGroups])
  const hiddenUnsavedKeys = [...unsavedKeys].filter(key => !visibleStats.keys.has(key))
  const hiddenUnsaved = hiddenUnsavedKeys.length
  const searching = !!query.trim()
  const catalogueSummary = searching
    ? uiPlural(visibleStats.fields, '{0} match across all settings', '{0} matches across all settings')
    : mode === 'essential'
      ? uiPlural(visibleStats.fields, '{0} essential setting', '{0} essential settings')
      : `${uiPlural(visibleStats.fields, '{0} setting', '{0} settings')} ${uiPlural(visibleStats.groups, 'in {0} section', 'in {0} sections')}`

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
  const focusProviderHeading = () => {
    requestAnimationFrame(() => providerHeadingRef.current?.focus())
  }
  const focusSettingsRecovery = () => {
    requestAnimationFrame(() => {
      const target = recoveryHeadingRef.current || mutationUnknownHeadingRef.current
        || loadErrorHeadingRef.current || providerHeadingRef.current
      target?.focus()
    })
  }
  // State-driven `disabled` attributes render one tick after a click. The token closes that gap so
  // save and secret-clear can never issue overlapping writes, even under a same-tick double click.
  const beginMutation = kind => {
    if (mutationRef.current || (mutationUnknown && kind !== 'reconciling')) return null
    const tracksServerWrite = kind === 'saving' || kind === 'clearing secret'
    const launchLease = tracksServerWrite
      ? beginSettingsLaunchOperation(launchGuardOwnerRef.current, kind) : null
    if (tracksServerWrite && !launchLease) return null
    const token = { kind, launchLease, unresolved: false }
    mutationRef.current = token
    setMutationBusy(kind)
    return token
  }
  const finishMutation = token => {
    if (mutationRef.current !== token) return
    if (token.launchLease) {
      settleSettingsLaunchOperation(token.launchLease, { resolved: token.unresolved !== true })
    }
    mutationRef.current = null
    setMutationBusy('')
  }
  const rememberUnknown = (stage, submittedForm, submittedControl = {},
    uncertainKeys = [], uncertainControlKeys = [], ordinarySettingsAccepted = false) => {
    if (mutationRef.current) mutationRef.current.unresolved = true
    // Never copy a credential into recovery metadata, logs or error UI. The controlled password
    // field already owns the in-memory draft; recovery retains only its non-secret comparison shape.
    const normalizedSubmitted = schema
      ? toForm(fromForm(submittedForm, schema), schema) : submittedForm
    setMutationUnknown({ stage, submittedForm: publicSubmittedForm(normalizedSubmitted),
      submittedControl, uncertainKeys, uncertainControlKeys,
      ordinarySettingsAccepted,
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
    const launchRead = captureSettingsLaunchRead(launchGuardOwnerRef.current, { explicit: true })
    try {
      const data = await timed.promise
      validateSettingsResource(data, schema)
      if (!confirmSettingsLaunchRead(launchRead)) {
        show(uiText('An earlier Settings operation changed while this refresh was in flight; refresh again after it settles'))
        focusSettingsRecovery()
        return
      }
      const settings = data.settings || {}
      const acceptedForm = toForm(settings, schema)
      const acceptedControl = settings.agent_control || {}
      // This authoritative read also resolves any older stale-load warning; leaving that warning
      // behind would keep the launch guard blocked after reconciliation had already succeeded.
      setLoadError('')
      setDefaults(data.defaults)
      setSaved(acceptedForm); setSavedAC(acceptedControl)
      setForm(current => {
        const next = reconcileUnknownRecord(
          current, recovery.submittedForm, acceptedForm, recovery.uncertainKeys,
        )
        // GET reports only credential provenance/state, never the secret or which typed replacement
        // won. Retain the password-box draft for deliberate review; Test active LLM never sends it.
        if (recovery.preserveSecret) next.llm_api_key = current?.llm_api_key || ''
        return next
      })
      setAgentControl(current => reconcileUnknownRecord(
        current, recovery.submittedControl, acceptedControl, recovery.uncertainControlKeys))
      setCredential(data.credential)
      setCredentialWriteError('')
      setRevisions({ settings: data.settings_revision, secret: data.secret_revision })
      setMutationUnknown(null)
      show(((recovery.preserveSecret ? uiText('Server state refreshed; the typed API-key draft was not sent, and Test active LLM uses the server-resolved credential') : (recovery.stage.endsWith('-conflict') ? uiText('Current server settings loaded; review the retained draft before saving again') : uiText('Settings refreshed from the server; the unknown write was not replayed')))))
      focusProviderHeading()
    } catch {
      show(uiText('Could not refresh authoritative settings; the previous outcome is still unknown'))
      focusSettingsRecovery()
    } finally {
      finishMutation(mutation)
    }
  }
  const onSave = async () => {
    if (healthRecoveryBlocked) {
      show(uiText('Acknowledge or resolve the LLM provider warning before saving a new configuration'))
      return
    }
    if (invalidCount) {
      show(uiPlural(invalidCount, 'Fix {0} invalid setting before saving', 'Fix {0} invalid settings before saving'))
      focusFirstInvalid()
      return
    }
    const rawSubmittedForm = form
    const rawApiKey = String(rawSubmittedForm?.llm_api_key || '')
    const apiKey = rawApiKey.trim()
    const whitespaceOnlyApiKey = rawApiKey.length > 0 && !apiKey
    const submittedForm = whitespaceOnlyApiKey
      ? { ...rawSubmittedForm, llm_api_key: '' }
      : rawSubmittedForm
    if (whitespaceOnlyApiKey) {
      // The secret endpoint trims too, so an all-whitespace draft can never be persisted. Normalize
      // the controlled field now, but only if the operator has not typed a replacement since click.
      setForm(current => current?.llm_api_key === rawApiKey
        ? { ...current, llm_api_key: '' }
        : current)
    }
    const mutation = beginMutation('saving')
    if (!mutation) { show(uiText('A settings update is already in progress')); return }
    const submittedControl = agentControl
    const submittedRevisions = { ...revisions }
    let settingsPatch = null
    try {
      settingsPatch = settingsSavePayload(submittedForm, submittedControl, saved, savedAC, schema)
      const settingsChanged = Object.keys(settingsPatch).length > 0
      let acceptedForm = saved
      let acceptedControl = savedAC
      let acceptedRevisions = submittedRevisions
      if (settingsChanged) {
        // PATCH only edits since this tab's baseline; replaying the full stale form would overwrite
        // disjoint settings saved by another tab after this one loaded. A secret-only draft skips
        // this request entirely so it cannot manufacture a fake settings revision.
        const result = validateSettingsSaveAck(await boundedSettingsWrite(
          signal => saveSettings(settingsPatch, {
            signal, expectedRevision: submittedRevisions.settings,
          })), schema)
        acceptedForm = toForm(result.settings, schema)
        acceptedControl = result.settings.agent_control || {}
        acceptedRevisions = {
          settings: result.settings_revision,
          secret: result.secret_revision,
        }

        // Commit the ordinary-settings ACK immediately. If the independent secret write fails, the
        // accepted baseline must not be resent, while the API-key input remains available to retry.
        setSaved(acceptedForm)
        setSavedAC(acceptedControl)
        setCredential(result.credential)
        setRevisions(acceptedRevisions)
        if (Object.hasOwn(settingsPatch, 'output_language')) setUILanguage(result.settings.output_language)
        const formBeforeSecretAck = apiKey
          ? { ...acceptedForm, llm_api_key: submittedForm.llm_api_key }
          : acceptedForm
        setForm(current => reconcileAcceptedRecord(current, submittedForm, formBeforeSecretAck))
        setAgentControl(current => reconcileAcceptedRecord(
          current, submittedControl, acceptedControl,
        ))
      }
      if (apiKey) {
        let resultSecret
        try {
          resultSecret = validateSecretSaveAck(await boundedSettingsWrite(
            signal => saveSecret('llm_api_key', apiKey, {
              signal,
              expectedSettingsRevision: acceptedRevisions.settings,
              expectedSecretRevision: acceptedRevisions.secret,
            })), 'llm_api_key')
        } catch (error) {
          if (error?.code === 'secret_revision_conflict'
              || error?.code === 'settings_revision_conflict') {
            rememberUnknown(
              'secret-conflict', submittedForm, submittedControl, [], [], settingsChanged,
            )
            return
          }
          if (unknownTransport(error)) {
            rememberUnknown(
              'secret-set', submittedForm, submittedControl, [], [], settingsChanged,
            )
            return
          }
          setCredentialWriteError(
            `${settingsChanged ? 'The endpoint/settings changes were accepted, but the' : 'The'} replacement API key was rejected. The previous server-resolved credential state remains authoritative; no new key is assumed. The typed draft is retained but is not part of that credential.`,
          )
          show((settingsChanged
            ? 'Endpoint/settings saved, but the replacement API key was rejected: '
            : 'Replacement API key was rejected: ') + (error.message || error))
          return
        }
        setCredential(resultSecret.credential)
        setRevisions({
          settings: resultSecret.settings_revision,
          secret: resultSecret.secret_revision,
        })
        if (resultSecret.set !== true) {
          setCredentialWriteError(
            `${settingsChanged ? 'The endpoint/settings changes were accepted, but the server' : 'The server'} did not confirm the replacement API key. The previous server-resolved credential state remains authoritative; no new key is assumed. The typed draft is retained but is not part of that credential.`,
          )
          show(((settingsChanged ? uiText('Endpoint/settings saved, but the replacement API key was not confirmed') : uiText('The replacement API key was not confirmed'))))
          return
        }
        setCredentialWriteError('')
        // Clear only the submitted credential. A replacement typed while either request was in
        // flight remains an unsaved edit instead of being erased by the older acknowledgement.
        setForm(current => reconcileAcceptedRecord(current, submittedForm, acceptedForm))
      // An ordinary-settings ACK carries a credential snapshot from the same locked server state,
      // so it can clear an old write fence. A no-op cannot: keep the warning until an ACK or reload.
      } else if (settingsChanged) setCredentialWriteError('')
      const savedParts = [settingsChanged ? 'Submitted settings saved' : '', apiKey ? 'API key stored securely' : ''].filter(Boolean)
      if (whitespaceOnlyApiKey) {
        show(((settingsChanged ? uiText('Submitted settings saved; whitespace-only API-key draft discarded — settings applied to new runs') : uiText('Whitespace-only API-key draft discarded; no server changes were made'))))
      } else show(uiMessage("{0} — applied to new runs", [savedParts.join(' · ') || 'No persisted changes']))
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
      else show(uiMessage("Save failed: {0}", [error.message]))
    } finally {
      finishMutation(mutation)
    }
  }
  const onClearSecret = async key => {
    if (healthRecoveryBlocked) {
      show(uiText('Acknowledge or resolve the LLM provider warning before changing its credential'))
      return
    }
    if (!credential?.clearable) {
      show(uiText('The effective credential is read-only here and cannot be cleared'))
      return
    }
    const clearingIncompletePair = credential.status === 'incomplete'
      && credential.source === 'stored'
    const clearingAmbientFallback = credential.stored
      && (credential.source === 'environment' || credential.source === 'dotenv')
    const clearPrompt = clearingIncompletePair
      ? 'Clear the incomplete stored credential pair now? This removes its orphan endpoint binding immediately. Any typed replacement stays as an unsaved draft.'
      : clearingAmbientFallback
        ? 'Clear the stored fallback API key and endpoint binding now? The ambient source remains untouched. This is immediate, separate from Save, and cannot be undone. Any typed replacement stays as an unsaved draft.'
      : 'Clear the stored API key and its endpoint binding now? This is immediate, separate from Save, and cannot be undone. Any typed replacement stays as an unsaved draft.'
    if (!window.confirm(clearPrompt)) return
    const mutation = beginMutation('clearing secret')
    if (!mutation) { show(uiText('A settings update is already in progress')); return }
    const submittedForm = form
    const submittedRevisions = { ...revisions }
    try {
      const resultSecret = validateSecretSaveAck(await boundedSettingsWrite(
        signal => saveSecret(key, '', {
          signal,
          expectedSettingsRevision: submittedRevisions.settings,
          expectedSecretRevision: submittedRevisions.secret,
        })), key)
      setCredential(resultSecret.credential)
      setCredentialWriteError('')
      setRevisions({
        settings: resultSecret.settings_revision,
        secret: resultSecret.secret_revision,
      })
      if (resultSecret.set === false && resultSecret.credential.stored === false) {
        show((submittedForm?.[key] ? uiMessage("{0} cleared; the typed replacement remains an unsaved draft", [clearingIncompletePair ? 'Incomplete stored credential pair' : clearingAmbientFallback ? 'Stored fallback API key and endpoint binding' : 'Stored API key and endpoint binding']) : uiMessage("{0} cleared", [clearingIncompletePair ? 'Incomplete stored credential pair' : clearingAmbientFallback ? 'Stored fallback API key and endpoint binding' : 'Stored API key and endpoint binding'])))
      } else show(uiText('The server did not clear the stored credential material'))
    } catch (error) {
      if (error?.code === 'secret_revision_conflict'
          || error?.code === 'settings_revision_conflict') {
        rememberUnknown('secret-clear-conflict', submittedForm)
      }
      else if (unknownTransport(error)) rememberUnknown('secret-clear', submittedForm)
      else show(uiMessage("Clear failed: {0}", [error.message]))
    } finally {
      finishMutation(mutation)
    }
  }
  const requestResetToDefaults = () => {
    if (mutationRef.current || !defaults || !schema || !form || !canResetDefaults) return
    setResetConfirmOpen(true)
  }
  const resetToDefaults = () => {
    if (mutationRef.current || !defaults || !schema || !form || !canResetDefaults) {
      setResetConfirmOpen(false)
      return
    }
    setForm(toForm(defaults, schema))
    setAgentControl({ ...(defaults.agent_control || {}) })
    setResetConfirmOpen(false)
    show(((hasSecretDraft ? uiText('Engine defaults loaded; credential drafts were discarded as confirmed. Review and Save to apply.') : uiText('Engine defaults loaded into the draft. Review and Save to apply.'))))
    requestAnimationFrame(() => {
      const target = saveButtonRef.current?.disabled ? saveStateRef.current : saveButtonRef.current
      target?.focus({ preventScroll: true })
    })
  }
  const revealChanges = () => {
    const first = hiddenUnsavedKeys[0]
    setQuery('')
    setMode('all')
    if (first) setInvalidFocus(previous => ({ key: first, request: previous.request + 1 }))
  }
  const clearSearch = () => {
    setQuery('')
    requestAnimationFrame(() => searchInputRef.current?.focus())
  }
  const reloadSavedSettings = async (options = {}) => {
    // CredentialState passes its click event through this callback; read only the explicit option
    // fields so that ordinary provider refreshes retain their existing defaults.
    const reloadSchema = options?.reloadSchema === true
    const successMessage = typeof options?.successMessage === 'string'
      ? options.successMessage : 'Server state refreshed; saved credential status is up to date'
    if (unsaved && !window.confirm(uiText('Reload saved settings and discard the current draft changes?'))) {
      return false
    }
    const mutation = beginMutation('reloading settings')
    if (!mutation) return false
    try {
      const result = await load(reloadSchema, true, true)
      if (!result.loaded) {
        show(uiText('Saved settings could not be reloaded; current values and warnings were kept'))
        focusSettingsRecovery()
      } else if (!result.reconciled) {
        show(uiText('Settings could not be reconciled yet; wait for earlier activity or browser storage to recover, then refresh again'))
        focusSettingsRecovery()
      } else {
        show(successMessage)
        focusProviderHeading()
      }
      return result.loaded && result.reconciled
    } finally { finishMutation(mutation) }
  }
  const retryInitialSettings = async () => {
    const result = await load(true, false, true)
    if (result.loaded && result.reconciled) focusProviderHeading()
    else focusSettingsRecovery()
  }
  const requestBack = () => {
    if (navigationUnsafe && !window.confirm(navigationWarning(
      mutationBusy, !!mutationUnknown, healthRecoveryBlocked))) return
    allowNavigationRef.current = true
    onBack()
  }

  return <div className="app">
    <div className="topbar">
      <GlobalMenu current="settings" />
      <button className="btn sm ghost" onClick={requestBack}>{uiText("← runs")}</button>
      <span className="ttl" style={{ fontWeight: 700, fontSize: 15 }}>{uiText("Settings")}</span>
      <span className="muted">{uiText("model, resources, and limits")}</span>
      <span className="spacer" style={{ flex: 1 }} />
    </div>

    <main className="settings-page" data-route-main tabIndex={-1}>
      {!form || !schema ? (launchGuardExternalPending
        ? <div className="notice resource-error" role="status"><b ref={recoveryHeadingRef} tabIndex={-1}>{uiText("An earlier Settings action is still finishing.")}</b><span>{uiText(settingsLaunchSnapshot.reason)}</span></div>
        : loadError
        ? <div className="notice resource-error" role="alert"><b ref={loadErrorHeadingRef} tabIndex={-1}>{uiText("Could not load settings.")}</b><span>{loadError}</span><button className="btn sm primary" onClick={retryInitialSettings}>{uiText("Retry")}</button></div>
        : <div className="notice" role="status">{uiText("Loading settings…")}</div>) : <>
        <section className="settings-overview" aria-labelledby="settings-heading">
          <div className="settings-heading-row">
            <div>
              <h1 id="settings-heading">{uiText("Defaults for new runs")}</h1>
              <p>{uiText("Connect a model, choose limits, then return to Assistant. Review overrides on each launch card.")}</p>
            </div>
            <details className="settings-help">
              <summary>{uiText("How changes work")}</summary>
              <p><span className="sf-dot unsaved">●</span>{uiText(" Amber dots mark edits not yet saved; they disappear after a successful Save.")}<span className="sf-dot fromdefault">●</span>{uiText(" Customized values differ from the engine default.")}</p>
              <p>{uiText("R, S, and B control whether the Researcher, Strategist, or Boss may change a setting at runtime.")}</p>
            </details>
          </div>

          <div className="settings-toolbar">
            <div className="settings-mode-block">
              <span id="settings-mode-label" className="settings-control-label">{uiText("Visible settings")}</span>
              <div className="settings-mode" role="group" aria-labelledby="settings-mode-label">
                <button type="button" className={mode === 'essential' ? 'active' : ''}
                        aria-pressed={mode === 'essential'} disabled={searching}
                        onClick={() => setMode('essential')}>{uiText("Essential")}</button>
                <button type="button" className={mode === 'all' ? 'active' : ''}
                        aria-pressed={mode === 'all'} disabled={searching}
                        onClick={() => setMode('all')}>{uiText("All")}</button>
              </div>
            </div>
            <div className="settings-search-block">
              <label className="settings-control-label" htmlFor="settings-search">{uiText("Find a setting")}</label>
              <div className="settings-search-control">
                <OpIcon name="search" className="t-ic" />
                <input ref={searchInputRef} id="settings-search" type="search" value={query}
                       aria-describedby={searching ? 'settings-search-scope' : undefined}
                       placeholder={uiText("Name, key, option, or purpose…")}
                       onChange={event => setQuery(event.target.value)} />
                {query && <button type="button" className="settings-search-clear"
                                  aria-label={uiText("Clear settings search")} onClick={clearSearch}>×</button>}
              </div>
              {searching && <span id="settings-search-scope" className="settings-search-scope">{uiText("Search includes advanced settings.")}</span>}
            </div>
          </div>

          <div className="settings-summary" role="status" aria-live="polite">
            <span>{uiText(catalogueSummary)}</span>
            <span className="settings-summary-divider" aria-hidden="true">·</span>
            <span className={unsaved ? 'is-unsaved' : ''}>{((unsaved ? uiPlural(unsavedKeys.size, '{0} unsaved change', '{0} unsaved changes') : uiText('No unsaved changes')))}</span>
            <span className="settings-summary-divider" aria-hidden="true">·</span>
            <span>{uiPlural(dirty.size, '{0} customized value', '{0} customized values')}</span>
            {hiddenUnsaved > 0 && <button type="button" className="settings-summary-link" onClick={revealChanges}>{uiPlural(hiddenUnsaved, 'Review {0} hidden change', 'Review {0} hidden changes')}
            </button>}
          </div>
        </section>

        <section className="settings-provider-check" aria-labelledby="settings-provider-check-heading">
          <div className="settings-provider-check-copy">
            <strong ref={providerHeadingRef} id="settings-provider-check-heading" tabIndex={-1}>{uiText("Saved LLM connection")}</strong>
            <span>{uiText("For Assistant: set Model and Base URL, Save, then Test active LLM. The test may bill; local endpoints may need no key.")}</span>
          </div>
          <CredentialState credential={credential} writeError={credentialWriteError}
            onRefresh={mutationUnknown ? reconcileUnknown : reloadSavedSettings}
            refreshing={mutationBusy === 'reloading settings' || mutationBusy === 'reconciling'}
            refreshDisabled={(!!mutationBusy && mutationBusy !== 'reloading settings'
              && mutationBusy !== 'reconciling') || launchGuardExternalPending} />
          <LlmHealth savedSettingsRevision={revisions.settings} savedSecretRevision={revisions.secret}
            unsavedCount={unsavedKeys.size}
            actionBlocked={!!mutationBusy || !!mutationUnknown || settingsActionRecoveryBlocked}
            actionKind={mutationBusy}
            providerBlockedReason={credentialBlockedReason}
            beginAction={beginMutation} finishAction={finishMutation}
            onRecoveryChange={setHealthRecoveryActive}
            reloadSavedSettings={reloadSavedSettings} />
        </section>

        {launchGuardExternalRecovery && <div className="notice resource-error settings-stale-warning" role="alert">
          <b ref={recoveryHeadingRef} tabIndex={-1}>{((launchGuardExternalPending ? uiText('An earlier Settings action is still finishing.') : uiText('Refresh Settings before starting a run.')))}</b>
          <span>{(((launchGuardExternalPending ? uiText(settingsLaunchSnapshot.reason) : uiText('The visible values may predate an earlier Settings action. Refresh once more to load a causally newer server snapshot.'))))}</span>
          {!launchGuardExternalPending && <button className="btn sm primary" disabled={!!mutationBusy}
            onClick={() => reloadSavedSettings({
              reloadSchema: true,
              successMessage: 'Settings reconciled with the current server state',
            })}>{uiText("Refresh server state")}</button>}
        </div>}

        {loadError && !launchGuardExternalRecovery
          && <div className="notice resource-error settings-stale-warning" role="alert">
          <b ref={loadErrorHeadingRef} tabIndex={-1}>{uiText("Could not refresh settings.")}</b>
          <span>{uiText("The last loaded values remain visible, but new runs stay blocked until the current server state is loaded.")}</span>
          <button className="btn sm primary"
            disabled={!!mutationBusy || !!mutationUnknown || launchGuardExternalPending}
            onClick={() => reloadSavedSettings({
              reloadSchema: true,
              successMessage: 'Settings and editor schema refreshed from the server',
            })}>{((mutationBusy === 'reloading settings' ? uiText('Retrying...') : uiText('Retry')))}</button>
        </div>}

        {mutationUnknown && <div className="notice resource-error" role="alert">
          <b ref={mutationUnknownHeadingRef} tabIndex={-1}>{((mutationUnknown.stage.endsWith('-conflict') ? uiText('Server state changed in another client.') : uiText('Update outcome unknown.')))}</b>
          <span>{((mutationUnknown.stage === 'settings-conflict' ? uiText('Your draft is retained. Refresh the current server state before deliberately saving it against the new revision.') : (mutationUnknown.stage === 'secret-conflict' ? (mutationUnknown.ordinarySettingsAccepted ? uiText('The endpoint/settings changes were accepted, but another credential update won. The typed replacement is retained. The previous server-resolved credential state remains authoritative, and Test active LLM stays blocked until refresh.') : uiText('Another credential update won before this replacement. The typed replacement is retained. The previous server-resolved credential state remains authoritative, and Test active LLM stays blocked until refresh.')) : (mutationUnknown.stage === 'secret-clear-conflict' ? uiText('Another credential update won before this clear. Refresh before deciding whether to clear the current credential.') : (mutationUnknown.stage === 'secret-set' ? (mutationUnknown.ordinarySettingsAccepted ? uiText('The endpoint/settings changes were accepted, but the API-key replacement could not be confirmed. The typed draft is retained; the previous server-resolved credential state remains authoritative, and Test active LLM stays blocked until refresh.') : uiText('The API-key replacement could not be confirmed. The typed draft is retained; the previous server-resolved credential state remains authoritative, and Test active LLM stays blocked until refresh.')) : (mutationUnknown.stage === 'secret-clear' ? uiText('The API-key clear may or may not have reached the server. Do not repeat it blindly.') : uiText('The settings save may or may not have reached the server. Current edits are kept and will not be replayed automatically.')))))))}</span>
          <button className="btn sm primary" disabled={!!mutationBusy} onClick={reconcileUnknown}>
            {((mutationBusy === 'reconciling' ? uiText('Refreshing…') : uiText('Refresh server state')))}
          </button>
        </div>}

        <SettingsForm form={form} onChange={onChange} dirty={dirty} unsaved={unsavedKeys}
                      errors={validationErrors}
                      agentControl={agentControl} onToggleAgent={onToggleAgent}
                      credential={credential} onClearSecret={onClearSecret}
                      secretActionDisabled={!!mutationBusy || !!mutationUnknown || healthRecoveryBlocked
                        || settingsActionRecoveryBlocked}
                      interactionDisabled={mutationBusy === 'reloading settings' || settingsActionRecoveryBlocked}
                      mode={mode} query={query} schema={schema} initialGroup={initialSection}
                      focusKey={invalidFocus.key} focusRequest={invalidFocus.request} />
      </>}
    </main>

    {form && schema && <div className="settings-actions"><div className="sa-inner">
      <span className="spacer" style={{ flex: 1 }} />
      {invalidCount
        ? <button type="button" className="settings-summary-link settings-save-state is-invalid"
            ref={saveStateRef} onClick={focusFirstInvalid}>{uiPlural(invalidCount, '{0} invalid setting — review', '{0} invalid settings — review')}</button>
        : <span className={'settings-save-state' + (unsaved ? ' is-unsaved' : '')}
            ref={saveStateRef} role="status" aria-live="polite" tabIndex={-1}>
          {((unsaved ? uiPlural(unsavedKeys.size, '{0} unsaved change', '{0} unsaved changes') : uiText('All changes saved')))}
        </span>}
      <button className="btn sm ghost" disabled={!!mutationBusy || !canResetDefaults
        || settingsActionRecoveryBlocked}
              onClick={requestResetToDefaults}
              title={uiText("Reset ordinary settings, runtime access controls, and typed credential drafts")}>{uiText("↻ Reset all")}</button>
      <button ref={saveButtonRef} className="btn sm primary" disabled={!unsaved || invalidCount > 0
        || !!mutationBusy || !!mutationUnknown || healthRecoveryBlocked
        || settingsActionRecoveryBlocked} onClick={onSave}>
        {((mutationBusy === 'saving' ? uiText('Saving...') : uiText('Save')))}
      </button>
    </div></div>}
    {resetConfirmOpen && <ResetDefaultsDialog hasSecretDraft={hasSecretDraft}
      onCancel={() => setResetConfirmOpen(false)} onConfirm={resetToDefaults} />}
    {toast && <div className="toast" role="status">{uiText(toast)}</div>}
  </div>
}
