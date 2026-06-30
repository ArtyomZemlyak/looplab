import React, { useState } from 'react'
import { SETTINGS_GROUPS, AGENT_ROLE_PILLS } from './settingsSchema.js'

// Renders the grouped settings form from the schema. Controlled: `form` is the editable shape
// (see settingsSchema.toForm), `onChange(key, value)` reports edits. `dirty` (a Set of changed
// keys) highlights fields that differ from the engine default. `only` optionally restricts to a
// subset of group titles (the run dialog shows a compact subset by default).
//
// Groups render as TABS (one per group) instead of one long scroll — the standard app tab look.
// `secretState` (key→bool "is a value stored") drives the write-only secret fields; `onClearSecret`
// removes a stored secret; `hideSecret` drops secret fields entirely (per-run / launch dialog, where
// the global credential already applies via env).
function AgentPills({ f, granted, onToggleAgent }) {
  if (!f.agents || !onToggleAgent) return null
  return <div className="sf-agents" title="who may change this setting at runtime">
    {f.agents.map(role => {
      const p = AGENT_ROLE_PILLS[role]; const on = granted.includes(role)
      return <button key={role} type="button" className={'agpill' + (on ? ' on' : '')}
                     title={(on ? '✓ ' : '✕ ') + p.title} onClick={() => onToggleAgent(f.key, role)}>{p.short}</button>
    })}
  </div>
}

// One source of truth for the two-tier change dot (unsaved wins over differs-from-default), shared by
// the per-field label and the per-tab header so they can never disagree.
function changeDot(unsaved, changed) {
  if (unsaved) return <span className="sf-dot unsaved" title="unsaved — clears on Save">●</span>
  if (changed) return <span className="sf-dot fromdefault" title="differs from the engine default">●</span>
  return null
}

function Field({ f, value, onChange, changed, unsaved, granted, onToggleAgent, secretSet, onClearSecret }) {
  const set = (v) => onChange(f.key, v)
  let input
  if (f.type === 'bool') {
    input = <label className="switch"><input type="checkbox" checked={!!value} onChange={e => set(e.target.checked)} /><span className="track" /></label>
  } else if (f.type === 'enum') {
    input = <select className="text" value={value ?? ''} onChange={e => set(e.target.value)}>
      {f.options.map(o => <option key={o} value={o}>{o}</option>)}
    </select>
  } else if (f.type === 'secret') {
    // Write-only credential: the box is always blank (the value is never sent back from the server).
    // Typing sets a new key; the status line + Clear reflect/remove the stored one.
    input = <div className="sf-secret">
      <input className="text" type="password" autoComplete="new-password" value={value ?? ''}
             placeholder={secretSet ? '•••••••• stored — leave blank to keep' : 'not set'}
             onChange={e => set(e.target.value)} />
      {secretSet && onClearSecret &&
        <button type="button" className="btn sm ghost" title="remove the stored key"
                onClick={() => onClearSecret(f.key)}>Clear</button>}
    </div>
  } else {
    input = <input className="text" type={f.type === 'int' || f.type === 'float' ? 'number' : 'text'}
                   step={f.type === 'float' ? 'any' : undefined} value={value ?? ''}
                   placeholder={f.placeholder || ''} onChange={e => set(e.target.value)} />
  }
  const dot = changeDot(unsaved, changed)   // amber (unsaved) wins over faint (differs-from-default)
  return <div className={'sf-field' + (unsaved ? ' unsaved' : changed ? ' changed' : '')}>
    <div className="sf-label">{f.label}{dot}
      <AgentPills f={f} granted={granted || []} onToggleAgent={onToggleAgent} /></div>
    <div className="sf-input">{input}</div>
    {f.help && <div className="sf-help">{f.help}</div>}
  </div>
}

export default function SettingsForm({ form, onChange, dirty, unsaved, only, agentControl, onToggleAgent,
                                       secretState, onClearSecret, hideSecret }) {
  let groups = only ? SETTINGS_GROUPS.filter(g => only.includes(g.title)) : SETTINGS_GROUPS
  if (hideSecret) {
    groups = groups
      .map(g => ({ ...g, fields: g.fields.filter(f => f.type !== 'secret') }))
      .filter(g => g.fields.length)
  }
  const [active, setActive] = useState(0)
  const idx = groups.length ? Math.min(active, groups.length - 1) : 0
  const g = groups[idx]
  if (!g) return null
  const groupUnsaved = (gr) => gr.fields.some(f => unsaved?.has(f.key))
  const groupChanged = (gr) => gr.fields.some(f => dirty?.has(f.key))

  return <div className="settings-form tabbed">
    <div className="tabs sf-tabs">
      {groups.map((gr, i) => <div key={gr.title}
        className={'tab' + (i === idx ? ' active' : '')}
        onClick={() => setActive(i)} title={gr.sub || ''}>
        {gr.title}{changeDot(groupUnsaved(gr), groupChanged(gr))}
      </div>)}
    </div>
    <div className="sf-group" key={g.title}>
      {g.sub && <div className="sf-group-h"><span className="muted">{g.sub}</span></div>}
      <div className="sf-grid">
        {g.fields.map(f => <Field key={f.key} f={f} value={form[f.key]}
                                  changed={dirty?.has(f.key)} unsaved={unsaved?.has(f.key)} onChange={onChange}
                                  granted={agentControl?.[f.key]} onToggleAgent={onToggleAgent}
                                  secretSet={secretState?.[f.key]} onClearSecret={onClearSecret} />)}
      </div>
    </div>
  </div>
}
