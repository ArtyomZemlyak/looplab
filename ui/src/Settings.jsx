import React, { useEffect, useMemo, useState } from 'react'
import { getSettings, saveSettings, saveSecret, llmHealth } from './util.js'
import { toForm, fromForm, FIELD_BY_KEY } from './settingsSchema.js'
import SettingsForm from './SettingsForm.jsx'
import { OpIcon } from './icons.jsx'

// LLM endpoint self-test (the UI equivalent of `LoopLab smoke`): pings the configured model so the
// user knows it's reachable before launching a run against it.
export function LlmHealth() {
  const [s, setS] = useState(null)
  const [busy, setBusy] = useState(false)
  const check = () => { setBusy(true); llmHealth().then(setS).catch(e => setS({ ok: false, error: e.message })).finally(() => setBusy(false)) }
  return <span className="llm-health">
    <button className="btn sm" disabled={busy} onClick={check} title="ping the configured LLM endpoint">{busy ? '… pinging' : <><OpIcon name="bolt" className="t-ic" /> Test LLM</>}</button>
    {s && <span className={'chip ' + (s.ok ? 'ok' : 'alarm')} title={s.ok ? s.text : s.error}>
      {s.ok ? '✓' : '✗'} {s.model}</span>}
  </span>
}

// Full-page editor for the engine DEFAULTS used by every new run (persisted server-side in
// <run-root>/ui_settings.json and applied to a spawned run as LOOPLAB_* env). Per-run overrides
// live in each run's "Settings" panel (the snapshot the next resume reads); this is the global default.
export default function Settings({ onBack }) {
  const [defaults, setDefaults] = useState(null)
  const [form, setForm] = useState(null)
  const [saved, setSaved] = useState(null)   // last persisted form (to detect unsaved edits)
  const [agentControl, setAgentControl] = useState({})   // Settings.agent_control matrix (governance pills)
  const [savedAC, setSavedAC] = useState({})
  const [secretState, setSecretState] = useState({})     // key → is a value stored server-side (masked)
  const [toast, setToast] = useState(null)
  const load = () => getSettings().then(d => {
    const f = toForm(d.settings); setDefaults(d.defaults); setForm(f); setSaved(f)
    const ac = d.settings.agent_control || {}; setAgentControl(ac); setSavedAC(ac)
    setSecretState({ llm_api_key: !!d.settings.llm_api_key })   // server reports a stored secret as "***"
  }).catch(() => {})
  useEffect(() => { load() }, [])

  const dirty = useMemo(() => {   // keys whose value differs from the engine default (the "●")
    if (!form || !defaults) return new Set()
    const cur = fromForm(form); const s = new Set()
    for (const k of Object.keys(FIELD_BY_KEY)) {
      if (FIELD_BY_KEY[k].type === 'secret') continue   // write-only; fromForm skips it, so it can't be compared
      const d = defaults[k] ?? (FIELD_BY_KEY[k].type === 'list' ? [] : null)
      if (JSON.stringify(cur[k]) !== JSON.stringify(d ?? null)) s.add(k)
    }
    return s
  }, [form, defaults])
  const unsaved = useMemo(() => form && saved &&
    (JSON.stringify(form) !== JSON.stringify(saved) || JSON.stringify(agentControl) !== JSON.stringify(savedAC)),
    [form, saved, agentControl, savedAC])
  // Per-field UNSAVED set (form value ≠ last-saved) — drives the amber dot that DOES clear on Save,
  // distinct from `dirty` above (value ≠ engine default, which persists after saving a custom value).
  const unsavedKeys = useMemo(() => {
    const s = new Set()
    if (form && saved) for (const k of Object.keys(form)) {
      if (JSON.stringify(form[k]) !== JSON.stringify(saved[k])) s.add(k)
    }
    return s
  }, [form, saved])

  const onChange = (k, v) => setForm(f => ({ ...f, [k]: v }))
  // Toggle whether a role may change a setting (the governance pills).
  const onToggleAgent = (key, role) => setAgentControl(ac => {
    const cur = new Set(ac[key] || [])
    cur.has(role) ? cur.delete(role) : cur.add(role)
    return { ...ac, [key]: [...cur] }
  })
  const show = (m) => { setToast(m); setTimeout(() => setToast(null), 2500) }
  const onSave = async () => {
    try {
      // A secret field never travels in the settings payload (fromForm skips it) — store it via the
      // dedicated owner-only endpoint. A non-empty value means "set a new key"; blank = keep existing.
      const apiKey = (form.llm_api_key || '').trim()
      const r = await saveSettings({ ...fromForm(form), agent_control: agentControl })
      if (apiKey) { const s = await saveSecret('llm_api_key', apiKey); setSecretState(st => ({ ...st, llm_api_key: !!s.set })) }
      const f = toForm(r.settings); setForm(f); setSaved(f)   // toForm blanks the secret box again
      const ac = r.settings.agent_control || {}; setAgentControl(ac); setSavedAC(ac)
      show('settings saved' + (apiKey ? ' · API key stored securely' : '') + ' — applied to new runs')
    } catch (e) { show('save failed: ' + e.message) }
  }
  const onClearSecret = async (key) => {
    try {
      await saveSecret(key, '')
      setSecretState(st => ({ ...st, [key]: false }))
      setForm(f => ({ ...f, [key]: '' }))
      show('API key cleared')
    } catch (e) { show('clear failed: ' + e.message) }
  }
  const resetToDefaults = () => { if (defaults) { setForm(toForm(defaults)); setAgentControl(defaults.agent_control || {}) } }

  return <div className="app">
    <div className="topbar">
      <span className="brand"><span className="dot">◉</span> LoopLab</span>
      <button className="btn sm ghost" onClick={onBack}>← runs</button>
      <span className="ttl" style={{ fontWeight: 700, fontSize: 15 }}>Settings</span>
      <span className="muted">engine defaults for new runs</span>
      <span className="spacer" style={{ flex: 1 }} />
    </div>
    <div className="settings-page">
      {!form ? <div className="notice">Loading settings…</div>
        : <>
          <div className="muted" style={{ marginBottom: 14 }}>
            These are saved as defaults and applied to every new run via <code>LOOPLAB_*</code> env.
            Per-run overrides are still available in the <b>New run</b> dialog.
            A <span style={{ color: 'var(--working)' }}>●</span> marks an <b>unsaved</b> edit (clears on Save);
            a <span style={{ color: 'var(--accent)', opacity: .6 }}>●</span> marks a saved value that differs from the engine default.
            The <span className="agpill on" style={{ position: 'static' }}>R</span><span className="agpill on" style={{ position: 'static' }}>S</span><span className="agpill on" style={{ position: 'static' }}>B</span> pills set whether the <b>R</b>esearcher (per experiment), <b>S</b>trategist, or <b>B</b>oss may change a setting at runtime.
          </div>
          <SettingsForm form={form} onChange={onChange} dirty={dirty} unsaved={unsavedKeys} agentControl={agentControl} onToggleAgent={onToggleAgent}
                        secretState={secretState} onClearSecret={onClearSecret} />
        </>}
    </div>
    {/* Action bar lives INSIDE the settings window (a flex child pinned to the bottom of the viewport),
        so Save / Defaults / Test-LLM stay with the settings instead of the global top bar. */}
    {form && <div className="settings-actions"><div className="sa-inner">
      <LlmHealth />
      <span className="spacer" style={{ flex: 1 }} />
      {unsaved && <span className="pill" style={{ color: 'var(--working)', borderColor: '#7a5a1d' }}>unsaved</span>}
      <button className="btn sm ghost" onClick={resetToDefaults} title="reset every field to the engine default">↺ Defaults</button>
      <button className="btn sm primary" disabled={!unsaved} onClick={onSave}>Save</button>
    </div></div>}
    {toast && <div className="toast">{toast}</div>}
  </div>
}
