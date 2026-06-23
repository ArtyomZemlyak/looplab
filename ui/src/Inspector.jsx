import React, { useEffect, useState } from 'react'
import { get, fmt, fmtInt, CONTROL } from './util.js'

const TABS = ['Overview', 'Reasoning', 'Code', 'Metrics', 'Trust', 'Agent', 'Cost']

export default function Inspector({ runId, nodeId, state, live, onToast }) {
  const [detail, setDetail] = useState(null)
  const [tab, setTab] = useState('Overview')
  useEffect(() => {
    if (nodeId == null) { setDetail(null); return }
    let on = true
    get(`/api/runs/${runId}/nodes/${nodeId}`).then(d => on && setDetail(d)).catch(() => {})
    return () => { on = false }
  }, [runId, nodeId, state?.nodes?.[nodeId]?.status])

  if (nodeId == null) return <div className="insp-empty">Select a node to inspect its idea, code, metrics, trust, and agent trace.</div>
  const n = detail || (state.nodes[nodeId])
  if (!n) return <div className="insp-empty">…</div>
  const act = async (fn, msg) => { try { await fn(); onToast(msg) } catch (e) { onToast('failed: ' + e.message) } }

  return (
    <>
      <div className="tabs">
        {TABS.map(t => <div key={t} className={'tab' + (t === tab ? ' active' : '') + (t === 'Trust' && (n.violations?.length || (detail?.drifts || []).length) ? ' alarm' : '')}
                            onClick={() => setTab(t)}>{t}</div>)}
      </div>
      <div className="insp-body">
        <div className="toolbar" style={{ marginBottom: 10 }}>
          {!live.finished && n.status === 'pending' &&
            <button className="btn sm danger" onClick={() => act(() => CONTROL.nodeAbort(runId, n.id), `aborted node ${n.id}`)}>⦸ Stop</button>}
          <button className="btn sm" disabled={live.finished || n.status !== 'evaluated'}
                  title={n.status !== 'evaluated' ? 'only evaluated nodes can be confirmed' : 'multi-seed confirm'}
                  onClick={() => act(() => CONTROL.forceConfirm(runId, n.id), `confirm requested for ${n.id}`)}>↻ Confirm</button>
          <button className="btn sm" disabled={live.finished} onClick={() => act(() => CONTROL.forceAblate(runId, n.id), `ablate requested for ${n.id}`)}>⊟ Ablate</button>
          <button className="btn sm" onClick={() => act(() => CONTROL.fork(runId, n.id), `forked from ${n.id}`)}>⑂ Fork</button>
          <button className="btn sm warn" onClick={() => act(() => CONTROL.promote(runId, n.id), `promoted ${n.id} → champion`)}>★ Promote</button>
          <button className="btn sm" onClick={() => { const t = prompt('Note for node ' + n.id); if (t) act(() => CONTROL.annotate(runId, n.id, t), 'note saved') }}>✎ Note</button>
        </div>

        {tab === 'Overview' && <Overview n={n} state={state} />}
        {tab === 'Reasoning' && <Reasoning n={n} />}
        {tab === 'Code' && <Code n={n} />}
        {tab === 'Metrics' && <Metrics n={n} detail={detail} />}
        {tab === 'Trust' && <Trust n={n} detail={detail} />}
        {tab === 'Agent' && <Agent n={n} />}
        {tab === 'Cost' && <Cost state={state} />}
      </div>
    </>
  )
}

function KV({ k, v }) { return <><div className="k">{k}</div><div className="v">{v}</div></> }

function Overview({ n, state }) {
  const p = n.idea?.params || {}
  return <>
    <div className="kv">
      <KV k="node" v={`#${n.id}`} />
      <KV k="operator" v={n.operator} />
      <KV k="parents" v={(n.parent_ids || []).join(', ') || '—'} />
      <KV k="status" v={n.status + (n.id === state.best_node_id ? '  ♚ champion' : '')} />
      <KV k="metric" v={fmt(n.metric)} />
      {n.confirmed_mean != null && <KV k="robust mean" v={`${fmt(n.confirmed_mean)} ± ${fmt(n.confirmed_std)} (${n.confirmed_seeds}×)`} />}
      <KV k="feasible" v={String(n.feasible)} />
      <KV k="eval seconds" v={fmt(n.eval_seconds)} />
    </div>
    <div className="section-h">Idea params</div>
    {Object.keys(p).length ? <div className="kv">{Object.entries(p).map(([k, v]) => <KV key={k} k={k} v={fmt(v)} />)}</div> : <div className="muted">none</div>}
    {n.idea?.rationale && <><div className="section-h">Rationale</div><div className="v">{n.idea.rationale}</div></>}
    {n.annotations?.length > 0 && <><div className="section-h">Notes</div>{n.annotations.map((a, i) => <div key={i} className="chip" style={{ margin: 2 }}>{a}</div>)}</>}
    {n.deleted?.length > 0 && <><div className="section-h">Deleted files</div><div className="v">{n.deleted.join(', ')}</div></>}
  </>
}

function Reasoning({ n }) {
  const spans = n.trace?.nodes || []
  if (!spans.length) return <div className="muted">No execution trace for this node (toy/offline nodes may have minimal spans).</div>
  const Row = ({ s, depth }) => <>
    <div className="feed ev" style={{ paddingLeft: depth * 14 }}>
      <span className="ty" style={{ color: s.status === 'ERROR' ? 'var(--fail)' : undefined }}>{s.name}</span>
      <span className="t">{fmt(s.duration_s, 3)}s</span>
      {s.status === 'ERROR' && <span className="badge reason">ERROR</span>}
    </div>
    {(s.children || []).map((c, i) => <Row key={i} s={c} depth={depth + 1} />)}
  </>
  return <div className="feed">{spans.map((s, i) => <Row key={i} s={s} depth={0} />)}</div>
}

function diffLines(a, b) {
  const A = (a || '').split('\n'), B = (b || '').split('\n')
  const setA = new Set(A), setB = new Set(B)
  return B.map(l => ({ l, cls: setA.has(l) ? '' : 'diff-add' }))
    .concat(A.filter(l => !setB.has(l)).map(l => ({ l, cls: 'diff-del' })))
}

function Code({ n }) {
  const [diff, setDiff] = useState(false)
  const files = n.files || {}
  return <>
    <div className="toolbar" style={{ marginBottom: 8 }}>
      {n.parent_code != null && <button className={'btn sm' + (diff ? ' primary' : '')} onClick={() => setDiff(d => !d)}>diff vs parent #{n.parent_id_diffed}</button>}
    </div>
    {diff && n.parent_code != null
      ? <pre className="code">{diffLines(n.parent_code, n.code).map((d, i) => <span key={i} className={d.cls}>{d.l + '\n'}</span>)}</pre>
      : <pre className="code">{n.code || '(no solution.py — repo task or no code)'}</pre>}
    {Object.keys(files).length > 0 && <>
      <div className="section-h">Helper files <span className="pill">{Object.keys(files).length}</span></div>
      {Object.entries(files).map(([fn, c]) => <div key={fn}><div className="muted" style={{ marginTop: 6 }}>{fn}</div><pre className="code">{c}</pre></div>)}
    </>}
  </>
}

function Metrics({ n, detail }) {
  const seeds = detail?.confirm_seeds_detail || {}
  const vals = Object.entries(seeds).map(([s, v]) => ({ s: Number(s), v })).filter(x => x.v != null).sort((a, b) => a.s - b.s)
  return <>
    <div className="kv">
      <KV k="single metric" v={fmt(n.metric)} />
      {n.confirmed_mean != null && <KV k="robust mean ± std" v={`${fmt(n.confirmed_mean)} ± ${fmt(n.confirmed_std)}`} />}
      {Object.entries(n.extra_metrics || {}).map(([k, v]) => <KV key={k} k={'aux: ' + k} v={fmt(v)} />)}
    </div>
    {vals.length > 0 && <>
      <div className="section-h">Per-seed confirmation</div>
      <table className="tbl"><thead><tr><th>seed</th><th>metric</th></tr></thead>
        <tbody>{vals.map(x => <tr key={x.s}><td>{x.s}</td><td>{fmt(x.v)}</td></tr>)}</tbody></table>
    </>}
    {!vals.length && <div className="muted" style={{ marginTop: 10 }}>No per-step series for this task kind; run multi-seed confirmation to populate robustness.</div>}
  </>
}

function Trust({ n, detail }) {
  const drifts = (detail?.drifts || [])
  return <>
    <div className="section-h">Robustness</div>
    {n.confirmed_mean != null
      ? <div className="kv">
        <KV k="single" v={fmt(n.metric)} />
        <KV k="robust mean" v={fmt(n.confirmed_mean)} />
        <KV k="std" v={fmt(n.confirmed_std)} />
        <KV k="seeds" v={n.confirmed_seeds} />
      </div>
      : <div className="muted">Not multi-seed confirmed — single-eval metric only (could be seed-lucky).</div>}
    <div className="section-h">Feasibility</div>
    {n.violations?.length
      ? <table className="tbl"><thead><tr><th>constraint</th><th>value</th><th>bound</th></tr></thead>
        <tbody>{n.violations.map((v, i) => <tr key={i}><td className="flag">{v.name}</td><td>{fmt(v.value)}</td><td>{v.max != null ? `≤ ${fmt(v.max)}` : `≥ ${fmt(v.min)}`}</td></tr>)}</tbody></table>
      : <div className="chip ok">no constraint violations</div>}
    {n.status === 'failed' && <><div className="section-h">Failure</div><span className="badge reason">{n.error_reason}</span><pre className="code">{n.error}</pre></>}
  </>
}

function Agent({ n }) {
  const r = n.agent_report
  if (!r) return <div className="muted">Not produced by an external coding agent (templated/LLM developer).</div>
  return <>
    <div className="kv">
      <KV k="ok" v={String(r.ok)} />
      <KV k="fell back" v={String(r.fell_back)} />
      <KV k="attempts" v={r.attempts} />
      <KV k="shipped ok" v={String(r.shipped_ok)} />
    </div>
    <div className="section-h">Validation checks</div>
    <table className="tbl"><thead><tr><th>check</th><th>ok</th><th>detail</th></tr></thead>
      <tbody>{(r.checks || []).map((c, i) => <tr key={i}>
        <td>{c.name}</td><td style={{ color: c.ok ? 'var(--ok)' : 'var(--fail)' }}>{c.ok ? '✓' : '✗'}</td>
        <td className="muted">{c.detail || c.severity || ''}</td></tr>)}</tbody></table>
  </>
}

function Cost({ state }) {
  const c = state.llm_cost
  if (!c) return <div className="muted">No LLM cost recorded (offline/toy run, or run not finished).</div>
  return <div className="kv">
    <KV k="$ spent" v={fmt(c.cost)} />
    <KV k="calls" v={fmtInt(c.calls)} />
    <KV k="prompt tokens" v={fmtInt(c.prompt_tokens)} />
    <KV k="completion tokens" v={fmtInt(c.completion_tokens)} />
    <KV k="total tokens" v={fmtInt(c.total_tokens)} />
  </div>
}
