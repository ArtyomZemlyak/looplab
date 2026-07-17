import React, { useEffect, useMemo, useRef, useState } from 'react'
import { peekReportRefreshIntent, reportRefreshIntent, isTransientCommandReadError, get, fmt,
  fmtInt, CONTROL, runNodeApiPath } from './util.js'
import { Trajectory, ImprovementWaterfall } from './charts.jsx'
import { analyze, buildModelCard, verdict, paramDiffLabel, toMarkdown, hyperImportance } from './report.js'
import MemoCard from './MemoCard.jsx'
import Markdown from './markdown.jsx'
import { OpIcon } from './icons.jsx'
import { reportStepIdentity } from './trustSemantics.js'
import { DataTable, downloadBlob } from './accessibility.jsx'
import { normalizeResearchMemos } from './researchMemoModel.js'
import { normalizeReportNodeDetail, normalizeRunReport, reportCoverageText,
  reportNarrativeCoverage } from './reportModel.js'
import { nodeTheme } from './conceptId.js'
import './report-trust-polish.css'

const TRUST_CLASS = { unverified: 'neutral', caveats: 'warn', suspect: 'alarm' }
const TRUST_LABEL = { unverified: 'not fully verified', caveats: 'with caveats', suspect: 'flags found' }
const OUTCOME_LABEL = { improved: '▲ improved', flat: '— flat', regressed: '▼ regressed', none: 'no result' }

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
  const cls = TRUST_CLASS[v.trust] || 'warn'
  const canOpen = panel => !!onOpenPanel && canOpenPanel?.(panel) !== false
  return (
    <section className={'verdict-banner ' + cls} aria-labelledby="report-verdict-heading">
      <div className="verdict-row">
        <span className={'verdict-pill ' + (v.outcome === 'improved' ? 'ok' : v.outcome === 'regressed' ? 'fail' : '')}>{OUTCOME_LABEL[v.outcome] || v.outcome}</span>
        {v.robustness && v.robustness !== 'n/a' && <span className="pill">{v.robustness}</span>}
        <span className="pill verdict-trust-label">{TRUST_LABEL[v.trust] || v.trust}</span>
      </div>
      <h2 id="report-verdict-heading" className="verdict-headline">{v.headline}</h2>
      {v.caveats.length > 0 && <div className="caveat-chips">
        {v.caveats.map((c, i) => {
          const openable = canOpen(c.panel)
          return <button key={i} className={'caveat-chip ' + c.severity}
            disabled={!openable} title={openable ? `see ${c.panel} →` : 'Unavailable in this read-only view'}
            onClick={() => { if (canOpen(c.panel)) onOpenPanel(c.panel) }}>
            <OpIcon name="alert" size={11} /> {c.text}
          </button>
        })}
      </div>}
    </section>
  )
}

function AgentNarrative({ rep, coverage, generation, snapshotSeq }) {
  if (!rep) return null
  const publishedIso = rep.published_at == null ? null : new Date(rep.published_at * 1000).toISOString()
  const warning = coverage.status === 'stale'
    ? 'This narrative predates visible nodes. Refresh it before relying on its claims.'
    : coverage.status === 'inconsistent'
      ? 'The report watermark is ahead of this view. Treat the narrative as stale and verify the run generation.'
      : coverage.status === 'unknown'
        ? 'The publication did not record a valid node watermark. Its coverage cannot be established.'
        : ''
  const groups = [
    ['What worked', rep.what_worked], ['Learnings', rep.learnings],
    ["What didn't work", rep.what_didnt], ['Next directions', rep.next_directions],
  ]
  return <section className={`agent-report ${coverage.status}`} role="note"
    aria-labelledby="agent-report-heading">
    <div className="agent-report-head">
      <h2 id="agent-report-heading">Agent narrative</h2>
      <span className="pill">advisory · not deterministic</span>
    </div>
    <div className="report-provenance" aria-label="Agent narrative publication provenance">
      <span className={`report-coverage ${coverage.status}`}>{reportCoverageText(coverage)}</span>
      {rep.published_seq != null && <span>published event <b>#{rep.published_seq}</b></span>}
      {publishedIso && <span>at <time dateTime={publishedIso}>{new Date(rep.published_at * 1000).toLocaleString()}</time></span>}
      {rep.trigger && <span>trigger <b>{rep.trigger}</b></span>}
      {Number.isSafeInteger(snapshotSeq) && snapshotSeq >= 0 && <span>view snapshot <b>#{snapshotSeq}</b></span>}
      {/^[0-9a-f]{64}$/.test(generation || '') && <span>generation <code title={generation}>{generation.slice(0, 12)}…</code></span>}
    </div>
    {warning && <div className="report-coverage-warning" role="status"><OpIcon name="alert" size={13} /> {warning}</div>}
    {rep.headline && <h3 className="agent-report-headline">{rep.headline}</h3>}
    {(rep.verdict || rep.summary) && <div className="agent-report-text"><Markdown text={rep.verdict || rep.summary} /></div>}
    {rep.caveats.length > 0 && <div className="agent-report-caveats">
      <div className="agent-report-caveats-title"><OpIcon name="alert" size={12} />
        <strong>Agent caveats</strong><span className="muted">advisory narrative</span></div>
      <List items={rep.caveats} />
    </div>}
    {rep.champion_summary && <div className="agent-report-group">
      <h3>Champion note at publication</h3><Markdown text={rep.champion_summary} />
    </div>}
    {groups.map(([label, items]) => items?.length > 0 && <div className="agent-report-group" key={label}>
      <h3>{label}</h3><List items={items} />
    </div>)}
  </section>
}

function ChampionCard({ best }) {
  if (!best) return null
  const m = best.confirmed_mean ?? best.metric
  const direction = nodeTheme(best)
  return (
    <div className="champion-card">
      <div className="kv">
        <div className="k">champion</div><div className="v">#{best.id} · {best.operator}{direction ? ` · ${direction}` : ''}</div>
        <div className="k">metric</div><div className="v"><b>{fmt(m)}</b>{best.confirmed_mean != null
          ? <span className="muted"> ±{fmt(best.confirmed_std)} over {best.confirmed_seeds} seed{best.confirmed_seeds === 1 ? '' : 's'}</span>
          : <span className="muted"> (single-seed)</span>}</div>
        <div className="k">params</div><div className="v">{Object.keys(best.idea?.params || {}).length
          ? Object.entries(best.idea.params).map(([k, val]) => `${k}=${fmt(val)}`).join(', ') : '—'}</div>
        {(best.parent_ids || []).length > 0 && <><div className="k">lineage</div><div className="v">{best.parent_ids.map(p => '#' + p).join(' → ')}</div></>}
        <div className="k">feasible</div><div className="v">{best.feasible === true ? 'yes' : best.feasible === false ? 'no — constraint violated' : 'unknown — not established'}</div>
      </div>
    </div>
  )
}

function List({ items }) {
  if (!items || !items.length) return null
  return <ul className="bul">{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
}

export default function ReportView({ state, runId, onOpenPanel, canOpenPanel, onToast, onPickNode, readOnly = false,
  historySeq = null, expectedGeneration = null, observedSeq = null,
  readOnlyReason = 'history', evidenceAvailable = true }) {
  const best = state.best_node_id != null ? state.nodes[state.best_node_id] : null
  const failed = Object.values(state.nodes).filter(n => n.status === 'failed')
  const a = useMemo(() => analyze(state), [state])
  const v = useMemo(() => verdict(state, a), [state, a])
  const rep = useMemo(() => normalizeRunReport(state.report), [state.report])
  const nodeCount = Object.keys(state.nodes).length
  const coverage = useMemo(() => reportNarrativeCoverage(rep, nodeCount), [rep, nodeCount])
  const imp = useMemo(() => hyperImportance(state).slice(0, 6), [state])
  const memoProjection = useMemo(() => normalizeResearchMemos(state.research), [state.research])
  const memos = memoProjection.memos
  const [bestCodeResource, setBestCodeResource] = useState({ status: 'idle', data: null, error: null })
  const [bestCodeNonce, setBestCodeNonce] = useState(0)
  const [openMemo, setOpenMemo] = useState(memos.length ? memos[memos.length - 1].sourceIndex : null)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState('')
  const [refreshRetryAllowed, setRefreshRetryAllowed] = useState(true)
  const [savedRefreshIntent, setSavedRefreshIntent] = useState(null)
  const refreshStorageReady = savedRefreshIntent !== false
  const refreshGenerationReady = /^[0-9a-f]{64}$/.test(expectedGeneration || '')
  const refreshRequestRef = useRef({
    token: 0, receiptSeq: null, timer: null, busy: false,
    idempotencyKey: null, generation: null, controller: null,
  })
  const observedSeqRef = useRef(observedSeq)
  observedSeqRef.current = observedSeq
  useEffect(() => {
    if (!best) { setBestCodeResource({ status: 'idle', data: null, error: null }); return }
    if (readOnlyReason === 'review' && !evidenceAvailable) {
      setBestCodeResource({ status: 'restricted', data: null, error: null }); return
    }
    let alive = true
    const controller = typeof AbortController === 'undefined' ? null : new AbortController()
    setBestCodeResource({ status: 'loading', data: null, error: null })
    const at = readOnly && historySeq != null
      ? `?seq=${encodeURIComponent(historySeq)}&expected_generation=${encodeURIComponent(expectedGeneration || '')}`
      : ''
    get(runNodeApiPath(runId, best.id, at), controller ? { signal: controller.signal } : {})
      .then(data => {
        if (!alive) return
        const exact = normalizeReportNodeDetail(data, {
          nodeId: best.id, historySeq: readOnly && historySeq != null ? historySeq : null,
          expectedGeneration,
        })
        setBestCodeResource(exact
          ? { status: 'ready', data: exact, error: null }
          : { status: 'error', data: null,
              error: 'The node detail response did not match this report.' })
      })
      .catch(() => { if (alive) setBestCodeResource({ status: 'error', data: null, error: 'The node detail request failed.' }) })
    return () => { alive = false; controller?.abort() }
  }, [runId, best?.id, readOnly, historySeq, expectedGeneration, readOnlyReason,
    evidenceAvailable, bestCodeNonce])
  const bestCode = bestCodeResource.data

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
            : 'Paid AI action: provider charges may apply. One request identity is saved so you can safely leave and resume.'
  const refreshButtonLabel = refreshing ? 'Paid refresh running…'
    : savedRefreshIntent ? 'Resume paid refresh' : 'Refresh report · paid'
  const refreshDisabledReason = !refreshGenerationReady
    ? 'Reload the run and wait for its verified generation before starting a paid refresh.'
    : !refreshStorageReady
      ? 'Paid refresh is unavailable because this tab cannot safely store its request identity.'
      : !refreshRetryAllowed
        ? (refreshError || 'This paid refresh cannot be resumed safely yet.')
        : ''
  const impr = s => s.delta == null || (state.direction === 'min' ? s.delta < 0 : s.delta > 0)
  const exportContext = { generation: expectedGeneration, snapshotSeq: observedSeq }
  const modelCard = () => JSON.stringify(buildModelCard({ ...state, report: rep }, best, exportContext), null, 2)

  return (
    <div className="report-view" aria-busy={refreshing || undefined}>
      <div className="toolbar report-toolbar">
        {!readOnly && <button className="btn sm primary"
          disabled={refreshing || !refreshRetryAllowed || !refreshGenerationReady || !refreshStorageReady}
          onClick={refresh}
          aria-describedby="paid-report-refresh-status"
          title={refreshDisabledReason || refreshStatus}><OpIcon name="replay" size={12} /> {refreshButtonLabel}</button>}
        {readOnly && <span className="history-inline">{readOnlyReason === 'review'
          ? 'Read-only review · report refresh disabled'
          : `Snapshot seq ${historySeq} · report refresh disabled`}</span>}
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn sm" onClick={() => window.print()}><OpIcon name="printer" size={12} /> Print / PDF</button>
        <button className="btn sm" onClick={() => dl(`${state.run_id}_report.md`, toMarkdown({ ...state, report: rep }, best, exportContext), 'text/markdown')}><OpIcon name="download" size={12} /> Markdown</button>
        {best && evidenceAvailable && <button className="btn sm" disabled={!bestCode?.code} onClick={() => dl(`solution_node${best.id}.py`, bestCode.code, 'text/x-python')}><OpIcon name="download" size={12} /> Solution</button>}
        <button className="btn sm" onClick={() => dl(`${state.run_id}_model_card.json`, modelCard(), 'application/json')}><OpIcon name="download" size={12} /> Model card</button>
      </div>
      {!readOnly && <div id="paid-report-refresh-status" className="report-inline-state paid"
        role="status" aria-live="polite" aria-atomic="true">
        <OpIcon name={savedRefreshIntent || refreshing ? 'replay' : 'bolt'} size={14} />
        <span>{refreshStatus}</span>
      </div>}
      {refreshError && <div className="report-inline-state error" role="alert">
        <OpIcon name="alert" size={14} /><span>{refreshError}</span>
        {!readOnly && refreshRetryAllowed && refreshGenerationReady
          && <button className="btn sm" onClick={refresh}>{savedRefreshIntent
            ? 'Resume paid request' : 'Retry paid refresh'}</button>}
      </div>}

      <h1 className="report-title">{state.goal || state.task_id}</h1>
      <div className="report-sub muted">{state.run_id} · {state.direction} · {state.phase || (state.finished ? 'finished' : 'running')}{state.stop_reason ? ` (${state.stop_reason})` : ''}
        {' · '}{nodeCount} nodes ({a.nEval} evaluated, {failed.length} failed)
        {state.llm_cost && ` · ${fmtInt(state.llm_cost.total_tokens)} tokens · $${fmt(state.llm_cost.cost)}`}</div>

      <VerdictBanner v={v} onOpenPanel={onOpenPanel} canOpenPanel={canOpenPanel} />

      {!best && <div className="report-empty-state" role="status">
        <h2>{a.nEval ? 'No feasible champion yet' : 'No champion yet'}</h2>
        <p>{a.nEval
          ? 'Evaluations exist, but none currently qualifies for winner selection. Review constraints and failed checks.'
          : 'The report will add a champion, trajectory, and reproducible solution after the first successful evaluation.'}</p>
      </div>}

      {best && <><h2 className="section-h">Champion — the answer</h2>
        <ChampionCard best={best} /></>}

      {a.steps.length > 0 && <>
        <h2 className="section-h">{a.steps.length > 1 ? 'How the metric got better' : 'Metric baseline'}</h2>
        <Trajectory nodes={Object.values(state.nodes)} direction={state.direction} steps={a.steps} onPick={onPickNode} />
        <ImprovementWaterfall steps={a.steps} direction={state.direction} />
        <DataTable caption="Metric trajectory steps" card={false}><table className="tbl"><thead><tr><th>#</th><th>node</th><th>operator</th><th>metric</th><th>Δ</th><th>what changed</th></tr></thead><tbody>
          {a.steps.map((s, i) => <tr key={s.id}>
            <td>{i + 1}</td><td>#{s.id}</td><td><span className="report-step-kind" aria-hidden="true">
              {s.operator || 'unknown operator'}
              {s.theme && s.theme !== s.operator && <span className="pill report-step-theme">{s.theme}</span>}
            </span><span className="sr-only">{reportStepIdentity(s.operator, s.theme)}</span></td>
            <td>{fmt(s.to)}</td>
            <td className={`report-delta ${s.delta == null ? 'baseline' : (impr(s) ? 'improved' : 'regressed')}`}>{s.delta == null ? 'baseline' : fmt(s.delta)}</td>
            <td className="muted">{paramDiffLabel(s.diff)}</td></tr>)}
        </tbody></table></DataTable>
        {a.steps.length > 1 && <div className="muted">Total improvement <b>{fmt(a.totalGain)}</b> over {a.steps.length} steps (baseline {fmt(a.firstBest)} → best {fmt(a.finalBest)}).</div>}
      </>}

      {(memos.length || imp.length) ? <>
        <h2 className="section-h">What we learned</h2>
        {imp.length > 0 && <>
          <div className="muted" style={{ marginTop: 6 }}>which knobs mattered (|correlation| with the metric)</div>
          <DataTable caption="Report hyperparameter importance" card={false}><table className="tbl"><thead><tr><th>param</th><th>importance</th><th>r</th><th>n</th></tr></thead><tbody>
            {imp.map(row => <tr key={row.k}><td>{row.k}</td><td>{fmt(row.imp, 3)}</td>
              <td className="muted">{row.r >= 0 ? '+' : ''}{fmt(row.r, 3)}</td><td className="muted">{row.n}</td></tr>)}
          </tbody></table></DataTable></>}
        {memos.length > 0 && <div style={{ marginTop: 8 }}>
          {memoProjection.omitted > 0 && <div className="muted">
            Showing the latest {memos.length} of {memoProjection.total} research memos; older, malformed, or over-budget entries are omitted.
          </div>}
          {memos.map(m => <MemoCard key={m.sourceIndex} memo={m} idx={m.sourceIndex}
            open={openMemo === m.sourceIndex} onToggle={(key) => setOpenMemo(current => current === key ? null : key)} />)}
        </div>}
      </> : null}

      <h2 className="section-h">What didn't work</h2>
      <div className="cardgrid" style={{ marginBottom: 10 }}>
        {Object.entries(a.failures).map(([r, ns]) => <div key={r} className="stat"><div className="n">{ns.length}</div><div className="l">failed · {r}</div></div>)}
        {a.regressions.length > 0 && <div className="stat"><div className="n">{a.regressions.length}</div><div className="l">regressions</div></div>}
        {a.infeasible.length > 0 && <div className="stat"><div className="n">{a.infeasible.length}</div><div className="l">infeasible</div></div>}
        {!Object.keys(a.failures).length && !a.regressions.length && !a.infeasible.length && <div className="stat"><div className="n">0</div><div className="l">nothing notably failed</div></div>}
      </div>

      {best && <><h2 className="section-h">Reproduce — winning solution</h2>
        {bestCodeResource.status === 'restricted' && <div className="report-inline-state report-code-state" role="status">
          Solution source was not included in this summary-only review link.
        </div>}
        {bestCodeResource.status === 'loading' && <div className="report-inline-state report-code-state" role="status">Loading solution code…</div>}
        {bestCodeResource.status === 'error' && <div className="report-inline-state report-code-state error" role="alert">
          <span>Couldn’t load the winning code: {bestCodeResource.error}</span>
          <button className="btn sm" onClick={() => setBestCodeNonce(n => n + 1)}>Retry</button>
        </div>}
        {bestCodeResource.status === 'ready' && (bestCode?.code
          ? <pre className="code">{bestCode.code}</pre>
          : <div className="report-inline-state report-code-state" role="status">No solution source was recorded for this node (for example, a repository task may not use solution.py).</div>)}
      </>}

      {/* Provider prose is intentionally last: it may explain the run, but cannot visually bury
          the deterministic champion, trajectory, failures, or reproduction evidence. */}
      <AgentNarrative rep={rep} coverage={coverage} generation={expectedGeneration} snapshotSeq={observedSeq} />
    </div>
  )
}
