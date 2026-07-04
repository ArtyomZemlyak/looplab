import React, { useEffect, useMemo, useRef, useState } from 'react'
import { get, fmt, workingId, CONTROL, resumeRun, resetRun } from './util.js'
import Markdown from './markdown.jsx'
import { NodeTrace } from './Inspector.jsx'
import { OpIcon } from './icons.jsx'


// The run's EVENTS window (round-9): one scrubbable, filterable feed that renders every run event
// as a differentiated message. The per-run "boss" chat moved to the single persistent assistant, so
// there is no composer here — just the timeline, scrubber, filters, and transport.

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
  node_abort: (d) => `stop requested for #${d.node_id}`,
  budget_extend: (d) => {
    const bits = []
    if (d.add_nodes) bits.push(`+${d.add_nodes} experiment node${d.add_nodes === 1 ? '' : 's'}`)
    if (d.max_seconds != null) bits.push(`wall-clock ${d.max_seconds}s`)
    if (d.max_eval_seconds != null) bits.push(`per-eval ${d.max_eval_seconds}s`)
    return `run budget extended — ${bits.join(', ') || 'no change'}`
  },
  hint: (d) => `hint: ${d.text}`, promote: (d) => `promoted #${d.node_id} → ${d.alias || 'champion'}`,
  policy_decision: (d) => `chose #${d.chosen}${d.reason ? ' (' + d.reason + ')' : ''} over ${Object.keys(d.scores || {}).length} candidate(s)`,
  strategy_decision: (d) => `strategy → ${d.strategy?.policy || '?'}${d.strategy?.fidelity ? '/' + d.strategy.fidelity : ''}${d.strategy?.rationale ? ' — ' + d.strategy.rationale.slice(0, 70) : ''}`,
  rung_promoted: (d) => `ASHA rung ↑${d.rung}: promoted ${(d.survivors || []).map(s => '#' + s).join(', ')}`,
  set_strategy: (d) => `operator pinned strategy → ${d.strategy?.policy || ''}${d.strategy?.fidelity ? '/' + d.strategy.fidelity : ''}`,
  deep_research: () => 'deep research requested',
  research_completed: (d) => `deep research (${d.trigger || 'auto'})${d.memo?.summary ? ' — ' + String(d.memo.summary).slice(0, 80) : ''}`,
  report_generated: (d) => `run report updated${d.content?.headline ? ' — ' + String(d.content.headline).slice(0, 90) : ''}`,
  reflection_note: (d) => `run reflection → memory: ${d.n_lessons || 0} lesson${(d.n_lessons || 0) === 1 ? '' : 's'}${d.n_skills ? `, ${d.n_skills} auto-skill${d.n_skills === 1 ? '' : 's'}` : ''}${d.note ? ' — ' + String(d.note).slice(0, 80) : ''}`,
  proxy_scored: (d) => `proxy scored #${d.node_id}: ${fmt(d.score)}${d.skipped ? ' (skipped full eval)' : ''}`,
  reward_hack_suspected: (d) => `reward-hack suspected on #${d.node_id}: ${(d.signals || []).map(s => s.signal).join(', ')}`,
  novelty_rejected: (d) => `dedup: proposal near #${d.near_node} (dist ${fmt(d.distance, 3)}) nudged to diversify`,
  hypothesis_ranked: (d) => `foresight ranked ${d.n || (d.order || []).length} open hypotheses by predicted payoff${d.confidence != null ? ` (${Math.round(d.confidence * 100)}% conf)` : ''}${d.reason ? ' — ' + String(d.reason).slice(0, 70) : ''}`,
  foresight_selected: (d) => `foresight picked ${d.kind === 'solution' ? 'implementation' : 'idea'} ${(d.chosen ?? 0) + 1} of ${d.n || (d.order || []).length}${d.confidence != null ? ` (${Math.round(d.confidence * 100)}% conf)` : ''}${d.reason ? ' — ' + String(d.reason).slice(0, 70) : ''}`,
  run_finished: (d) => `run finished${d.reason ? ' (' + d.reason + ')' : ''}`,
  llm_cost: (d) => `LLM: ${d.total_tokens} tokens, $${fmt(d.cost)}`,
  // --- operator/boss control INTENTS + their engine confirmations. Every event the agentic boss can
  // produce gets a plain-English line here, so an action never shows in the feed as a raw-JSON blob. ---
  force_confirm: (d) => `requested a multi-seed confirm of #${d.node_id}`,
  force_ablate: (d) => `requested an ablation probe on #${d.node_id}`,
  fork: (d) => `forked a fresh improve-branch from #${d.from_node_id}`,
  inject_node: (d) => { const i = d.idea || {}; return `added experiment: ${i.operator || 'improve'}${d.parent_id != null ? ' from #' + d.parent_id : ''}${i.rationale ? ' — ' + String(i.rationale).slice(0, 70) : ''}` },
  annotation: (d) => `note on #${d.node_id}: ${String(d.text || '').slice(0, 80)}`,
  run_reopened: () => 'run reopened to keep going',
  fork_done: () => 'fork fulfilled — branch added',
  inject_done: () => 'experiment injected into the tree',
  confirm_done: (d) => `multi-seed confirm finished for #${d.node_id}`,
  confirm_eval: (d) => `confirm seed ${d.seed} on #${d.node_id} → ${fmt(d.metric)}`,
  agent_decision: (d) => `agent chose ${d.chosen?.kind || '?'}${d.chosen?.node_id != null ? ' → #' + d.chosen.node_id : ''} (of ${(d.legal || []).length} legal move${(d.legal || []).length === 1 ? '' : 's'})${d.rationale ? ' — ' + String(d.rationale).slice(0, 70) : ''}`,
  agent_validated: (d) => `developer validated #${d.node_id}${d.fell_back ? ' (fell back to a simpler build)' : d.ok === false ? ' (checks failed)' : ' ✓'}`,
  spec_proposed: () => 'eval spec proposed — awaiting ratification',
  spec_approval_requested: () => 'awaiting your approval of the eval spec',
  spec_approved: () => 'eval spec ratified',
  spec_drift: (d) => `spec drift on #${d.node_id}${d.seed != null ? ' (seed ' + d.seed + ')' : ''} — metric discarded`,
  drift_unavailable: (d) => `drift check unavailable${d.reason ? ' — ' + String(d.reason).slice(0, 80) : ''}`,
  data_profiled: (d) => { const c = d.columns; const n = Array.isArray(c) ? c.length : Object.keys(c || {}).length; return `dataset profiled (${n} column${n === 1 ? '' : 's'})` },
  data_provenance: (d) => { const n = Object.keys(d.assets || {}).length; return `dataset provenance pinned (${n} asset${n === 1 ? '' : 's'})` },
  // Setup phase (task + data), made watchable: these appear live between run start and the first node.
  setup_started: (d) => `setting up task & data${d.repo ? ' (repo)' : ''}…`,
  setup_step: (d) => `setup: ${d.step}${d.detail ? ' — ' + String(d.detail).slice(0, 80) : (d.sources?.length ? ' (' + d.sources.join(', ') + ')' : '')}`,
  setup_finished: (d) => `setup done${d.seconds != null ? ` (${d.seconds}s)` : ''}`,
  workspace_seeded: (d) => `seeded node #${d.node_id ?? '?'} workspace: ${(d.materialized || []).join(', ').slice(0, 90)}`,
  run_setup_started: (d) => `run setup (once): ${(d.command || []).join(' ').slice(0, 80)}`,
  run_setup_finished: (d) => `run setup ${d.exit_code === 0 ? 'ok' : 'FAILED (exit ' + d.exit_code + ')'}`,
  host_grading: (d) => `host-side grading active${d.scorer ? ' (' + d.scorer + ')' : ''}${d.competition ? ' · ' + d.competition : ''}`,
  diversity_archive: () => 'diversity archive updated',
  workspace_changed: () => 'workspace changed since the last run — re-grounding',
  budget: (d) => `checkpoint — ${d.nodes} node${d.nodes === 1 ? '' : 's'}, ${fmt(d.elapsed_s, 3)}s elapsed`,
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
  policy_decision: 'decision', strategy_decision: 'decision', rung_promoted: 'decision', agent_decision: 'decision', set_strategy: 'decision', hypothesis_ranked: 'decision', foresight_selected: 'decision',
  research_completed: 'research', deep_research: 'research',
  report_generated: 'report', reflection_note: 'report',
  reward_hack_suspected: 'trust', data_leakage: 'trust', spec_drift: 'trust', novelty_rejected: 'trust',
  hint: 'control', pause: 'control', resume: 'control', run_abort: 'control', node_abort: 'control',
  fork: 'control', promote: 'control', annotation: 'control', inject_node: 'control', force_confirm: 'control',
  force_ablate: 'control', approval_requested: 'control', approval_granted: 'control', budget_extend: 'control',
  run_reopened: 'control', spec_approved: 'control', spec_approval_requested: 'control', spec_proposed: 'control',
  fork_done: 'control', inject_done: 'control',
  confirm_done: 'eval', confirm_eval: 'eval', agent_validated: 'eval',
  drift_unavailable: 'trust', workspace_changed: 'trust',
  run_started: 'lifecycle', run_finished: 'lifecycle', llm_cost: 'lifecycle', budget: 'lifecycle',
  data_profiled: 'lifecycle', data_provenance: 'lifecycle', host_grading: 'lifecycle', diversity_archive: 'lifecycle',
  setup_started: 'lifecycle', setup_step: 'lifecycle', setup_finished: 'lifecycle', workspace_seeded: 'lifecycle',
  run_setup_started: 'lifecycle', run_setup_finished: 'lifecycle',
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
  deep_research: 'search', agent_decision: 'bot', run_reopened: 'play', inject_node: 'gitbranch',
  fork: 'gitbranch', agent_validated: 'target', hypothesis_ranked: 'bulb', foresight_selected: 'bulb',
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
  // Zombie guard: the run isn't finished but no engine process holds the lock (engine_running===false,
  // server-probed). Without this the strip would pulse "Thinking about the next step…" forever even
  // though nothing is running — the exact symptom of a resume that died without emitting run_finished.
  if (live.engine_running === false) return 'Engine stopped — resume to keep going'
  const phase = live.phase
  if (phase === 'grounding' || phase === 'onboarding') return 'Setting up the task & data…'
  if (phase === 'approval') return 'Waiting for your approval…'
  if (phase === 'spec_approval') return 'Waiting to ratify the eval spec…'
  const wid = workingId(live)                                   // a node is pending → it's evaluating
  if (wid != null) return `Running experiment #${wid}…`
  const last = log.length ? log[log.length - 1].type : null     // between nodes → infer from last event
  if (last === 'setup_started' || last === 'setup_step' || last === 'workspace_seeded') return 'Setting up task & data…'
  if (last === 'run_setup_started') return 'Installing dependencies…'
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
      {think.length > 0 && <Disclosure label="Researcher thinking (debug)">
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
function EventRow({ e, trace, onFocusEvent, autoOpen, runId }) {
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
  // carries only parent_id — never renders the PARENT node's trace mislabeled as its own. The
  // `setup_started` row surfaces the SETUP phase's span tree (task/data materialization, profiling,
  // genesis) — traced under the pseudo-node -1 — so the opaque "setting up task & data" step becomes a
  // watchable trace like any experiment.
  const traceNid = e.data?.node_id ?? (e.type === 'setup_started' ? -1 : null)
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
          {hasTrace && <NodeTrace spans={nodeSpans} runId={runId} />}
        </div>}
      </div>
    </div>
  )
}

// Round-9: the per-run "boss" chat moved to the single persistent assistant, so the Dock is purely
// the run's EVENTS window — the timeline feed + scrubber + filters + transport.
export default function Dock({ runId, live, liveSeq, viewSeq, setViewSeq, onFocus, collapsed, onToggleCollapse, height = 230, onToast }) {
  const [log, setLog] = useState([])
  const [trace, setTrace] = useState(null)
  const [filter, setFilter] = useState('')
  const [kinds, setKinds] = useState(() => new Set())     // selected kind chips (empty = all)
  const feedRef = useRef(null)
  // round-7: scrubber + filter chips collapse into one block to save space; default hidden, remembered.
  const [showControls, setShowControls] = useState(() => localStorage.getItem('ll.dock.controls') === '1')
  const toggleControls = () => setShowControls(v => { const n = !v; localStorage.setItem('ll.dock.controls', n ? '1' : '0'); return n })
  useEffect(() => {
    let alive = true
    get(`/api/runs/${runId}/log`).then(d => { if (alive) setLog(d) }).catch(() => {})
    return () => { alive = false }
  }, [runId, liveSeq])
  // The trace re-folds the whole spans.jsonl server-side, and it only backs the inline "thinking"
  // cards — so refetch when a NODE is added (or the run finishes), not on every SSE seq tick.
  const nodeCount = live ? Object.keys(live.nodes || {}).length : 0
  // Refetch the trace when a node is ADDED or SETTLES (evaluate/repair spans land on a node-in-place,
  // which doesn't change nodeCount) — so a node's eval/repair waterfall appears on its feed rows
  // without waiting for the next node. Still not every SSE tick (idle ticks don't change either key).
  const settledCount = live ? Object.values(live.nodes || {}).filter(n => n.status === 'evaluated' || n.status === 'failed').length : 0
  // The task/data SETUP phase runs before any node exists (nodeCount 0), so the node-count/settled
  // triggers never fire and its span tree would sit frozen. While in setup, key the refetch on liveSeq
  // so the setup trace streams in live (the phase is short; a few small refetches). Once the first node
  // lands this collapses to 0 and normal node-based refetching resumes.
  const inSetup = !!live && !live.finished && nodeCount === 0
  useEffect(() => { get(`/api/runs/${runId}/trace`).then(setTrace).catch(() => {}) },
    [runId, nodeCount, settledCount, live?.finished, inSetup ? liveSeq : 0])
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

  // The chronological feed: events, filtered + time-scrubbed.
  const feed = useMemo(() =>
    log.filter(e => (atLive || e.seq <= viewSeq) && kindMatch(e) && textMatch(e)),
    [log, atLive, viewSeq, filter, kinds])

  // Scroll the CONTAINER after paint — tall detail cards lay out late, so scrollIntoView fired
  // mid-layout would land short and leave a new row visually colliding with the row above it.
  useEffect(() => {
    if (!atLive) return
    const el = feedRef.current
    if (el) requestAnimationFrame(() => { el.scrollTop = el.scrollHeight })
  }, [feed.length, atLive])
  // round-7 transport: mode derived from live state; wired to existing control endpoints + reset.
  const paused = !!live?.paused, finished = !!live?.finished
  // Zombie: not finished, not paused, but no engine holds the lock (engine_running===false) — the run
  // looks "running" in the event log yet nothing drives it. Give it its own transport so the user gets a
  // resume button instead of the running-run's pause/stop (which would do nothing useful).
  const stalled = !finished && !paused && live?.engine_running === false
  const mode = finished ? 'finished' : stalled ? 'stalled' : paused ? 'paused' : 'running'
  const onPause = () => CONTROL.pause(runId).then(() => onToast?.('paused')).catch(e => onToast?.('pause failed: ' + e.message))
  const onResume = () => CONTROL.resume(runId).then(() => onToast?.('resumed')).catch(e => onToast?.('resume failed: ' + e.message))
  const onStop = () => CONTROL.abort(runId).then(() => onToast?.('stop requested')).catch(e => onToast?.('stop failed: ' + e.message))
  const onReopen = async () => { try { await CONTROL.reopen(runId); await resumeRun(runId); onToast?.('reopened & resumed') } catch (e) { onToast?.('reopen failed: ' + e.message) } }
  const onReplay = async () => {
    if (!window.confirm('Reset this run? Wipes all events & nodes and restarts from scratch.')) return
    try { await resetRun(runId); onToast?.('replaying from scratch') } catch (e) { onToast?.('reset failed: ' + e.message) }
  }
  return (
    <div className="dock chat-dock">
      <div className="dock-tabs">
        <span className="chat-label"><OpIcon name="flag" size={14} /> events &amp; timeline</span>
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
            ? <div className="muted">{(filter || kinds.size) ? 'nothing matches the filter' : 'no events yet'}</div>
            : feed.map(e =>
                <EventRow key={'e' + e.seq} e={e} trace={trace} onFocusEvent={focusEvent} runId={runId}
                    autoOpen={(e.type === 'node_created' && eventNode(e) === livePendingId)
                      || (e.type === 'setup_started' && inSetup)} />)}
          {(() => {
            // A short, honest "what's the agent doing now" strip at the foot of the feed.
            const pipeline = atLive ? agentStatus(live, log) : null
            if (!pipeline) return null
            return <div className="agent-status">
              <span className="as-dot" />
              <span className="as-seg">{pipeline}</span>
            </div>
          })()}
        </div>
        <div className="dock-foot">
          <span className="muted" style={{ fontSize: 11, flex: 1 }}>
            Steer this run from the assistant bar below — say what to do, or use <code className="cmd-hint">/pause</code> · <code className="cmd-hint">/stop</code> · <code className="cmd-hint">/approve #id</code>.
          </span>
          <div className="transport">
            {mode === 'running' && <>
              <button className="btn sm" title="pause the run" onClick={onPause}><OpIcon name="pause" size={13} /></button>
              <button className="btn sm danger" title="stop the run" onClick={onStop}><OpIcon name="stop" size={13} /></button></>}
            {mode === 'paused' && <>
              <button className="btn sm primary" title="resume the run" onClick={onResume}><OpIcon name="play" size={13} /></button>
              <button className="btn sm danger" title="stop the run" onClick={onStop}><OpIcon name="stop" size={13} /></button></>}
            {mode === 'stalled' && <>
              <button className="btn sm primary" title="engine stopped — resume to keep going" onClick={onReopen}><OpIcon name="play" size={13} /></button>
              <button className="btn sm danger" title="give up — mark the run finished" onClick={onStop}><OpIcon name="stop" size={13} /></button></>}
            {mode === 'finished' && <>
              <button className="btn sm primary" title="reopen & resume" onClick={onReopen}><OpIcon name="play" size={13} /></button>
              <button className="btn sm" title="reset & restart from scratch" onClick={onReplay}><OpIcon name="replay" size={13} /></button></>}
          </div>
        </div>
      </div>}
    </div>
  )
}
