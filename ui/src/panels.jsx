import React, { useEffect, useMemo, useRef, useState } from 'react'
import { get, putText, post, fmt, fmtInt, fmtBytes, CONTROL, gpuStat, saveRunConfig, resumeRun, apiPrefix, operatorMeta } from './util.js'
import { Bars, ParallelCoords, Scatter, MultiTrajectory } from './charts.jsx'
import { hyperImportance } from './report.js'
import Markdown from './markdown.jsx'
import { OpIcon } from './icons.jsx'
import { diffLines } from './Inspector.jsx'
import SettingsForm from './SettingsForm.jsx'
import { toForm, fromForm, FIELD_BY_KEY } from './settingsSchema.js'

export function Panel({ title, sub, onClose, children, wide }) {
  return (
    <div className="overlay" onClick={onClose}>
      <div className="panel" style={wide ? { width: 'min(1100px, 95%)' } : {}} onClick={e => e.stopPropagation()}>
        <div className="panel-h"><span className="ttl">{title}</span>{sub && <span className="pill">{sub}</span>}<span className="right" /><button className="btn sm ghost" onClick={onClose}>✕</button></div>
        <div className="panel-b">{children}</div>
      </div>
    </div>
  )
}

const Stat = ({ n, l }) => <div className="stat"><div className="n">{n}</div><div className="l">{l}</div></div>

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
        <Stat n={fmt(evalSec, 1) + 's' + (maxEval ? ' / ' + maxEval : '')} l="eval time" />
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
        {state.reward_hacks?.length > 0 && <span className="chip alarm" style={{ cursor: 'pointer' }}
          title="suspicious wins flagged (B5)" onClick={() => onOpenPanel && onOpenPanel('trust')}><OpIcon name="alert" size={11} /> hack? {state.reward_hacks.length}</span>}
      </div>}
    </Panel>
  )
}

// Deep-research drawer: every memo in one place (instead of scrolling the timeline feed), with
// ACTIONABLE directions — "steer →" posts a hint the Researcher folds into the next proposal. Deep
// research is no longer a DAG node; this drawer + the Dock timeline marker are its home.
export function ResearchPanel({ state, runId, onToast, onClose }) {
  const memos = [...(state.research || [])].reverse()   // newest first
  const steer = async (text) => {
    try { await CONTROL.hint(runId, 'try this research direction: ' + text); onToast && onToast('steered the next proposal →') }
    catch { onToast && onToast('could not steer (run offline?)') }
  }
  return (
    <Panel title="Deep research" sub={memos.length ? `${memos.length} memo${memos.length === 1 ? '' : 's'}` : 'none yet'} onClose={onClose} wide>
      {!memos.length && <div className="muted">No deep-research memos yet. Trigger one with <code>/deep-research</code> in the chat, or set a cadence in Config.</div>}
      {memos.map((m, i) => (
        // Key by the STABLE original index (research is append-only), not the reversed position:
        // keyed by `i`, a new memo landing at index 0 reuses the prior memo's DOM node and its open
        // <details> state bleeds onto the new one.
        <div className="rsch-memo" key={memos.length - 1 - i}>
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
          {m.verification && (m.verification.verdicts || []).length > 0 && <>
            <div className="section-h">Verified claims
              {m.verification.unsupported > 0 &&
                <span className="pill warn" title="claims whose cited evidence does not support them">
                  {m.verification.unsupported} unsupported</span>}
              <span className="muted"> ({m.verification.method})</span></div>
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
            <ul className="bul">{m.sources.map((s, j) => (
              <li key={j}>{s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.title || s.url}</a> : (s.title || 'source')}
                {s.snippet && <div className="muted">{String(s.snippet).slice(0, 160)}</div>}</li>))}</ul></>}
          {m.reasoning && <details className="rsch-reasoning"><summary>reasoning (debug)</summary>
            <Markdown className="think-body" text={m.reasoning} /></details>}
        </div>
      ))}
    </Panel>
  )
}

export function TrustPanel({ state, runId, onClose, onSelect, onToast }) {
  const [cfg, setCfg] = useState(null)
  useEffect(() => { get(`/api/runs/${runId}/config`).then(setCfg).catch(() => {}) }, [runId])
  const quarantine = async (id) => {   // U6: act on a flagged node — remove it from the search
    try { await CONTROL.nodeAbort(runId, id); onToast && onToast(`quarantined #${id} (aborted)`) }
    catch { onToast && onToast('could not quarantine (run not live?)') }
  }
  const nodes = Object.values(state.nodes)
  const evald = nodes.filter(n => n.metric != null && n.feasible !== false)
  const chooser = state.direction === 'min' ? (a, b) => a < b : (a, b) => a > b
  const naive = evald.slice().sort((a, b) => chooser(a.metric, b.metric) ? -1 : 1)[0]
  const robust = state.best_node_id != null ? state.nodes[state.best_node_id] : null
  const leak = state.leakage
  return (
    <Panel title="Trust & rigor" sub="the point of LoopLab" onClose={onClose} wide>
      <div className="cardgrid" style={{ marginBottom: 14 }}>
        <Stat n={cfg?.trust_mode || '—'} l="sandbox tier" />
        <Stat n={cfg?.eval_trust_mode || '—'} l="eval trust mode" />
        <Stat n={state.host_grading ? 'host-side' : 'self-reported'} l="metric scoring" />
        <Stat n={state.workspace_changed ? 'changed' : 'pinned'} l="workspace repro" />
      </div>
      {state.host_grading && <div className="chip ok" style={{ marginBottom: 12 }}>
        Out-of-process grading: the candidate writes predictions only; the host scores them
        ({state.host_grading.scorer}, {state.host_grading.n_labels} held-out labels) — the answer key
        never touches the candidate process, so the metric can’t be self-reported.</div>}

      <div className="section-h">Seed-luck check (naive leader vs robust winner)</div>
      {robust && naive
        ? <div className="kv">
          <div className="k">naive single-eval leader</div><div className="v">#{naive.id} · {fmt(naive.metric)}</div>
          <div className="k">selected (robust) winner</div><div className="v">#{robust.id} · {fmt(robust.confirmed_mean ?? robust.metric)}{robust.confirmed_mean != null ? ` ±${fmt(robust.confirmed_std)}` : ' (unconfirmed)'}</div>
          {naive.id !== robust.id && <><div className="k flag">demotion</div><div className="v">single-eval leader #{naive.id} was NOT selected — multi-seed confirmation corrected a seed-lucky result.</div></>}
        </div>
        : <div className="muted">No feasible evaluated nodes yet.</div>}

      <div className="section-h">Leakage scan {leak && leak.leak && <span className="chip alarm">LEAK — run refused</span>}</div>
      {leak
        ? <table className="tbl"><thead><tr><th>detector</th><th>leak</th><th>detail</th></tr></thead><tbody>
          {(leak.verdicts || []).map((v, i) => <tr key={i}>
            <td>{v.detector}</td><td style={{ color: v.leak ? 'var(--fail)' : 'var(--ok)' }}>{v.leak ? 'YES' : 'no'}</td>
            <td className="muted">{Object.entries(v).filter(([k]) => !['detector', 'leak'].includes(k)).map(([k, val]) => `${k}=${typeof val === 'object' ? JSON.stringify(val) : val}`).join('  ')}</td>
          </tr>)}</tbody></table>
        : <div className="chip ok">no leakage scan recorded (or task exposes no split/target/time data)</div>}

      <div className="section-h">Drift cross-check</div>
      {(state.drifts || []).length
        ? <table className="tbl"><thead><tr><th>node</th><th>primary</th><th>cross</th><th>tolerance</th></tr></thead><tbody>
          {state.drifts.map((d, i) => <tr key={i}><td className="flag">#{d.node_id}</td><td>{fmt(d.primary)}</td><td>{fmt(d.cross)}</td><td>{fmt(d.tolerance)}</td></tr>)}</tbody></table>
        : <div className="chip ok">no metric drift detected</div>}

      <div className="section-h">Reward-hacking monitor (B5) {(state.reward_hacks || []).length > 0 && <span className="chip alarm">{state.reward_hacks.length} flagged</span>}</div>
      {/* Folded state is the enforcement truth (it applies trust_gate_changed events);
          the config snapshot alone can claim a gate the fold never engages. */}
      {(state.trust_gate || cfg) && <div className="muted" style={{ marginBottom: 6 }}>
        enforcement: <b>{state.trust_gate || cfg?.trust_gate || 'audit'}</b>{(state.trust_gate || cfg?.trust_gate || 'audit') === 'audit'
          ? ' — flags are logged only; set trust_gate=gate/block (or the thorough profile) to keep a flagged node from winning.'
          : ' — a flagged node is excluded from best-selection.'}</div>}
      {(state.reward_hacks || []).length
        ? <table className="tbl"><thead><tr><th>node</th><th>signal</th><th>detail</th><th /></tr></thead><tbody>
          {state.reward_hacks.map((h, i) => <tr key={i}>
            <td className="flag"><button className="btn xs ghost" onClick={() => { onSelect && onSelect(h.node_id); onClose() }}>#{h.node_id}</button></td>
            <td>{(h.signals || []).map(s => s.signal).join(', ')}</td>
            <td className="muted">{(h.signals || []).map(s => s.detail).filter(Boolean).join(' · ')}</td>
            <td><button className="btn xs ghost" title="quarantine: abort this node so it can't be selected"
              onClick={() => quarantine(h.node_id)}>quarantine</button></td>
          </tr>)}</tbody></table>
        : <div className="chip ok">no suspicious wins flagged{cfg && !cfg.reward_hack_detect ? ' (detector off — enable reward_hack_detect)' : ''}</div>}
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
      <table className="tbl"><thead><tr><th>node</th><th>reason</th><th>error</th></tr></thead><tbody>
        {failed.map(n => <tr key={n.id} style={{ cursor: 'pointer' }} onClick={() => { onSelect(n.id); onClose() }}>
          <td>#{n.id}</td><td className="flag">{n.error_reason}</td><td className="muted">{(n.error || '').slice(0, 80)}</td></tr>)}
      </tbody></table>
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
  const confirmReq = (state.confirm_requests || []).filter(id => !(state.confirmed_forced || []).includes(id))
  const ablateReq = state.ablate_requests || []
  const cancel = async (id) => {
    try { await CONTROL.nodeAbort(runId, id); onToast && onToast(`cancelled #${id}`) }
    catch { onToast && onToast('could not cancel (run not live?)') }
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
        ? <table className="tbl"><thead><tr><th>node</th><th>op</th><th>parents</th><th>hypothesis / rationale</th><th /></tr></thead><tbody>
          {pending.map(n => <tr key={n.id}>
            <td><button className="btn xs ghost" onClick={() => { onSelect && onSelect(n.id); onClose() }}>#{n.id}</button></td>
            <td><span className="op-icon"><OpIcon name={operatorMeta(n.operator).icon} size={12} /></span> {n.operator}</td>
            <td className="muted">{(n.parent_ids || []).map(p => '#' + p).join(', ') || '—'}</td>
            <td className="muted">{(n.idea?.hypothesis || n.idea?.rationale || '').slice(0, 70)}</td>
            <td><button className="btn xs ghost" title="cancel this experiment (node_abort)" onClick={() => cancel(n.id)}><OpIcon name="cross" size={11} /></button></td>
          </tr>)}
        </tbody></table>
        : <div className="muted">No experiment is queued right now — the loop is idle or between picks.</div>}
      {(injects.length + forks.length + confirmReq.length + ablateReq.length) > 0 && <>
        <div className="section-h">Queued control requests</div>
        <div className="chips">
          {injects.map((q, i) => <span key={'i' + i} className="chip sm" title="operator-injected experiment awaiting materialization">inject: {(q.idea?.operator || 'experiment')}</span>)}
          {forks.map((q, i) => <span key={'f' + i} className="chip sm" title="fork awaiting materialization">fork #{q.from_node_id ?? q.parent_id ?? '?'}</span>)}
          {confirmReq.map((id, i) => <span key={'c' + i} className="chip sm">confirm #{id}</span>)}
          {ablateReq.map((id, i) => <span key={'a' + i} className="chip sm">ablate #{id}</span>)}
        </div>
      </>}
    </Panel>
  )
}

// U6 · live "why" strip: a compact, always-visible narration of the loop's latest AUTONOMOUS
// decisions — folded from the events LoopLab already logs (policy "why-this-node", A7 Strategist,
// the unified agent's macro-action). Builds operator trust ("it explored because it stalled") so a
// run can be left unattended. Click a referenced node to jump to it. Pure projection; no new events.
export function WhyStrip({ state, onSelect }) {
  const items = []
  const strat = (state.strategy_history || [])[(state.strategy_history || []).length - 1]
  if (strat && (strat.strategy?.rationale || strat.strategy?.policy)) {
    items.push({ icon: 'compass', label: 'strategy',
      text: strat.strategy.rationale || `policy → ${strat.strategy.policy}`, at: strat.at_node })
  }
  const dec = (state.agent_decisions || [])[(state.agent_decisions || []).length - 1]
  if (dec && (dec.rationale || dec.chosen)) {
    // `chosen` used to be a bare node id but the agentic policy makes it an ACTION object
    // {kind, parent_id, parent_ids, node_id} — render a readable label, never the raw object
    // (a raw object as a JSX child is React error #31, which black-screened the whole run view).
    const ch = dec.chosen
    const label = (ch && typeof ch === 'object')
      ? `${ch.kind || 'action'}${ch.node_id != null ? ' #' + ch.node_id : (ch.parent_id != null ? ' from #' + ch.parent_id : '')}`
      : (ch || 'action')
    items.push({ icon: 'bolt', label, text: dec.rationale || '', at: dec.at_node })
  }
  if (state.policy_reason) {
    items.push({ icon: 'target', label: 'policy', node: state.policy_chosen,
      text: `${state.policy_reason}${state.policy_chosen != null ? ` → #${state.policy_chosen}` : ''}` })
  }
  if (!items.length) return null
  return (
    <div className="why-strip" title="why the loop is doing what it's doing (live)">
      {items.slice(0, 3).map((it, i) => <span key={i} className="why-item"
        onClick={() => it.node != null && onSelect && onSelect(it.node)}
        style={{ cursor: it.node != null ? 'pointer' : 'default' }}>
        <OpIcon name={it.icon} size={12} className="why-ic" />
        <b>{it.label}</b> {it.text}{it.at != null ? <span className="muted"> @{it.at}</span> : null}
      </span>)}
    </div>
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
            ? <table className="tbl"><thead><tr><th>node</th><th>metric</th>{keys.map(k => <th key={k}>{k}</th>)}</tr></thead><tbody>
                {front.sort((a, b) => (state.direction === 'min' ? a.metric - b.metric : b.metric - a.metric)).map(n =>
                  <tr key={n.id}><td>#{n.id}{n.id === state.best_node_id ? <OpIcon name="crown" size={10} /> : ''}</td><td>{fmt(n.confirmed_mean ?? n.metric)}</td>
                    {keys.map(k => <td key={k} className="muted">{fmt(n.extra_metrics?.[k])}</td>)}</tr>)}
              </tbody></table>
            : <div className="muted">Single-objective task — the Pareto front is just the best node (#{state.best_node_id ?? '—'}). Add extra_metrics (e.g. latency, size) to trade off.</div>}
        </>
      })()}
      <div className="section-h">Pareto (metric vs constraint)</div>
      {scatter || <div className="muted">No constraints/aux metrics in this task.</div>}
      <div className="section-h">Diversity archive {archive && <span className="pill">{archive.niches} niches</span>}</div>
      {archive?.elites?.length
        ? <table className="tbl"><thead><tr><th>node</th><th>metric</th><th>params</th></tr></thead><tbody>
          {archive.elites.map((e, i) => <tr key={i}><td>#{e.node_id}</td><td>{fmt(e.metric)}</td><td className="muted">{JSON.stringify(e.params)}</td></tr>)}</tbody></table>
        : <div className="muted">No archive (run not finished).</div>}
      <div className="section-h">Operator productivity</div>
      <table className="tbl"><thead><tr><th>operator</th><th>nodes</th><th>evaluated</th></tr></thead><tbody>
        {Object.entries(ops).map(([o, s]) => <tr key={o}><td>{o}</td><td>{s.n}</td><td>{s.ev}</td></tr>)}</tbody></table>
    </Panel>
  )
}

export function DataQualityPanel({ state, onClose }) {
  const prof = state.data_profile
  if (!prof) return <Panel title="Data quality" onClose={onClose}><div className="muted">No data profile (task exposes no dataset).</div></Panel>
  const cols = Object.entries(prof)
  return (
    <Panel title="Data quality" sub={`${cols.length} columns`} onClose={onClose} wide>
      <table className="tbl"><thead><tr><th>column</th><th>dtype</th><th>missing%</th><th>unique</th><th>min</th><th>max</th><th>mean</th><th>flags</th></tr></thead><tbody>
        {cols.map(([c, s]) => <tr key={c}>
          <td>{c}</td><td>{s.dtype}</td><td>{fmt((s.missing_frac || 0) * 100, 3)}</td><td>{fmtInt(s.n_unique)}</td>
          <td>{fmt(s.min)}</td><td>{fmt(s.max)}</td><td>{fmt(s.mean)}</td>
          <td>{s.constant && <span className="flag">constant </span>}{s.high_missing && <span className="flag">high-missing</span>}</td></tr>)}
      </tbody></table>
    </Panel>
  )
}

// Per-run config: shows the run's config.snapshot.json and lets you EDIT it. Edits are saved back to
// the snapshot, which a later RESUME re-reads (resume does NOT pick up the UI's global new-run
// defaults), so this is how you change a specific run's settings (e.g. raise `timeout`, enable timeout
// repair). Works for live runs too: saving the snapshot is safe mid-run (the engine never re-reads it),
// and a "Pause & resume" applies it now by restarting the engine (pause → wait for it to stop → resume).
export function ConfigPanel({ runId, state, live, onClose, onToast }) {
  const [cfg, setCfg] = useState(null)
  const [form, setForm] = useState(null)
  const [saved, setSaved] = useState(null)   // last-persisted form (to detect unsaved edits)
  const [agentControl, setAgentControl] = useState({})   // per-run governance matrix (agent_control)
  const [savedAC, setSavedAC] = useState({})
  const [sec, setSec] = useState('')
  const [busy, setBusy] = useState(false)
  const [raw, setRaw] = useState(false)
  const load = () => get(`/api/runs/${runId}/config`).then(c => {
    setCfg(c); const f = toForm(c); setForm(f); setSaved(f)
    const ac = c.agent_control || {}; setAgentControl(ac); setSavedAC(ac)
  }).catch(() => {})
  useEffect(() => { load() }, [runId])

  // A live engine keeps its in-memory settings until it restarts; gate on `live` (not the possibly
  // historical `state`) so time-travel doesn't misreport liveness.
  const engineLive = live?.engine_running === true
  const dirty = useMemo(() => {
    if (!form || !saved) return new Set()
    const cur = fromForm(form), base = fromForm(saved), s = new Set()
    for (const k of Object.keys(FIELD_BY_KEY)) if (JSON.stringify(cur[k]) !== JSON.stringify(base[k])) s.add(k)
    return s
  }, [form, saved])
  const acDirty = useMemo(() => JSON.stringify(agentControl) !== JSON.stringify(savedAC), [agentControl, savedAC])
  const canSave = dirty.size > 0 || acDirty
  const onChange = (k, v) => setForm(f => ({ ...f, [k]: v }))
  const onToggleAgent = (key, role) => setAgentControl(ac => {
    const cur = new Set(ac[key] || []); cur.has(role) ? cur.delete(role) : cur.add(role)
    return { ...ac, [key]: [...cur] }
  })
  const sleep = ms => new Promise(r => setTimeout(r, ms))

  const onSave = async () => {
    const cur = fromForm(form), changed = {}
    for (const k of dirty) changed[k] = cur[k]    // send ONLY edited fields (minimal snapshot diff)
    if (acDirty) changed.agent_control = agentControl
    if (!Object.keys(changed).length) return
    setBusy(true)
    try {
      const r = await saveRunConfig(runId, changed)
      const f = toForm(r.config); setCfg(r.config); setForm(f); setSaved(f)
      const ac = r.config.agent_control || {}; setAgentControl(ac); setSavedAC(ac)
      const what = r.changed?.length ? `saved ${r.changed.join(', ')}` : 'saved'
      onToast(what + (r.engine_running ? ' — applies when the live run restarts' : ' — applies on next resume'))
    } catch (e) { onToast('save failed: ' + e.message) }   // e.message now carries the server detail (e.g. which field)
    finally { setBusy(false) }
  }
  const onResume = async () => {           // stalled/finished: just spawn the engine (re-reads the snapshot)
    setBusy(true)
    try { await resumeRun(runId); onToast('resuming with the saved settings…') }
    catch (e) { onToast('resume failed: ' + e.message) }
    finally { setBusy(false) }
  }
  const onPauseResume = async () => {      // live: pause → wait for the engine to stop → resume with new settings
    setBusy(true)
    try {
      onToast('pausing — the current experiment finishes first…')
      await CONTROL.pause(runId)
      let stopped = false
      for (let i = 0; i < 900 && !stopped; i++) {   // poll up to ~15 min (a node can hold an eval that long)
        await sleep(1000)
        const s = await get(`/api/runs/${runId}/state`).then(r => r.state).catch(() => null)
        if (s && s.engine_running === false) stopped = true
      }
      if (!stopped) { onToast('still busy — retry once the current experiment finishes'); return }
      await CONTROL.resume(runId)          // clear the paused flag so the fresh engine doesn't immediately re-pause
      await resumeRun(runId)               // new engine process re-reads the snapshot
      onToast('resumed with the new settings')
    } catch (e) { onToast('pause/resume failed: ' + e.message) }
    finally { setBusy(false) }
  }

  const rawTable = <table className="tbl"><tbody>{cfg && Object.entries(cfg).map(([k, v]) =>
    <tr key={k}><td className="muted">{k}</td><td>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</td></tr>)}</tbody></table>

  return (
    <Panel title="Run settings" sub={engineLive ? 'live · applies on restart' : 'edit · applies on resume'} onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <span className="muted">extend eval budget:</span>
        <input className="text" style={{ width: 120 }} placeholder="seconds" value={sec} onChange={e => setSec(e.target.value)} />
        <button className="btn sm primary" disabled={!sec} onClick={async () => { await CONTROL.budget(runId, Number(sec)); onToast('budget extended +' + sec + 's') }}>apply</button>
      </div>
      {!form ? <div className="muted">…</div> : <>
        <div className="notice" style={{ marginBottom: 10 }}>
          {engineLive
            ? <>This run is <b>live</b>. Saving updates its <code>config.snapshot.json</code>, but the running engine keeps its current settings until it restarts — use <b>Pause &amp; resume</b> to stop it (the current experiment finishes first) and continue with the new settings.</>
            : <>Edits are saved to this run's <code>config.snapshot.json</code> and applied on the next <b>resume</b>.</>}
          {' '}<span className="sf-dot unsaved">●</span> = changed.
        </div>
        <div className="toolbar" style={{ marginBottom: 10 }}>
          <span className="spacer" style={{ flex: 1 }} />
          <button className="btn sm ghost" onClick={() => setRaw(r => !r)}>{raw ? 'form' : 'raw'}</button>
          <button className="btn sm ghost" disabled={busy || !canSave} onClick={() => { setForm(saved); setAgentControl(savedAC) }}>↺ revert</button>
          <button className="btn sm primary" disabled={busy || !canSave} onClick={onSave}>Save</button>
          {engineLive
            ? <button className="btn sm" disabled={busy || canSave} onClick={onPauseResume} title="pause the run, then resume it with the saved settings">Pause &amp; resume ▸</button>
            : <button className="btn sm" disabled={busy || canSave} onClick={onResume} title="continue this run with the saved settings">Resume ▸</button>}
        </div>
        {/* This panel's `dirty` is changed-vs-saved (unsaved), so feed it as `unsaved` → the amber dot that clears on Save. */}
        {raw ? rawTable : <SettingsForm form={form} onChange={onChange} unsaved={dirty} agentControl={agentControl} onToggleAgent={onToggleAgent} hideSecret />}
      </>}
    </Panel>
  )
}

export function AuthoringPanel({ onClose, onToast }) {
  const [kind, setKind] = useState('prompts')
  const [data, setData] = useState({ dir: null, files: [] })
  const [sel, setSel] = useState(null)
  const [text, setText] = useState('')
  const load = (k) => get(`/api/${k}`).then(d => { setData(d); setSel(null); setText('') }).catch(() => setData({ dir: null, files: [] }))
  useEffect(() => { load(kind) }, [kind])
  return (
    <Panel title="Authoring — configure the scientist" sub="hot-reloaded next run" onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 10 }}>
        {['prompts', 'skills', 'knowledge'].map(k => <button key={k} className={'btn sm' + (k === kind ? ' primary' : '')} onClick={() => setKind(k)}>{k}</button>)}
        <span className="muted">{data.dir || `no ${kind} dir configured (set LOOPLAB_${kind.toUpperCase()}_DIR)`}</span>
      </div>
      <div style={{ display: 'flex', gap: 12 }}>
        <div style={{ width: 200 }}>
          {data.files.map(f => <div key={f.name} className={'run-card' + (sel === f.name ? ' sel' : '')} style={{ padding: 8 }} onClick={() => { setSel(f.name); setText(f.text) }}>{f.name}</div>)}
          {!data.files.length && <div className="muted">no files</div>}
        </div>
        <div style={{ flex: 1 }}>
          {sel ? <>
            <textarea className="text" value={text} onChange={e => setText(e.target.value)} />
            <button className="btn sm primary" style={{ marginTop: 8 }} onClick={async () => { await putText(`/api/${kind}/${sel}`, text); onToast('saved ' + sel) }}>Save</button>
          </> : <div className="muted">select a file to edit</div>}
        </div>
      </div>
    </Panel>
  )
}

function KbNote({ note }) {
  const [open, setOpen] = useState(false)
  return <div className="mem-card">
    <div style={{ cursor: 'pointer', fontWeight: 600 }} onClick={() => setOpen(o => !o)}>
      <span style={{ opacity: 0.6, fontSize: 10, marginRight: 4 }}>{open ? '▾' : '▸'}</span>{note.name}</div>
    {open && <div style={{ marginTop: 6 }}><Markdown>{note.text || note.content || ''}</Markdown></div>}
  </div>
}

export function MemoryPanel({ onClose }) {
  // Everything the run has LEARNED, in one place: distilled lessons, solved-task cases, meta-notes, and
  // the agentic knowledge-base markdown notes (best configs / recipes the agents save + later retrieve).
  const [mem, setMem] = useState({ dir: null, cases: [], lessons: [], notes: [] })
  const [kb, setKb] = useState({ dir: null, files: [] })   // /api/knowledge → {dir, files:[{name,text}]}
  const [tab, setTab] = useState('lessons')
  useEffect(() => {
    get('/api/memory').then(setMem).catch(() => {})
    get('/api/knowledge').then(setKb).catch(() => {})
  }, [])
  const kbFiles = kb.files || []
  const tabs = [['lessons', 'Lessons', mem.lessons.length], ['cases', 'Cases', mem.cases.length],
    ['notes', 'Notes', mem.notes.length], ['knowledge', 'Knowledge', kbFiles.length]]
  return (
    <Panel title="Memory & knowledge — what the runs have learned" sub={mem.dir || 'no memory dir'} onClose={onClose} wide>
      <div className="conv-toggle" style={{ marginBottom: 12 }}>
        {tabs.map(([k, label, n]) => <button key={k} className={'seg' + (tab === k ? ' on' : '')}
          onClick={() => setTab(k)}>{label} <span className="muted">{n}</span></button>)}
      </div>
      {tab === 'lessons' && (mem.lessons.length
        ? mem.lessons.map((l, i) => <div key={i} className="mem-card">
            <div>{l.statement}</div>
            <div className="mem-meta" style={{ marginTop: 4, display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
              {l.kind && <span className="chip xs">{l.kind}</span>}
              {l.outcome && <span className="chip xs">{l.outcome}</span>}
              {l.delta != null && <span className={'chip xs' + (l.delta > 0 ? ' ok' : '')}>Δ{fmt(l.delta)}</span>}
              {l.confidence != null && <span className="muted" style={{ fontSize: 11 }}>conf {Math.round(l.confidence * 100)}%</span>}
              {l.evidence_count ? <span className="muted" style={{ fontSize: 11 }}>· {l.evidence_count} evidence</span> : null}
              {l.task_id && <span className="muted" style={{ fontSize: 11 }}>· {l.task_id}</span>}
            </div>
          </div>)
        : <div className="muted">No lessons yet — they accrue as runs finish (reflection distils them into memory).</div>)}
      {tab === 'cases' && (mem.cases.length
        ? <table className="tbl"><thead><tr><th>task</th><th>goal</th><th>metric</th><th>params</th></tr></thead><tbody>
          {mem.cases.map((c, i) => <tr key={i}><td>{c.task_id}</td><td className="muted">{c.goal}</td><td>{fmt(c.metric)}</td><td className="muted">{JSON.stringify(c.params)}</td></tr>)}</tbody></table>
        : <div className="muted">No cases stored.</div>)}
      {tab === 'notes' && (mem.notes.length
        ? mem.notes.map((n, i) => <div key={i} className="mem-card">
            {n.task_id && <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>{n.task_id}</div>}
            <Markdown>{n.note || n.statement || JSON.stringify(n)}</Markdown></div>)
        : <div className="muted">No meta-notes yet.</div>)}
      {tab === 'knowledge' && (kbFiles.length
        ? <><div className="muted" style={{ fontSize: 11, marginBottom: 6 }}>{kb.dir} — agents save + retrieve these via kb_search</div>
          {kbFiles.map((n, i) => <KbNote key={i} note={n} />)}</>
        : <div className="muted">No knowledge notes ({kb.dir || 'no knowledge dir'}).</div>)}
    </Panel>
  )
}

export function RegistryPanel({ state, onClose }) {
  const [runs, setRuns] = useState([])
  useEffect(() => { get('/api/runs').then(setRuns).catch(() => {}) }, [])
  const champ = state.champion != null ? state.nodes[state.champion] : (state.best_node_id != null ? state.nodes[state.best_node_id] : null)
  return (
    <Panel title="Solution registry & cross-run" onClose={onClose} wide>
      <div className="section-h">Champion (this run)</div>
      {champ ? <div className="kv"><div className="k">node</div><div className="v">#{champ.id} {state.champion != null ? '(promoted)' : '(auto-best)'}</div>
        <div className="k">metric</div><div className="v">{fmt(champ.confirmed_mean ?? champ.metric)}</div></div> : <div className="muted">no champion yet</div>}
      <div className="toolbar" style={{ marginTop: 6 }}>
        <button className="btn sm" onClick={async () => {
          const p = await get(`/api/runs/${state.run_id}/prov`)
          const blob = new Blob([JSON.stringify(p, null, 2)], { type: 'application/json' })
          const a = document.createElement('a'); a.href = URL.createObjectURL(blob)
          a.download = `${state.run_id}_prov.json`; a.click(); URL.revokeObjectURL(a.href)
        }}><OpIcon name="download" size={12} /> W3C-PROV graph (JSON)</button>
      </div>
      <div className="section-h">Promotions</div>
      {(state.promotions || []).length
        ? <table className="tbl"><thead><tr><th>node</th><th>alias</th></tr></thead><tbody>{state.promotions.map((p, i) => <tr key={i}><td>#{p.node_id}</td><td>{p.alias || 'champion'}</td></tr>)}</tbody></table>
        : <div className="muted">none — use Promote on a node</div>}
      <div className="section-h">Cross-run leaderboard</div>
      <table className="tbl"><thead><tr><th>run</th><th>task</th><th>phase</th><th>best</th><th>nodes</th></tr></thead><tbody>
        {[...runs].sort((a, b) => (b.best_confirmed ?? b.best_metric ?? -Infinity) - (a.best_confirmed ?? a.best_metric ?? -Infinity))
          .map(r => <tr key={r.run_id}><td>{r.run_id}</td><td className="muted">{r.task_id}</td><td>{r.phase}</td><td>{fmt(r.best_confirmed ?? r.best_metric)}</td><td>{r.nodes}</td></tr>)}
      </tbody></table>
    </Panel>
  )
}

// Live GPU telemetry (nvidia-smi via /api/gpu). Polls while open so an operator can watch
// utilization / VRAM / power during a real training run without leaving the browser.
export function GpuPanel({ onClose }) {
  const [data, setData] = useState(null)
  useEffect(() => {
    let on = true
    const tick = () => gpuStat().then(d => on && setData(d)).catch(() => on && setData({ available: false }))
    tick(); const t = setInterval(tick, 2000)
    return () => { on = false; clearInterval(t) }
  }, [])
  const bar = (v, max, hot) => <div className="bar" style={{ height: 8 }}>
    <div className={'fill' + (hot ? ' hot' : '')} style={{ width: Math.min(100, max ? v / max * 100 : 0) + '%' }} /></div>
  return (
    <Panel title="GPU monitor" sub="nvidia-smi · live" onClose={onClose} wide>
      {!data ? <div className="muted">polling…</div>
        : !data.available ? <div className="notice">No GPU / nvidia-smi not available on the server host.</div>
          : (data.gpus || []).map((g, i) => (
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

// "Research" — the Deep-Research stage memos (Phase 2). One entry per `research_completed` memo:
// the model's conclusion (summary/findings/recommended_directions) up front + the consulted sources
// as clickable links; the raw deliberation sits in a collapsed "reasoning (debug)" disclosure. The
// `focus` index (set when an operator clicks a research node in the DAG) auto-expands that memo.
export function MemoCard({ memo, idx, open, onToggle }) {
  const [think, setThink] = useState(false)
  return (
    <div className="memo-card">
      <div className="memo-head" onClick={() => onToggle(idx)}>
        <span className="span-tw">{open ? '▾' : '▸'}</span>
        <b><OpIcon name="search" className="t-ic" /> memo #{idx + 1}</b>
        {memo.trigger && <span className="pill">{memo.trigger}</span>}
        {memo.at_node != null && <span className="muted"> @{memo.at_node} nodes</span>}
        <span className="spacer" style={{ flex: 1 }} />
        <span className="muted">{(memo.sources || []).length} source{(memo.sources || []).length === 1 ? '' : 's'}</span>
      </div>
      {open && <div className="memo-body">
        <div className="section-h">Conclusion</div>
        <div className="v">{memo.summary || '—'}</div>
        {(memo.findings || []).length > 0 && <>
          <div className="section-h">Findings</div>
          <ul className="bul">{memo.findings.map((f, i) => <li key={i}>{f}</li>)}</ul></>}
        {(memo.recommended_directions || []).length > 0 && <>
          <div className="section-h">Recommended directions (fed to the Researcher)</div>
          <ul className="bul">{memo.recommended_directions.map((d, i) => <li key={i}>{d}</li>)}</ul></>}
        {(memo.sources || []).length > 0 && <>
          <div className="section-h">Sources consulted</div>
          <ul className="bul">{memo.sources.map((s, i) => <li key={i}>
            {s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.title || s.url}</a> : (s.title || '—')}
            {s.snippet && <span className="muted"> — {String(s.snippet).slice(0, 120)}</span>}</li>)}</ul></>}
        {memo.reasoning && <div className="think-debug" style={{ marginTop: 8 }}>
          <div className="role-think" onClick={() => setThink(v => !v)} style={{ cursor: 'pointer', fontSize: 11, textTransform: 'uppercase', letterSpacing: '.5px' }}>
            {think ? '▾' : '▸'} reasoning (debug)</div>
          {think && <Markdown className="think-body" text={memo.reasoning} />}
        </div>}
      </div>}
    </div>
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
            <table className="tbl"><thead><tr><th>param</th><th>importance</th><th>r</th><th>n</th><th></th></tr></thead><tbody>
              {rows.map(row => <tr key={row.k}>
                <td>{row.k}</td><td>{fmt(row.imp, 3)}</td>
                <td className="muted">{row.r >= 0 ? '+' : ''}{fmt(row.r, 3)}</td><td className="muted">{row.n}</td>
                <td style={{ width: 160 }}><div className="bar" style={{ height: 8 }}>
                  <div className="fill" style={{ width: Math.min(100, row.imp / top * 100) + '%' }} /></div></td></tr>)}
            </tbody></table>
            <div className="muted" style={{ marginTop: 8 }}>Sign of r shows direction: with a {state.direction === 'min' ? 'minimize' : 'maximize'} objective,
              a {state.direction === 'min' ? 'negative' : 'positive'} r means a larger value tends to help. Needs ≥3 evaluated nodes per param.</div></>
        : <div className="muted">Not enough numeric-param data yet — run more experiments (≥3 evaluated nodes that share a numeric param).</div>}
    </Panel>
  )
}

// F2 · Cross-run sweep aggregation — a lab dashboard overlaying every run of the same task:
// best-metric comparison + which settings won. Uses the /api/runs summary (no per-run refetch).
export function CrossRunPanel({ state, onClose }) {
  const [runs, setRuns] = useState(null)
  const [task, setTask] = useState(state.task_id || '')
  const [overlay, setOverlay] = useState(false)   // U4: convergence-trajectory overlay
  const [traj, setTraj] = useState([])
  useEffect(() => { get('/api/runs').then(setRuns).catch(() => setRuns([])) }, [])
  // All hooks must run on every render — the loading early-return sits AFTER the effects
  // (a conditional hook here unmounts the whole app once /api/runs resolves).
  const tasks = [...new Set((runs || []).map(r => r.task_id).filter(Boolean))].sort()
  const dir = ((runs || []).find(r => r.task_id === task)?.direction) || state.direction
  const rows = (runs || []).filter(r => r.task_id === task)
    .map(r => ({ ...r, m: r.best_confirmed ?? r.best_metric }))
    .filter(r => r.m != null)
    .sort((a, b) => dir === 'min' ? a.m - b.m : b.m - a.m)
  const top = rows[0]?.m
  const worst = rows.length ? rows[rows.length - 1].m : 0
  const span = Math.abs((top ?? 0) - worst) || 1
  const rowKey = rows.map(r => r.run_id).join(',')
  useEffect(() => {   // U4: fetch each run's node metrics and build a running-best trajectory to overlay
    if (!overlay || !rows.length) { setTraj([]); return }
    let cancelled = false
    Promise.all(rows.slice(0, 8).map(r => get(`/api/runs/${r.run_id}/state`).then(p => {
      const ns = Object.values(p.state?.nodes || {}).filter(n => n.metric != null && n.feasible !== false).sort((a, b) => a.id - b.id)
      let best = null; const series = []
      for (const n of ns) { best = best == null ? n.metric : (dir === 'min' ? Math.min(best, n.metric) : Math.max(best, n.metric)); series.push(best) }
      return { run_id: r.run_id, label: r.label || r.run_id, series }
    }).catch(() => ({ run_id: r.run_id, label: r.label || r.run_id, series: [] })))).then(res => { if (!cancelled) setTraj(res) })
    return () => { cancelled = true }
  }, [overlay, task, rowKey])
  if (!runs) return <Panel title="Cross-run sweep" onClose={onClose} wide><div className="muted">Loading runs…</div></Panel>
  return (
    <Panel title="Cross-run sweep" sub={`${runs.length} runs · ${tasks.length} tasks`} onClose={onClose} wide>
      <div className="row" style={{ gap: 8, alignItems: 'center', marginBottom: 8 }}>
        <span className="muted">task:</span>
        <select className="inp sm" value={task} onChange={e => setTask(e.target.value)}>
          {tasks.map(t => <option key={t} value={t}>{t}</option>)}</select>
        <span className="muted">{rows.length} comparable run(s) · {dir}</span>
        <span className="spacer" style={{ flex: 1 }} />
        {rows.length > 0 && <button className={'btn sm' + (overlay ? ' primary' : '')}
          onClick={() => setOverlay(o => !o)} title="overlay each run's running-best trajectory on one axis">overlay trajectories</button>}
      </div>
      {overlay && <div style={{ marginBottom: 12 }}><MultiTrajectory runs={traj} /></div>}
      {rows.length
        ? <table className="tbl"><thead><tr><th>run</th><th>best metric</th><th>nodes</th><th>status</th><th></th></tr></thead><tbody>
            {rows.map((r, i) => <tr key={r.run_id} className={i === 0 ? 'sel' : ''}>
              <td>{r.label || r.run_id}{i === 0 ? <OpIcon name="crown" size={10} /> : ''}</td>
              <td>{fmt(r.m)}{r.best_confirmed != null ? ' (conf)' : ''}</td>
              <td className="muted">{r.nodes}</td><td className="muted">{r.phase || (r.finished ? 'finished' : '—')}</td>
              <td style={{ width: 180 }}><div className="bar" style={{ height: 8 }}>
                <div className="fill" style={{ width: Math.max(4, 100 - Math.abs(r.m - top) / span * 100) + '%' }} /></div></td></tr>)}
          </tbody></table>
        : <div className="muted">No comparable runs for this task yet (need ≥1 finished run with a metric).</div>}
      <div className="muted" style={{ marginTop: 8 }}>Best run per task is starred. Bars are relative to the task’s best (longer = closer to best).</div>
    </Panel>
  )
}

// F4 · Collaboration — a thread view of every node annotation (folded from `annotation` events) plus
// a read-only share link. Annotations are already events; this surfaces them as a reviewable thread.
export function CollabPanel({ state, runId, onSelect, onClose, onToast }) {
  const ann = state.annotations || {}
  const entries = Object.entries(ann).flatMap(([nid, notes]) =>
    (notes || []).map((text, i) => ({ nid: Number(nid), i, text })))
    .sort((a, b) => a.nid - b.nid)
  const share = () => {
    const url = `${location.origin}${apiPrefix()}/#/run/${encodeURIComponent(runId)}`
    if (navigator.clipboard) navigator.clipboard.writeText(url).then(() => onToast && onToast('share link copied'))
    else onToast && onToast(url)
  }
  return (
    <Panel title="Collaboration" sub={`${entries.length} note(s)`} onClose={onClose}>
      <div className="toolbar" style={{ marginBottom: 10 }}>
        <button className="btn sm" onClick={share}><OpIcon name="link" size={12} /> Copy read-only share link</button>
      </div>
      <div className="muted" style={{ marginBottom: 8 }}>Annotations are appended as events (any reviewer with the run can add them via a node’s Note action); they read back here as a thread.</div>
      {entries.length
        ? <div className="thread">{entries.map((e, k) => <div key={k} className="kv" style={{ alignItems: 'baseline' }}>
            <div className="k"><button className="btn sm ghost" onClick={() => { onSelect && onSelect(e.nid); onClose() }}>#{e.nid}</button></div>
            <div className="v">{e.text}</div></div>)}</div>
        : <div className="muted">No annotations yet — add one from a node’s Inspector (Note).</div>}
    </Panel>
  )
}

// P1 hypothesis board: the run's ledger of what it's TRYING TO LEARN, as a kanban. Turns the DAG
// from "mutations tried" into "questions asked → answered". Columns are the derived verdict; each
// card links to its evidence nodes. Audit-only (never affects selection), so it's a pure projection
// of state.hypotheses + a thin human add/abandon via control events.
const _HYP_COLUMNS = [
  ['open', 'Open', 'question posed, not yet tested'],
  ['testing', 'Testing', 'experiments running'],
  ['supported', 'Supported', 'an experiment improved'],
  ['tested', 'Tested', 'evaluated, no improvement'],
  ['abandoned', 'Abandoned', 'dropped'],
]
// Monochrome source glyphs (no emoji): who posed the hypothesis. Reuses the shared icon set.
const _HYP_ICON = { researcher: 'search', deep_research: 'bulb', human: 'user', strategist: 'compass' }

export function HypothesisBoard({ state, runId, onSelect, onClose, onToast }) {
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
    try { await CONTROL.addHypothesis(runId, s); setDraft(''); onToast && onToast('hypothesis added') }
    catch { onToast && onToast('could not add (run not live?)') }
  }
  const _revert = (id) => setOptim(o => { const n = { ...o }; delete n[id]; return n })
  const abandon = async (h) => {
    setOptim(o => ({ ...o, [h.id]: 'abandoned' }))          // reflect immediately (SSE lag)
    try { await CONTROL.abandonHypothesis(runId, h.id); onToast && onToast('hypothesis abandoned') }
    catch { _revert(h.id); onToast && onToast('could not update') }
  }
  const del = async (h) => {
    setOptim(o => ({ ...o, [h.id]: 'deleted' }))            // remove from the board at once
    try { await CONTROL.deleteHypothesis(runId, h.id); onToast && onToast('hypothesis deleted') }
    catch { _revert(h.id); onToast && onToast('could not delete') }
  }
  return (
    <Panel title="Hypotheses" sub={`${hyps.length} tracked — what the run is trying to learn`} onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 10, gap: 6 }}>
        <input className="text" style={{ flex: 1 }} placeholder="Pose a hypothesis to test (e.g. “target is right-skewed; a log transform helps”)"
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

// Module scope so their identity is stable across SSE frames (ComparePanel re-renders on every live
// fold); defined inline they remounted the <select> each frame, closing an open dropdown mid-pick.
function CmpSel({ v, set, ids }) {
  return <select className="text" style={{ width: 90 }} value={v} onChange={e => set(Number(e.target.value))}>{ids.map(i => <option key={i} value={i}>#{i}</option>)}</select>
}
function CmpCol({ d }) {
  return d ? <div style={{ flex: 1 }}>
    <div className="kv">
      <div className="k">operator</div><div className="v">{d.operator}</div>
      <div className="k">metric</div><div className="v">{fmt(d.confirmed_mean ?? d.metric)}</div>
      <div className="k">status</div><div className="v">{d.status}</div>
      <div className="k">params</div><div className="v">{JSON.stringify(d.idea?.params)}</div>
    </div>
    <pre className="code" style={{ maxHeight: 280 }}>{d.code || '(no code)'}</pre>
  </div> : <div className="muted" style={{ flex: 1 }}>…</div>
}

export function ComparePanel({ state, runId, onClose, initialPair = null }) {
  const ids = Object.keys(state.nodes).map(Number).sort((a, b) => a - b)
  const [a, setA] = useState(null), [b, setB] = useState(null)
  const [da, setDa] = useState(null), [db, setDb] = useState(null)
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
  useEffect(() => { if (a != null) get(`/api/runs/${runId}/nodes/${a}`).then(setDa).catch(() => {}) }, [runId, a])
  useEffect(() => { if (b != null) get(`/api/runs/${runId}/nodes/${b}`).then(setDb).catch(() => {}) }, [runId, b])
  if (!ids.length) return <Panel title="Compare nodes" onClose={onClose}><div className="muted">No nodes yet.</div></Panel>
  return (
    <Panel title="Compare nodes" onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 10 }}>
        <CmpSel v={a} set={setA} ids={ids} /><span className="muted">vs</span><CmpSel v={b} set={setB} ids={ids} />
        <span className="spacer" style={{ flex: 1 }} />
        <button className={'btn sm' + (diff ? ' primary' : '')} onClick={() => setDiff(d => !d)}
          title="line diff of the two nodes' code">diff</button>
      </div>
      {diff
        ? (da?.code != null && db?.code != null
            ? <pre className="code" style={{ maxHeight: 460 }}>{diffLines(da.code, db.code).map((d, i) =>
                <span key={i} className={d.cls}>{d.l + '\n'}</span>)}</pre>
            : <div className="muted">Loading code for both nodes…</div>)
        : <div style={{ display: 'flex', gap: 14 }}><CmpCol d={da} /><CmpCol d={db} /></div>}
    </Panel>
  )
}


export function EventExplorer({ runId, onClose }) {
  const [log, setLog] = useState([])
  const [f, setF] = useState('')
  useEffect(() => { get(`/api/runs/${runId}/log`).then(setLog).catch(() => {}) }, [runId])
  const rows = log.filter(e => !f || e.type.includes(f))
  return (
    <Panel title="Event & span explorer" sub={`${log.length} events`} onClose={onClose} wide>
      <input className="text" placeholder="filter by type…" value={f} onChange={e => setF(e.target.value)} style={{ marginBottom: 8 }} />
      <table className="tbl"><thead><tr><th>seq</th><th>type</th><th>data</th></tr></thead><tbody>
        {rows.map(e => <tr key={e.seq}><td>{e.seq}</td><td style={{ color: 'var(--accent)' }}>{e.type}</td><td className="muted" style={{ maxWidth: 600, overflow: 'hidden' }}>{JSON.stringify(e.data).slice(0, 200)}</td></tr>)}
      </tbody></table>
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
            <input className="text art-filter" placeholder="filter files…" value={filter}
                   onChange={e => setFilter(e.target.value)} />
            {roots.length === 0 && <div className="muted">No artifacts found for this run.</div>}
            {roots.map(r => {
              const isOpen = !!open[r.id]
              // Filter only the EXPANDED root — collapsed roots aren't rendered, so don't rescan their
              // (possibly large) file lists on every keystroke.
              const files = isOpen ? (ql ? r.files.filter(f => f.path.toLowerCase().includes(ql)) : r.files) : null
              return (
                <div className="art-root" key={r.id}>
                  <div className="art-root-h" title={r.path}
                       onClick={() => setOpen(o => ({ ...o, [r.id]: !isOpen }))}>
                    <span className="art-chev">{isOpen ? '▾' : '▸'}</span>
                    <b>{r.label}</b>
                    <span className="muted art-root-n">
                      {isOpen && ql ? `${files.length}/${r.n_files}` : `${r.n_files}${r.truncated ? '+' : ''}`}</span>
                    {!r.is_run_dir && <span className="pill art-tag" title={r.path}>repo path</span>}
                  </div>
                  {isOpen && <div className="art-files">
                    {files.length === 0 ? <div className="muted art-empty">{ql ? 'no match' : 'empty'}</div>
                      : files.map(f => (
                        <div key={f.path} title={f.path + (f.is_text ? '' : ' · looks binary')}
                             className={'art-file' + (sel && sel.root === r.id && sel.path === f.path ? ' sel' : '')
                               + (f.is_text ? '' : ' bin')}
                             onClick={() => view(r.id, f)}>
                          <span className="art-name">{f.path}</span>
                          <span className="art-size">{fmtBytes(f.size)}</span>
                        </div>))}
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
                    <pre className="art-pre">{content.content}</pre>
                  </> : <div className="muted art-hint">Could not load this file.</div>}
            </>}
          </div>
        </div>}
    </Panel>
  )
}
