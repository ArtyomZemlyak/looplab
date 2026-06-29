import React, { useEffect, useState } from 'react'
import { get, fmt, fmtInt, isSweep } from './util.js'
import { Trajectory, ParallelCoords, Scatter } from './charts.jsx'
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
const TABS = ['Overview', 'Trace', 'Code', 'Metrics', 'Trust', 'Cost']

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
        <div className="insp-hint muted">Actions (confirm · ablate · fork · promote · note) live in the chat — add <span className="ctx-chip" style={{ padding: '0 6px' }}>＋ #{n.id}</span> as context there, or type a <code>/command</code>.</div>

        {activeTab === 'Overview' && <Overview n={n} state={state} />}
        {activeTab === 'Trials' && <Trials n={n} detail={detail} state={state} />}
        {activeTab === 'Trace' && <Trace n={n} />}
        {activeTab === 'Code' && <Code n={n} />}
        {activeTab === 'Metrics' && <Metrics n={n} detail={detail} />}
        {activeTab === 'Trust' && <Trust n={n} drifts={nodeDrifts} />}
        {activeTab === 'Cost' && <Cost state={state} />}
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
  const uses = mergeSummary(n, state.nodes || {})   // E3: for merges, which technique each parent fused
  const chg = nodeChip(n, state.nodes || {})        // same chip as the card (sweep-aware; '' for merges)
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
    {chg && <><div className="section-h">What changed vs parent</div><div className="v">{chg}</div></>}
    {uses.length > 0 && <><div className="section-h">Merge — techniques fused</div>
      <ul className="bul">{uses.map(u => <li key={u.parentId}>
        <b>#{u.parentId}</b>{u.theme ? ` · ${u.theme}` : ''}{u.change && u.change !== '—' ? ` — ${u.change}` : ''}</li>)}</ul></>}
    <div className="section-h">Idea params</div>
    {Object.keys(p).length ? <div className="kv">{Object.entries(p).map(([k, v]) => <KV key={k} k={k} v={fmt(v)} />)}</div> : <div className="muted">none</div>}
    {n.idea?.rationale && <><div className="section-h">Rationale</div><div className="v">{n.idea.rationale}</div></>}
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
// the stage/span header so you see the expensive steps without expanding anything.
function spanRollup(s) {
  let calls = 0, tok = 0
  const walk = (x) => {
    (x.events || []).forEach(e => { if (e.name === 'llm_call') { calls++; const t = callTok(e); tok += t.total || 0 } })
    ;(x.children || []).forEach(walk)
  }
  walk(s)
  return { calls, tok }
}

// One span and its subtree, drawn as a langfuse-style waterfall row: the bar is positioned by the
// span's OFFSET from the trace start (t0) and sized by its duration, so concurrency/sequence reads at
// a glance. LLM I/O is inlined (expand the span to read it) so a step's prompt/response sits next to
// the step that made it.
function SpanRow({ s, depth, t0, total }) {
  const [open, setOpen] = useState(false)
  const attrs = Object.entries(s.attributes || {}).filter(([k]) => k !== 'node_id')
  const events = (s.events || []).filter(e => e.name !== 'llm_call')
  const calls = llmEvents(s)
  const m = stageMeta(s.name)
  const detail = attrs.length || events.length || calls.length
  const off = (typeof s.start === 'number') ? Math.max(0, (s.start - t0) / total * 100) : 0
  const wid = Math.max(1.5, (s.duration_s || 0) / total * 100)
  return <>
    <div className={'span-row' + (s.status === 'ERROR' ? ' err' : '')} style={{ paddingLeft: depth * 14 }}
         onClick={() => detail && setOpen(o => !o)} title={detail ? 'click for step detail' : ''}>
      <span className="span-tw">{detail ? (open ? '▾' : '▸') : '·'}</span>
      <span className="span-name" title={m.desc}><OpIcon name={m.icon} className="t-ic" /> {m.role !== s.name ? m.role : s.name}</span>
      <span className="span-bar"><span className="span-fill" style={{
        marginLeft: Math.min(98, off) + '%', width: wid + '%',
        background: s.status === 'ERROR' ? 'var(--fail)' : m.tone }} /></span>
      <span className="t">{fmt(s.duration_s, 3)}s</span>
      {calls.length > 0 && (() => { const tok = calls.reduce((a, c) => a + (callTok(c).total || 0), 0)
        return <span className="badge" title="model calls in this step — expand to read prompt & completion">{calls.length}×LLM{tok ? ` · ${ktok(tok)}` : ''}</span> })()}
      {s.status === 'ERROR' && <span className="badge reason">ERROR</span>}
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
    {(s.children || []).map((c, i) => <SpanRow key={i} s={c} depth={depth + 1} t0={t0} total={total} />)}
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
function StageBlock({ s, t0, total }) {
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
        ? (s.children || []).map((c, i) => <SpanRow key={i} s={c} depth={0} t0={t0} total={total} />)
        : <SpanRow s={s} depth={0} t0={t0} total={total} />}
    </div>
  </div>
}

// Reusable langfuse-style trace for ONE node's span forest — the lifecycle stages on a shared
// timeline. Exported so the chat feed can show the same waterfall inline (Dock.jsx) as the Inspector.
export function NodeTrace({ spans }) {
  const roots = spans || []
  if (!roots.length) return <div className="muted" style={{ fontSize: 12 }}>No LLM/execution spans captured for this node yet.</div>
  const { t0, total } = traceBounds(roots)
  return <div className="trace">{roots.map((s, i) => <StageBlock key={i} s={s} t0={t0} total={total} />)}</div>
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

function Trace({ n }) {
  const spans = n.trace?.nodes || []
  const agent = n.agent_report
  if (!spans.length && !agent)
    return <div className="muted">No execution spans for this node yet — toy/offline nodes have minimal spans, and a node still in progress fills its trace as it runs.</div>
  const { t0, total } = traceBounds(spans)
  // create_node already nests propose→implement; if an agent wrote the node, the report belongs
  // right after that authoring stage (placed by index), otherwise it trails the whole lifecycle.
  const authorIdx = spans.findIndex(s => ['create_node', 'implement', 'repair'].includes(s.name))
  return <div className="trace">
    <div className="muted" style={{ marginBottom: 10 }}>
      Lifecycle of node #{n.id} — each part on a shared timeline (offset = when it ran, bar = how long).
      Expand a step to read its LLM prompt &amp; completion.
    </div>
    {spans.map((s, i) => <React.Fragment key={i}>
      <StageBlock s={s} t0={t0} total={total} />
      {agent && i === authorIdx && <AgentReport r={agent} />}
    </React.Fragment>)}
    {agent && authorIdx < 0 && <AgentReport r={agent} />}
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
        <td>#{t._i}{t._i === bestIdx ? ' ♚' : ''}</td>
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
