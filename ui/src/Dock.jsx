import React, { useEffect, useMemo, useRef, useState } from 'react'
import { get, fmt, chat, command, applyAction, workingId, CONTROL, resumeRun, resetRun } from './util.js'
import Markdown from './markdown.jsx'
import { NodeTrace, LlmCall } from './Inspector.jsx'
import { OpIcon } from './icons.jsx'

const HELP = 'Commands: /confirm #n · /ablate #n · /fork #n · /promote #n · /note #n text · /hint text · /strategy policy=ucb fidelity=low · /deep-research · /experiment <idea> · /approve #n · /ratify · /pause · /resume · /stop · /refresh. Or just say what you want and I’ll do it right away (boss mode — actions apply immediately; reversible ones offer Undo).'

// Slash-command catalogue powering the type-ahead hints above the input. `desc` is the one-line label
// in the list; `why` is the "what this does / when to reach for it" shown on hover (and in the detail
// strip). Order = roughly most-reached-for first. Kept in sync with parseSlash() below.
const COMMANDS = [
  { cmd: 'confirm', args: '#n', desc: 'multi-seed re-eval of a node',
    why: 'Re-runs a node across several seeds and ranks by the robust mean — confirms a score is real, not luck from one good seed.' },
  { cmd: 'ablate', args: '#n', desc: 'sensitivity / ablation probe',
    why: 'Turns parts of a node’s idea off one at a time to see which actually move the metric — separates what matters from dead weight.' },
  { cmd: 'fork', args: '#n', desc: 'branch a fresh improve from a node',
    why: 'Starts a new improve-branch off this (known-good) node so the search explores a different direction from a solid base.' },
  { cmd: 'promote', args: '#n', desc: 'pin a node as champion',
    why: 'Marks this node as the current best/champion the report and final answer build on.' },
  { cmd: 'note', args: '#n text', desc: 'annotate a node',
    why: 'Attaches a human note to a node — kept in the audit trail and visible to the agent as context.' },
  { cmd: 'hint', args: 'text', desc: 'steer the agent (soft)',
    why: 'Drops a free-text directive into the agent’s context to bias what it tries next, without forcing a specific action.' },
  { cmd: 'strategy', args: 'policy=ucb fidelity=low', desc: 'switch search strategy',
    why: 'Changes the search policy/fidelity live — e.g. explore harder (ucb) or run cheaper low-fidelity evals to cover more ground.' },
  { cmd: 'deep-research', args: '', desc: 'run a deep-research step now',
    why: 'Fires the arXiv + web research stage immediately to pull outside ideas in before the next round of proposals.' },
  { cmd: 'experiment', args: '<idea>', desc: 'propose & add a node',
    why: 'Hands your idea to the agent to turn into the next experiment node (LLM-authored), instead of you specifying an exact action.' },
  { cmd: 'approve', args: '#n', desc: 'approve a gated node (HITL)',
    why: 'Grants human approval at an approval checkpoint so the run can proceed past it.' },
  { cmd: 'ratify', args: '', desc: 'ratify the eval spec',
    why: 'Approves the evaluation spec so the run can start/continue scoring under it.' },
  { cmd: 'pause', args: '', desc: 'pause the run (resumable)',
    why: 'Cleanly stops taking new work between nodes while keeping all state — resume picks up exactly where it left off.' },
  { cmd: 'resume', args: '', desc: 'resume a paused run',
    why: 'Continues a paused run from where it stopped.' },
  { cmd: 'stop', args: '', desc: 'stop / abort the run',
    why: 'Aborts and finalizes the run. Not resumable in place — use Replay to start over from scratch.' },
  { cmd: 'refresh', args: '', desc: 'rebuild the run report',
    why: 'Regenerates the agent-authored report from the latest events.' },
  { cmd: 'help', args: '', desc: 'list all commands',
    why: 'Prints the full command reference into the chat.' },
]

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
// [key, label] — the icon is a monochrome glyph (GROUP_GLYPH), not an emoji (round-7 readability pass).
const GROUPS = [
  ['proposal', 'proposals'],
  ['eval', 'results'],
  ['decision', 'decisions'],
  ['research', 'research'],
  ['report', 'report'],
  ['trust', 'trust'],
  ['control', 'actions'],
  ['lifecycle', 'lifecycle'],
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
// Monochrome glyph per kind (default) + a few high-signal per-type overrides. Names resolve in
// icons.jsx GLYPHS (unknown → 'dot' fallback). Color is carried by CSS (only user/trust/highlighted).
const GROUP_GLYPH = {
  proposal: 'trending', eval: 'target', decision: 'gitbranch', research: 'search',
  report: 'doc', trust: 'alert', control: 'gear', lifecycle: 'flag',
}
const TYPE_GLYPH = {
  node_failed: 'bug', node_repaired: 'gear', node_confirmed: 'target', best_confirmed: 'star',
  run_started: 'play', run_finished: 'stop', report_generated: 'doc', research_completed: 'search',
  deep_research: 'search', agent_decision: 'bot',
}
function kindOf(type) {
  const g = TYPE2GROUP[type] || 'lifecycle'
  return { group: g, glyph: TYPE_GLYPH[type] || GROUP_GLYPH[g] || 'dot' }
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
  const { group, glyph } = kindOf(e.type)
  const narr = (NARR[e.type] || ((d) => JSON.stringify(d).slice(0, 80)))(e.data)
  return (
    <div className={'feed-msg k-' + group}>
      <div className="fm-ic" title={group}><OpIcon name={glyph} size={14} className="fm-ic-svg" /></div>
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

// An action the boss-handler APPLIED (round-7: no confirm — it acts immediately). The row is the audit
// record of what it did; reversible verbs (pause⇄resume) offer one-click Undo. Critical verbs
// (abort) are highlighted so they stay visible in the feed.
function AppliedRow({ m, onUndo }) {
  const a = m.action
  return (
    <div className={'feed-msg action' + (m.highlight ? ' hl' : '')}>
      <div className="fm-ic"><OpIcon name="bolt" size={14} className="fm-ic-svg" /></div>
      <div className="fm-body">
        <div className="fm-line">
          <b className="pa-label">{a.label}</b>
          {m.highlight && <OpIcon name="star" size={12} className="fm-star" />}
          <span className="spacer" style={{ flex: 1 }} />
          <span className={'pa-status ' + m.status}>{
            m.status === 'running' ? '… applying' : m.status === 'done' ? '✓ applied'
              : m.status === 'failed' ? '✗ ' + (m.err || 'failed') : ''}</span>
        </div>
        {a.rationale && <div className="muted pa-why">{a.rationale}</div>}
        {m.status === 'done' && m.undo &&
          <button className="btn sm ghost pa-undo" onClick={() => onUndo(m)}>Undo ({m.undo.label})</button>}
      </div>
    </div>
  )
}

// A chat turn. The human's own messages sit RIGHT, in a filled accent bubble (so they never visually
// merge with the left-aligned event/agent stream); the agent's replies sit LEFT in a neutral bubble,
// and a long reply (the "wall of text") collapses to a clamp with a show-more toggle.
function ChatRow({ m }) {
  const [open, setOpen] = useState(false)
  const isUser = m.role === 'user'
  const tr = !isUser ? m.trace : null                  // the LLM I/O behind this reply, if captured
  const long = !isUser && (m.content || '').length > 600
  const [expanded, setExpanded] = useState(false)
  const icon = <div className="fm-ic"><OpIcon name={isUser ? 'user' : 'bot'} size={14} className="fm-ic-svg" /></div>
  return (
    <div className={'feed-msg chat ' + m.role}>
      {icon}
      <div className="fm-body">
        {(m.ctx || []).length > 0 && <div className="ctx-row">{m.ctx.map(id => <span key={id} className="ctx-chip">#{id}</span>)}</div>}
        <div className="chat-who">{isUser ? 'you' : 'agent'}</div>
        <div className={'chat-bubble' + (long && !expanded ? ' clamp' : '')}>
          {isUser ? <div className="chat-text">{m.content}</div> : <Markdown className="chat-text" text={m.content} />}
        </div>
        {long && <div className="chat-more" onClick={() => setExpanded(e => !e)}>
          {expanded ? '▾ show less' : '▸ show full reply'}</div>}
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
  const [sugIdx, setSugIdx] = useState(0)                 // highlighted command suggestion
  const [sugOpen, setSugOpen] = useState(true)            // false only after an explicit Esc (reopens on edit)
  const taRef = useRef(null)
  const feedRef = useRef(null)
  const msgCtr = useRef(0)
  // round-7: scrubber + filter chips collapse into one block to save space; default hidden, remembered.
  const [showControls, setShowControls] = useState(() => localStorage.getItem('ll.dock.controls') === '1')
  const toggleControls = () => setShowControls(v => { const n = !v; localStorage.setItem('ll.dock.controls', n ? '1' : '0'); return n })
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

  // round-7: anchor each chat turn to the live tail so a just-sent message can't sort into the past
  // (the old "overlaps previous messages" bug). ts ≥ newest known event; seq grows with liveSeq + order.
  const tailTs = () => Math.max(Date.now() / 1000, log.length ? (log[log.length - 1].ts || 0) : 0) + 1e-3
  const chatSeq = () => 1e15 + liveSeq * 1e6 + (msgCtr.current++)

  // The unified, chronological feed: events (filtered + time-scrubbed) interleaved with chat turns.
  const feed = useMemo(() => {
    const evItems = log
      .filter(e => (atLive || e.seq <= viewSeq) && kindMatch(e) && textMatch(e))
      .map(e => ({ t: 'ev', ts: e.ts || 0, seq: e.seq, e }))
    const msgItems = msgs.map((m, i) => ({ t: 'msg', ts: m.ts || 0, seq: m.seq ?? (1e15 + i), m, i }))
    return [...evItems, ...msgItems].sort((a, b) => (a.ts - b.ts) || (a.seq - b.seq))
  }, [log, msgs, atLive, viewSeq, filter, kinds])

  // Scroll the CONTAINER after paint — tall Markdown replies lay out late, so scrollIntoView fired
  // mid-layout would land short and leave a new turn visually colliding with the row above it.
  useEffect(() => {
    if (!atLive) return
    const el = feedRef.current
    if (el) requestAnimationFrame(() => { el.scrollTop = el.scrollHeight })
  }, [feed.length, busy, atLive])

  const pushAssistant = (content, trace = null) =>
    setMsgs(m => [...m, { role: 'assistant', content, trace, ts: tailTs(), seq: chatSeq() }])
  // round-7 boss mode: the action is APPLIED immediately (no confirm card). Reversible verbs
  // (pause⇄resume) carry an Undo; critical verbs (abort) are highlighted so they stay visible.
  const isCritical = (a) => a.type === 'run_abort' || a.type === 'node_abort'
  const undoFor = (a) =>
    a.type === 'pause' ? { label: 'resume', type: 'resume', data: {} }
      : a.type === 'resume' ? { label: 'pause', type: 'pause', data: {} } : null
  const autoApply = async (action, highlight = false) => {
    const seq = chatSeq(), id = 'a' + seq
    setMsgs(m => [...m, {
      role: 'action', action, status: 'running', id, ts: tailTs(), seq,
      highlight: highlight || isCritical(action), undo: undoFor(action),
    }])
    try {
      await applyAction(runId, action, live?.finished)   // util.applyAction: reopens a finished run
      setMsgs(m => m.map(x => x.id === id ? { ...x, status: 'done' } : x))
      onToast?.('applied: ' + action.label)
    } catch (e) {
      setMsgs(m => m.map(x => x.id === id ? { ...x, status: 'failed', err: e.message } : x))
      onToast?.('failed: ' + e.message)
    }
  }
  const onUndo = async (m) => {
    if (!m.undo) return
    try { await CONTROL.raw(runId, m.undo.type, m.undo.data); onToast?.('undid: ' + m.action.label) }
    catch (e) { onToast?.('undo failed: ' + e.message) }
  }
  // Free text -> the boss applies an action immediately, or replies; soft-fails to advisory chat.
  // A reply carries its `trace` (the LLM I/O) so the chat row can expand into a langfuse-style card.
  const runCommand = async (instruction, nid, history) => {
    const r = await command(runId, { messages: history, node_id: nid, instruction })
    if (r.ok && r.action) await autoApply(r.action)
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
    setMsgs(m => [...m, { role: 'user', content: text, ts: tailTs(), seq: chatSeq(), ctx: [...ctx] }])
    setInput(''); setCtx([]); setBusy(true)   // the feed.length effect scrolls the new turn into view
    try {
      if (text.startsWith('/')) {
        const p = parseSlash(text, nid, live || {})
        if (p?.help) pushAssistant(HELP)
        else if (p?.error) pushAssistant('⚠ ' + p.error)
        else if (p?.action) await autoApply(p.action)
        else if (p?.llm) await runCommand(p.llm, nid, history)
        else pushAssistant('⚠ unrecognized — try /help')
      } else {
        await runCommand(text, nid, history)
      }
    } catch (e) { pushAssistant('⚠ ' + e.message) }
    setBusy(false)
  }
  // round-7 transport: mode derived from live state; wired to existing control endpoints + reset.
  const paused = !!live?.paused, finished = !!live?.finished
  const mode = finished ? 'finished' : paused ? 'paused' : 'running'
  const onPause = () => CONTROL.pause(runId).then(() => onToast?.('paused')).catch(e => onToast?.('pause failed: ' + e.message))
  const onResume = () => CONTROL.resume(runId).then(() => onToast?.('resumed')).catch(e => onToast?.('resume failed: ' + e.message))
  const onStop = () => CONTROL.abort(runId).then(() => onToast?.('stop requested')).catch(e => onToast?.('stop failed: ' + e.message))
  const onReopen = async () => { try { await CONTROL.reopen(runId); await resumeRun(runId); onToast?.('reopened & resumed') } catch (e) { onToast?.('reopen failed: ' + e.message) } }
  const onReplay = async () => {
    if (!window.confirm('Reset this run? Wipes all events & nodes and restarts from scratch.')) return
    try { await resetRun(runId); onToast?.('replaying from scratch') } catch (e) { onToast?.('reset failed: ' + e.message) }
  }
  // Command type-ahead: a `/word` with no space yet → matching commands (prefix first, then substring).
  // Once a space is typed (entering args) or the text isn't a bare slash-word, the list disappears.
  const sugTok = input.match(/^\/(\S*)$/)
  const suggestions = useMemo(() => {
    if (!sugTok) return []
    const q = sugTok[1].toLowerCase()
    const pre = COMMANDS.filter(c => c.cmd.startsWith(q))
    return q ? [...pre, ...COMMANDS.filter(c => !c.cmd.startsWith(q) && c.cmd.includes(q))] : COMMANDS
  }, [sugTok ? sugTok[1] : null])
  const showSuggest = sugOpen && suggestions.length > 0
  const sugSel = Math.min(sugIdx, Math.max(0, suggestions.length - 1))
  const acceptSuggestion = (c) => {
    if (!c) return
    setInput('/' + c.cmd + ' ')          // trailing space → list closes, caret ready for args
    setSugIdx(0); setSugOpen(true)
    requestAnimationFrame(() => taRef.current?.focus())
  }
  const onInputKey = (e) => {
    if (showSuggest) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setSugIdx(i => Math.min(i + 1, suggestions.length - 1)); return }
      if (e.key === 'ArrowUp') { e.preventDefault(); setSugIdx(i => Math.max(i - 1, 0)); return }
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) { e.preventDefault(); acceptSuggestion(suggestions[sugSel]); return }
      if (e.key === 'Escape') { e.preventDefault(); setSugOpen(false); return }
    }
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
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
        <span className="chat-label"><OpIcon name="chat" size={14} /> chat &amp; timeline</span>
        {/* clickable so the user can return to live even when the controls (with the Live button) are hidden */}
        <span className={'hist-tag-mini ' + (atLive ? 'live' : 'hist')}
              onClick={() => { if (!atLive) { setViewSeq(null); setDrag(null) } }}
              style={atLive ? undefined : { cursor: 'pointer' }}
              title={atLive ? '' : 'click to return to live'}>
          {atLive ? `live · ${liveSeq}` : `replay ${sliderVal}/${liveSeq} → live`}</span>
        {/* a left-over filter is invisible once controls collapse — surface it so the feed never looks empty for no reason */}
        {!showControls && (filter || kinds.size > 0) &&
          <span className="hist-tag-mini hist" style={{ cursor: 'pointer' }}
                title="a filter is active — open controls to change it" onClick={toggleControls}>⌕ filtered</span>}
        <span className="spacer" style={{ flex: 1 }} />
        <button className={'btn sm ghost' + (showControls ? ' on' : '')} title="time-travel & filters"
                onClick={toggleControls}><OpIcon name="sliders" size={13} /> controls</button>
        <button className="btn sm ghost dock-collapse" title={collapsed ? 'expand' : 'collapse'}
                onClick={onToggleCollapse}><OpIcon name={collapsed ? 'chevron-up' : 'chevron-down'} size={13} /></button>
      </div>
      {!collapsed && <div className="dock-body chat-body" style={{ height }}>
        {showControls && <div className="dock-controls">
          <div className="scrubber inline">
            <button className="btn sm" onClick={() => { setViewSeq(null); setDrag(null) }} disabled={atLive && drag == null}><OpIcon name="play" size={11} /> Live</button>
            <input type="range" min={0} max={Math.max(0, liveSeq)} value={sliderVal}
                   onChange={e => onScrub(Number(e.target.value))}
                   onPointerUp={endScrub} onMouseUp={endScrub} onKeyUp={endScrub} onBlur={endScrub} />
            <span className={(sliderVal >= liveSeq) ? 'live-tag' : 'hist-tag'}>{(sliderVal >= liveSeq) ? `live · ${liveSeq}` : `replay · ${sliderVal}/${liveSeq}`}</span>
          </div>
          <div className="kind-chips">
            {GROUPS.map(([g, label]) => <button key={g}
              className={'kind-chip k-' + g + (kinds.has(g) ? ' on' : '')} onClick={() => toggleKind(g)}>
              <OpIcon name={GROUP_GLYPH[g]} size={12} /> {label}</button>)}
            {kinds.size > 0 && <button className="kind-chip clear" onClick={() => setKinds(new Set())}>clear</button>}
            <input className="text feed-filter" placeholder="filter text…" value={filter} onChange={e => setFilter(e.target.value)} />
          </div>
        </div>}
        <div className="feed chat-feed" ref={feedRef}>
          {feed.length === 0
            ? <div className="muted">{(filter || kinds.size) ? 'nothing matches the filter' : 'no events yet — say hello below'}</div>
            : feed.map(it => it.t === 'ev'
                ? <EventRow key={'e' + it.seq} e={it.e} trace={trace} onFocusEvent={focusEvent}
                    autoOpen={it.e.type === 'node_created' && eventNode(it.e) === livePendingId} />
                : it.m.role === 'action'
                  ? <AppliedRow key={it.m.id || ('a' + it.i)} m={it.m} onUndo={onUndo} />
                  : <ChatRow key={'m' + it.i} m={it.m} />)}
          {(() => {
            // Concurrent transparency: the pipeline keeps working (research/eval) WHILE the boss
            // replies to your message — show both at once, never one masking the other.
            const pipeline = atLive ? agentStatus(live, log) : null
            if (!pipeline && !busy) return null
            return <div className="agent-status">
              <span className="as-dot" />
              {pipeline && <span className="as-seg">{pipeline}</span>}
              {pipeline && busy && <span className="as-bullet">·</span>}
              {busy && <span className="as-seg as-reply"><OpIcon name="chat" size={11} /> replying to your message…</span>}
            </div>
          })()}
        </div>
        <div className={'chat-in' + (dropActive ? ' drop' : '')}
             onDragOver={e => { e.preventDefault(); setDropActive(true) }}
             onDragLeave={() => setDropActive(false)} onDrop={onDrop}>
          {showSuggest && <div className="cmd-suggest" role="listbox" aria-label="commands">
            <div className="cmd-list">
              {suggestions.map((c, i) => <div key={c.cmd} role="option" aria-selected={i === sugSel}
                  className={'cmd-item' + (i === sugSel ? ' on' : '')} title={c.why}
                  onMouseEnter={() => setSugIdx(i)}
                  onMouseDown={e => { e.preventDefault(); acceptSuggestion(c) }}>
                <span className="cmd-name">/{c.cmd}{c.args && <span className="cmd-args"> {c.args}</span>}</span>
                <span className="cmd-desc">{c.desc}</span>
              </div>)}
            </div>
            <div className="cmd-why">{suggestions[sugSel]?.why}
              <span className="cmd-keys">↑↓ choose · Tab insert · Esc close</span></div>
          </div>}
          {(ctx.length > 0 || selectedId != null) && <div className="ctx-row">
            {ctx.map(id => <span key={id} className="ctx-chip">#{id}<button onClick={() => setCtx(c => c.filter(x => x !== id))}>✕</button></span>)}
            {selectedId != null && !ctx.includes(selectedId) && <button className="ctx-chip add" onClick={() => addCtx(selectedId)}>＋ #{selectedId}</button>}
          </div>}
          <textarea ref={taRef} className="text" rows={2} placeholder="Ask, run a /command (type “/” for hints), or just say what to do… select a node to add it as ＋context · Enter to send"
            value={input} onChange={e => { setInput(e.target.value); setSugIdx(0); setSugOpen(true) }}
            onKeyDown={onInputKey} />
          <div className="toolbar" style={{ marginTop: 4 }}>
            <button className="btn sm primary" disabled={busy || !input.trim()} onClick={send}>Send</button>
            <button className="btn sm ghost" title="list commands" onClick={() => pushAssistant(HELP)}>/help</button>
            <span className="muted" style={{ fontSize: 11 }}>{busy ? 'thinking…' : 'boss mode — actions apply immediately'}</span>
            <span className="spacer" style={{ flex: 1 }} />
            <div className="transport">
              {mode === 'running' && <>
                <button className="btn sm" title="pause the run" onClick={onPause}><OpIcon name="pause" size={13} /></button>
                <button className="btn sm danger" title="stop the run" onClick={onStop}><OpIcon name="stop" size={13} /></button></>}
              {mode === 'paused' && <>
                <button className="btn sm primary" title="resume the run" onClick={onResume}><OpIcon name="play" size={13} /></button>
                <button className="btn sm danger" title="stop the run" onClick={onStop}><OpIcon name="stop" size={13} /></button></>}
              {mode === 'finished' && <>
                <button className="btn sm primary" title="reopen & resume" onClick={onReopen}><OpIcon name="play" size={13} /></button>
                <button className="btn sm" title="reset & restart from scratch" onClick={onReplay}><OpIcon name="replay" size={13} /></button></>}
            </div>
          </div>
        </div>
      </div>}
    </div>
  )
}
