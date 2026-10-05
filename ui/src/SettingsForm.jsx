import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import React, { useEffect, useId, useRef, useState } from 'react'
import { filterSettingsGroups, normalizeSettingsQuery } from './settingsModel.js'
import './settings-polish.css'

// Renders the grouped settings form from the schema. Controlled: `form` is the editable shape
// (see settingsSchema.toForm), `onChange(key, value)` reports edits. `dirty` highlights fields that
// differ from the engine default; `unsaved` tracks edits since the last save.
//
// `only` and `hideSecret` keep compact consumers (run settings and launch dialogs) compatible.
// `mode` and `query` add progressive disclosure to the full Settings page.
function AgentPills({ f, granted, onToggleAgent, rolePills, interactionDisabled = false }) {
  useUILanguage()

  if (!f.agents || !onToggleAgent) return null
  return <div className="sf-agents" role="group" aria-label={uiMessage("Runtime access for {0}", [uiText(f.label)])}>
    {f.agents.map(role => {
      const p = rolePills[role]
      const on = granted.includes(role)
      return <button key={role} type="button" className={'agpill' + (on ? ' on' : '')}
                     disabled={interactionDisabled}
                     aria-pressed={on} aria-label={`${uiText(p.title)}: ${uiText(on ? 'allowed' : 'not allowed')}`}
                     title={uiMessage(on ? 'Allowed: {0}' : 'Not allowed: {0}', [uiText(p.title)])}
                     onClick={() => onToggleAgent(f.key, role)}>{p.short}</button>
    })}
  </div>
}

// One source of truth for the two-tier change dot (unsaved wins over differs-from-default), shared by
// the per-field label and the per-tab header so they can never disagree.
function changeDot(unsaved, changed) {
  if (unsaved) return <span className="sf-dot unsaved" title={uiText("unsaved — clears on Save")} aria-label={uiText("unsaved")}>●</span>
  if (changed) return <span className="sf-dot fromdefault" title={uiText("differs from the engine default")} aria-label={uiText("customized")}>●</span>
  return null
}

const safeId = value => String(value).replace(/[^a-zA-Z0-9_-]/g, '-')
const credentialSourceLabel = source => ({
  stored: 'stored settings',
  environment: 'process environment',
  dotenv: '.env file',
  none: 'no source',
}[source] || 'unknown source')

function Field({ idPrefix, f, value, onChange, changed, unsaved, error, granted, onToggleAgent,
                 secretSet, credential, onClearSecret, secretActionDisabled, readOnly, rolePills,
                 interactionDisabled = false, compact = false }) {
  useUILanguage()

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
  const storedCredential = credential ? credential.stored : !!secretSet
  const effectiveCredential = credential?.effective === true
  const activeCredential = credential?.active === true
  const ambientCredential = credential
    && (credential.source === 'environment' || credential.source === 'dotenv')
  const ambientEffectiveCredential = ambientCredential && effectiveCredential
  const storedFallbackUnderAmbient = ambientCredential && storedCredential
  const clearableCredential = credential ? credential.clearable : storedCredential
  const incompleteStoredCredential = credential?.source === 'stored'
    && credential.status === 'incomplete'

  if (f.type === 'bool') {
    input = <label className="switch" title={uiMessage("Toggle {0}", [f.label])}>
      <input id={inputId} name={f.key} type="checkbox" checked={!!value}
             aria-describedby={describedBy} disabled={readOnly || interactionDisabled}
             onChange={e => set(e.target.checked)} />
      <span className="track" aria-hidden="true" />
    </label>
  } else if (f.type === 'enum') {
    input = <select id={inputId} name={f.key} className="text" value={value ?? ''}
                    aria-describedby={describedBy} disabled={readOnly || interactionDisabled}
                    onChange={e => set(e.target.value)}>
      {f.options.map(o => <option key={o || '__default'} value={o}>{o === '' ? uiText('Use provider default') : uiText(o)}</option>)}
    </select>
  } else if (f.type === 'secret') {
    // Write-only credential: the box is always blank (the value is never sent back from the server).
    input = <div className="sf-secret">
      <input id={inputId} name={f.key} className="text" type="password" autoComplete="new-password"
             value={value ?? ''} aria-describedby={describedBy}
             disabled={readOnly || interactionDisabled}
             placeholder={((ambientCredential ? (ambientEffectiveCredential ? uiText('Ambient shared key matches base URL — enter to store a fallback') : uiText('Ambient source has no key — enter to store a fallback')) : (storedCredential ? (incompleteStoredCredential ? uiText('API key missing — enter to complete the pair') : uiText('Stored — leave blank to keep')) : uiText('Not set'))))}
             onChange={e => set(e.target.value)} />
      {storedCredential && clearableCredential && onClearSecret &&
        <button type="button" className="btn sm ghost"
                aria-label={((incompleteStoredCredential ? uiText('Clear incomplete stored credential pair') : storedFallbackUnderAmbient ? uiMessage("Clear stored fallback {0} and endpoint binding", [f.label]) : uiMessage("Clear stored {0} and endpoint binding", [f.label])))}
                title={((incompleteStoredCredential ? uiText('Remove the orphan stored endpoint binding immediately (separate from Save)') : (storedFallbackUnderAmbient ? uiText('Remove the stored fallback key and endpoint binding; the ambient source remains untouched') : uiText('Remove the stored key and endpoint binding immediately (separate from Save)'))))}
                disabled={secretActionDisabled}
                onClick={() => onClearSecret(f.key)}>{uiText("Clear now")}</button>}
      {ambientCredential && <span className="sf-secret-readonly"
        title={(ambientEffectiveCredential ? uiMessage("The effective credential comes from the {0} and cannot be changed or cleared here.{1}", [credentialSourceLabel(credential.source), storedFallbackUnderAmbient ? ' The separate stored fallback can be cleared.' : '']) : uiMessage("The {0} controls credential resolution but currently supplies no effective key; it cannot be changed or cleared here.{1}", [credentialSourceLabel(credential.source), storedFallbackUnderAmbient ? ' The separate stored fallback can be cleared.' : '']))}>{uiText("Ambient source · read-only")}</span>}
    </div>
  } else {
    const numeric = f.type === 'int' || f.type === 'float'
    // Keep the operator's raw transitional text (`-`, `1e`, etc.) intact. Native number inputs
    // sanitize those states to an empty string, which is our deliberate clear/null operation.
    input = <input id={inputId} name={f.key} className="text"
                   type="text" inputMode={f.type === 'int' ? 'numeric' : f.type === 'float' ? 'decimal' : undefined}
                   value={value ?? ''} spellCheck={numeric ? false : undefined}
                   aria-describedby={describedBy} aria-invalid={error ? 'true' : undefined}
                   aria-readonly={readOnly || interactionDisabled || undefined}
                   readOnly={readOnly || interactionDisabled}
                   placeholder={f.placeholder || ''}
                   onChange={e => set(e.target.value)} />
  }

  const dot = changeDot(unsaved, changed)
  const permissions = <AgentPills f={f} granted={granted || []}
    onToggleAgent={readOnly ? undefined : onToggleAgent}
    rolePills={rolePills} interactionDisabled={interactionDisabled} />
  return <div className={'sf-field' + (unsaved ? ' unsaved' : changed ? ' changed' : '') + (error ? ' invalid' : '') + (readOnly ? ' readonly' : '')}>
    <div className="sf-label-row">
      <label className="sf-label" htmlFor={inputId}>{uiText(f.label)}{dot}
        {readOnly && <span className="muted" title={uiText("Fixed when this run started")}>{uiText(" · launch-pinned")}</span>}
      </label>
      {compact && f.agents && onToggleAgent && !readOnly
        ? <details className="sf-details sf-runtime-access"><summary>{uiText("Runtime permissions")}</summary>
          {permissions}
        </details>
        : permissions}
    </div>
    <div className="sf-input">{input}</div>
    {error && <div id={errorId} className="sf-error" role="alert">{uiText(error)}</div>}
    {readOnly && <div id={readOnlyId} className="sf-help" role="note">{uiText("Fixed when this run started. Create a new run to use a different value; resume and replay keep this recorded value.")}</div>}
    {hasDescription && <div id={helpId} className="sf-help">
      {f.type === 'secret' && (credential ? <span className="sf-secret-state">{uiText("Stored material: ")}{((storedCredential ? uiText('yes') : uiText('no')))}{uiText(" · Shared key: ")}{((effectiveCredential ? uiText('yes') : uiText('no')))}{uiText(" · Matches base URL: ")}{((activeCredential ? uiText('yes') : uiText('no')))}.{' '}
        {((ambientCredential ? ambientEffectiveCredential ? uiMessage("The effective key comes from the {0} and is read-only here. A value entered above is stored only as a fallback pair while that override exists. {1}", [credentialSourceLabel(credential.source), storedCredential ? 'Existing stored material may be a complete pair or only a binding; its key is never exposed. ' : '']) : uiMessage("The {0} controls credential resolution but supplies no effective key. A value entered above is stored only as an inactive fallback pair while that ambient source remains selected. {1}", [credentialSourceLabel(credential.source), storedCredential ? 'Existing stored material may be a complete pair or only a binding; its key is never exposed. ' : '']) : (incompleteStoredCredential ? uiText('The stored pair is missing its API key. Enter a value to complete and rebind it to the saved endpoint, or use Clear now to remove the incomplete pair. ') : (storedCredential ? uiText('Enter a value only to replace the stored key. ') : uiText('No credential is stored. ')))))}
        {((storedCredential && clearableCredential ? (storedFallbackUnderAmbient ? uiText('Clear now removes only the stored fallback; the ambient source remains untouched. ') : uiText('Clear now is immediate and separate from Save. ')) : ''))}
      </span> : <span className="sf-secret-state">
        {((secretSet ? uiText('A credential is stored. Enter a value only to replace it. ') : uiText('No credential is stored. ')))}{uiText("Clear now is immediate and separate from Save.")}{' '}
      </span>)}
      {uiText(f.help)}
    </div>}
    {f.technicalHelp && <details className="sf-details"><summary>{uiText("Technical details")}</summary>
      <div className="sf-help">{uiText(f.technicalHelp)}</div>
    </details>}
    {f.warning && <div id={warningId} className={`sf-warning${f.warningTone === 'info' ? ' info' : ''}`} role="note">
      <strong>{uiText(f.warningTitle || 'High-risk experimental setting.')}</strong> {uiText(f.warning)}
    </div>}
  </div>
}

function GroupPanel({ group, idPrefix, form, onChange, dirty, unsaved, errors, agentControl,
                      onToggleAgent, secretState, credential, onClearSecret, secretActionDisabled, readOnlyKeys,
                      panelId, labelledBy, searchable, rolePills, interactionDisabled, compact }) {
  useUILanguage()

  const headingId = `${idPrefix}-heading-${safeId(group.title)}`
  return <section className="sf-group" id={panelId}
                  role={labelledBy ? 'tabpanel' : undefined}
                  aria-labelledby={labelledBy || headingId} tabIndex={labelledBy ? 0 : undefined}>
    <div className="sf-group-h">
      {searchable && <h2 id={headingId}>{uiText(group.displayTitle || group.title)}</h2>}
      {group.sub && <span className="muted">{uiText(group.sub)}</span>}
    </div>
    <div className="sf-grid">
      {group.fields.map(f => <Field key={f.key} idPrefix={idPrefix} f={f} value={form[f.key]}
        changed={dirty?.has(f.key)} unsaved={unsaved?.has(f.key)} error={errors?.[f.key]} onChange={onChange}
        granted={agentControl?.[f.key]} onToggleAgent={onToggleAgent}
        secretSet={secretState?.[f.key]} credential={f.type === 'secret' ? credential : null}
        onClearSecret={onClearSecret}
        secretActionDisabled={secretActionDisabled}
        readOnly={readOnlyKeys?.has(f.key)} rolePills={rolePills}
        interactionDisabled={interactionDisabled} compact={compact} />)}
    </div>
  </section>
}

export default function SettingsForm({ form, onChange, dirty, unsaved, errors, only, agentControl, onToggleAgent,
                                       secretState, credential, onClearSecret, secretActionDisabled, readOnlyKeys, hideSecret,
                                       mode = 'all', query = '', schema, initialGroup = '',
                                       focusKey = '', focusRequest = 0, interactionDisabled = false }) {
  useUILanguage()

  const groups = filterSettingsGroups(schema.groups, { mode, query, only, hideSecret })
  const rolePills = schema.agentRolePills
  // Keep the selected section by stable identity. The Essential catalogue is a sparse subset of
  // All, so retaining a numeric index silently selected a different section when modes changed.
  const [activeGroup, setActiveGroup] = useState(initialGroup)
  const reactId = useId()
  const idPrefix = `sf-${safeId(reactId)}`
  const searching = !!normalizeSettingsQuery(query)
  const selectedIndex = groups.findIndex(item => item.title === activeGroup)
  const idx = selectedIndex >= 0 ? selectedIndex : 0
  const group = groups[idx]
  const handledFocusRef = useRef('')
  const tablistRef = useRef(null)
  const groupUnsaved = gr => gr.fields.some(f => unsaved?.has(f.key))
  const groupChanged = gr => gr.fields.some(f => dirty?.has(f.key))

  useEffect(() => {
    if (!focusKey || !Object.hasOwn(schema.fieldByKey, focusKey)) return undefined
    const focusCommand = `${focusRequest}:${focusKey}`
    if (handledFocusRef.current === focusCommand) return undefined
    const groupIndex = groups.findIndex(item => item.fields.some(field => field.key === focusKey))
    if (groupIndex < 0) return undefined
    if (!searching && idx !== groupIndex) {
      setActiveGroup(groups[groupIndex].title)
      return undefined
    }
    const timer = setTimeout(() => {
      const target = document.querySelector(
        `[data-settings-form="${idPrefix}"] [name="${focusKey}"]`,
      )
      if (!target) return
      target.focus()
      // A review request is a one-shot focus command. Remember it only after the target exists so
      // the tab-switch render above can finish first, but never replay it on ordinary navigation.
      handledFocusRef.current = focusCommand
    }, 0)
    return () => clearTimeout(timer)
  }, [focusKey, focusRequest, mode, query, only, hideSecret, schema, searching, idx])

  useEffect(() => {
    if (searching) return
    const tablist = tablistRef.current
    const activeTab = tablist?.querySelector('[aria-selected="true"]')
    if (!tablist || !activeTab) return

    // Keep the selected tab visible without moving the settings page vertically. scrollIntoView()
    // also scrolls ancestor containers, which made the mobile route open halfway down the page.
    const listRect = tablist.getBoundingClientRect()
    const tabRect = activeTab.getBoundingClientRect()
    if (tabRect.left < listRect.left) tablist.scrollLeft -= listRect.left - tabRect.left
    else if (tabRect.right > listRect.right) tablist.scrollLeft += tabRect.right - listRect.right
  }, [idx, searching])

  if (!groups.length) return <div className="settings-empty" role="status">
    <strong>{uiText("No settings match “")}{query.trim()}”</strong>
    <span>{uiText("Try a field name, key, option, or a broader term.")}</span>
  </div>

  if (searching) return <div className="settings-form settings-search-results" role="form"
                              data-settings-form={idPrefix}
                              aria-label={uiText("Matching settings")}>
    {groups.map(gr => <GroupPanel key={gr.title} group={gr} idPrefix={idPrefix} form={form}
      onChange={onChange} dirty={dirty} unsaved={unsaved} errors={errors} agentControl={agentControl}
      onToggleAgent={onToggleAgent} secretState={secretState} credential={credential}
      onClearSecret={onClearSecret}
      secretActionDisabled={secretActionDisabled}
      readOnlyKeys={readOnlyKeys} searchable rolePills={rolePills}
      interactionDisabled={interactionDisabled} />)}
  </div>

  const onTabKeyDown = (event, index) => {
    let next = index
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % groups.length
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + groups.length) % groups.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = groups.length - 1
    else return
    event.preventDefault()
    setActiveGroup(groups[next].title)
    event.currentTarget.parentElement?.querySelectorAll('[role="tab"]')[next]?.focus()
  }

  const tabId = `${idPrefix}-tab-${idx}`
  const panelId = `${idPrefix}-panel-${idx}`
  return <div className="settings-form tabbed" role="form" aria-label={uiText("Settings fields")}
              data-settings-form={idPrefix}>
    <div ref={tablistRef} className="tabs sf-tabs" role="tablist" aria-label={uiText("Settings sections")}>
      {groups.map((gr, index) => <button key={gr.title} type="button" role="tab"
        id={`${idPrefix}-tab-${index}`}
        aria-controls={index === idx ? `${idPrefix}-panel-${index}` : undefined}
        aria-selected={index === idx} tabIndex={index === idx ? 0 : -1}
        className={'tab' + (index === idx ? ' active' : '')}
        onClick={() => setActiveGroup(gr.title)} onKeyDown={event => onTabKeyDown(event, index)}
        title={gr.sub || ''}>
        {uiText(gr.displayTitle || gr.title)}{changeDot(groupUnsaved(gr), groupChanged(gr))}
      </button>)}
    </div>
    <GroupPanel key={group.title} group={group} idPrefix={idPrefix} form={form}
      onChange={onChange} dirty={dirty} unsaved={unsaved} errors={errors} agentControl={agentControl}
      onToggleAgent={onToggleAgent} secretState={secretState} credential={credential}
      onClearSecret={onClearSecret}
      secretActionDisabled={secretActionDisabled}
      readOnlyKeys={readOnlyKeys} panelId={panelId} labelledBy={tabId} rolePills={rolePills}
      interactionDisabled={interactionDisabled} compact={mode === 'essential'} />
  </div>
}
