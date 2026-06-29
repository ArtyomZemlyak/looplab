import React, { useEffect, useMemo, useState } from 'react'
import { getSettings, saveSettings, llmHealth } from './util.js'
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
  const [toast, setToast] = useState(null)
  const load = () => getSettings().then(d => {
    const f = toForm(d.settings); setDefaults(d.defaults); setForm(f); setSaved(f)
  }).catch(() => {})
  useEffect(() => { load() }, [])

  const dirty = useMemo(() => {   // keys whose value differs from the engine default (the "●")
    if (!form || !defaults) return new Set()
    const cur = fromForm(form); const s = new Set()
    for (const k of Object.keys(FIELD_BY_KEY)) {
      const d = defaults[k] ?? (FIELD_BY_KEY[k].type === 'list' ? [] : null)
      if (JSON.stringify(cur[k]) !== JSON.stringify(d ?? null)) s.add(k)
    }
    return s
  }, [form, defaults])
  const unsaved = useMemo(() => form && saved && JSON.stringify(form) !== JSON.stringify(saved), [form, saved])

  const onChange = (k, v) => setForm(f => ({ ...f, [k]: v }))
  const show = (m) => { setToast(m); setTimeout(() => setToast(null), 2000) }
  const onSave = async () => {
    try { const r = await saveSettings(fromForm(form)); const f = toForm(r.settings); setForm(f); setSaved(f); show('settings saved — applied to new runs') }
    catch (e) { show('save failed: ' + e.message) }
  }
  const resetToDefaults = () => { if (defaults) setForm(toForm(defaults)) }

  return <div className="app">
    <div className="topbar">
      <span className="brand"><span className="dot">◉</span> LoopLab</span>
      <button className="btn sm ghost" onClick={onBack}>← runs</button>
      <span className="ttl" style={{ fontWeight: 700, fontSize: 15 }}>Settings</span>
      <span className="muted">engine defaults for new runs</span>
      <span className="spacer" style={{ flex: 1 }} />
      <LlmHealth />
      {unsaved && <span className="pill" style={{ color: 'var(--working)', borderColor: '#7a5a1d' }}>unsaved</span>}
      <button className="btn sm ghost" onClick={resetToDefaults} title="reset every field to the engine default">↺ Defaults</button>
      <button className="btn sm primary" disabled={!unsaved} onClick={onSave}>Save</button>
    </div>
    <div className="settings-page">
      {!form ? <div className="notice">Loading settings…</div>
        : <>
          <div className="muted" style={{ marginBottom: 14 }}>
            These are saved as defaults and applied to every new run via <code>LOOPLAB_*</code> env.
            Per-run overrides are still available in the <b>New run</b> dialog. A <span style={{ color: 'var(--accent)' }}>●</span> marks a value changed from the engine default.
          </div>
          <SettingsForm form={form} onChange={onChange} dirty={dirty} />
        </>}
    </div>
    {toast && <div className="toast">{toast}</div>}
  </div>
}
