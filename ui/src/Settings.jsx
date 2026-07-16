import React, { useEffect, useMemo, useRef, useState } from 'react'
import { getSettings, saveSettings, saveSecret, llmHealth } from './util.js'
import { toForm, fromForm, settingsSavePayload, settingsValidationErrors, FIELD_BY_KEY, SETTINGS_GROUPS } from './settingsSchema.js'
import { filterSettingsGroups, reconcileAcceptedRecord, settingsViewStats } from './settingsModel.js'
import SettingsForm from './SettingsForm.jsx'
import { OpIcon } from './icons.jsx'

const countLabel = (count, singular, plural = `${singular}s`) => `${count} ${count === 1 ? singular : plural}`

// LLM endpoint self-test (the UI equivalent of `LoopLab smoke`): pings the configured model so the
// user knows it is reachable before launching a run against it.
export function LlmHealth() {
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)
  const check = () => {
    setBusy(true)
    llmHealth().then(setStatus).catch(() => setStatus({ ok: false, error: 'Check provider configuration and network access.' })).finally(() => setBusy(false))
  }
  return <span className="llm-health">
    <button className="btn sm" disabled={busy} onClick={check} title="Ping the configured LLM endpoint">
      {busy ? '… pinging' : <><OpIcon name="bolt" className="t-ic" /> Test LLM</>}
    </button>
    {status && <span className={'chip ' + (status.ok ? 'ok' : 'alarm')}
                     title={status.ok ? status.text : 'Check provider configuration and network access.'} role="status">
      {status.ok ? '✓' : '×'} {status.model || 'Connection failed'}
    </span>}
  </span>
}

// Full-page editor for the engine defaults used by every new run. Per-run overrides remain in each
// run's Settings panel; this page deliberately starts with the small set most people need.
export default function Settings({ onBack }) {
  const [defaults, setDefaults] = useState(null)
  const [form, setForm] = useState(null)
  const [saved, setSaved] = useState(null)
  const [agentControl, setAgentControl] = useState({})
  const [savedAC, setSavedAC] = useState({})
  const [secretState, setSecretState] = useState({})
  const [loadError, setLoadError] = useState('')
  const [toast, setToast] = useState(null)
  const [mode, setMode] = useState('essential')
  const [query, setQuery] = useState('')
  const [mutationBusy, setMutationBusy] = useState('')
  const mutationRef = useRef(null)

  const load = () => {
    setLoadError('')
    return getSettings().then(data => {
      const settings = data.settings || {}
      const nextForm = toForm(settings)
      setDefaults(data.defaults)
      setForm(nextForm)
      setSaved(nextForm)
      const control = settings.agent_control || {}
      setAgentControl(control)
      setSavedAC(control)
      setSecretState({ llm_api_key: !!settings.llm_api_key })
    }).catch(error => setLoadError(error?.message || 'Could not load settings.'))
  }
  useEffect(() => { load() }, [])

  // Values that differ from engine defaults stay marked after Save.
  const dirty = useMemo(() => {
    if (!form || !defaults) return new Set()
    const current = fromForm(form)
    const changed = new Set()
    for (const key of Object.keys(FIELD_BY_KEY)) {
      if (FIELD_BY_KEY[key].type === 'secret') continue
      const defaultValue = defaults[key] ?? (FIELD_BY_KEY[key].type === 'list' ? [] : null)
      if (JSON.stringify(current[key]) !== JSON.stringify(defaultValue ?? null)) changed.add(key)
    }
    return changed
  }, [form, defaults])

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
  const validationErrors = useMemo(() => form ? settingsValidationErrors(form) : {}, [form])
  const invalidCount = Object.keys(validationErrors).length

  const visibleGroups = useMemo(() => filterSettingsGroups(SETTINGS_GROUPS, { mode, query }), [mode, query])
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
    if (mutationRef.current) return null
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
  const onSave = async () => {
    if (invalidCount) {
      show(`Fix ${countLabel(invalidCount, 'invalid numeric setting')} before saving`)
      return
    }
    const mutation = beginMutation('saving')
    if (!mutation) { show('A settings update is already in progress'); return }
    const submittedForm = form
    const submittedControl = agentControl
    const apiKey = (submittedForm.llm_api_key || '').trim()
    try {
      const settingsPatch = settingsSavePayload(submittedForm, submittedControl, saved, savedAC)
      const settingsChanged = Object.keys(settingsPatch).length > 0
      // PATCH only edits since this tab's baseline; replaying the full stale form
      // would overwrite disjoint settings saved by another tab after this one loaded.
      const result = await saveSettings(settingsPatch)
      const acceptedForm = toForm(result.settings)
      const acceptedControl = result.settings.agent_control || {}

      // Commit the ordinary-settings ACK immediately. If the independent secret write fails, the
      // accepted baseline must not be resent, while the API-key input remains available to retry.
      setSaved(acceptedForm)
      setSavedAC(acceptedControl)
      const formBeforeSecretAck = apiKey
        ? { ...acceptedForm, llm_api_key: submittedForm.llm_api_key }
        : acceptedForm
      setForm(current => reconcileAcceptedRecord(current, submittedForm, formBeforeSecretAck))
      setAgentControl(current => reconcileAcceptedRecord(current, submittedControl, acceptedControl))
      if (apiKey) {
        let resultSecret
        try {
          resultSecret = await saveSecret('llm_api_key', apiKey)
        } catch (error) {
          const prefix = settingsChanged
            ? 'Settings saved, but the API key was not stored: '
            : 'API key was not stored: '
          show(prefix + (error.message || error))
          return
        }
        setSecretState(current => ({ ...current, llm_api_key: resultSecret.set === true }))
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
      show('Save failed: ' + error.message)
    } finally {
      finishMutation(mutation)
    }
  }
  const onClearSecret = async key => {
    const mutation = beginMutation('clearing secret')
    if (!mutation) { show('A settings update is already in progress'); return }
    const submittedForm = form
    try {
      await saveSecret(key, '')
      setSecretState(current => ({ ...current, [key]: false }))
      setForm(current => reconcileAcceptedRecord(current, submittedForm, { ...submittedForm, [key]: '' }))
      show('API key cleared')
    } catch (error) {
      show('Clear failed: ' + error.message)
    } finally {
      finishMutation(mutation)
    }
  }
  const resetToDefaults = () => {
    if (defaults) {
      setForm(toForm(defaults))
      setAgentControl(defaults.agent_control || {})
    }
  }
  const revealChanges = () => {
    setQuery('')
    setMode('all')
  }

  return <div className="app">
    <div className="topbar">
      <span className="brand"><span className="dot">◉</span> LoopLab</span>
      <button className="btn sm ghost" onClick={onBack}>← runs</button>
      <span className="ttl" style={{ fontWeight: 700, fontSize: 15 }}>Settings</span>
      <span className="muted">engine defaults for new runs</span>
      <span className="spacer" style={{ flex: 1 }} />
    </div>

    <main className="settings-page" data-route-main tabIndex={-1}>
      {!form ? (loadError
        ? <div className="notice resource-error" role="alert"><b>Could not load settings.</b><span>{loadError}</span><button className="btn sm primary" onClick={load}>Retry</button></div>
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

        <SettingsForm form={form} onChange={onChange} dirty={dirty} unsaved={unsavedKeys}
                      errors={validationErrors}
                      agentControl={agentControl} onToggleAgent={onToggleAgent}
                      secretState={secretState} onClearSecret={onClearSecret}
                      mode={mode} query={query} />
      </>}
    </main>

    {form && <div className="settings-actions"><div className="sa-inner">
      <LlmHealth />
      <span className="spacer" style={{ flex: 1 }} />
      <span className={'settings-save-state' + (unsaved ? ' is-unsaved' : '') + (invalidCount ? ' is-invalid' : '')}
            role="status" aria-live="polite">
        {invalidCount ? countLabel(invalidCount, 'invalid numeric setting')
          : unsaved ? countLabel(unsavedKeys.size, 'unsaved change') : 'All changes saved'}
      </span>
      <button className="btn sm ghost" onClick={resetToDefaults}
              title="Reset every field to the engine default">↻ Defaults</button>
      <button className="btn sm primary" disabled={!unsaved || invalidCount > 0 || !!mutationBusy} onClick={onSave}>
        {mutationBusy === 'saving' ? 'Saving...' : 'Save'}
      </button>
    </div></div>}
    {toast && <div className="toast" role="status">{toast}</div>}
  </div>
}
