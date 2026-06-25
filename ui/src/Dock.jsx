import React, { useEffect, useMemo, useRef, useState } from 'react'
import { get, fmt, chat, command, applyAction, workingId } from './util.js'
import Markdown from './markdown.jsx'
import { NodeTrace, LlmCall } from './Inspector.jsx'

const HELP = 'Commands: /confirm #n · /ablate #n · /fork #n · /promote #n · /note #n text · /hint text · /strategy policy=ucb fidelity=low · /deep-research · /experiment <idea> · /approve #n · /ratify · /pause · /resume · /stop · /refresh. Or just type what you want and I’ll propose an action to confirm.'

// Parse a leading-slash command into a control action (no LLM — deterministic + offline-safe). Returns
// {action} | {error} | {help} | {llm:instruction} (route to the action-router) | null (not a command).
function parseSlash(text, ctxId, state) {
  const m = text.trim().match(/^\/(\S+)\s*([\s\S]*)$/)
  if (!m) return null
  const cmd = m[1].toLowerCase(), rest = (m[2] || '').trim()
  const idArg = () => { const mm = rest.match(/#?(\d+)/); return mm ? Number(mm[1]) : (ctxId ?? null) }
  const afterId = () => rest.replace(/#?\d+\s*/, '').trim()
  const need = (id, label) => id == null ? { error: `which node? add #id or drag one in — e.g. /${cmd} #5` } : label
  switch (cmd) {
    case 'help': return { help: true }
    case 'pause': return { action: { type: 'pause', data: {}, label: 'Pause the run' } }
    case 'resume': return { action: { type: 'resume', data: {}, label: 'Resume the run' } }
    case 'stop': case 'abort': return { action: { type: 'run_abort', data: { reason: 'ui' }, label: 'Stop the run' } }
    case 'confirm': { const id = idArg(); return need(id, { action: { type: 'force_confirm', data: { node_id: id }, label: `Confirm #${id} (multi-seed robustness)` } }) }
    case 'ablate': { const id = idArg(); return need(id, { action: { type: 'force_ablate', data: { node_id: id }, label: `Ablate #${id} (sensitivity probe)` } }) }
    case 'fork': { const id = idArg(); return need(id, { action: { type: 'fork', data: { from_node_id: id }, label: `Fork an improve-branch from #${id}` } }) }
    case 'promote': { const id = idArg(); return need(id, { action: { type: 'promote', data: { node_id: id, alias: 'champion' }, label: `Promote #${id} to champion` } }) }
    case 'note': { const id = idArg(), txt = afterId(); return (id == null || !txt) ? { error: 'usage: /note #id your note' } : { action: { type: 'annotation', data: { node_id: id, text: txt }, label: `Note on #${id}` } } }
    case 'hint': return rest ? { action: { type: 'hint', data: { text: rest }, label: `Hint: ${rest.slice(0, 60)}` } } : { error: 'usage: /hint your directive' }
    case 'strategy': { const s = {}; rest.split(/\s+/).forEach(kv => { const [k, v] = kv.split('='); if (k && v) s[k] = v }); return Object.keys(s).length ? { action: { type: 'set_strategy', data: { strategy: s }, label: `Switch strategy → ${JSON.stringify(s)}` } } : { error: 'usage: /strategy policy=ucb fidelity=low' } }
    case 'deep-research': case 'research': return { action: { type: 'deep_research', data: {}, label: 'Run a deep-research step now' } }
    case 'approve': { const id = idArg() ?? state.best_node_id; return { action: { type: 'approval_granted', data: { node_id: id }, label: `Approve #${id}` } } }
    case 'ratify': return { action: { type: 'spec_approved', data: {}, label: 'Ratify the eval spec' } }
    case 'refresh': return { action: { type: '__refresh_report__', data: {}, label: 'Refresh the run report' } }
    case 'experiment': case 'new': return { llm: rest || 'propose and add the next experiment' }
    default: return { error: `unknown command /${cmd} — try /help` }
  }
}

// The unified chat surface (Workstream B): one scrubbable, filterable feed that renders every run
// event as a differentiated chat message AND hosts the command/conversation input. There are no tab
// names and no Gantt — this is simply "the chat".

const NARR = {
  run_started: (d) => `run started — ${d.goal || d.task_id} (${d.direction})`,
  node_created: (d) => `node #${d.node_id} via ${d.operator}${d.idea?.rationale ? ' — ' + d.idea.rationale.slice(0, 80) : ''}`,
  node_evaluated: (d) => `node #${d.node_id} → ${fmt(d.metric)}`,
  node_failed: (d) => `node #${d.node_id} failed (${d.reason})${d.triage_action === 'reject_idea' ? ' — idea rejected' + (d.triage_rationale ? ': ' + String(d.triage_rationale).slice(0, 70) : '') : ''}`,
  node_repaired: (d) => `node #${d.node_id} repaired in place (attempt ${d.attempt})${d.rationale ? ' — ' + String(d.rationale).slice(0, 80) : ''}`,
  node_confirmed: (d) => `node #${d.node_id} confirmed: ${fmt(d.mean)} ±${fmt(d.std)} (${d.seeds}×)`,
  best_confirmed: (d) => `robust winner: #${d.node_id}${d.significant ? ' (significant >1SE)' : ''}`,
  ablate: (d) => `ablated #${d.parent_id}: ${Object.entries(d.impacts || {}).map(([k, v]) => `${k}=${fmt(v, 2)}`).join(', ')}`,
  data_leakage: (d) => `leakage scan: ${d.leak ? 'LEAK DETECTED' : 'clean'}`,
  approval_requested: (d) => `awaiting approval of #${d.node_id}`,
  approval_granted: (d) => `approved #${d.node_id}`,
  pause: () => 'paused by operator', resume: () => 'resumed', run_abort: () => 'abort requested',
  node_abort: (d) => `stop requested for #${d.node_id}`, budget_extend: (d) => `budget extended ${JSON.stringify(d)}`,
  hint: (d) => `hint: ${d.text}`, promote: (d) => `promoted #${d.node_id} → ${d.alias || 'champion'}`,
  policy_decision: (d) => `chose #${d.chosen}${d.reason ? ' (' + d.reason + ')' : ''} over ${Object.keys(d.scores || {}).length} candidate(s)`,
  strategy_decision: (d) => `strategy → ${d.strategy?.policy || '?'}${d.strategy?.fidelity ? '/' + d.strategy.fidelity : ''}${d.strategy?.rationale ? ' — ' + d.strategy.rationale.slice(0, 70) : ''}`,
  rung_promoted: (d) => `ASHA rung ↑${d.rung}: promoted ${(d.survivors || []).map(s => '#' + s).join(', ')}`,
  set_strategy: (d) => `operator pinned strategy → ${d.strategy?.policy || ''}${d.strategy?.fidelity ? '/' + d.strategy.fidelity : ''}`,
  deep_research: () => 'deep research requested',
  research_completed: (d) => `deep research (${d.trigger || 'auto'})${d.memo?.summary ? ' — ' + String(d.memo.summary).slice(0, 80) : ''}`,
  report_generated: (d) => `run report updated${d.content?.headline ? ' — ' + String(d.content.headline).slice(0, 90) : ''}`,
  proxy_scored: (d) => `proxy scored #${d.node_id}: ${fmt(d.score)}${d.skipped ? ' (skipped full eval)' : ''}`,
  reward_hack_suspected: (d) => `reward-hack suspected on #${d.node_id}: ${(d.signals || []).map(s => s.signal).join(', ')}`,
  novelty_rejected: (d) => `dedup: proposal near #${d.near_node} (dist ${fmt(d.distance, 3)}) nudged to diversify`,
  run_finished: (d) => `run finished${d.reason ? ' (' + d.reason + ')' : ''}`,
  llm_cost: (d) => `LLM: ${d.total_tokens} tokens, $${fmt(d.cost)}`,
}

// The node an event refers to, if any — lets a feed click drill into that node.
function eventNode(e) {
  const d = e.data || {}
  return d.node_id ?? d.parent_id ?? null
}

// Coarse "kind" per event type: drives the icon, the accent color, and the filter chips. One place so
// the legend, the row, and the filter all agree.
const GROUPS = [
  ['proposal', '🧪', 'proposal'],
  ['eval', '📊', 'results'],
  ['decision', '🧭', 'decisions'],
  ['research', '🔬', 'research'],
  ['report', '📋', 'report'],
  ['trust', '⚠', 'trust'],
  ['control', '⚙', 'actions'],
  ['lifecycle', '▸', 'lifecycle'],
]
const TYPE2GROUP = {
  node_created: 'proposal',
  node_evaluated: 'eval', node_failed: 'eval', node_repaired: 'eval', node_confirmed: 'eval', best_confirmed: 'eval', proxy_scored: 'eval', ablate: 'eval',
  policy_decision: 'decision', strategy_decision: 'decision', rung_promoted: 'decision', agent_decision: 'decision', set_strategy: 'decision',
  research_completed: 'research', deep_research: 'research',
  report_generated: 'report',
  reward_hack_suspected: 'trust', data_leakage: 'trust', spec_drift: 'trust', novelty_rejected: 'trust',
  hint: 'control', pause: 'control', resume: 'control', run_abort: 'control', node_abort: 'control',
  fork: 'control', promote: 'control', annotation: 'control', inject_node: 'control', force_confirm: 'control',
  force_ablate: 'control', approval_requested: 'control', approval_granted: 'control', budget_extend: 'control',
  run_reopened: 'control', spec_approved: 'control', spec_approval_requested: 'control', spec_proposed: 'control',
  run_started: 'lifecycle', run_finished: 'lifecycle', llm_cost: 'lifecycle',
  data_profiled: 'lifecycle', data_provenance: 'lifecycle', host_grading: 'lifecycle', diversity_archive: 'lifecycle',
}
const ICON = {
  node_evaluated: '📊', node_failed: '✗', node_repaired: '🔧', node_confirmed: '✓', best_confirmed: '🏆',
  reward_hack_suspected: '⚠', data_leakage: '⚠', research_completed: '🔬', report_generated: '📋',
  run_started: '▶', run_finished: '■', llm_cost: '💲', agent_decision: '🤖',
}
function kindOf(type) {
  const g = TYPE2GROUP[type] || 'lifecycle'
  return { group: g, icon: ICON[type] || (GROUPS.find(x => x[0] === g) || [, '·'])[1] }
}

// Events whose row expands to a "why" detail card (reasoning, considered alternatives, context).
const REASONING_TYPES = new Set(['node_created', 'policy_decision', 'strategy_decision', 'research_completed'])

// Pull the model's raw <think> chain-of-thought for a node out of the trace view (spans.jsonl) — so
// the feed can surface "what was the Researcher thinking" inline. Returns [{op, text}] for the node.
function collectThinking(trace, nid) {
  if (nid == null) return []
  const spans = (trace?.nodes || {})[String(nid)] || []
  const out = []
  const walk = (arr) => (arr || []).forEach(s => {
    (s.events || []).forEach(ev => { if (ev.name === 'llm_call' && ev.thinking) out.push({ op: s.name, text: ev.thinking }) })
    walk(s.children)
  })
  walk(spans)
  return out
}

// A short, honest "what's the agent doing now" line, derived purely from the live state + the latest
// event (no backend signal needed). Drives the animated status strip at the foot of the feed.
function agentStatus(live, log) {
  if (!live || live.finished) return null
  if (live.paused) return 'Paused'
  const phase = live.phase
  if (phase === 'grounding' || phase === 'onboarding') return 'Setting up the task & data…'
  if (phase === 'approval') return 'Waiting for your approval…'
  if (phase === 'spec_approval') return 'Waiting to ratify the eval spec…'
  const wid = workingId(live)                                   // a node is pending → it's evaluating
  if (wid != null) return `Running experiment #${wid}…`
  const last = log.length ? log[log.length - 1].type : null     // between nodes → infer from last event
  if (last === 'strategy_decision' || last === 'set_strategy') return 'Choosing a strategy…'
  if (last === 'policy_decision' || last === 'agent_decision') return 'Planning the next experiment…'
  if (last === 'research_completed' || last === 'deep_research') return 'Reading the literature…'
  if (last === 'node_created') return 'Writing & running the experiment…'
  return 'Thinking about the next step…'
}

const Disclosure = ({ label, children }) => {
  const [open, setOpen] = useState(false)
  return <div className="think-debug" style={{ marginTop: 6 }}>
    <div className="role-think" onClick={() => setOpen(v => !v)}
         style={{ cursor: 'pointer', fontSize: 11, textTransform: 'uppercase', letterSpacing: '.5px' }}>
      {open ? '▾' : '▸'} {label}</div>
    {open && children}
  </div>
}

function NodeCreatedDetail({ d, trace }) {
  const idea = d.idea || {}
  const think = collectThinking(trace, d.node_id)
  const params = idea.params || {}
  const space = idea.space || {}
  return (
    <div className="ev-detail">
      <div className="section-h">Conclusion — why this experiment next</div>
      <div className="v">{idea.rationale || '—'}</div>
      <div className="ev-meta">
        <span>operator <b>{idea.operator || d.operator}</b></span>
        {(d.parent_ids || []).length > 0 && <span>built from {d.parent_ids.map(p => '#' + p).join(', ')}</span>}
        {Object.keys(params).length > 0 && <span>params {Object.entries(params).map(([k, v]) => `${k}=${fmt(v, 3)}`).join(', ')}</span>}
        {Object.keys(space).length > 0 && <span>sweep {Object.entries(space).map(([k, v]) => `${k}∈[${(v || []).join(', ')}]`).join('; ')}</span>}
      </div>
      {think.length > 0 && <Disclosure label="🧠 Researcher thinking (debug)">
        {think.map((t, i) => <Markdown key={i} className="think-body" text={t.text} />)}
      </Disclosure>}
    </div>
  )
}

function PolicyDetail({ d }) {
  const scores = d.scores || {}
  const entries = Object.entries(scores).sort((a, b) => b[1] - a[1])
  return (
    <div className="ev-detail">
      <div className="section-h">Why this node{d.reason ? ` — ${d.reason}` : ''}</div>
      {entries.length === 0
        ? <div className="v muted">chose #{d.chosen} (no candidate scores recorded)</div>
        : <table className="tbl"><thead><tr><th>node</th><th>score</th></tr></thead>
            <tbody>{entries.map(([nid, sc]) =>
              <tr key={nid} className={String(nid) === String(d.chosen) ? 'chosen-row' : ''}>
                <td>#{nid}{String(nid) === String(d.chosen) ? ' ✓ chosen' : ''}</td><td>{fmt(sc, 4)}</td></tr>)}
            </tbody></table>}
    </div>
  )
}

function StrategyDetail({ d }) {
  const s = d.strategy || {}
  const ctx = d.ctx || {}
  const ctxRows = Object.entries(ctx).filter(([, v]) => v != null && typeof v !== 'object')
  return (
    <div className="ev-detail">
      <div className="section-h">Why this strategy</div>
      <div className="v">{s.rationale || '—'}</div>
      <div className="ev-meta">
        <span>policy <b>{s.policy || '?'}</b></span>
        {s.fidelity && <span>fidelity {s.fidelity}</span>}
        {s.developer && <span>developer {s.developer}</span>}
        {s.source && <span>source {s.source}</span>}
      </div>
      {ctxRows.length > 0 && <>
        <div className="section-h">Decision context</div>
        <div className="ev-meta">{ctxRows.map(([k, v]) =>
          <span key={k} className="ev-ctx"><b>{k}</b> {String(v)}</span>)}</div>
      </>}
    </div>
  )
}

function ResearchDetail({ d }) {
  const memo = d.memo || {}
  return (
    <div className="ev-detail">
      <div className="section-h">Conclusion</div>
      <div className="v">{memo.summary || '—'}</div>
      {(memo.findings || []).length > 0 && <>
        <div className="section-h">Findings</div>
        <ul className="bul">{memo.findings.map((f, i) => <li key={i}>{f}</li>)}</ul></>}
      {(memo.recommended_directions || []).length > 0 && <>
        <div className="section-h">Recommended directions (fed to the Researcher)</div>
        <ul className="bul">{memo.recommended_directions.map((x, i) => <li key={i}>{x}</li>)}</ul></>}
      {memo.reasoning && <Disclosure label="reasoning (debug)">
        <Markdown className="think-body" text={memo.reasoning} />
      </Disclosure>}
    </div>
  )
}

function reasoningDetail(e, trace) {
  const d = e.data || {}
  if (e.type === 'node_created') return <NodeCreatedDetail d={d} trace={trace} />
  if (e.type === 'policy_decision') return <PolicyDetail d={d} />
  if (e.type === 'strategy_decision') return <StrategyDetail d={d} />
  if (e.type === 'research_completed') return <ResearchDetail d={d} />
  return null
}

// The full, UNtruncated text behind a feed row whose one-line narration clamped it (node_failed's
// triage, node_repaired's rationale, the report headline, a hint, …) — so every message is fully
// readable on expand even when it has no dedicated reasoning card. Returns [] when there's nothing.
function genericRows(e) {
  const d = e.data || {}
  const rows = []
  const add = (label, v) => { if (v != null && String(v).trim()) rows.push([label, String(v)]) }
  add('rationale', d.rationale)
  add('triage', d.triage_rationale)
  add('reason', d.reason)
  add('error', d.error)
  add('headline', d.content?.headline)
  add('summary', d.memo?.summary || d.summary)
  add('hint', d.text)
  return rows
}

function GenericDetail({ e }) {
  const rows = genericRows(e)
  if (!rows.length) return <div className="ev-detail"><pre className="code" style={{ maxHeight: 220 }}>{JSON.stringify(e.data || {}, null, 2)}</pre></div>
  return <div className="ev-detail">{rows.map(([k, v], i) =>
    <React.Fragment key={i}><div className="section-h">{k}</div><div className="v">{v}</div></React.Fragment>)}</div>
}

// One feed row, chat-message styled: an icon/color by kind, the narration, an expandable "why" card.
// node_created auto-expands ONLY while its node is the live frontier (the Researcher is "thinking");
// once the node is evaluated the card collapses to its one-line key info.
function EventRow({ e, trace, onFocusEvent, autoOpen }) {
  const [open, setOpen] = useState(autoOpen)
  const touched = useRef(false)   // once the user toggles, stop auto-following the live frontier
  // collapse-when-done: follow autoOpen (expand while live, collapse when the node resolves) UNLESS
  // the user manually toggled this card — then their choice wins.
  useEffect(() => { if (!touched.current) setOpen(autoOpen) }, [autoOpen])
  const nid = eventNode(e)
  const hasReason = REASONING_TYPES.has(e.type)
  // langfuse-style: any event that resolves a node expands to that node's span trace inline (it
  // appears once spans land, so the toggle shows up for evaluated/failed/repaired nodes too).
  // Use the event's OWN node_id (not eventNode's parent_id fallback) so e.g. an `ablate` row — which
  // carries only parent_id — never renders the PARENT node's trace mislabeled as its own.
  const traceNid = e.data?.node_id
  const nodeSpans = traceNid != null ? (trace?.nodes || {})[String(traceNid)] : null
  const hasTrace = !!(nodeSpans && nodeSpans.length)
  // no-truncation: a row whose one-line narration clamped text (or used the raw JSON fallback) is
  // expandable to its FULL content even without a dedicated reasoning card.
  const isRawFallback = !hasReason && !NARR[e.type]
  const hasGeneric = !hasReason && (genericRows(e).length > 0 || isRawFallback)
  const expandable = hasReason || hasTrace || hasGeneric
  const { group, icon } = kindOf(e.type)
  const narr = (NARR[e.type] || ((d) => JSON.stringify(d).slice(0, 80)))(e.data)
  return (
    <div className={'feed-msg k-' + group}>
      <div className="fm-ic" title={group}>{icon}</div>
      <div className="fm-body">
        <div className="fm-line clickable" onClick={() => onFocusEvent(e)}
             title={nid != null ? `open node #${nid} @ seq ${e.seq}` : `jump to seq ${e.seq}`}>
          {expandable && <span className="fm-tw" onClick={(ev) => { ev.stopPropagation(); touched.current = true; setOpen(o => !o) }}>{open ? '▾' : '▸'}</span>}
          <span className="fm-narr">{narr}</span>
          {nid != null && <span className="ev-go">↗</span>}
        </div>
        {open && expandable && <div className="ev-detail-wrap">
          {hasReason && reasoningDetail(e, trace)}
          {hasGeneric && <GenericDetail e={e} />}
          {hasTrace && <NodeTrace spans={nodeSpans} />}
        </div>}
      </div>
    </div>
  )
}

// A proposed action awaiting confirmation — every chat action surfaces here before it executes.
function ActionRow({ m, idx, onResolve }) {
  const a = m.action
  return (
    <div className="feed-msg action">
      <div className="fm-ic">⚡</div>
      <div className="fm-body">
        <div className="pending-action">
          <div className="pa-label"><b>{a.label}</b></div>
          {a.rationale && <div className="muted pa-why">{a.rationale}</div>}
          {m.status === 'pending'
            ? <div className="toolbar" style={{ marginTop: 4 }}>
                <button className="btn sm primary" onClick={() => onResolve(idx, true, a)}>Confirm</button>
                <button className="btn sm ghost" onClick={() => onResolve(idx, false, a)}>Cancel</button>
              </div>
            : <span className={'pa-status ' + m.status}>{
                m.status === 'done' ? '✓ done' : m.status === 'running' ? '… running'
                  : m.status === 'failed' ? '✗ failed' : 'cancelled'}</span>}
        </div>
      </div>
    </div>
  )
}

function ChatRow({ m }) {
  const [open, setOpen] = useState(false)
  const tr = m.role === 'assistant' ? m.trace : null   // the LLM I/O behind this reply, if captured
  return (
    <div className={'feed-msg chat ' + m.role}>
      <div className="fm-ic">{m.role === 'user' ? '🧑' : '🤖'}</div>
      <div className="fm-body">
        {(m.ctx || []).length > 0 && <div className="ctx-row">{m.ctx.map(id => <span key={id} className="ctx-chip">#{id}</span>)}</div>}
        {m.role === 'user' ? <div className="chat-text">{m.content}</div> : <Markdown className="chat-text" text={m.content} />}
        {tr && <div className="chat-trace-tog" onClick={() => setOpen(o => !o)}>{open ? '▾' : '▸'} trace</div>}
        {tr && open && <div className="chat-trace">
          <LlmCall idx={0} call={{ op: 'chat', model: tr.model, tokens: tr.tokens, completion: tr.completion,
            prompt: [...(tr.system ? [{ role: 'system', content: tr.system }] : []),
                     ...(tr.user ? [{ role: 'user', content: tr.user }] : []),
                     ...(tr.messages || [])] }} />
        </div>}
      </div>
    </div>
  )
}

export default function Dock({ runId, live, liveSeq, viewSeq, setViewSeq, onFocus, collapsed, onToggleCollapse, height = 230, selectedId, onToast }) {
  const [log, setLog] = useState([])
  const [trace, setTrace] = useState(null)
  const [filter, setFilter] = useState('')
  const [kinds, setKinds] = useState(() => new Set())     // selected kind chips (empty = all)
  const [msgs, setMsgs] = useState([])                    // chat turns {role, content, ts, ctx?}
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [ctx, setCtx] = useState([])                      // node-id context chips for the next message
  const [dropActive, setDropActive] = useState(false)
  const endRef = useRef(null)
  useEffect(() => { get(`/api/runs/${runId}/log`).then(setLog).catch(() => {}) }, [runId, liveSeq])
  // The trace re-folds the whole spans.jsonl server-side, and it only backs the inline "thinking"
  // cards — so refetch when a NODE is added (or the run finishes), not on every SSE seq tick.
  const nodeCount = live ? Object.keys(live.nodes || {}).length : 0
  // Refetch the trace when a node is ADDED or SETTLES (evaluate/repair spans land on a node-in-place,
  // which doesn't change nodeCount) — so a node's eval/repair waterfall appears on its feed rows
  // without waiting for the next node. Still not every SSE tick (idle ticks don't change either key).
  const settledCount = live ? Object.values(live.nodes || {}).filter(n => n.status === 'evaluated' || n.status === 'failed').length : 0
  useEffect(() => { get(`/api/runs/${runId}/trace`).then(setTrace).catch(() => {}) }, [runId, nodeCount, settledCount, live?.finished])
  const atLive = viewSeq == null || viewSeq >= liveSeq

  // The live frontier: the highest-id node still pending while the run runs — its proposal card stays
  // expanded ("thinking") until it resolves. null on a finished/replayed run.
  const livePendingId = useMemo(() => {
    if (!live || live.finished) return null
    const pend = Object.values(live.nodes || {}).filter(n => n.status === 'pending').map(n => n.id)
    return pend.length ? Math.max(...pend) : null
  }, [live])

  // Scrubber: the thumb tracks a LOCAL value (instant) while the history fetch (re-folds + re-lays the
  // DAG) is throttled to ~11fps. `drag == null` means "follow the committed seq".
  const [drag, setDrag] = useState(null)
  const thr = useRef({ last: 0, timer: null })
  const sliderVal = drag != null ? drag : (atLive ? liveSeq : viewSeq)
  const commit = (v) => setViewSeq(v >= liveSeq ? null : v)
  const onScrub = (v) => {
    setDrag(v)
    const now = Date.now(), st = thr.current
    if (now - st.last >= 90) { st.last = now; commit(v) }
    else { clearTimeout(st.timer); st.timer = setTimeout(() => { st.last = Date.now(); commit(v) }, 90) }
  }
  const endScrub = () => { clearTimeout(thr.current.timer); commit(sliderVal); setDrag(null) }

  const focusEvent = (e) => {
    const nid = eventNode(e)
    if (nid == null) { setViewSeq(e.seq); return }
    onFocus?.(Number(nid), e.type === 'node_created' ? 'Trace' : 'Overview', e.seq)
  }
  const toggleKind = (g) => setKinds(s => { const n = new Set(s); n.has(g) ? n.delete(g) : n.add(g); return n })
  const textMatch = (e) => {
    if (!filter) return true
    const q = filter.toLowerCase()
    const narr = (NARR[e.type] || (() => ''))(e.data)
    return e.type.toLowerCase().includes(q) || String(narr).toLowerCase().includes(q)
  }
  const kindMatch = (e) => kinds.size === 0 || kinds.has(TYPE2GROUP[e.type] || 'lifecycle')

  // The unified, chronological feed: events (filtered + time-scrubbed) interleaved with chat turns.
  const feed = useMemo(() => {
    const evItems = log
      .filter(e => (atLive || e.seq <= viewSeq) && kindMatch(e) && textMatch(e))
      .map(e => ({ t: 'ev', ts: e.ts || 0, seq: e.seq, e }))
    const msgItems = msgs.map((m, i) => ({ t: 'msg', ts: m.ts || 0, seq: 1e15 + i, m, i }))
    return [...evItems, ...msgItems].sort((a, b) => (a.ts - b.ts) || (a.seq - b.seq))
  }, [log, msgs, atLive, viewSeq, filter, kinds])

  useEffect(() => { if (atLive) endRef.current?.scrollIntoView({ block: 'end' }) }, [feed.length, busy, atLive])

  const pushAssistant = (content, trace = null) => setMsgs(m => [...m, { role: 'assistant', content, trace, ts: Date.now() / 1000 }])
  const pushAction = (action) => setMsgs(m => [...m, { role: 'action', action, status: 'pending', ts: Date.now() / 1000 }])
  // Free text -> the action-router proposes an action (confirm-first) or replies; soft-fails to chat.
  // A reply carries its `trace` (the LLM I/O) so the chat row can expand into a langfuse-style card.
  const runCommand = async (instruction, nid, history) => {
    const r = await command(runId, { messages: history, node_id: nid, instruction })
    if (r.ok && r.action) pushAction(r.action)
    else if (r.ok && r.reply) pushAssistant(r.reply, r.trace)
    else {
      // Soft-fail to advisory chat. `history` is the PRIOR turns; the current instruction must be
      // appended or the model answers the previous message (it's not in `history` yet).
      const c = await chat(runId, [...history, { role: 'user', content: instruction }], nid)
      pushAssistant(c.ok ? c.text : `⚠ ${c.error || 'no model reachable'}`, c.ok ? c.trace : null)
    }
  }
  const send = async () => {
    const text = input.trim()
    if (!text || busy) return
    const nid = ctx.length ? ctx[ctx.length - 1] : (selectedId ?? null)
    const history = msgs.filter(m => m.role === 'user' || m.role === 'assistant').map(m => ({ role: m.role, content: m.content }))
    setMsgs(m => [...m, { role: 'user', content: text, ts: Date.now() / 1000, ctx: [...ctx] }])
    setInput(''); setCtx([]); setBusy(true)
    try {
      if (text.startsWith('/')) {
        const p = parseSlash(text, nid, live || {})
        if (p?.help) pushAssistant(HELP)
        else if (p?.error) pushAssistant('⚠ ' + p.error)
        else if (p?.action) pushAction(p.action)
        else if (p?.llm) await runCommand(p.llm, nid, history)
        else pushAssistant('⚠ unrecognized — try /help')
      } else {
        await runCommand(text, nid, history)
      }
    } catch (e) { pushAssistant('⚠ ' + e.message) }
    setBusy(false)
  }
  // Confirm/cancel a proposed action. Confirm funnels through applyAction (reopens finished runs).
  const resolveAction = async (idx, ok, action) => {
    setMsgs(m => m.map((x, i) => i === idx ? { ...x, status: ok ? 'running' : 'cancelled' } : x))
    if (!ok) return
    try {
      await applyAction(runId, action, live?.finished)
      onToast?.('done: ' + action.label)
      setMsgs(m => m.map((x, i) => i === idx ? { ...x, status: 'done' } : x))
    } catch (e) {
      onToast?.('failed: ' + e.message)
      setMsgs(m => m.map((x, i) => i === idx ? { ...x, status: 'failed' } : x))
    }
  }
  const addCtx = (id) => { if (id != null) setCtx(c => (c.includes(id) ? c : [...c, id])) }
  const onDrop = (ev) => {
    ev.preventDefault(); setDropActive(false)
    const raw = ev.dataTransfer.getData('application/looplab-node') || ev.dataTransfer.getData('text/plain')
    const id = Number(String(raw).replace('#', ''))
    if (!Number.isNaN(id)) addCtx(id)
  }

  return (
    <div className="dock chat-dock">
      <div className="dock-tabs">
        <span className="chat-label">💬 chat & timeline</span>
        <div className="scrubber inline">
          <button className="btn sm" onClick={() => { setViewSeq(null); setDrag(null) }} disabled={atLive && drag == null}>⏵ Live</button>
          <input type="range" min={0} max={Math.max(0, liveSeq)} value={sliderVal}
                 onChange={e => onScrub(Number(e.target.value))}
                 onPointerUp={endScrub} onMouseUp={endScrub} onKeyUp={endScrub} onBlur={endScrub} />
          <span className={(sliderVal >= liveSeq) ? 'live-tag' : 'hist-tag'}>{(sliderVal >= liveSeq) ? `live · ${liveSeq}` : `replay · ${sliderVal}/${liveSeq}`}</span>
        </div>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn sm ghost dock-collapse" title={collapsed ? 'expand' : 'collapse'}
                onClick={onToggleCollapse}>{collapsed ? '▴' : '▾'}</button>
      </div>
      {!collapsed && <div className="dock-body chat-body" style={{ height }}>
        <div className="kind-chips">
          {GROUPS.map(([g, ic, label]) => <button key={g}
            className={'kind-chip k-' + g + (kinds.has(g) ? ' on' : '')} onClick={() => toggleKind(g)}>{ic} {label}</button>)}
          {kinds.size > 0 && <button className="kind-chip clear" onClick={() => setKinds(new Set())}>clear</button>}
          <input className="text feed-filter" placeholder="filter text…" value={filter} onChange={e => setFilter(e.target.value)} />
        </div>
        <div className="feed chat-feed">
          {feed.length === 0
            ? <div className="muted">{(filter || kinds.size) ? 'nothing matches the filter' : 'no events yet — say hello below'}</div>
            : feed.map(it => it.t === 'ev'
                ? <EventRow key={'e' + it.seq} e={it.e} trace={trace} onFocusEvent={focusEvent}
                    autoOpen={it.e.type === 'node_created' && eventNode(it.e) === livePendingId} />
                : it.m.role === 'action'
                  ? <ActionRow key={'a' + it.i} m={it.m} idx={it.i} onResolve={resolveAction} />
                  : <ChatRow key={'m' + it.i} m={it.m} />)}
          {(() => { const st = busy ? 'Working on your request…' : (atLive ? agentStatus(live, log) : null)
            return st ? <div className="agent-status"><span className="as-dot" />{st}</div> : null })()}
          <div ref={endRef} />
        </div>
        <div className={'chat-in' + (dropActive ? ' drop' : '')}
             onDragOver={e => { e.preventDefault(); setDropActive(true) }}
             onDragLeave={() => setDropActive(false)} onDrop={onDrop}>
          {(ctx.length > 0 || selectedId != null) && <div className="ctx-row">
            {ctx.map(id => <span key={id} className="ctx-chip">#{id}<button onClick={() => setCtx(c => c.filter(x => x !== id))}>✕</button></span>)}
            {selectedId != null && !ctx.includes(selectedId) && <button className="ctx-chip add" onClick={() => addCtx(selectedId)}>＋ #{selectedId}</button>}
          </div>}
          <textarea className="text" rows={2} placeholder="Ask, run a /command, or just say what to do (I’ll propose an action)… select a node to add it as ＋context · Enter to send"
            value={input} onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} />
          <div className="toolbar" style={{ marginTop: 4 }}>
            <button className="btn sm primary" disabled={busy || !input.trim()} onClick={send}>Send</button>
            <button className="btn sm ghost" title="list commands" onClick={() => setMsgs(m => [...m, { role: 'assistant', content: HELP, ts: Date.now() / 1000 }])}>/help</button>
            <span className="muted" style={{ fontSize: 11 }}>{busy ? 'thinking…' : 'every action is confirmed before it runs'}</span>
          </div>
        </div>
      </div>}
    </div>
  )
}
