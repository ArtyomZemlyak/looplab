import React, { useId, useState } from 'react'
import { filterSettingsGroups, normalizeSettingsQuery } from './settingsModel.js'
import './settings-polish.css'

// Renders the grouped settings form from the schema. Controlled: `form` is the editable shape
// (see settingsSchema.toForm), `onChange(key, value)` reports edits. `dirty` highlights fields that
// differ from the engine default; `unsaved` tracks edits since the last save.
//
// `only` and `hideSecret` keep compact consumers (run settings and launch dialogs) compatible.
// `mode` and `query` add progressive disclosure to the full Settings page.
function AgentPills({ f, granted, onToggleAgent, rolePills }) {
  if (!f.agents || !onToggleAgent) return null
  return <div className="sf-agents" role="group" aria-label={`Runtime access for ${f.label}`}>
    {f.agents.map(role => {
      const p = rolePills[role]
      const on = granted.includes(role)
      return <button key={role} type="button" className={'agpill' + (on ? ' on' : '')}
                     aria-pressed={on} aria-label={`${p.title}: ${on ? 'allowed' : 'not allowed'}`}
                     title={(on ? 'Allowed: ' : 'Not allowed: ') + p.title}
                     onClick={() => onToggleAgent(f.key, role)}>{p.short}</button>
    })}
  </div>
}

// One source of truth for the two-tier change dot (unsaved wins over differs-from-default), shared by
// the per-field label and the per-tab header so they can never disagree.
function changeDot(unsaved, changed) {
  if (unsaved) return <span className="sf-dot unsaved" title="unsaved — clears on Save" aria-label="unsaved">●</span>
  if (changed) return <span className="sf-dot fromdefault" title="differs from the engine default" aria-label="customized">●</span>
  return null
}

const safeId = value => String(value).replace(/[^a-zA-Z0-9_-]/g, '-')

function Field({ idPrefix, f, value, onChange, changed, unsaved, error, granted, onToggleAgent,
                 secretSet, onClearSecret, readOnly, rolePills }) {
  const set = (v) => onChange(f.key, v)
  const inputId = `${idPrefix}-setting-${safeId(f.key)}`
  const helpId = `${inputId}-help`
  const warningId = `${inputId}-warning`
  const errorId = `${inputId}-error`
  const readOnlyId = `${inputId}-readonly`
  const hasDescription = !!f.help || f.type === 'secret'
  const describedBy = [hasDescription ? helpId : '', f.warning ? warningId : '', error ? errorId : '',
    readOnly ? readOnlyId : '']
    .filter(Boolean).join(' ') || undefined
  let input

  if (f.type === 'bool') {
    input = <label className="switch" title={`Toggle ${f.label}`}>
      <input id={inputId} name={f.key} type="checkbox" checked={!!value}
             aria-describedby={describedBy} disabled={readOnly}
             onChange={e => set(e.target.checked)} />
      <span className="track" aria-hidden="true" />
    </label>
  } else if (f.type === 'enum') {
    input = <select id={inputId} name={f.key} className="text" value={value ?? ''}
                    aria-describedby={describedBy} disabled={readOnly}
                    onChange={e => set(e.target.value)}>
      {f.options.map(o => <option key={o || '__default'} value={o}>{o === '' ? 'Use provider default' : o}</option>)}
    </select>
  } else if (f.type === 'secret') {
    // Write-only credential: the box is always blank (the value is never sent back from the server).
    input = <div className="sf-secret">
      <input id={inputId} name={f.key} className="text" type="password" autoComplete="new-password"
             value={value ?? ''} aria-describedby={describedBy}
             disabled={readOnly}
             placeholder={secretSet ? 'Stored — leave blank to keep' : 'Not set'}
             onChange={e => set(e.target.value)} />
      {secretSet && onClearSecret &&
        <button type="button" className="btn sm ghost" aria-label={`Clear stored ${f.label}`}
                title="remove the stored key" onClick={() => onClearSecret(f.key)}>Clear</button>}
    </div>
  } else {
    const numeric = f.type === 'int' || f.type === 'float'
    // Keep the operator's raw transitional text (`-`, `1e`, etc.) intact. Native number inputs
    // sanitize those states to an empty string, which is our deliberate clear/null operation.
    input = <input id={inputId} name={f.key} className="text"
                   type="text" inputMode={f.type === 'int' ? 'numeric' : f.type === 'float' ? 'decimal' : undefined}
                   value={value ?? ''} spellCheck={numeric ? false : undefined}
                   aria-describedby={describedBy} aria-invalid={error ? 'true' : undefined}
                   aria-readonly={readOnly || undefined} readOnly={readOnly}
                   placeholder={f.placeholder || ''}
                   onChange={e => set(e.target.value)} />
  }

  const dot = changeDot(unsaved, changed)
  return <div className={'sf-field' + (unsaved ? ' unsaved' : changed ? ' changed' : '') + (error ? ' invalid' : '') + (readOnly ? ' readonly' : '')}>
    <div className="sf-label-row">
      <label className="sf-label" htmlFor={inputId}>{f.label}{dot}
        {readOnly && <span className="muted" title="Fixed when this run started"> · launch-pinned</span>}
      </label>
      <AgentPills f={f} granted={granted || []} onToggleAgent={readOnly ? undefined : onToggleAgent}
        rolePills={rolePills} />
    </div>
    <div className="sf-input">{input}</div>
    {error && <div id={errorId} className="sf-error" role="alert">{error}</div>}
    {readOnly && <div id={readOnlyId} className="sf-help" role="note">
      Fixed when this run started. Create a new run to use a different value; resume and replay keep this recorded value.
    </div>}
    {hasDescription && <div id={helpId} className="sf-help">
      {f.type === 'secret' && <span className="sf-secret-state">
        {secretSet ? 'A credential is stored. Enter a value only to replace it. ' : 'No credential is stored. '}
      </span>}
      {f.help}
    </div>}
    {f.warning && <div id={warningId} className={`sf-warning${f.warningTone === 'info' ? ' info' : ''}`} role="note">
      <strong>{f.warningTitle || 'High-risk experimental setting.'}</strong> {f.warning}
    </div>}
  </div>
}

function GroupPanel({ group, idPrefix, form, onChange, dirty, unsaved, errors, agentControl,
                      onToggleAgent, secretState, onClearSecret, readOnlyKeys,
                      panelId, labelledBy, searchable, rolePills }) {
  const headingId = `${idPrefix}-heading-${safeId(group.title)}`
  return <section className="sf-group" id={panelId}
                  role={labelledBy ? 'tabpanel' : undefined}
                  aria-labelledby={labelledBy || headingId} tabIndex={labelledBy ? 0 : undefined}>
    <div className="sf-group-h">
      {searchable && <h2 id={headingId}>{group.title}</h2>}
      {group.sub && <span className="muted">{group.sub}</span>}
    </div>
    <div className="sf-grid">
      {group.fields.map(f => <Field key={f.key} idPrefix={idPrefix} f={f} value={form[f.key]}
        changed={dirty?.has(f.key)} unsaved={unsaved?.has(f.key)} error={errors?.[f.key]} onChange={onChange}
        granted={agentControl?.[f.key]} onToggleAgent={onToggleAgent}
        secretSet={secretState?.[f.key]} onClearSecret={onClearSecret}
        readOnly={readOnlyKeys?.has(f.key)} rolePills={rolePills} />)}
    </div>
  </section>
}

export default function SettingsForm({ form, onChange, dirty, unsaved, errors, only, agentControl, onToggleAgent,
                                       secretState, onClearSecret, readOnlyKeys, hideSecret,
                                       mode = 'all', query = '', schema }) {
  const groups = filterSettingsGroups(schema.groups, { mode, query, only, hideSecret })
  const rolePills = schema.agentRolePills
  const [active, setActive] = useState(0)
  const reactId = useId()
  const idPrefix = `sf-${safeId(reactId)}`
  const searching = !!normalizeSettingsQuery(query)
  const idx = groups.length ? Math.min(active, groups.length - 1) : 0
  const group = groups[idx]
  const groupUnsaved = gr => gr.fields.some(f => unsaved?.has(f.key))
  const groupChanged = gr => gr.fields.some(f => dirty?.has(f.key))

  if (!groups.length) return <div className="settings-empty" role="status">
    <strong>No settings match “{query.trim()}”</strong>
    <span>Try a field name, key, option, or a broader term.</span>
  </div>

  if (searching) return <div className="settings-form settings-search-results" role="form"
                              aria-label="Matching settings">
    {groups.map(gr => <GroupPanel key={gr.title} group={gr} idPrefix={idPrefix} form={form}
      onChange={onChange} dirty={dirty} unsaved={unsaved} errors={errors} agentControl={agentControl}
      onToggleAgent={onToggleAgent} secretState={secretState} onClearSecret={onClearSecret}
      readOnlyKeys={readOnlyKeys} searchable rolePills={rolePills} />)}
  </div>

  const onTabKeyDown = (event, index) => {
    let next = index
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % groups.length
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + groups.length) % groups.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = groups.length - 1
    else return
    event.preventDefault()
    setActive(next)
    event.currentTarget.parentElement?.querySelectorAll('[role="tab"]')[next]?.focus()
  }

  const tabId = `${idPrefix}-tab-${idx}`
  const panelId = `${idPrefix}-panel-${idx}`
  return <div className="settings-form tabbed" role="form" aria-label="Settings fields">
    <div className="tabs sf-tabs" role="tablist" aria-label="Settings sections">
      {groups.map((gr, index) => <button key={gr.title} type="button" role="tab"
        id={`${idPrefix}-tab-${index}`}
        aria-controls={index === idx ? `${idPrefix}-panel-${index}` : undefined}
        aria-selected={index === idx} tabIndex={index === idx ? 0 : -1}
        className={'tab' + (index === idx ? ' active' : '')}
        onClick={() => setActive(index)} onKeyDown={event => onTabKeyDown(event, index)}
        title={gr.sub || ''}>
        {gr.title}{changeDot(groupUnsaved(gr), groupChanged(gr))}
      </button>)}
    </div>
    <GroupPanel key={group.title} group={group} idPrefix={idPrefix} form={form}
      onChange={onChange} dirty={dirty} unsaved={unsaved} errors={errors} agentControl={agentControl}
      onToggleAgent={onToggleAgent} secretState={secretState} onClearSecret={onClearSecret}
      readOnlyKeys={readOnlyKeys} panelId={panelId} labelledBy={tabId} rolePills={rolePills} />
  </div>
}
