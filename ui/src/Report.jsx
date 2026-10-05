import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import React, { useEffect, useMemo, useRef, useState } from 'react'
import BaseRevision from './BaseRevision.jsx'
import UpstreamPanel from './UpstreamPanel.jsx'
import { peekReportRefreshIntent, reportRefreshIntent, isTransientCommandReadError, deadlineGet, fmt,
  fmtCost, fmtInt, CONTROL, runNodeApiPath } from './util.js'
import { Trajectory, ImprovementWaterfall } from './charts.jsx'
import { analyze, buildModelCard, verdict, paramDiffLabel, toMarkdown, hyperImportance, reportOutcomeEvidence } from './report.js'
import ReportOutcomes from './ReportOutcomes.jsx'
import Markdown from './markdown.jsx'
import { OpIcon } from './icons.jsx'
import { OBJECTIVE_SOURCE_LABEL, objectiveMetricSource, objectiveSourceCaveated,
  objectiveSourceHelp, reportStepIdentity } from './trustSemantics.js'
import { DataTable, downloadBlob } from './accessibility.jsx'
import { normalizeReportNodeDetail, normalizeRunReport, reportCoverageText,
  reportNarrativeCoverage } from './reportModel.js'
import { nodeTheme } from './conceptId.js'
import { nodeIsActive } from './nodeProjection.js'
import { readOnlyLabel } from './runMode.js'
import { resultMeasurement } from './resultMeasurement.js'
import './report-trust-polish.css'

const TRUST_CLASS = { unverified: 'neutral', caveats: 'warn', suspect: 'alarm' }
const TRUST_LABEL = { unverified: 'not fully verified', caveats: 'with caveats', suspect: 'flags found' }
const SOLUTION_DETAIL_TIMEOUT_MS = 12_000
const RUN_GENERATION_RE = /^[0-9a-f]{64}$/
const OUTCOME_LABEL = { improved: 'better evaluation score', flat: 'same evaluation score',
  regressed: 'worse evaluation score', baseline: 'first eligible result',
  uncompared: 'comparison not established', none: 'no selected result' }

export const reportRefreshFailure = (failure, thrown = false) => {
  const code = failure?.code
  if (code === 'run_generation_changed' || code === 'run_generation_unavailable') {
    return ['The run changed during report generation. Reload it.', false]
  }
  if ([401, 403, 404].includes(Number(failure?.status))) {
    return ['Report refresh is unavailable in this run or session. Reload.', false, true]
  }
  if (code === 'job_unknown') {
    return ['Report receipt expired. Retry checks the same paid request.', true, true]
  }
  if (code === 'job_capacity') {
    return ['The report service is busy. Retry shortly.', true]
  }
  if (code === 'report_refresh_in_progress') {
    return ['Another paid report refresh owns this run. Wait for it to finish, then reload.', false]
  }
  if (code === 'report_refresh_uncertain') {
    return ['Outcome unknown. Resume rechecks the saved paid request; never start another. A crashed worker may need operator recovery.', true, true]
  }
  if (code === 'REPORT_REFRESH_PROTOCOL_ERROR' || code === 'job_protocol_error') {
    return ['Receipt invalid. Resume rechecks the saved paid request; completion remains unknown.', true, true]
  }
  const status = Number(failure?.status)
  const transientThrow = thrown && (failure?.submissionMayHaveSucceeded === true
    || isTransientCommandReadError(failure))
  if (failure?.ambiguous === true || transientThrow) {
    return ['Report-job connection lost. Retry resumes the same paid job.', true, true]
  }
  if (thrown && status >= 400 && status < 500) {
    return ['The report request was rejected. Reload before retrying.', false]
  }
  const kind = failure?.error_kind
  if (kind === 'credentials') return ['Check report-provider credentials in Settings.', true]
  if (kind === 'rate_limit') return ['The report provider is busy. Retry shortly.', true]
  if (kind === 'accounting_pending') {
    return ['Durable cost accounting is pending. Retry after storage recovers.', true]
  }
  return ['Report generation failed. Check provider settings and retry.', true]
}

// The authority banner is deterministic. Provider prose is rendered only in AgentNarrative below.
function VerdictBanner({ v, onOpenPanel, canOpenPanel }) {
  useUILanguage()

  const cls = TRUST_CLASS[v.trust] || 'warn'
  const canOpen = panel => !!onOpenPanel && canOpenPanel?.(panel) !== false
  return (
    <section className={'verdict-banner ' + cls} aria-labelledby="report-verdict-heading">
      <div className="verdict-row">
        <span className={'verdict-pill ' + (v.outcome === 'improved' ? 'ok' : v.outcome === 'regressed' ? 'fail' : '')}>{uiText(OUTCOME_LABEL[v.outcome] || v.outcome)}</span>
        {v.robustness && v.robustness !== 'n/a' && <span className="pill">{uiText(v.robustness)}</span>}
        <span className="pill verdict-trust-label">{uiText(TRUST_LABEL[v.trust] || v.trust)}</span>
      </div>
      <h2 id="report-verdict-heading" className="verdict-headline">{uiText(v.headline)}</h2>
      <p className="report-next-step"><strong>{uiText("Next step")}</strong> {uiText(v.nextStep)}</p>
      {v.caveats.length > 0 && <div className="caveat-chips">
        {v.caveats.map((c, i) => {
          const openable = canOpen(c.panel)
          return <button key={i} className={'caveat-chip ' + c.severity}
            disabled={!openable} title={((openable ? uiMessage("see {0} →", [c.panel]) : uiText('Unavailable in this read-only view')))}
            onClick={event => { if (canOpen(c.panel)) onOpenPanel(c.panel, event.currentTarget) }}>
            <OpIcon name="alert" size={11} /> {uiText(c.text)}
          </button>
        })}
      </div>}
    </section>
  )
}

function AgentNarrative({ rep, coverage, generation, snapshotSeq }) {
  useUILanguage()

  if (!rep) return null
  const publishedIso = rep.published_at == null ? null : new Date(rep.published_at * 1000).toISOString()
  const warning = coverage.status === 'stale'
    ? 'This narrative predates visible nodes. Refresh it before relying on its claims.'
    : coverage.status === 'inconsistent'
      ? 'The report watermark is ahead of this view. Treat the narrative as stale and verify the run generation.'
      : coverage.status === 'unknown'
        ? 'The publication did not record a valid node watermark. Its coverage cannot be established.'
        : ''
  const groups = [['What worked', rep.what_worked], ["What didn't work", rep.what_didnt],
    ['Learnings', rep.learnings], ['Next directions', rep.next_directions]]
  return <section className={`agent-report ${coverage.status}`} role="note"
    aria-labelledby="agent-report-heading">
    <div className="agent-report-head">
      <h2 id="agent-report-heading" tabIndex={-1}>{uiText("Agent narrative")}</h2>
      <span className="pill">{uiText("advisory · not deterministic")}</span>
    </div>
    <p className="report-section-intro">{uiText('The assistant explains the recorded results. These interpretations may be wrong; the measured verdict and comparison coverage above take precedence.')}</p>
    <span className={`report-coverage ${coverage.status}`}>{uiText(reportCoverageText(coverage))}</span>
    <details className="report-publication-details"><summary>{uiText('Publication details')}</summary>
    <div className="report-provenance" aria-label={uiText("Agent narrative publication provenance")}>
      {rep.published_seq != null && <span>{uiText("published event ")}<b>#{rep.published_seq}</b></span>}
      {publishedIso && <span>{uiText("at ")}<time dateTime={publishedIso}>{new Date(rep.published_at * 1000).toLocaleString()}</time></span>}
      {rep.trigger && <span>{uiText("trigger ")}<b>{rep.trigger}</b></span>}
      {Number.isSafeInteger(snapshotSeq) && snapshotSeq >= 0 && <span>{uiText("view snapshot ")}<b>#{snapshotSeq}</b></span>}
      {/^[0-9a-f]{64}$/.test(generation || '') && <span>{uiText("generation ")}<code title={generation}>{generation.slice(0, 12)}…</code></span>}
    </div>
    </details>
    {warning && <div className="report-coverage-warning" role="status"><OpIcon name="alert" size={13} /> {uiText(warning)}</div>}
    {rep.headline && <h3 className="agent-report-headline">{rep.headline}</h3>}
    {(rep.verdict || rep.summary) && <div className="agent-report-text"><Markdown text={rep.verdict || rep.summary} /></div>}
    <div className="agent-report-columns">{groups.map(([label, items]) => <div className="agent-report-group" key={label}>
      <h3>{uiText(label)}</h3>{items?.length ? <List items={items} /> : <p className="muted">{uiText('No interpretation was published for this section.')}</p>}
    </div>)}</div>
    {rep.caveats.length > 0 && <div className="agent-report-caveats">
      <div className="agent-report-caveats-title"><OpIcon name="alert" size={12} />
        <strong>{uiText("Agent caveats")}</strong><span className="muted">{uiText("advisory narrative")}</span></div>
      <List items={rep.caveats} />
    </div>}
    {rep.champion_summary && <div className="agent-report-group">
      <h3>{uiText("Champion note at publication")}</h3><Markdown text={rep.champion_summary} />
    </div>}

  </section>
}

function ChampionCard({ best, state }) {
  useUILanguage()

  if (!best) return null
  const m = best.confirmed_mean ?? best.metric
  const direction = nodeTheme(best, state)
  const params = Object.entries(best.idea?.params || {})
  // The champion card is the report's own statement of the result, and it printed the number bare —
  // the fifth surface of the salvage/subject vocabulary, and the one where the number IS the claim.
  // Same call, same label, same sentence as the Metrics tab, the Pareto front and the cross-run
  // champion row (`panels.jsx::CrossRunPanel`), so the run's headline result cannot read as measured
  // on the page whose whole subject is that result while reading `salvaged` one tab over.
  //
  // The `feasible` row below is deliberately UNTOUCHED. Under `metric_salvage: "select"` the engine
  // really does admit this node — `feasible = not violations`, and that rung mints no row — so `yes`
  // is the engine's own answer to the question that row asks. `trustSemantics.js` states the split:
  // feasibility asks "why is this node excluded", the objective source asks "what is this number",
  // and a node can honestly be `feasible` and `salvaged` at once. Softening the feasibility word
  // here would put this card back in disagreement with the Trust tab, one vocabulary over.
  const objective = objectiveMetricSource(best)
  const objectiveCaveated = objectiveSourceCaveated(objective)
  const measurement = resultMeasurement(best.confirmed_mean != null, best.confirmed_seeds)
  return (
    <div className="champion-card">
      <div className="kv">
        <div className="k">{uiText("selected")}</div><div className="v">#{best.id} · {best.operator}
          {(direction ? uiMessage(" · primary concept axis {0}", [direction]) : '')}</div>
        <div className="k">{uiText(measurement.label)}</div><div className="v"><b>{fmt(m)}</b>{best.confirmed_mean != null
          ? <span className="muted">{((Number.isFinite(best.confirmed_std) && best.confirmed_std >= 0 ? ` ±${fmt(best.confirmed_std)}` : uiText(' · spread not recorded')))}</span>
          : null}
          {objectiveCaveated && <span className="warn" title={objectiveSourceHelp(objective)}>
            {' · '}{OBJECTIVE_SOURCE_LABEL[objective.channel]}</span>}</div>
        {best.confirmed_mean != null && <><div className="k">{uiText("evaluation score")}</div><div className="v">{fmt(best.metric)}</div></>}
        <div className="k">{uiText("repeat evidence")}</div><div className="v">{uiText(measurement.reliability)}</div>
        <div className="k">{uiText("params")}</div><div className="v">{params.length
          ? <details className="champion-params">
              <summary>{params.length}{uiText(" parameters · ")}<span className="champion-params-show">{uiText("show values")}</span><span className="champion-params-hide">{uiText("hide values")}</span></summary>
              <dl className="champion-params-list">{params.map(([key, value]) => <React.Fragment key={key}>
                <dt>{key}</dt><dd>{fmt(value)}</dd>
              </React.Fragment>)}</dl>
            </details> : '—'}</div>
        {(best.parent_ids || []).length > 0 && <><div className="k">{uiText("lineage")}</div><div className="v">{best.parent_ids.map(p => '#' + p).join(' → ')}</div></>}
        <div className="k">{uiText("feasible")}</div><div className="v">{((best.feasible === true ? uiText('yes') : (best.feasible === false ? uiText('no — constraint violated') : uiText('unknown — not established'))))}</div>
      </div>
    </div>
  )
}

function List({ items }) {
  useUILanguage()

  if (!items || !items.length) return null
  return <ul className="bul">{items.map((x, i) => <li key={i}><Markdown text={x} externalOnly /></li>)}</ul>
}

export default function ReportView({ state, runId, onOpenPanel, canOpenPanel, onToast,
  onPickNode, onPickEvidence, readOnly = false,
  historySeq = null, expectedGeneration = null, observedSeq = null,
  readOnlyReason = 'history', evidenceAvailable = true }) {
  const [, , localeRevision] = useUILanguage()

  const failed = Object.values(state.nodes).filter(n => nodeIsActive(n, state) && n.status === 'failed')
  const a = useMemo(() => analyze(state), [state])
  const outcomes = useMemo(() => reportOutcomeEvidence(state), [state])
  const v = useMemo(() => verdict(state, a), [state, a, localeRevision])
  const best = v.best
  const rep = useMemo(() => normalizeRunReport(state.report), [state.report])
  const nodeCount = Object.keys(state.nodes).length
  const coverage = useMemo(() => reportNarrativeCoverage(rep, nodeCount), [rep, nodeCount])
  const imp = useMemo(() => hyperImportance(state).slice(0, 6), [state])
  const solutionRunReady = typeof runId === 'string' && runId.length > 0 && state.run_id === runId
  const solutionGeneration = RUN_GENERATION_RE.test(expectedGeneration || '')
    ? expectedGeneration : null
  const solutionSeq = Number.isSafeInteger(observedSeq) && observedSeq >= 0 ? observedSeq : null
  const solutionHistoryAligned = !(readOnly && historySeq != null) || historySeq === solutionSeq
  const solutionIdentityReady = solutionRunReady && solutionGeneration != null
    && solutionSeq != null && solutionHistoryAligned
  // Scope the async source projection to the exact report identity. In particular, a late live
  // response from an older event must not become the winning code for a newer report render even
  // when the run generation and champion id happen to be unchanged.
  const bestCodeScope = useMemo(() => JSON.stringify({
    runId: String(runId),
    stateRunId: state.run_id ?? null,
    generation: solutionGeneration,
    nodeId: best?.id ?? null,
    access: { readOnlyReason, evidenceAvailable: evidenceAvailable !== false },
    snapshot: {
      kind: readOnly && historySeq != null ? 'history' : readOnly ? 'read-only' : 'live',
      seq: solutionSeq, routeSeq: readOnly ? historySeq : null,
    },
  }), [runId, state.run_id, solutionGeneration, best?.id, readOnlyReason, evidenceAvailable,
    readOnly, historySeq, solutionSeq])
  const [bestCodeResource, setBestCodeResource] = useState({
    scope: null, status: 'idle', data: null, error: null,
  })
  const [bestCodeNonce, setBestCodeNonce] = useState(0)
  const bestCodeRequestRef = useRef(null)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState('')
  const [refreshRetryAllowed, setRefreshRetryAllowed] = useState(true)
  const [savedRefreshIntent, setSavedRefreshIntent] = useState(null)
  const refreshStorageReady = savedRefreshIntent !== false
  const refreshGenerationReady = RUN_GENERATION_RE.test(expectedGeneration || '')
  const refreshRequestRef = useRef({
    token: 0, receiptSeq: null, timer: null, busy: false,
    idempotencyKey: null, generation: null, controller: null,
  })
  const observedSeqRef = useRef(observedSeq)
  observedSeqRef.current = observedSeq
  useEffect(() => {
    bestCodeRequestRef.current?.controller.abort()
    bestCodeRequestRef.current = null
    if (!best) {
      setBestCodeResource({ scope: bestCodeScope, status: 'idle', data: null, error: null })
      return
    }
    if (readOnlyReason === 'review' && !evidenceAvailable) {
      setBestCodeResource({ scope: bestCodeScope, status: 'restricted', data: null, error: null })
      return
    }
    if (!solutionIdentityReady) {
      setBestCodeResource({ scope: bestCodeScope, status: 'waiting', data: null, error: null })
      return
    }
    const params = new URLSearchParams()
    params.set('seq', String(solutionSeq))
    params.set('expected_generation', solutionGeneration)
    const at = `?${params.toString()}`
    const timed = deadlineGet(runNodeApiPath(runId, best.id, at), SOLUTION_DETAIL_TIMEOUT_MS)
    const request = { scope: bestCodeScope, controller: timed.controller }
    bestCodeRequestRef.current = request
    setBestCodeResource({ scope: bestCodeScope, status: 'loading', data: null, error: null })
    timed.promise.then(
      data => {
        if (bestCodeRequestRef.current !== request) return
        bestCodeRequestRef.current = null
        const exact = normalizeReportNodeDetail(data, {
          nodeId: best.id, historySeq: solutionSeq, expectedGeneration: solutionGeneration,
        })
        setBestCodeResource(exact
          ? { scope: request.scope, status: 'ready', data: exact, error: null }
          : { scope: request.scope, status: 'error', data: null,
              error: 'The node detail response did not match this run, generation, and snapshot.' })
      },
      () => {
        if (bestCodeRequestRef.current !== request) return
        bestCodeRequestRef.current = null
        const timeout = timed.timedOut()
        setBestCodeResource({
          scope: request.scope, status: timeout ? 'timeout' : 'error', data: null,
          error: timeout
            ? `The node detail request timed out after ${SOLUTION_DETAIL_TIMEOUT_MS / 1000} seconds.`
            : 'The node detail request failed. Check the connection and retry.',
        })
      },
    )
    return () => {
      if (bestCodeRequestRef.current !== request) return
      bestCodeRequestRef.current = null
      request.controller.abort()
    }
  }, [runId, best?.id, readOnlyReason, evidenceAvailable, solutionIdentityReady,
    solutionSeq, solutionGeneration, bestCodeScope, bestCodeNonce])
  const bestCodeCurrent = bestCodeResource.scope === bestCodeScope
  const bestCodeStatus = bestCodeCurrent ? bestCodeResource.status
    : !best ? 'idle'
      : readOnlyReason === 'review' && !evidenceAvailable ? 'restricted'
        : !solutionIdentityReady ? 'waiting' : 'loading'
  const solutionWaitingMessage = !solutionRunReady
    ? 'Waiting for this report to match the requested run before loading solution code…'
    : solutionGeneration == null
      ? 'Waiting for the exact run generation before loading solution code…'
      : solutionSeq == null
        ? 'Waiting for the exact report snapshot before loading solution code…'
        : 'Waiting for the historical snapshot to finish reconciling…'
  const bestCode = bestCodeCurrent ? bestCodeResource.data : null

  const finishRefresh = (token, {
    error = '', canRetry = true, preserveIntent = false,
  } = {}) => {
    const request = refreshRequestRef.current
    if (request.token !== token) return
    if (request.timer) clearTimeout(request.timer)
    let finalError = error
    let finalCanRetry = canRetry
    let finalPreserveIntent = preserveIntent
    if (!finalPreserveIntent && request.idempotencyKey && request.generation) {
      try {
        reportRefreshIntent(runId, request.generation, request.idempotencyKey)
      } catch {
        finalError = 'The completed report identity could not be cleared. Reload.'
        finalCanRetry = false
        finalPreserveIntent = true
      }
    }
    request.timer = null
    request.controller?.abort()
    request.controller = null
    request.receiptSeq = null
    request.busy = false
    if (!finalPreserveIntent) {
      request.idempotencyKey = null
      request.generation = null
    }
    setSavedRefreshIntent(finalPreserveIntent && request.idempotencyKey && request.generation
      ? { generation: request.generation, idempotencyKey: request.idempotencyKey }
      : null)
    setRefreshing(false)
    setRefreshError(finalError)
    setRefreshRetryAllowed(finalCanRetry)
  }
  // The endpoint receipt names the exact report event; content fields such as at_node and trigger
  // may legitimately repeat, so they are not completion identities.
  useEffect(() => {
    const request = refreshRequestRef.current
    if (refreshing && Number.isSafeInteger(request.receiptSeq)
        && request.generation === expectedGeneration
        && Number.isSafeInteger(observedSeq) && observedSeq >= request.receiptSeq) {
      finishRefresh(request.token)
    }
  }, [observedSeq, refreshing, expectedGeneration])
  useEffect(() => {
    const request = refreshRequestRef.current
    request.token += 1
    if (request.timer) clearTimeout(request.timer)
    request.timer = null
    request.controller?.abort()
    request.controller = null
    request.receiptSeq = null
    request.busy = false
    request.idempotencyKey = null
    request.generation = null
    setRefreshing(false)
    setRefreshError('')
    setRefreshRetryAllowed(true)
    setSavedRefreshIntent(null)
    if (!readOnly && refreshGenerationReady) {
      try {
        setSavedRefreshIntent(peekReportRefreshIntent(runId, expectedGeneration))
      } catch {
        setSavedRefreshIntent(false)
        setRefreshError('Paid report refresh needs working session storage to preserve one request identity.')
        setRefreshRetryAllowed(false)
      }
    }
    return () => {
      request.token += 1
      if (request.timer) clearTimeout(request.timer)
      request.timer = null
      request.controller?.abort()
      request.controller = null
      request.receiptSeq = null
      request.busy = false
      request.idempotencyKey = null
      request.generation = null
    }
  }, [runId, expectedGeneration, readOnly, refreshGenerationReady])

  const dl = (name, text, type) => downloadBlob(name, [text], type)
  const refresh = async () => {
    const request = refreshRequestRef.current
    if (request.busy) return
    // Bind paid work to the generation visible at click time. Until the result is authoritative,
    // remounts and retries retain this identity and rejoin the same server job.
    let intent
    try {
      intent = reportRefreshIntent(runId, expectedGeneration)
    } catch {
      const message = 'Report refresh needs working session storage.'
      setRefreshError(message)
      setRefreshRetryAllowed(false)
      onToast?.(message)
      return
    }
    if (!intent) {
      const message = 'Reload the run before generating its report; its generation is not verified.'
      setRefreshError(message)
      setRefreshRetryAllowed(false)
      onToast?.(message)
      return
    }
    request.token += 1
    const token = request.token
    request.busy = true
    request.receiptSeq = null
    request.generation = intent.generation
    request.idempotencyKey = intent.idempotencyKey
    setSavedRefreshIntent(intent)
    request.controller = typeof AbortController === 'undefined' ? null : new AbortController()
    if (request.timer) clearTimeout(request.timer)
    request.timer = null
    setRefreshing(true)
    setRefreshError('')
    setRefreshRetryAllowed(true)
    try {
      const r = await CONTROL.refreshReport(runId, {
        expectedGeneration: intent.generation,
        idempotencyKey: intent.idempotencyKey,
        signal: request.controller?.signal,
      })
      if (refreshRequestRef.current.token !== token) return
      if (r && r.ok === false) {
        const [message, canRetry, preserveIntent] = reportRefreshFailure(r)
        finishRefresh(token, {
          error: message, canRetry, preserveIntent,
        })
        onToast?.(message)
        return
      }
      if (!Number.isSafeInteger(r?.seq) || r.seq < 0) {
        const message = 'No durable report receipt was returned. Reload and reconcile it.'
        finishRefresh(token, {
          error: message, canRetry: false, preserveIntent: true,
        }); onToast?.(message)
        return
      }
      request.receiptSeq = r.seq
      if (Number.isSafeInteger(observedSeqRef.current) && observedSeqRef.current >= r.seq) {
        finishRefresh(token)
        return
      }
      request.timer = setTimeout(() => {
        const message = 'The report was generated, but this view did not observe its event. Reload the run before generating again.'
        finishRefresh(token, { error: message, canRetry: false })
        onToast?.(message)
      }, 30000)
    } catch (error) {
      if (refreshRequestRef.current.token !== token) return
      const [message, canRetry, preserveIntent] = reportRefreshFailure(error, true)
      finishRefresh(token, {
        error: message, canRetry, preserveIntent,
      })
      onToast?.(message)
    }
  }
  const refreshStatus = readOnly ? ''
    : !refreshGenerationReady
      ? 'Paid report refresh is disabled: reload the run and wait for its verified generation.'
      : !refreshStorageReady
        ? 'Paid refresh is disabled: this tab cannot safely save its request identity. Enable session storage, then reload.'
        : refreshing
          ? 'Paid refresh is running with the saved request. You can safely leave and resume it later.'
          : savedRefreshIntent
            ? 'Paid request saved. Resume rechecks the same request; it cannot start a second job. You can safely leave.'
            : 'Paid AI action: provider charges may apply. One request identity will be saved when you start, so you can safely leave and resume.'
  const refreshButtonLabel = refreshing ? 'Paid refresh running…'
    : savedRefreshIntent ? 'Resume paid refresh' : 'Refresh report · paid'
  const refreshDisabledReason = !refreshGenerationReady
    ? 'Reload the run and wait for its verified generation before starting a paid refresh.'
    : !refreshStorageReady
      ? 'Paid refresh is unavailable because this tab cannot safely store its request identity.'
      : !refreshRetryAllowed
        ? (refreshError || 'This paid refresh cannot be resumed safely yet.')
        : ''
  const exportContext = { generation: expectedGeneration, snapshotSeq: observedSeq }
  const modelCard = () => JSON.stringify(buildModelCard({ ...state, report: rep }, best, exportContext), null, 2)
  const reportSections = [
    ['report-section-summary', 'Summary'],
    ['report-section-outcomes', 'Outcomes'],
    ['report-section-failures', 'Execution problems'],
    rep && ['agent-report-heading', 'Agent narrative'],
    best && ['report-section-champion', 'Selected'],
    a.steps.length > 0 && ['report-section-trajectory', 'Trajectory'],
    imp.length > 0 && ['report-section-learnings', 'Exploratory analysis'],
    a.nEval > 0 && ['report-section-comparisons', 'Comparisons'],
    ['report-section-research', 'Hypothesis search'],
    best && ['report-section-solution', 'Solution'],
  ].filter(Boolean)
  const jumpToSection = id => {
    const heading = document.getElementById(id)
    heading?.scrollIntoView({ block: 'start' })
    heading?.focus({ preventScroll: true })
  }

  return (
    <div className="report-view" aria-busy={refreshing || undefined}>
      <h2 id="report-section-summary" tabIndex={-1} className="report-title">{state.label || state.run_id || state.task_id}</h2>
      <div className="report-sub muted">{state.label && state.label !== state.run_id ? `${state.run_id} · ` : ''}{state.direction} · {uiText(state.phase || (state.finished ? 'finished' : 'running'))}{state.stop_reason ? ` (${state.stop_reason})` : ''}
        {' · '}{nodeCount}{uiText(" nodes (")}{a.nEval}{uiText(" evaluated, ")}{failed.length}{uiText(" failed)")}{(state.llm_cost && uiMessage(" · {0} tokens · {1}", [fmtInt(state.llm_cost.total_tokens), fmtCost(state.llm_cost)]))}</div>

      <VerdictBanner v={v} onOpenPanel={onOpenPanel} canOpenPanel={canOpenPanel} />

      <div className="toolbar report-toolbar" role="group" aria-label={uiText("Report actions")}>
        {readOnly && <span className="history-inline">{((readOnlyReason === 'review' ? uiText('Read-only review · report refresh disabled') : (readOnlyReason === 'start-over' ? uiText('Start over unresolved · report refresh disabled') : uiMessage("{0} · report refresh disabled", [readOnlyLabel(readOnlyReason, historySeq)]))))}</span>}
        <button className="btn sm" onClick={() => window.print()}><OpIcon name="printer" size={12} />{uiText(" Print / PDF")}</button>
        <button className="btn sm" onClick={() => dl(`${state.run_id}_report.md`, toMarkdown({ ...state, report: rep }, best, exportContext), 'text/markdown')}><OpIcon name="download" size={12} />{uiText(" Markdown")}</button>
        {best && evidenceAvailable && <button className="btn sm" disabled={!bestCode?.code} onClick={() => dl(`solution_node${best.id}.py`, bestCode.code, 'text/x-python')}><OpIcon name="download" size={12} />{uiText(" Solution")}</button>}
        <button className="btn sm" onClick={() => dl(`${state.run_id}_model_card.json`, modelCard(), 'application/json')}><OpIcon name="download" size={12} />{uiText(" Model card")}</button>
        {!readOnly && <button className="btn sm"
          disabled={refreshing || !refreshRetryAllowed || !refreshGenerationReady || !refreshStorageReady}
          onClick={refresh}
          aria-describedby="paid-report-refresh-status"
          title={refreshDisabledReason || refreshStatus}><OpIcon name="replay" size={12} /> {uiText(refreshButtonLabel)}</button>}
        <nav className="report-sections" aria-label={uiText("Report sections")}>
          <span className="report-sections-label">{uiText("Jump to")}</span>
          {reportSections.map(([id, label]) => <button type="button" key={id}
            onClick={() => jumpToSection(id)}>{uiText(label)}</button>)}
        </nav>

      </div>
      {!readOnly && <div id="paid-report-refresh-status" className="report-inline-state paid"
        role="status" aria-live="polite" aria-atomic="true">
        <OpIcon name={savedRefreshIntent || refreshing ? 'replay' : 'bolt'} size={14} />
        <span>{uiText(refreshStatus)}</span>
      </div>}
      {refreshError && <div className="report-inline-state error" role="alert">
        <OpIcon name="alert" size={14} /><span>{refreshError}</span>
        {!readOnly && refreshRetryAllowed && refreshGenerationReady
          && <button className="btn sm" onClick={refresh}>{((savedRefreshIntent ? uiText('Resume paid request') : uiText('Retry paid refresh')))}</button>}
      </div>}

      {!best && <div className="report-empty-state" role="status">
        <h2>{((a.nEval ? uiText('No feasible champion yet') : uiText('No champion yet')))}</h2>
        <p>{((a.nEval ? uiText('Evaluations exist, but none currently qualifies for winner selection. Review constraints and failed checks.') : uiText('The report will add a champion, trajectory, and reproducible solution after the first successful evaluation.')))}</p>
      </div>}

      <ReportOutcomes evidence={outcomes} state={state} onPickNode={onPickNode} />
      <h2 id="report-section-failures" tabIndex={-1} className="section-h">{uiText("Recorded failures")}</h2>
      <p className="report-section-intro">{uiText('A crash, timeout or missing score means the experiment did not complete. It does not show that its hypothesis is wrong.')}</p>
      <dl className="report-failure-list">
        {Object.entries(a.failures).map(([reason, nodes]) => <React.Fragment key={reason}>
          <dt>{uiText(reason)}</dt><dd><b>{nodes.length}</b>{' · '}{nodes.map((node, index) => <React.Fragment key={node.id}>
            {index > 0 && ', '}{onPickNode ? <button type="button" className="report-node-link"
              aria-label={uiMessage('Inspect experiment #{0}', [node.id])}
              onClick={() => onPickNode(node.id)}>#{node.id}</button> : `#${node.id}`}
          </React.Fragment>)}</dd>
        </React.Fragment>)}
        {a.infeasible.length > 0 && <><dt>{uiText('infeasible')}</dt><dd>{a.infeasible.length}</dd></>}
      </dl>
      {!Object.keys(a.failures).length && !a.infeasible.length && <p className="muted">{uiText('No execution failures or constraint violations are recorded.')}</p>}

      <AgentNarrative rep={rep} coverage={coverage} generation={expectedGeneration} snapshotSeq={observedSeq} />
      {!rep && <p className="report-section-intro">{uiText('No detailed Assistant report has been published. Recorded outcomes remain available; discuss them in chat or refresh the report.')}</p>}

      {best && <><h2 id="report-section-champion" tabIndex={-1} className="section-h">{uiText("Selected experiment")}</h2>
        <ChampionCard best={best} state={state} /><BaseRevision node={best} state={state} /></>}
      <UpstreamPanel state={state} />

      {a.steps.length > 0 && <>
        <h2 id="report-section-trajectory" tabIndex={-1} className="section-h">{((a.steps.length > 1 ? uiText('Recorded metric trajectory') : uiText('First eligible metric')))}</h2>
        <p className="muted">{uiText("This numeric frontier may combine evaluation scores and confirmation means. Its changes do not establish a comparable improvement. Use the selected-result verdict above.")}</p>
        <Trajectory nodes={Object.values(state.nodes)} direction={state.direction} state={state}
          steps={a.steps} onPick={onPickNode} />
        <ImprovementWaterfall steps={a.steps} direction={state.direction} />
        <DataTable caption={uiText("Metric trajectory steps")} card={false}><table className="tbl report-steps-table"><thead><tr><th>#</th><th>{uiText("node")}</th><th>{uiText("operator")}</th><th>{uiText("recorded value")}</th><th>{uiText("measurement")}</th><th>{uiText("numeric change")}</th><th>{uiText("base")}</th><th>{uiText("what changed")}</th></tr></thead><tbody>
          {a.steps.map((s, i) => <tr key={s.id}>
            <td>{i + 1}</td><td>#{s.id}</td><td><span className="report-step-kind" aria-hidden="true">
              {((s.operator || uiText('unknown operator')))}
              {s.theme && s.theme !== s.operator && <span className="pill report-step-theme">{s.theme}</span>}
            </span><span className="sr-only">{reportStepIdentity(s.operator, s.theme)}</span></td>
            <td>{fmt(s.to)}</td>
            <td>{uiText(s.measurement)}</td>
            <td className="report-delta">{((s.delta == null ? uiText('first eligible') : fmt(s.delta)))}</td>
            <td><BaseRevision node={state.nodes[s.id]} state={state} compact /></td>
            <td className="muted">{s.diff.length > 2
              ? <details className="report-step-changes"><summary>{s.diff.length}{uiText(" parameter changes")}</summary>
                  <span>{uiText(paramDiffLabel(s.diff))}</span></details>
              : uiText(paramDiffLabel(s.diff))}</td></tr>)}
        </tbody></table></DataTable>
        {a.steps.length > 1 && <div className="muted">{uiText("Recorded frontier change ")}<b>{fmt(a.totalGain)}</b>{uiText(" over ")}{a.steps.length}{uiText(" steps (first eligible ")}{fmt(a.firstBest)}{uiText(" → numeric frontier ")}{fmt(a.finalBest)}).</div>}
      </>}

      {imp.length > 0 ? <>
        <h2 id="report-section-learnings" tabIndex={-1} className="section-h">{uiText("Exploratory parameter analysis")}</h2>
        {imp.length > 0 && <>
          <div className="muted" style={{ marginTop: 6 }}>{uiText("Exploratory correlation with the metric. Small n is fragile; correlation does not establish cause.")}</div>
          <DataTable caption={uiText("Report hyperparameter correlations")} card={false}><table className="tbl"><thead><tr><th>{uiText("param")}</th><th>|r|</th><th>r</th><th>n</th></tr></thead><tbody>
            {/* `row.r >= 0` is TRUE for null, so an unmeasurable correlation used to sign its own
                absence as "+—". A param no node varied has nothing to report here. */}
            {imp.map(row => <tr key={row.k}><td>{row.k}</td><td>{fmt(row.imp, 3)}</td>
              <td className="muted">{row.r == null ? '—' : `${row.r >= 0 ? '+' : ''}${fmt(row.r, 3)}`}</td>
              <td className="muted">{row.n}</td></tr>)}
          </tbody></table></DataTable></>}
      </> : null}

      {a.nEval > 0 && <>
        <h2 id="report-section-comparisons" tabIndex={-1} className="section-h">{uiText("Parent comparison coverage")}</h2>
        <p className="muted">{uiText("Only evaluation scores under matching recorded conditions count as better. Not compared includes first experiments and missing evidence; it does not mean failure.")}</p>
        <DataTable caption={uiText("Parent comparison coverage")} card={false}><table className="tbl"><thead><tr>
          <th>{uiText("Operator")}</th><th>{uiText("Evaluated")}</th><th>{uiText("Compared with parent")}</th><th>{uiText("Better score")}</th>
          <th>{uiText("Not compared")}</th><th>{uiText("Numeric frontier")}</th></tr></thead><tbody>
          {a.operators.map(row => <tr key={row.key}><td>{row.key}</td><td>{row.evaluated}</td>
            <td>{row.compared}</td><td>{row.improved}</td><td>{row.uncompared}</td>
            <td>{fmt(row.best)}{row.best != null && <span className="muted">
              {' · '}{uiText(resultMeasurement(row.bestConfirmed).label)}</span>}</td></tr>)}
        </tbody></table></DataTable>
      </>}

      <section className="report-research-link" aria-labelledby="report-section-research">
        <h2 id="report-section-research" tabIndex={-1} className="section-h">{uiText('Hypothesis search · Deep Research')}</h2>
        <p className="report-section-intro">{uiText('Research memos explore possible explanations and future experiments. They are not evidence that an approach worked. Read sources, proposed directions and verification status in Deep Research.')}</p>
        {onOpenPanel && canOpenPanel?.('research') !== false
          ? <button type="button" className="btn sm" onClick={event => onOpenPanel('research', event.currentTarget)}>{uiText('Open Deep Research')}</button>
          : <p className="muted">{uiText('Deep Research is unavailable in this view.')}</p>}
      </section>

      {best && <><h2 id="report-section-solution" tabIndex={-1} className="section-h">{uiText("Reproduce — selected solution")}</h2>
        {bestCodeStatus === 'restricted' && <div className="report-inline-state report-code-state" role="status">{uiText("Solution source was not included in this summary-only review link.")}</div>}
        {bestCodeStatus === 'waiting' && <div className="report-inline-state report-code-state" role="status" aria-live="polite">
          {uiText(solutionWaitingMessage)}
        </div>}
        {bestCodeStatus === 'loading' && <div className="report-inline-state report-code-state" role="status">{uiText("Loading solution code…")}</div>}
        {(bestCodeStatus === 'error' || bestCodeStatus === 'timeout') && <div className="report-inline-state report-code-state error" role="alert">
          <span>{uiText("Couldn’t load the winning code: ")}{bestCodeResource.error}</span>
          <button type="button" className="btn sm" onClick={() => setBestCodeNonce(n => n + 1)}>{uiText("Retry")}</button>
        </div>}
        {bestCodeStatus === 'ready' && (bestCode?.code
          ? <pre className="code">{bestCode.code}</pre>
          : <div className="report-inline-state report-code-state" role="status">{uiText("No solution source was recorded for this node (for example, a repository task may not use solution.py).")}</div>)}
      </>}

    </div>
  )
}
