import React, { useEffect, useMemo, useRef, useState } from 'react'
import { get, putText, post, fmt, fmtInt, fmtBytes, fmtElapsedSeconds, CONTROL, saveRunConfig, operatorMeta,
  commandFeedback, runApiPath, runNodeApiPath,
} from './util.js'
import { usePoll } from './hooks.js'
import { Bars, ParallelCoords, Scatter } from './charts.jsx'
import { hyperImportance } from './report.js'
import Markdown, { stripMd } from './markdown.jsx'
import { OpIcon } from './icons.jsx'
import CodeViewer from './CodeViewer.jsx'
import { diffLines } from './lineDiff.js'
import SettingsForm from './SettingsForm.jsx'
import { toForm, fromForm, settingsValidationErrors, loadSettingsSchema } from './settingsSchema.js'
import {
  reconcileAcceptedRecord, reconcileUnknownRecord, runConfigWriteDisposition, splitRunConfigPayload,
  validateRunConfigSaveAck,
} from './settingsModel.js'
import { driftStatus, leakageStatus, rewardHackStatus } from './trustSemantics.js'
import VirtualTimeline from './VirtualTimeline.jsx'
import { timelineEventKey } from './timelineModel.js'
import { queuedGenerationControls } from './queue.js'
import Panel from './PanelShell.jsx'
import { DataTable } from './accessibility.jsx'
import { safeExternalHref } from './urlSafety.js'
import { normalizeResearchMemos } from './researchMemoModel.js'
import { deadlineRequest } from './requestDeadline.js'
import { installNavigationLossGuard } from './navigationLossGuard.js'

export { default as Panel } from './PanelShell.jsx'

const Stat = ({ n, l }) => <div className="stat"><div className="n">{n}</div><div className="l">{l}</div></div>

const invalidPanelPayload = () => { throw new Error('Invalid panel payload') }
const isRecord = value => !!value && typeof value === 'object' && !Array.isArray(value)
const nullableText = value => value === null || typeof value === 'string'
const nullableNumber = value => value === null || (typeof value === 'number' && Number.isFinite(value))

// HTTP 200 proves transport success, not resource truth. Each panel validates its exact envelope
// before replacing last-good data or presenting an authoritative empty state.
const PANEL_REQUEST_TIMEOUT_MS = 15_000
const publicConfigForm = form => ({ ...(form || {}), llm_api_key: '' })

function authoringPayload(value) {
  if (!isRecord(value) || !nullableText(value.dir) || !Array.isArray(value.files)) invalidPanelPayload()
  const files = value.files.map(file => {
    if (!isRecord(file) || !file.name || typeof file.name !== 'string' || typeof file.text !== 'string') invalidPanelPayload()
    return { name: file.name, text: file.text }
  })
  return { dir: value.dir, files }
}

function memoryPayload(value) {
  if (!isRecord(value) || !nullableText(value.dir)
      || !['cases', 'lessons', 'notes'].every(key => Array.isArray(value[key]))) invalidPanelPayload()
  for (const row of value.cases) {
    if (!isRecord(row) || !row.task_id || typeof row.task_id !== 'string' || typeof row.goal !== 'string'
        || !nullableNumber(row.metric) || !Object.hasOwn(row, 'params')) invalidPanelPayload()
  }
  const lessonText = ['role', 'kind', 'outcome', 'task_id']
  for (const row of value.lessons) {
    if (!isRecord(row) || !row.statement || typeof row.statement !== 'string'
        || lessonText.some(key => row[key] != null && typeof row[key] !== 'string')
        || ['delta', 'confidence'].some(key => row[key] != null && !nullableNumber(row[key]))
        || (row.evidence_count != null && (!Number.isSafeInteger(row.evidence_count) || row.evidence_count < 0))) invalidPanelPayload()
  }
  const notes = value.notes.map(row => {
    const note = isRecord(row) && (row.note || row.statement)
    if (typeof note !== 'string' || !note || (row.task_id != null && typeof row.task_id !== 'string')) invalidPanelPayload()
    return { ...row, note }
  })
  return { dir: value.dir, cases: value.cases, lessons: value.lessons, notes }
}

function runsPayload(value) {
  if (!Array.isArray(value)) invalidPanelPayload()
  for (const row of value) {
    if (!isRecord(row) || !row.run_id || typeof row.run_id !== 'string' || typeof row.task_id !== 'string'
        || !['min', 'max'].includes(row.direction) || typeof row.finished !== 'boolean'
        || typeof row.phase !== 'string' || !Number.isSafeInteger(row.nodes) || row.nodes < 0
        || !nullableNumber(row.best_metric) || !nullableNumber(row.best_confirmed)
        || !nullableText(row.label)) invalidPanelPayload()
  }
  return value
}

function gpuPayload(value) {
  if (!isRecord(value) || typeof value.available !== 'boolean'
      || (value.gpus != null && !Array.isArray(value.gpus))) invalidPanelPayload()
  const gpus = value.gpus || []
  for (const gpu of gpus) {
    if (!isRecord(gpu) || !gpu.name || typeof gpu.name !== 'string'
        || ['util', 'mem_used', 'mem_total', 'temp', 'power'].some(key => !nullableNumber(gpu[key]))) invalidPanelPayload()
  }
  if (value.available && !Array.isArray(value.gpus)) invalidPanelPayload()
  return { available: value.available, gpus }
}

// A poll and a manual retry share one synchronous lock: interval ticks skip an active request, while
// Retry starts immediately when idle. A failed refresh retains last-good data and marks it stale.
function usePanelResource(loader, normalize = value => value, key = '', pollMs = null) {
  const [value, setValue] = useState({ key, state: 'loading', data: null, pending: null })
  const flight = useRef(null)
  const startRef = useRef(null)
  useEffect(() => {
    let alive = true
    const owner = {}
    const start = (intent = 'refresh') => {
      if (flight.current) return false
      const timed = deadlineRequest(signal => Promise.resolve().then(() => loader(signal)).then(normalize),
        PANEL_REQUEST_TIMEOUT_MS)
      const request = { owner, controller: timed.controller }
      flight.current = request
      setValue(previous => previous.key !== key
        ? { key, state: 'loading', data: null, pending: null }
        : ['error', 'stale'].includes(previous.state) ? { ...previous, pending: intent } : previous)
      const finish = (ok, data = null) => {
        if (flight.current !== request) return
        flight.current = null
        if (!alive) return
        setValue(previous => {
          const lastGood = previous.key === key ? previous.data : null
          return ok ? { key, state: 'ready', data, pending: null }
            : { key, state: lastGood == null ? 'error' : 'stale', data: lastGood, pending: null }
        })
      }
      timed.promise.then(data => finish(true, data), () => finish(false))
      return true
    }
    startRef.current = start
    start('load')
    const timer = pollMs == null ? null : setInterval(start, pollMs)
    return () => {
      alive = false
      if (timer != null) clearInterval(timer)
      if (startRef.current === start) startRef.current = null
      if (flight.current?.owner === owner) {
        flight.current.controller.abort()
        flight.current = null
      }
    }
  }, [key, pollMs])
  const resource = value.key === key ? value : { key, state: 'loading', data: null, pending: null }
  return [resource, () => startRef.current?.('retry') || false]
}

function PanelResourceNotice({ resource, label, onRetry }) {
  if (resource.state === 'ready') return null
  if (resource.state === 'loading') return <div className="muted" role="status">Loading {label}…</div>
  const stale = resource.state === 'stale'
  return <div className={'report-inline-state' + (stale ? '' : ' error')} role={stale ? 'status' : 'alert'}>
    <OpIcon name="alert" size={14} />
    <span>{label}: {resource.pending
      ? `${resource.pending === 'retry' ? 'Retrying' : 'Refreshing'}…${stale ? ' Last loaded data remains visible.' : ''}`
      : stale ? 'Last loaded data; refresh failed.' : 'Unavailable.'}</span>
    <button className="btn sm" disabled={!!resource.pending} onClick={onRetry}>
      {resource.pending === 'retry' ? 'Retrying…' : resource.pending ? 'Refreshing…' : 'Retry'}</button>
  </div>
}

// Overall-info tab (round-8): the run's at-a-glance metrics, lifted out of the cramped top bar so the
// header stays a single line. Everything derives from the folded state (+ maxEval from config).
export function OverviewPanel({ state, maxEval, onClose, onOpenPanel }) {
  const nodes = Object.values(state.nodes || {})
  const evaluated = nodes.filter(n => n.metric != null).length
  const failed = nodes.filter(n => n.status === 'failed').length
  const best = state.best_node_id != null ? (state.nodes || {})[state.best_node_id] : null
  const evalSec = state.total_eval_seconds || 0
  const cost = state.llm_cost
  const strat = state.active_strategy
  const hints = state.pending_hints || []
  return (
    <Panel title="Overview" sub={state.task_id || ''} onClose={onClose}>
      {state.goal && <div className="ov-goal">{state.goal}</div>}
      <div className="stat-grid">
        <Stat n={best ? fmt(best.confirmed_mean ?? best.metric) : '—'} l="best metric" />
        <Stat n={state.direction || '—'} l="direction" />
        <Stat n={nodes.length} l="nodes" />
        <Stat n={evaluated} l="evaluated" />
        <Stat n={failed} l="failed" />
        <Stat n={fmtElapsedSeconds(evalSec) + (maxEval != null ? ' / ' + fmtElapsedSeconds(maxEval) : '')} l="eval time" />
        {cost && <Stat n={fmtInt(cost.total_tokens)} l="tokens" />}
        {state.paused ? <Stat n="paused" l="status" /> : null}
      </div>
      {strat && <div className="ov-row"><span className="k"><OpIcon name="compass" className="t-ic" /> strategy</span>{' '}
        {(strat.policy || 'greedy') + (strat.fidelity ? '/' + strat.fidelity : '')}
        {strat.rationale && <div className="muted ov-why">{strat.rationale}</div>}</div>}
      {hints.length > 0 && <div className="ov-row"><span className="k"><OpIcon name="bulb" className="t-ic" /> hints ({hints.length})</span>
        <ul className="ov-hints">{hints.map((h, i) => <li key={(h.text || '') + i}>{h.text || JSON.stringify(h)}</li>)}</ul></div>}
      {(state.novelty_events?.length > 0 || state.reward_hacks?.length > 0) && <div className="ov-row ov-alerts">
        {state.novelty_events?.length > 0 && <span className="chip" title="near-duplicate proposals nudged to diversify (E1)"><OpIcon name="replay" className="t-ic" /> dedup {state.novelty_events.length}</span>}
        {state.reward_hacks?.length > 0 && <button type="button" className="chip alarm run-metric-chip"
          title="suspicious wins flagged (B5)" onClick={() => onOpenPanel?.('trust')}>
          <OpIcon name="alert" size={11} /> hack? {state.reward_hacks.length}</button>}
      </div>}
    </Panel>
  )
}

// Deep-research drawer: every memo in one place (instead of scrolling the timeline feed), with
// ACTIONABLE directions — "steer →" posts a hint the Researcher folds into the next proposal. Deep
// research is no longer a DAG node; this drawer + the Dock timeline marker are its home.
export function ResearchPanel({ state, runId, onToast, onClose }) {
  const memoProjection = useMemo(() => normalizeResearchMemos(state.research), [state.research])
  const memos = [...memoProjection.memos].reverse()   // newest retained first
  const steer = async (text) => {
    try {
      const feedback = commandFeedback(await CONTROL.hint(runId, 'try this research direction: ' + text), {
        success: 'Steered the next proposal', noop: 'That direction was already queued',
        executing: 'Steer request accepted — waiting for the run', failure: 'Could not steer',
      }); onToast?.(feedback.message)
    } catch (error) { onToast?.(`Could not steer: ${error.message || error}`) }
  }
  return (
    <Panel title="Deep research" sub={memos.length ? `${memos.length} memo${memos.length === 1 ? '' : 's'}` : 'none yet'} onClose={onClose} wide>
      {!memos.length && <div className="muted">No deep-research memos yet. Trigger one with <code>/deep-research</code> in the chat, or set a cadence in Config.</div>}
      {memoProjection.omitted > 0 && <div className="muted">
        Showing {memos.length} of {memoProjection.total} newest valid memos; older, malformed, or over-budget entries are omitted.
      </div>}
      {memos.map(m => (
        // Key by the STABLE original index (research is append-only), not the reversed position:
        // keyed by `i`, a new memo landing at index 0 reuses the prior memo's DOM node and its open
        // <details> state bleeds onto the new one.
        <div className="rsch-memo" key={m.sourceIndex}>
          <div className="rsch-h">
            <span className="rsch-ic"><OpIcon name="search" /></span>
            <b>{m.summary || '(no summary)'}</b>
            <span className="right" />
            {m.trigger && <span className="pill">{m.trigger}</span>}
            {m.at_node != null && <span className="pill">@#{m.at_node}</span>}
          </div>
          {(m.findings || []).length > 0 && <><div className="section-h">Findings</div>
            <ul className="bul">{m.findings.map((f, j) => <li key={j}>{f}</li>)}</ul></>}
          {/* D8: decoupled Verifier verdicts over the memo's claims (synthesis is the weak link) */}
          {m.verification && ((m.verification.verdicts || []).length > 0
            || m.verification.omittedVerdicts > 0) && <>
            <div className="section-h">Verification
              {m.verification.unsupported > 0 &&
                <span className="chip warn" title="claims whose cited evidence does not support them">
                  {m.verification.unsupported} unsupported</span>}
              {m.verification.omittedVerdicts > 0 &&
                <span className="chip warn">verification incomplete</span>}
              <span className="muted"> ({m.verification.method})</span></div>
            {m.verification.omittedVerdicts > 0 && <p className="memo-verification-incomplete" role="note">
              Showing {m.verification.verdicts.length} of {m.verification.totalVerdicts} verifier verdicts;
              omitted verdicts make this check incomplete.
            </p>}
            <ul className="bul">{m.verification.verdicts.map((v, j) => (
              <li key={j} className={v.verdict === 'supported' ? 'ok'
                : (v.verdict === 'unclear' || v.verdict === 'cited') ? '' : 'bad'}>
                <span className="pill">{v.verdict}</span> {v.statement}
                {v.note && <span className="muted"> — {v.note}</span>}</li>))}</ul></>}
          {(m.recommended_directions || []).length > 0 && <><div className="section-h">Recommended directions</div>
            <ul className="rsch-dirs">{m.recommended_directions.map((d, j) => (
              <li key={j}><span>{d}</span>
                <button className="btn sm ghost" title="steer the next proposal toward this direction (posts a hint)"
                        onClick={() => steer(d)}>steer →</button></li>))}</ul></>}
          {(m.sources || []).length > 0 && <><div className="section-h">Sources</div>
            <ul className="bul">{m.sources.map((source, index) => {
              // Deep-research sources are untrusted provider output. Only credential-free HTTP(S)
              // URLs become links; unsafe, oversized or malformed values remain bounded inert text.
              const href = safeExternalHref(source?.url)
              const label = String(source?.title ?? source?.url ?? 'source').slice(0, 300)
              const snippet = source?.snippet == null ? '' : String(source.snippet).slice(0, 160)
              return <li key={index}>{href
                ? <a href={href} target="_blank" rel="noreferrer noopener">{label}</a> : label}
                {snippet && <div className="muted">{snippet}</div>}</li>
            })}</ul></>}
          {m.reasoning && <details className="rsch-reasoning"><summary>reasoning (debug)</summary>
            <Markdown className="think-body" text={m.reasoning} /></details>}
        </div>
      ))}
    </Panel>
  )
}

function TrustState({ value, action = null }) {
  const icon = value.tone === 'alarm' ? 'alert' : value.tone === 'ok' ? 'check' : 'dot'
  return <div className={`trust-state ${value.tone}`} role={value.tone === 'alarm' ? 'alert' : 'status'}>
    <OpIcon name={icon} size={14} />
    <strong>{value.label}</strong>
    <span>{value.detail}</span>
    {action && <div className="trust-state-actions">{action}</div>}
  </div>
}

export function TrustPanel({ state, runId, onClose, onSelect, onToast, readOnly = false }) {
  const [configResource, setConfigResource] = useState({ status: 'loading', data: null, error: null })
  const [configNonce, setConfigNonce] = useState(0)
  useEffect(() => {
    let alive = true
    setConfigResource({ status: 'loading', data: null, error: null })
    get(`/api/runs/${encodeURIComponent(runId)}/config`)
      .then(data => { if (alive) setConfigResource({ status: 'ready', data, error: null }) })
      .catch(error => { if (alive) setConfigResource({ status: 'error', data: null, error: error.message || 'Request failed' }) })
    return () => { alive = false }
  }, [runId, configNonce])
  const cfg = configResource.data
  const quarantine = async (id) => {   // U6: act on a flagged node — remove it from the search
    try {
      const feedback = commandFeedback(await CONTROL.nodeAbort(runId, id, state.nodes?.[id]?.attempt), {
        success: `Quarantined #${id}`, noop: `#${id} was already settled`,
        executing: `Quarantine of #${id} requested — waiting for the run`, failure: `Could not quarantine #${id}`,
      }); onToast?.(feedback.message)
    } catch (error) { onToast?.(`Could not quarantine #${id}: ${error.message || error}`) }
  }
  const nodes = Object.values(state.nodes)
  const evald = nodes.filter(n => n.metric != null && n.feasible !== false)
  const chooser = state.direction === 'min' ? (a, b) => a < b : (a, b) => a > b
  const naive = evald.slice().sort((a, b) => chooser(a.metric, b.metric) ? -1 : 1)[0]
  const robust = state.best_node_id != null ? state.nodes[state.best_node_id] : null
  const leak = state.leakage
  const leakState = leakageStatus(leak)
  const driftState = driftStatus(state.drifts, cfg, evald.length)
  const hackState = rewardHackStatus(state.reward_hacks, cfg, evald.length)
  return (
    <Panel title="Trust & rigor" sub="evidence and coverage" onClose={onClose} wide>
      <div className="trust-panel-body">
      {configResource.status === 'loading' && <TrustState value={{ tone: 'loading', label: 'Loading detector configuration', detail: 'Checking which trust controls were actually enabled for this run.' }} />}
      {configResource.status === 'error' && <TrustState
        value={{ tone: 'unknown', label: 'Detector configuration unavailable', detail: `Coverage cannot be verified: ${configResource.error}` }}
        action={<button className="btn sm" onClick={() => setConfigNonce(n => n + 1)}>Retry</button>} />}

      <div className="cardgrid">
        <Stat n={cfg?.trust_mode || (configResource.status === 'loading' ? 'Loading…' : 'Unknown')} l="sandbox tier" />
        <Stat n={cfg?.eval_trust_mode || (configResource.status === 'loading' ? 'Loading…' : 'Unknown')} l="eval trust mode" />
        <Stat n={state.host_grading ? 'host-side' : 'self-reported'} l="metric scoring" />
        <Stat n={state.workspace_changed ? 'changed' : 'no change flag'} l="workspace drift" />
      </div>
      {state.host_grading
        ? <TrustState value={{ tone: 'ok', label: 'Host-side grading recorded', detail: `The candidate writes predictions only; ${state.host_grading.scorer || 'the host scorer'} evaluates ${state.host_grading.n_labels ?? 'held-out'} labels outside the candidate process.` }} />
        : <TrustState value={{ tone: 'warn', label: 'Metric is not host-graded', detail: 'This run does not record an out-of-process grader, so the displayed metric may be self-reported by the candidate process.' }} />}

      <div className="section-h">Seed-luck and robustness</div>
      {robust && naive
        ? <>
          <TrustState value={robust.confirmed_mean != null
            ? { tone: 'ok', label: 'Winner is multi-seed confirmed', detail: `${robust.confirmed_seeds || 'Multiple'} successful seeds produced ${fmt(robust.confirmed_mean)} ±${fmt(robust.confirmed_std)}.` }
            : { tone: 'warn', label: 'Winner is single-evaluation', detail: 'Seed luck has not been ruled out; the selected winner is not a robust result yet.' }} />
          <div className="kv">
            <div className="k">single-eval leader</div><div className="v">#{naive.id} · {fmt(naive.metric)}</div>
            <div className="k">selected winner</div><div className="v">#{robust.id} · {fmt(robust.confirmed_mean ?? robust.metric)}{robust.confirmed_mean != null ? ` ±${fmt(robust.confirmed_std)}` : ' (unconfirmed)'}</div>
            {robust.confirmed_mean != null && naive.id !== robust.id && <><div className="k flag">demotion</div><div className="v">Single-eval leader #{naive.id} was not selected — multi-seed confirmation corrected a seed-lucky result.</div></>}
          </div>
        </>
        : <TrustState value={{ tone: 'unknown', label: 'No result to confirm', detail: 'There are no feasible evaluated nodes yet.' }} />}

      <div className="section-h">Leakage scan {leak && leak.leak && <span className="chip alarm">LEAK — run refused</span>}</div>
      <TrustState value={leakState} />
      {(leak?.verdicts || []).length > 0 &&
        <DataTable caption="Leakage detector verdicts" card={false}><table className="tbl"><thead><tr><th>detector</th><th>result</th><th>detail</th></tr></thead><tbody>
          {(leak.verdicts || []).map((v, i) => <tr key={i}>
            <td>{v.detector || 'unnamed detector'}</td><td className={`trust-result ${v.leak ? 'fail' : 'pass'}`}>{v.leak ? 'Flagged' : 'Passed'}</td>
            <td className="muted">{Object.entries(v).filter(([k]) => !['detector', 'leak'].includes(k)).map(([k, val]) => `${k}=${typeof val === 'object' ? JSON.stringify(val) : val}`).join('  ') || '—'}</td>
          </tr>)}</tbody></table></DataTable>}

      <div className="section-h">Drift cross-check</div>
      <TrustState value={driftState} />
      {(state.drifts || []).length
        ? <DataTable caption="Run metric drift comparisons" card={false}><table className="tbl"><thead><tr><th>node</th><th>primary</th><th>cross</th><th>tolerance</th></tr></thead><tbody>
          {state.drifts.map((d, i) => <tr key={i}><td className="flag">#{d.node_id}</td><td>{fmt(d.primary)}</td><td>{fmt(d.cross)}</td><td>{fmt(d.tolerance)}</td></tr>)}</tbody></table></DataTable>
        : null}

      <div className="section-h">Reward-hacking monitor (B5) {(state.reward_hacks || []).length > 0 && <span className="chip alarm">{state.reward_hacks.length} flagged</span>}</div>
      <TrustState value={hackState} />
      {/* Folded state is the enforcement truth (it applies trust_gate_changed events);
          the config snapshot alone can claim a gate the fold never engages. */}
      {(state.trust_gate || cfg) && <div className="muted" style={{ marginBottom: 6 }}>
        enforcement: <b>{state.trust_gate || cfg?.trust_gate || 'audit'}</b>{(state.trust_gate || cfg?.trust_gate || 'audit') === 'audit'
          ? ' — signals are logged only; set trust_gate=gate/block (or the thorough profile) to keep a high-precision flag from winning.'
          : ' — a high-precision flag is excluded from best-selection and breeding/confirmation.'}
        {' '}Only high-precision signals gate; broad critic/perfect-score warnings stay advisory, except
        <code> critic:hardcoded_metric</code>, which is classified as high precision.</div>}
      {(state.reward_hacks || []).length
        ? <DataTable caption="Reward-hacking signals" card={false}><table className="tbl"><thead><tr><th>node</th><th>signal</th><th>detail</th><th>action</th></tr></thead><tbody>
          {state.reward_hacks.map((h, i) => <tr key={i}>
            <td className="flag"><button className="btn xs ghost" onClick={() => { onSelect && onSelect(h.node_id); onClose() }}>#{h.node_id}</button></td>
            <td>{(h.signals || []).map(s => s.signal).join(', ')}</td>
            <td className="muted">{(h.signals || []).map(s => s.detail).filter(Boolean).join(' · ')}</td>
            <td>{!readOnly && <button className="btn xs ghost" title="quarantine: abort this node so it can't be selected"
              onClick={() => quarantine(h.node_id)}>quarantine</button>}</td>
          </tr>)}</tbody></table></DataTable>
        : null}
      </div>
    </Panel>
  )
}

export function SensitivityPanel({ state, onClose, onSelect }) {
  // Aggregate ablation impacts across all ablate events (latest wins per param).
  const impacts = {}
  ;(state.ablations || []).forEach(a => Object.entries(a.impacts || {}).forEach(([k, v]) => { impacts[k] = Math.abs(v) }))
  const bars = Object.entries(impacts).map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value)
  return (
    <Panel title="Parameter sensitivity" onClose={onClose} wide>
      <div className="section-h">Ablation impact (|Δmetric| when param zeroed)</div>
      {bars.length ? <Bars data={bars} color="#9a6bff" /> : <div className="muted">No ablation events yet (enable ablate_every or use Force-ablate on a node).</div>}
      <div className="section-h">Parallel coordinates — params → metric</div>
      <ParallelCoords nodes={Object.values(state.nodes)} direction={state.direction}
        onPick={onSelect ? (id) => { onSelect(id); onClose && onClose() } : undefined} />
    </Panel>
  )
}

export function FailuresPanel({ state, onClose, onSelect }) {
  const failed = Object.values(state.nodes).filter(n => n.status === 'failed')
  const byReason = {}
  failed.forEach(n => { (byReason[n.error_reason || 'unknown'] ||= []).push(n) })
  return (
    <Panel title="Failures" sub={`${failed.length} failed`} onClose={onClose}>
      <div className="cardgrid" style={{ marginBottom: 12 }}>
        {Object.entries(byReason).map(([r, ns]) => <Stat key={r} n={ns.length} l={r} />)}
        {!failed.length && <Stat n={0} l="no failures" />}
      </div>
      <DataTable caption="Failed nodes and errors" card={false}><table className="tbl"><thead><tr><th>node</th><th>reason</th><th>error</th></tr></thead><tbody>
        {failed.map(n => <tr key={n.id}>
          <td><button type="button" className="btn xs ghost" onClick={() => { onSelect?.(n.id); onClose?.() }}>#{n.id}</button></td>
          <td className="flag">{n.error_reason}</td><td className="muted">{(n.error || '').slice(0, 80)}</td></tr>)}
      </tbody></table></DataTable>
    </Panel>
  )
}

// U1 · experiment queue: the search's planned/in-flight work, made VISIBLE and cancelable. Pending
// nodes (created, not yet evaluated) are the concrete queue — each cancelable via node_abort; queued
// control requests (injects/forks/confirm/ablate not yet materialized) show as read-only chips so
// the operator can see what's coming. Order is policy-driven (the engine picks next), so this is
// "see + cancel + add", not manual reordering (which no engine event supports).
export function QueuePanel({ state, runId, onSelect, onClose, onToast }) {
  const nodes = Object.values(state.nodes || {})
  const pending = nodes.filter(n => n.status === 'pending').sort((a, b) => a.id - b.id)
  const working = nodes.filter(n => n.status === 'running')   // (if the fold ever exposes it)
  const injects = (state.inject_requests || []).slice(state.injects_done || 0)
  const forks = (state.fork_requests || []).slice(state.forks_done || 0)
  const { confirms: confirmReq, ablates: ablateReq } = queuedGenerationControls(state)
  const cancel = async (id) => {
    try {
      const feedback = commandFeedback(await CONTROL.nodeAbort(runId, id, state.nodes?.[id]?.attempt), {
        success: `Cancelled #${id}`, noop: `#${id} was already settled`,
        executing: `Cancellation of #${id} requested — waiting for the run`, failure: `Could not cancel #${id}`,
      }); onToast?.(feedback.message)
    } catch (error) { onToast?.(`Could not cancel #${id}: ${error.message || error}`) }
  }
  const queuedCount = pending.length + injects.length + forks.length + confirmReq.length + ablateReq.length
  return (
    <Panel title="Queue" sub={`${queuedCount} planned / in-flight`} onClose={onClose}>
      <div className="muted" style={{ marginBottom: 10 }}>
        The next experiment is chosen by the search policy; this is the live work-list — cancel a
        pending experiment, or add one from the chat (<code>/experiment</code>) or a node’s “explore”.
      </div>
      <div className="section-h">Pending experiments {pending.length > 0 && <span className="pill">{pending.length}</span>}</div>
      {pending.length
        ? <DataTable caption="Pending experiment queue" card={false}><table className="tbl"><thead><tr><th>node</th><th>op</th><th>parents</th><th>hypothesis / rationale</th><th>action</th></tr></thead><tbody>
          {pending.map(n => <tr key={n.id}>
            <td><button className="btn xs ghost" onClick={() => { onSelect && onSelect(n.id); onClose() }}>#{n.id}</button></td>
            <td><span className="op-icon"><OpIcon name={operatorMeta(n.operator).icon} size={12} /></span> {n.operator}</td>
            <td className="muted">{(n.parent_ids || []).map(p => '#' + p).join(', ') || '—'}</td>
            <td className="muted">{stripMd(n.idea?.hypothesis || n.idea?.rationale || '').slice(0, 70)}</td>
            <td><button className="btn xs ghost" aria-label={`Cancel experiment ${n.id}`}
              title="cancel this experiment (node_abort)" onClick={() => cancel(n.id)}><OpIcon name="cross" size={11} /></button></td>
          </tr>)}
        </tbody></table></DataTable>
        : <div className="muted">No experiment is queued right now — the loop is idle or between picks.</div>}
      {(injects.length + forks.length + confirmReq.length + ablateReq.length) > 0 && <>
        <div className="section-h">Queued control requests</div>
        <div className="chips">
          {injects.map((q, i) => <span key={'i' + i} className="chip sm" title="operator-injected experiment awaiting materialization">inject: {(q.idea?.operator || 'experiment')}</span>)}
          {forks.map((q, i) => <span key={'f' + i} className="chip sm" title="fork awaiting materialization">fork #{q.from_node_id ?? q.parent_id ?? '?'}</span>)}
          {confirmReq.map(r => <span key={`c:${r.node_id}:${r.generation ?? 'legacy'}`} className="chip sm" title={r.generation == null ? undefined : `node generation ${r.generation}`}>confirm #{r.node_id}</span>)}
          {ablateReq.map(r => <span key={`a:${r.node_id}:${r.generation ?? 'legacy'}`} className="chip sm" title={r.generation == null ? undefined : `node generation ${r.generation}`}>ablate #{r.node_id}</span>)}
        </div>
      </>}
    </Panel>
  )
}

// I5 · non-dominated (Pareto-optimal) set over the primary metric (direction-aware) + every
// extra_metric (treated as cost-like / minimize). A node is Pareto-optimal if no other node is
// at-least-as-good on all objectives and strictly better on one.
function paretoFront(nodes, direction) {
  const keys = [...new Set(nodes.flatMap(n => Object.keys(n.extra_metrics || {})))]
  const vec = (n) => [direction === 'min' ? n.metric : -n.metric,
    ...keys.map(k => { const v = n.extra_metrics?.[k]; return v == null ? Infinity : v })]
  const dominates = (a, b) => { let strict = false; for (let i = 0; i < a.length; i++) { if (a[i] > b[i]) return false; if (a[i] < b[i]) strict = true } return strict }
  const pts = nodes.map(n => ({ n, v: vec(n) }))
  return { keys, front: pts.filter(p => !pts.some(q => q !== p && dominates(q.v, p.v))).map(p => p.n) }
}
export function ParetoPanel({ state, onClose, onSelect }) {
  const nodes = Object.values(state.nodes).filter(n => n.metric != null && n.feasible !== false)
  // first constraint dimension, if any
  const withV = nodes.filter(n => (n.violations || []).length || Object.keys(n.extra_metrics || {}).length)
  let scatter = null
  const cName = withV.length ? (withV[0].violations?.[0]?.name || Object.keys(withV[0].extra_metrics || {})[0]) : null
  if (cName) {
    const data = nodes.map(n => {
      const cv = (n.violations || []).find(v => v.name === cName)?.value ?? n.extra_metrics?.[cName]
      return cv == null ? null : { x: cv, y: n.confirmed_mean ?? n.metric, feasible: n.feasible !== false, id: n.id }
    }).filter(Boolean)
    scatter = <Scatter data={data} xlab={cName} ylab="metric"
      onPick={onSelect ? (id) => { onSelect(id); onClose && onClose() } : undefined} />
  }
  const archive = state.archive
  const ops = {}
  Object.values(state.nodes).forEach(n => { const o = (ops[n.operator] ||= { n: 0, ev: 0 }); o.n++; if (n.status === 'evaluated') o.ev++ })
  return (
    <Panel title="Pareto · Diversity · Operators" onClose={onClose} wide>
      {(() => {
        const { keys, front } = paretoFront(nodes, state.direction)
        return <>
          <div className="section-h">Pareto-optimal set (I5) {keys.length ? <span className="pill">{keys.length + 1} objectives</span> : <span className="pill">metric only</span>}</div>
          {keys.length
            ? <DataTable caption="Pareto-optimal node metrics" card={false}><table className="tbl"><thead><tr><th>node</th><th>metric</th>{keys.map(k => <th key={k}>{k}</th>)}</tr></thead><tbody>
                {front.sort((a, b) => (state.direction === 'min' ? a.metric - b.metric : b.metric - a.metric)).map(n =>
                  <tr key={n.id}><td>#{n.id}{n.id === state.best_node_id ? <OpIcon name="crown" size={10} /> : ''}</td><td>{fmt(n.confirmed_mean ?? n.metric)}</td>
                    {keys.map(k => <td key={k} className="muted">{fmt(n.extra_metrics?.[k])}</td>)}</tr>)}
              </tbody></table></DataTable>
            : <div className="muted">Single-objective task — the Pareto front is just the best node (#{state.best_node_id ?? '—'}). Add extra_metrics (e.g. latency, size) to trade off.</div>}
        </>
      })()}
      <div className="section-h">Pareto (metric vs constraint)</div>
      {scatter || <div className="muted">No constraints/aux metrics in this task.</div>}
      <div className="section-h">Diversity archive {archive && <span className="pill">{archive.niches} niches</span>}</div>
      {archive?.elites?.length
        ? <DataTable caption="Diversity archive elite nodes" card={false}><table className="tbl"><thead><tr><th>node</th><th>metric</th><th>params</th></tr></thead><tbody>
          {archive.elites.map((e, i) => <tr key={i}><td>#{e.node_id}</td><td>{fmt(e.metric)}</td><td className="muted">{JSON.stringify(e.params)}</td></tr>)}</tbody></table></DataTable>
        : <div className="muted">No archive (run not finished).</div>}
      <div className="section-h">Operator productivity</div>
      <DataTable caption="Operator productivity summary" card={false}><table className="tbl"><thead><tr><th>operator</th><th>nodes</th><th>evaluated</th></tr></thead><tbody>
        {Object.entries(ops).map(([o, s]) => <tr key={o}><td>{o}</td><td>{s.n}</td><td>{s.ev}</td></tr>)}</tbody></table></DataTable>
    </Panel>
  )
}

export function DataQualityPanel({ state, onClose }) {
  const prof = state.data_profile
  if (!prof) return <Panel title="Data quality" onClose={onClose}><div className="muted">No data profile (task exposes no dataset).</div></Panel>
  const cols = Object.entries(prof)
  return (
    <Panel title="Data quality" sub={`${cols.length} columns`} onClose={onClose} wide>
      <DataTable caption="Dataset column quality profile" card={false}><table className="tbl"><thead><tr><th>column</th><th>dtype</th><th>missing%</th><th>unique</th><th>min</th><th>max</th><th>mean</th><th>flags</th></tr></thead><tbody>
        {cols.map(([c, s]) => <tr key={c}>
          <td>{c}</td><td>{s.dtype}</td><td>{fmt((s.missing_frac || 0) * 100, 3)}</td><td>{fmtInt(s.n_unique)}</td>
          <td>{fmt(s.min)}</td><td>{fmt(s.max)}</td><td>{fmt(s.mean)}</td>
          <td>{s.constant && <span className="flag">constant </span>}{s.high_missing && <span className="flag">high-missing</span>}</td></tr>)}
      </tbody></table></DataTable>
    </Panel>
  )
}

// Per-run config: shows the run's config.snapshot.json and lets you EDIT it. Edits are saved back to
// the snapshot, which a later RESUME re-reads (resume does NOT pick up the UI's global new-run
// defaults), so this is how you change a specific run's settings (e.g. raise `timeout`, enable timeout
// repair). Works for live runs too: saving the snapshot is safe mid-run (the engine never re-reads it),
// and a "Pause & resume" applies it now by restarting the engine (pause → wait for it to stop → resume).
export function ConfigPanel({ runId, state, live, onClose: closePanel, onToast }) {
  const [cfg, setCfg] = useState(null)
  const [settingsSchema, setSettingsSchema] = useState(null)
  const [form, setForm] = useState(null)
  const [loadError, setLoadError] = useState('')
  const [loadNonce, setLoadNonce] = useState(0)
  const [saved, setSaved] = useState(null)   // last-persisted form (to detect unsaved edits)
  const [agentControl, setAgentControl] = useState({})   // per-run governance matrix (agent_control)
  const [savedAC, setSavedAC] = useState({})
  const [configMeta, setConfigMeta] = useState({
    configRevision: '', pinnedFields: new Set(), readOnlyFields: new Set(), mismatchFields: [],
  })
  const [sec, setSec] = useState('')
  const [busy, setBusy] = useState(false)
  const [raw, setRaw] = useState(false)
  const [configMutationUnknown, setConfigMutationUnknown] = useState(null)
  const [invalidFocus, setInvalidFocus] = useState({ key: '', request: 0 })
  const loadGenerationRef = useRef(0)
  const mutationRef = useRef(null)
  const allowConfigNavigationRef = useRef(false)
  useEffect(() => {
    const generation = ++loadGenerationRef.current
    const configRequest = deadlineRequest(
      signal => get(runApiPath(runId, '/config'), { signal }), PANEL_REQUEST_TIMEOUT_MS,
    )
    // A reused panel must never display or reconcile the previous run while the next config loads.
    mutationRef.current = null
    setBusy(false); setCfg(null); setSettingsSchema(null); setForm(null); setSaved(null); setLoadError('')
    setConfigMutationUnknown(null)
    setAgentControl({}); setSavedAC({})
    setConfigMeta({ configRevision: '', pinnedFields: new Set(), readOnlyFields: new Set(), mismatchFields: [] })
    Promise.all([
      configRequest.promise,
      loadSettingsSchema({ reload: loadNonce > 0 }),
    ]).then(([c, nextSchema]) => {
      if (configRequest.controller.signal.aborted || generation !== loadGenerationRef.current) return
      const parsed = splitRunConfigPayload(c, nextSchema)
      setCfg(parsed.config); setConfigMeta(parsed)
      setSettingsSchema(nextSchema)
      const f = toForm(parsed.config, nextSchema); setForm(f); setSaved(f)
      const ac = parsed.config.agent_control || {}; setAgentControl(ac); setSavedAC(ac)
    }).catch(error => {
      if (error?.name !== 'AbortError' && generation === loadGenerationRef.current) {
        setCfg(null)
        setLoadError('Run settings could not be loaded. Check the connection and retry.')
      }
    })
    return () => configRequest.controller.abort()
  }, [runId, loadNonce])

  // A live engine keeps its in-memory settings until it restarts; gate on `live` (not the possibly
  // historical `state`) so time-travel doesn't misreport liveness.
  const engineLive = live?.engine_running === true
  const controlBusy = busy || !!configMutationUnknown
  const resumeLabels = { success: 'Resumed with the saved settings', noop: 'Run was already running',
    executing: 'Resume requested — waiting for the engine to load the saved settings', failure: 'Resume failed' }
  const restartLabels = { success: 'Restarted with the saved settings', noop: 'Restart was already satisfied',
    executing: 'Restart requested — the current experiment will stop before a replacement engine loads the saved settings',
    failure: 'Restart failed' }
  const acceptResume = async (expectedGeneration = loadGenerationRef.current, requestedRunId = runId) => {
    const record = await CONTROL.resume(requestedRunId)
    if (expectedGeneration !== loadGenerationRef.current) return null
    const feedback = commandFeedback(record, resumeLabels)
    onToast(feedback.message)
    return feedback
  }
  const dirty = useMemo(() => {
    if (!form || !saved || !settingsSchema) return new Set()
    const cur = fromForm(form, settingsSchema, { allowClear: false })
    const base = fromForm(saved, settingsSchema, { allowClear: false }), s = new Set()
    for (const k of Object.keys(settingsSchema.fieldByKey)) {
      if (JSON.stringify(cur[k]) !== JSON.stringify(base[k])) s.add(k)
    }
    return s
  }, [form, saved, settingsSchema])
  const acDirty = useMemo(() => JSON.stringify(agentControl) !== JSON.stringify(savedAC), [agentControl, savedAC])
  const validationErrors = useMemo(() => form && settingsSchema
    ? settingsValidationErrors(form, settingsSchema, { allowClear: false }) : {}, [form, settingsSchema])
  const invalidCount = Object.keys(validationErrors).length
  const hasChanges = dirty.size > 0 || acDirty
  const canSave = hasChanges && invalidCount === 0
  const configNavigationUnsafe = hasChanges || busy || !!configMutationUnknown
  useEffect(() => {
    if (!configNavigationUnsafe) {
      allowConfigNavigationRef.current = false
      return undefined
    }
    const guardedHash = location.hash
    return installNavigationLossGuard({
      allowRef: allowConfigNavigationRef,
      guardedHash,
      message: () => {
        const warning = configMutationUnknown?.stage === 'conflict'
          ? 'The server version changed while this draft was open.'
          : configMutationUnknown
            ? 'The last run-settings save may or may not have reached the server.'
          : busy ? 'A run-settings operation is still in progress.'
            : 'This run-settings panel has unsaved changes.'
        return `${warning} Leave this run anyway?`
      },
    })
  }, [configNavigationUnsafe, busy, configMutationUnknown])
  const onChange = (k, v) => setForm(f => ({ ...f, [k]: v }))
  const onToggleAgent = (key, role) => setAgentControl(ac => {
    const cur = new Set(ac[key] || []); cur.has(role) ? cur.delete(role) : cur.add(role)
    return { ...ac, [key]: [...cur] }
  })
  const beginMutation = (kind = 'control') => {
    if (mutationRef.current || (configMutationUnknown && kind !== 'reconciling')) return null
    const token = { generation: loadGenerationRef.current, kind }
    mutationRef.current = token
    setBusy(true)
    return token
  }
  const finishMutation = token => {
    if (mutationRef.current !== token) return
    mutationRef.current = null
    if (token.generation === loadGenerationRef.current) setBusy(false)
  }
  const focusFirstInvalid = () => {
    const key = Object.keys(validationErrors)[0]
    if (!key) return
    setRaw(false)
    setInvalidFocus(previous => ({ key, request: previous.request + 1 }))
  }
  const rememberUnknownSave = (stage, submittedForm, submittedControl, submittedRunId, generation,
    uncertainKeys, uncertainControlKeys) => {
    if (generation !== loadGenerationRef.current || submittedRunId !== runId) return
    setConfigMutationUnknown({
      stage,
      runId: submittedRunId,
      generation,
      submittedForm: publicConfigForm(toForm(
        fromForm(submittedForm, settingsSchema, { allowClear: false }), settingsSchema)),
      submittedControl,
      uncertainKeys,
      uncertainControlKeys,
    })
    onToast(stage === 'conflict'
      ? 'Run settings changed elsewhere. Load the current server version and review your retained draft.'
      : 'Save outcome unknown. Refresh the server state before making another change.')
  }
  const reconcileUnknownSave = async () => {
    const recovery = configMutationUnknown
    if (!recovery) return
    const mutation = beginMutation('reconciling')
    if (!mutation) return
    const request = deadlineRequest(
      signal => get(runApiPath(recovery.runId, '/config'), { signal }), PANEL_REQUEST_TIMEOUT_MS,
    )
    try {
      const response = await request.promise
      if (mutation.generation !== loadGenerationRef.current || recovery.runId !== runId) return
      const parsed = splitRunConfigPayload(response, settingsSchema)
      const acceptedForm = toForm(parsed.config, settingsSchema)
      const acceptedControl = parsed.config.agent_control || {}
      setCfg(parsed.config); setConfigMeta(parsed); setSaved(acceptedForm); setSavedAC(acceptedControl)
      setForm(current => reconcileUnknownRecord(
        current, recovery.submittedForm, acceptedForm, recovery.uncertainKeys,
      ))
      setAgentControl(current => reconcileUnknownRecord(
        current, recovery.submittedControl, acceptedControl, recovery.uncertainControlKeys,
      ))
      setConfigMutationUnknown(null)
      onToast(recovery.stage === 'conflict'
        ? 'Current server settings loaded. Review the retained draft before saving again.'
        : 'Server state refreshed. The uncertain save was not replayed.')
    } catch (error) {
      if (mutation.generation === loadGenerationRef.current && recovery.runId === runId) {
        onToast('Server state is still unavailable: ' + (error.message || error))
      }
    } finally {
      finishMutation(mutation)
    }
  }
  const onSave = async () => {
    if (invalidCount) {
      focusFirstInvalid()
      onToast('Fix invalid settings before saving')
      return
    }
    const submittedForm = form
    const submittedControl = agentControl
    const submittedRevision = configMeta.configRevision
    const cur = fromForm(submittedForm, settingsSchema, { allowClear: false }), changed = {}
    for (const k of dirty) changed[k] = cur[k]    // send ONLY edited fields (minimal snapshot diff)
    if (acDirty) changed.agent_control = submittedControl
    if (!Object.keys(changed).length) return
    const mutation = beginMutation('save')
    if (!mutation) return
    const submittedRunId = runId
    try {
      const write = deadlineRequest(
        signal => saveRunConfig(submittedRunId, changed, {
          signal, expectedRevision: submittedRevision,
        }), PANEL_REQUEST_TIMEOUT_MS,
      )
      const r = validateRunConfigSaveAck(await write.promise, settingsSchema)
      if (mutation.generation !== loadGenerationRef.current) return
      const parsed = splitRunConfigPayload(r.config, settingsSchema)
      const acceptedForm = toForm(parsed.config, settingsSchema)
      const acceptedControl = parsed.config.agent_control || {}
      setCfg(parsed.config); setConfigMeta(parsed); setSaved(acceptedForm); setSavedAC(acceptedControl)
      setForm(current => reconcileAcceptedRecord(current, submittedForm, acceptedForm))
      setAgentControl(current => reconcileAcceptedRecord(current, submittedControl, acceptedControl))
      const repaired = r.normalized_pinned?.length
        ? `; repaired legacy snapshot drift in ${r.normalized_pinned.join(', ')}` : ''
      const what = (r.changed?.length ? `saved ${r.changed.join(', ')}` : 'saved') + repaired
      onToast(what + (r.engine_running ? ' — applies when the live run restarts' : ' — applies on next resume'))
    } catch (e) {
      const disposition = runConfigWriteDisposition(e)
      if (disposition === 'conflict') {
        rememberUnknownSave(
          'conflict', submittedForm, submittedControl, submittedRunId, mutation.generation,
          Object.keys(changed).filter(key => key !== 'agent_control'),
          acDirty ? [...new Set([...Object.keys(submittedControl), ...Object.keys(savedAC)])] : [],
        )
      } else if (disposition === 'unknown') {
        rememberUnknownSave(
          'unknown', submittedForm, submittedControl, submittedRunId, mutation.generation,
          Object.keys(changed).filter(key => key !== 'agent_control'),
          acDirty ? [...new Set([...Object.keys(submittedControl), ...Object.keys(savedAC)])] : [],
        )
      } else if (mutation.generation === loadGenerationRef.current) {
        onToast('save failed: ' + e.message)
      }
    } finally { finishMutation(mutation) }
  }
  const onResume = async () => {           // stalled/finished: just spawn the engine (re-reads the snapshot)
    const mutation = beginMutation()
    if (!mutation) return
    try {
      await acceptResume(mutation.generation, runId)
    } catch (e) {
      if (mutation.generation === loadGenerationRef.current) onToast('Resume failed: ' + e.message)
    }
    finally { finishMutation(mutation) }
  }
  const onPauseResume = async () => {
    const mutation = beginMutation()
    if (!mutation) return
    const submittedRunId = runId
    try {
      // This is one durable command/postcondition. Never restore a client-side
      // pause-then-resume saga here: unmounting between commands would strand the accepted intent.
      const record = await CONTROL.restart(submittedRunId)
      if (mutation.generation !== loadGenerationRef.current) return
      const feedback = commandFeedback(record, restartLabels)
      onToast(feedback.message)
    } catch (e) {
      if (mutation.generation === loadGenerationRef.current) onToast('Pause/resume failed: ' + e.message)
    }
    finally { finishMutation(mutation) }
  }
  const extendBudget = async () => {
    if (!sec || controlBusy) return
    const mutation = beginMutation()
    if (!mutation) return
    const submittedRunId = runId
    const submittedSeconds = sec
    try {
      const record = await CONTROL.budget(submittedRunId, Number(submittedSeconds))
      if (mutation.generation !== loadGenerationRef.current) return
      const feedback = commandFeedback(record, {
        success: `Budget extended +${submittedSeconds}s`, noop: 'That budget extension was already applied',
        executing: `Budget extension +${submittedSeconds}s requested — waiting for the run`, failure: 'Budget extension failed',
      }); onToast(feedback.message)
    } catch (error) {
      if (mutation.generation === loadGenerationRef.current) onToast(`Budget extension failed: ${error.message || error}`)
    }
    finally { finishMutation(mutation) }
  }
  const requestClose = () => {
    if (!hasChanges && !busy && !configMutationUnknown) { closePanel(); return }
    const warning = configMutationUnknown?.stage === 'conflict'
      ? 'The server version changed while this draft was open.'
      : configMutationUnknown
        ? 'The last save may or may not have reached the server.'
      : busy ? 'A settings operation is still in progress.' : 'This panel has unsaved changes.'
    if (window.confirm(`${warning} Close the run settings panel anyway?`)) {
      allowConfigNavigationRef.current = true
      closePanel()
    }
  }
  // PanelShell routes Escape, backdrop clicks, and its close button through this single guard.
  const onClose = requestClose

  const rawTable = <DataTable caption="Raw run configuration" card={false}><table className="tbl"><tbody>{cfg && Object.entries(cfg).map(([k, v]) =>
    <tr key={k}><th scope="row" className="muted">{k}</th><td>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</td></tr>)}</tbody></table></DataTable>

  return (
    <Panel title="Run settings" sub={engineLive ? 'live · applies on restart' : 'edit · applies on resume'} onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <span className="muted">extend eval budget:</span>
        <input className="text" style={{ width: 120 }} aria-label="Additional evaluation budget in seconds"
          placeholder="seconds" value={sec} onChange={e => setSec(e.target.value)} />
        <button className="btn sm primary" disabled={!sec || controlBusy} onClick={extendBudget}>apply</button>
      </div>
      {configMutationUnknown && <div className="report-inline-state error" role="alert" style={{ marginBottom: 12 }}>
        <OpIcon name="alert" size={14} />
        {configMutationUnknown.stage === 'conflict'
          ? <span><b>Run settings changed elsewhere.</b> Load the current server version, then review
              your retained draft before saving it against the new version.</span>
          : <span><b>Save outcome unknown.</b> The request timed out or lost its response. Refresh the
              authoritative server state; this client will not replay the save automatically.</span>}
        <button className="btn sm" disabled={busy} onClick={reconcileUnknownSave}>
          {configMutationUnknown.stage === 'conflict' ? 'Load current version' : 'Refresh server state'}
        </button>
      </div>}
      {!form || !settingsSchema ? (loadError
        ? <div className="report-inline-state error" role="alert">
            <OpIcon name="alert" size={14} /><span>{loadError}</span>
            <button className="btn sm" onClick={() => setLoadNonce(value => value + 1)}>Retry</button>
          </div>
        : <div className="muted" role="status">Loading run settings…</div>) : <>
        <div className="notice" style={{ marginBottom: 10 }}>
          {engineLive
            ? <>This run is <b>live</b>. Saving updates its <code>config.snapshot.json</code>, but the running engine keeps its current settings until it restarts — use <b>Pause &amp; resume</b> to stop it (the current experiment finishes first) and continue with the new settings.</>
            : <>Edits are saved to this run's <code>config.snapshot.json</code> and applied on the next <b>resume</b>.</>}
          {' '}<span className="sf-dot unsaved">●</span> = changed.
        </div>
        {configMeta.pinnedFields.size > 0 && <div className="notice" role="note" style={{ marginBottom: 10 }}>
          Fields marked <b>launch-pinned</b> show the values recorded in this run's event log and cannot
          be changed on resume. Start a new run to change holdout or verifier semantics.
          {configMeta.mismatchFields.length > 0 && <>
            {' '}A legacy snapshot disagrees for {configMeta.mismatchFields.join(', ')}; the effective
            launch values are shown and will be repaired when another editable setting is saved.
          </>}
        </div>}
        <div className="toolbar" style={{ marginBottom: 10 }}>
          <span className="spacer" style={{ flex: 1 }} />
          <button className="btn sm ghost" onClick={() => setRaw(r => !r)}>{raw ? 'form' : 'raw'}</button>
          {invalidCount > 0 && <button type="button"
            className="settings-summary-link settings-save-state is-invalid"
            onClick={focusFirstInvalid}>
            {invalidCount} invalid numeric setting{invalidCount === 1 ? '' : 's'} — review
          </button>}
          <button className="btn sm ghost" disabled={controlBusy || !hasChanges} onClick={() => { setForm(saved); setAgentControl(savedAC) }}>↺ revert</button>
          <button className="btn sm primary" disabled={controlBusy || !canSave} onClick={onSave}>Save</button>
          {engineLive
            ? <button className="btn sm" disabled={controlBusy || hasChanges} onClick={onPauseResume} title="pause the run, then resume it with the saved settings">Pause &amp; resume ▸</button>
            : <button className="btn sm" disabled={controlBusy || hasChanges} onClick={onResume} title="continue this run with the saved settings">Resume ▸</button>}
        </div>
        {/* This panel's `dirty` is changed-vs-saved (unsaved), so feed it as `unsaved` → the amber dot that clears on Save. */}
        {raw ? rawTable : <SettingsForm form={form} onChange={onChange} unsaved={dirty}
          errors={validationErrors} agentControl={agentControl} onToggleAgent={onToggleAgent}
          readOnlyKeys={configMeta.readOnlyFields} hideSecret schema={settingsSchema}
          focusKey={invalidFocus.key} focusRequest={invalidFocus.request} />}
      </>}
    </Panel>
  )
}

export function AuthoringPanel({ onClose, onToast }) {
  const [kind, setKind] = useState('prompts')
  const [sel, setSel] = useState(null)
  const [text, setText] = useState('')
  const [source, retry] = usePanelResource(signal => get(`/api/${kind}`, { signal }), authoringPayload, kind)
  const data = source.data || { dir: null, files: [] }
  return (
    <Panel title="Authoring — configure the scientist" sub="hot-reloaded next run" onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 10 }}>
        {['prompts', 'skills', 'knowledge'].map(k => <button key={k} className={'btn sm' + (k === kind ? ' primary' : '')}
          onClick={() => { setKind(k); setSel(null); setText('') }}>{k}</button>)}
        {source.state === 'ready' && <span className="muted">{data.dir || `no ${kind} dir configured (set LOOPLAB_${kind.toUpperCase()}_DIR)`}</span>}
      </div>
      <PanelResourceNotice resource={source} label={`${kind} files`} onRetry={retry} />
      <div className="authoring-layout">
        <div className="authoring-list">
          {(data.files || []).map(f => <button type="button" key={f.name}
            className={'run-card authoring-file' + (sel === f.name ? ' sel' : '')}
            onClick={() => { setSel(f.name); setText(f.text) }}>{f.name}</button>)}
          {source.state === 'ready' && !data.files?.length && <div className="muted">no files</div>}
        </div>
        <div className="authoring-editor">
          {sel ? <>
            <textarea className="text" aria-label={`Edit ${sel}`} value={text} onChange={e => setText(e.target.value)} />
            <button className="btn sm primary" style={{ marginTop: 8 }} onClick={async () => { await putText(`/api/${kind}/${sel}`, text); onToast('saved ' + sel) }}>Save</button>
          </> : source.state === 'ready' && <div className="muted">select a file to edit</div>}
        </div>
      </div>
    </Panel>
  )
}

function KbNote({ note }) {
  const [open, setOpen] = useState(false)
  return <div className="mem-card">
    <button type="button" className="memory-note-toggle disclosure-button" aria-expanded={open}
      onClick={() => setOpen(o => !o)}>
      <span style={{ opacity: 0.6, fontSize: 10, marginRight: 4 }}>{open ? '▾' : '▸'}</span>{note.name}</button>
    {open && <div style={{ marginTop: 6 }}><Markdown text={note.text || note.content || ''} /></div>}
  </div>
}

export function MemoryPanel({ onClose }) {
  // Everything the run has LEARNED, in one place: distilled lessons, solved-task cases, meta-notes, and
  // the agentic knowledge-base markdown notes (best configs / recipes the agents save + later retrieve).
  const [memory, retryMemory] = usePanelResource(signal => get('/api/memory', { signal }), memoryPayload)
  const [knowledge, retryKnowledge] = usePanelResource(signal => get('/api/knowledge', { signal }), authoringPayload)
  const mem = memory.data || { dir: null, cases: [], lessons: [], notes: [] }
  const kb = knowledge.data || { dir: null, files: [] }   // /api/knowledge → {dir, files:[{name,text}]}
  const [tab, setTab] = useState('lessons')
  const [lessonRole, setLessonRole] = useState('all')      // §role-split: Researcher vs Developer lessons
  const kbFiles = kb.files || []
  const tabs = [['lessons', 'Lessons', mem.lessons?.length], ['cases', 'Cases', mem.cases?.length],
    ['notes', 'Notes', mem.notes?.length], ['knowledge', 'Knowledge', kbFiles.length]]
  const selectedResource = tab === 'knowledge' ? knowledge : memory
  return (
    <Panel title="Memory & knowledge — what the runs have learned" sub={memory.data ? (mem.dir || 'no memory dir') : ''} onClose={onClose} wide>
      <div className="conv-toggle" style={{ marginBottom: 12 }}>
        {tabs.map(([k, label, n]) => <button key={k} aria-pressed={tab === k} className={'seg' + (tab === k ? ' on' : '')}
          onClick={() => setTab(k)}>{label} <span className="muted">{(k === 'knowledge' ? knowledge : memory).data ? n : '…'}</span></button>)}
      </div>
      <PanelResourceNotice resource={selectedResource} label={tab === 'knowledge' ? 'Knowledge notes' : 'Cross-run memory'}
        onRetry={tab === 'knowledge' ? retryKnowledge : retryMemory} />
      {/* General orientation shown on every tab; the role-split detail (§role-split) is lessons-only. */}
      <div className="muted" style={{ fontSize: 11, marginBottom: 10, lineHeight: 1.5 }}>
        Cross-run memory reused to guide future runs. Cases, notes and the knowledge base are shared;
        {' '}<b>lessons are split by role</b>.
      </div>
      {tab === 'lessons' && <div className="muted" style={{ fontSize: 11, marginBottom: 10, lineHeight: 1.5 }}>
        The <b>Researcher</b> gets R&D / “what technique to try” lessons; the <b>Developer</b> gets only
        its own “what code change fixed a crash” lessons (untagged/legacy lessons are shared).
      </div>}
      {tab === 'lessons' && <div className="conv-toggle" style={{ marginBottom: 8 }}>
        {[['all', 'All'], ['researcher', 'Researcher'], ['developer', 'Developer']].map(([r, label]) =>
          <button key={r} aria-pressed={lessonRole === r} className={'seg' + (lessonRole === r ? ' on' : '')}
            onClick={() => setLessonRole(r)}>{label}</button>)}
      </div>}
      {tab === 'lessons' && (() => {
        // Researcher/Developer filters ALSO include untagged (shared) lessons — mirrors the backend
        // routing where an untagged lesson reaches both roles.
        const shown = (mem.lessons || []).filter(l => lessonRole === 'all' || !l.role || l.role === lessonRole)
        return shown.length
          ? shown.map((l, i) => <div key={i} className="mem-card">
              <div>{l.statement}</div>
              <div className="mem-meta" style={{ marginTop: 4, display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                <span className="chip xs">{l.role || 'shared'}</span>
                {l.kind && <span className="chip xs">{l.kind}</span>}
                {l.outcome && <span className="chip xs">{l.outcome}</span>}
                {l.delta != null && <span className={'chip xs' + (l.delta > 0 ? ' ok' : '')}>Δ{fmt(l.delta)}</span>}
                {l.confidence != null && <span className="muted" style={{ fontSize: 11 }}>conf {Math.round(l.confidence * 100)}%</span>}
                {l.evidence_count ? <span className="muted" style={{ fontSize: 11 }}>· {l.evidence_count} evidence</span> : null}
                {l.task_id && <span className="muted" style={{ fontSize: 11 }}>· {l.task_id}</span>}
              </div>
            </div>)
          : memory.state === 'ready' && <div className="muted">No {lessonRole === 'all' ? '' : lessonRole + ' '}lessons yet — they accrue as runs finish (reflection distils them into memory).</div>
      })()}
      {tab === 'cases' && ((mem.cases || []).length
        ? <DataTable caption="Stored memory cases" card={false}><table className="tbl"><thead><tr><th>task</th><th>goal</th><th>metric</th><th>params</th></tr></thead><tbody>
          {mem.cases.map((c, i) => <tr key={i}><td>{c.task_id}</td><td className="muted">{c.goal}</td><td>{fmt(c.metric)}</td><td className="muted">{JSON.stringify(c.params)}</td></tr>)}</tbody></table></DataTable>
        : memory.state === 'ready' && <div className="muted">No cases stored.</div>)}
      {tab === 'notes' && ((mem.notes || []).length
        ? mem.notes.map((n, i) => <div key={i} className="mem-card">
            {n.task_id && <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>{n.task_id}</div>}
            <Markdown text={n.note || n.statement || JSON.stringify(n)} /></div>)
        : memory.state === 'ready' && <div className="muted">No meta-notes yet.</div>)}
      {tab === 'knowledge' && (kbFiles.length
        ? <><div className="muted" style={{ fontSize: 11, marginBottom: 6 }}>{kb.dir} — agents save + retrieve these via kb_search</div>
          {kbFiles.map((n, i) => <KbNote key={i} note={n} />)}</>
        : knowledge.state === 'ready' && <div className="muted">No knowledge notes ({kb.dir || 'no knowledge dir'}).</div>)}
    </Panel>
  )
}

export function RegistryPanel({ state, onClose }) {
  const [resource, retry] = usePanelResource(signal => get('/api/runs', { signal }), runsPayload)
  const runs = resource.data || []
  const champ = state.champion != null ? state.nodes[state.champion] : (state.best_node_id != null ? state.nodes[state.best_node_id] : null)
  return (
    <Panel title="Solution registry & cross-run" onClose={onClose} wide>
      <div className="section-h">Champion (this run)</div>
      {champ ? <div className="kv"><div className="k">node</div><div className="v">#{champ.id} {state.champion != null ? '(promoted)' : '(auto-best)'}</div>
        <div className="k">metric</div><div className="v">{fmt(champ.confirmed_mean ?? champ.metric)}</div></div> : <div className="muted">no champion yet</div>}
      <div className="toolbar" style={{ marginTop: 6 }}>
        <button className="btn sm" onClick={async () => {
          const p = await get(runApiPath(state.run_id, '/prov'))
          const blob = new Blob([JSON.stringify(p, null, 2)], { type: 'application/json' })
          const a = document.createElement('a'); a.href = URL.createObjectURL(blob)
          a.download = `${state.run_id}_prov.json`; a.click(); URL.revokeObjectURL(a.href)
        }}><OpIcon name="download" size={12} /> W3C-PROV graph (JSON)</button>
      </div>
      <div className="section-h">Promotions</div>
      {(state.promotions || []).length
        ? <DataTable caption="Promoted solution nodes" card={false}><table className="tbl"><thead><tr><th>node</th><th>alias</th></tr></thead><tbody>{state.promotions.map((p, i) => <tr key={i}><td>#{p.node_id}</td><td>{p.alias || 'champion'}</td></tr>)}</tbody></table></DataTable>
        : <div className="muted">none — use Promote on a node</div>}
      <div className="section-h">Cross-run leaderboard</div>
      <PanelResourceNotice resource={resource} label="Cross-run leaderboard" onRetry={retry} />
      {runs.length > 0 && <DataTable caption="Cross-run solution leaderboard" card={false}><table className="tbl"><thead><tr><th>run</th><th>task</th><th>phase</th><th>best</th><th>nodes</th></tr></thead><tbody>
        {[...runs].sort((a, b) => (b.best_confirmed ?? b.best_metric ?? -Infinity) - (a.best_confirmed ?? a.best_metric ?? -Infinity))
          .map(r => <tr key={r.run_id}><td>{r.run_id}</td><td className="muted">{r.task_id}</td><td>{r.phase}</td><td>{fmt(r.best_confirmed ?? r.best_metric)}</td><td>{r.nodes}</td></tr>)}
      </tbody></table></DataTable>}
      {resource.state === 'ready' && !runs.length && <div className="muted">No runs in the registry yet.</div>}
    </Panel>
  )
}

// Live GPU telemetry (nvidia-smi via /api/gpu). Polls while open so an operator can watch
// utilization / VRAM / power during a real training run without leaving the browser.
export function GpuPanel({ onClose }) {
  const [resource, retry] = usePanelResource(signal => get('/api/gpu', { signal }), gpuPayload, '', 2000)
  const data = resource.data
  const bar = (v, max, hot) => <div className="bar" style={{ height: 8 }}>
    <div className={'fill' + (hot ? ' hot' : '')} style={{ width: Math.min(100, max ? v / max * 100 : 0) + '%' }} /></div>
  return (
    <Panel title="GPU monitor" sub="nvidia-smi · live" onClose={onClose} wide>
      <PanelResourceNotice resource={resource} label="GPU telemetry" onRetry={retry} />
      {data && !data.available
        ? <div className="notice">No GPU / nvidia-smi not available on the server host.</div>
        : data && !data.gpus.length
          ? <div className="notice">No GPU devices reported.</div>
        : data?.available && (data.gpus || []).map((g, i) => (
            <div key={i} style={{ marginBottom: 16 }}>
              <div className="section-h">{g.name}</div>
              <div className="cardgrid" style={{ marginBottom: 10 }}>
                <Stat n={`${fmt(g.util)}%`} l="utilization" />
                <Stat n={`${fmt(g.mem_used)} / ${fmt(g.mem_total)} MiB`} l="memory" />
                <Stat n={`${fmt(g.temp)}°C`} l="temperature" />
                <Stat n={`${fmt(g.power)} W`} l="power draw" />
              </div>
              <div className="kv">
                <div className="k">GPU util</div><div className="v">{bar(g.util, 100, true)}</div>
                <div className="k">VRAM</div><div className="v">{bar(g.mem_used, g.mem_total)}</div>
              </div>
            </div>))}
    </Panel>
  )
}

// F1 · Global hyperparameter importance — across ALL evaluated feasible nodes in the run, how
// strongly does each numeric param predict the metric (|Pearson r|)? The per-node Sensitivity panel
// is local (one ablation); this is the run-wide W&B-style "which knobs matter" view. Pure UI.
// The computation lives in report.js::hyperImportance (shared with the Report's Learnings section).
export function HyperImportancePanel({ state, onClose }) {
  const nodes = Object.values(state.nodes).filter(n => n.status === 'evaluated' && n.metric != null && n.feasible !== false)
  const rows = hyperImportance(state)
  const top = rows[0]?.imp || 1
  return (
    <Panel title="Hyperparameter importance" sub={`${nodes.length} evaluated`} onClose={onClose}>
      {rows.length
        ? <><div className="section-h">|correlation| of each param with the metric (run-wide)</div>
            <DataTable caption="Run-wide hyperparameter importance" card={false}><table className="tbl"><thead><tr><th>param</th><th>importance</th><th>r</th><th>n</th><th>relative importance</th></tr></thead><tbody>
              {rows.map(row => <tr key={row.k}>
                <td>{row.k}</td><td>{fmt(row.imp, 3)}</td>
                <td className="muted">{row.r >= 0 ? '+' : ''}{fmt(row.r, 3)}</td><td className="muted">{row.n}</td>
                <td style={{ width: 160 }}><div className="bar" style={{ height: 8 }}>
                  <div className="fill" style={{ width: Math.min(100, row.imp / top * 100) + '%' }} /></div></td></tr>)}
            </tbody></table></DataTable>
            <div className="muted" style={{ marginTop: 8 }}>Sign of r shows direction: with a {state.direction === 'min' ? 'minimize' : 'maximize'} objective,
              a {state.direction === 'min' ? 'negative' : 'positive'} r means a larger value tends to help. Needs ≥3 evaluated nodes per param.</div></>
        : <div className="muted">Not enough numeric-param data yet — run more experiments (≥3 evaluated nodes that share a numeric param).</div>}
    </Panel>
  )
}

// # CODEX AGENT: Same-task IDs are an operational lookup key, not a ComparisonContract. Keep this
// legacy panel useful for navigation without ranking raw objectives whose metric unit, dataset/eval
// identity, and protocol are absent from /api/runs. The Research Atlas owns contract-bound comparison.
const CROSS_RUN_OBSERVATION_LIMIT = 100

export function CrossRunPanel({ state, onClose }) {
  const [resource, retry] = usePanelResource(signal => get('/api/runs', { signal }), runsPayload)
  const runs = resource.data || []
  const task = typeof state.task_id === 'string' && state.task_id.trim() ? state.task_id : ''
  // CODEX AGENT: an absent task identity is not a shared identity. Never combine legacy rows merely
  // because they all encode the missing value as an empty string.
  const observations = (task ? runs.filter(r => r.task_id === task) : [])
    .map(r => ({ ...r, m: r.best_confirmed ?? r.best_metric }))
    .filter(r => r.m != null)
  const rows = observations.slice(0, CROSS_RUN_OBSERVATION_LIMIT)
  const hidden = observations.length - rows.length
  return (
    <Panel title="Same-task run observations"
      sub={resource.data ? `${observations.length} metric observation${observations.length === 1 ? '' : 's'}` : ''}
      onClose={onClose} wide>
      <PanelResourceNotice resource={resource} label="Cross-run results" onRetry={retry} />
      {resource.data && <div className="panel-resource-toolbar">
        <span className="muted">task ID:</span><code>{task || 'not recorded'}</code>
        <span className="muted">{rows.length} shown · comparison unavailable</span>
      </div>}
      {resource.data && (task
        ? <div className="notice resource-warning" role="status">
            <b>Cross-run ranking unavailable.</b>
            <span> A shared task ID does not bind metric name/unit, dataset and evaluation identity, or a
              comparison protocol. Values below remain per-run observations.</span>
          </div>
        : <div className="notice resource-warning" role="status">
            <b>Same-task observations unavailable.</b>
            <span> This run has no recorded task ID, so no portfolio row can be bound to it. Rows with
              missing identities are never grouped.</span>
          </div>)}
      {rows.length
        ? <DataTable caption="Same-task per-run metric observations" card={false}><table className="tbl"><thead><tr><th>run</th><th>recorded objective</th><th>direction</th><th>nodes</th><th>status</th></tr></thead><tbody>
            {rows.map(r => <tr key={r.run_id}>
              <td><a href={`#/run/${encodeURIComponent(r.run_id)}`}>{r.label || r.run_id}</a></td>
              <td>{fmt(r.m)}{r.best_confirmed != null ? ' (confirmed mean)' : ''}</td>
              <td className="muted">{r.direction}</td><td className="muted">{r.nodes}</td>
              <td className="muted">{r.phase || (r.finished ? 'finished' : '—')}</td></tr>)}
          </tbody></table></DataTable>
        : resource.state === 'ready' && task
          && <div className="muted">No per-run metric observations for this task ID yet.</div>}
      {hidden > 0 && <div className="muted" style={{ marginTop: 8 }}>
        {hidden} additional observation{hidden === 1 ? '' : 's'} omitted by the client render limit.
      </div>}
    </Panel>
  )
}

// Legacy direction board retained as a graceful fallback for pre-Card logs. Current runs use the
// bounded public Card DTO and four generation-fenced, server-stamped operator controls below.
const _HYP_COLUMNS = [
  ['open', 'Open', 'question posed, not yet tested'],
  ['testing', 'Testing', 'experiments running'],
  ['supported', 'Supported', 'an experiment improved'],
  ['tested', 'Tested', 'evaluated, no improvement'],
  ['abandoned', 'Abandoned', 'dropped'],
]
// Monochrome source glyphs (no emoji): who posed the hypothesis. Reuses the shared icon set.
const _HYP_ICON = { researcher: 'search', deep_research: 'bulb', human: 'user', strategist: 'compass' }

const _CARD_COLUMNS = [
  ['proposed', 'Proposed', 'work item is open and has not started'],
  ['speculating', 'Speculating', 'speculative build requested'],
  ['building', 'Building', 'code is being produced'],
  ['built-awaiting-commit', 'Awaiting commit', 'build finished; durable node commit is pending'],
  ['coded', 'Coded', 'code exists and is waiting to run'],
  ['running', 'Running', 'evaluation is in flight'],
  ['evaluated', 'Evaluated', 'evidence has reached a verdict'],
  ['gated', 'Gated', 'trust or breeding gates exclude the available evidence'],
  ['dropped', 'Dropped', 'operator or engine removed the work item'],
]
const _CARD_FROZEN_STATUSES = new Set(['proposed', 'building', 'coded', 'running', 'evaluated', 'gated', 'dropped'])
const _CARD_OPTIONAL_STATUSES = new Set(['speculating', 'built-awaiting-commit'])
const _CARD_ICON = {
  researcher: 'search', deep_research: 'bulb', human: 'user', strategist: 'compass',
  operator: 'user', engine: 'bot', novelty: 'bulb',
}
const _CARD_RENDER_LIMIT = 256 // mirrors PUBLIC_CARD_MAX_COUNT at the wire boundary

const _cardText = value => typeof value === 'string' && value.trim() ? value.trim() : null
const _cardNumber = value => typeof value === 'number' && Number.isFinite(value) ? value : null
const _cardInt = value => Number.isSafeInteger(value) && value >= 0 ? value : null
const _cardRefs = value => Array.isArray(value)
  ? value.filter(item => typeof item === 'string' && item).slice(0, 32) : []
const _cardNodes = value => Array.isArray(value)
  ? value.filter(item => Number.isSafeInteger(item) && item >= 0).slice(0, 32) : []

function _cardStatus(card) {
  return _cardText(card.status) || 'unknown'
}

function _cardStatusLabel(status) {
  return status.split(/[-_]/).filter(Boolean)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ') || 'Other'
}

function _cardRows(state) {
  if (!isRecord(state?.cards)) return []
  return Object.entries(state.cards)
    .filter(([id, card]) => typeof id === 'string' && id && isRecord(card))
    .slice(0, _CARD_RENDER_LIMIT)
    // The mapping key is authoritative. Never let a malformed/spoofed body id change joins or receipts.
    .map(([id, card]) => ({ ...card, id }))
}

function _cardLanes(cards) {
  const occupied = new Set(cards.map(_cardStatus))
  const configured = _CARD_COLUMNS.filter(([status]) => !_CARD_OPTIONAL_STATUSES.has(status) || occupied.has(status))
  const known = new Set(_CARD_COLUMNS.map(([status]) => status))
  const extra = [...occupied].filter(status => !known.has(status)).sort()
    .map(status => [status, _cardStatusLabel(status), 'new derived lifecycle status'])
  const dropped = configured.find(([status]) => status === 'dropped')
  return [...configured.filter(([status]) => status !== 'dropped'), ...extra, dropped].filter(Boolean)
}

function _cardOrder(a, b) {
  const ap = _cardNumber(a.priority) ?? _cardNumber(a.foresight_rank) ?? Infinity
  const bp = _cardNumber(b.priority) ?? _cardNumber(b.foresight_rank) ?? Infinity
  if (ap !== bp) return ap - bp
  const an = _cardInt(a.created_at_node) ?? Infinity
  const bn = _cardInt(b.created_at_node) ?? Infinity
  return an - bn || a.id.localeCompare(b.id)
}

const _CARD_CONTROL_KINDS = ['edit', 'priority', 'resources', 'drop']

function _cardResourceValues(value) {
  if (!isRecord(value)) return null
  const gpus = _cardInt(value.gpus)
  const gpuMem = _cardInt(value.gpu_mem_mib)
  return gpus == null && gpuMem == null ? null : {
    ...(gpus == null ? {} : { gpus }),
    ...(gpuMem == null ? {} : { gpu_mem_mib: gpuMem }),
  }
}

function _sameCardResourceValues(left, right) {
  const a = _cardResourceValues(left)
  const b = _cardResourceValues(right)
  if (a == null || b == null) return a === b
  return ['gpus', 'gpu_mem_mib'].every(key => (
    Object.hasOwn(a, key) === Object.hasOwn(b, key) && a[key] === b[key]
  ))
}

function _cardControlReflected(card, kind, patch, baseline) {
  if (!card || !isRecord(patch)) return false
  if (kind === 'edit') {
    // The server clips the display statement (fold cap) and may redact secrets, so the folded value is
    // not always byte-equal to what the operator typed. Treat an exact match OR a non-empty clipped
    // PREFIX of the submitted text as reflected — otherwise the optimistic override (and its aria-busy
    // pending state) would never clear when the statement was clipped.
    if (typeof card.statement !== 'string' || typeof patch.statement !== 'string') return false
    if (card.statement === patch.statement) return true
    // A clipped/redacted fold is a PROPER prefix of the submitted text — but so is the PRE-EDIT value for
    // a common extend edit ("foo" -> "foobar"): matching the prefix alone would clear the override before
    // the write is even folded, letting an unrelated SSE frame drop the waiting-for-fold fence against
    // stale UI truth. Require the card to have actually MOVED off the submit-time baseline before a prefix
    // counts as this write landing; without a baseline (legacy entry) only an exact match reflects.
    return typeof baseline === 'string' && card.statement !== baseline
      && card.statement.length > 0 && patch.statement.startsWith(card.statement)
  }
  if (kind === 'priority') return card.priority === patch.priority && card.pinned === true
  if (kind === 'resources') return _sameCardResourceValues(card.resource_pin, patch.resource_pin)
  if (kind === 'drop') return card.status === 'dropped'
    && (!patch.dropped_reason || card.dropped_reason === patch.dropped_reason)
  return false
}

function _cardWithOptimisticControls(card, controlState) {
  if (!isRecord(controlState?.updates)) return card
  const visible = { ...card }
  for (const kind of _CARD_CONTROL_KINDS) {
    if (isRecord(controlState.updates[kind])) Object.assign(visible, controlState.updates[kind])
  }
  return visible
}

function _cardResourceSummary(value, { unavailable = 'unspecified' } = {}) {
  const footprint = _cardResourceValues(value)
  if (!footprint) return unavailable
  const gpus = footprint.gpus
  const memory = footprint.gpu_mem_mib
  return [
    gpus == null ? 'GPU count unspecified'
      : gpus === 0 ? 'CPU only' : `${gpus} GPU${gpus === 1 ? '' : 's'}`,
    memory == null ? null : `${fmtInt(memory)} MiB/GPU`,
  ].filter(Boolean).join(' · ')
}

function _CardProjectionNotice({ projection, cards }) {
  if (!isRecord(projection)) return <div className="card-projection-note" role="status">
    Card coverage receipt unavailable; this older payload may be incomplete.
  </div>
  if (projection.complete === true) return null
  const total = _cardInt(projection.total)
  const returned = _cardInt(projection.returned) ?? cards.length
  const sourceInvalid = projection.source_valid === false
  return <div className="card-projection-note" role="status">
    <OpIcon name="alert" size={12} />
    <span>{sourceInvalid
      ? 'Card source was invalid; no complete board can be claimed.'
      : `Showing ${returned}${total == null ? '' : ` of ${total}`} Cards; clipped or redacted public fields are marked partial.`}</span>
  </div>
}

function _CardKanbanCard({
  card, receipt, onSelect, onClose, onControl, controlState, controlsLocked,
}) {
  const statement = _cardText(card.statement) || `Card ${card.id}`
  const source = _cardText(card.source)
  const operator = _cardText(card.operator)
  const evalProfile = _cardText(card.eval_profile)
  const params = isRecord(card.params) ? Object.entries(card.params)
    .filter(([, value]) => _cardNumber(value) != null).slice(0, 6) : []
  const spaceCount = isRecord(card.space) ? Object.keys(card.space).length : 0
  const footprintKnown = Object.hasOwn(card, 'footprint')
  const baseFootprint = isRecord(card.footprint) ? card.footprint : null
  const resourcePin = isRecord(card.resource_pin) ? card.resource_pin : null
  // CODEX AGENT: this is only a client-side merge of requested values, not the scheduler's effective
  // footprint after host-capacity clamping. Rendering it as "Effective pin" can overstate GPU memory
  // whenever the server accepted a partial pin and clamped the inherited footprint; project the
  // server-computed effective allocation, or label this explicitly as a requested/configured merge.
  const effectiveFootprint = baseFootprint || resourcePin
    ? { ...(_cardResourceValues(baseFootprint) || {}), ...(_cardResourceValues(resourcePin) || {}) }
    : null
  if (effectiveFootprint?.gpus === 0) delete effectiveFootprint.gpu_mem_mib
  const effectiveGpus = effectiveFootprint ? _cardInt(effectiveFootprint.gpus) : null
  const pinValues = _cardResourceValues(resourcePin)
  const formGpus = pinValues?.gpus ?? effectiveGpus
  const formGpuMem = pinValues && Object.hasOwn(pinValues, 'gpu_mem_mib')
    ? pinValues.gpu_mem_mib : null
  const evalTimeout = _cardNumber(card.eval_timeout)
  const identity = isRecord(card.identity) ? card.identity : null
  const selection = isRecord(card.selection_provenance) ? card.selection_provenance : null
  const blockersKnown = Object.hasOwn(card, 'selection_blockers')
  const blockers = _cardRefs(card.selection_blockers)
  const evidenceKnown = Object.hasOwn(card, 'evidence')
  const evidence = _cardNodes(card.evidence).slice(0, 8)
  const concepts = _cardRefs(card.concept_tags).slice(0, 5)
  const parents = _cardNodes(card.parent_ids)
  const parent = _cardInt(card.parent_id)
  if (parent != null && !parents.includes(parent)) parents.unshift(parent)
  const scoredAgainst = _cardInt(card.scored_against)
  const bestDelta = _cardNumber(card.best_delta)
  const priority = _cardNumber(card.priority)
  const novelty = isRecord(card.novelty_verdict) ? _cardText(card.novelty_verdict.grade) : null
  const omissionCount = isRecord(receipt?.omissions) ? Object.keys(receipt.omissions).length : 0
  const declaredResources = footprintKnown
    ? _cardResourceSummary(baseFootprint) : 'resource projection unavailable'
  const effectiveResources = _cardResourceSummary(effectiveFootprint)
  const pinResources = _cardResourceSummary(resourcePin)
  const provenanceBits = [
    source && `source ${source}`,
    identity && _cardText(identity.kind) && `identity ${identity.kind}`,
    _cardText(card.provenance_tier) && `tier ${card.provenance_tier}`,
    selection && _cardText(selection.action_source) && `action ${selection.action_source}`,
    baseFootprint && _cardText(baseFootprint.proposed_by) && `proposed ${baseFootprint.proposed_by}`,
    baseFootprint && _cardText(baseFootprint.finalized_by) && `finalized ${baseFootprint.finalized_by}`,
    resourcePin && _cardText(resourcePin.pinned_by) && `resource pin ${resourcePin.pinned_by}`,
    _cardText(card.research_origin) && `research ${card.research_origin}`,
  ].filter(Boolean)
  const [statementDraft, setStatementDraft] = useState(statement)
  const [priorityDraft, setPriorityDraft] = useState(
    _cardInt(card.priority) == null ? '' : String(card.priority + 1))
  const [gpuDraft, setGpuDraft] = useState(formGpus == null ? '' : String(formGpus))
  const [memoryDraft, setMemoryDraft] = useState(formGpuMem == null ? '' : String(formGpuMem))
  const [dropReason, setDropReason] = useState('operator dropped')
  const [controlError, setControlError] = useState('')
  const ownPending = isRecord(controlState?.pending) ? controlState.pending : null
  const busy = !!ownPending || controlsLocked === true
  const terminal = _cardStatus(card) === 'dropped' || !!_cardText(card.merged_into)
  // Re-seed each draft ONLY when its own folded source (or the card identity) changes. A single effect
  // over every dep re-ran on ANY change, so an unrelated live fold (e.g. a card_ranked priority bump
  // arriving while the operator is typing a new statement) reset ALL four drafts and silently discarded
  // the in-progress edits in the other fields. Per-field effects keep each edit until its own source moves.
  useEffect(() => { setStatementDraft(statement) }, [card.id, statement])
  useEffect(() => {
    setPriorityDraft(_cardInt(card.priority) == null ? '' : String(card.priority + 1))
  }, [card.id, card.priority])
  useEffect(() => { setGpuDraft(formGpus == null ? '' : String(formGpus)) }, [card.id, formGpus])
  useEffect(() => {
    setMemoryDraft(formGpuMem == null ? '' : String(formGpuMem))
  }, [card.id, formGpuMem])

  const control = async (kind, data, patch) => {
    if (!onControl || busy) return
    setControlError('')
    try { await onControl(card, kind, data, patch) }
    catch (error) { setControlError(error?.message || String(error)) }
  }
  const saveStatement = () => {
    const next = statementDraft.trim()
    if (!next || next.length > 4000) { setControlError('Display statement must be 1–4000 characters.'); return }
    if (next !== statement) control('edit', { statement: next }, { statement: next })
  }
  const savePriority = () => {
    const visible = Number(priorityDraft)
    if (!Number.isSafeInteger(visible) || visible < 1 || visible > 256) {
      setControlError('Priority must be between 1 and 256.'); return
    }
    control('priority', { priority: visible - 1 }, { priority: visible - 1 })
  }
  const saveResources = () => {
    const nextGpus = Number(gpuDraft)
    const nextMemory = memoryDraft.trim() === '' ? null : Number(memoryDraft)
    if (!Number.isSafeInteger(nextGpus) || nextGpus < 0
      || (nextMemory != null && (!Number.isSafeInteger(nextMemory) || nextMemory < 0))) {
      setControlError('GPU count and memory must be non-negative integers.'); return
    }
    if (nextGpus === 0 && nextMemory != null) {
      setControlError('CPU-only Cards cannot request GPU memory.'); return
    }
    // This local patch contains quantitative display values only. Authority/provenance is stamped by
    // the server event and is never supplied by the browser, even optimistically.
    const pin = { gpus: nextGpus, ...(nextMemory == null ? {} : { gpu_mem_mib: nextMemory }) }
    control('resources', { gpus: nextGpus, gpu_mem_mib: nextMemory }, { resource_pin: pin })
  }
  const drop = () => {
    const reason = dropReason.trim() || 'operator dropped'
    control('drop', { reason }, { status: 'dropped', dropped_reason: reason })
  }
  return <article className="card-kanban-card" data-card-id={card.id} aria-busy={ownPending ? 'true' : undefined}>
    <div className="card-kanban-stmt">
      <span className="hyp-src" title={source ? `source: ${source}` : 'source unavailable'}>
        <OpIcon name={_CARD_ICON[source] || 'dot'} size={12} />
      </span>
      <span>{statement}</span>
    </div>
    <div className="card-kanban-meta">
      <span className="chip xs" title="durable Card identity">{card.id}</span>
      {priority != null && <span className="chip xs" title="derived priority; 1 is highest">#{priority + 1}</span>}
      {card.pinned === true && <span className="chip xs warn"><OpIcon name="flag" size={10} /> pinned</span>}
      {card.selection_ready === true
        ? <span className="chip xs ok" title="eligible for Card-driven selection">selection ready</span>
        : card.selection_ready === false
          ? <span className="chip xs warn" title="not eligible for Card-driven selection">not selection ready</span>
          : <span className="chip xs" title="selection readiness was not present in the public projection">readiness unknown</span>}
      {receipt && receipt.complete !== true && <span className="chip xs warn"
        title={`${omissionCount} public field omission${omissionCount === 1 ? '' : 's'}`}>
        partial DTO{omissionCount ? ` · ${omissionCount}` : ''}</span>}
    </div>
    {(operator || evalProfile || params.length || spaceCount) && <div className="card-kanban-fact">
      <span className="card-kanban-k">Action</span>
      <span>{operator || 'operator unspecified'}</span>
      {evalProfile && <span>profile {evalProfile}</span>}
      {params.map(([key, value]) => <span key={key} className="card-param">{key}={fmt(value)}</span>)}
      {spaceCount > 0 && <span>{spaceCount} search variable{spaceCount === 1 ? '' : 's'}</span>}
    </div>}
    <div className="card-kanban-fact">
      <span className="card-kanban-k">Declared</span>
      <span>{declaredResources}{evalTimeout == null ? '' : ` · ${fmt(evalTimeout)}s timeout`}</span>
    </div>
    {resourcePin && <div className="card-kanban-fact card-resource-pin">
      <span className="card-kanban-k">Effective pin</span>
      <span><span className="chip xs warn">{_cardText(resourcePin.pinned_by) === 'operator'
        ? 'operator override' : 'pending operator override'}</span> {effectiveResources}
        <span className="card-resource-request">requested {pinResources}</span></span>
    </div>}
    <div className="card-kanban-fact">
      <span className="card-kanban-k">Provenance</span>
      <span>{provenanceBits.length ? provenanceBits.join(' · ') : 'unavailable'}</span>
    </div>
    <div className="card-kanban-fact">
      <span className="card-kanban-k">Gate</span>
      <span>{selection && _cardText(selection.freshness) ? `freshness ${selection.freshness}` : 'freshness unknown'}
        {selection && _cardText(selection.owner_state) ? ` · owner ${selection.owner_state}` : ''}
        {selection && typeof selection.action_complete === 'boolean'
          ? ` · action ${selection.action_complete ? 'complete' : 'incomplete'}` : ''}</span>
    </div>
    {blockers.length > 0 && <div className="card-kanban-blockers" aria-label="Selection blockers">
      {blockers.slice(0, 5).map(blocker => <span key={blocker} className="chip xs warn">
        {blocker.replaceAll('_', ' ')}</span>)}
      {blockers.length > 5 && <span className="muted">+{blockers.length - 5}</span>}
    </div>}
    {!blockersKnown && <div className="muted card-kanban-unknown">Selection blockers unavailable</div>}
    {(parents.length > 0 || scoredAgainst != null) && <div className="card-kanban-fact">
      <span className="card-kanban-k">Lineage</span>
      <span>{parents.length ? `parent ${parents.map(id => `#${id}`).join(', ')}` : ''}
        {scoredAgainst != null ? `${parents.length ? ' · ' : ''}scored vs #${scoredAgainst}` : ''}</span>
    </div>}
    {(concepts.length > 0 || novelty) && <div className="card-kanban-tags">
      {novelty && <span className="chip xs">novelty {novelty}</span>}
      {concepts.map(concept => <span key={concept} className="chip xs">{concept}</span>)}
    </div>}
    {(_cardText(card.merged_into) || _cardText(card.dropped_reason)) && <div className="card-kanban-terminal">
      {_cardText(card.merged_into) ? `Merged into ${card.merged_into}` : card.dropped_reason}
      {_cardText(card.dropped_by) ? ` · by ${card.dropped_by}` : ''}
    </div>}
    <div className="card-kanban-evidence">
      {evidence.map(nid => <button key={nid} type="button" className="btn xs ghost"
        aria-label={`Open evidence node #${nid}`} title={`evidence node #${nid}`}
        onClick={() => { onSelect?.(nid); onClose?.() }}>#{nid}</button>)}
      {bestDelta != null && <span className={'chip xs ' + (bestDelta > 0 ? 'ok' : '')}
        title="best improvement over parent among the evidence">Δ{fmt(bestDelta)}</span>}
      {evidence.length === 0 && <span className="muted">
        {evidenceKnown ? 'No evidence nodes' : 'Evidence unavailable'}</span>}
    </div>
    {onControl && !terminal && <details className="card-kanban-controls">
      <summary aria-label={`Operator controls for ${card.id}`}>Operator controls</summary>
      <form className="card-control-form" onSubmit={event => { event.preventDefault(); saveStatement() }}>
        <label><span>Display statement</span><textarea className="text card-control-statement"
          aria-label={`Display statement for ${card.id}`} rows="3" value={statementDraft}
          maxLength={4000} disabled={busy} onChange={event => setStatementDraft(event.target.value)} /></label>
        <button type="submit" className="btn xs" disabled={busy || !statementDraft.trim()
          || statementDraft.trim() === statement}>Save text</button>
      </form>
      <form className="card-control-form" onSubmit={event => { event.preventDefault(); savePriority() }}>
        <label><span>Priority (1 is highest)</span><input className="text" type="number" min="1" max="256"
          aria-label={`Priority for ${card.id}`} value={priorityDraft} disabled={busy}
          onChange={event => setPriorityDraft(event.target.value)} /></label>
        <button type="submit" className="btn xs" disabled={busy || !priorityDraft}>Pin priority</button>
      </form>
      <form className="card-control-resource" onSubmit={event => { event.preventDefault(); saveResources() }}>
        <fieldset disabled={busy}>
          <legend>Effective resource pin</legend>
          <div className="card-control-resource-fields">
            <label><span>GPUs</span><input className="text" type="number" min="0" step="1"
              aria-label={`GPU count for ${card.id}`} value={gpuDraft}
              onChange={event => {
                setGpuDraft(event.target.value)
                if (event.target.value === '0') setMemoryDraft('')
              }} /></label>
            <label><span>MiB / GPU</span><input className="text" type="number" min="0" step="1"
              aria-label={`GPU memory in MiB for ${card.id}`} placeholder="inherit declared"
              value={memoryDraft} disabled={busy || gpuDraft === '0'}
              onChange={event => setMemoryDraft(event.target.value)} /></label>
          </div>
          <div className="card-control-help">Validated against the current server GPU envelope;
            blank memory inherits the declared value.</div>
          <button type="submit" className="btn xs" disabled={busy || gpuDraft === ''}>Pin resources</button>
        </fieldset>
      </form>
      <details className="card-control-danger">
        <summary>Drop Card…</summary>
        <form className="card-control-form" onSubmit={event => { event.preventDefault(); drop() }}>
          <label><span>Reason (optional)</span><input className="text" value={dropReason} maxLength={400}
            aria-label={`Drop reason for ${card.id}`} disabled={busy}
            onChange={event => setDropReason(event.target.value)} /></label>
          <button type="submit" className="btn xs danger" disabled={busy}>Confirm drop</button>
        </form>
      </details>
      {controlsLocked && !ownPending && <div className="card-control-feedback" role="status">
        Another Card command is still being submitted for this run.</div>}
    </details>}
    {isRecord(controlState?.notice) && <div
      className={'card-control-feedback ' + (controlState.notice.tone || '')}
      role={controlState.notice.tone === 'error' ? 'alert' : 'status'} aria-live="polite">
      {controlState.notice.text}</div>}
    {controlError && <div className="card-control-feedback error" role="alert">{controlError}</div>}
  </article>
}

function _CardKanban({ state, cards, runId, onSelect, onClose, onToast }) {
  const [optim, setOptim] = useState({})
  const inFlight = useRef(new Set())
  const cardsById = new Map(cards.map(card => [card.id, card]))
  const cardsByIdRef = useRef(cardsById)
  cardsByIdRef.current = cardsById
  useEffect(() => {
    setOptim(current => {
      let changed = false
      const next = { ...current }
      for (const [id, entry] of Object.entries(current)) {
        const card = cardsByIdRef.current.get(id)
        if (!card) { delete next[id]; changed = true; continue }
        const updates = { ...(entry.updates || {}) }
        for (const kind of _CARD_CONTROL_KINDS) {
          if (updates[kind]
              && _cardControlReflected(card, kind, updates[kind], entry.baselines?.[kind])) {
            delete updates[kind]
            changed = true
          }
        }
        const pending = entry.pending && updates[entry.pending.kind] ? entry.pending : null
        if (pending !== entry.pending) changed = true
        if (Object.keys(updates).length === 0 && !pending) {
          delete next[id]
          changed = true
        } else if (changed || pending !== entry.pending) {
          next[id] = { ...entry, updates, pending }
        }
      }
      return changed ? next : current
    })
  }, [state.cards])
  const visibleCards = cards.map(card => _cardWithOptimisticControls(card, optim[card.id]))
  // A 'confirmation-unknown' pending (a lost/uncertain submission) can NEVER self-clear — the fold never
  // reflects an intent that may not have landed — so it must not count toward the board-wide lock, or one
  // uncertain command would freeze the controls on EVERY Card until reload. The real concurrency guard is
  // `inFlight` (released in the finally), and the stuck Card still shows its own 'waiting for the live
  // fold' notice via its own pending. Only genuinely-progressing pendings gate the rest of the board.
  const globalPending = Object.values(optim).some(
    entry => isRecord(entry?.pending) && entry.pending.phase !== 'confirmation-unknown')
  const cardControl = async (card, kind, data, patch) => {
    const labels = {
      edit: { saving: 'Saving Card display text…', success: 'Card display text updated', failure: 'Could not edit Card' },
      priority: { saving: 'Pinning Card priority…', success: 'Card priority pinned', failure: 'Could not pin Card priority' },
      resources: { saving: 'Pinning Card resources…', success: 'Card resources pinned', failure: 'Could not pin Card resources' },
      drop: { saving: 'Dropping Card…', success: 'Card dropped', failure: 'Could not drop Card' },
    }[kind]
    if (!labels || inFlight.current.size > 0) {
      const message = 'Another Card command is still being submitted for this run.'
      onToast?.(message)
      return { kind: 'pending', message }
    }
    inFlight.current.add(card.id)
    // Snapshot the card's CURRENT statement so a later clipped/redacted fold of THIS edit is
    // distinguishable from the not-yet-folded state (see `_cardControlReflected`): a landed edit moves
    // the card off this baseline, whereas an extend edit's pre-value still prefix-matches the submission.
    const editBaseline = kind === 'edit' && typeof card.statement === 'string'
      ? card.statement : undefined
    setOptim(current => {
      const entry = current[card.id] || {}
      return { ...current, [card.id]: {
        ...entry,
        updates: { ...(entry.updates || {}), [kind]: patch },
        baselines: { ...(entry.baselines || {}),
          ...(editBaseline !== undefined ? { edit: editBaseline } : {}) },
        pending: { kind, phase: 'submitting' },
        notice: { tone: 'pending', text: labels.saving },
      } }
    })
    try {
      const record = kind === 'edit'
        ? await CONTROL.editCard(runId, card.id, data.statement)
        : kind === 'priority'
          ? await CONTROL.reprioritizeCard(runId, card.id, data.priority)
          : kind === 'resources'
            ? await CONTROL.pinCardResources(runId, card.id, data.gpus, data.gpu_mem_mib)
            : await CONTROL.dropCard(runId, card.id, data.reason)
      const feedback = commandFeedback(record, {
        success: labels.success, noop: `${labels.success} (already current)`,
        executing: `${labels.success} — waiting for the live fold`, failure: labels.failure,
      })
      onToast?.(feedback.message)
      setOptim(current => {
        const entry = current[card.id]
        if (!entry) return current
        const updates = { ...(entry.updates || {}) }
        const rawCard = cardsByIdRef.current.get(card.id)
        // Clear the optimistic override once the command DEFINITIVELY settles (success/noop/error) — not
        // only on an exact fold reflection: the server may clip/redact the value, so waiting for a
        // byte-equal fold would leave the card stuck showing operator text with its controls disabled.
        // Only a 'pending' (accepted, engine will apply later) settle keeps the override until the fold.
        const settled = ['error', 'success', 'noop'].includes(feedback.kind)
        if (settled || _cardControlReflected(rawCard, kind, patch, entry.baselines?.[kind])) {
          delete updates[kind]
        }
        const pending = feedback.kind === 'pending' && updates[kind]
          ? { kind, phase: 'waiting-for-fold' } : null
        const notice = feedback.kind === 'error'
          ? { tone: 'error', text: feedback.message }
          : { tone: feedback.kind === 'pending' ? 'pending' : 'success', text: feedback.message }
        return { ...current, [card.id]: { ...entry, updates, pending, notice } }
      })
      return feedback
    } catch (error) {
      const uncertain = error?.submissionMayHaveSucceeded === true || error?.commandUnknown === true
        || ['accepted', 'executing'].includes(error?.commandRecord?.status)
      const message = uncertain
        ? `${labels.success} may still complete — waiting for the live fold`
        : `${labels.failure}: ${error?.message || error}`
      onToast?.(message)
      setOptim(current => {
        const entry = current[card.id]
        if (!entry) return current
        const updates = { ...(entry.updates || {}) }
        if (!uncertain) delete updates[kind]
        return { ...current, [card.id]: {
          ...entry, updates,
          pending: uncertain ? { kind, phase: 'confirmation-unknown' } : null,
          notice: { tone: uncertain ? 'pending' : 'error', text: message },
        } }
      })
      return { kind: uncertain ? 'pending' : 'error', message }
    } finally {
      inFlight.current.delete(card.id)
    }
  }
  const projection = isRecord(state.cards_projection) ? state.cards_projection : null
  const receipts = isRecord(projection?.items) ? projection.items : {}
  const lanes = _cardLanes(visibleCards)
  const total = _cardInt(projection?.total)
  const sub = total != null && total !== cards.length
    ? `${visibleCards.length} of ${total} public work items` : `${visibleCards.length} work item${visibleCards.length === 1 ? '' : 's'}`
  return <Panel title="Cards" sub={sub} onClose={onClose} wide>
    <_CardProjectionNotice projection={projection} cards={visibleCards} />
    <div className="card-board" aria-label="Card lifecycle kanban">
      {lanes.map(([key, label, hint]) => {
        const rows = visibleCards.filter(card => _cardStatus(card) === key).sort(_cardOrder)
        const tone = _CARD_FROZEN_STATUSES.has(key) ? ` card-${key}` : ''
        return <section key={key} className={'card-col' + tone} aria-label={`${label}: ${rows.length}`}>
          <div className="card-col-h" title={hint}>{label} <span className="muted">{rows.length}</span></div>
          {rows.map(card => <_CardKanbanCard key={card.id} card={card}
            receipt={isRecord(receipts[card.id]) ? receipts[card.id] : null}
            controlState={optim[card.id]} controlsLocked={globalPending && !optim[card.id]?.pending}
            onSelect={onSelect} onClose={onClose}
            onControl={typeof runId === 'string' && runId ? cardControl : null} />)}
          {rows.length === 0 && <div className="muted card-empty">—</div>}
        </section>
      })}
    </div>
  </Panel>
}

function _HypothesisFallback({ state, runId, onSelect, onClose, onToast }) {
  const [draft, setDraft] = useState('')
  // Optimistic status overrides {id: 'abandoned'|'deleted'}: the run-state round-trip that reflects a
  // control event can lag (its SSE is buffered by a proxy), so apply the click to the board AT ONCE
  // instead of leaving it looking dead for up to a minute. The real fold catches up idempotently.
  const [optim, setOptim] = useState({})
  // Drop an optimistic override once the real fold REFLECTS it (deleted card gone from state; abandoned
  // card now status='abandoned'), so a stale override can't keep masking a LATER server-side reopen of
  // the same hypothesis while the board stays mounted.
  useEffect(() => {
    setOptim(o => {
      const next = {}
      for (const [id, v] of Object.entries(o)) {
        const h = (state.hypotheses || {})[id]
        if (v === 'deleted' && h) next[id] = v                          // not yet dropped by the fold
        else if (v === 'abandoned' && h && h.status !== 'abandoned') next[id] = v   // not yet reflected
      }
      return next
    })
  }, [state.hypotheses])
  const hyps = Object.values(state.hypotheses || {})
    .filter(h => optim[h.id] !== 'deleted')
    .map(h => optim[h.id] ? { ...h, status: optim[h.id] } : h)
  // FOREAGENT board prioritization: order cards by predicted payoff (`priority`, 0 = best;
  // unranked cards last), so the kanban shows the sort the world model chose. `ranking` carries the
  // analysis trace (reason + confidence) surfaced as a header note and per-card tooltip.
  const ranking = state.hypothesis_ranking || null
  const rankConf = ranking && typeof ranking.confidence === 'number' ? Math.round(ranking.confidence * 100) : null
  const byStatus = (s) => hyps.filter(h => (h.status || 'open') === s)
    .sort((a, b) => (a.priority ?? Infinity) - (b.priority ?? Infinity))
  const add = async () => {
    const s = draft.trim()
    if (!s) return
    try {
      const feedback = commandFeedback(await CONTROL.addHypothesis(runId, s), {
        success: 'Hypothesis added', noop: 'That hypothesis was already tracked',
        executing: 'Hypothesis requested — waiting for the run', failure: 'Could not add hypothesis',
      })
      if (feedback.kind === 'success') setDraft('')
      onToast?.(feedback.message)
    } catch (error) { onToast?.(`Could not add hypothesis: ${error.message || error}`) }
  }
  const _revert = (id) => setOptim(o => { const n = { ...o }; delete n[id]; return n })
  const abandon = async (h) => {
    setOptim(o => ({ ...o, [h.id]: 'abandoned' }))          // reflect immediately (SSE lag)
    try {
      const feedback = commandFeedback(await CONTROL.abandonHypothesis(runId, h.id), {
        success: 'Hypothesis abandoned', noop: 'Hypothesis was already abandoned',
        executing: 'Abandon requested — waiting for the run', failure: 'Could not update hypothesis',
      })
      if (feedback.kind !== 'success') _revert(h.id)
      onToast?.(feedback.message)
    } catch (error) { _revert(h.id); onToast?.(`Could not update hypothesis: ${error.message || error}`) }
  }
  const del = async (h) => {
    setOptim(o => ({ ...o, [h.id]: 'deleted' }))            // remove from the board at once
    try {
      const feedback = commandFeedback(await CONTROL.deleteHypothesis(runId, h.id), {
        success: 'Hypothesis deleted', noop: 'Hypothesis was already deleted',
        executing: 'Delete requested — waiting for the run', failure: 'Could not delete hypothesis',
      })
      if (feedback.kind !== 'success') _revert(h.id)
      onToast?.(feedback.message)
    } catch (error) { _revert(h.id); onToast?.(`Could not delete hypothesis: ${error.message || error}`) }
  }
  return (
    <Panel title="Hypotheses" sub={`${hyps.length} tracked — what the run is trying to learn`} onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 10, gap: 6 }}>
        <input className="text" style={{ flex: 1 }} aria-label="New hypothesis"
          placeholder="Pose a hypothesis to test (e.g. “target is right-skewed; a log transform helps”)"
          value={draft} onChange={e => setDraft(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') add() }} />
        <button className="btn sm primary" onClick={add} disabled={!draft.trim()}>+ Add</button>
      </div>
      {ranking && <div className="muted" style={{ marginBottom: 8, fontSize: 12, display: 'flex', gap: 6, alignItems: 'baseline' }}
        title={ranking.reason || 'predicted before execution'}>
        <OpIcon name="bulb" size={11} />
        <span>Predicted priority order (FOREAGENT{rankConf != null ? `, ${rankConf}% confidence` : ''})
          {ranking.reason ? `: ${ranking.reason}` : ''}</span>
      </div>}
      {hyps.length === 0
        ? <div className="muted">No hypotheses yet. The Researcher states one per experiment (its
          <code> hypothesis</code> field); deep-research directions and your “+ Add” questions land here too,
          then get tracked to a verdict as experiments run.</div>
        : <div className="hyp-board">
          {_HYP_COLUMNS.map(([key, label, hint]) => {
            const col = byStatus(key)
            return <div key={key} className={'hyp-col hyp-' + key}>
              <div className="hyp-col-h" title={hint}>{label} <span className="muted">{col.length}</span></div>
              {col.map(h => <div key={h.id} className="hyp-card">
                <div className="hyp-stmt">
                  <span className="hyp-src" title={`source: ${h.source}`}>
                    <OpIcon name={_HYP_ICON[h.source] || 'dot'} size={12} /></span> {h.statement}
                </div>
                <div className="hyp-meta">
                  {h.priority != null && <span className="chip xs" title={'predicted priority '
                    + (h.priority + 1) + (rankConf != null ? ` · ${rankConf}% confidence` : '')
                    + (ranking && ranking.reason ? ` · ${ranking.reason}` : '')}>#{h.priority + 1}</span>}
                  {(h.evidence || []).slice(0, 8).map(nid => <button key={nid} className="btn xs ghost"
                    title={`experiment #${nid}`} onClick={() => { onSelect && onSelect(nid); onClose() }}>#{nid}</button>)}
                  {h.best_delta != null && <span className={'chip xs ' + (h.best_delta > 0 ? 'ok' : '')}
                    title="best improvement over parent among the evidence">Δ{fmt(h.best_delta)}</span>}
                  {key !== 'abandoned' && <button className="btn xs ghost" title="abandon — move to the Abandoned column (keeps the record)"
                    onClick={() => abandon(h)}><OpIcon name="cross" size={11} /></button>}
                  <button className="btn xs ghost danger" title="delete this hypothesis permanently (remove from the board)"
                    onClick={() => del(h)}>🗑</button>
                </div>
              </div>)}
              {col.length === 0 && <div className="muted hyp-empty">—</div>}
            </div>
          })}
        </div>}
    </Panel>
  )
}

export function HypothesisBoard({ state, runId, onSelect, onClose, onToast }) {
  const cards = _cardRows(state)
  const projection = isRecord(state?.cards_projection) ? state.cards_projection : null
  // A non-empty/omitted/invalid Card projection is authoritative. With no Cards at all, preserve the
  // hypothesis add/abandon workflow for older logs and for a run before its first Card is minted.
  const hasAuthoritativeCards = cards.length > 0 || (_cardInt(projection?.total) ?? 0) > 0
    || projection?.source_valid === false
  return hasAuthoritativeCards
    ? <_CardKanban state={state} cards={cards} runId={runId} onSelect={onSelect}
      onClose={onClose} onToast={onToast} />
    : <_HypothesisFallback state={state} runId={runId} onSelect={onSelect} onClose={onClose} onToast={onToast} />
}

// Module scope so their identity is stable across SSE frames (ComparePanel re-renders on every live
// fold); defined inline they remounted the <select> each frame, closing an open dropdown mid-pick.
function CmpSel({ label, v, set, ids }) {
  return <label className="cmp-select"><span>{label}</span>
    <select className="text" value={v ?? ''} aria-label={label}
            onChange={e => set(Number(e.target.value))}>{ids.map(i => <option key={i} value={i}>#{i}</option>)}</select>
  </label>
}

function useNodeResource(runId, nodeId) {
  const [resource, setResource] = useState({ nodeId: null, status: 'idle', data: null, error: null })
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    if (nodeId == null) { setResource({ nodeId, status: 'idle', data: null, error: null }); return }
    let alive = true; const requested = nodeId
    setResource({ nodeId: requested, status: 'loading', data: null, error: null })
    get(runNodeApiPath(runId, requested))
      .then(data => { if (alive) setResource({ nodeId: requested, status: 'ready', data, error: null }) })
      .catch(error => { if (alive) setResource({ nodeId: requested, status: 'error', data: null, error: error.message }) })
    return () => { alive = false }
  }, [runId, nodeId, nonce])
  const current = resource.nodeId === nodeId ? resource : { nodeId, status: 'loading', data: null, error: null }
  return { ...current, retry: () => setNonce(n => n + 1) }
}

function CmpCol({ resource, label }) {
  if (resource.status === 'error') return <div className="notice resource-error" style={{ flex: 1 }} role="alert"><span>{label}: full details could not be loaded.</span><button className="btn sm" onClick={resource.retry}>Retry</button></div>
  const d = resource.data
  return d ? <div className="cmp-col">
    <div className="kv">
      <div className="k">operator</div><div className="v">{d.operator}</div>
      <div className="k">metric</div><div className="v">{fmt(d.confirmed_mean ?? d.metric)}</div>
      <div className="k">status</div><div className="v">{d.status}</div>
      <div className="k">params</div><div className="v">{JSON.stringify(d.idea?.params)}</div>
    </div>
    <CodeViewer code={d.code || '(no code)'} label={`${label} code`} maxHeight={280} />
  </div> : <div className="muted" style={{ flex: 1 }}>…</div>
}

export function ComparePanel({ state, runId, onClose, initialPair = null }) {
  const ids = Object.keys(state.nodes).map(Number).sort((a, b) => a - b)
  const [a, setA] = useState(null), [b, setB] = useState(null)
  const [diff, setDiff] = useState(false)   // U4: line diff between ANY two nodes (not just vs parent)
  // Seed from an explicit pair (e.g. canvas "diff vs champion"), else best vs latest.
  useEffect(() => {
    if (initialPair && initialPair.length === 2) {
      if (initialPair[0] != null) setA(initialPair[0])
      if (initialPair[1] != null) setB(initialPair[1])
      setDiff(true)
    }
  }, [initialPair && initialPair.join(',')])
  // Seed/repair the selectors once nodes exist (the panel may open before any node arrives).
  // Functional updates: on mount this runs in the same commit as the initialPair seeding above
  // and would otherwise read a=null from the stale closure and overwrite the explicit pair.
  useEffect(() => {
    if (!ids.length) return
    setA(cur => (cur == null || !ids.includes(cur)) ? (state.best_node_id ?? ids[0]) : cur)
    setB(cur => (cur == null || !ids.includes(cur)) ? ids[ids.length - 1] : cur)
  }, [ids.join(','), state.best_node_id])
  const resourceA = useNodeResource(runId, a)
  const resourceB = useNodeResource(runId, b)
  const da = resourceA.data, db = resourceB.data
  const codeDiff = useMemo(
    () => diff && da?.code != null && db?.code != null ? diffLines(da.code, db.code) : null,
    [diff, da?.code, db?.code])
  const diffError = resourceA.status === 'error' || resourceB.status === 'error'
  if (!ids.length) return <Panel title="Compare nodes" onClose={onClose}><div className="muted">No nodes yet.</div></Panel>
  return (
    <Panel title="Compare nodes" onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 10 }}>
        <CmpSel label="Left node" v={a} set={setA} ids={ids} /><span className="muted">vs</span><CmpSel label="Right node" v={b} set={setB} ids={ids} />
        <span className="spacer" style={{ flex: 1 }} />
        <button className={'btn sm' + (diff ? ' primary' : '')} onClick={() => setDiff(d => !d)}
          title="ordered line diff of the two nodes' code">Code diff</button>
      </div>
      {diff
        ? (diffError
            ? <div className="notice resource-error" role="alert"><span>Could not load both node details for the diff.</span><button className="btn sm" onClick={() => { resourceA.retry(); resourceB.retry() }}>Retry</button></div>
            : codeDiff
            ? <CodeViewer diff={codeDiff} copyText={db.code || ''}
                label={`Code diff #${a} to #${b}`} maxHeight={460} />
            : <div className="muted" role="status">Loading code for both nodes…</div>)
        : <div className="cmp-cols"><CmpCol resource={resourceA} label={`Node #${a}`} /><CmpCol resource={resourceB} label={`Node #${b}`} /></div>}
    </Panel>
  )
}


const explorerEvent = event => {
  const omitted = event?._log_page?.truncated === true
  const bytes = omitted ? Number(event._log_page.raw_bytes || 0) : 0
  if (omitted) {
    const preview = `details omitted · ${bytes.toLocaleString()} source bytes exceed page limit`
    return { event, preview, search: `${event.type || ''} ${preview}`.toLowerCase(), omitted: true }
  }
  let serialized = '{}'
  try { serialized = JSON.stringify(event.data || {}) } catch { serialized = '[unserializable event data]' }
  const preview = serialized.length > 500 ? serialized.slice(0, 500) + '…' : serialized
  return { event, preview, search: `${event.type || ''} ${serialized.slice(0, 4_000)}`.toLowerCase(), omitted: false }
}
const explorerEventKey = item => timelineEventKey(item.event)

export function EventExplorer({ runId, timeline, historyActive = false, onReturnToLive = null, onClose }) {
  const [f, setF] = useState('')
  const query = f.trim().toLowerCase()
  const indexed = useMemo(() => timeline.rows.map(explorerEvent), [timeline.rows])
  const rows = useMemo(() => indexed.filter(item => !query || item.search.includes(query)), [indexed, query])
  const totalLabel = timeline.totalEvents == null
    ? `${timeline.rows.length} loaded events`
    : `${timeline.rows.length} loaded of ${timeline.totalEvents} events`
  return (
    <Panel title="Raw event explorer" sub={totalLabel} onClose={onClose} wide>
      <div className="event-explorer-tools">
        <input className="text" aria-label="Filter loaded events by type or first 4,000 data characters"
          placeholder="filter loaded type or first 4k data chars…"
          value={f} onChange={event => setF(event.target.value)} />
        <button type="button" className="btn sm" disabled={!timeline.hasMore.older || timeline.loading.older}
          onClick={timeline.loadOlder}>{timeline.loading.older ? 'Loading…' : 'Load older'}</button>
        {timeline.hasMore.newer && <button type="button" className="btn sm" disabled={timeline.loading.newer}
          onClick={timeline.loadNewer}>{timeline.loading.newer ? 'Loading…' : 'Load newer'}</button>}
        <button type="button" className="btn sm ghost" disabled={timeline.loading.tail || (!historyActive && timeline.followingTail && timeline.windowAtTail)}
          onClick={onReturnToLive || timeline.jumpToLive}>{timeline.loading.tail ? 'Refreshing…' : 'Latest'}</button>
      </div>
      {timeline.totalEvents != null && timeline.totalEvents > timeline.rows.length && <div className="timeline-window-note" role="note">
        Search covers the loaded window only. Page through the log to inspect other source events.
      </div>}
      {timeline.errors.tail && <div className="notice resource-error compact" role="alert">
        Newest events could not be refreshed. <button type="button" className="btn sm" onClick={() => timeline.retry('tail')}>Retry</button></div>}
      {timeline.errors.older && <div className="notice resource-error compact" role="alert">
        Could not load older events; current rows are unchanged. <button type="button" className="btn sm" onClick={timeline.loadOlder}>Retry</button></div>}
      {timeline.errors.newer && <div className="notice resource-error compact" role="alert">
        Newer-page refresh failed; loaded rows may be behind. <button type="button" className="btn sm" onClick={timeline.loadNewer}>Retry</button></div>}
      {timeline.errors.around && <div className="notice resource-error compact" role="alert">
        Replay window could not be loaded. <button type="button" className="btn sm" onClick={() => timeline.retry('around')}>Retry</button></div>}
      {timeline.tornTail && <div className="timeline-window-note warning" role="status">
        {timeline.sourceTailLimited ? 'Raw source tail exceeded the safety limit.' : 'Only the verified canonical event prefix is shown.'}
      </div>}
      {timeline.status === 'loading' && !timeline.rows.length
        ? <div className="timeline-resource muted" role="status">Loading events…</div>
        : timeline.status !== 'error' && rows.length === 0
          ? <div className="timeline-resource muted">{query ? 'No matches in the loaded window.' : 'No verified events.'}</div>
          : <VirtualTimeline rows={rows} getKey={explorerEventKey}
              identity={`${runId}:${timeline.generation || 'pending'}:explorer`}
              className="event-explorer-timeline" ariaLabel="Loaded raw events"
              followingTail={!historyActive && timeline.followingTail} windowAtTail={!historyActive && timeline.windowAtTail}
              unread={timeline.unread} unreadUnknown={timeline.unreadUnknown}
              busy={Object.values(timeline.loading).some(Boolean)}
              onFollowingTailChange={value => { if (!historyActive) timeline.setFollowingTail(value) }}
              onJumpToLive={onReturnToLive || timeline.jumpToLive}
              estimateSize={42}
              renderRow={item => <div className={'event-explorer-row' + (item.omitted ? ' omitted' : '')}>
                <span className="event-explorer-seq">{item.event.seq}</span>
                <span className="event-explorer-type">{item.event.type}</span>
                <span className="event-explorer-data">{item.preview}</span>
              </div>} />}
    </Panel>
  )
}

const _MAX_VIEW = 2_000_000   // mirrors server _ART_MAX_BYTES (inline text view cap)

// Artifacts browser: lists every file a run produced — the run directory (events/snapshots, per-node
// eval workdirs, operator subdirs) AND, for a RepoTask, the separate host repo / reference / data paths
// the task declared (where a training command may have written checkpoints / submissions / logs). Text
// files open inline; binary / oversize ones are flagged. Backed by GET /api/runs/{id}/artifacts (the
// listing) + /artifact (one file's content, traversal-guarded + 2 MB cap, both server-side).
export function ArtifactsPanel({ runId, onToast, onClose }) {
  const [roots, setRoots] = useState(null)
  const [err, setErr] = useState(null)
  const [open, setOpen] = useState({})        // root id -> expanded?
  const [sel, setSel] = useState(null)        // { root, path }
  const [content, setContent] = useState(null)
  const [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState('')
  const reqRef = useRef(0)                     // request token — drops out-of-order view() responses

  useEffect(() => {
    let alive = true
    get(`/api/runs/${encodeURIComponent(runId)}/artifacts`).then(d => {
      if (!alive) return
      const rs = d.roots || []
      setRoots(rs)
      const o = {}; rs.forEach(r => { o[r.id] = !!r.is_run_dir })   // expand the run dir, collapse repos
      setOpen(o)
    }).catch(e => alive && setErr(e.message))
    return () => { alive = false }
  }, [runId])

  const view = (rootId, f) => {
    const token = ++reqRef.current
    setSel({ root: rootId, path: f.path })
    setContent(null); setBusy(true)
    // Always fetch — don't trust the extension-based is_text GUESS (a text .bin/.pb would otherwise be
    // unviewable); the SERVER does the authoritative binary sniff. The token ignores a stale response
    // (fast-clicking A then B must not let A's slower reply render under B).
    get(`/api/runs/${encodeURIComponent(runId)}/artifact?root=${encodeURIComponent(rootId)}&path=${encodeURIComponent(f.path)}`)
      .then(c => { if (token === reqRef.current) setContent(c) })
      .catch(e => { if (token === reqRef.current) { setContent(null); onToast && onToast('view failed: ' + e.message) } })
      .finally(() => { if (token === reqRef.current) setBusy(false) })
  }

  const ql = filter.trim().toLowerCase()
  const binary = content && content.is_text === false   // the server's verdict, not the extension guess
  return (
    <Panel title="Artifacts" sub={runId} wide onClose={onClose}>
      {err && <div className="notice">Could not load artifacts: {err}</div>}
      {!roots ? <div className="muted">Loading…</div> :
        <div className="art-wrap">
          <div className="art-list">
            <input className="text art-filter" aria-label="Filter artifact files" placeholder="filter files…" value={filter}
                   onChange={e => setFilter(e.target.value)} />
            {roots.length === 0 && <div className="muted">No artifacts found for this run.</div>}
            {roots.map(r => {
              const isOpen = !!open[r.id]
              // Filter only the EXPANDED root — collapsed roots aren't rendered, so don't rescan their
              // (possibly large) file lists on every keystroke.
              const files = isOpen ? (ql ? r.files.filter(f => f.path.toLowerCase().includes(ql)) : r.files) : null
              return (
                <div className="art-root" key={r.id}>
                  <button type="button" className="art-root-h disclosure-button" title={r.path}
                       aria-expanded={isOpen} onClick={() => setOpen(o => ({ ...o, [r.id]: !isOpen }))}>
                    <span className="art-chev">{isOpen ? '▾' : '▸'}</span>
                    <b>{r.label}</b>
                    <span className="muted art-root-n">
                      {isOpen && ql ? `${files.length}/${r.n_files}` : `${r.n_files}${r.truncated ? '+' : ''}`}</span>
                    {!r.is_run_dir && <span className="pill art-tag" title={r.path}>repo path</span>}
                  </button>
                  {isOpen && <div className="art-files">
                    {files.length === 0 ? <div className="muted art-empty">{ql ? 'no match' : 'empty'}</div>
                      : files.map(f => (
                        <button type="button" key={f.path} title={f.path + (f.is_text ? '' : ' · looks binary')}
                             aria-pressed={!!(sel && sel.root === r.id && sel.path === f.path)}
                             className={'art-file disclosure-button' + (sel && sel.root === r.id && sel.path === f.path ? ' sel' : '')
                               + (f.is_text ? '' : ' bin')}
                             onClick={() => view(r.id, f)}>
                          <span className="art-name">{f.path}</span>
                          <span className="art-size">{fmtBytes(f.size)}</span>
                        </button>))}
                    {r.truncated && !ql && <div className="muted art-empty">… listing capped at {r.n_files} files</div>}
                  </div>}
                </div>
              )
            })}
          </div>
          <div className="art-view">
            {!sel ? <div className="muted art-hint">Select a file to view its contents.</div> : <>
              <div className="art-view-h">
                <span className="art-view-path" title={sel.path}>{sel.path}</span>
                {content && <span className="muted">{fmtBytes(content.size)}</span>}
              </div>
              {busy ? <div className="muted">Loading…</div>
                : binary ? <div className="notice">Binary file — not shown inline ({fmtBytes(content.size)}).</div>
                : content ? <>
                    {content.truncated && <div className="notice art-trunc">Showing the first {fmtBytes(_MAX_VIEW)} — the file is larger.</div>}
                    <pre className="art-pre" role="region" aria-label={`Artifact ${sel.path} contents`} tabIndex={0}>{content.content}</pre>
                  </> : <div className="muted art-hint">Could not load this file.</div>}
            </>}
          </div>
        </div>}
    </Panel>
  )
}
