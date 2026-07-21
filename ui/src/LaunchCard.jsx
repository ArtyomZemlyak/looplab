import React, { useEffect, useId, useMemo, useRef, useState } from 'react'
import {
  clearLaunchTransport, createIdempotencyKey, getStartStatus, loadLaunchTransport,
  preflightRunStart, saveLaunchTransport, startRun,
} from './util.js'
import {
  LAUNCH_RUNTIME_FIELDS, buildLaunchBody, createLaunchDraft,
  launchFingerprint, parseObjectJson, runtimeValue, summarizeLaunchTask, updateRuntimeValue,
} from './launchDraft.js'

const messageOf = error => String(error?.message || 'Could not validate this run')
const structuredDetail = error => error?.detail && typeof error.detail === 'object' ? error.detail : null
const errorCode = error => String(error?.code || structuredDetail(error)?.code || '')
const externalStartConflict = error => [
  'external_start_in_progress', 'external_start_uncertain',
].includes(errorCode(error))
const runExists = error => error?.status === 409 && ([
  'run_id_conflict', 'run_exists', 'external_start_in_progress', 'external_start_uncertain',
].includes(errorCode(error))
  || /already exists|pick another (?:id|name)/i.test(messageOf(error)))
const launchAmbiguous = error => !error?.status || error.status >= 500
  || [408, 425, 429].includes(Number(error.status))
  || (error.status === 409 && ['start_in_progress', 'start_uncertain', 'spawn_claim_unknown', 'engine_start_uncertain']
    .includes(errorCode(error)))
  || (error.status === 409 && /start(?:up)? .*in progress|engine starting/i.test(messageOf(error)))

const fieldErrors = error => {
  const detail = structuredDetail(error)
  const raw = detail?.field_errors || detail?.errors
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  return Object.fromEntries(Object.entries(raw).map(([key, value]) => [key, String(value)]))
}

const statusStarted = result => {
  const state = String(result?.status || result?.state || '').toLowerCase()
  return result?.started === true || ['executing', 'succeeded'].includes(state)
}

const statusRetryable = result => {
  const state = String(result?.status || result?.state || '').toLowerCase()
  if (result?.can_retry === true) return true
  if (result?.can_retry === false) return false
  return ['not_started', 'failed', 'rejected'].includes(state)
}

function EnumOptions({ field, value }) {
  const options = field.options || []
  const extra = value && !options.includes(String(value)) ? [String(value)] : []
  return <>
    <option value="">Use inherited value</option>
    {[...extra, ...options].map(option => <option key={option} value={option}>{option}</option>)}
  </>
}

export default function LaunchCard({
  spec, chat = [], onStarted, retainedDraft = null, onDraftChange, launchIdentity = '',
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
  const [missingStart, setMissingStart] = useState(false)
  const [errors, setErrors] = useState({})
  const [notice, setNotice] = useState('Review the proposal, then validate it before starting.')
  const [warnings, setWarnings] = useState([])
  const [preview, setPreview] = useState(null)
  const [storageBlocked, setStorageBlocked] = useState(false)
  // Keep the proposal SIMPLE by default: the editable run settings are collapsed so a user can just Start.
  // "Configure settings" reveals them; they also auto-reveal whenever there is an error to fix.
  const [configOpen, setConfigOpen] = useState(false)
  const reactId = useId().replace(/:/g, '')
  const titleId = `launch-${reactId}-title`
  const errorId = `launch-${reactId}-errors`
  const runIdRef = useRef(null)
  const errorRef = useRef(null)
  const validationRequestRef = useRef(0)

  useEffect(() => {
    const saved = loadLaunchTransport(transportIdentity)
    setStorageBlocked(false)
    setUnknownStart(null)
    setMissingStart(false)
    if (saved?.invalid) {
      setStorageBlocked(true)
      setErrors({ form: 'Durable startup recovery storage is corrupt or unavailable. Reset this proposal before starting.' })
      setNotice('Paid Start is blocked until its recovery identity can be stored safely.')
    } else if (saved) {
      setUnknownStart({ runId: saved.runId, idempotencyKey: saved.idempotencyKey })
      setNotice(`Recovered unfinished startup “${saved.runId}” from this tab. Check it; no new launch will be sent.`)
    }
  }, [transportIdentity])

  const fingerprint = launchFingerprint(draft, chat)
  const fingerprintRef = useRef(fingerprint)
  fingerprintRef.current = fingerprint
  const validatedCurrent = !!validation?.token && validation.fingerprint === fingerprint
  const settingsParsed = parseObjectJson(draft.settings_json, 'Settings')
  const operationBusy = validating || starting || checking
  const locked = operationBusy || !!unknownStart
  const taskRows = summarizeLaunchTask(draft)

  const focusFirstError = next => requestAnimationFrame(() => {
    const first = Object.keys(next || {})[0]
    const owner = first?.startsWith('task.') ? 'task'
      : first?.startsWith('settings.') ? 'settings' : first
    const field = owner === 'run_id' ? runIdRef.current
      : document.getElementById(`launch-${reactId}-${owner?.replace(/[^a-zA-Z0-9_-]/g, '-')}`)
    ;(field || errorRef.current)?.focus()
  })

  // The stable Assistant owner retains only the editable draft in memory.  Validation and its token
  // intentionally stay local, so a card remount preserves exact JSON edits but requires free revalidation.
  const setDraft = next => {
    const value = typeof next === 'function' ? next(draft) : next
    setLocalDraft(value)
    onDraftChange?.(value)
  }

  const clearRecovery = () => {
    if (clearLaunchTransport(transportIdentity)) {
      setStorageBlocked(false)
      return true
    }
    setStorageBlocked(true)
    setErrors({ form: 'Durable startup recovery could not be cleared. No new paid Start will be sent.' })
    setNotice('Restore session storage access, then clear this recovery identity before starting anything else.')
    requestAnimationFrame(() => errorRef.current?.focus())
    return false
  }

  const update = patch => {
    setDraft(current => ({ ...current, ...patch }))
    validationRequestRef.current += 1; setValidating(false)
    setValidation(null); setWarnings([]); setPreview(null); setErrors({})
    setNotice('Proposal edited — validate the current version before starting.')
  }

  const reset = () => {
    const saved = loadLaunchTransport(transportIdentity)
    if (saved && !clearRecovery()) return
    validationRequestRef.current += 1
    setDraft(createLaunchDraft(spec)); setValidation(null); setErrors({}); setWarnings([])
    setPreview(null); setUnknownStart(null); setMissingStart(false); setStorageBlocked(false)
    setNotice('Proposal reset. Review it, then validate.')
    requestAnimationFrame(() => runIdRef.current?.focus())
  }

  const validate = async () => {
    const built = buildLaunchBody(draft, chat)
    if (!built.ok) {
      setValidation(null); setWarnings([]); setPreview(null); setErrors(built.errors)
      setNotice('Fix the highlighted fields before validation.')
      focusFirstError(built.errors); return
    }
    const requestId = validationRequestRef.current + 1
    validationRequestRef.current = requestId
    const requestFingerprint = fingerprint
    setValidating(true); setErrors({}); setWarnings([]); setPreview(null)
    setNotice('Validating task, settings, paths, and run name…')
    try {
      const result = await preflightRunStart(built.body)
      if (!result?.ok || !result?.validation_token) throw new Error('The server did not return a validation token')
      if (validationRequestRef.current !== requestId || fingerprintRef.current !== requestFingerprint) {
        setValidation(null); setWarnings([]); setPreview(null)
        setNotice('Proposal changed while validation was in flight. Validate the current version again.')
        return
      }
      setValidation({ token: result.validation_token, fingerprint: requestFingerprint })
      setWarnings(Array.isArray(result.warnings) ? result.warnings : [])
      setPreview(result.preview || null)
      setNotice('Validated. This exact proposal is ready to start.')
    } catch (error) {
      if (validationRequestRef.current !== requestId || fingerprintRef.current !== requestFingerprint) return
      const serverFields = fieldErrors(error)
      if (serverFields) { setErrors(serverFields); focusFirstError(serverFields) }
      else if (runExists(error)) {
        setErrors({ run_id: 'A run with this name already exists' })
        requestAnimationFrame(() => { runIdRef.current?.focus(); runIdRef.current?.select() })
      } else setErrors({ form: messageOf(error) })
      setValidation(null); setNotice('Validation failed. Your edits are preserved.')
    } finally {
      if (validationRequestRef.current === requestId) setValidating(false)
    }
  }

  const start = async () => {
    const built = buildLaunchBody(draft, chat)
    if (!built.ok || !validatedCurrent) {
      setValidation(null); setErrors(built.errors || { form: 'Validate this exact proposal before starting' })
      setNotice('The proposal changed after validation. Validate it again.'); return
    }
    const idempotencyKey = createIdempotencyKey()
    if (!saveLaunchTransport(transportIdentity, { runId: draft.run_id, idempotencyKey })) {
      setStorageBlocked(true)
      setErrors({ form: 'Durable tab storage is unavailable; paid Start was not sent.' })
      setNotice('Enable session storage or free browser storage, then Reset and validate again.')
      requestAnimationFrame(() => errorRef.current?.focus())
      return
    }
    setStarting(true); setErrors({}); setNotice('Starting this run. Do not submit another launch while this is pending…')
    try {
      const result = await startRun({ ...built.body, validation_token: validation.token,
        idempotency_key: idempotencyKey })
      const runId = result?.run_id || draft.run_id
      if (statusStarted(result)) {
        const cleared = clearRecovery()
        setNotice(cleared
          ? `Started ${runId}. Opening the run…`
          : `Started ${runId}, but tab recovery storage could not be cleared. Opening the proven run…`)
        onStarted?.(runId)
        location.hash = `#/run/${encodeURIComponent(runId)}`
      } else if (result?.paid_effect_unknown) {
        setUnknownStart({ runId, idempotencyKey, paidEffectUnknown: true })
        setMissingStart(false)
        setNotice('Startup is unresolved and provider work or cost cannot be ruled out. Inspect usage and keep checking this same identity; no second launch will be sent.')
      } else if (statusRetryable(result)) {
        if (!clearRecovery()) {
          setUnknownStart({ runId, idempotencyKey })
          return
        }
        setValidation(null)
        setNotice('The engine process was not started. Review and validate again before retrying.')
      } else {
        setUnknownStart({ runId, idempotencyKey })
        setMissingStart(false)
        setNotice('The server accepted the startup identity but has not proved a running engine. Check this same startup; no second launch will be sent.')
      }
    } catch (error) {
      if (runExists(error)) {
        const cleared = clearRecovery()
        const external = externalStartConflict(error)
        setErrors({ run_id: external
          ? 'This run name has an existing or unresolved startup; inspect it or choose another name.'
          : 'A run with this name already exists',
          ...(!cleared ? { form: 'Durable startup recovery could not be cleared; paid Start remains blocked.' } : {}) })
        setValidation(null)
        if (cleared) setNotice(external
          ? 'This card did not create the existing startup. Inspect its run/provider activity, or choose another run name.'
          : 'Choose another run name; every other edit is preserved.')
        requestAnimationFrame(() => { runIdRef.current?.focus(); runIdRef.current?.select() })
      } else if (launchAmbiguous(error)) {
        setUnknownStart({ runId: draft.run_id, idempotencyKey })
        setMissingStart(false)
        setNotice('The launch response was inconclusive. Check this same startup before doing anything else.')
      } else {
        const cleared = clearRecovery()
        const serverFields = fieldErrors(error)
        setErrors({ ...(serverFields || { form: messageOf(error) }),
          ...(!cleared ? { recovery: 'Durable startup recovery could not be cleared; paid Start remains blocked.' } : {}) })
        setValidation(null)
        if (cleared) setNotice('The server rejected the launch. Your edits are preserved.')
        if (serverFields) focusFirstError(serverFields)
      }
    } finally { setStarting(false) }
  }

  const checkStartup = async () => {
    if (!unknownStart) return
    setChecking(true); setMissingStart(false); setErrors({}); setNotice('Checking the same startup request…')
    try {
      const result = await getStartStatus(unknownStart.runId, unknownStart.idempotencyKey)
      if (statusStarted(result)) {
        const runId = result?.run_id || unknownStart.runId
        const cleared = clearRecovery()
        setNotice(cleared
          ? `Startup is proven for ${runId}. Opening the run…`
          : `Startup is proven for ${runId}, but tab recovery storage could not be cleared. Opening the run…`)
        onStarted?.(runId); location.hash = `#/run/${encodeURIComponent(runId)}`
      } else if (result?.paid_effect_unknown) {
        setUnknownStart(current => ({ ...current, paidEffectUnknown: true }))
        setNotice('Startup is unresolved and provider work or cost cannot be ruled out. Inspect usage and keep checking this same identity; no second launch will be sent.')
      } else if (statusRetryable(result)) {
        if (!clearRecovery()) return
        setUnknownStart(null); setValidation(null)
        setNotice('The run did not start. Review and validate again before retrying.')
      } else {
        setNotice('Startup is still pending or cannot yet be proven. Check again; no new launch was sent.')
      }
    } catch (error) {
      if (error?.status === 404 && errorCode(error) === 'start_not_found') {
        setMissingStart(true)
        setNotice('No durable record exists yet, but the original Start may still be preflighting. Wait and Check again; do not send another launch.')
      } else {
        setErrors({ form: messageOf(error) })
        setNotice('Startup is still unknown. No new launch was sent; check again later.')
      }
    } finally { setChecking(false) }
  }

  const releaseStartupRecovery = () => {
    if (!unknownStart || (!missingStart && !unknownStart.paidEffectUnknown)) return
    const paidUnknown = unknownStart.paidEffectUnknown === true
    const prompt = paidUnknown
      ? 'Provider work or cost cannot be ruled out. Release this local recovery key only after inspecting the run and provider usage. The original run name remains reserved; use a new name for any later launch. Continue?'
      : 'The original Start may still arrive. Release this recovery key only after checking the run list and provider activity. Any later Start is a separate paid action; only the observed run name is duplicate-fenced. Continue?'
    const confirmed = typeof window === 'undefined' || window.confirm(prompt)
    if (!confirmed) return
    if (!clearRecovery()) return
    setUnknownStart(null); setMissingStart(false); setValidation(null)
    if (paidUnknown) {
      setErrors({ run_id: 'This unresolved run name remains reserved. Choose a new run name before validating.' })
      setNotice('Local recovery released after explicit inspection. Choose a new run name; do not reuse the unresolved identity.')
      requestAnimationFrame(() => { runIdRef.current?.focus(); runIdRef.current?.select() })
    } else {
      setErrors({})
      setNotice('Recovery identity released after confirmation. Any later Start is a separate paid action; review its run name and validate again.')
    }
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
  const taskInvalid = errorEntries.some(([path]) => path === 'task' || path.startsWith('task.'))
  const settingsInvalid = errorEntries.some(([path]) => path === 'settings' || path.startsWith('settings.'))
  // Reveal the editable config on explicit request OR whenever there is an error to fix (so a collapsed
  // field is never the reason an error can't be seen/focused).
  const showConfig = configOpen || errorEntries.length > 0
  return <form className="asst-launch" aria-labelledby={titleId} aria-busy={operationBusy ? 'true' : 'false'}
    onSubmit={event => { event.preventDefault(); validate() }}>
    <div className="asst-launch-h" id={titleId}>
      <span className="asst-perm-badge">new run</span>
      <b>Review launch proposal</b>
      <span className={'asst-launch-state' + (validatedCurrent ? ' ready' : '')}>
        {validatedCurrent ? '✓ validated' : 'not validated'}</span>
    </div>

    {draft.rationale && <p className="asst-launch-rationale">{draft.rationale}</p>}

    {/* Compact, always-visible run summary + a toggle. Collapsed by default so the common path is just
        "Start"; the editable settings below open on demand (or automatically when there's an error). */}
    {!showConfig && <dl className="asst-launch-summary" aria-label="Run summary">
      {taskRows.map((row, index) => <div key={`${row.label}-${index}`} className={row.invalid ? 'invalid' : ''}>
        <dt>{row.label}</dt><dd>{row.value}</dd>
      </div>)}
    </dl>}
    <button type="button" className="btn xs ghost asst-launch-configtoggle" aria-expanded={showConfig}
      disabled={locked} onClick={() => setConfigOpen(open => !open)}>
      {showConfig ? '▾ Hide settings' : '⚙ Configure settings'}</button>

    {showConfig && <>
    <section className="asst-launch-section" aria-labelledby={`${titleId}-identity`}>
      <h4 id={`${titleId}-identity`}>Run identity</h4>
      <label htmlFor={`launch-${reactId}-run_id`}>Run name</label>
      <input ref={runIdRef} id={`launch-${reactId}-run_id`} className="text" value={draft.run_id}
        disabled={locked} aria-invalid={errors.run_id ? 'true' : undefined}
        aria-describedby={errors.run_id ? errorId : undefined}
        onChange={event => update({ run_id: event.target.value })} />
    </section>

    <section className="asst-launch-section" aria-labelledby={`${titleId}-task`}>
      <h4 id={`${titleId}-task`}>Task and evaluation contract</h4>
      <fieldset className="asst-launch-source" disabled={locked}>
        <legend>Task source</legend>
        <label><input type="radio" name={`launch-${reactId}-source`} value="task"
          checked={draft.source === 'task'} onChange={() => update({ source: 'task' })} /> Inline task JSON</label>
        <label><input type="radio" name={`launch-${reactId}-source`} value="task_file"
          checked={draft.source === 'task_file'} onChange={() => update({ source: 'task_file' })} /> Task file</label>
      </fieldset>
      {draft.source === 'task_file' ? <>
        <label htmlFor={`launch-${reactId}-task_file`}>Task file path</label>
        <input id={`launch-${reactId}-task_file`} className="text" value={draft.task_file} disabled={locked}
          aria-invalid={errors.task_file ? 'true' : undefined} aria-describedby={errors.task_file ? errorId : undefined}
          onChange={event => update({ task_file: event.target.value })} />
      </> : <>
        <label htmlFor={`launch-${reactId}-task`}>Inline task JSON</label>
        <textarea id={`launch-${reactId}-task`} className="text asst-launch-json" value={draft.task_json}
          disabled={locked} spellCheck="false" aria-invalid={taskInvalid ? 'true' : undefined}
          aria-describedby={taskInvalid ? errorId : `${titleId}-task-help`}
          onChange={event => update({ task_json: event.target.value })} />
        <span className="asst-launch-help" id={`${titleId}-task-help`}>
          Lossless task contract: goal, direction, repo/data, command, metric reader, and edit boundaries.
        </span>
      </>}
      <dl className="asst-launch-summary" aria-label="Task summary">
        {taskRows.map((row, index) => <div key={`${row.label}-${index}`} className={row.invalid ? 'invalid' : ''}>
          <dt>{row.label}</dt><dd>{row.value}</dd>
        </div>)}
      </dl>
    </section>

    <section className="asst-launch-section" aria-labelledby={`${titleId}-runtime`}>
      <h4 id={`${titleId}-runtime`}>Runtime and budget</h4>
      <div className="asst-launch-runtime">
        {LAUNCH_RUNTIME_FIELDS.map(field => {
          const id = `launch-${reactId}-settings-${field.key}`
          const fieldError = errors[`settings.${field.key}`]
          const value = runtimeValue(draft, field.key)
          const helpId = field.help ? `${id}-help` : undefined
          const describedBy = [helpId, fieldError ? errorId : null].filter(Boolean).join(' ') || undefined
          return <div className="asst-launch-field" key={field.key}>
            <label htmlFor={id}>{field.label}</label>
            {field.type === 'enum'
              ? <select id={id} className="text" value={value} disabled={locked || !settingsParsed.ok}
                  aria-invalid={fieldError ? 'true' : undefined} aria-describedby={describedBy}
                  onChange={event => changeRuntime(field, event.target.value)}>
                  <EnumOptions field={field} value={value} />
                </select>
              : field.type === 'bool'
                ? <input id={id} checked={value === true} disabled={locked || !settingsParsed.ok}
                    type="checkbox" aria-invalid={fieldError ? 'true' : undefined}
                    aria-describedby={describedBy}
                    onChange={event => changeRuntime(field, event.target.checked)} />
              : <input id={id} className="text" value={value} disabled={locked || !settingsParsed.ok}
                  type={field.type === 'text' ? 'text' : 'number'} min={field.min}
                  step={field.type === 'int' ? 1 : field.type === 'float' ? 'any' : undefined}
                  placeholder={field.placeholder || 'inherit'} aria-invalid={fieldError ? 'true' : undefined}
                  aria-describedby={describedBy}
                  onChange={event => changeRuntime(field, event.target.value)} />}
            {field.help && <span className="asst-launch-help" id={helpId}>{field.help}</span>}
          </div>
        })}
      </div>
      <details className="asst-launch-advanced" open={!settingsParsed.ok}>
        <summary>Advanced settings JSON</summary>
        <label htmlFor={`launch-${reactId}-settings`}>Lossless settings overrides</label>
        <textarea id={`launch-${reactId}-settings`} className="text asst-launch-json" value={draft.settings_json}
          disabled={locked} spellCheck="false" aria-invalid={settingsInvalid ? 'true' : undefined}
          aria-describedby={settingsInvalid ? errorId : `${titleId}-settings-help`}
          onChange={event => update({ settings_json: event.target.value })} />
        <span className="asst-launch-help" id={`${titleId}-settings-help`}>
          The shortcuts above edit this same object; unlisted proposal settings are preserved.
        </span>
      </details>
    </section>

    {draft.setup_steps.length > 0 && <section className="asst-launch-section asst-launch-notes"
      aria-labelledby={`${titleId}-notes`}>
      <h4 id={`${titleId}-notes`}>Readiness notes</h4>
      <p>Operator checklist only — these notes are not commands and are not executed automatically.</p>
      <ol aria-label="Setup notes">{draft.setup_steps.map((step, index) => <li key={index}>{step}</li>)}</ol>
    </section>}
    </>}

    {errorEntries.length > 0 && <div ref={errorRef} id={errorId} className="asst-launch-errors"
      role="alert" tabIndex={-1}>
      <strong>Cannot start yet</strong>
      <ul>{errorEntries.map(([path, error]) => <li key={path}>
        {path !== 'form' && <><code>{path}</code>{': '}</>}{error}</li>)}</ul>
    </div>}
    {warnings.length > 0 && <div className="asst-launch-warnings" role="status">
      <strong>Warnings</strong><ul>{warnings.map((warning, index) => <li key={index}>
        {typeof warning === 'string' ? warning : warning.message || JSON.stringify(warning)}</li>)}</ul>
    </div>}
    {preview && <details className="asst-launch-preview"><summary>Validated preview</summary>
      <pre>{typeof preview === 'string' ? preview : JSON.stringify(preview, null, 2)}</pre></details>}
    {unknownStart && <div className="asst-launch-recovery" role="status">
      <strong>Startup being observed</strong><code>{unknownStart.runId}</code>
      <span>The recovery key stays hidden and no new launch will be sent.</span>
    </div>}
    <div className="asst-launch-progress" role="status" aria-live="polite" aria-atomic="true">{notice}</div>
    <p className="asst-launch-cost"><strong>Validate is free:</strong> it makes no model/provider call.
      <strong> Start may incur cost</strong> when it launches provider-backed work.</p>

    <div className="asst-perm-actions asst-launch-actions">
      <button type="button" className="btn xs ghost" disabled={locked} onClick={reset}>Reset proposal</button>
      {unknownStart
        ? <>
          <button type="button" className="btn xs primary" disabled={checking} onClick={checkStartup}>
            {checking ? 'Checking…' : 'Check startup'}</button>
          {(missingStart || unknownStart.paidEffectUnknown) && <button type="button" className="btn xs ghost"
            disabled={checking} onClick={releaseStartupRecovery}>Release after inspection</button>}
        </>
        : <>
          <button type="button" className="btn xs" disabled={locked} onClick={validate}>
            {validating ? 'Validating…' : validatedCurrent ? 'Validate again — free' : 'Validate — free'}</button>
          <button type="button" className="btn xs primary"
            disabled={locked || storageBlocked || !validatedCurrent} onClick={start}>
            {starting ? 'Starting…' : 'Start run'}</button>
        </>}
    </div>
  </form>
}
