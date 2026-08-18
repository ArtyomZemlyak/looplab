import React, { useEffect, useMemo, useRef, useState } from 'react'

import PanelShell from './PanelShell.jsx'
import { CONTROL, commandFeedback } from './util.js'
import {
  FORK_ACCESS_REASONS, FORK_BLOCKED_REASONS, buildForkPayload, classifyForkFailure,
  forkIdeaFromSnapshot, forkLandedNotice, forkRetryable, forkSubmitDecision,
} from './forkFromSeqModel.js'

// Branch from the experiment you are READING, with its idea edited — the React half of
// `forkFromSeqModel.js`, which owns every decision this file merely renders. What lives here is the
// choreography the model must not have: the form's own state, the JSON draft the operator is
// mid-way through typing, one in-flight submission, the unmount fence, and the three different
// things the panel becomes depending on how a submission ended.
//
// It is a PANEL and not an Inspector tab on purpose. The gesture is only reachable while a
// historical snapshot is on screen, where the Inspector is already narrowed to
// `READ_ONLY_INSPECT_TABS`; putting a mutation inside a tab strip whose whole point is that it
// cannot mutate would be the confusing place for it. `?panel=fork` is also addressable, so the form
// survives a reload with its subject (the route's node id) intact.

// A branch takes an operator's whole afternoon of reading to compose and one keystroke to lose, so
// the submission is given more room than the 8 s a click-and-forget control gets: the durable
// command lifecycle answers as soon as the record exists, but a busy server on a geesefs mount can
// take seconds to get there. This is the WAIT for the record to reach a terminal status, not a
// request timeout — `runCommand` keeps its own per-request one.
const FORK_COMMAND_WAIT_MS = 12_000

const prettyJson = value => {
  try { return JSON.stringify(value ?? {}, null, 2) } catch { return '' }
}

/**
 * The parameters the operator typed, or the reason they are not usable yet.
 *
 * An empty box means "this branch carries no params", NOT "params: {}" — those are different ideas
 * and the second one would count as an edit the operator never made (`forkIdeaEdited` compares the
 * serialized field). Only a JSON OBJECT is accepted: `params` is a mapping everywhere else in the
 * system, and a bare `4` or `[1,2]` would be refused by the server after the operator had already
 * been told the form was fine.
 */
export function parseForkParams(text) {
  if (!String(text ?? '').trim()) return { ok: true, value: undefined }
  let parsed
  try { parsed = JSON.parse(text) } catch (error) {
    return { ok: false, error: `Parameters must be valid JSON: ${error.message}` }
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, error: 'Parameters must be a JSON object, for example {"lr": 0.001}.' }
  }
  return { ok: true, value: parsed }
}

export default function ForkFromSeqPanel({
  runId, node = null, viewSeq = null, expectedGeneration = null, access = null,
  liveNodes = null, onToast, onClose, onOpenLive,
}) {
  // The snapshot's idea, narrowed by the model. Keyed on the node's identity rather than the object
  // so a poll that re-materializes an equal snapshot cannot silently reset a half-typed form.
  const base = useMemo(() => forkIdeaFromSnapshot(node),
    [node?.id, node?.attempt, JSON.stringify(node?.idea ?? null)])
  const [operator, setOperator] = useState(() => base.operator || 'manual')
  const [rationale, setRationale] = useState(() => base.rationale || '')
  const [paramsText, setParamsText] = useState(() => base.params === undefined ? '' : prettyJson(base.params))
  const [submitting, setSubmitting] = useState(false)
  const [outcome, setOutcome] = useState(null)   // {kind, text, failure?, landed?}
  const aliveRef = useRef(true)
  const noticeRef = useRef(null)
  useEffect(() => () => { aliveRef.current = false }, [])
  // Re-seed when the SUBJECT changes (the operator closed this and opened another node's branch
  // through the same route). Not on every `base` identity: that is the poll case above.
  useEffect(() => {
    setOperator(base.operator || 'manual')
    setRationale(base.rationale || '')
    setParamsText(base.params === undefined ? '' : prettyJson(base.params))
    setOutcome(null)
  }, [node?.id, node?.attempt])
  // An outcome is the most important thing on the panel and a keyboard operator is standing on the
  // submit button when it appears; move them to it rather than announcing into an unfocused region.
  useEffect(() => {
    if (!outcome) return
    const frame = requestAnimationFrame(() => noticeRef.current?.focus({ preventScroll: true }))
    return () => cancelAnimationFrame(frame)
  }, [outcome])

  const params = parseForkParams(paramsText)
  // REVIEW 2026-08-18 (correctness): the draft ALWAYS carries a `rationale` key (seeded '' above
  // when the idea has none), while `forkIdeaFromSnapshot` OMITS absent fields from `base` — so for
  // a rationale-less snapshot idea `forkIdeaEdited` compares '""' against "null" and reports the
  // untouched form as edited (driven: base {operator,params} vs draft {…, rationale: ''} →
  // edited=true, decision ok). The `unedited: "an unchanged copy is not a new experiment"` refusal
  // never fires, the submit is live on a byte-for-byte copy, and the server stamps
  // `changed_fields: ['rationale']` for a field the operator never touched. Fix direction: drop
  // `rationale` from the draft when it is '' and `base` has none (mirroring how empty `params` is
  // deleted two lines down), or seed `base.rationale` to '' in the model.
  const draft = useMemo(() => {
    const next = { ...base, operator: operator.trim() || 'manual', rationale }
    if (params.ok && params.value !== undefined) next.params = params.value
    else delete next.params
    return next
  }, [base, operator, rationale, params.ok, paramsText])

  // The form is FENCED once a submission ended in a way that resubmitting these same bytes cannot
  // improve — the run moved, or the outcome is unknown. `forkRetryable` is that rule; the panel only
  // renders it.
  const fenced = !!outcome && (outcome.kind === 'landed' || !forkRetryable(outcome.failure))
  const decision = forkSubmitDecision({ node, viewSeq, base, draft, submitting })
  const accessRefusal = access && !access.ok ? FORK_ACCESS_REASONS[access.reason] : null
  const blocked = accessRefusal
    || (params.ok ? null : params.error)
    || (decision.ok ? null : FORK_BLOCKED_REASONS[decision.reason])

  const submit = async () => {
    if (fenced || submitting) return
    const payload = buildForkPayload({ node, viewSeq, base, draft })
    if (!payload) {
      // Unreachable while the button is gated, and deliberately not trusted to be: `buildForkPayload`
      // re-asks `forkSubmitDecision` itself, so a future render path that forgets to gate refuses
      // here instead of posting a payload the model declined to build.
      setOutcome({ kind: 'error', text: blocked || FORK_BLOCKED_REASONS[decision.reason] })
      return
    }
    setSubmitting(true)
    setOutcome(null)
    let record = null
    let thrown = null
    try {
      record = await CONTROL.forkFrom(runId, payload, {
        expectedGeneration, waitMs: FORK_COMMAND_WAIT_MS,
      })
    } catch (error) { thrown = error }
    if (!aliveRef.current) return
    setSubmitting(false)
    if (thrown) {
      const failure = classifyForkFailure(thrown)
      setOutcome({ kind: 'error', failure, text: failure.message })
      return
    }
    const feedback = commandFeedback(record, {
      success: `Branched from experiment #${node.id} as it was at seq ${viewSeq}.`,
      noop: `That branch already exists on experiment #${node.id}.`,
      executing: `Branch from experiment #${node.id} submitted — the engine is creating it.`,
      failure: 'Branch refused',
    })
    if (feedback.kind === 'error') {
      // THE ASYMMETRY THIS PANEL EXISTS TO READ. The legacy `/control` route answers a stale parent
      // with a 409, i.e. the `thrown` arm above; the durable `/commands` route answers 200 with a
      // REJECTED RECORD carrying the same refusal in `record.error`. Both are proof that the intake
      // refused before anything was appended, and `classifyForkFailure` is what knows that — reading
      // `feedback.message` here instead would turn "the run moved under you, go and re-read the
      // node" into "the outcome is unknown, your branch may already exist".
      const failure = classifyForkFailure(record)
      setOutcome({ kind: 'error', failure, text: failure.message })
      return
    }
    const landed = forkLandedNotice({ node, live: liveNodes, viewSeq })
    setOutcome({
      kind: 'landed',
      text: [feedback.message, landed].filter(Boolean).join(' '),
      pending: feedback.kind === 'pending',
    })
    onToast?.(feedback.message)
  }

  const title = 'Branch from this snapshot'
  const subject = node?.id == null ? null : `experiment #${node.id} · attempt ${node.attempt ?? 0}`
  return <PanelShell title={title} sub={subject || undefined} onClose={onClose} wide>
    <p className="muted">
      A branch is a NEW experiment whose parent is the one you are reading and whose idea is the one
      you edit here. It is fenced by CONTENT, not by the point in the timeline you are standing at:
      it is accepted at the live tail as long as {subject ? `#${node.id}` : 'that experiment'} is
      still the attempt shown above, and refused outright if it has been re-run since.
    </p>
    {accessRefusal
      ? <div className="notice" role="status">{accessRefusal}</div>
      : node?.id == null
        ? <div className="notice" role="status">{FORK_BLOCKED_REASONS.no_node}</div>
        : <>
          <div className="sf-field">
            <label className="sf-label" htmlFor="fork-operator">Operator</label>
            <div className="sf-input">
              <input id="fork-operator" className="text" value={operator} maxLength={120}
                disabled={fenced || submitting}
                onChange={event => setOperator(event.target.value)} />
            </div>
          </div>
          <div className="sf-field">
            <label className="sf-label" htmlFor="fork-rationale">Rationale</label>
            <div className="sf-input">
              <textarea id="fork-rationale" className="text" value={rationale} rows={4}
                style={{ minHeight: 90 }} maxLength={20_000} disabled={fenced || submitting}
                onChange={event => setRationale(event.target.value)} />
            </div>
          </div>
          <div className="sf-field">
            <label className="sf-label" htmlFor="fork-params">Parameters (JSON object, or empty)</label>
            <div className="sf-input">
              <textarea id="fork-params" className="text" value={paramsText} rows={8}
                style={{ minHeight: 140 }} disabled={fenced || submitting}
                aria-invalid={params.ok ? undefined : true}
                aria-describedby={params.ok ? undefined : 'fork-params-error'}
                onChange={event => setParamsText(event.target.value)} />
            </div>
            {!params.ok && <div id="fork-params-error" className="muted" role="alert">{params.error}</div>}
          </div>
          {/* The idea's remaining substance rides along unchanged and is stated rather than shown as
              a form: `eval_profile`, `eval_timeout` and `space` are carried, and the concept
              envelope, the Card, the hypothesis and the footprint deliberately are NOT (the reason
              is in FORK_IDEA_FIELDS). An operator who cannot see which is which will assume the
              branch is a copy. */}
          <p className="muted">
            The evaluation profile, timeout and search space are carried over unchanged. Concept
            tags, the Card, the hypothesis and the footprint are not: a branch you authored is not
            inside the Researcher's Card budget and carries no taxonomy you did not write.
          </p>
          {outcome && <div ref={noticeRef} tabIndex={-1} className="notice"
            role={outcome.kind === 'landed' ? 'status' : 'alert'}>
            {outcome.text}
            {/* The only affordance a MOVED refusal may offer. Retrying these bytes is refused
                identically every time — what helps is reading the parent as it is now, which is a
                different address, so this is a navigation and not a retry button. */}
            {outcome.failure?.moved && onOpenLive && <div style={{ marginTop: 8 }}>
              <button type="button" className="btn sm primary"
                onClick={() => onOpenLive(outcome.failure.nodeId ?? node.id)}>
                Return to live and re-read #{outcome.failure.nodeId ?? node.id}</button>
            </div>}
            {outcome.kind === 'landed' && onOpenLive && <div style={{ marginTop: 8 }}>
              <button type="button" className="btn sm primary" onClick={() => onOpenLive(null)}>
                Return to live</button>
            </div>}
          </div>}
          <div className="row" style={{ display: 'flex', gap: 8, marginTop: 12 }}>
            <button type="button" className="btn primary"
              disabled={!!blocked || fenced || submitting}
              title={blocked || undefined} onClick={submit}>
              {submitting ? 'Branching…' : 'Create branch'}
            </button>
            <button type="button" className="btn ghost" onClick={onClose}>
              {fenced ? 'Close' : 'Cancel'}
            </button>
            {/* A disabled button explains nothing, and the commonest reason to be here is the one
                worth saying out loud: an unedited copy is not a new experiment. */}
            {blocked && !fenced && <span className="muted" role="status">{blocked}</span>}
          </div>
        </>}
  </PanelShell>
}
