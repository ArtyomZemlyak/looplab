import React, { useEffect, useState, useRef } from 'react'
import { get, fmt, fmtInt, isSweep, spanDetail, nodeConversation, CONTROL } from './util.js'
import { Trajectory, ParallelCoords, Scatter, MetricLines } from './charts.jsx'
import { groupAggregate } from './grouping.js'
import { mergeSummary, nodeChip } from './report.js'
import { OpIcon } from './icons.jsx'
import Markdown from './markdown.jsx'

// One lifecycle "Trace" tab replaces the old Reasoning / LLM / Agent split: a node is worked on by
// several parts in sequence (Researcher proposes, Developer implements/repairs, then it's evaluated
// and confirmed), so we show that whole story in one place — each stage with its sub-steps, inline
// LLM I/O, and the coding-agent's validation — instead of three disconnected panes. The Inspector is
// READ-ONLY (Workstream C): every node action — confirm/ablate/fork/promote/note — is done from the
// chat (add the node via its ＋#id chip, or use a /command), so there's no per-node button toolbar.
// Tab order (user preference): Overview → Trace → Training → Code → Metrics → Trust → Cost.
const TABS = ['Overview', 'Trace', 'Training', 'Code', 'Metrics', 'Trust', 'Cost']

// The ONE per-node write action (Workstream-C exception): re-run THIS node in place — no new node —
// from a chosen stage. It's a recovery/fix control (natural to trigger from the failed node itself),
// unlike the exploratory confirm/ablate/fork which stay in the chat. Appends a node_reset control
// event; the engine applies it on the next resume.
function ResetBtn({ runId, id, onToast }) {
  const [open, setOpen] = useState(false)
  const STAGES = [
    ['eval', 're-score', 'keep the idea + code, just re-run the evaluation (an infra / API-key blip)'],
    ['implement', 're-run the Developer', "keep the Researcher's idea, re-write the code (its code crashed)"],
    ['propose', 'full redo', 're-propose the idea, re-develop, then re-evaluate'],
  ]
  const doReset = (stage) => {
    setOpen(false)
    CONTROL.resetNode(runId, id, stage)
      .then(() => onToast && onToast(`Reset #${id} (${stage}) queued — resume the run to apply`))
      .catch(() => onToast && onToast(`Reset #${id} failed`))
  }
  return <span style={{ position: 'relative', marginLeft: 8 }}>
    <button className="ctx-chip" style={{ padding: '0 6px', cursor: 'pointer' }}
            title="re-run THIS node in place (no new node) from a chosen stage"
            onClick={() => setOpen(o => !o)}>↻ Reset ▾</button>
    {open && <div style={{ position: 'absolute', zIndex: 30, top: '110%', left: 0, background: 'var(--panel,#1b1f2a)', border: '1px solid var(--border,#333)', borderRadius: 6, padding: 4, minWidth: 240, boxShadow: '0 4px 16px rgba(0,0,0,.4)' }}>
      {STAGES.map(([stage, label, desc]) =>
        <div key={stage} style={{ padding: '6px 8px', cursor: 'pointer', borderRadius: 4 }}
             title={desc} onMouseDown={() => doReset(stage)}
             onMouseEnter={e => e.currentTarget.style.background = 'var(--hover,#2a3140)'}
             onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
          <div><b style={{ fontSize: 12 }}>{label}</b> <span className="muted" style={{ fontSize: 10 }}>from {stage}</span></div>
          <div className="muted" style={{ fontSize: 10 }}>{desc}</div>
        </div>)}
    </div>}
  </span>
}

export default function Inspector({ runId, nodeId, state, live, tab, setTab, onToast }) {
  const [detail, setDetail] = useState(null)
  useEffect(() => {
    setDetail(null)               // clear stale detail immediately so we never render node A's
    if (nodeId == null) return    // payload under node B while B's fetch is in flight (or failed)
    let on = true
    get(`/api/runs/${runId}/nodes/${nodeId}`).then(d => on && setDetail(d)).catch(() => {})
    return () => { on = false }
  }, [runId, nodeId, state?.nodes?.[nodeId]?.status])
  // Live-refresh the node detail (it carries n.trace spans + the agent report) while the run is ACTIVELY
  // working this node — so the Trace tab fills in WITHOUT the user toggling tabs. Only while an LLM is
  // plausibly working it (building / still pending), engine alive; stops at terminal or engine death.
  // "Working" = an LLM is ACTUALLY authoring this node RIGHT NOW (building = propose+implement, or a
  // repair). NOT during training: a `pending` node is being EVALUATED (the sandbox trains it, no LLM),
  // so no live pulse/poll there — that's the "тренировка уже не надо" case.
  const nodeWorking = !!live && live.engine_running !== false && !live.finished
    && nodeId != null && live.building?.node_id === nodeId
  useEffect(() => {
    if (!nodeWorking) return
    const iv = setInterval(() => {
      get(`/api/runs/${runId}/nodes/${nodeId}`).then(d => { if (d) setDetail(d) }).catch(() => {})
    }, 4000)
    return () => clearInterval(iv)
  }, [runId, nodeId, nodeWorking])

  if (nodeId == null) return <div className="insp-empty">Select a node to inspect its idea, code, metrics, trust, and agent trace.</div>
  const n = detail || (state.nodes[nodeId])
  if (!n) return <div className="insp-empty">…</div>
  // Metric-drift is run-level state (state.drifts), each entry tagged with its node_id — the
  // per-node detail payload has no `drifts` key, so filter the run state down to this node.
  const nodeDrifts = (state?.drifts || []).filter(d => d.node_id === n.id)
  // Sweep nodes get a Trials tab (right after Overview). `activeTab` guards against a stale tab
  // (e.g. 'Trials' left selected after switching to a non-sweep node) falling through to nothing.
  const sweep = isSweep(n)
  const tabs = sweep ? ['Overview', 'Trials', ...TABS.slice(1)] : TABS
  const activeTab = tabs.includes(tab) ? tab : 'Overview'

  return (
    <>
      <div className="tabs">
        {tabs.map(t => <div key={t} className={'tab' + (t === activeTab ? ' active' : '') + (t === 'Trust' && (n.violations?.length || nodeDrifts.length) ? ' alarm' : '')}
                            onClick={() => setTab(t)}>{t}</div>)}
      </div>
      <div className="insp-body">
        <div className="insp-hint muted">Actions (confirm · ablate · fork · promote · note) live in the chat — <button className="ctx-chip" style={{ padding: '0 6px', cursor: 'pointer' }} title="attach this experiment to the assistant context" onClick={() => window.dispatchEvent(new CustomEvent('ll:attach-node', { detail: { id: n.id } }))}>＋ #{n.id}</button> as context there, or type a <code>/command</code>.<ResetBtn runId={runId} id={n.id} onToast={onToast} /></div>

        {activeTab === 'Overview' && <Overview n={n} state={state} />}
        {activeTab === 'Trials' && <Trials n={n} detail={detail} state={state} />}
        {activeTab === 'Trace' && <Trace n={n} runId={runId} live={live} working={nodeWorking} />}
        {activeTab === 'Code' && <Code n={n} />}
        {activeTab === 'Metrics' && <Metrics n={n} detail={detail} state={state} />}
        {activeTab === 'Training' && <Training runId={runId} nodeId={nodeId} n={n} />}
        {activeTab === 'Trust' && <Trust n={n} drifts={nodeDrifts} />}
        {activeTab === 'Cost' && <Cost state={state} />}
      </div>
    </>
  )
}

function KV({ k, v }) { return <><div className="k">{k}</div><div className="v">{v}</div></> }

// "Training" tab: the node's LIVE training/eval logs (streamed eval.log) + online metric curves for
// EVERY metric the training framework logged (loss, each recall@k, lr, grad norms, …) — read from the
// node's TensorBoard events via the metrics adapters. Polls while open so it updates as training runs.
function Training({ runId, nodeId, n }) {
  const [logs, setLogs] = useState({ eval: '', setup: '', run_setup: '' })
  const [metrics, setMetrics] = useState({})
  const [tick, setTick] = useState(0)
  const [follow, setFollow] = useState(true)
  const preRef = useRef(null)
  const done = ['evaluated', 'failed', 'confirmed'].includes(n?.status)
  useEffect(() => {
    if (nodeId == null) return
    let on = true
    const load = () => {
      get(`/api/runs/${runId}/nodes/${nodeId}/logs`).then(d => on && setLogs(d || {})).catch(() => {})
      get(`/api/runs/${runId}/nodes/${nodeId}/metrics`).then(d => on && setMetrics((d && d.metrics) || {})).catch(() => {})
    }
    load()
    // Poll faster while the node is still running; slow to a light refresh once it's terminal.
    const iv = setInterval(() => { load(); setTick(x => x + 1) }, done ? 15000 : 3000)
    return () => { on = false; clearInterval(iv) }
  }, [runId, nodeId, done])
  useEffect(() => { if (follow && preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight }, [logs.eval, tick, follow])

  const evalLog = logs.eval || ''
  return (
    <div className="training-tab">
      <div className="section-title">Metric curves <span className="muted" style={{ fontWeight: 400 }}>· live, all logged metrics</span></div>
      <MetricLines series={metrics} />
      <div className="section-title" style={{ marginTop: 14 }}>
        Training / eval log
        <span className="muted" style={{ fontWeight: 400, marginLeft: 8 }}>{done ? 'finished' : 'live'}</span>
        <label className="muted" style={{ fontWeight: 400, marginLeft: 10, fontSize: 12 }}>
          <input type="checkbox" checked={follow} onChange={e => setFollow(e.target.checked)} /> follow
        </label>
      </div>
      <pre ref={preRef} className="training-log" style={{
        maxHeight: 360, overflow: 'auto', background: '#0b0e14', border: '1px solid #20252f',
        borderRadius: 6, padding: 8, fontSize: 11.5, lineHeight: 1.4, whiteSpace: 'pre-wrap', wordBreak: 'break-word'
      }}>{evalLog || (done ? '(no eval log)' : 'waiting for the eval to start…')}</pre>
      {logs.run_setup ? <>
        <div className="section-title" style={{ marginTop: 12 }}>Run setup (deps, once)</div>
        <pre className="training-log muted" style={{ maxHeight: 120, overflow: 'auto', background: '#0b0e14', border: '1px solid #20252f', borderRadius: 6, padding: 8, fontSize: 11 }}>{logs.run_setup.slice(-4000)}</pre>
      </> : null}
    </div>
  )
}

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
      <Trajectory nodes={members} direction={dir} height={150} onPick={onSelectNode} />
      <div className="section-h">Members <span className="pill">{members.length}</span></div>
      <table className="tbl"><thead><tr><th>node</th><th>operator</th><th>metric</th><th>status</th></tr></thead>
        <tbody>{members.map(n => <tr key={n.id} style={{ cursor: 'pointer' }} onClick={() => onSelectNode(n.id)}>
          <td>#{n.id}</td><td>{n.operator}</td><td>{fmt(n.confirmed_mean ?? n.metric)}</td><td>{n.status}</td></tr>)}</tbody></table>
    </div>
  </>
}

function Overview({ n, state }) {
  const p = n.idea?.params || {}
  const uses = mergeSummary(n, state.nodes || {})   // E3: for merges, which technique each parent fused
  const chg = nodeChip(n, state.nodes || {})        // same chip as the card (sweep-aware; '' for merges)
  return <>
    <div className="kv">
      <KV k="node" v={`#${n.id}`} />
      <KV k="operator" v={n.operator} />
      <KV k="parents" v={(n.parent_ids || []).join(', ') || '—'} />
      <KV k="status" v={n.status + (n.id === state.best_node_id ? ' — champion' : '')} />
      <KV k="metric" v={fmt(n.metric)} />
      {n.confirmed_mean != null && <KV k="robust mean" v={`${fmt(n.confirmed_mean)} ± ${fmt(n.confirmed_std)} (${n.confirmed_seeds}×)`} />}
      <KV k="feasible" v={String(n.feasible)} />
      <KV k="eval seconds" v={fmt(n.eval_seconds)} />
    </div>
    {chg && <><div className="section-h">What this node did</div><div className="v">{chg}</div></>}
    {uses.length > 0 && <><div className="section-h">Merge — techniques fused</div>
      <ul className="bul">{uses.map(u => <li key={u.parentId}>
        <b>#{u.parentId}</b>{u.theme ? ` · ${u.theme}` : ''}{u.change && u.change !== '—' ? ` — ${u.change}` : ''}</li>)}</ul></>}
    <div className="section-h">Idea params</div>
    {Object.keys(p).length ? <div className="kv">{Object.entries(p).map(([k, v]) => <KV key={k} k={k} v={fmt(v)} />)}</div> : <div className="muted">none</div>}
    {n.idea?.rationale && !(chg && chg.includes(n.idea.rationale)) && <><div className="section-h">Rationale</div><div className="v">{n.idea.rationale}</div></>}
    {n.annotations?.length > 0 && <><div className="section-h">Notes</div>{n.annotations.map((a, i) => <div key={i} className="chip" style={{ margin: 2 }}>{a}</div>)}</>}
    {n.deleted?.length > 0 && <><div className="section-h">Deleted files</div><div className="v">{n.deleted.join(', ')}</div></>}
  </>
}

// Trace timeline bounds: earliest start + total wall-span across the forest, so every span bar can be
// positioned by its OFFSET from t0 (a langfuse-style waterfall) rather than just sized by duration.
function traceBounds(spans) {
  let lo = Infinity, hi = 0
  const walk = (arr) => (arr || []).forEach(s => {
    const st = (typeof s.start === 'number') ? s.start : null
    const en = st != null ? st + (s.duration_s || 0) : (s.duration_s || 0)
    if (st != null && st < lo) lo = st
    if (en > hi) hi = en
    walk(s.children)
  })
  walk(spans)
  if (!isFinite(lo)) lo = 0
  return { t0: lo, total: Math.max(1e-9, hi - lo) }
}

// Friendly identity for each span kind — turns raw span names into "who did what" so the trace
// reads as the node's life story rather than instrumentation. `tone` colours the waterfall bar so
// phases are distinguishable at a glance. (Span names come from orchestrator.py.)
// icon = an OpIcon glyph name (monochrome, inherits the stage tone via currentColor — no color emoji).
const STAGE = {
  onboard:      { icon: 'flag', role: 'Onboarding', desc: 'task setup & eval spec', tone: '#8a7bb0' },
  create_node:  { icon: 'trending', role: 'Author node', desc: 'propose an idea, then build the solution', tone: '#6f8bb0' },
  propose:      { icon: 'search', role: 'Researcher', desc: 'propose the next idea', tone: '#6fa3b0' },
  implement:    { icon: 'gear', role: 'Developer', desc: 'write / edit the solution code', tone: '#6fae97' },
  repair:       { icon: 'bug', role: 'Developer · repair', desc: 'fix a failed parent', tone: '#b0936f' },
  evaluate:     { icon: 'target', role: 'Evaluation', desc: 'run the solution & score it', tone: '#a87da8' },
  confirm_seed: { icon: 'replay', role: 'Confirmation', desc: 'multi-seed robustness check', tone: '#9aa06f' },
  ablate:       { icon: 'sliders', role: 'Ablation', desc: 'sensitivity probe', tone: '#6f8bb0' },
}
const stageMeta = (name) => STAGE[name] || { icon: 'dot', role: name, desc: '', tone: 'var(--accent)' }

function llmEvents(s) { return (s.events || []).filter(e => e.name === 'llm_call') }

// Compact info helpers so each trace row carries the data that DIFFERENTIATES it (langfuse/Phoenix
// convention: model · input→output tokens · a content preview), instead of a bare op name repeated.
const ktok = (n) => (n == null ? '' : (n >= 1000 ? +(n / 1000).toFixed(n >= 9950 ? 0 : 1) + 'k' : String(n)))
const shortModel = (m) => (m || '').split('/').pop()
function callTok(c) { const t = c.tokens || {}; return { in: t.prompt, out: t.completion, total: t.total || ((t.prompt || 0) + (t.completion || 0)) } }
// First meaningful line of the completion (what the call PRODUCED) — falls back to the last user
// message (what it was ASKED) so even an empty/streaming completion still reads as something.
function callPreview(c) {
  const firstLine = (s) => (s || '').trim().split('\n').map(l => l.trim()).find(Boolean) || ''
  const compl = firstLine(c.completion)
  if (compl) return compl
  const lastUser = [...(c.prompt || [])].reverse().find(m => m.role === 'user')
  return firstLine(lastUser && lastUser.content)
}
// Roll the whole subtree of a span up to "how many model calls and how many tokens it cost" — shown on
// the stage/span header so you see the expensive steps without expanding anything. Counts first-class
// GENERATION spans (kind), and legacy `llm_call` events (older runs) so both render.
function spanRollup(s) {
  let calls = 0, tok = 0
  const walk = (x) => {
    if (x.kind === 'generation') { calls++; const u = (x.attributes || {}).usage || {}; tok += (u.total != null ? u.total : (u.prompt || 0) + (u.completion || 0)) }
    ;(x.events || []).forEach(e => { if (e.name === 'llm_call') { calls++; const t = callTok(e); tok += t.total || 0 } })
    ;(x.children || []).forEach(walk)
  }
  walk(s)
  return { calls, tok }
}

// Adapt a first-class GENERATION span (kind='generation', I/O held in attributes) to the same
// {op,model,prompt,completion,tokens,thinking,tool_calls} shape the legacy llm_call renderer uses —
// so a generation span and an old llm_call event display identically.
function genToCall(s) {
  const a = s.attributes || {}, u = a.usage || {}
  return {
    op: a.op, model: a.model, prompt: a.input || [],
    completion: typeof a.output === 'string' ? a.output : (a.output != null ? JSON.stringify(a.output, null, 2) : ''),
    thinking: a.thinking, tool_calls: a.tool_calls, model_parameters: a.model_parameters, cost: a.cost,
    tokens: { prompt: u.prompt, completion: u.completion, total: u.total },
  }
}
const asText = (v) => v == null ? '' : (typeof v === 'string' ? v : JSON.stringify(v, null, 2))

// The expandable body of a generation: the INPUT (prompt messages) and the OUTPUT (the model's text),
// plus a collapsed reasoning disclosure. Tool CALLS are NOT shown here — they render as their own
// indented tool observations directly beneath this chat (no duplication); when a turn produced only
// tool calls, its output is empty and we say so, pointing at the tools below.
function GenBody({ c }) {
  const [think, setThink] = useState(false)
  const nTools = (c.tool_calls || []).length
  return <div className="llm-io">
    {(c.model || c.model_parameters || c.cost != null) && <div className="kv">
      {c.model && <KV k="model" v={c.model} />}
      {c.model_parameters && <KV k="params" v={JSON.stringify(c.model_parameters)} />}
      {c.cost ? <KV k="cost" v={'$' + c.cost} /> : null}</div>}
    <div className="gen-sec-h">input</div>
    {(c.prompt || []).map((m, i) => <div key={i} className="msg">
      <div className={'msg-role role-' + (m.role || 'user')}>{m.role}</div>
      <pre className="code">{m.content}</pre></div>)}
    <div className="gen-sec-h">output</div>
    {c.completion
      ? <div className="msg"><pre className="code">{c.completion}</pre></div>
      : <div className="muted" style={{ fontSize: 12, padding: '2px 2px 4px' }}>
          {nTools ? `→ called ${nTools} tool${nTools > 1 ? 's' : ''} (shown below)` : '(no text output)'}</div>}
    {c.thinking && <div className="msg think-debug">
      <div className="msg-role role-think" onClick={() => setThink(v => !v)} style={{ cursor: 'pointer' }}>
        {think ? '▾' : '▸'} reasoning (debug)</div>
      {think && <Markdown className="think-body" text={c.thinking} />}</div>}
  </div>
}

// Render a list of sibling spans. Two behaviours:
//  • INDENT each tool observation one level under the generation before it — in the tool-loop the
//    sequence is (chat → tool → tool → chat → …), so a tool belongs to the last chat, making "which
//    chat called this tool" obvious without re-parenting the trace.
//  • CAP how many are rendered at once (a heavily-repaired node can have 800+ spans — rendering them
//    all freezes the browser / black screen). Show the first SPAN_CAP, then a "show N more" button;
//    the rest (and every span's full I/O) are always one click away — nothing is lost.
const SPAN_CAP = 60
function SpanList({ items, depth, t0, total, runId, parentOp = null }) {
  const [all, setAll] = useState(false)
  const rows = []
  let genDepth = null
  ;(items || []).forEach((c, i) => {
    const kind = c.kind || 'operation'
    if (kind === 'tool' && genDepth != null) { rows.push({ c, d: genDepth + 1, i }) }
    else { rows.push({ c, d: depth, i }); genDepth = (kind === 'generation') ? depth : null }
  })
  const shown = all ? rows : rows.slice(0, SPAN_CAP)
  return <>
    {shown.map(({ c, d, i }) => <SpanRow key={i} s={c} depth={d} t0={t0} total={total} runId={runId} parentOp={parentOp} />)}
    {!all && rows.length > SPAN_CAP && <button className="span-more" style={{ marginLeft: depth * 14 + 4 }}
      onClick={() => setAll(true)}>… show {rows.length - SPAN_CAP} more observations</button>}
  </>
}

// One span and its subtree, drawn as a langfuse-style waterfall row: the bar is positioned by the
// span's OFFSET from the trace start (t0) and sized by its duration, so sequence reads at a glance.
// Renders three observation kinds distinctly — GENERATION (an LLM call: op·model·in→out·preview, its
// prompt/output on expand), TOOL (name·arg, its input/output on expand), and OPERATION (a phase of
// work) — so the tree shows exactly what called what and what each produced. Nothing is truncated.
function SpanRow({ s, depth, t0, total, runId, parentOp = null }) {
  const [open, setOpen] = useState(false)
  const [io, setIo] = useState(null)   // lazily-fetched FULL i/o for a generation/tool (Langfuse-style)
  const kind = s.kind || 'operation'
  const err = s.status === 'ERROR'
  const off = (typeof s.start === 'number') ? Math.max(0, (s.start - t0) / total * 100) : 0
  const wid = Math.max(1.5, (s.duration_s || 0) / total * 100)
  const barTone = err ? 'var(--fail)' : kind === 'generation' ? 'var(--accent)' : kind === 'tool' ? 'var(--working)' : stageMeta(s.name).tone
  const bar = <span className="span-bar"><span className="span-fill" style={{ marginLeft: Math.min(98, off) + '%', width: wid + '%', background: barTone }} /></span>
  const kids = <SpanList items={s.children} depth={depth + 1} t0={t0} total={total} runId={runId} parentOp={s.name} />
  // On first expand of a generation/tool, pull the full (uncapped) input/output on demand — the tree
  // is served light so a long run stays fast, and NO information is lost (full text fetched here).
  useEffect(() => {
    if (open && io === null && runId && s.span_id && (kind === 'generation' || kind === 'tool')) {
      let on = true
      spanDetail(runId, s.span_id).then(d => on && setIo((d && d.attributes) || {})).catch(() => on && setIo({}))
      return () => { on = false }
    }
  }, [open])

  if (kind === 'generation') {
    // Row header from the LIGHT span (op·model·tokens); the prompt/output come from the fetched `io`.
    const a = { ...(s.attributes || {}), ...(io || {}) }
    const c = genToCall({ ...s, attributes: a }), t = callTok(c)
    return <>
      <div className={'span-row gen' + (err ? ' err' : '')} style={{ paddingLeft: depth * 14 }} onClick={() => setOpen(o => !o)} title="expand for prompt & output">
        <span className="span-tw">{open ? '▾' : '▸'}</span>
        {(() => {   // name the call by ROLE so "who writes code" is unmistakable: the Developer's LLM
          // call (under implement/repair) is "writing code"; the Researcher's (under propose) is "reasoning".
          const dev = parentOp === 'implement' || parentOp === 'repair'
          const label = dev ? 'writing code' : (parentOp === 'propose' && a.op === 'chat' ? 'reasoning' : (a.op || 'llm'))
          return <span className="span-name gen"><OpIcon name={dev ? 'pencil' : 'bulb'} className="t-ic" /> <span className={'llm-op' + (dev ? ' dev-code' : '')}>{label}</span>{a.model && <span className="llm-model" title={a.model}>{shortModel(a.model)}</span>}</span>
        })()}
        {bar}
        <span className="t">{fmt(s.duration_s, 3)}s</span>
        {(t.in != null || t.out != null) && <span className="badge" title={`${t.in || 0} prompt → ${t.out || 0} completion tokens`}>{ktok(t.in)}→{ktok(t.out)}</span>}
        {err && <span className="badge reason">ERROR</span>}
      </div>
      {open && <div className="span-detail" style={{ marginLeft: depth * 14 + 16 }}>
        {io === null ? <div className="muted" style={{ fontSize: 12 }}>loading…</div> : <GenBody c={c} />}</div>}
      {kids}
    </>
  }
  if (kind === 'tool') {
    const a = { ...(s.attributes || {}), ...(io || {}) }
    const inp = asText(a.input), outp = asText(a.output), name = (s.attributes || {}).tool || a.tool || 'tool'
    return <>
      <div className={'span-row tool' + (err ? ' err' : '')} style={{ paddingLeft: depth * 14 }} onClick={() => setOpen(o => !o)} title="expand for input & output">
        <span className="span-tw">{open ? '▾' : '▸'}</span>
        <span className="span-name tool"><OpIcon name="gear" className="t-ic" /> <b className="tool-name">{name}</b></span>
        {bar}
        <span className="t">{fmt(s.duration_s, 3)}s</span>
        {err && <span className="badge reason">ERROR</span>}
      </div>
      {open && <div className="span-detail" style={{ marginLeft: depth * 14 + 16 }}>
        {io === null ? <div className="muted" style={{ fontSize: 12 }}>loading…</div> : <>
          {inp && <div className="msg"><div className="msg-role role-user">input</div><pre className="code">{inp}</pre></div>}
          {outp && <div className="msg"><div className="msg-role role-completion">output</div><pre className="code">{outp}</pre></div>}
          {!inp && !outp && <div className="muted" style={{ fontSize: 12 }}>(no input/output recorded)</div>}</>}
      </div>}
      {kids}
    </>
  }
  // OPERATION span (a phase of work): its attributes, non-llm events, + legacy llm_call events (old runs).
  const attrs = Object.entries(s.attributes || {}).filter(([k]) => k !== 'node_id')
  const events = (s.events || []).filter(e => e.name !== 'llm_call')
  const calls = llmEvents(s)
  const m = stageMeta(s.name)
  const detail = attrs.length || events.length || calls.length
  return <>
    <div className={'span-row' + (err ? ' err' : '')} style={{ paddingLeft: depth * 14 }}
         onClick={() => detail && setOpen(o => !o)} title={detail ? 'click for step detail' : ''}>
      <span className="span-tw">{detail ? (open ? '▾' : '▸') : '·'}</span>
      <span className="span-name" title={m.desc}><OpIcon name={m.icon} className="t-ic" /> {m.role !== s.name ? m.role : s.name}</span>
      {bar}
      <span className="t">{fmt(s.duration_s, 3)}s</span>
      {calls.length > 0 && (() => { const tok = calls.reduce((a, c) => a + (callTok(c).total || 0), 0)
        return <span className="badge" title="model calls in this step — expand to read prompt & completion">{calls.length}×LLM{tok ? ` · ${ktok(tok)}` : ''}</span> })()}
      {err && <span className="badge reason">ERROR</span>}
    </div>
    {open && detail && <div className="span-detail" style={{ marginLeft: depth * 14 + 16 }}>
      {attrs.length > 0 && <div className="kv">{attrs.map(([k, v]) =>
        <KV key={k} k={k} v={typeof v === 'object' ? JSON.stringify(v) : String(v)} />)}</div>}
      {events.map((e, i) => <div key={i} className="span-ev">
        <span className="ty">{e.name}</span>{e.error ? <span className="flag"> {e.error}</span> :
          <span className="muted"> {Object.entries(e).filter(([k]) => k !== 'name').map(([k, v]) => `${k}=${v}`).join(' ')}</span>}
      </div>)}
      {calls.map((c, i) => <LlmCall key={i} call={{ ...c, span: s.name }} idx={i} />)}
    </div>}
    {kids}
  </>
}

// One LLM call as a COMPACT, information-dense row (the langfuse "generation" line): op · model ·
// in→out tokens · #prompt-msgs · 🧠 · a one-line content preview — so repeated calls in a loop read
// as distinct steps, not "chat / chat / chat". Click to expand the full prompt / completion / reasoning.
export function LlmCall({ call, idx }) {
  const [open, setOpen] = useState(idx === 0)   // first call expanded by default
  const [think, setThink] = useState(false)     // raw reasoning is debug-only — collapsed by default
  const t = callTok(call)
  const msgs = (call.prompt || []).length
  const preview = callPreview(call)
  return <div className={'llm-row' + (open ? ' open' : '')}>
    <div className="llm-line" onClick={() => setOpen(o => !o)} title={preview || 'expand for prompt & completion'}>
      <span className="span-tw">{open ? '▾' : '▸'}</span>
      {typeof idx === 'number' && <span className="llm-i">{idx + 1}</span>}
      <span className="llm-op">{call.op || 'llm'}</span>
      {call.model && <span className="llm-model" title={call.model}>{shortModel(call.model)}</span>}
      {(t.in != null || t.out != null) && <span className="llm-tok" title={`${t.in || 0} prompt → ${t.out || 0} completion tokens`}>{ktok(t.in)}→{ktok(t.out)}</span>}
      {msgs > 2 && <span className="llm-msgs" title={`${msgs} messages in the prompt (context size)`}>{msgs}m</span>}
      {call.thinking && <span className="llm-think" title="model reasoning captured"><OpIcon name="bulb" /></span>}
      {preview && <span className="llm-prev">{preview}</span>}
    </div>
    {open && <div className="llm-io">
      {(call.prompt || []).map((m, i) => <div key={i} className="msg">
        <div className={'msg-role role-' + (m.role || 'user')}>{m.role}</div>
        <pre className="code">{m.content}</pre>
      </div>)}
      <div className="msg">
        <div className="msg-role role-completion">completion</div>
        <pre className="code">{call.completion || '(empty)'}</pre>
      </div>
      {/* Raw <think> chain-of-thought: a debug aid only, kept collapsed so the clean answer above
          stays the primary view. The conclusion is what matters; this is how it got there. */}
      {call.thinking && <div className="msg think-debug">
        <div className="msg-role role-think" onClick={() => setThink(v => !v)} style={{ cursor: 'pointer' }}>
          {think ? '▾' : '▸'} reasoning (debug)
        </div>
        {think && <Markdown className="think-body" text={call.thinking} />}
      </div>}
    </div>}
  </div>
}

// A top-level lifecycle stage (one root span = one phase of work on this node), with its sub-steps.
// The header rolls up the stage's model-call count + token cost so the expensive phases stand out.
function StageBlock({ s, t0, total, runId }) {
  const m = stageMeta(s.name)
  const roll = spanRollup(s)
  return <div className={'stage' + (s.status === 'ERROR' ? ' err' : '')}>
    <div className="stage-h" title={m.desc}>
      <span className="stage-ic"><OpIcon name={m.icon} /></span>
      <b>{m.role}</b>
      {roll.calls > 0 && <span className="stage-roll" title={`${roll.calls} model call(s) · ~${roll.tok} tokens in this stage`}>{roll.calls} call{roll.calls > 1 ? 's' : ''} · {ktok(roll.tok)} tok</span>}
      <span className="spacer" style={{ flex: 1 }} />
      <span className="t">{fmt(s.duration_s, 3)}s</span>
    </div>
    <div className="spans">
      {(s.children || []).length
        ? <SpanList items={s.children} depth={0} t0={t0} total={total} runId={runId} />
        : <SpanRow s={s} depth={0} t0={t0} total={total} runId={runId} />}
    </div>
  </div>
}

// Reusable langfuse-style trace for ONE node's span forest — the lifecycle stages on a shared
// timeline. Exported so the chat feed can show the same waterfall inline (Dock.jsx) as the Inspector.
export function NodeTrace({ spans, runId }) {
  const roots = spans || []
  if (!roots.length) return <div className="muted" style={{ fontSize: 12 }}>No LLM/execution spans captured for this node yet.</div>
  const { t0, total } = traceBounds(roots)
  return <div className="trace">{roots.map((s, i) => <StageBlock key={i} s={s} t0={t0} total={total} runId={runId} />)}</div>
}

// The coding-agent's own validation report (was its own tab) — folded into the lifecycle as the
// Developer stage's verification footnote, only when an external agent actually wrote the node.
function AgentReport({ r }) {
  return <div className="stage">
    <div className="stage-h">
      <span className="stage-ic" style={{ color: r.ok && !r.fell_back ? 'var(--ok)' : r.fell_back ? 'var(--working)' : 'var(--fail)' }}>
        <OpIcon name={r.ok && !r.fell_back ? 'check' : r.fell_back ? 'replay' : 'cross'} /></span>
      <b>Developer · agent validation</b>
      <span className="muted">{r.fell_back ? 'fell back to template' : r.ok ? 'shipped clean' : 'failed checks'}</span>
      <span className="spacer" style={{ flex: 1 }} />
      <span className="muted">{r.attempts} attempt{r.attempts === 1 ? '' : 's'}</span>
    </div>
    <table className="tbl"><thead><tr><th>check</th><th>ok</th><th>detail</th></tr></thead>
      <tbody>{(r.checks || []).map((c, i) => <tr key={i}>
        <td>{c.name}</td><td style={{ color: c.ok ? 'var(--ok)' : 'var(--fail)' }}>{c.ok ? '✓' : '✗'}</td>
        <td className="muted">{c.detail || c.severity || ''}</td></tr>)}</tbody></table>
  </div>
}

// ── linear conversation view ─────────────────────────────────────────────────────────────────────
// The raw span tree re-shows the WHOLE re-sent message list on every generation (a tool-loop re-sends
// the growing history each turn → the system+user prompt and every prior turn duplicate N times). The
// conversation view reconstructs the loop as a readable thread: the request once per sub-loop, then
// each generation's DELTA (reasoning + text + tool calls) interleaved with the tool executions.
function ConvRequest({ t }) {
  const [open, setOpen] = useState(false)   // system prompt is big — collapsed by default
  const roles = (t.messages || []).map(m => m.role).join(' + ')
  return <div className="conv-req">
    <div className="conv-req-h" onClick={() => setOpen(o => !o)} title="the system + user prompt for this sub-loop (shown once)">
      <span className="span-tw">{open ? '▾' : '▸'}</span>
      <OpIcon name="chat" className="t-ic" /> <b>request</b>
      {t.label && <span className="llm-op">{t.label}</span>}
      <span className="muted conv-req-roles"> {roles}</span>
    </div>
    {open && <div className="conv-req-body">
      {(t.messages || []).map((m, i) => <div key={i} className="msg">
        <div className={'msg-role role-' + (m.role || 'user')}>{m.role}</div>
        <pre className="code">{m.content}</pre></div>)}
    </div>}
  </div>
}

function ConvGen({ t }) {
  const [think, setThink] = useState(false)
  const calls = t.tool_calls || []
  const u = t.usage || {}
  const tok = u.total || (u.prompt || 0) + (u.completion || 0)
  // strip the trailing "[tool_calls: …]" marker — the calls are their own chip + the tool rows below
  const text = (t.output || '').replace(/\n*\[tool_calls:[^\]]*\]\s*$/, '').trim()
  return <div className={'conv-gen' + (t.status === 'ERROR' ? ' err' : '')}>
    <div className="conv-gen-h">
      <OpIcon name="bulb" className="t-ic" />
      {t.model && <span className="llm-model" title={t.model}>{shortModel(t.model)}</span>}
      {tok ? <span className="badge" title={`${u.prompt || 0} prompt → ${u.completion || 0} completion tokens`}>{ktok(tok)} tok</span> : null}
      {t.seconds != null && <span className="t">{fmt(t.seconds, 2)}s</span>}
      {t.status === 'ERROR' && <span className="badge reason">ERROR</span>}
    </div>
    {t.think && <div className="msg think-debug">
      <div className="msg-role role-think" onClick={() => setThink(v => !v)} style={{ cursor: 'pointer' }}>
        {think ? '▾' : '▸'} thinking</div>
      {think && <Markdown className="think-body" text={t.think} />}</div>}
    {text && <div className="conv-out"><Markdown text={text} /></div>}
    {calls.length > 0 && <div className="conv-calls muted">→ called {calls.join(', ')}</div>}
    {!text && !t.think && calls.length === 0 && <div className="muted" style={{ fontSize: 12 }}>(no output)</div>}
  </div>
}

function ConvTool({ t }) {
  const [open, setOpen] = useState(false)
  const err = t.status === 'ERROR'
  return <div className={'conv-tool' + (err ? ' err' : '')}>
    <div className="conv-tool-h" onClick={() => setOpen(o => !o)} title="tool call — expand for input & output">
      <span className="span-tw">{open ? '▾' : '▸'}</span>
      <OpIcon name="gear" className="t-ic" /> <b className="tool-name">{t.name}</b>
      {!open && t.input && <span className="muted conv-tool-prev"> {t.input.slice(0, 60)}</span>}
      {err && <span className="badge reason">ERROR</span>}
      {t.seconds != null && <span className="t">{fmt(t.seconds, 2)}s</span>}
    </div>
    {open && <div className="conv-tool-body">
      {t.input && <div className="msg"><div className="msg-role role-user">input</div><pre className="code">{t.input}</pre></div>}
      {t.output && <div className="msg"><div className="msg-role role-completion">output</div><pre className="code">{t.output}</pre></div>}
      {!t.input && !t.output && <div className="muted" style={{ fontSize: 12 }}>(no input/output recorded)</div>}
    </div>}
  </div>
}

function ConvStage({ st }) {
  const m = stageMeta(st.label)
  const roll = st.rollup || {}
  const rtok = (roll.tokens || {}).total
  return <div className={'stage' + (st.status === 'ERROR' ? ' err' : '')}>
    <div className="stage-h" title={m.desc}>
      <span className="stage-ic"><OpIcon name={m.icon} /></span>
      <b>{m.role}</b>
      {(roll.generations || roll.tools) ? <span className="stage-roll">
        {roll.generations || 0} turn{roll.generations === 1 ? '' : 's'}
        {roll.tools ? ` · ${roll.tools} tool call${roll.tools === 1 ? '' : 's'}` : ''}
        {rtok ? ` · ${ktok(rtok)} tok` : ''}</span> : null}
    </div>
    <div className="conv-turns">
      {(st.turns || []).map((t, j) => t.type === 'request' ? <ConvRequest key={j} t={t} />
        : t.type === 'tool' ? <ConvTool key={j} t={t} /> : <ConvGen key={j} t={t} />)}
    </div>
  </div>
}

function Conversation({ n, runId, working }) {
  const [conv, setConv] = useState(null)
  useEffect(() => {
    let on = true
    setConv(null)   // node changed → clear before the first load (poll ticks below don't clear, so no flash)
    const load = () => nodeConversation(runId, n.id).then(d => on && setConv(d || { stages: [] })).catch(() => on && setConv({ stages: [] }))
    load()
    const iv = working ? setInterval(load, 4000) : null   // live-refresh while the agent works this node
    return () => { on = false; if (iv) clearInterval(iv) }
  }, [runId, n.id, working])
  if (conv === null) return <div className="muted" style={{ fontSize: 12 }}>loading…</div>
  const stages = conv.stages || []
  if (!stages.length) return <div className="muted">No conversation captured for this node yet.</div>
  return <div className="conv">{stages.map((st, i) => <ConvStage key={i} st={st} />)}</div>
}

function Trace({ n, runId, live, working }) {
  const [view, setView] = useState('conversation')   // linear reading by default; raw tree on demand
  const bodyRef = useRef(null)
  const spans = n.trace?.nodes || []
  const agent = n.agent_report
  // Live status: what an LLM is doing on this node RIGHT NOW — only while it's actually working the node
  // (writing code / repairing), never during the training eval (the user doesn't want a pulse then).
  const _op = (working && live?.building?.node_id === n.id) ? (live.building.operator || '') : ''
  const statusLabel = working
    ? (/repair|debug/.test(_op) ? '🔧 repairing…' : /merge/.test(_op) ? '🔀 merging…' : '✍️ writing code…')
    : null
  const status = statusLabel && <div className="trace-live-status"><span className="tls-dot" />{statusLabel}
    <span className="muted" style={{ marginLeft: 6, fontSize: 11 }}>live — updates on its own</span></div>
  const scrollTo = (where) => { const c = bodyRef.current?.closest('.insp-body'); if (c) c.scrollTop = where === 'top' ? 0 : c.scrollHeight }
  const nav = <span className="trace-nav">
    <button className="seg" title="scroll to top" onClick={() => scrollTo('top')}>↑</button>
    <button className="seg" title="scroll to newest (bottom)" onClick={() => scrollTo('bottom')}>↓</button></span>
  if (!spans.length && !agent) {
    // While the agent is WORKING this node, node_detail's trace may still be empty (its create_node
    // root span hasn't closed) — but /conversation rebuilds LIVE from the sub-spans that have already
    // flushed. So mount the live-polling Conversation (it refreshes every 4s) instead of a dead
    // placeholder: steps now appear as each generation/tool completes, not all at once at the end.
    if (working)
      return <div className="trace" ref={bodyRef}>{status}<Conversation n={n} runId={runId} working={working} /></div>
    return <div className="trace" ref={bodyRef}>{status}<div className="muted">No execution spans for this node yet — toy/offline nodes have minimal spans, and a node still in progress fills its trace as it runs.</div></div>
  }
  const toggle = <div className="conv-toggle">
    <button className={'seg' + (view === 'conversation' ? ' on' : '')} onClick={() => setView('conversation')}
      title="Linear, de-duplicated reading: request once, then each turn's reasoning + tools">conversation</button>
    <button className={'seg' + (view === 'raw' ? ' on' : '')} onClick={() => setView('raw')}
      title="The raw span tree with each generation's full re-sent message list">raw spans</button>
    <span style={{ flex: 1 }} />{nav}
  </div>
  if (view === 'conversation')
    return <div className="trace" ref={bodyRef}>{status}{toggle}<Conversation n={n} runId={runId} working={working} />
      {agent && <AgentReport r={agent} />}</div>
  const { t0, total } = traceBounds(spans)
  // create_node already nests propose→implement; if an agent wrote the node, the report belongs
  // right after that authoring stage (placed by index), otherwise it trails the whole lifecycle.
  const authorIdx = spans.findIndex(s => ['create_node', 'implement', 'repair'].includes(s.name))
  const roll = n.trace?.rollup || {}
  const rtok = roll.tokens || {}
  return <div className="trace" ref={bodyRef}>
    {status}
    {toggle}
    <div className="muted" style={{ marginBottom: 10 }}>
      Lifecycle of node #{n.id} — each part on a shared timeline (offset = when it ran, bar = how long).
      Expand any observation to read its input &amp; output.
      {(roll.generations || roll.tools) ? <span className="trace-totals">
        {' · '}{roll.generations || 0} generation{roll.generations === 1 ? '' : 's'}
        {roll.tools ? ` · ${roll.tools} tool call${roll.tools === 1 ? '' : 's'}` : ''}
        {rtok.total ? ` · ${ktok(rtok.total)} tok` : ''}
        {roll.cost ? ` · $${roll.cost}` : ''}
      </span> : null}
    </div>
    {spans.map((s, i) => <React.Fragment key={i}>
      <StageBlock s={s} t0={t0} total={total} runId={runId} />
      {agent && i === authorIdx && <AgentReport r={agent} />}
    </React.Fragment>)}
    {agent && authorIdx < 0 && <AgentReport r={agent} />}
  </div>
}

export function diffLines(a, b) {
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

function Metrics({ n, detail, state }) {
  const seeds = detail?.confirm_seeds_detail || {}
  const vals = Object.entries(seeds).map(([s, v]) => ({ s: Number(s), v })).filter(x => x.v != null).sort((a, b) => a.s - b.s)
  // Every metric reported anywhere in the run (the objective ★ + all auto-captured extras), shown for
  // THIS node and for the champion (the run's best node), so "the metrics you wanted to see overall"
  // are all visible + comparable. Only the objective drives selection; extras are audit-only.
  const nodes = Object.values(state?.nodes || {})
  const extraKeys = [...new Set(nodes.flatMap(x => Object.keys(x.extra_metrics || {})))]
  const champ = state?.best_node_id != null ? nodes.find(x => x.id === state.best_node_id) : null
  const showChamp = champ && champ.id !== n.id
  const rows = [
    { k: 'objective', mine: n.confirmed_mean ?? n.metric, best: champ ? (champ.confirmed_mean ?? champ.metric) : null, star: true },
    ...extraKeys.map(k => ({ k, mine: n.extra_metrics?.[k], best: champ?.extra_metrics?.[k] })),
  ]
  return <>
    <div className="section-h">Reported metrics{champ ? ` · best = #${champ.id}` : ''}</div>
    <table className="tbl"><thead><tr><th>metric</th><th>this node</th>{showChamp && <th>best #{champ.id}</th>}</tr></thead>
      <tbody>{rows.map(r => <tr key={r.k} className={r.star ? 'chosen-row' : ''}>
        <td>{r.star ? '★ ' : ''}{r.k}</td><td>{fmt(r.mine)}</td>
        {showChamp && <td>{fmt(r.best)}</td>}</tr>)}</tbody></table>
    {n.confirmed_mean != null && <div className="kv" style={{ marginTop: 8 }}>
      <KV k="robust mean ± std" v={`${fmt(n.confirmed_mean)} ± ${fmt(n.confirmed_std)} over ${n.confirmed_seeds || vals.length} seeds`} /></div>}
    {vals.length > 0 && <>
      <div className="section-h">Per-seed confirmation</div>
      <table className="tbl"><thead><tr><th>seed</th><th>metric</th></tr></thead>
        <tbody>{vals.map(x => <tr key={x.s}><td>{x.s}</td><td>{fmt(x.v)}</td></tr>)}</tbody></table>
    </>}
  </>
}

// Intra-node sweep trials: a sortable table of every config the node ran in-process, plus
// parallel-coords / scatter views. Trials aren't backend nodes, so the charts get pseudo-node
// adapters ({id, metric, idea:{params}, feasible}) — no charts.jsx change needed.
function Trials({ n, detail, state }) {
  const trials = detail?.trials ?? n.trials ?? []
  const summary = n.trials_summary
  const [sortKey, setSortKey] = useState('metric')
  const [sortDir, setSortDir] = useState(state.direction === 'min' ? 'asc' : 'desc')
  const [showAll, setShowAll] = useState(false)
  if (!trials.length) {
    return <div className="muted">{summary
      ? `Sweep of ${summary.count} trial(s) — loading full results…`
      : 'No trials recorded for this node.'}</div>
  }
  const dir = state.direction
  const params = Array.from(new Set(trials.flatMap(t => Object.keys(t.params || {}))))
  // best trial = best metric under direction (matches the node's scalar metric)
  let bestIdx = -1, bestV = null
  trials.forEach((t, i) => { if (t.metric != null && (bestV == null || (dir === 'min' ? t.metric < bestV : t.metric > bestV))) { bestV = t.metric; bestIdx = i } })
  const setSort = (k) => { if (k === sortKey) setSortDir(d => d === 'asc' ? 'desc' : 'asc'); else { setSortKey(k); setSortDir('asc') } }
  const val = (t, k) => k === 'idx' ? t._i : k === 'metric' ? t.metric : k === 'seconds' ? t.seconds : t.params?.[k]
  const rowsAll = trials.map((t, i) => ({ ...t, _i: i })).sort((a, b) => {
    const av = val(a, sortKey), bv = val(b, sortKey)
    if (av == null) return 1; if (bv == null) return -1
    const cmp = (typeof av === 'number' && typeof bv === 'number') ? av - bv : String(av).localeCompare(String(bv))
    return sortDir === 'asc' ? cmp : -cmp
  })
  const CAP = 100
  const rows = showAll ? rowsAll : rowsAll.slice(0, CAP)
  const okN = trials.filter(t => t.metric != null).length
  const totSec = trials.reduce((s, t) => s + (t.seconds || 0), 0)
  // pseudo-nodes for the existing charts (they read n.idea?.params and n.confirmed_mean ?? n.metric)
  const pseudo = trials.map((t, i) => ({ id: i, metric: t.metric, confirmed_mean: null, idea: { params: t.params || {} }, feasible: t.metric != null }))
  const scatter = params.length
    ? trials.map((t, i) => ({ x: t.params?.[params[0]] ?? i, y: t.metric, feasible: t.metric != null, id: i })).filter(d => d.y != null)
    : []
  const Th = ({ k, children }) => <th style={{ cursor: 'pointer' }} onClick={() => setSort(k)}>{children}{sortKey === k ? (sortDir === 'asc' ? ' ▲' : ' ▼') : ''}</th>
  return <>
    <div className="kv">
      <KV k="trials" v={trials.length} />
      <KV k="best metric" v={`${fmt(bestV)}${bestIdx >= 0 ? ` (#${bestIdx})` : ''}`} />
      <KV k="ok / failed" v={`${okN} / ${trials.length - okN}`} />
      <KV k="Σ seconds" v={fmt(totSec)} />
    </div>
    {params.length > 0 && <>
      <div className="section-h">Params → metric</div>
      <ParallelCoords nodes={pseudo} direction={dir} height={220} />
    </>}
    {scatter.length > 0 && <>
      <div className="section-h">{params[0]} vs metric</div>
      <Scatter data={scatter} xlab={params[0]} ylab="metric" height={220} />
    </>}
    <div className="section-h">Trials <span className="pill">{trials.length}</span></div>
    <table className="tbl">
      <thead><tr><Th k="idx">#</Th>{params.map(p => <Th key={p} k={p}>{p}</Th>)}<Th k="metric">metric</Th><Th k="seconds">s</Th></tr></thead>
      <tbody>{rows.map(t => <tr key={t._i}
        className={t._i === bestIdx ? 'best-row' : ''}>
        <td>#{t._i}{t._i === bestIdx ? <OpIcon name="crown" size={10} /> : ''}</td>
        {params.map(p => <td key={p}>{t.params?.[p] != null ? fmt(t.params[p]) : '—'}</td>)}
        <td>{t.metric != null ? fmt(t.metric) : <span className="badge reason">{t.error ? 'error' : 'failed'}</span>}</td>
        <td className="muted">{fmt(t.seconds)}</td></tr>)}</tbody>
    </table>
    {rowsAll.length > CAP && <button className="btn sm ghost" style={{ marginTop: 6 }} onClick={() => setShowAll(s => !s)}>
      {showAll ? 'show fewer' : `show all ${rowsAll.length}`}</button>}
  </>
}

function Trust({ n, drifts = [] }) {
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
    <div className="section-h">Metric drift</div>
    {drifts.length
      ? <table className="tbl"><thead><tr><th>seed</th><th>primary</th><th>cross-check</th><th>tol</th></tr></thead>
        <tbody>{drifts.map((d, i) => <tr key={i}><td>{d.seed ?? '—'}</td><td className="flag">{fmt(d.primary)}</td><td>{fmt(d.cross)}</td><td className="muted">{fmt(d.tolerance)}</td></tr>)}</tbody></table>
      : <div className="chip ok">no uncorroborated (drifted) metrics</div>}
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
