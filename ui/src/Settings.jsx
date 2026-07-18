import React, { useEffect, useMemo, useRef, useState } from 'react'
import { get, saveSettings, saveSecret, llmHealth } from './util.js'
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

const countLabel = (count, singular, plural = `${singular}s`) => `${count} ${count === 1 ? singular : plural}`
const SETTINGS_READ_TIMEOUT_MS = 15_000
const SETTINGS_WRITE_TIMEOUT_MS = 15_000
const LLM_HEALTH_TIMEOUT_MS = 15_000
const unknownTransport = error => !Number.isInteger(error?.status)
const publicSubmittedForm = form => ({ ...(form || {}), llm_api_key: '' })
const boundedSettingsWrite = work =>
  deadlineRequest(signal => work(signal), SETTINGS_WRITE_TIMEOUT_MS).promise
const navigationWarning = busy => busy
  ? 'A settings update is still in flight and may finish after you leave. Leave this page anyway?'
  : 'Discard unsaved settings changes and leave this page?'

// LLM endpoint self-test (the UI equivalent of `LoopLab smoke`): pings the configured model so the
// user knows it is reachable before launching a run against it.
export function LlmHealth() {
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)
  const requestRef = useRef(null)
  useEffect(() => () => {
    requestRef.current?.controller.abort()
    requestRef.current = null
  }, [])
  const check = () => {
    if (requestRef.current) return
    const request = deadlineRequest(signal => llmHealth({ signal }), LLM_HEALTH_TIMEOUT_MS)
    requestRef.current = request
    setBusy(true)
    request.promise.then(value => {
      if (requestRef.current === request) setStatus(value)
    }).catch(error => {
      if (requestRef.current !== request) return
      setStatus({ ok: false, error: error?.name === 'TimeoutError'
        ? 'Provider check timed out. No automatic retry was sent.'
        : 'Check provider configuration and network access.' })
    }).finally(() => {
      if (requestRef.current !== request) return
      requestRef.current = null
      setBusy(false)
    })
  }
  return <span className="llm-health">
    <button className="btn sm" disabled={busy} onClick={check} title="Ping the configured LLM endpoint">
      {busy ? '… pinging' : <><OpIcon name="bolt" className="t-ic" /> Test LLM</>}
    </button>
    {status && <span className={'chip ' + (status.ok ? 'ok' : 'alarm')}
                     title={status.ok ? status.text
                       : status.error || 'Check provider configuration and network access.'} role="status">
      {status.ok ? '✓' : '×'} {status.model || 'Connection failed'}
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
  const [invalidFocus, setInvalidFocus] = useState({ key: '', request: 0 })
  const mutationRef = useRef(null)
  const loadRef = useRef(0)
  const loadControllerRef = useRef(null)
  const allowNavigationRef = useRef(false)
  const settingsHashRef = useRef(typeof location === 'undefined' ? '#/settings' : location.hash)

  const load = (reloadSchema = false) => {
    const owner = ++loadRef.current
    loadControllerRef.current?.abort()
    const timed = deadlineRequest(signal => get('/api/settings', { signal }), SETTINGS_READ_TIMEOUT_MS)
    loadControllerRef.current = timed.controller
    setLoadError('')
    return Promise.all([timed.promise, loadSettingsSchema({ reload: reloadSchema })]).then(([data, nextSchema]) => {
      if (loadRef.current !== owner) return
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
    }).catch(() => {
      if (loadRef.current === owner) {
        setSchema(null)
        setLoadError('Settings or their editor schema could not be loaded.')
      }
    }).finally(() => {
      if (loadRef.current === owner && loadControllerRef.current === timed.controller) {
        loadControllerRef.current = null
      }
    })
  }
  useEffect(() => {
    load()
    return () => { loadRef.current += 1; loadControllerRef.current?.abort() }
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

  // A field is unsaved when either its value or runtime-governance roles changed since the last save.
  const unsavedKeys = useMemo(() => {
    const changed = new Set()
    if (form && saved) for (const key of Object.keys(form)) {
      if (JSON.stringify(form[key]) !== JSON.stringify(saved[key])) changed.add(key)
    }
    const controlKeys = new Set([...Object.keys(agentControl || {}), ...Object.keys(savedAC || {})])
    for (const key of controlKeys) {
      if (JSON.stringify((agentControl || {})[key] || []) !== JSON.stringify((savedAC || {})[key] || [])) changed.add(key)
    }
    return changed
  }, [form, saved, agentControl, savedAC])
  const unsaved = unsavedKeys.size > 0
  const validationErrors = useMemo(() => form && schema
    ? settingsValidationErrors(form, schema) : {}, [form, schema])
  const invalidCount = Object.keys(validationErrors).length
  const navigationUnsafe = unsaved || !!mutationBusy || !!mutationUnknown

  useEffect(() => {
    if (!navigationUnsafe) return undefined
    // Capture listeners run before App's ordinary route listeners. Restore Settings before App
    // reads location so a cancelled browser/hash navigation cannot unmount the draft.
    return installNavigationLossGuard({
      allowRef: allowNavigationRef,
      guardedHash: settingsHashRef.current,
      message: () => navigationWarning(!!mutationBusy || !!mutationUnknown),
    })
  }, [navigationUnsafe, mutationBusy, mutationUnknown])

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

  const onChange = (key, value) => setForm(current => ({ ...current, [key]: value }))
  const onToggleAgent = (key, role) => setAgentControl(current => {
    const roles = new Set(current[key] || [])
    roles.has(role) ? roles.delete(role) : roles.add(role)
    return { ...current, [key]: [...roles] }
  })
  const show = message => {
    setToast(message)
    setTimeout(() => setToast(null), 2500)
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
    const timed = deadlineRequest(signal => get('/api/settings', { signal }), SETTINGS_READ_TIMEOUT_MS)
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
        // password-box draft so the operator can Test LLM and deliberately decide what to do next.
        if (recovery.preserveSecret) next.llm_api_key = current?.llm_api_key || ''
        return next
      })
      setAgentControl(current => reconcileUnknownRecord(
        current, recovery.submittedControl, acceptedControl, recovery.uncertainControlKeys))
      setSecretState({ llm_api_key: !!settings.llm_api_key })
      setRevisions({ settings: data.settings_revision, secret: data.secret_revision })
      setMutationUnknown(null)
      show(recovery.stage.endsWith('-conflict')
        ? 'Current server settings loaded; review the retained draft before saving again'
        : recovery.preserveSecret
        ? 'Server state refreshed; test the write-only credential before replacing it again'
        : 'Settings refreshed from the server; the unknown write was not replayed')
    } catch {
      show('Could not refresh authoritative settings; the previous outcome is still unknown')
    } finally {
      finishMutation(mutation)
    }
  }
  const onSave = async () => {
    if (invalidCount) {
      show(`Fix ${countLabel(invalidCount, 'invalid numeric setting')} before saving`)
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
    if (navigationUnsafe && !window.confirm(navigationWarning(!!mutationBusy || !!mutationUnknown))) return
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
              ? 'Ordinary settings were accepted, but another credential update won. The typed replacement is retained for review.'
            : mutationUnknown.stage === 'secret-clear-conflict'
              ? 'Another credential update won before this clear. Refresh before deciding whether to clear the current credential.'
            : mutationUnknown.stage === 'secret-set'
            ? 'Ordinary settings were accepted, but the write-only API-key replacement could not be confirmed. The draft is retained; never submit it blindly.'
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
                      secretActionDisabled={!!mutationBusy || !!mutationUnknown}
                      mode={mode} query={query} schema={schema}
                      focusKey={invalidFocus.key} focusRequest={invalidFocus.request} />
      </>}
    </main>

    {form && schema && <div className="settings-actions"><div className="sa-inner">
      <LlmHealth />
      <span className="spacer" style={{ flex: 1 }} />
      {invalidCount
        ? <button type="button" className="settings-summary-link settings-save-state is-invalid"
            onClick={focusFirstInvalid}>{countLabel(invalidCount, 'invalid numeric setting')} — review</button>
        : <span className={'settings-save-state' + (unsaved ? ' is-unsaved' : '')}
            role="status" aria-live="polite">
          {unsaved ? countLabel(unsavedKeys.size, 'unsaved change') : 'All changes saved'}
        </span>}
      <button className="btn sm ghost" onClick={resetToDefaults}
              title="Reset every field to the engine default">↻ Defaults</button>
      <button className="btn sm primary" disabled={!unsaved || invalidCount > 0 || !!mutationBusy || !!mutationUnknown} onClick={onSave}>
        {mutationBusy === 'saving' ? 'Saving...' : 'Save'}
      </button>
    </div></div>}
    {toast && <div className="toast" role="status">{toast}</div>}
  </div>
}
