
import { uiText, useUILanguage } from './uiLanguage.js'
import React, { useEffect, useMemo, useRef, useState } from 'react'

import PanelShell from './PanelShell.jsx'
import { CONTROL, submitCommand } from './api.js'
import {
  INJECT_BLOCKED_REASONS, INJECT_RATIONALE_MAX, buildInjectPayload, injectCandidates,
  injectQuestions, injectSubmitDecision, parseInjectParams, suggestedInjectQuestion,
} from './injectNodeModel.js'

// Add an experiment or an ARTIFACT node to the run by hand (doc 73 §1.4) — the React half of
// `injectNodeModel.js`, which owns every decision this file renders. Here: the form's own state, one
// in-flight submission, and the unmount fence. The engine builds the node from the description (the
// Developer reads it), so the description is the one required field.

const INJECT_COMMAND_WAIT_MS = 12_000

export default function InjectNodePanel({ state, runId, readOnly = false, onToast, onClose }) {
  useUILanguage()
  const [rationale, setRationale] = useState('')
  const [kind, setKind] = useState('experiment')
  const [parentId, setParentId] = useState(null)
  const [uses, setUses] = useState([])
  const [paramsText, setParamsText] = useState('')
  // The question this node answers. `undefined` = the operator has not chosen, so the parent's own
  // question is offered (`suggestedInjectQuestion`); a choice — including "none" (null) — sticks.
  const [chosenQuestion, setChosenQuestion] = useState(undefined)
  const [submitting, setSubmitting] = useState(false)
  const [outcome, setOutcome] = useState(null)    // {kind: 'landed'|'error', text}
  const aliveRef = useRef(true)
  useEffect(() => () => { aliveRef.current = false }, [])

  const candidates = useMemo(() => injectCandidates(state), [state?.nodes])
  const questions = useMemo(() => injectQuestions(state), [state?.cards])
  const suggestion = suggestedInjectQuestion(state, parentId)
  const questionId = chosenQuestion === undefined ? suggestion : chosenQuestion
  const params = parseInjectParams(paramsText)
  const draft = { rationale, kind, uses, parentId, params, questionId }
  const decision = injectSubmitDecision({ state, draft, submitting })
  const blocked = readOnly ? 'This view cannot steer the run.'
    : (decision.ok ? null : (decision.code === 'bad_params' ? params.error
      : INJECT_BLOCKED_REASONS[decision.code]))

  const toggleUse = id => setUses(current => (current.includes(id)
    ? current.filter(x => x !== id) : [...current, id]))

  const submit = async () => {
    if (blocked || submitting) return
    const payload = buildInjectPayload({ state, draft })
    if (!payload) return
    setSubmitting(true)
    setOutcome(null)
    const what = kind === 'artifact' ? uiText('Artifact node') : uiText('Experiment')
    const feedback = await submitCommand(
      CONTROL.injectPayload(runId, payload, { waitMs: INJECT_COMMAND_WAIT_MS }), {
        success: `${what} queued — the engine builds it at its next turn.`,
        executing: `${what} submitted — the engine is creating it.`,
        requested: what,
        failure: `${what} refused`,
      }, onToast)
    if (!aliveRef.current) return
    setSubmitting(false)
    setOutcome({ kind: feedback.kind === 'error' ? 'error' : 'landed', text: feedback.message })
    if (feedback.kind !== 'error') {
      setRationale('')
      setParamsText('')
      setUses([])
      setChosenQuestion(undefined)
    }
  }

  return <PanelShell title={uiText('Add an experiment')} onClose={onClose} wide>
    <p className="muted">{uiText("A node you author: the Developer builds it from your description, and the engine evaluates it like any other. An ARTIFACT node produces files later experiments read (a prepared dataset): it succeeds without a metric and is never ranked. An experiment that uses artifacts gets their directories in LOOPLAB_USES_WORKDIRS.")}</p>
    <div className="sf-field">
      <span className="sf-label">{uiText('Kind')}</span>
      <div className="sf-input" role="radiogroup" aria-label={uiText('Kind')}>
        <label style={{ marginRight: 12 }}>
          <input type="radio" name="inject-kind" checked={kind === 'experiment'}
            disabled={submitting} onChange={() => setKind('experiment')} /> {uiText('Experiment')}
        </label>
        <label>
          <input type="radio" name="inject-kind" checked={kind === 'artifact'}
            disabled={submitting} onChange={() => setKind('artifact')} /> {uiText('Artifact (preparation)')}
        </label>
      </div>
    </div>
    <div className="sf-field">
      <label className="sf-label" htmlFor="inject-rationale">{uiText('Description')}</label>
      <div className="sf-input">
        <textarea id="inject-rationale" className="text" value={rationale} rows={5}
          style={{ minHeight: 110 }} maxLength={INJECT_RATIONALE_MAX} disabled={submitting}
          placeholder={kind === 'artifact'
            ? uiText('What files it writes, and where under its working directory')
            : uiText('What this experiment changes, and why')}
          onChange={event => setRationale(event.target.value)} />
      </div>
    </div>
    <div className="sf-field">
      <label className="sf-label" htmlFor="inject-parent">{uiText('Parent')}</label>
      <div className="sf-input">
        <select id="inject-parent" value={parentId == null ? '' : String(parentId)}
          disabled={submitting}
          onChange={event => setParentId(event.target.value === '' ? null : Number(event.target.value))}>
          <option value="">{uiText('None — start from the task code')}</option>
          {candidates.parents.map(p => <option key={p.id} value={String(p.id)}>
            #{p.id}{p.label ? ` — ${p.label}` : ''}</option>)}
        </select>
      </div>
    </div>
    <div className="sf-field">
      <label className="sf-label" htmlFor="inject-question">{uiText('Answers question')}</label>
      <div className="sf-input">
        <select id="inject-question" value={questionId ?? ''} disabled={submitting}
          onChange={event => setChosenQuestion(event.target.value === '' ? null : event.target.value)}>
          <option value="">{uiText('None — not filed under a research question')}</option>
          {questions.map(q => <option key={q.id} value={q.id}>{uiText(q.label)}</option>)}
        </select>
        {suggestion && chosenQuestion === undefined && <div className="muted">
          {uiText("Suggested: the question its parent's experiment is filed under.")}</div>}
        {questions.length === 0 && <div className="muted">
          {uiText('No research question is on the board yet.')}</div>}
      </div>
    </div>
    <div className="sf-field">
      <span className="sf-label">{uiText('Uses artifacts')}</span>
      <div className="sf-input">
        {candidates.artifacts.length === 0
          ? <span className="muted">{uiText('No artifact node has been produced in this run yet.')}</span>
          : candidates.artifacts.map(a => <label key={a.id} style={{ display: 'block' }}>
            <input type="checkbox" checked={uses.includes(a.id)} disabled={submitting}
              onChange={() => toggleUse(a.id)} /> #{a.id}{a.label ? ` — ${a.label}` : ''}
          </label>)}
      </div>
    </div>
    <div className="sf-field">
      <label className="sf-label" htmlFor="inject-params">{uiText('Parameters (JSON object, or empty)')}</label>
      <div className="sf-input">
        <textarea id="inject-params" className="text" value={paramsText} rows={3}
          disabled={submitting} aria-invalid={params.ok ? undefined : true}
          onChange={event => setParamsText(event.target.value)} />
      </div>
      {!params.ok && <div className="muted" role="alert">{uiText(params.error)}</div>}
    </div>
    {outcome && <div className="notice" role={outcome.kind === 'error' ? 'alert' : 'status'}>
      {uiText(outcome.text)}</div>}
    <div className="row" style={{ display: 'flex', gap: 8, marginTop: 12 }}>
      <button type="button" className="btn primary" disabled={!!blocked || submitting}
        title={blocked ? uiText(blocked) : undefined} onClick={submit}>
        {submitting ? uiText('Adding…') : uiText('Add to the run')}
      </button>
      <button type="button" className="btn ghost" onClick={onClose}>{uiText('Close')}</button>
      {blocked && !submitting && <span className="muted" role="status">{uiText(blocked)}</span>}
    </div>
  </PanelShell>
}
