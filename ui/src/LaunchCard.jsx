import React, { useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import {
  clearDamagedLaunchTransport, clearLaunchTransport, createIdempotencyKey, getStartStatus,
  listLaunchTransports, loadLaunchTransport, preflightRunStart, saveLaunchTransport, startRun,
} from './util.js'
import {
  LAUNCH_RUNTIME_FIELDS, buildLaunchBody, createLaunchDraft,
  launchFingerprint, parseObjectJson, runtimeValue, summarizeLaunchTask, updateRuntimeValue,
} from './launchDraft.js'
import { launchStatusOutcome, pollLaunchStatus } from './launchRecovery.js'
import {
  getSnapshot as getSettingsLaunchGuard, subscribe as subscribeSettingsLaunchGuard,
} from './settingsLaunchGuard.js'

const structuredDetail = error => error?.detail && typeof error.detail === 'object' ? error.detail : null
const messageOf = error => String(error?.message || 'Could not validate this run')
const actionableMessageOf = error => {
  const message = messageOf(error)
  const remediation = String(error?.remediation || structuredDetail(error)?.remediation || '').trim()
  return remediation && !message.toLowerCase().includes(remediation.toLowerCase())
    ? `${message} — ${remediation}` : message
}
const errorCode = error => String(error?.code || structuredDetail(error)?.code || '')
const externalStartConflict = error => [
  'external_start_in_progress', 'external_start_uncertain',
].includes(errorCode(error))
const runExists = error => error?.status === 409 && ([
  'run_id_conflict', 'run_exists', 'external_start_in_progress', 'external_start_uncertain',
].includes(errorCode(error))
  || /already exists|pick another (?:id|name)/i.test(messageOf(error)))
const launchAmbiguous = error => error?.submissionMayHaveSucceeded === true
  || !error?.status || error.status >= 500
  || [408, 425, 429].includes(Number(error.status))
  || (error.status === 409 && ['start_in_progress', 'start_uncertain', 'spawn_claim_unknown', 'engine_start_uncertain']
    .includes(errorCode(error)))
  || (error.status === 409 && /start(?:up)? .*in progress|engine starting/i.test(messageOf(error)))

const launchProtocolError = message => Object.assign(new Error(message), {
  code: 'LAUNCH_PREFLIGHT_PROTOCOL',
})

const fieldErrors = error => {
  const detail = structuredDetail(error)
  const raw = detail?.field_errors || detail?.errors
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const fallback = actionableMessageOf(error)
  const entries = Object.entries(raw).filter(([key]) => String(key || '').trim())
    .map(([key, value]) => {
      const message = typeof value === 'string' ? value.trim() : ''
      return [String(key), message || fallback]
    })
  const details = entries.map(([key, message]) => `${key}: ${message}`).join('; ')
  if (errorCode(error) === 'invalid_base_settings') {
    return { form: details ? `${fallback} — ${details}` : fallback }
  }
  if (errorCode(error) === 'invalid_launch_settings'
      && /task-file settings/i.test(messageOf(error))) {
    return { task_file: details ? `${fallback} — ${details}` : fallback }
  }
  return entries.length ? Object.fromEntries(entries) : null
}

const RUNTIME_FIELD_KEYS = new Set(LAUNCH_RUNTIME_FIELDS.map(field => field.key))
const RUNTIME_FIELD_LABELS = new Map(LAUNCH_RUNTIME_FIELDS.map(field => [field.key, field.label]))
const errorTarget = path => {
  if (path === 'run_id' || path === 'source' || path === 'task_file') return path
  if (path === 'task' || path.startsWith('task.')) return 'task'
  if (path === 'settings') return 'advanced-settings'
  if (path.startsWith('settings.')) {
    const key = path.slice('settings.'.length)
    return RUNTIME_FIELD_KEYS.has(key) ? `settings-${key}` : 'advanced-settings'
  }
  return null
}

const errorLabel = path => {
  if (path === 'run_id') return 'Run name'
  if (path === 'source') return 'Task source'
  if (path === 'task_file') return 'Task file'
  if (path === 'task' || path.startsWith('task.')) return 'Task'
  if (path === 'settings') return 'Advanced settings'
  if (path.startsWith('settings.')) {
    const key = path.slice('settings.'.length)
    return RUNTIME_FIELD_LABELS.get(key) || 'Advanced settings'
  }
  if (path === 'validation_token') return 'Validation'
  if (path === 'idempotency_key') return 'Startup identity'
  if (path === 'chat') return 'Chat context'
  if (path === 'recovery') return 'Startup recovery'
  return path
}

const isObject = value => !!value && typeof value === 'object' && !Array.isArray(value)
const REVIEWED_SETTING_KEYS = [
  'backend', 'llm_model', 'max_nodes', 'n_seeds', 'max_parallel', 'parallel_build',
  'eval_parallel', 'llm_parallel', 'max_seconds', 'max_eval_seconds',
]
const integerIn = (value, min, max) => Number.isInteger(value) && value >= min && value <= max
const nullableNumber = value => value == null
  || (typeof value === 'number' && Number.isFinite(value) && value >= 0)
const validReviewedSettings = settings => isObject(settings)
  && REVIEWED_SETTING_KEYS.every(key => Object.hasOwn(settings, key))
  && (settings.backend === 'toy' || settings.backend === 'llm')
  && typeof settings.llm_model === 'string'
  && integerIn(settings.max_nodes, 1, 1_000_000)
  && integerIn(settings.n_seeds, 1, 1024)
  && integerIn(settings.max_parallel, 0, 1024)
  && integerIn(settings.parallel_build, 0, 64)
  && (settings.eval_parallel == null || integerIn(settings.eval_parallel, 0, 1024))
  && (settings.llm_parallel == null || integerIn(settings.llm_parallel, 0, 64))
  && nullableNumber(settings.max_seconds)
  && nullableNumber(settings.max_eval_seconds)
const normalizedWarnings = value => Array.isArray(value) ? value.map(warning => {
  if (typeof warning === 'string') return warning.trim()
  return isObject(warning) && typeof warning.message === 'string' ? warning.message.trim() : ''
}).filter(Boolean) : []
const validLaunchPreview = (preview, runId, expectedSource) => isObject(preview)
  && typeof preview.run_id === 'string' && preview.run_id === runId
  && preview.source === expectedSource
  && Object.hasOwn(preview, 'source_task_file')
  && (preview.source === 'task_file'
    ? typeof preview.source_task_file === 'string' && !!preview.source_task_file.trim()
    : preview.source_task_file == null)
  && isObject(preview.task)
  && validReviewedSettings(preview.settings)
  && Array.isArray(preview.referenced_paths)

function EnumOptions({ field, value }) {
  const options = field.options || []
  const extra = value && !options.includes(String(value)) ? [String(value)] : []
  return <>
    <option value="">Use inherited value</option>
    {[...extra, ...options].map(option => <option key={option} value={option}>{option}</option>)}
  </>
}

const BOOL_CHOICE = Object.freeze({
  inherit: 'inherit',
  enabled: 'enabled',
  disabled: 'disabled',
  invalid: 'invalid',
})

const booleanChoice = (parsedSettings, key) => {
  if (!parsedSettings.ok) return BOOL_CHOICE.invalid
  const settings = parsedSettings.value
  if (!Object.hasOwn(settings, key) || settings[key] == null) return BOOL_CHOICE.inherit
  if (settings[key] === true) return BOOL_CHOICE.enabled
  if (settings[key] === false) return BOOL_CHOICE.disabled
  return BOOL_CHOICE.invalid
}

export default function LaunchCard({
  spec, chat = [], onStarted, retainedDraft = null, onDraftChange, launchIdentity = '',
  retainedConfigOpen = false, onConfigOpenChange,
}) {
  const original = useMemo(() => createLaunchDraft(spec), [spec])
  const transportIdentity = useMemo(() => String(
    launchIdentity || spec?.proposal_id || `legacy:${spec?.run_id || 'proposal'}`),
  [launchIdentity, spec?.proposal_id, spec?.run_id])
  const [localDraft, setLocalDraft] = useState(() => retainedDraft || original)
  const draft = retainedDraft || localDraft
  const [validation, setValidation] = useState(null)
  const [validating, setValidating] = useState(false)
  const [starting, setStarting] = useState(false)
  const [checking, setChecking] = useState(false)
  const [unknownStart, setUnknownStart] = useState(null)
  const [damagedRecovery, setDamagedRecovery] = useState(null)
  const [missingStart, setMissingStart] = useState(false)
  const [hydratedTransportIdentity, setHydratedTransportIdentity] = useState('')
  const [startedRunId, setStartedRunId] = useState('')
  const [reservedRunId, setReservedRunId] = useState('')
  const [errors, setErrors] = useState({})
  const [notice, setNotice] = useState('Review the proposal, then validate it before starting.')
  const [warnings, setWarnings] = useState([])
  const [preview, setPreview] = useState(null)
  const [storageBlocked, setStorageBlocked] = useState(false)
  // Keep the proposal SIMPLE by default: the editable run settings are collapsed so a user can just Start.
  // "Configure settings" reveals them; they also auto-reveal whenever there is an error to fix.
  const [configOpen, setConfigOpen] = useState(() => retainedConfigOpen === true)
  const settingsLaunchGuard = useSyncExternalStore(
    subscribeSettingsLaunchGuard, getSettingsLaunchGuard, getSettingsLaunchGuard)
  const settingsRouteActive = typeof location !== 'undefined' && location.hash.startsWith('#/settings')
  const settingsLaunchBlocked = settingsLaunchGuard.active
    ? settingsLaunchGuard.blocked : settingsRouteActive
  const settingsLaunchReason = settingsLaunchGuard.active
    ? settingsLaunchGuard.reason : 'Settings are still loading. Wait for saved defaults before starting a run.'
  const reactId = useId().replace(/:/g, '')
  const titleId = `launch-${reactId}-title`
  const errorId = `launch-${reactId}-errors`
  const costId = `launch-${reactId}-cost`
  const configId = `launch-${reactId}-config`
  const runIdRef = useRef(null)
  const errorRef = useRef(null)
  const advancedRef = useRef(null)
  const pendingErrorTargetRef = useRef('')
  const validateActionRef = useRef(null)
  const recoveryActionRef = useRef(null)
  const startedActionRef = useRef(null)
  const validationRequestRef = useRef(0)
  const startRequestRef = useRef(null)
  const statusRequestRef = useRef(0)
  const statusFlightRef = useRef(null)
  const settingsBlockedRef = useRef(settingsLaunchBlocked)
  const transportIdentityRef = useRef(transportIdentity)
  const navigationEpochRef = useRef(0)
  transportIdentityRef.current = transportIdentity
  useEffect(() => {
    const noteNavigation = () => { navigationEpochRef.current += 1 }
    window.addEventListener('hashchange', noteNavigation)
    window.addEventListener('popstate', noteNavigation)
    return () => {
      window.removeEventListener('hashchange', noteNavigation)
      window.removeEventListener('popstate', noteNavigation)
      transportIdentityRef.current = ''
      validationRequestRef.current += 1
      statusRequestRef.current += 1
      statusFlightRef.current = null
    }
  }, [])

  useEffect(() => {
    const wasBlocked = settingsBlockedRef.current
    settingsBlockedRef.current = settingsLaunchBlocked
    if (settingsLaunchBlocked) {
      validationRequestRef.current += 1
      setValidating(false); setValidation(null); setWarnings([]); setPreview(null)
      if (!startRequestRef.current && !unknownStart && !startedRunId && !damagedRecovery) {
        setNotice(settingsLaunchReason)
      }
      return
    }
    if (wasBlocked && !startRequestRef.current && !unknownStart && !startedRunId && !damagedRecovery) {
      setNotice('Settings are saved. Validate this proposal again before starting.')
    }
  }, [damagedRecovery, settingsLaunchBlocked, settingsLaunchReason, startedRunId, unknownStart])

  useEffect(() => {
    validationRequestRef.current += 1
    statusRequestRef.current += 1
    statusFlightRef.current = null
    // A keyed render normally remounts the card, but keep an in-place identity transition safe too:
    // detach the old request while its durable recovery record remains available globally.
    startRequestRef.current = null
    setValidating(false); setValidation(null); setStarting(false); setChecking(false)
    const saved = loadLaunchTransport(transportIdentity)
    setStorageBlocked(false); setUnknownStart(null); setDamagedRecovery(null)
    setMissingStart(false); setStartedRunId(''); setReservedRunId(''); setErrors({})
    if (saved?.invalid) {
      const damaged = listLaunchTransports().find(record => record.invalid
        && record.identity === transportIdentity) || { identity: transportIdentity, storageKey: '', invalid: true }
      setDamagedRecovery(damaged)
      setStorageBlocked(true)
      setErrors({ form: damaged.storageKey
        ? 'A damaged startup recovery record may represent an unresolved paid Start. Inspect the run and provider activity before releasing it.'
        : 'Startup recovery storage cannot be inspected safely. Restore session storage access and reload this proposal.' })
      setNotice('Paid Start is blocked. Reset cannot remove this recovery record.')
    } else if (saved) {
      setUnknownStart({ runId: saved.runId, idempotencyKey: saved.idempotencyKey })
      setNotice(`Recovered unfinished startup “${saved.runId}” from this tab. Check it; no new launch will be sent.`)
    } else {
      setNotice('Review the proposal, then validate it before starting.')
    }
    setHydratedTransportIdentity(transportIdentity)
  }, [transportIdentity])

  const fingerprint = launchFingerprint(draft, chat)
  const fingerprintRef = useRef(fingerprint)
  fingerprintRef.current = fingerprint
  const validatedCurrent = !!validation?.token && validation.fingerprint === fingerprint
  const settingsParsed = parseObjectJson(draft.settings_json, 'Settings')
  // The first paint (and any in-place identity change) is non-interactive until tab recovery has
  // been read. Otherwise Reset could erase an unresolved paid-Start fence before this effect runs.
  const transportPending = hydratedTransportIdentity !== transportIdentity
  const operationBusy = transportPending || validating || starting || checking
  const locked = operationBusy || !!unknownStart || !!damagedRecovery || !!startedRunId
  const taskRows = summarizeLaunchTask(draft)
  const launchErrorTarget = path => {
    const target = errorTarget(path)
    return target === 'task' && draft.source === 'task_file' ? 'task_file' : target
  }

  const focusFirstError = next => {
    const fieldPath = Object.keys(next || {}).find(path => launchErrorTarget(path))
    const target = fieldPath ? launchErrorTarget(fieldPath) : null
    pendingErrorTargetRef.current = target || 'summary'
    if (target) {
      setConfigOpen(true)
      onConfigOpenChange?.(true)
    }
  }

  // The stable Assistant owner retains only the editable draft in memory.  Validation and its token
  // intentionally stay local, so a card remount preserves exact JSON edits but requires free revalidation.
  const setDraft = next => {
    const value = typeof next === 'function' ? next(draft) : next
    setLocalDraft(value)
    onDraftChange?.(value)
  }

  const clearRecovery = (expectedState = null) => {
    if (clearLaunchTransport(transportIdentity, undefined, expectedState)) {
      statusRequestRef.current += 1
      setStorageBlocked(false)
      return true
    }
    setStorageBlocked(true)
    const nextErrors = { form: 'Durable startup recovery could not be cleared. No new paid Start will be sent.' }
    setErrors(nextErrors)
    setNotice('Restore session storage access, then clear this recovery identity before starting anything else.')
    focusFirstError(nextErrors)
    return false
  }

  const update = patch => {
    setDraft(current => ({ ...current, ...patch }))
    validationRequestRef.current += 1; setValidating(false)
    setValidation(null); setWarnings([]); setPreview(null); setErrors({})
    setNotice('Proposal edited — validate the current version before starting.')
  }

  const reset = () => {
    if (transportPending) return
    const saved = loadLaunchTransport(transportIdentity)
    if (saved?.invalid || damagedRecovery) {
      setStorageBlocked(true)
      const nextErrors = { form: 'Reset cannot remove a damaged startup recovery record. Inspect the run and provider activity, then use Release after inspection.' }
      setErrors(nextErrors)
      setNotice('Paid Start remains blocked; the damaged recovery identity was retained.')
      focusFirstError(nextErrors)
      return
    }
    if (saved && !clearRecovery(saved)) return
    validationRequestRef.current += 1
    setDraft(createLaunchDraft(spec)); setValidation(null); setErrors({}); setWarnings([])
    setPreview(null); setUnknownStart(null); setMissingStart(false); setStorageBlocked(false)
    setNotice('Proposal reset. Review it, then validate.')
    requestAnimationFrame(() => runIdRef.current?.focus())
  }

  const validate = async () => {
    if (settingsLaunchBlocked) {
      setValidation(null)
      setNotice(settingsLaunchReason)
      return
    }
    const built = buildLaunchBody(draft, chat)
    if (!built.ok) {
      setValidation(null); setWarnings([]); setPreview(null); setErrors(built.errors)
      setNotice('Fix the highlighted fields before validation.')
      focusFirstError(built.errors); return
    }
    if (reservedRunId && String(built.body?.run_id || '').trim() === reservedRunId) {
      setValidation(null); setWarnings([]); setPreview(null)
      const nextErrors = { run_id: 'This unresolved run name remains reserved. Choose a new run name before validating.' }
      setErrors(nextErrors)
      setNotice('Choose a new run name for any later Start; the released identity may still have provider activity.')
      focusFirstError(nextErrors)
      return
    }
    const requestId = validationRequestRef.current + 1
    validationRequestRef.current = requestId
    const requestFingerprint = fingerprint
    setValidating(true); setValidation(null); setErrors({}); setWarnings([]); setPreview(null)
    setNotice('Validating task, settings, paths, and run name…')
    try {
      const result = await preflightRunStart(built.body)
      if (result?.ok !== true || typeof result.validation_token !== 'string'
          || !result.validation_token.trim()) {
        throw launchProtocolError('The server did not return a valid validation token')
      }
      const authoritativePreview = result.preview
      const expectedSource = built.body.task_file ? 'task_file' : 'inline'
      if (!validLaunchPreview(authoritativePreview, built.body.run_id, expectedSource)) {
        throw launchProtocolError('The server did not return an authoritative launch preview')
      }
      if (validationRequestRef.current !== requestId || fingerprintRef.current !== requestFingerprint) {
        setValidation(null); setWarnings([]); setPreview(null)
        setNotice('Proposal changed while validation was in flight. Validate the current version again.')
        return
      }
      setValidation({ token: result.validation_token, fingerprint: requestFingerprint })
      setWarnings(normalizedWarnings(result.warnings))
      setPreview(authoritativePreview)
      setNotice('Validated. This exact proposal is ready to start.')
    } catch (error) {
      if (validationRequestRef.current !== requestId || fingerprintRef.current !== requestFingerprint) return
      const serverFields = fieldErrors(error)
      let failureNotice = 'Validation failed. Your edits are preserved.'
      if (serverFields) { setErrors(serverFields); focusFirstError(serverFields) }
      else if (runExists(error)) {
        const nextErrors = { run_id: 'A run with this name already exists' }
        setErrors(nextErrors); focusFirstError(nextErrors)
      } else if (errorCode(error) === 'LAUNCH_PREFLIGHT_PROTOCOL') {
        const nextErrors = { form: 'The validation response could not be verified. No Start was sent.' }
        setErrors(nextErrors); focusFirstError(nextErrors)
        failureNotice = 'Validation failed closed because the server response was incomplete or malformed.'
      } else if (errorCode(error) === 'LAUNCH_PREFLIGHT_TIMEOUT' || error?.name === 'TimeoutError') {
        const nextErrors = { form: 'Validation could not be completed. No Start was sent; it is safe to validate again.' }
        setErrors(nextErrors); focusFirstError(nextErrors)
        failureNotice = 'Validation stopped at its deadline. Your edits are preserved and no paid launch was sent.'
      } else if (error?.status == null || Number(error.status) >= 500
          || [408, 425, 429].includes(Number(error.status))) {
        const nextErrors = { form: 'Validation could not reach a trustworthy server response. No Start was sent; it is safe to validate again.' }
        setErrors(nextErrors); focusFirstError(nextErrors)
        failureNotice = 'Validation was interrupted or unavailable. Your edits are preserved and no paid launch was sent.'
      } else {
        const nextErrors = { form: actionableMessageOf(error) }
        setErrors(nextErrors); focusFirstError(nextErrors)
      }
      setValidation(null); setNotice(failureNotice)
    } finally {
      if (validationRequestRef.current === requestId) setValidating(false)
    }
  }

  const captureNavigation = () => ({
    navigationEpoch: navigationEpochRef.current,
    navigationHash: location.hash,
  })
  const navigationStillOwned = operation => operation.navigationEpoch === navigationEpochRef.current
    && operation.navigationHash === location.hash

  const presentStartupResult = (result, operation, { checked = false, pollTimedOut = false } = {}) => {
    const outcome = launchStatusOutcome(result, operation.runId)
    if (outcome.kind === 'started') {
      const cleared = clearRecovery(operation)
      const keepNavigation = navigationStillOwned(operation)
      setUnknownStart(null)
      setStartedRunId(outcome.runId)
      const receipt = cleared
        ? `Startup is proven for ${outcome.runId}. This card is locked against another Start.`
        : `Startup is proven for ${outcome.runId}. This card is locked, but tab recovery storage could not be cleared.`
      setNotice(`${receipt}${keepNavigation ? '' : ' Open the started run from this card when ready.'}`)
      onStarted?.(outcome.runId)
      if (keepNavigation) {
        location.hash = `#/run/${encodeURIComponent(outcome.runId)}`
        requestAnimationFrame(() => startedActionRef.current?.focus())
      }
      return
    }
    if (outcome.kind === 'unknown-paid') {
      setUnknownStart({ ...operation, paidEffectUnknown: true })
      setMissingStart(false); setErrors({})
      setNotice('Startup is unresolved and provider work or cost cannot be ruled out. Inspect usage and keep checking this same identity; no second launch will be sent.')
      return
    }
    if (outcome.kind === 'retryable') {
      if (!clearRecovery(operation)) { setUnknownStart(operation); return }
      setUnknownStart(null); setMissingStart(false); setValidation(null); setErrors({})
      setNotice('The exact startup is proven not to have started. Review and validate again before sending a new Start.')
      requestAnimationFrame(() => validateActionRef.current?.focus())
      return
    }
    setUnknownStart(operation); setMissingStart(false)
    if (outcome.kind === 'pending') {
      setErrors({})
      setNotice(checked && pollTimedOut
        ? 'Startup is still pending after the bounded status check. Check again later; no new launch was sent.'
        : 'The server accepted this startup identity but has not proved a running engine. Check this same startup; no second launch will be sent.')
      return
    }
    const nextErrors = { form: 'The startup response could not be verified against this exact run identity.' }
    setErrors(nextErrors)
    setNotice('Startup remains unknown. Keep this recovery identity and Check again; do not send another Start.')
    focusFirstError(nextErrors)
  }

  const start = async () => {
    if (transportPending || startRequestRef.current || unknownStart || startedRunId
        || settingsLaunchBlocked) return
    const built = buildLaunchBody(draft, chat)
    if (!built.ok || !validatedCurrent) {
      const nextErrors = built.errors || { form: 'Validate this exact proposal before starting' }
      setValidation(null); setErrors(nextErrors)
      focusFirstError(nextErrors)
      setNotice('The proposal changed after validation. Validate it again.'); return
    }
    const operation = {
      transportIdentity, runId: String(draft.run_id), idempotencyKey: createIdempotencyKey(),
      ...captureNavigation(),
    }
    startRequestRef.current = operation
    setStarting(true)
    try {
      if (!saveLaunchTransport(transportIdentity, operation)) {
        const retained = loadLaunchTransport(transportIdentity)
        if (retained && !retained.invalid) {
          setUnknownStart({ runId: retained.runId, idempotencyKey: retained.idempotencyKey })
          setStorageBlocked(false); setErrors({})
          setNotice('An unfinished startup already owns this proposal. Check that exact startup; no new launch was sent.')
        } else {
          setStorageBlocked(true)
          const nextErrors = { form: 'Durable tab storage is unavailable; paid Start was not sent.' }
          setErrors(nextErrors)
          setNotice('Enable session storage or free browser storage, then Reset and validate again.')
          focusFirstError(nextErrors)
        }
        return
      }
      setUnknownStart(operation); setMissingStart(false); setErrors({})
      setNotice('Starting this run. Do not submit another launch while this is pending…')
      const result = await startRun({ ...built.body, validation_token: validation.token,
        idempotency_key: operation.idempotencyKey })
      if (startRequestRef.current !== operation
          || transportIdentityRef.current !== operation.transportIdentity) return
      presentStartupResult(result, operation)
    } catch (error) {
      if (startRequestRef.current !== operation
          || transportIdentityRef.current !== operation.transportIdentity) return
      if (runExists(error)) {
        const cleared = clearRecovery(operation)
        if (cleared) setUnknownStart(null)
        const external = externalStartConflict(error)
        const nextErrors = { run_id: external
          ? 'This run name has an existing or unresolved startup; inspect it or choose another name.'
          : 'A run with this name already exists',
          ...(!cleared ? { form: 'Durable startup recovery could not be cleared; paid Start remains blocked.' } : {}) }
        setErrors(nextErrors)
        setValidation(null)
        if (cleared) setNotice(external
          ? 'This card did not create the existing startup. Inspect its run/provider activity, or choose another run name.'
          : 'Choose another run name; every other edit is preserved.')
        focusFirstError(nextErrors)
      } else if (launchAmbiguous(error)) {
        setUnknownStart(operation); setMissingStart(false); setErrors({})
        setNotice('The launch response was inconclusive. Check this same startup before doing anything else; no second Start will be sent.')
      } else {
        const cleared = clearRecovery(operation)
        if (cleared) setUnknownStart(null)
        const serverFields = fieldErrors(error)
        const nextErrors = { ...(serverFields || { form: actionableMessageOf(error) }),
          ...(!cleared ? { recovery: 'Durable startup recovery could not be cleared; paid Start remains blocked.' } : {}) }
        setErrors(nextErrors)
        setValidation(null)
        if (cleared) setNotice('The server rejected the launch. Your edits are preserved.')
        focusFirstError(nextErrors)
      }
    } finally {
      if (startRequestRef.current === operation) startRequestRef.current = null
      if (transportIdentityRef.current === operation.transportIdentity) {
        setStarting(false)
        requestAnimationFrame(() => (startedActionRef.current || recoveryActionRef.current)?.focus())
      }
    }
  }

  const checkStartup = async () => {
    if (transportPending || !unknownStart || statusFlightRef.current) return
    const operation = { ...unknownStart, transportIdentity, ...captureNavigation() }
    const requestId = statusRequestRef.current + 1
    statusRequestRef.current = requestId; statusFlightRef.current = requestId
    setChecking(true); setMissingStart(false); setErrors({}); setNotice('Checking the same startup request…')
    try {
      const observation = await pollLaunchStatus(
        remainingMs => getStartStatus(operation.runId, operation.idempotencyKey, {
          requestTimeoutMs: Math.min(5_000, remainingMs),
        }),
        { expectedRunId: operation.runId })
      if (statusRequestRef.current !== requestId
          || transportIdentityRef.current !== operation.transportIdentity) return
      presentStartupResult(observation.result, operation, {
        checked: true, pollTimedOut: observation.timedOut,
      })
    } catch (error) {
      if (statusRequestRef.current !== requestId
          || transportIdentityRef.current !== operation.transportIdentity) return
      if (error?.status === 404 && errorCode(error) === 'start_not_found') {
        setMissingStart(true)
        setNotice('No durable record exists yet, but the original Start may still be preflighting. Wait and Check again; do not send another launch.')
      } else {
        setErrors({ form: errorCode(error) === 'LAUNCH_STATUS_TIMEOUT' || error?.name === 'TimeoutError'
          ? 'The status check reached its deadline. The startup outcome is still unknown.'
          : 'The exact startup status could not be read. Its recovery identity is still retained.' })
        setNotice('Startup is still unknown. No new launch was sent; Check again later.')
      }
    } finally {
      if (statusFlightRef.current === requestId) {
        statusFlightRef.current = null
        if (transportIdentityRef.current === operation.transportIdentity) setChecking(false)
      }
    }
  }

  const releaseStartupRecovery = () => {
    if (transportPending || !unknownStart || (!missingStart && !unknownStart.paidEffectUnknown)) return
    const paidUnknown = unknownStart.paidEffectUnknown === true
    const prompt = paidUnknown
      ? 'Provider work or cost cannot be ruled out. Release this local recovery key only after inspecting the run and provider usage. The original run name remains reserved; use a new name for any later launch. Continue?'
      : 'The original Start may still arrive. Release this recovery key only after checking the run list and provider activity. Any later Start is a separate paid action; only the observed run name is duplicate-fenced. Continue?'
    const confirmed = typeof window === 'undefined' || window.confirm(prompt)
    if (!confirmed) return
    if (!clearRecovery(unknownStart)) return
    setUnknownStart(null); setMissingStart(false); setValidation(null)
    const releasedRunId = String(unknownStart.runId || '').trim()
    setReservedRunId(releasedRunId)
    if (releasedRunId && String(draft.run_id || '').trim() === releasedRunId) {
      setErrors({ run_id: 'This unresolved run name remains reserved. Choose a new run name before validating.' })
    } else {
      setErrors({})
    }
    if (paidUnknown) {
      setNotice('Local recovery released after explicit inspection. Choose a new run name; do not reuse the unresolved identity.')
      requestAnimationFrame(() => { runIdRef.current?.focus(); runIdRef.current?.select() })
    } else {
      setNotice('Recovery identity released after confirmation. Choose a new run name; any later Start is a separate paid action.')
      requestAnimationFrame(() => { runIdRef.current?.focus(); runIdRef.current?.select() })
    }
  }

  const releaseDamagedRecovery = () => {
    if (transportPending || !damagedRecovery) return
    if (!damagedRecovery.storageKey) {
      setStorageBlocked(true)
      const nextErrors = { form: 'The damaged recovery record cannot be addressed while session storage is unavailable. Restore access and reload.' }
      setErrors(nextErrors)
      setNotice('Nothing was released. Paid Start remains blocked.')
      focusFirstError(nextErrors)
      return
    }
    const prompt = 'This damaged record cannot be matched to a trustworthy startup outcome. Removing only the local fence does not prove that no provider work or cost occurred. Inspect the run list and provider usage first, then use a new run name for any later Start. Release this exact damaged record?'
    const confirmed = typeof window === 'undefined' || window.confirm(prompt)
    if (!confirmed) return
    if (!clearDamagedLaunchTransport(damagedRecovery.storageKey)) {
      setStorageBlocked(true)
      const nextErrors = { form: 'The damaged recovery record changed or could not be removed. No new paid Start will be sent.' }
      setErrors(nextErrors)
      setNotice('Nothing was released. Reload and inspect the current recovery state.')
      focusFirstError(nextErrors)
      return
    }
    const releasedRunId = String(draft.run_id || '').trim()
    setReservedRunId(releasedRunId); setDamagedRecovery(null); setStorageBlocked(false)
    setUnknownStart(null); setMissingStart(false); setValidation(null); setWarnings([]); setPreview(null)
    setErrors(releasedRunId
      ? { run_id: 'Use a new run name after releasing an unverified recovery record.' } : {})
    setNotice('Damaged recovery released after explicit inspection. Edit the run name and validate again before any Start.')
    requestAnimationFrame(() => { runIdRef.current?.focus(); runIdRef.current?.select() })
  }

  const changeRuntime = (field, raw) => {
    const changed = updateRuntimeValue(draft, field, raw)
    if (!changed.ok) {
      setErrors({ settings: changed.error }); setNotice('Fix the settings JSON before using the shortcuts below.')
      return
    }
    validationRequestRef.current += 1; setValidating(false)
    setDraft(changed.draft); setValidation(null); setWarnings([]); setPreview(null); setErrors({})
    setNotice('Runtime settings changed — validate again before starting.')
  }

  const errorEntries = Object.entries(errors)
  const hasErrors = errorEntries.length > 0
  const fieldErrorEntries = errorEntries.filter(([path]) => launchErrorTarget(path))
  const unmappedErrorEntries = errorEntries.filter(([path]) => !launchErrorTarget(path))
  const hasFieldErrors = fieldErrorEntries.length > 0
  const hasUnmappedErrors = unmappedErrorEntries.length > 0
  const taskInvalid = fieldErrorEntries.some(([path]) => launchErrorTarget(path) === 'task')
  const settingsInvalid = fieldErrorEntries.some(([path]) => launchErrorTarget(path) === 'advanced-settings')
  const errorsForTarget = target => fieldErrorEntries.filter(([path]) => launchErrorTarget(path) === target)
  const targetHasErrors = target => errorsForTarget(target).length > 0
  const errorItem = ([path, error]) => <li key={path}>
    {path !== 'form' && <><strong>{errorLabel(path)}</strong>{': '}</>}{error}
  </li>
  const inlineErrorId = target => `launch-${reactId}-${target}-error`
  const describedBy = (...ids) => ids.filter(Boolean).join(' ') || undefined
  const inlineError = target => {
    const entries = errorsForTarget(target)
    if (!entries.length) return null
    const showPath = target === 'advanced-settings' || target === 'task'
      || (target === 'task_file' && entries.some(([path]) => path !== 'task_file'))
    const content = ([path, error]) => showPath
      ? <><code>{path}</code>{': '}{error}</> : error
    return <div id={inlineErrorId(target)} className="asst-launch-field-error">
      {entries.length === 1 ? content(entries[0])
        : <ul>{entries.map(entry => <li key={entry[0]}>{content(entry)}</li>)}</ul>}
    </div>
  }
  const draftSettings = settingsParsed.ok ? settingsParsed.value : null
  const reviewSettings = validatedCurrent ? preview?.settings : draftSettings
  const visibleTaskRows = validatedCurrent
    ? summarizeLaunchTask({ source: 'task', task_json: JSON.stringify(preview.task) }) : taskRows
  const inheritedReview = 'inherit (validate)'
  const reviewSetting = (key, { resolvedNull = 'not set', format = String } = {}) => {
    if (!settingsParsed.ok) return 'invalid JSON'
    const hasValue = reviewSettings && Object.hasOwn(reviewSettings, key)
    const value = hasValue ? reviewSettings[key] : null
    if (validatedCurrent) return value == null ? resolvedNull : format(value)
    return !hasValue || value == null || value === '' ? inheritedReview : format(value)
  }
  const evalParallelReview = () => {
    if (!settingsParsed.ok) return 'invalid JSON'
    const value = reviewSettings?.eval_parallel
    if (!validatedCurrent && (value == null || value === '')) return inheritedReview
    const resolved = value == null ? reviewSettings?.max_parallel : value
    if (Number(resolved) === 0) return 'Auto (GPU count)'
    return value == null ? `default ${resolved ?? 'unknown'}` : String(resolved)
  }
  const llmParallelReview = () => {
    if (!settingsParsed.ok) return 'invalid JSON'
    const value = reviewSettings?.llm_parallel
    if (!validatedCurrent && (value == null || value === '')) return inheritedReview
    if (value == null) return `${reviewSettings?.parallel_build ?? 'default'} build · no shared provider cap`
    if (Number(value) === 0) return 'Auto build · no shared provider cap'
    return `${value} build/shared cap`
  }
  const timeReview = key => reviewSetting(key, {
    resolvedNull: 'no limit', format: value => `${value}s`,
  })
  const reviewSettingsInvalid = !settingsParsed.ok || settingsInvalid
  const reviewedBackend = reviewSetting('backend')
  const reviewedModel = reviewSettings?.backend === 'toy'
    ? 'Not used (toy backend)'
    : validatedCurrent && !reviewSettings?.llm_model ? 'Not configured' : reviewSetting('llm_model')
  const reviewRows = [
    { label: 'Run', value: String((validatedCurrent ? preview.run_id : draft.run_id) || 'Missing run name'),
      invalid: !!errors.run_id },
    { label: 'Task source', value: validatedCurrent
      ? preview.source === 'task_file' ? preview.source_task_file : 'Inline task JSON'
      : draft.source === 'task_file' ? String(draft.task_file || 'Missing task file') : 'Inline task JSON',
      invalid: targetHasErrors('source') || targetHasErrors('task_file') || taskInvalid },
    { label: 'Backend', value: reviewedBackend,
      invalid: reviewSettingsInvalid || !!errors['settings.backend'] },
    { label: 'Model', value: reviewedModel,
      invalid: reviewSettingsInvalid || !!errors['settings.llm_model'] },
    { label: 'Search', value: `nodes ${reviewSetting('max_nodes')} · seeds ${reviewSetting('n_seeds')}`,
      invalid: reviewSettingsInvalid || !!errors['settings.max_nodes'] || !!errors['settings.n_seeds'] },
    { label: 'Parallel', value: `eval ${evalParallelReview()} · LLM ${llmParallelReview()}`,
      invalid: reviewSettingsInvalid || !!errors['settings.eval_parallel'] || !!errors['settings.llm_parallel'] },
    { label: 'Time limits', value: `run ${timeReview('max_seconds')} · eval ${timeReview('max_eval_seconds')}`,
      invalid: reviewSettingsInvalid || !!errors['settings.max_seconds'] || !!errors['settings.max_eval_seconds'] },
  ]
  const normalLaunchActions = !startedRunId && !damagedRecovery && !unknownStart
  const costDisclosure = validatedCurrent && reviewedBackend === 'toy'
    ? 'The validated toy backend makes no model/provider call.'
    : validatedCurrent
      ? 'The validated LLM backend may incur provider cost. No monetary cap is configured.'
      : 'If validation resolves to an LLM backend, Start may incur provider cost. No monetary cap is configured.'
  // Reveal the editable config on explicit request OR whenever there is a field error to fix (so a
  // collapsed field is never the reason an error can't be seen/focused).
  const showConfig = configOpen || hasFieldErrors
  useEffect(() => {
    if (!hasFieldErrors || configOpen) return
    setConfigOpen(true)
    onConfigOpenChange?.(true)
  }, [configOpen, hasFieldErrors, onConfigOpenChange])
  useEffect(() => {
    if (showConfig && (settingsInvalid || !settingsParsed.ok) && advancedRef.current) {
      advancedRef.current.open = true
    }
  }, [settingsInvalid, settingsParsed.ok, showConfig])
  useEffect(() => {
    const pending = pendingErrorTargetRef.current
    if (!pending || operationBusy) return
    const frame = requestAnimationFrame(() => {
      const target = pending === 'summary' ? null : pending
      if (target === 'advanced-settings' && advancedRef.current) advancedRef.current.open = true
      const field = target === 'run_id' ? runIdRef.current
        : target ? document.getElementById(`launch-${reactId}-${target}`) : null
      pendingErrorTargetRef.current = ''
      const fieldDisabled = field && (field.disabled || field.matches?.(':disabled'))
      const focusTarget = field && !fieldDisabled ? field : errorRef.current
      if (!focusTarget) return
      focusTarget.focus()
      if (target === 'run_id' && focusTarget === field) focusTarget.select?.()
    })
    return () => cancelAnimationFrame(frame)
  }, [configOpen, errors, hasFieldErrors, operationBusy, reactId])
  return <form className="asst-launch" aria-labelledby={titleId} aria-busy={operationBusy ? 'true' : 'false'}
    onSubmit={event => { event.preventDefault(); validate() }}>
    <div className="asst-launch-h" id={titleId}>
      <span className="asst-perm-badge">new run</span>
      <b>Review launch proposal</b>
      <span className={'asst-launch-state' + (validatedCurrent ? ' ready' : '')}>
        {validatedCurrent ? '✓ validated' : 'not validated'}</span>
    </div>

    {draft.rationale && <p className="asst-launch-rationale">{draft.rationale}</p>}

    {hasErrors && <div ref={errorRef} id={errorId} className="asst-launch-errors" tabIndex={-1}>
      <strong>Cannot start yet</strong>
      {hasUnmappedErrors && <div role="alert" aria-label="Cannot start yet">
        <ul>{unmappedErrorEntries.map(errorItem)}</ul>
      </div>}
      {hasFieldErrors && <ul>{fieldErrorEntries.map(errorItem)}</ul>}
    </div>}

    {/* Compact, always-visible run summary + a toggle. Collapsed by default so the common path is just
        "Start"; the editable settings below open on demand (or automatically when there's an error). */}
    {!showConfig && <dl className="asst-launch-summary" aria-label="Run summary">
      {visibleTaskRows.map((row, index) => <div key={`${row.label}-${index}`} className={row.invalid ? 'invalid' : ''}>
        <dt>{row.label}</dt><dd>{row.value}</dd>
      </div>)}
    </dl>}
    {hasFieldErrors
      ? <p className="asst-launch-openreason">Proposal details are open so you can fix the highlighted fields.</p>
      : <button type="button" className="btn xs ghost asst-launch-configtoggle" aria-expanded={showConfig}
          aria-controls={configId}
          disabled={locked} onClick={() => {
            const next = !configOpen
            setConfigOpen(next)
            onConfigOpenChange?.(next)
          }}>
          {showConfig ? 'Hide proposal details' : 'Edit proposal details'}</button>}

    {showConfig && <div id={configId} className="asst-launch-config">
    <section className="asst-launch-section" aria-labelledby={`${titleId}-identity`}>
      <h4 id={`${titleId}-identity`}>Run identity</h4>
      <label htmlFor={`launch-${reactId}-run_id`}>Run name</label>
      <input ref={runIdRef} id={`launch-${reactId}-run_id`} className="text" value={draft.run_id}
        disabled={locked} aria-invalid={errors.run_id ? 'true' : undefined}
        aria-describedby={targetHasErrors('run_id') ? inlineErrorId('run_id') : undefined}
        onChange={event => update({ run_id: event.target.value })} />
      {inlineError('run_id')}
    </section>

    <section className="asst-launch-section" aria-labelledby={`${titleId}-task`}>
      <h4 id={`${titleId}-task`}>Task and evaluation contract</h4>
      <fieldset className="asst-launch-source" disabled={locked}
        aria-invalid={targetHasErrors('source') ? 'true' : undefined}
        aria-describedby={targetHasErrors('source') ? inlineErrorId('source') : undefined}>
        <legend>Task source</legend>
        <label><input id={`launch-${reactId}-source`} type="radio" name={`launch-${reactId}-source`} value="task"
          checked={draft.source === 'task'} onChange={() => update({ source: 'task' })} /> Inline task JSON</label>
        <label><input type="radio" name={`launch-${reactId}-source`} value="task_file"
          checked={draft.source === 'task_file'} onChange={() => update({ source: 'task_file' })} /> Task file</label>
      </fieldset>
      {inlineError('source')}
      {draft.source === 'task_file' ? <>
        <label htmlFor={`launch-${reactId}-task_file`}>Task file path</label>
        <input id={`launch-${reactId}-task_file`} className="text" value={draft.task_file} disabled={locked}
          aria-invalid={targetHasErrors('task_file') ? 'true' : undefined}
          aria-describedby={targetHasErrors('task_file') ? inlineErrorId('task_file') : undefined}
          onChange={event => update({ task_file: event.target.value })} />
        {inlineError('task_file')}
      </> : <>
        <label htmlFor={`launch-${reactId}-task`}>Inline task JSON</label>
        <textarea id={`launch-${reactId}-task`} className="text asst-launch-json" value={draft.task_json}
          disabled={locked} spellCheck="false" aria-invalid={taskInvalid ? 'true' : undefined}
          aria-describedby={describedBy(`${titleId}-task-help`, taskInvalid && inlineErrorId('task'))}
          onChange={event => update({ task_json: event.target.value })} />
        <span className="asst-launch-help" id={`${titleId}-task-help`}>
          Lossless task contract: goal, direction, repo/data, command, metric reader, and edit boundaries.
        </span>
        {inlineError('task')}
      </>}
      <dl className="asst-launch-summary" aria-label="Task summary">
        {visibleTaskRows.map((row, index) => <div key={`${row.label}-${index}`} className={row.invalid ? 'invalid' : ''}>
          <dt>{row.label}</dt><dd>{row.value}</dd>
        </div>)}
      </dl>
    </section>

    <section className="asst-launch-section" aria-labelledby={`${titleId}-runtime`}>
      <h4 id={`${titleId}-runtime`}>Runtime and budget</h4>
      <div className="asst-launch-runtime">
        {LAUNCH_RUNTIME_FIELDS.map(field => {
          const id = `launch-${reactId}-settings-${field.key}`
          const target = `settings-${field.key}`
          const fieldError = targetHasErrors(target)
          const value = field.type === 'bool'
            ? booleanChoice(settingsParsed, field.key)
            : runtimeValue(draft, field.key)
          const hasHelp = !!field.help || field.type === 'bool'
          const helpId = hasHelp ? `${id}-help` : undefined
          const fieldDescribedBy = describedBy(helpId, fieldError && inlineErrorId(target))
          const resolvedBoolean = field.type === 'bool' && value === BOOL_CHOICE.inherit
            && validatedCurrent && typeof preview?.settings?.[field.key] === 'boolean'
              ? preview.settings[field.key] : null
          const booleanInvalid = field.type === 'bool' && value === BOOL_CHOICE.invalid
          return <div className="asst-launch-field" key={field.key}>
            <label htmlFor={id}>{field.label}</label>
            {field.type === 'enum'
              ? <select id={id} className="text" value={value} disabled={locked || !settingsParsed.ok}
                  aria-invalid={fieldError ? 'true' : undefined} aria-describedby={fieldDescribedBy}
                  onChange={event => changeRuntime(field, event.target.value)}>
                  <EnumOptions field={field} value={value} />
                </select>
              : field.type === 'bool'
                ? <select id={id} className="text" value={value} disabled={locked || !settingsParsed.ok}
                    aria-invalid={fieldError || booleanInvalid ? 'true' : undefined}
                    aria-describedby={fieldDescribedBy}
                    onChange={event => changeRuntime(field,
                      event.target.value === BOOL_CHOICE.enabled ? true
                        : event.target.value === BOOL_CHOICE.disabled ? false : '')}>
                    {booleanInvalid &&
                      <option value={BOOL_CHOICE.invalid} disabled>Invalid value — edit Advanced JSON</option>}
                    <option value={BOOL_CHOICE.inherit}>Use inherited value</option>
                    <option value={BOOL_CHOICE.enabled}>Override: Enabled</option>
                    <option value={BOOL_CHOICE.disabled}>Override: Disabled</option>
                  </select>
              : <input id={id} className="text" value={value} disabled={locked || !settingsParsed.ok}
                  type={field.type === 'text' ? 'text' : 'number'} min={field.min}
                  step={field.type === 'int' ? 1 : field.type === 'float' ? 'any' : undefined}
                  placeholder={field.placeholder || 'inherit'} aria-invalid={fieldError ? 'true' : undefined}
                  aria-describedby={fieldDescribedBy}
                  onChange={event => changeRuntime(field, event.target.value)} />}
            {hasHelp && <span className="asst-launch-help" id={helpId}>
              {field.help}{field.help && field.type === 'bool' ? ' ' : ''}
              {field.type === 'bool' && <>Inherit resolves from task-file, saved, environment, profile,
                or default settings. Validate for free to see the effective value.
                {resolvedBoolean != null &&
                  <> <strong>Validated inherited value: {resolvedBoolean ? 'Enabled' : 'Disabled'}.</strong></>}</>}
            </span>}
            {inlineError(target)}
          </div>
        })}
      </div>
      <details ref={advancedRef} className="asst-launch-advanced">
        <summary>Advanced settings JSON</summary>
        <label htmlFor={`launch-${reactId}-advanced-settings`}>Lossless settings overrides</label>
        <textarea id={`launch-${reactId}-advanced-settings`} className="text asst-launch-json" value={draft.settings_json}
          disabled={locked} spellCheck="false" aria-invalid={settingsInvalid ? 'true' : undefined}
          aria-describedby={describedBy(`${titleId}-settings-help`,
            settingsInvalid && inlineErrorId('advanced-settings'))}
          onChange={event => update({ settings_json: event.target.value })} />
        <span className="asst-launch-help" id={`${titleId}-settings-help`}>
          The shortcuts above edit this same object; unlisted proposal settings are preserved.
        </span>
        {inlineError('advanced-settings')}
      </details>
    </section>

    {draft.setup_steps.length > 0 && <section className="asst-launch-section asst-launch-notes"
      aria-labelledby={`${titleId}-notes`}>
      <h4 id={`${titleId}-notes`}>Readiness notes</h4>
      <p>Operator checklist only — these notes are not commands and are not executed automatically.</p>
      <ol aria-label="Setup notes">{draft.setup_steps.map((step, index) => <li key={index}>{step}</li>)}</ol>
    </section>}
    </div>}
    {validatedCurrent && warnings.length > 0 && <div className="asst-launch-warnings" role="status">
      <strong>Warnings</strong><ul>{warnings.map((warning, index) => <li key={index}>
        {warning}</li>)}</ul>
    </div>}
    {validatedCurrent && preview && <details className="asst-launch-preview"><summary>Validated preview</summary>
      <pre>{typeof preview === 'string' ? preview : JSON.stringify(preview, null, 2)}</pre></details>}
    {unknownStart && <div className="asst-launch-recovery" role="status">
      <strong>Startup being observed</strong><code>{unknownStart.runId}</code>
      <span>The recovery key stays hidden and no new launch will be sent.</span>
    </div>}
    {damagedRecovery && <div className="asst-launch-recovery" role="alert">
      <strong>Damaged startup recovery</strong>
      <span>Its outcome cannot be trusted. Inspect the run list and provider activity before releasing this exact local fence.</span>
    </div>}
    {settingsLaunchBlocked && <div className="asst-launch-recovery" role="status">
      <strong>Finish Settings first</strong>
      <span>{settingsLaunchReason}</span>
    </div>}
    <section className="asst-launch-section asst-launch-review" aria-labelledby={`${titleId}-review`}>
      <h4 id={`${titleId}-review`}>Launch review</h4>
      <dl className="asst-launch-summary"
        aria-label={validatedCurrent ? 'Effective launch settings' : 'Launch settings to validate'}>
        {reviewRows.map(row => <div key={row.label} className={row.invalid ? 'invalid' : ''}>
          <dt>{row.label}</dt><dd>{row.value}</dd>
        </div>)}
      </dl>
    </section>
    <div className="asst-launch-progress" role="status" aria-live="polite" aria-atomic="true">{notice}</div>
    {normalLaunchActions && <>
      <p className="asst-launch-cost"><strong>Validate is free:</strong> it makes no model/provider call and
        resolves inherited values in the review above.</p>
      <p id={costId} className="asst-launch-cost"><strong>Provider cost:</strong> {costDisclosure}
        {' '}Review the model, workload, parallelism, and time limits above.</p>
    </>}

    <div className="asst-perm-actions asst-launch-actions">
      <button type="button" className="btn xs ghost" disabled={locked} onClick={reset}>Reset proposal</button>
      {startedRunId
        ? <button ref={startedActionRef} type="button" className="btn xs primary"
          onClick={() => { location.hash = `#/run/${encodeURIComponent(startedRunId)}` }}>
          Open started run</button>
        : damagedRecovery
        ? <button type="button" className="btn xs ghost" disabled={operationBusy || !damagedRecovery.storageKey}
          onClick={releaseDamagedRecovery}>Release after inspection</button>
        : unknownStart
        ? <>
          <button ref={recoveryActionRef} type="button" className="btn xs primary" disabled={operationBusy}
            onClick={checkStartup}>
            {checking ? 'Checking…' : starting ? 'Waiting for Start…' : 'Check startup'}</button>
          {(missingStart || unknownStart.paidEffectUnknown) && <button type="button" className="btn xs ghost"
            disabled={operationBusy} onClick={releaseStartupRecovery}>Release after inspection</button>}
        </>
        : <>
          <button ref={validateActionRef} type="button" className="btn xs"
            disabled={locked || settingsLaunchBlocked} onClick={validate}>
            {validating ? 'Validating…' : validatedCurrent ? 'Validate again — free' : 'Validate — free'}</button>
          <button type="button" className="btn xs primary"
            disabled={locked || storageBlocked || settingsLaunchBlocked || !validatedCurrent}
            aria-describedby={costId} onClick={start}>
            {starting ? 'Starting…' : 'Start run'}</button>
        </>}
    </div>
  </form>
}
