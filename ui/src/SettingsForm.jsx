import React from 'react'
import { SETTINGS_GROUPS } from './settingsSchema.js'

// Renders the grouped settings form from the schema. Controlled: `form` is the editable shape
// (see settingsSchema.toForm), `onChange(key, value)` reports edits. `dirty` (a Set of changed
// keys) highlights fields that differ from the engine default. `only` optionally restricts to a
// subset of group titles (the run dialog shows a compact subset by default).
function Field({ f, value, onChange, changed }) {
  const set = (v) => onChange(f.key, v)
  let input
  if (f.type === 'bool') {
    input = <label className="switch"><input type="checkbox" checked={!!value} onChange={e => set(e.target.checked)} /><span className="track" /></label>
  } else if (f.type === 'enum') {
    input = <select className="text" value={value ?? ''} onChange={e => set(e.target.value)}>
      {f.options.map(o => <option key={o} value={o}>{o}</option>)}
    </select>
  } else {
    input = <input className="text" type={f.type === 'int' || f.type === 'float' ? 'number' : 'text'}
                   step={f.type === 'float' ? 'any' : undefined} value={value ?? ''}
                   placeholder={f.placeholder || ''} onChange={e => set(e.target.value)} />
  }
  return <div className={'sf-field' + (changed ? ' changed' : '')}>
    <div className="sf-label">{f.label}{changed && <span className="sf-dot" title="changed from default">●</span>}</div>
    <div className="sf-input">{input}</div>
    {f.help && <div className="sf-help">{f.help}</div>}
  </div>
}

export default function SettingsForm({ form, onChange, dirty, only }) {
  const groups = only ? SETTINGS_GROUPS.filter(g => only.includes(g.title)) : SETTINGS_GROUPS
  return <div className="settings-form">
    {groups.map(g => <div className="sf-group" key={g.title}>
      <div className="sf-group-h"><b>{g.title}</b>{g.sub && <span className="muted">{g.sub}</span>}</div>
      <div className="sf-grid">
        {g.fields.map(f => <Field key={f.key} f={f} value={form[f.key]}
                                  changed={dirty?.has(f.key)} onChange={onChange} />)}
      </div>
    </div>)}
  </div>
}
