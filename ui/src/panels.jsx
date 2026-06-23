import React, { useEffect, useMemo, useState } from 'react'
import { get, putText, post, fmt, fmtInt, CONTROL } from './util.js'
import { Trajectory, Bars, Gantt, ParallelCoords, Scatter } from './charts.jsx'
import MapView from './MapView.jsx'

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

export function ParetoPanel({ state, onClose }) {
  const nodes = Object.values(state.nodes).filter(n => n.metric != null)
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
        {runs.sort((a, b) => (b.best_confirmed ?? b.best_metric ?? -Infinity) - (a.best_confirmed ?? a.best_metric ?? -Infinity))
          .map(r => <tr key={r.run_id}><td>{r.run_id}</td><td className="muted">{r.task_id}</td><td>{r.phase}</td><td>{fmt(r.best_confirmed ?? r.best_metric)}</td><td>{r.nodes}</td></tr>)}
      </tbody></table>
    </Panel>
  )
}

export function ReportPanel({ state, runId, onClose }) {
  const best = state.best_node_id != null ? state.nodes[state.best_node_id] : null
  const failed = Object.values(state.nodes).filter(n => n.status === 'failed')
  const [bestCode, setBestCode] = useState(null)
  useEffect(() => { if (best) get(`/api/runs/${runId}/nodes/${best.id}`).then(d => setBestCode(d)).catch(() => {}) }, [runId, best?.id])
  const download = () => {
    if (!bestCode?.code) return
    const blob = new Blob([bestCode.code], { type: 'text/x-python' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob)
    a.download = `solution_node${best.id}.py`; a.click(); URL.revokeObjectURL(a.href)
  }
  return (
    <Panel title="Run report" onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 10 }}>
        <button className="btn sm primary" onClick={() => window.print()}>🖨 Print / PDF</button>
        {best && <button className="btn sm" disabled={!bestCode?.code} onClick={download}>⬇ Download solution</button>}
      </div>
      <h2>{state.goal || state.task_id}</h2>
      <div className="kv">
        <div className="k">run</div><div className="v">{state.run_id}</div>
        <div className="k">direction</div><div className="v">{state.direction}</div>
        <div className="k">status</div><div className="v">{state.phase}{state.stop_reason ? ` (${state.stop_reason})` : ''}</div>
        <div className="k">nodes</div><div className="v">{Object.keys(state.nodes).length} ({Object.values(state.nodes).filter(n => n.status === 'evaluated').length} evaluated, {failed.length} failed)</div>
        {best && <><div className="k">best</div><div className="v">#{best.id} · {fmt(best.confirmed_mean ?? best.metric)} · {JSON.stringify(best.idea?.params)}</div></>}
        {state.llm_cost && <><div className="k">LLM</div><div className="v">{fmtInt(state.llm_cost.total_tokens)} tokens · ${fmt(state.llm_cost.cost)}</div></>}
      </div>
      <div className="section-h">Best-metric trajectory</div>
      <Trajectory nodes={Object.values(state.nodes)} direction={state.direction} />
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
