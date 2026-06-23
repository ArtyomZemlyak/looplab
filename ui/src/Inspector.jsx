import React, { useEffect, useState } from 'react'
import { get, fmt, fmtInt, CONTROL } from './util.js'
import { Trajectory } from './charts.jsx'
import { groupAggregate } from './grouping.js'

const TABS = ['Overview', 'Reasoning', 'LLM', 'Code', 'Metrics', 'Trust', 'Agent', 'Cost']

export default function Inspector({ runId, nodeId, state, live, tab, setTab, onToast }) {
  const [detail, setDetail] = useState(null)
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
        {tab === 'LLM' && <LLM n={n} />}
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

// Summary for a COLLAPSED group's super-node (semantic zoom): aggregate + drill back to members.
export function GroupSummary({ groupKey, memberIds, state, onSelectNode, onClose }) {
  const members = (memberIds || []).map(id => state.nodes[id]).filter(Boolean).sort((a, b) => a.id - b.id)
  const dir = state.direction
  const best = groupAggregate(memberIds || [], state.nodes, dir).best   // same aggregate as the super-node card
  const themes = [...new Set(members.map(n => n.idea?.theme).filter(Boolean))]
  return <>
    <div className="tabs">
      <div className="tab active">Group · {groupKey}</div>
      <span style={{ flex: 1 }} />
      <button className="btn sm ghost" onClick={onClose} title="close group view">✕</button>
    </div>
    <div className="insp-body">
      <div className="kv">
        <KV k="experiments" v={members.length} />
        <KV k="best" v={fmt(best)} />
        {themes.length > 0 && <KV k="themes" v={themes.join(', ')} />}
      </div>
      <div className="section-h">Best over members</div>
      <Trajectory nodes={members} direction={dir} height={150} />
      <div className="section-h">Members <span className="pill">{members.length}</span></div>
      <table className="tbl"><thead><tr><th>node</th><th>operator</th><th>metric</th><th>status</th></tr></thead>
        <tbody>{members.map(n => <tr key={n.id} style={{ cursor: 'pointer' }} onClick={() => onSelectNode(n.id)}>
          <td>#{n.id}</td><td>{n.operator}</td><td>{fmt(n.confirmed_mean ?? n.metric)}</td><td>{n.status}</td></tr>)}</tbody></table>
    </div>
  </>
}

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

// Max span duration across the forest — for proportional duration bars (langflow-style trace).
function maxDur(spans) {
  let m = 0
  const walk = (arr) => arr.forEach(s => { m = Math.max(m, s.duration_s || 0); walk(s.children || []) })
  walk(spans)
  return m || 1e-9
}

function SpanRow({ s, depth, max }) {
  const [open, setOpen] = useState(false)
  const attrs = Object.entries(s.attributes || {}).filter(([k]) => k !== 'node_id')
  const events = (s.events || []).filter(e => e.name !== 'llm_call')   // LLM I/O has its own tab
  const llmCount = (s.events || []).filter(e => e.name === 'llm_call').length
  const detail = attrs.length || events.length
  return <>
    <div className={'span-row' + (s.status === 'ERROR' ? ' err' : '')} style={{ paddingLeft: depth * 14 }}
         onClick={() => detail && setOpen(o => !o)} title={detail ? 'click for span detail' : ''}>
      <span className="span-tw">{detail ? (open ? '▾' : '▸') : '·'}</span>
      <span className="span-name">{s.name}</span>
      <span className="span-bar"><span className="span-fill" style={{ width: Math.max(2, (s.duration_s || 0) / max * 100) + '%' }} /></span>
      <span className="t">{fmt(s.duration_s, 3)}s</span>
      {llmCount > 0 && <span className="badge" title="LLM calls — see the LLM tab">{llmCount}×LLM</span>}
      {s.status === 'ERROR' && <span className="badge reason">ERROR</span>}
    </div>
    {open && detail && <div className="span-detail" style={{ marginLeft: depth * 14 + 16 }}>
      {attrs.length > 0 && <div className="kv">{attrs.map(([k, v]) =>
        <KV key={k} k={k} v={typeof v === 'object' ? JSON.stringify(v) : String(v)} />)}</div>}
      {events.map((e, i) => <div key={i} className="span-ev">
        <span className="ty">{e.name}</span>{e.error ? <span className="flag"> {e.error}</span> :
          <span className="muted"> {Object.entries(e).filter(([k]) => k !== 'name').map(([k, v]) => `${k}=${v}`).join(' ')}</span>}
      </div>)}
    </div>}
    {(s.children || []).map((c, i) => <SpanRow key={i} s={c} depth={depth + 1} max={max} />)}
  </>
}

function Reasoning({ n }) {
  const spans = n.trace?.nodes || []
  if (!spans.length) return <div className="muted">No execution trace for this node (toy/offline nodes may have minimal spans).</div>
  const max = maxDur(spans)
  return <div className="spans">{spans.map((s, i) => <SpanRow key={i} s={s} depth={0} max={max} />)}</div>
}

// Flatten every captured LLM call across a node's span forest (set by tracing.record_llm_call).
function collectLlm(spans) {
  const out = []
  const walk = (arr) => arr.forEach(s => {
    (s.events || []).forEach(e => { if (e.name === 'llm_call') out.push({ ...e, span: s.name }) })
    walk(s.children || [])
  })
  walk(spans || [])
  return out
}

function LlmCall({ call, idx }) {
  const [open, setOpen] = useState(idx === 0)   // first call expanded by default
  const t = call.tokens || {}
  return <div className="llm-card">
    <div className="llm-head" onClick={() => setOpen(o => !o)}>
      <span className="span-tw">{open ? '▾' : '▸'}</span>
      <b>{call.op || 'llm'}</b>
      <span className="muted"> {call.span} · {call.model}</span>
      <span className="spacer" style={{ flex: 1 }} />
      <span className="muted">{(t.total || (t.prompt + t.completion)) || 0} tok</span>
    </div>
    {open && <>
      {(call.prompt || []).map((m, i) => <div key={i} className="msg">
        <div className={'msg-role role-' + (m.role || 'user')}>{m.role}</div>
        <pre className="code">{m.content}</pre>
      </div>)}
      <div className="msg">
        <div className="msg-role role-completion">completion</div>
        <pre className="code">{call.completion || '(empty)'}</pre>
      </div>
    </>}
  </div>
}

function LLM({ n }) {
  const calls = collectLlm(n.trace?.nodes)
  if (!calls.length) return <div className="muted">No LLM calls captured for this node — toy/offline run, an external coding agent wrote it, or capture is off (LOOPLAB_TRACE_LLM_IO=0).</div>
  return <div className="llm-list">
    <div className="muted" style={{ marginBottom: 6 }}>{calls.length} LLM call{calls.length > 1 ? 's' : ''} for node #{n.id}</div>
    {calls.map((c, i) => <LlmCall key={i} call={c} idx={i} />)}
  </div>
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
