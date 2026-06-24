import React, { useEffect, useMemo, useState } from 'react'
import { get, putText, post, fmt, fmtInt, CONTROL, gpuStat } from './util.js'
import { Trajectory, Bars, Gantt, ParallelCoords, Scatter, ImprovementWaterfall } from './charts.jsx'
import MapView from './MapView.jsx'
import { analyze, paramDiffLabel, toMarkdown } from './report.js'

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

export function TrustPanel({ state, runId, onClose }) {
  const [cfg, setCfg] = useState(null)
  useEffect(() => { get(`/api/runs/${runId}/config`).then(setCfg).catch(() => {}) }, [runId])
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
        <Stat n={state.spec_confirmed ? 'ratified' : (state.proposed_spec ? 'pending' : 'n/a')} l="eval spec" />
        <Stat n={state.workspace_changed ? '⚠ changed' : 'pinned'} l="workspace repro" />
      </div>

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
      {(state.reward_hacks || []).length
        ? <table className="tbl"><thead><tr><th>node</th><th>signal</th><th>detail</th></tr></thead><tbody>
          {state.reward_hacks.flatMap((h, i) => (h.signals || []).map((s, j) =>
            <tr key={`${i}-${j}`}><td className="flag">#{h.node_id}</td><td>{s.signal}</td>
              <td className="muted">{s.detail}</td></tr>))}</tbody></table>
        : <div className="chip ok">no suspicious wins flagged{cfg && !cfg.reward_hack_detect ? ' (detector off — enable reward_hack_detect)' : ''}</div>}
    </Panel>
  )
}

export function SensitivityPanel({ state, onClose }) {
  // Aggregate ablation impacts across all ablate events (latest wins per param).
  const impacts = {}
  ;(state.ablations || []).forEach(a => Object.entries(a.impacts || {}).forEach(([k, v]) => { impacts[k] = Math.abs(v) }))
  const bars = Object.entries(impacts).map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value)
  return (
    <Panel title="Parameter sensitivity" onClose={onClose} wide>
      <div className="section-h">Ablation impact (|Δmetric| when param zeroed)</div>
      {bars.length ? <Bars data={bars} color="#9a6bff" /> : <div className="muted">No ablation events yet (enable ablate_every or use Force-ablate on a node).</div>}
      <div className="section-h">Parallel coordinates — params → metric</div>
      <ParallelCoords nodes={Object.values(state.nodes)} direction={state.direction} />
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
export function ParetoPanel({ state, onClose }) {
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
    scatter = <Scatter data={data} xlab={cName} ylab="metric" />
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
                  <tr key={n.id}><td>#{n.id}{n.id === state.best_node_id ? ' ★' : ''}</td><td>{fmt(n.confirmed_mean ?? n.metric)}</td>
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

export function ConfigPanel({ runId, state, onClose, onToast }) {
  const [cfg, setCfg] = useState(null)
  const [sec, setSec] = useState('')
  useEffect(() => { get(`/api/runs/${runId}/config`).then(setCfg).catch(() => {}) }, [runId])
  return (
    <Panel title="Run config" onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <span className="muted">extend eval budget:</span>
        <input className="text" style={{ width: 120 }} placeholder="seconds" value={sec} onChange={e => setSec(e.target.value)} />
        <button className="btn sm primary" disabled={!sec} onClick={async () => { await CONTROL.budget(runId, Number(sec)); onToast('budget extended +' + sec + 's') }}>apply</button>
      </div>
      {cfg ? <table className="tbl"><tbody>{Object.entries(cfg).map(([k, v]) => <tr key={k}><td className="muted">{k}</td><td>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</td></tr>)}</tbody></table> : <div className="muted">…</div>}
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

export function MemoryPanel({ onClose }) {
  const [data, setData] = useState({ dir: null, cases: [] })
  useEffect(() => { get('/api/memory').then(setData).catch(() => {}) }, [])
  return (
    <Panel title="Cross-run memory — case library" sub={data.dir || 'no memory dir'} onClose={onClose} wide>
      {data.cases.length
        ? <table className="tbl"><thead><tr><th>task</th><th>goal</th><th>metric</th><th>params</th></tr></thead><tbody>
          {data.cases.map((c, i) => <tr key={i}><td>{c.task_id}</td><td className="muted">{c.goal}</td><td>{fmt(c.metric)}</td><td className="muted">{JSON.stringify(c.params)}</td></tr>)}</tbody></table>
        : <div className="muted">No cases stored (set memory_dir to accumulate cross-run knowledge).</div>}
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
        }}>⬇ W3C-PROV graph (JSON)</button>
      </div>
      <div className="section-h">Promotions</div>
      {(state.promotions || []).length
        ? <table className="tbl"><thead><tr><th>node</th><th>alias</th></tr></thead><tbody>{state.promotions.map((p, i) => <tr key={i}><td>#{p.node_id}</td><td>{p.alias || 'champion'}</td></tr>)}</tbody></table>
        : <div className="muted">none — use ★ Promote on a node</div>}
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

// "Why this node" — the search policy's last decision. MCTS surfaces per-candidate UCB1 scores
// (policy_decision events → state.policy_scores / policy_chosen); greedy/evo expose the chosen
// node only. Makes the otherwise-opaque search strategy auditable.
export function PolicyPanel({ state, onClose, onSelect }) {
  const scores = state.policy_scores || {}
  const chosen = state.policy_chosen
  const rows = Object.entries(scores).map(([id, s]) => ({ id: Number(id), s })).sort((a, b) => b.s - a.s)
  const cfgPolicy = state.policy || null
  return (
    <Panel title="Policy — why this node" sub={cfgPolicy || 'search'} onClose={onClose}>
      {chosen != null
        ? <div className="kv" style={{ marginBottom: 8 }}><div className="k">last expanded</div>
            <div className="v">#{chosen} {onSelect && <button className="btn sm ghost" onClick={() => { onSelect(chosen); onClose() }}>inspect →</button>}</div></div>
        : <div className="muted">No policy decision recorded yet.</div>}
      {rows.length
        ? <><div className="section-h">Candidate scores (UCB1 — higher = more promising to expand)</div>
            <table className="tbl"><thead><tr><th>node</th><th>score</th><th></th></tr></thead><tbody>
              {rows.map(r => <tr key={r.id} className={r.id === chosen ? 'sel' : ''} style={{ cursor: onSelect ? 'pointer' : 'default' }}
                                 onClick={() => onSelect && (onSelect(r.id), onClose())}>
                <td>#{r.id}{r.id === chosen && ' ◀ chosen'}</td><td>{fmt(r.s, 4)}</td>
                <td><div className="bar" style={{ height: 8 }}><div className="fill" style={{ width: Math.min(100, r.s / (rows[0].s || 1) * 100) + '%' }} /></div></td></tr>)}
            </tbody></table></>
        : <div className="muted" style={{ marginTop: 8 }}>This policy ({cfgPolicy || 'greedy'}) doesn't expose per-candidate scores — only MCTS surfaces UCB1 values. The chosen node above is the latest expansion.</div>}
    </Panel>
  )
}

// "Why this strategy" — the A7 Strategist's adaptive meta-control. Shows the active Strategy
// (policy / Developer / fidelity / operator mix + rationale), the timeline of switches
// (strategy_history), the ASHA rung-promotion trail, and an operator override (set_strategy) so a
// human can pin a policy live (HITL parity). Mirrors the policy "why-this-node" panel.
export function StrategistPanel({ state, runId, onClose, onToast }) {
  const active = state.active_strategy
  const history = state.strategy_history || []
  const rungs = state.rungs || []
  const [pol, setPol] = useState('')
  const [fid, setFid] = useState('')
  const POLICIES = ['greedy', 'evolutionary', 'mcts', 'asha', 'bohb']
  const pin = async () => {
    const strat = {}
    if (pol) strat.policy = pol
    if (fid) strat.fidelity = fid
    if (!Object.keys(strat).length) return
    strat.rationale = 'operator-pinned via UI'
    try { await CONTROL.setStrategy(runId, strat); onToast && onToast('strategy pinned') }
    catch (e) { onToast && onToast('failed: ' + e.message) }
  }
  const opLine = (ops) => ops ? Object.entries(ops).map(([k, v]) => `${k}=${v}`).join(', ') : '—'
  return (
    <Panel title="Strategist — why this strategy" sub={active ? (active.source || 'rule') : 'off'} onClose={onClose} wide>
      {active
        ? <div className="kvs" style={{ marginBottom: 10 }}>
            <div className="kv"><div className="k">policy</div><div className="v"><b>{active.policy || state.policy || 'greedy'}</b>
              {active.policy_params && Object.keys(active.policy_params).length ? <span className="muted"> ({opLine(active.policy_params)})</span> : null}</div></div>
            <div className="kv"><div className="k">developer</div><div className="v">{active.developer || 'default'}</div></div>
            <div className="kv"><div className="k">fidelity</div><div className="v">{active.fidelity || 'adaptive'}</div></div>
            <div className="kv"><div className="k">operators</div><div className="v">{opLine(active.operators)}</div></div>
            <div className="kv"><div className="k">why</div><div className="v">{active.rationale || '—'}</div></div>
          </div>
        : <div className="muted">Strategist is <b>off</b> — the static config policy (<b>{state.policy || 'greedy'}</b>) drives the search.
            Every choice below is also a direct config knob; turn the Strategist on (Settings → strategist_backend = rule|llm) to adapt it at runtime.</div>}

      {history.length > 0 && <>
        <div className="section-h">Strategy timeline ({history.length} switch{history.length === 1 ? '' : 'es'})</div>
        <table className="tbl"><thead><tr><th>@node</th><th>policy</th><th>fidelity</th><th>operators</th><th>why</th></tr></thead><tbody>
          {history.map((h, i) => { const s = h.strategy || {}; return (
            <tr key={i}><td>{h.at_node ?? '—'}</td><td>{s.policy || '—'}</td><td>{s.fidelity || 'adaptive'}</td>
              <td className="muted">{opLine(s.operators)}</td><td className="muted" style={{ maxWidth: 320 }}>{s.rationale || ''}</td></tr>) })}
        </tbody></table></>}

      {rungs.length > 0 && <>
        <div className="section-h">ASHA rung promotions (successive-halving)</div>
        <table className="tbl"><thead><tr><th>rung</th><th>survivors promoted</th></tr></thead><tbody>
          {rungs.map((r, i) => <tr key={i}><td>↑ {r.rung}</td><td>{(r.survivors || []).map(s => '#' + s).join(', ') || '—'}</td></tr>)}
        </tbody></table></>}

      {Object.keys(state.proxy_scores || {}).length > 0 && <>
        <div className="section-h">A6 proxy scoring — early-signal candidate ranking
          {state.proxy_skipped?.length ? ` (${state.proxy_skipped.length} doomed candidate(s) skipped)` : ''}</div>
        <table className="tbl"><thead><tr><th>node</th><th>predicted</th><th>outcome</th></tr></thead><tbody>
          {Object.entries(state.proxy_scores).sort((a, b) => Number(a[0]) - Number(b[0])).map(([id, s]) =>
            <tr key={id} className={(state.proxy_skipped || []).includes(Number(id)) ? 'sel' : ''}>
              <td>#{id}</td><td>{fmt(s)}</td>
              <td>{(state.proxy_skipped || []).includes(Number(id)) ? '⏭ skipped full eval' : 'evaluated'}</td></tr>)}
        </tbody></table></>}

      {!state.finished && <>
        <div className="section-h">Override (pin a strategy — human wins over the Strategist)</div>
        <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <select className="inp sm" value={pol} onChange={e => setPol(e.target.value)}>
            <option value="">policy…</option>{POLICIES.map(p => <option key={p} value={p}>{p}</option>)}</select>
          <select className="inp sm" value={fid} onChange={e => setFid(e.target.value)}>
            <option value="">fidelity…</option>{['smoke', 'full', 'adaptive'].map(f => <option key={f} value={f}>{f}</option>)}</select>
          <button className="btn sm primary" onClick={pin} disabled={!pol && !fid}>📌 Pin strategy</button>
        </div></>}
    </Panel>
  )
}

// F1 · Global hyperparameter importance — across ALL evaluated feasible nodes in the run, how
// strongly does each numeric param predict the metric (|Pearson r|)? The per-node Sensitivity panel
// is local (one ablation); this is the run-wide W&B-style "which knobs matter" view. Pure UI.
function _pearson(xs, ys) {
  const n = xs.length
  const mx = xs.reduce((a, b) => a + b, 0) / n, my = ys.reduce((a, b) => a + b, 0) / n
  let sxy = 0, sxx = 0, syy = 0
  for (let i = 0; i < n; i++) { const dx = xs[i] - mx, dy = ys[i] - my; sxy += dx * dy; sxx += dx * dx; syy += dy * dy }
  const d = Math.sqrt(sxx * syy)
  return d === 0 ? 0 : sxy / d
}
export function HyperImportancePanel({ state, onClose }) {
  const nodes = Object.values(state.nodes).filter(n => n.status === 'evaluated' && n.metric != null && n.feasible !== false)
  const keys = new Set()
  nodes.forEach(n => Object.entries(n.idea?.params || {}).forEach(([k, v]) => { if (typeof v === 'number') keys.add(k) }))
  const rows = []
  keys.forEach(k => {
    const pts = nodes.filter(n => typeof n.idea?.params?.[k] === 'number')
    if (pts.length < 3) return
    const r = _pearson(pts.map(n => n.idea.params[k]), pts.map(n => n.metric))
    rows.push({ k, imp: Math.abs(r), r, n: pts.length })
  })
  rows.sort((a, b) => b.imp - a.imp)
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
  useEffect(() => { get('/api/runs').then(setRuns).catch(() => setRuns([])) }, [])
  if (!runs) return <Panel title="Cross-run sweep" onClose={onClose} wide><div className="muted">Loading runs…</div></Panel>
  const tasks = [...new Set(runs.map(r => r.task_id).filter(Boolean))].sort()
  const dir = (runs.find(r => r.task_id === task)?.direction) || state.direction
  const rows = runs.filter(r => r.task_id === task)
    .map(r => ({ ...r, m: r.best_confirmed ?? r.best_metric }))
    .filter(r => r.m != null)
    .sort((a, b) => dir === 'min' ? a.m - b.m : b.m - a.m)
  const top = rows[0]?.m
  const worst = rows.length ? rows[rows.length - 1].m : 0
  const span = Math.abs((top ?? 0) - worst) || 1
  return (
    <Panel title="Cross-run sweep" sub={`${runs.length} runs · ${tasks.length} tasks`} onClose={onClose} wide>
      <div className="row" style={{ gap: 8, alignItems: 'center', marginBottom: 8 }}>
        <span className="muted">task:</span>
        <select className="inp sm" value={task} onChange={e => setTask(e.target.value)}>
          {tasks.map(t => <option key={t} value={t}>{t}</option>)}</select>
        <span className="muted">{rows.length} comparable run(s) · {dir}</span>
      </div>
      {rows.length
        ? <table className="tbl"><thead><tr><th>run</th><th>best metric</th><th>nodes</th><th>status</th><th></th></tr></thead><tbody>
            {rows.map((r, i) => <tr key={r.run_id} className={i === 0 ? 'sel' : ''}>
              <td>{r.label || r.run_id}{i === 0 ? ' ★' : ''}</td>
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

export function ReportPanel({ state, runId, onClose }) {
  const best = state.best_node_id != null ? state.nodes[state.best_node_id] : null
  const failed = Object.values(state.nodes).filter(n => n.status === 'failed')
  const [bestCode, setBestCode] = useState(null)
  useEffect(() => { if (best) get(`/api/runs/${runId}/nodes/${best.id}`).then(d => setBestCode(d)).catch(() => {}) }, [runId, best?.id])
  const a = useMemo(() => analyze(state), [state])
  const dl = (name, text, type) => {
    const blob = new Blob([text], { type }); const u = URL.createObjectURL(blob)
    const el = document.createElement('a'); el.href = u; el.download = name; el.click(); URL.revokeObjectURL(u)
  }
  const impr = (s) => (s.delta == null ? true : (state.direction === 'min' ? s.delta < 0 : s.delta > 0))
  // F3 · lineage / model-card export: a portable champion artifact tying data-hash -> code ->
  // params -> metric -> selection, plus provenance, for handoff/reproducibility.
  const modelCard = () => {
    const champ = state.champion != null ? state.nodes[state.champion] : best
    const card = {
      task: state.task_id, goal: state.goal, direction: state.direction,
      generated: new Date().toISOString(), run_id: state.run_id,
      champion: champ ? {
        node_id: champ.id, operator: champ.operator,
        metric: champ.confirmed_mean ?? champ.metric,
        confirmed: champ.confirmed_mean != null,
        params: champ.idea.params, rationale: champ.idea.rationale,
        lineage: (champ.parent_ids || []),
      } : null,
      data_provenance: state.data_provenance || null,
      workspace: state.workspace || null,
      counts: { nodes: Object.keys(state.nodes).length, evaluated: Object.values(state.nodes).filter(n => n.status === 'evaluated').length },
    }
    return JSON.stringify(card, null, 2)
  }
  return (
    <Panel title="Run report" onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 10 }}>
        <button className="btn sm primary" onClick={() => window.print()}>🖨 Print / PDF</button>
        <button className="btn sm" onClick={() => dl(`${state.run_id}_report.md`, toMarkdown(state, best), 'text/markdown')}>⬇ Markdown report</button>
        {best && <button className="btn sm" disabled={!bestCode?.code} onClick={() => dl(`solution_node${best.id}.py`, bestCode.code, 'text/x-python')}>⬇ Solution</button>}
        <button className="btn sm" title="F3: portable champion lineage + provenance" onClick={() => dl(`${state.run_id}_model_card.json`, modelCard(), 'application/json')}>⬇ Model card</button>
      </div>
      <h2>{state.goal || state.task_id}</h2>
      <div className="kv">
        <div className="k">run</div><div className="v">{state.run_id}</div>
        <div className="k">direction</div><div className="v">{state.direction}</div>
        <div className="k">status</div><div className="v">{state.phase}{state.stop_reason ? ` (${state.stop_reason})` : ''}</div>
        <div className="k">nodes</div><div className="v">{Object.keys(state.nodes).length} ({a.nEval} evaluated, {failed.length} failed)</div>
        {best && <><div className="k">best</div><div className="v">#{best.id} · {fmt(best.confirmed_mean ?? best.metric)} · {JSON.stringify(best.idea?.params)}</div></>}
        {a.steps.length > 1 && <><div className="k">total gain</div><div className="v">{fmt(a.totalGain)} over {a.steps.length} steps (baseline {fmt(a.firstBest)} → {fmt(a.finalBest)})</div></>}
        {state.llm_cost && <><div className="k">LLM</div><div className="v">{fmtInt(state.llm_cost.total_tokens)} tokens · ${fmt(state.llm_cost.cost)}</div></>}
      </div>

      <div className="section-h">Best-metric trajectory <span className="muted">(○ = a step that moved the frontier)</span></div>
      <Trajectory nodes={Object.values(state.nodes)} direction={state.direction} steps={a.steps} />

      <div className="section-h">Key improvements — how the metric got better</div>
      {a.steps.length
        ? <><ImprovementWaterfall steps={a.steps} direction={state.direction} />
            <table className="tbl"><thead><tr><th>#</th><th>node</th><th>operator</th><th>metric</th><th>Δ</th><th>what changed</th><th>why</th></tr></thead><tbody>
              {a.steps.map((s, i) => <tr key={s.id}>
                <td>{i + 1}</td><td>#{s.id}</td><td>{s.operator}{s.theme ? <span className="pill" style={{ marginLeft: 4 }}>{s.theme}</span> : null}</td>
                <td>{fmt(s.to)}</td>
                <td style={{ color: s.delta == null ? 'var(--accent)' : (impr(s) ? 'var(--ok)' : 'var(--fail)') }}>{s.delta == null ? 'baseline' : fmt(s.delta)}</td>
                <td className="muted">{paramDiffLabel(s.diff)}</td>
                <td className="muted" style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={s.rationale}>{s.rationale}</td>
              </tr>)}
            </tbody></table></>
        : <div className="muted">No improving steps recorded yet.</div>}

      <div className="section-h">What worked — operator &amp; theme effectiveness</div>
      <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 280 }}>
          <div className="muted" style={{ marginBottom: 4 }}>operators</div>
          <table className="tbl"><thead><tr><th>operator</th><th>nodes</th><th>eval</th><th>improved</th><th>best</th></tr></thead><tbody>
            {a.operators.map(o => <tr key={o.key}><td>{o.key}</td><td>{o.count}</td><td>{o.evaluated}</td>
              <td style={{ color: o.improved ? 'var(--ok)' : 'inherit' }}>{o.improved}</td><td>{fmt(o.best)}</td></tr>)}
          </tbody></table>
        </div>
        {a.themes.length > 0 && <div style={{ flex: 1, minWidth: 280 }}>
          <div className="muted" style={{ marginBottom: 4 }}>themes</div>
          <table className="tbl"><thead><tr><th>theme</th><th>nodes</th><th>improved</th><th>best</th></tr></thead><tbody>
            {a.themes.map(t => <tr key={t.key}><td>{t.key}</td><td>{t.count}</td>
              <td style={{ color: t.improved ? 'var(--ok)' : 'inherit' }}>{t.improved}</td><td>{fmt(t.best)}</td></tr>)}
          </tbody></table>
        </div>}
      </div>

      <div className="section-h">What didn't work</div>
      <div className="cardgrid" style={{ marginBottom: 10 }}>
        {Object.entries(a.failures).map(([r, ns]) => <Stat key={r} n={ns.length} l={`failed · ${r}`} />)}
        {a.regressions.length > 0 && <Stat n={a.regressions.length} l="regressions" />}
        {a.infeasible.length > 0 && <Stat n={a.infeasible.length} l="infeasible" />}
        {(state.drifts || []).length > 0 && <Stat n={state.drifts.length} l="metric drift" />}
        {!Object.keys(a.failures).length && !a.regressions.length && !a.infeasible.length && <Stat n={0} l="nothing notably failed" />}
      </div>
      {a.regressions.length > 0 && <>
        <div className="muted" style={{ marginBottom: 4 }}>tried but didn't beat the parent</div>
        <table className="tbl"><thead><tr><th>node</th><th>operator</th><th>metric</th><th>vs parent</th><th>change</th></tr></thead><tbody>
          {a.regressions.slice(0, 12).map(r => <tr key={r.id}><td>#{r.id}</td><td>{r.operator}</td><td>{fmt(r.metric)}</td>
            <td className="muted">#{r.parentId} {fmt(r.parentMetric)}</td><td className="muted">{paramDiffLabel(r.diff)}</td></tr>)}
        </tbody></table>
      </>}

      {best && <><div className="section-h">Winning solution</div><pre className="code">{bestCode?.code || '(loading…)'}</pre></>}
    </Panel>
  )
}

export function ComparePanel({ state, runId, onClose }) {
  const ids = Object.keys(state.nodes).map(Number).sort((a, b) => a - b)
  const [a, setA] = useState(null), [b, setB] = useState(null)
  const [da, setDa] = useState(null), [db, setDb] = useState(null)
  // Seed/repair the selectors once nodes exist (the panel may open before any node arrives).
  useEffect(() => {
    if (!ids.length) return
    if (a == null || !ids.includes(a)) setA(state.best_node_id ?? ids[0])
    if (b == null || !ids.includes(b)) setB(ids[ids.length - 1])
  }, [ids.join(','), state.best_node_id])
  useEffect(() => { if (a != null) get(`/api/runs/${runId}/nodes/${a}`).then(setDa).catch(() => {}) }, [runId, a])
  useEffect(() => { if (b != null) get(`/api/runs/${runId}/nodes/${b}`).then(setDb).catch(() => {}) }, [runId, b])
  if (!ids.length) return <Panel title="Compare nodes" onClose={onClose}><div className="muted">No nodes yet.</div></Panel>
  const Sel = ({ v, set }) => <select className="text" style={{ width: 90 }} value={v} onChange={e => set(Number(e.target.value))}>{ids.map(i => <option key={i} value={i}>#{i}</option>)}</select>
  const Col = ({ d }) => d ? <div style={{ flex: 1 }}>
    <div className="kv">
      <div className="k">operator</div><div className="v">{d.operator}</div>
      <div className="k">metric</div><div className="v">{fmt(d.confirmed_mean ?? d.metric)}</div>
      <div className="k">status</div><div className="v">{d.status}</div>
      <div className="k">params</div><div className="v">{JSON.stringify(d.idea?.params)}</div>
    </div>
    <pre className="code" style={{ maxHeight: 280 }}>{d.code || '(no code)'}</pre>
  </div> : <div className="muted" style={{ flex: 1 }}>…</div>
  return (
    <Panel title="Compare nodes" onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 10 }}><Sel v={a} set={setA} /><span className="muted">vs</span><Sel v={b} set={setB} /></div>
      <div style={{ display: 'flex', gap: 14 }}><Col d={da} /><Col d={db} /></div>
    </Panel>
  )
}

export function MetaGraphPanel({ onClose }) {
  // The cross-run map: projects › runs › themes as one collapsible semantic-zoom continuum
  // (same hull / super-node language as the in-run canvas). Clicking a run drills into it.
  return (
    <Panel title="Multi-tree meta-graph" sub="projects › runs › themes" onClose={onClose} wide>
      <div style={{ height: 540 }}>
        <MapView onOpen={(id) => { onClose(); location.hash = '#/run/' + encodeURIComponent(id) }} />
      </div>
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
