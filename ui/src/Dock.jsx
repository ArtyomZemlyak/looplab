import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { get, fmt, workingId, resetRun, getRunCommand, retryRunCommand, runCommand,
  commandFeedback, commandErrorMessage, commandFailureRecord, commandCanRetry, createIdempotencyKey,
  commandActionForEvent, commandRecordMatchesAction, commandEventForAction,
  loadRunTransport, saveRunTransport, clearRunTransport, isTransientCommandReadError,
  clearRunCommandLock, loadRunCommandLock, saveRunCommandLock, subscribeRunCommandLock,
  COMMAND_SUCCEEDED, COMMAND_FAILED, storageGet, storageSet, runApiPath, runNodeApiPath } from './util.js'
import { usePoll } from './hooks.js'
import Markdown from './markdown.jsx'
import { NodeTrace } from './Inspector.jsx'
import { OpIcon } from './icons.jsx'
import { runLifecycle } from './runIndex.js'
import VirtualTimeline from './VirtualTimeline.jsx'
import { timelineEventKey } from './timelineModel.js'
import { DataTable } from './accessibility.jsx'


// The run's EVENTS window (round-9): one scrubbable, filterable feed that renders every run event
// as a differentiated message. The per-run "boss" chat moved to the single persistent assistant, so
// there is no composer here — just the timeline, scrubber, filters, and transport.

const NARR = {
  run_started: (d) => `run started — ${d.goal || d.task_id} (${d.direction})`,
  node_building: (d) => `building node #${d.node_id} via ${d.operator || 'improve'}…`,
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
  pause: () => 'stopped (frozen — not finalized)',
  // (`resume` / `run_abort` are defined once, below with the richer wording — the duplicate keys here
  // were dead: the later definitions always won. arch-review §5 P3.)
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
  run_finished: (d) => (d?.reason === 'aborted' || d?.reason === 'finalized') ? 'run finalized (wrapped up)'
    : `run finished${d.reason ? ' (' + d.reason + ')' : ''}`,
  llm_cost: (d) => `LLM: ${d.total_tokens} tokens, $${fmt(d.cost)}`,
  // --- operator/boss control INTENTS + their engine confirmations. Every event the agentic boss can
  // produce gets a plain-English line here, so an action never shows in the feed as a raw-JSON blob. ---
  force_confirm: (d) => `requested a multi-seed confirm of #${d.node_id}`,
  force_ablate: (d) => `requested an ablation probe on #${d.node_id}`,
  fork: (d) => `forked a fresh improve-branch from #${d.from_node_id}`,
  inject_node: (d) => { const i = d.idea || {}; return `added experiment: ${i.operator || 'improve'}${d.parent_id != null ? ' from #' + d.parent_id : ''}${i.rationale ? ' — ' + String(i.rationale).slice(0, 70) : ''}` },
  annotation: (d) => `note on #${d.node_id}: ${String(d.text || '').slice(0, 80)}`,
  run_reopened: () => 'run reopened to keep going',
  resume: () => 'run resumed — continuing',
  run_abort: (d) => `finalize requested${d.reason ? ' (' + d.reason + ')' : ''} — wrapping up`,
  command_ack: (d) => `engine acknowledged command${d.event_seq != null ? ` at event ${d.event_seq}` : ''}`,
  hypothesis_added: (d) => `hypothesis added${d.source ? ' (' + d.source + ')' : ''} — ${String(d.statement || '').slice(0, 90)}`,
  hypothesis_merged: (d) => `hypotheses merged — ${String(d.statement || '').slice(0, 80)}${(d.aliases || []).length ? ` (${(d.aliases || []).length} paraphrase${(d.aliases || []).length === 1 ? '' : 's'} folded)` : ''}`,
  lessons_distilled: (d) => `distilled ${d.count || 0} lesson${d.count === 1 ? '' : 's'}${d.trigger ? ' (' + d.trigger + ')' : ''}`,
  lessons_refreshed: (d) => d.skipped ? 'cross-run lessons refresh skipped' : 'cross-run lessons refreshed',
  coverage_snapshot: (d) => `coverage — ${d.themes || 0} theme${d.themes === 1 ? '' : 's'} · ${d.niches || 0} niche${d.niches === 1 ? '' : 's'}${d.dominant_theme_frac != null ? ` · dominant ${Math.round(d.dominant_theme_frac * 100)}%` : ''}`,
  deps_installed: (d) => `dependencies installed${d.packages ? ': ' + (Array.isArray(d.packages) ? d.packages.slice(0, 6).join(', ') : String(d.packages).slice(0, 80)) : ''}`,
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
  node_building: 'proposal', node_created: 'proposal',
  node_evaluated: 'eval', node_failed: 'eval', node_repaired: 'eval', node_confirmed: 'eval', best_confirmed: 'eval', proxy_scored: 'eval', ablate: 'eval',
  policy_decision: 'decision', strategy_decision: 'decision', rung_promoted: 'decision', agent_decision: 'decision', set_strategy: 'decision', hypothesis_ranked: 'decision', foresight_selected: 'decision',
  research_completed: 'research', deep_research: 'research',
  hypothesis_added: 'research', hypothesis_merged: 'research', lessons_refreshed: 'research', lessons_distilled: 'research', coverage_snapshot: 'decision', deps_installed: 'eval',
  report_generated: 'report', reflection_note: 'report',
  reward_hack_suspected: 'trust', data_leakage: 'trust', spec_drift: 'trust', novelty_rejected: 'trust',
  hint: 'control', pause: 'control', resume: 'control', run_abort: 'control', node_abort: 'control',
  fork: 'control', promote: 'control', annotation: 'control', inject_node: 'control', force_confirm: 'control',
  force_ablate: 'control', approval_requested: 'control', approval_granted: 'control', budget_extend: 'control',
  run_reopened: 'control', spec_approved: 'control', spec_approval_requested: 'control', spec_proposed: 'control',
  command_ack: 'control',
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
// Events that OWN a node's agent trace — only these expand to the node's span tree (create_node =
// propose+implement, evaluate, repair). Every other event that happens to carry a node_id (foresight/
// hypothesis/strategy/coverage/lessons) shows only its OWN detail, never the node's whole trace.
const TRACE_OWNER_TYPES = new Set(['node_created', 'node_evaluated', 'node_failed', 'node_repaired', 'node_building', 'setup_started'])
// Events the engine wraps in their OWN new_trace op-span (so their trace_id isolates just that
// operation). ONLY these get the per-op trace expansion — an allow-list, because eventstore stamps
// EVERY event with the ambient span's trace_id, so an incidental event appended inside evaluate/
// create_node (spec_drift, novelty_rejected) would otherwise dump that whole node/eval trace.
const OP_TRACE_TYPES = new Set(['strategy_decision', 'hypothesis_merged', 'research_completed',
  'report_generated', 'hypothesis_ranked', 'foresight_selected', 'lessons_distilled', 'lessons_refreshed'])
const CLOSED_EXPANSION = Object.freeze({ open: false, touched: false })

// Pull the model's raw <think> chain-of-thought for a node out of the trace view (spans.jsonl) — so
// the feed can surface "what was the Researcher thinking" inline. Returns [{op, text}] for the node.
function collectThinking(trace, nid) {
  if (nid == null) return []
  // `trace` here is the PER-NODE trace (/nodes/{nid}/trace), whose `nodes` is already this node's tree
  // LIST; tolerate the old whole-run shape (a {nid: [...]} map) too so nothing breaks mid-transition.
  const spans = Array.isArray(trace?.nodes) ? trace.nodes : ((trace?.nodes || {})[String(nid)] || [])
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
// Live agent trace: a collapsed disclosure under the "Thinking…/Planning…" status that streams the
// most recent LLM thoughts + tool calls (with args), so you can see WHAT the agent is doing, not just
// a coarse label. Polls /trace/tail only while OPEN + live (cheap when collapsed). Server-side the feed
// is bounded (tail of spans.jsonl); full I/O of any observation is at /spans/{sid}.
function LiveTrace({ runId, active }) {
  const [tail, setTail] = useState([])
  const [open, setOpen] = useState(false)
  const bodyRef = useRef(null)
  usePoll((alive) => get(runApiPath(runId, '/trace/tail?limit=40'))
    .then(r => { if (alive()) setTail(r.tail || []) }).catch(() => {}),
    3000, [runId, active, open], { enabled: active && open })
  useEffect(() => { if (open && bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight }, [tail, open])
  return (
    <div className={'live-trace' + (open ? ' open' : '')}>
      {/* Standard inline disclosure — caret ▸ left of the label, expands IN PLACE (not a popup). */}
      <button type="button" className="lt-toggle disclosure-button" aria-expanded={open}
           onClick={() => setOpen(o => !o)} title="stream the agent's thoughts + tool calls">
        <span className="lt-caret">{open ? '▾' : '▸'}</span>trace
      </button>
      {open && <div className="lt-body" ref={bodyRef}>
        {tail.length === 0
          ? <div className="muted lt-empty">waiting for the next agent step…</div>
          : tail.map((it, i) => it.kind === 'generation'
            ? <div key={it.span_id || i} className="lt-row lt-gen">
                <span className="lt-ic">🧠</span>
                <span className="lt-txt">{it.text || <span className="muted">({it.model})</span>}</span>
              </div>
            : <div key={it.span_id || i} className="lt-row lt-tool">
                <span className="lt-ic">🔧</span>
                <span className="lt-tool-name">{it.tool}</span>
                {it.arg && <span className="lt-arg">{it.arg}</span>}
              </div>)}
      </div>}
    </div>
  )
}

// Bookkeeping events that do NOT reflect what the agent is DOING — skip them when inferring the
// between-experiments status, else the strip flickers to "Thinking…" every time one of these lands
// right after a node (coverage/cost/lessons/reflection all fire post-eval).
const STATUS_NOISE = new Set([
  'coverage_snapshot', 'llm_cost', 'reflection_note', 'diversity_archive',
  'lessons_distilled', 'lessons_refreshed', 'budget', 'node_building', 'command_ack',
])

function agentStatus(live, log) {
  if (!live) return null
  const lifecycle = runLifecycle(live)
  if (lifecycle.mode === 'finished') return null
  if (lifecycle.mode === 'finishing') return 'Finishing terminal write-out…'
  if (lifecycle.mode === 'finalization-stalled') return 'Finalization stalled — recovery is required.'
  if (lifecycle.mode === 'finalizing') return 'Finalizing — wrapping up report, lessons, and cost…'
  if (live.paused) return 'Paused'
  // Zombie guard: the run isn't finished but no engine process holds the lock (engine_running===false,
  // server-probed). Without this the strip would pulse "Thinking about the next step…" forever even
  // though nothing is running — the exact symptom of a resume that died without emitting run_finished.
  if (live.engine_running === false) return 'Engine stopped — resume to keep going'
  const phase = live.phase
  if (phase === 'grounding' || phase === 'onboarding') return 'Setting up the task & data…'
  if (phase === 'approval') return 'Waiting for your approval…'
  if (phase === 'spec_approval') return 'Waiting to ratify the eval spec…'
  // WRITING vs RUNNING are distinct and were conflated before (both said "Running experiment"):
  //   • `building` is set from node_building until node_created folds → the Developer is WRITING code;
  //   • a node with status 'pending' → its code is written and the sandbox is TRAINING it.
  if (live.building && live.building.node_id != null) {
    const op = live.building.operator || ''
    const id = live.building.node_id
    return /repair|debug/.test(op) ? `Repairing experiment #${id}…`
      : /merge/.test(op) ? `Merging into experiment #${id}…`
      : `Writing experiment #${id}…`
  }
  const pend = Object.values(live.nodes || {}).filter(n => n.status === 'pending').map(n => n.id)
  if (pend.length) return `Running experiment #${Math.max(...pend)}… (training)`
  // Between experiments: infer from the last MEANINGFUL event (skip the bookkeeping noise above), so the
  // label stays put on "Planning…" instead of blinking every time a coverage/cost event lands.
  let last = null
  for (let i = log.length - 1; i >= 0; i--) { if (!STATUS_NOISE.has(log[i].type)) { last = log[i].type; break } }
  if (last === 'setup_started' || last === 'setup_step' || last === 'workspace_seeded') return 'Setting up task & data…'
  if (last === 'run_setup_started') return 'Installing dependencies…'
  if (last === 'strategy_decision' || last === 'set_strategy') return 'Choosing a strategy…'
  if (last === 'research_completed' || last === 'deep_research') return 'Reading the literature…'
  if (last === 'node_created') return 'Writing & running the experiment…'
  // node_evaluated / node_failed / policy_decision / agent_decision → the loop is picking what's next.
  return 'Planning the next experiment…'
}

const Disclosure = ({ label, children }) => {
  const [open, setOpen] = useState(false)
  return <div className="think-debug" style={{ marginTop: 6 }}>
    <button type="button" className="role-think disclosure-button" aria-expanded={open}
         onClick={() => setOpen(v => !v)}
         style={{ cursor: 'pointer', fontSize: 11, textTransform: 'uppercase', letterSpacing: '.5px' }}>
      {open ? '▾' : '▸'} {label}</button>
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
        : <DataTable caption="Candidate scores for node selection" card={false}><table className="tbl"><thead><tr><th>node</th><th>score</th></tr></thead>
            <tbody>{entries.map(([nid, sc]) =>
              <tr key={nid} className={String(nid) === String(d.chosen) ? 'chosen-row' : ''}>
                <td>#{nid}{String(nid) === String(d.chosen) ? ' ✓ chosen' : ''}</td><td>{fmt(sc, 4)}</td></tr>)}
            </tbody></table></DataTable>}
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

// The trace of ONE sub-operation (strategy_consult / hypothesis_merge …), fetched lazily by the
// event's own trace_id — so a strategy_decision row shows only the strategist's reasoning, not the
// whole node. Rendered with the same span-tree component as a node's trace.
function OpTrace({ runId, traceId }) {
  const [spans, setSpans] = useState(null)
  useEffect(() => {
    let on = true
    setSpans(null)
    get(runApiPath(runId, `/trace/by_trace/${encodeURIComponent(traceId)}`))
      .then(d => on && setSpans(d?.spans || [])).catch(() => on && setSpans([]))
    return () => { on = false }
  }, [runId, traceId])
  if (spans === null) return <div className="muted" style={{ fontSize: 12, padding: '4px 2px' }}>loading trace…</div>
  if (!spans.length) return <div className="muted" style={{ fontSize: 12, padding: '4px 2px' }}>no trace captured for this step</div>
  return <NodeTrace spans={spans} runId={runId} />
}

// One feed row, chat-message styled: an icon/color by kind, the narration, an expandable "why" card.
function EventRow({ e, onFocusEvent, autoOpen, runId, readOnly = false, liveBuilding = null,
  expansion = null, onExpansionChange = null }) {
  const [localOpen, setLocalOpen] = useState(autoOpen)
  const localTouched = useRef(false)
  const controlled = expansion != null
  const open = controlled ? expansion.open === true : localOpen
  const touched = controlled ? expansion.touched === true : localTouched.current
  const changeOpen = (next, wasTouched = touched) => {
    if (controlled) onExpansionChange?.({ open: next, touched: wasTouched })
    else { localTouched.current = wasTouched; setLocalOpen(next) }
  }
  // collapse-when-done: follow autoOpen (expand while live, collapse when the node resolves) UNLESS
  // the user manually toggled this card — then their choice wins.
  useEffect(() => { if (!touched && open !== autoOpen) changeOpen(autoOpen, false) }, [autoOpen])
  const nid = eventNode(e)
  const hasReason = REASONING_TYPES.has(e.type)
  // The node's span trace (create_node = propose+implement, or evaluate) belongs ONLY to the events
  // that ARE that node's work — its lifecycle. Incidental sub-operation events (foresight_ranked/
  // _selected, hypothesis_ranked/_merged, strategy_decision, coverage_snapshot, …) merely carry a
  // node_id for CONTEXT; expanding them must NOT dump the node's whole Researcher+Developer trace —
  // that isn't their work, and it's the exact "why is the Researcher+Developer trace under a foresight
  // row" bug. They fall through to their OWN reasoning/data detail below. (Per-operation LLM traces
  // for these would need a named span + an event span_id — a backend change; TODO.) `setup_started`
  // surfaces the SETUP phase's tree (pseudo-node -1). OWN node_id only — never the parent_id fallback.
  const traceNid = TRACE_OWNER_TYPES.has(e.type)
    ? (e.data?.node_id ?? (e.type === 'setup_started' ? -1 : null))
    : null
  // LAZY: fetch only THIS node's trace (/nodes/{nid}/trace — reads just the node's spans via the index,
  // O(node)), and only when the row is expanded. Replaces the old whole-run /trace fetch+4s poll that
  // shipped (and re-rendered) the entire 4000-node timeline on every node boundary just to back a few
  // inline "thinking" cards. Full per-observation I/O is still fetched on demand via /spans/{sid}.
  const [nodeTrace, setNodeTrace] = useState(null)
  const [nodeTraceError, setNodeTraceError] = useState(false)
  const [nodeTraceNonce, setNodeTraceNonce] = useState(0)
  const rawTraceGeneration = Object.hasOwn(e.data || {}, 'generation')
    ? e.data.generation : (e.type === 'node_repaired' ? e.data?.attempt : 0)
  const traceGeneration = Number.isInteger(rawTraceGeneration) && rawTraceGeneration >= 0
    ? rawTraceGeneration : null
  const exactBuilding = liveBuilding != null && traceNid === liveBuilding.nodeId
    && traceGeneration != null
    && traceGeneration === liveBuilding.generation
  // Clear the error flag only on a SUCCESSFUL load (not eagerly at the start of every poll tick):
  // clearing then re-setting each 4s tick made the error/Retry banner flicker on a persistent failure.
  const loadNodeTrace = (alive) => get(runNodeApiPath(runId, traceNid, '/trace'))
    .then(d => { if (alive()) { setNodeTrace(d); setNodeTraceError(false) } })
    .catch(() => { if (alive()) { setNodeTrace(null); setNodeTraceError(true) } })
  usePoll((alive) => loadNodeTrace(alive), 4000,
    [open, readOnly, runId, traceNid, exactBuilding, nodeTraceNonce],
    { enabled: open && !readOnly && traceNid != null && exactBuilding })
  useEffect(() => {
    if (!open || readOnly || traceNid == null || exactBuilding) return undefined
    let alive = true
    loadNodeTrace(() => alive)
    return () => { alive = false }
  }, [open, readOnly, runId, traceNid, exactBuilding, nodeTraceNonce])
  const nodeSpans = Array.isArray(nodeTrace?.nodes) ? nodeTrace.nodes : []
  const hasTrace = !readOnly && traceNid != null
  // A sub-operation event the engine wrapped in its OWN named trace (strategy_decision, hypothesis_
  // merged) carries a trace_id — expand to ONLY that operation's trace (lazily fetched by trace_id),
  // never the node's whole Researcher+Developer trace. Old events (no trace_id) fall through to detail.
  const opTraceId = (!readOnly && OP_TRACE_TYPES.has(e.type) && e.trace_id) ? e.trace_id : null
  // no-truncation: a row whose one-line narration clamped text (or used the raw JSON fallback) is
  // expandable to its FULL content even without a dedicated reasoning card.
  const isRawFallback = !hasReason && !NARR[e.type]
  const hasGeneric = !hasReason && (genericRows(e).length > 0 || isRawFallback)
  const omittedBytes = e?._log_page?.truncated ? Number(e._log_page.raw_bytes || 0) : 0
  const hasOmittedDetail = e?._log_page?.truncated === true
  const expandable = hasReason || hasTrace || !!opTraceId || hasGeneric || hasOmittedDetail
  const { group, glyph } = kindOf(e.type)
  const narr = hasOmittedDetail
    ? `${e.type || 'event'} — details omitted (${omittedBytes.toLocaleString()} source bytes exceed page limit)`
    : (NARR[e.type] || ((d) => JSON.stringify(d).slice(0, 80)))(e.data)
  const detailsId = `timeline-event-${e.seq}-details`
  return (
    <div className={'feed-msg k-' + group}>
      <div className="fm-ic" title={group}><OpIcon name={glyph} size={14} className="fm-ic-svg" /></div>
      <div className="fm-body">
        <div className="fm-line">
          {expandable && <button type="button" className="fm-tw" aria-expanded={open}
            aria-controls={detailsId}
            aria-label={`${open ? 'Collapse' : 'Expand'} details for event ${e.seq}`}
            onClick={() => changeOpen(!open, true)}>{open ? '▾' : '▸'}</button>}
          <button type="button" className="fm-main" onClick={() => onFocusEvent(e)}
            title={nid != null ? `open node #${nid} @ seq ${e.seq}` : `jump to seq ${e.seq}`}>
            <span className="fm-narr">{narr}</span>
            {nid != null && <span className="ev-go">↗</span>}
          </button>
        </div>
        {open && expandable && <div className="ev-detail-wrap" id={detailsId}>
          {hasOmittedDetail && <div className="notice" role="note">
            Event details were not transferred: {omittedBytes.toLocaleString()} source bytes exceed the bounded page response.
          </div>}
          {hasReason && reasoningDetail(e, nodeTrace)}
          {hasGeneric && <GenericDetail e={e} />}
          {hasTrace && nodeTrace == null && !nodeTraceError && <div className="muted" role="status">loading node trace…</div>}
          {hasTrace && nodeTraceError && <div className="notice resource-error compact" role="alert">
            <span>Could not load node trace.</span><button type="button" className="btn sm"
              onClick={() => setNodeTraceNonce(value => value + 1)}>Retry</button>
          </div>}
          {hasTrace && nodeTrace != null && (nodeSpans.length
            ? <NodeTrace spans={nodeSpans} runId={runId} />
            : <div className="muted">no trace captured for this node</div>)}
          {opTraceId && <OpTrace runId={runId} traceId={opTraceId} />}
        </div>}
      </div>
    </div>
  )
}

const TRANSPORT_INTENTS = {
  stop: { type: 'pause', data: {} },
  finalize: { type: 'run_abort', data: { reason: 'finalized' } },
  resume: { type: 'resume', data: {} },
}

const recoveryForRun = (runId) => {
  const saved = loadRunTransport(runId)
  if (!saved) return { pending: null, failure: null }
  if (saved.protocolInvalid) return { pending: {
    action: saved.action, idempotencyKey: saved.idempotencyKey, record: saved.record,
    expectedGeneration: saved.expectedGeneration,
    statusUnavailable: true, observationKind: 'protocol', protocolInvalid: true,
    canResubmit: false, lastError: 'Stored command recovery data is invalid.',
  }, failure: null }
  const storedRecord = saved.record || (saved.commandId
    ? { id: saved.commandId, status: 'accepted' }
    : { status: 'submitting' })
  const record = saved.commandId && !storedRecord.id ? { ...storedRecord, id: saved.commandId } : storedRecord
  if (COMMAND_SUCCEEDED.has(record.status)) {
    clearRunTransport(runId)
    return { pending: null, failure: null }
  }
  const knownStatus = record.status === 'accepted' || record.status === 'executing'
    || COMMAND_FAILED.has(record.status)
  const needsObservation = !knownStatus || !record.id || !!saved.retrying || !!saved.checking
  const entry = {
    action: saved.action, idempotencyKey: saved.idempotencyKey, record,
    expectedGeneration: saved.expectedGeneration,
    statusUnavailable: !!saved.statusUnavailable || needsObservation,
    observationKind: saved.observationKind || (!knownStatus && record.id ? 'protocol'
      : (needsObservation ? 'transport' : null)),
    lastError: saved.retrying || saved.checking
      ? 'The page reloaded while recovery was in progress; check the durable command status.'
      : !knownStatus ? 'Stored command state needs to be checked against the server.' : '',
  }
  return COMMAND_FAILED.has(record.status) && !saved.statusUnavailable && !saved.retrying && !saved.checking
    ? { pending: null, failure: entry }
    : { pending: entry, failure: null }
}

const observationKind = error => {
  if (error?.status === 401 || error?.status === 403) return 'access'
  if (error?.code === 'COMMAND_PROTOCOL_ERROR') return 'protocol'
  if (error?.status === 404) return 'missing'
  return isTransientCommandReadError(error) ? 'transport' : 'request'
}

// Round-9: the per-run "boss" chat moved to the single persistent assistant, so the Dock is purely
// the run's EVENTS window — the timeline feed + scrubber + filters + transport.
export default function Dock({ runId, live, liveSeq, expectedGeneration, timeline, viewSeq, setViewSeq,
  onReturnToLive, onFocus, collapsed, onToggleCollapse, height = 230, onToast, readOnly = false,
  publishTransport = null, filter = '', onFilterChange = null, kindFilters = [],
  onKindFiltersChange = null, focusOnMount = false, onInitialFocus = null }) {
  const log = timeline.rows
  const collapseButtonRef = useRef(null)
  useEffect(() => {
    if (!focusOnMount) return
    collapseButtonRef.current?.focus({ preventScroll: true })
    onInitialFocus?.()
  }, [focusOnMount])
  // URL-owned diagnostic filters: Dock renders them, while RunView commits the canonical fragment
  // state. This lets reload/Back/Forward restore the exact event lens without a second store.
  const kinds = useMemo(() => new Set(kindFilters), [kindFilters])
  const setKinds = (value) => {
    const next = typeof value === 'function' ? value(new Set(kinds)) : value
    onKindFiltersChange?.([...next])
  }
  const restoredRef = useRef(null)
  if (!restoredRef.current || restoredRef.current.runId !== runId) {
    restoredRef.current = { runId, ...recoveryForRun(runId) }
  }
  const [transportPending, setTransportPending] = useState(() => restoredRef.current.pending)
  const [transportFailure, setTransportFailure] = useState(() => restoredRef.current.failure)
  const [runCommandLock, setRunCommandLock] = useState(() => loadRunCommandLock(runId))
  const externalTransportPending = runCommandLock?.source === 'assistant' ? runCommandLock : null
  const transportBusy = !!transportPending || !!externalTransportPending
  // Expansion is view-owned rather than row-owned: virtual rows may unmount offscreen, but a user's
  // open reasoning/trace card must still be open when that retained event comes back into view.
  const [eventExpansion, setEventExpansion] = useState(() => new Map())
  useEffect(() => setEventExpansion(new Map()), [runId, timeline.generation])
  useEffect(() => {
    const retained = new Set(log.map(timelineEventKey))
    setEventExpansion(current => {
      if ([...current.keys()].every(key => retained.has(key))) return current
      return new Map([...current].filter(([key]) => retained.has(key)))
    })
  }, [log])
  useEffect(() => {
    const restored = recoveryForRun(runId)
    const lock = loadRunCommandLock(runId)
    if (lock && lock.source !== 'dock' && (restored.pending || restored.failure)) {
      clearRunTransport(runId)
      setTransportPending(null); setTransportFailure(null)
    } else {
      let pending = restored.pending
      const entry = restored.pending || restored.failure
      const lockMismatch = lock?.source === 'dock' && entry && (
        lock.idempotencyKey !== entry.idempotencyKey || lock.action !== entry.action
        || lock.expectedGeneration !== entry.expectedGeneration
        || (lock.commandId && entry.record?.id && lock.commandId !== entry.record.id)
      )
      if (lockMismatch) pending = { ...entry, statusUnavailable: true, observationKind: 'protocol',
        protocolInvalid: true, canResubmit: false, lockIdentity: lock,
        lastError: 'Stored command identity does not match the active recovery lock.' }
      setTransportPending(pending); setTransportFailure(lockMismatch ? null : restored.failure)
      if (pending?.protocolInvalid) {
        saveRunCommandLock(runId, { ...pending, source: 'dock' })
      } else if (pending) saveRunTransport(runId, pending)
      else if (!restored.failure && lock?.source === 'dock') {
        clearRunCommandLock(runId, { source: 'dock', idempotencyKey: lock.idempotencyKey,
          action: lock.action, expectedGeneration: lock.expectedGeneration,
          commandId: lock.commandId })
      }
    }
  }, [runId])
  useEffect(() => {
    setRunCommandLock(loadRunCommandLock(runId))
    return subscribeRunCommandLock(runId, setRunCommandLock)
  }, [runId])
  // round-7: scrubber + filter chips collapse into one block to save space; default hidden, remembered.
  const [showControls, setShowControls] = useState(() => storageGet('ll.dock.controls') === '1')
  const toggleControls = () => setShowControls(v => { const n = !v; storageSet('ll.dock.controls', n ? '1' : '0'); return n })
  useEffect(() => {
    if (filter.trim() || kinds.size > 0) setShowControls(true)
  }, [filter, kinds.size])
  // Trace details are fetched per node, only when that row is expanded. This keeps the virtualized,
  // paged timeline O(visible events) and avoids folding or transferring the whole run trace.
  const atLiveView = viewSeq == null || viewSeq >= liveSeq
  const visiblyLive = atLiveView && timeline.followingTail && timeline.windowAtTail

  // The live frontier: the highest-id node still pending while the run runs — its proposal card stays
  // expanded ("thinking") until it resolves. null on a finished/replayed run — AND on a STALLED/zombie
  // run (engine_running===false): a run whose engine died mid-eval leaves a node stuck 'pending', and
  // without this guard its node_created row would auto-expand and dump the full span trace forever.
  const liveBuilding = useMemo(() => {
    if (readOnly || !atLiveView || timeline.generation !== expectedGeneration || !live?.building) return null
    const nodeId = Number(live.building.node_id)
    const generation = Object.hasOwn(live.building, 'generation')
      ? live.building.generation : (live.nodes?.[nodeId]?.attempt ?? 0)
    if (!Number.isInteger(nodeId) || nodeId < 0 || !Number.isInteger(generation) || generation < 0) return null
    return { nodeId, generation }
  }, [readOnly, atLiveView, timeline.generation, expectedGeneration, live])

  // Scrubber: pointer/key movement is a LOCAL preview. Commit only on pointer-up/key-up/blur so a
  // 50k-event drag cannot queue a series of expensive historical state folds on the server.
  const [drag, setDrag] = useState(null)
  const dragRef = useRef(null)
  useEffect(() => {
    if (viewSeq == null) {
      dragRef.current = null
      setDrag(null)
    }
  }, [viewSeq])
  const sliderVal = drag != null ? drag : (atLiveView ? liveSeq : viewSeq)
  const returnToLive = () => {
    dragRef.current = null
    setDrag(null)
    if (onReturnToLive) onReturnToLive()
    else { setViewSeq(null); timeline.jumpToLive() }
  }
  const commit = (v) => v >= liveSeq ? returnToLive() : setViewSeq(v)
  const onScrub = (v) => {
    dragRef.current = v
    setDrag(v)
  }
  const endScrub = () => {
    if (dragRef.current == null) return
    const value = dragRef.current
    dragRef.current = null
    commit(value); setDrag(null)
  }

  const focusEvent = (e) => {
    const nid = eventNode(e)
    if (nid == null) { setViewSeq(e.seq); return }
    onFocus?.(Number(nid), e.type === 'node_created' ? 'Trace' : 'Overview', e.seq)
  }
  const toggleKind = (g) => setKinds(s => { const n = new Set(s); n.has(g) ? n.delete(g) : n.add(g); return n })
  const searchableLog = useMemo(() => log.map(event => {
    let narration = ''
    try { narration = (NARR[event.type] || (() => ''))(event.data) } catch { /* malformed source stays inspectable */ }
    // Unknown/forward-compatible event types are rendered from their raw data fallback. Include the
    // same bounded source in search so text the user can plainly see is not reported as "0 matching".
    // Keep the projection capped: the raw Explorer owns deeper payload inspection, and the Timeline
    // may retain 5,000 rows.
    let rawPreview = ''
    try { rawPreview = JSON.stringify(event.data ?? {}).slice(0, 500) } catch { /* cyclic/malformed data */ }
    return { event, search: `${event.type || ''} ${narration} ${rawPreview}`.toLowerCase() }
  }), [log])
  const filterQuery = filter.trim().toLowerCase()
  const kindMatch = (e) => kinds.size === 0 || kinds.has(TYPE2GROUP[e.type] || 'lifecycle')

  // The chronological feed: events, filtered + time-scrubbed.
  const feed = useMemo(() =>
    searchableLog.filter(({ event, search }) => (atLiveView || event.seq <= viewSeq)
      && kindMatch(event) && (!filterQuery || search.includes(filterQuery))).map(item => item.event),
    [searchableLog, atLiveView, viewSeq, filterQuery, kinds])
  useEffect(() => {
    if (!atLiveView && viewSeq != null) timeline.ensureSeq(viewSeq)
  }, [atLiveView, viewSeq, timeline.revision, timeline.ensureSeq])
  // Observable run truth comes from the same pure lifecycle used by the run list/header. Local command
  // state only hides duplicate controls; it never promotes a run to finished by itself.
  const lifecycle = runLifecycle(live || {})
  const mode = lifecycle.mode
  const transportLabels = (action) => ({
    stop: { success: 'Stopped — frozen, not finalized', noop: 'Run was already stopped',
      executing: 'Stop requested — waiting for the run to freeze', failure: 'Stop failed' },
    finalize: { success: 'Finalized — report and wrap-up complete', noop: 'Run was already finalized',
      executing: 'Finalize requested — wrapping up', failure: 'Finalize failed' },
    resume: { success: 'Run resumed', noop: 'Run was already running',
      executing: 'Resume requested — waiting for the engine', failure: 'Resume failed' },
  })[action] || { success: 'Run command completed', noop: 'Run command was already satisfied',
    executing: 'Run command is pending', failure: 'Run command failed' }
  const persistTransport = (entry) => saveRunTransport(runId, {
    ...entry, commandId: entry?.record?.id || '',
  })
  const verifiedTransportAction = (action, record, protocolInvalid = false) => {
    const actual = protocolInvalid || !TRANSPORT_INTENTS[action]
      ? commandActionForEvent(record?.event_type) : action
    return actual && TRANSPORT_INTENTS[actual] && commandRecordMatchesAction(record, actual, 'dock')
      ? actual : null
  }
  const protocolTransportState = (action, idempotencyKey, record, message, lockIdentity = null,
    boundGeneration = transportPending?.expectedGeneration || lockIdentity?.expectedGeneration || '') => {
    const commandId = /^cmd_[0-9a-f]{32}$/.test(String(record?.id || '')) ? String(record.id) : ''
    const entry = { action: action || 'unknown', idempotencyKey,
      expectedGeneration: boundGeneration,
      record: commandId ? { id: commandId, status: 'accepted' } : { status: 'submitting' },
      statusUnavailable: true, observationKind: 'protocol', protocolInvalid: true,
      canResubmit: false, lastError: message, lockIdentity }
    saveRunCommandLock(runId, { ...entry, source: 'dock' })
    setTransportPending(entry); setTransportFailure(null)
    return entry
  }
  const storageTransportFailure = (action, idempotencyKey, boundGeneration) => {
    const record = { status: 'rejected', error: {
      code: 'command_storage_unavailable',
      message: 'The command was not sent because durable tab storage is unavailable.',
      remediation: 'Enable session storage or free browser storage, then try again.', retryable: false,
    } }
    const entry = { action, idempotencyKey, expectedGeneration: boundGeneration, record }
    setTransportPending(null); setTransportFailure(entry)
    onToast?.('Command not sent — durable recovery storage is unavailable')
    return entry
  }
  const acceptTransportRecord = (action, record, idempotencyKey, boundGeneration) => {
    const pendingState = transportPending
    const actualAction = verifiedTransportAction(action, record, pendingState?.protocolInvalid)
    if (!actualAction) return protocolTransportState(action, idempotencyKey, record,
      'Command identity does not match the requested action', pendingState?.lockIdentity,
      boundGeneration)
    if (pendingState?.protocolInvalid) {
      const identity = pendingState.lockIdentity || {
        source: 'dock', idempotencyKey: pendingState.idempotencyKey,
        action: pendingState.action, expectedGeneration: pendingState.expectedGeneration,
        commandId: pendingState.record?.id || '',
      }
      clearRunCommandLock(runId, identity)
    }
    const feedback = commandFeedback(record, transportLabels(actualAction))
    onToast?.(feedback.message)
    if (feedback.kind === 'pending') {
      const entry = { action: actualAction, idempotencyKey, expectedGeneration: boundGeneration,
        record, statusUnavailable: false }
      if (!persistTransport(entry)) {
        return protocolTransportState(actualAction, idempotencyKey, record,
          'Command accepted, but its updated durable status could not be stored', null,
          boundGeneration)
      }
      setTransportPending(entry)
      setTransportFailure(null)
    } else {
      setTransportPending(null)
      if (feedback.kind === 'error') {
        const entry = { action: actualAction, idempotencyKey, expectedGeneration: boundGeneration, record }
        if (!persistTransport(entry)) clearRunTransport(runId)
        setTransportFailure(entry)
      } else {
        clearRunTransport(runId); setTransportFailure(null)
      }
    }
  }
  const unavailableTransport = (action, idempotencyKey, boundGeneration, record, error, extra = {}) => {
    const kind = observationKind(error)
    let recoveryRecord = record || { status: 'submitting' }
    if (recoveryRecord.id && !recoveryRecord.event_type && TRANSPORT_INTENTS[action]) {
      recoveryRecord = { ...recoveryRecord, event_type: commandEventForAction(action, 'dock') }
    }
    const entry = {
      action, idempotencyKey, expectedGeneration: boundGeneration, record: recoveryRecord,
      statusUnavailable: true, observationKind: kind,
      lastError: error?.message || String(error), ...extra,
    }
    if (!persistTransport(entry)) saveRunCommandLock(runId, { ...entry, source: 'dock' })
    setTransportPending(entry); setTransportFailure(null)
    return entry
  }
  const failTransport = (action, idempotencyKey, boundGeneration, error, previous = null) => {
    const record = commandFailureRecord(error, previous)
    const entry = { action, idempotencyKey, expectedGeneration: boundGeneration, record }
    if (!persistTransport(entry)) clearRunTransport(runId)
    setTransportPending(null); setTransportFailure(entry)
    onToast?.(commandFeedback(record, transportLabels(action)).message)
  }
  const runTransport = async (action, idempotencyKey = createIdempotencyKey(), {
    allowPending = false, boundGeneration = null,
  } = {}) => {
    if (!allowPending && (transportPending || loadRunCommandLock(runId))) return
    const intent = TRANSPORT_INTENTS[action]
    if (!intent) {
      protocolTransportState(action, idempotencyKey, transportPending?.record,
        'Stored command identity cannot be safely replayed', transportPending?.lockIdentity)
      return
    }
    const generation = allowPending ? boundGeneration : expectedGeneration
    if (!/^[0-9a-f]{64}$/.test(generation || '')) {
      const error = new Error('The displayed run generation is unavailable.')
      error.code = 'run_generation_unavailable'
      error.remediation = 'Refresh the run and wait for its current state before submitting another action.'
      failTransport(action, idempotencyKey, generation || '', error)
      return
    }
    const start = { action, idempotencyKey, expectedGeneration: generation,
      record: { status: 'submitting' } }
    if (!persistTransport(start)) { storageTransportFailure(action, idempotencyKey, generation); return }
    setTransportPending(start)
    setTransportFailure(null)
    try {
      const record = await runCommand(runId, intent.type, intent.data, {
        idempotencyKey, expectedGeneration: generation, waitMs: 0,
        onRecord: next => {
          const visible = { action, idempotencyKey, expectedGeneration: generation,
            record: next, statusUnavailable: false }
          if (!persistTransport(visible)) return
          setTransportPending(current => current?.action === action && current?.idempotencyKey === idempotencyKey
            ? visible : current)
        },
      })
      acceptTransportRecord(action, record, idempotencyKey, generation)
    } catch (error) {
      const record = error?.commandRecord || (error?.commandId
        ? { id: error.commandId, status: 'accepted' } : null)
      const kind = observationKind(error)
      if (error?.commandUnknown || (record?.id && ['transport', 'access', 'protocol'].includes(kind))) {
        unavailableTransport(action, idempotencyKey, generation, record, error)
        onToast?.(`${transportLabels(action).failure}: command status unavailable; the same intent was preserved`)
      } else failTransport(action, idempotencyKey, generation, error, record)
    }
  }
  const onStop = () => runTransport('stop')
  const onFinalize = () => runTransport('finalize')
  const onResume = () => runTransport('resume')
  const onRetryTransport = async () => {
    const failure = transportFailure
    if (transportBusy || loadRunCommandLock(runId) || !commandCanRetry(failure?.record)) return
    const { action, record, idempotencyKey, expectedGeneration: boundGeneration } = failure
    const retrying = { action, idempotencyKey, expectedGeneration: boundGeneration,
      record, retrying: true }
    if (!persistTransport(retrying)) {
      onToast?.('Retry not sent — durable recovery storage is unavailable')
      return
    }
    setTransportFailure(null)
    setTransportPending(retrying)
    try {
      const next = await retryRunCommand(runId, record.id, {
        waitMs: 0,
        onRecord: value => {
          const visible = { action, idempotencyKey, expectedGeneration: boundGeneration,
            record: value, retrying: true }
          persistTransport(visible)
          setTransportPending(current => current?.action === action && current?.idempotencyKey === idempotencyKey
            ? visible : current)
        },
      })
      acceptTransportRecord(action, next, idempotencyKey, boundGeneration)
    } catch (error) {
      const kind = observationKind(error)
      if (['transport', 'access', 'protocol'].includes(kind)) {
        unavailableTransport(action, idempotencyKey, boundGeneration,
          error?.commandRecord || record, error)
      } else failTransport(action, idempotencyKey, boundGeneration,
        error, error?.commandRecord || record)
    }
  }
  const onCheckTransport = async () => {
    const pending = transportPending
    if (!pending || pending.checking) return
    const checking = { ...pending, checking: true }
    if (!pending.protocolInvalid) persistTransport(checking)
    setTransportPending(checking)
    if (!pending.record?.id) {
      if (pending.protocolInvalid || pending.canResubmit === false) {
        setTransportPending({ ...pending, checking: false })
        onToast?.('Stored command identity is invalid and cannot be safely replayed; dismiss it to continue')
        return
      }
      // The POST response was lost before the command id arrived. Re-submit the exact stored key and
      // deterministic action payload; the server returns the same command record.
      await runTransport(pending.action, pending.idempotencyKey, {
        allowPending: true, boundGeneration: pending.expectedGeneration,
      })
      return
    }
    try {
      const record = await getRunCommand(runId, pending.record.id)
      acceptTransportRecord(pending.action, record, pending.idempotencyKey,
        pending.expectedGeneration)
    } catch (error) {
      const kind = observationKind(error)
      if (pending.protocolInvalid) {
        protocolTransportState(pending.action, pending.idempotencyKey, pending.record,
          error?.message || 'Stored command could not be verified', pending.lockIdentity,
          pending.expectedGeneration)
      } else if (['transport', 'access', 'protocol'].includes(kind)) {
        unavailableTransport(pending.action, pending.idempotencyKey, pending.expectedGeneration,
          pending.record, error)
      } else failTransport(pending.action, pending.idempotencyKey, pending.expectedGeneration,
        error, pending.record)
    }
  }
  useEffect(() => {
    const command = transportPending?.record
    if (transportPending?.statusUnavailable || transportPending?.retrying || transportPending?.checking
        || !command?.id || (command.status !== 'accepted' && command.status !== 'executing')) return
    let active = true, timer = null
    let transientFailures = 0
    const { action, idempotencyKey, expectedGeneration: boundGeneration } = transportPending
    const schedule = delay => { if (active) timer = setTimeout(poll, delay) }
    const poll = async () => {
      try {
        const record = await getRunCommand(runId, command.id)
        if (!active) return
        transientFailures = 0
        if (COMMAND_SUCCEEDED.has(record.status) || COMMAND_FAILED.has(record.status)) {
          acceptTransportRecord(action, record, idempotencyKey, boundGeneration)
          return
        }
        const entry = { action, idempotencyKey, expectedGeneration: boundGeneration,
          record, statusUnavailable: false }
        persistTransport(entry); setTransportPending(entry)
        schedule(1500)
        return
      } catch (error) {
        if (!active) return
        const kind = observationKind(error)
        if (kind === 'transport') {
          transientFailures += 1
          if (transientFailures < 3) {
            schedule(Math.max(Number(error.retryAfterMs) || 0, Math.min(6000, 750 * (2 ** transientFailures))))
            return
          }
          unavailableTransport(action, idempotencyKey, boundGeneration, command, error)
          return
        }
        if (kind === 'access' || kind === 'protocol') {
          unavailableTransport(action, idempotencyKey, boundGeneration, command, error); return
        }
        failTransport(action, idempotencyKey, boundGeneration, error, command); return
      }
    }
    timer = setTimeout(poll, 1000)
    return () => { active = false; clearTimeout(timer) }
  }, [runId, transportPending?.record?.id, transportPending?.statusUnavailable,
    transportPending?.retrying, transportPending?.checking])
  const canRetryTransport = commandCanRetry(transportFailure?.record)
  const failedCommandId = transportFailure?.record?.id
  const conflictingCommandId = transportFailure?.record?.error?.existing_command_id
  const failureCode = transportFailure?.record?.error?.code
  const failureHeading = !transportFailure ? ''
    : failureCode === 'owner_access_required' ? 'Owner access required'
      : failureCode === 'command_protocol_error' ? 'Invalid command response'
        : transportFailure.record?.status === 'rejected' ? `${transportLabels(transportFailure.action).failure} — rejected`
          : transportFailure.action === 'finalize' && canRetryTransport ? 'Finalization stalled'
            : transportLabels(transportFailure.action).failure
  const dismissTransportFailure = () => {
    clearRunTransport(runId); setTransportFailure(null)
  }
  const dismissProtocolTransport = () => {
    const pending = transportPending
    if (!pending?.protocolInvalid) return
    clearRunTransport(runId)
    const identity = pending.lockIdentity || {
      source: 'dock', idempotencyKey: pending.idempotencyKey,
      action: pending.action, expectedGeneration: pending.expectedGeneration,
      commandId: pending.record?.id || '',
    }
    clearRunCommandLock(runId, identity)
    setTransportPending(null)
  }
  const onReplay = async () => {
    if (!window.confirm('Reset this run? Wipes all events & nodes and restarts from scratch.')) return
    try { await resetRun(runId); onToast?.('replaying from scratch') } catch (e) { onToast?.('reset failed: ' + e.message) }
  }
  // Publish only from the committed layout and bind the callable to this exact run generation. A
  // functional identity cleanup prevents an old StrictMode/unmount cleanup from erasing a newer
  // controller. The parent also receives busy/failure reactively, so a prominent canvas recovery CTA
  // cannot look enabled while Dock is preserving or observing a command.
  useLayoutEffect(() => {
    if (!publishTransport || readOnly) return undefined
    const controller = Object.freeze({
      runId, expectedGeneration, busy: transportBusy,
      pendingAction: transportPending?.action || externalTransportPending?.action || null,
      failure: !!transportFailure,
      invoke: action => {
        if (action !== 'resume' && action !== 'finalize') return undefined
        return runTransport(action)
      },
    })
    publishTransport(controller)
    return () => publishTransport(current => current === controller ? null : current)
  }, [publishTransport, readOnly, runId, expectedGeneration, transportBusy,
    transportPending?.action, externalTransportPending?.action, !!transportFailure])
  return (
    <div className="dock chat-dock">
      <div className="dock-tabs">
        <span className="chat-label"><OpIcon name="flag" size={14} /> events &amp; timeline</span>
        {/* clickable so the user can return to live even when the controls (with the Live button) are hidden */}
        <button type="button" className={'hist-tag-mini ' + (visiblyLive ? 'live' : 'hist')}
              onClick={returnToLive} disabled={visiblyLive}
              title={visiblyLive ? '' : 'jump to the newest verified events'}>
          {atLiveView
            ? visiblyLive ? `live · ${liveSeq}` : 'reading · jump latest'
            : `replay ${sliderVal}/${liveSeq} → live`}</button>
        {/* a left-over filter is invisible once controls collapse — surface it so the feed never looks empty for no reason */}
        {!showControls && (filter.trim() || kinds.size > 0) &&
          <button type="button" className="hist-tag-mini hist"
                title="a filter is active — open controls to change it" onClick={toggleControls}>⌕ filtered</button>}
        <span className="spacer" style={{ flex: 1 }} />
        <button className={'btn sm ghost' + (showControls ? ' on' : '')} title="time-travel & filters"
                onClick={toggleControls}><OpIcon name="sliders" size={13} /> controls</button>
        <button ref={collapseButtonRef} className="btn sm ghost dock-collapse" title={collapsed ? 'expand' : 'collapse'}
                aria-label={collapsed ? 'Expand events and timeline' : 'Collapse events and timeline'}
                aria-expanded={!collapsed} aria-controls="run-events-timeline"
                onClick={onToggleCollapse}><OpIcon name={collapsed ? 'chevron-up' : 'chevron-down'} size={13} /></button>
      </div>
      {!collapsed && <div id="run-events-timeline" className="dock-body chat-body" style={{ height }}>
        {showControls && <div className="dock-controls">
          <div className="scrubber inline">
            <button className="btn sm" onClick={returnToLive} disabled={drag == null && visiblyLive}><OpIcon name="play" size={11} /> Live</button>
            <input type="range" min={0} max={Math.max(0, liveSeq)} value={sliderVal}
                   aria-label="Timeline sequence" aria-valuetext={sliderVal >= liveSeq ? `live at ${liveSeq}` : `replay ${sliderVal} of ${liveSeq}`}
                   onChange={e => onScrub(Number(e.target.value))}
                   onPointerUp={endScrub} onMouseUp={endScrub} onKeyUp={endScrub} onBlur={endScrub} />
            <span className={(sliderVal >= liveSeq) ? 'live-tag' : 'hist-tag'}>{(sliderVal >= liveSeq) ? `live · ${liveSeq}` : `replay · ${sliderVal}/${liveSeq}`}</span>
          </div>
          <div className="kind-chips">
            {GROUPS.map(([g, label]) => <button key={g}
              className={'kind-chip k-' + g + (kinds.has(g) ? ' on' : '')} aria-pressed={kinds.has(g)}
              onClick={() => toggleKind(g)}>
              <OpIcon name={GROUP_GLYPH[g]} size={12} /> {label}</button>)}
            {kinds.size > 0 && <button className="kind-chip clear" onClick={() => setKinds(new Set())}>clear</button>}
            <input className="text feed-filter" aria-label="Filter loaded timeline events" placeholder="filter loaded events…" value={filter} onChange={e => onFilterChange?.(e.target.value)} />
          </div>
        </div>}
        <div className="timeline-pagebar">
          <button type="button" className="btn sm ghost" disabled={!timeline.hasMore.older || timeline.loading.older}
            onClick={timeline.loadOlder}>{timeline.loading.older ? 'Loading…' : 'Load older'}</button>
          <span className="muted">
            {timeline.totalEvents != null ? `${log.length} loaded of ${timeline.totalEvents}` : `${log.length} loaded`}
            {(filter.trim() || kinds.size) ? ` · ${feed.length} matching loaded events` : ''}
          </span>
          {timeline.hasMore.newer && <button type="button" className="btn sm ghost"
            disabled={timeline.loading.newer} onClick={timeline.loadNewer}>
            {timeline.loading.newer ? 'Loading…' : 'Load newer'}</button>}
        </div>
        {(filter.trim() !== '' || kinds.size > 0) && timeline.totalEvents != null && timeline.totalEvents > log.length &&
          <div className="timeline-window-note" role="note">Filter searches this loaded window. Page older/newer to inspect other events.</div>}
        {timeline.status === 'loading' && log.length === 0 && <div className="timeline-resource muted" role="status">Loading timeline…</div>}
        {timeline.loading.around && <div className="timeline-resource muted" role="status">Loading replay events around seq {viewSeq}…</div>}
        {timeline.errors.tail && <div className="notice resource-error" role="alert">
          <span>{log.length ? 'Could not refresh the newest events; the loaded window is unchanged.' : timeline.errors.tail}</span>
          <button className="btn sm" onClick={() => timeline.retry('tail')}>Retry</button></div>}
        {timeline.errors.older && <div className="notice resource-error compact" role="alert">
          <span>Could not load older events; the current window is unchanged.</span><button className="btn sm" onClick={timeline.loadOlder}>Retry</button></div>}
        {timeline.errors.newer && <div className="notice resource-error compact" role="alert">
          <span>Live event refresh failed; loaded events may be behind.</span><button className="btn sm" onClick={timeline.loadNewer}>Retry</button></div>}
        {timeline.errors.around && <div className="notice resource-error compact" role="alert">
          <span>Could not load events around replay seq {viewSeq}; the current window is unchanged.</span>
          <button className="btn sm" onClick={() => timeline.retry('around')}>Retry</button></div>}
        {timeline.tornTail && <div className="timeline-window-note warning" role="status">
          {timeline.sourceTailLimited
            ? 'The raw log tail exceeds the safety limit; showing the last verified canonical prefix.'
            : 'The final source row is incomplete or non-canonical; showing the last verified event prefix.'}
        </div>}
        {feed.length === 0 && timeline.status === 'ready' && !timeline.loading.around
          ? <div className="timeline-resource muted">{(filter.trim() || kinds.size) ? 'nothing matches the loaded window' : 'no events yet'}</div>
          : <VirtualTimeline rows={feed} getKey={timelineEventKey}
              identity={`${runId}:${timeline.generation || 'pending'}`}
              className="feed chat-feed" ariaLabel="Run events"
              followingTail={atLiveView && timeline.followingTail}
              windowAtTail={atLiveView && timeline.windowAtTail}
              unread={atLiveView ? timeline.unread : 0}
              unreadUnknown={atLiveView && timeline.unreadUnknown}
              busy={Object.values(timeline.loading).some(Boolean)}
              onFollowingTailChange={value => { if (atLiveView) timeline.setFollowingTail(value) }}
              onJumpToLive={returnToLive}
              renderRow={event => {
                const key = timelineEventKey(event)
                return <EventRow e={event} onFocusEvent={focusEvent} runId={runId}
                  readOnly={readOnly} liveBuilding={liveBuilding} autoOpen={false}
                  expansion={eventExpansion.get(key) || CLOSED_EXPANSION}
                  onExpansionChange={next => setEventExpansion(current => {
                    const updated = new Map(current); updated.set(key, next); return updated
                  })} />
              }} />}
        {!showControls && (() => {
          const pipeline = atLiveView ? agentStatus(live, log) : null
          if (!pipeline) return null
          return <div className="agent-status dock-agent-status">
            <div className="as-line"><span className="as-dot" /><span className="as-seg">{pipeline}</span></div>
            <LiveTrace runId={runId} active={atLiveView} />
          </div>
        })()}
        <div className="dock-foot">
          <span className="muted" style={{ fontSize: 11, flex: 1 }}>
            {readOnly ? 'Historical timeline — live controls and sidecar trace details are disabled.' : <>
              Steer this run from the assistant bar below — say what to do, or use <code className="cmd-hint">/stop</code> · <code className="cmd-hint">/finalize</code> · <code className="cmd-hint">/resume</code> · <code className="cmd-hint">/approve #id</code>.
            </>}
          </span>
          {!readOnly && <div className="transport">
            {transportPending && <div className={'transport-message' + (transportPending.statusUnavailable ? ' warning' : '')}
              role={transportPending.statusUnavailable ? 'alert' : 'status'}>
              <span>
                {transportPending.statusUnavailable
                  ? transportPending.observationKind === 'access' ? 'Owner access required to check command status'
                    : transportPending.observationKind === 'protocol' ? 'Invalid command status response'
                      : 'Command status unavailable — the same intent is preserved'
                  : transportPending.checking ? 'Checking the same command…'
                    : transportPending.retrying ? 'Retrying the same command…'
                      : transportPending.record?.status === 'submitting'
                        ? `Submitting ${transportPending.action}…`
                        : transportPending.action === 'finalize' ? 'Finalizing…'
                          : transportPending.action === 'stop' ? 'Stop requested…' : 'Resume requested…'}
                {transportPending.record?.id
                  ? <span className="transport-command-id" title={`Command ${transportPending.record.id}`}>
                      {' · '}{String(transportPending.record.id).slice(0, 12)}…</span> : null}
              </span>
              {transportPending.statusUnavailable && <>
                {transportPending.lastError && <span className="transport-detail">{transportPending.lastError}</span>}
                <button className="btn sm" onClick={onCheckTransport}
                  aria-label={`Check status of the preserved ${transportPending.action} command`}>
                  Check same command</button>
                {transportPending.protocolInvalid && <button className="btn sm ghost"
                  onClick={dismissProtocolTransport}>Dismiss</button>}
              </>}
            </div>}
            {!transportPending && externalTransportPending && <div className="transport-message" role="status"
              aria-live="polite" aria-atomic="true">
              <span>/{externalTransportPending.action} is pending in Assistant</span>
              {externalTransportPending.commandId && <span className="transport-command-id"
                title={`Command ${externalTransportPending.commandId}`}>
                {' · '}{String(externalTransportPending.commandId).slice(0, 12)}…</span>}
            </div>}
            {!transportBusy && transportFailure && <>
              <div className="transport-message error" role="alert">
                <span>{failureHeading}{failedCommandId
                  ? <span className="transport-command-id" title={`Command ${failedCommandId}`}>
                      {' · '}{String(failedCommandId).slice(0, 12)}…</span> : null}
                  {!failedCommandId && conflictingCommandId
                    ? <span className="transport-command-id" title={`Conflicting active command ${conflictingCommandId}`}>
                        {' · active '}{String(conflictingCommandId).slice(0, 12)}…</span> : null}</span>
                <span className="transport-detail">{commandErrorMessage(transportFailure.record)}</span>
              </div>
              {canRetryTransport && <button className="btn sm" onClick={onRetryTransport}
                title="Retry this exact durable command; no new intent is created">Retry same command</button>}
              <button className="btn sm ghost" onClick={dismissTransportFailure}
                title="Dismiss this command result">Dismiss</button>
            </>}
            {!transportBusy && !transportFailure && mode === 'finalizing' && <span className="muted" role="status">Finalizing…</span>}
            {!transportBusy && !transportFailure && mode === 'finishing' && <span className="muted" role="status">Finishing terminal write-out…</span>}
            {!transportBusy && !transportFailure && mode === 'finalization-stalled' && <>
              <span className="muted" role="alert">Finalization stalled</span>
              <button className="btn sm" onClick={onFinalize}
                title="The engine stopped before wrap-up completed; safely reattach to pending finalization">Reattach finalization</button>
            </>}
            {!transportBusy && !transportFailure && mode === 'running' && <>
              <button className="btn sm" aria-label="Stop run without finalizing"
                title="Stop — freeze the run (no wrap-up; resume or finalize later)" onClick={onStop}><OpIcon name="pause" size={13} /></button>
              <button className="btn sm danger" aria-label="Finalize run"
                title="Finalize — stop AND wrap up (report, cross-run lessons, cost)" onClick={onFinalize}><OpIcon name="stop" size={13} /></button></>}
            {!transportBusy && !transportFailure && (mode === 'paused' || mode === 'stalled') && <>
              <button className="btn sm primary" aria-label="Resume run" title="Resume — continue the run" onClick={onResume}><OpIcon name="play" size={13} /></button>
              <button className="btn sm danger" aria-label="Finalize run"
                title="Finalize — wrap it up now (report, cross-run lessons, cost)" onClick={onFinalize}><OpIcon name="stop" size={13} /></button></>}
            {!transportBusy && !transportFailure && mode === 'finished' && <>
              <button className="btn sm primary" aria-label="Resume finished run" title="Resume — reopen & continue" onClick={onResume}><OpIcon name="play" size={13} /></button>
              <button className="btn sm" aria-label="Replay run from scratch" title="reset & restart from scratch" onClick={onReplay}><OpIcon name="replay" size={13} /></button></>}
          </div>}
        </div>
      </div>}
    </div>
  )
}
