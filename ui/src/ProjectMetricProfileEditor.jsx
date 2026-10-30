import { uiMessage } from './uiLanguage.js'
import React, { useState } from 'react'
import { putProjectMetricProfile } from './api.js'
import { uiText, useUILanguage } from './uiLanguage.js'

const blankMetric = () => ({ key: '', label: '', role: 'primary', unit: '', denominator: '' })

export default function ProjectMetricProfileEditor({ context, onSaved }) {
  useUILanguage()
  const initial = context.profile || { name: '', metrics: [blankMetric()], slices: [], notes: '' }
  const [name, setName] = useState(initial.name), [metrics, setMetrics] = useState(initial.metrics)
  const [slices, setSlices] = useState(initial.slices.join(', ')), [notes, setNotes] = useState(initial.notes)
  const [authoringLanguage, setAuthoringLanguage] = useState(initial.authoring_language || 'auto')
  const [destination, setDestination] = useState(initial.publication?.destination_label || '')
  const [status, setStatus] = useState('idle')
  const update = (index, field, value) => setMetrics(rows => rows.map((row, i) => i === index ? { ...row, [field]: value } : row))
  const valid = name.trim() && metrics.length && metrics.every(m => m.key.trim()) && metrics.filter(m => m.role === 'primary').length === 1
    && new Set(metrics.map(m => m.key.trim())).size === metrics.length
  const save = async () => {
    setStatus('saving')
    try {
      const result = await putProjectMetricProfile(context.project_id, {
        expected_digest: context.digest, profile: { name, metrics, slices: slices.split(',').map(s => s.trim()).filter(Boolean), notes, authoring_language: authoringLanguage, ...(destination.trim() ? { publication: { adapter: 'manual_markdown', destination_label: destination.trim() } } : {}) },
      })
      onSaved(result); setStatus('saved')
    } catch (error) { setStatus(error?.status === 409 ? 'conflict' : 'unknown') }
  }
  return <details><summary>{uiText('Edit project metric profile')}</summary>
    <p>{uiText('This owner setting changes presentation preferences only; it does not change an experiment protocol.')}</p>
    <label>{uiText('Profile name')}<input maxLength={160} value={name} onChange={e => setName(e.target.value)} /></label>
    {metrics.map((metric, index) => <fieldset key={index}>
      <legend>{uiText('Metric')} {index + 1}</legend>
      <label>{uiText('Metric key')}<input maxLength={200} value={metric.key} onChange={e => update(index, 'key', e.target.value)} /></label>
      <label>{uiText('Display name')}<input maxLength={160} value={metric.label} onChange={e => update(index, 'label', e.target.value)} /></label>
      <label>{uiText('Role')}<select value={metric.role} onChange={e => update(index, 'role', e.target.value)}>
        <option value="primary">{uiText('Primary metric')}</option><option value="secondary">{uiText('Secondary metric')}</option>
        <option value="transport">{uiText('Transport metric')}</option></select></label>
      <label>{uiText('Unit')}<input maxLength={100} value={metric.unit} onChange={e => update(index, 'unit', e.target.value)} /></label>
      <label>{uiText('Denominator')}<input maxLength={300} value={metric.denominator} onChange={e => update(index, 'denominator', e.target.value)} /></label>
      <label>{uiText('Additive stack group (optional)')}<input maxLength={160} value={metric.stack_group || ''} onChange={e => update(index, 'stack_group', e.target.value)} /></label>
      <button type="button" disabled={metrics.length === 1} onClick={() => setMetrics(rows => rows.filter((_, i) => i !== index))}>{uiText('Remove metric')}</button>
    </fieldset>)}
    <button type="button" disabled={metrics.length >= 32} onClick={() => setMetrics(rows => [...rows, { ...blankMetric(), role: 'secondary' }])}>{uiText('Add metric')}</button>
    <label>{uiText('Requested slices, separated by commas')}<input maxLength={4000} value={slices} onChange={e => setSlices(e.target.value)} /></label>
    <label>{uiText('Project authoring language')}<select value={authoringLanguage} onChange={e => setAuthoringLanguage(e.target.value)}><option value="auto">{uiText('Follow task')}</option><option value="ru">Русский</option><option value="en">{uiText("English")}</option></select></label>
    <label>{uiText('Manual publication destination label (optional)')}<input maxLength={120} value={destination} onChange={e => setDestination(e.target.value)} /></label>
    <p>{uiText('Manual export does not send or publish to an external service.')}</p>
    <label>{uiText('Notes')}<textarea maxLength={1000} value={notes} onChange={e => setNotes(e.target.value)} /></label>
    {!valid && <p>{uiText('Use distinct metric keys and choose exactly one primary metric.')}</p>}
    <button type="button" onClick={save} disabled={!valid || status === 'saving' || status === 'conflict'}>{uiText('Save profile')}</button>
    {status === 'saved' && <p role="status">{uiText('Profile saved.')}</p>}
    {status === 'conflict' && <p role="status">{uiText('The profile changed elsewhere. Your draft remains here; refresh and reconcile before saving.')}</p>}
    {status === 'unknown' && <p role="status">{uiText('Save outcome is unknown. Read the current profile before retrying.')}</p>}
  </details>
}
