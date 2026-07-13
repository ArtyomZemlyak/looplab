import React, { useEffect, useMemo, useRef, useState } from 'react'
import { get, fmt, workingId, resetRun, getRunCommand, retryRunCommand, runCommand,
  commandFeedback, commandErrorMessage, commandFailureRecord, commandCanRetry, createIdempotencyKey,
  commandActionForEvent, commandRecordMatchesAction, commandEventForAction,
  loadRunTransport, saveRunTransport, clearRunTransport, isTransientCommandReadError,
  clearRunCommandLock, loadRunCommandLock, saveRunCommandLock, subscribeRunCommandLock,
  COMMAND_SUCCEEDED, COMMAND_FAILED, storageGet, storageSet } from './util.js'
import { usePoll } from './hooks.js'
import Markdown from './markdown.jsx'
import { NodeTrace } from './Inspector.jsx'
import { OpIcon } from './icons.jsx'
import { runLifecycle } from './runIndex.js'


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
// Live agent trace: a collapsed disclosure under the "Thinking…/Planning…" status that streams the
// most recent LLM thoughts + tool calls (with args), so you can see WHAT the agent is doing, not just
// a coarse label. Polls /trace/tail only while OPEN + live (cheap when collapsed). Server-side the feed
// is bounded (tail of spans.jsonl); full I/O of any observation is at /spans/{sid}.
function LiveTrace({ runId, active }) {
  const [tail, setTail] = useState([])
  const [open, setOpen] = useState(false)
  const bodyRef = useRef(null)
  usePoll((alive) => get(`/api/runs/${runId}/trace/tail?limit=40`)
    .then(r => { if (alive()) setTail(r.tail || []) }).catch(() => {}),
    3000, [runId, active, open], { enabled: active && open })
  useEffect(() => { if (open && bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight }, [tail, open])
  return (
    <div className={'live-trace' + (open ? ' open' : '')}>
      {/* Standard inline disclosure — caret ▸ left of the label, expands IN PLACE (not a popup). */}
      <div className="lt-toggle" onClick={() => setOpen(o => !o)}
           title="stream the agent's thoughts + tool calls">
        <span className="lt-caret">{open ? '▾' : '▸'}</span>trace
      </div>
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

// The trace of ONE sub-operation (strategy_consult / hypothesis_merge …), fetched lazily by the
// event's own trace_id — so a strategy_decision row shows only the strategist's reasoning, not the
// whole node. Rendered with the same span-tree component as a node's trace.
function OpTrace({ runId, traceId }) {
  const [spans, setSpans] = useState(null)
  useEffect(() => {
    let on = true
    setSpans(null)
    get(`/api/runs/${runId}/trace/by_trace/${traceId}`)
      .then(d => on && setSpans(d?.spans || [])).catch(() => on && setSpans([]))
    return () => { on = false }
  }, [runId, traceId])
  if (spans === null) return <div className="muted" style={{ fontSize: 12, padding: '4px 2px' }}>loading trace…</div>
  if (!spans.length) return <div className="muted" style={{ fontSize: 12, padding: '4px 2px' }}>no trace captured for this step</div>
  return <NodeTrace spans={spans} runId={runId} />
}

// One feed row, chat-message styled: an icon/color by kind, the narration, an expandable "why" card.
function EventRow({ e, trace, onFocusEvent, autoOpen, runId, readOnly = false }) {
  const [open, setOpen] = useState(autoOpen)
  const touched = useRef(false)   // once the user toggles, stop auto-following the live frontier
  // collapse-when-done: follow autoOpen (expand while live, collapse when the node resolves) UNLESS
  // the user manually toggled this card — then their choice wins.
  useEffect(() => { if (!touched.current) setOpen(autoOpen) }, [autoOpen])
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
  const nodeSpans = traceNid != null ? (trace?.nodes || {})[String(traceNid)] : null
  const hasTrace = !!(nodeSpans && nodeSpans.length)
  // A sub-operation event the engine wrapped in its OWN named trace (strategy_decision, hypothesis_
  // merged) carries a trace_id — expand to ONLY that operation's trace (lazily fetched by trace_id),
  // never the node's whole Researcher+Developer trace. Old events (no trace_id) fall through to detail.
  const opTraceId = (!readOnly && OP_TRACE_TYPES.has(e.type) && e.trace_id) ? e.trace_id : null
  // no-truncation: a row whose one-line narration clamped text (or used the raw JSON fallback) is
  // expandable to its FULL content even without a dedicated reasoning card.
  const isRawFallback = !hasReason && !NARR[e.type]
  const hasGeneric = !hasReason && (genericRows(e).length > 0 || isRawFallback)
  const expandable = hasReason || hasTrace || !!opTraceId || hasGeneric
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
export default function Dock({ runId, live, liveSeq, viewSeq, setViewSeq, onFocus, collapsed, onToggleCollapse, height = 230, onToast, readOnly = false }) {
  const [log, setLog] = useState([])
  const [logStatus, setLogStatus] = useState('loading')
  const [logNonce, setLogNonce] = useState(0)
  const [trace, setTrace] = useState(null)
  const [filter, setFilter] = useState('')
  const [kinds, setKinds] = useState(() => new Set())     // selected kind chips (empty = all)
  const restoredRef = useRef(null)
  if (!restoredRef.current || restoredRef.current.runId !== runId) {
    restoredRef.current = { runId, ...recoveryForRun(runId) }
  }
  const [transportPending, setTransportPending] = useState(() => restoredRef.current.pending)
  const [transportFailure, setTransportFailure] = useState(() => restoredRef.current.failure)
  const [runCommandLock, setRunCommandLock] = useState(() => loadRunCommandLock(runId))
  const externalTransportPending = runCommandLock?.source === 'assistant' ? runCommandLock : null
  const transportBusy = !!transportPending || !!externalTransportPending
  const feedRef = useRef(null)
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
          action: lock.action, commandId: lock.commandId })
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
    let alive = true
    if (!log.length) setLogStatus('loading')
    get(`/api/runs/${runId}/log`)
      .then(d => { if (alive) { setLog(d); setLogStatus('ready') } })
      .catch(() => { if (alive) setLogStatus('error') })
    return () => { alive = false }
  }, [runId, liveSeq, logNonce])
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
  const inSetup = !!live && !live.finished && live.engine_running !== false && nodeCount === 0
  useEffect(() => {
    if (readOnly) { setTrace(null); return }
    get(`/api/runs/${runId}/trace`).then(setTrace).catch(() => {})
  }, [runId, nodeCount, settledCount, live?.finished, inSetup ? liveSeq : 0, readOnly])
  // While a node is BUILDING, its spans stream in but nodeCount/settledCount don't change — so the
  // fetch above never re-runs and the building node's Trace stays FROZEN (you see spans, but they never
  // grow). Poll the trace while a node builds so its Trace tab/row fills in live; the poll stops the
  // moment building ends (node_created clears `building`), and the fetch above does the final refresh.
  const buildingId = live && !live.finished && live.engine_running !== false && live.building
    ? live.building.node_id : null
  usePoll(() => get(`/api/runs/${runId}/trace`).then(setTrace).catch(() => {}),
    4000, [runId, buildingId, readOnly], { enabled: !readOnly && buildingId != null })
  const atLive = viewSeq == null || viewSeq >= liveSeq

  // The live frontier: the highest-id node still pending while the run runs — its proposal card stays
  // expanded ("thinking") until it resolves. null on a finished/replayed run — AND on a STALLED/zombie
  // run (engine_running===false): a run whose engine died mid-eval leaves a node stuck 'pending', and
  // without this guard its node_created row would auto-expand and dump the full span trace forever.
  const livePendingId = useMemo(() => {
    if (!live || live.finished || live.engine_running === false) return null
    const pend = Object.values(live.nodes || {}).filter(n => n.status === 'pending').map(n => n.id)
    return pend.length ? Math.max(...pend) : null
  }, [live])

  // Scrubber: the thumb tracks a LOCAL value (instant) while the history fetch (re-folds + re-lays the
  // DAG) is throttled to ~11fps. `drag == null` means "follow the committed seq".
  const [drag, setDrag] = useState(null)
  const thr = useRef({ last: 0, timer: null })
  useEffect(() => () => clearTimeout(thr.current.timer), [])
  useEffect(() => {
    if (viewSeq == null) {
      clearTimeout(thr.current.timer)
      thr.current.timer = null
      setDrag(null)
    }
  }, [viewSeq])
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
  const protocolTransportState = (action, idempotencyKey, record, message, lockIdentity = null) => {
    const commandId = /^cmd_[0-9a-f]{32}$/.test(String(record?.id || '')) ? String(record.id) : ''
    const entry = { action: action || 'unknown', idempotencyKey,
      record: commandId ? { id: commandId, status: 'accepted' } : { status: 'submitting' },
      statusUnavailable: true, observationKind: 'protocol', protocolInvalid: true,
      canResubmit: false, lastError: message, lockIdentity }
    saveRunCommandLock(runId, { ...entry, source: 'dock' })
    setTransportPending(entry); setTransportFailure(null)
    return entry
  }
  const storageTransportFailure = (action, idempotencyKey) => {
    const record = { status: 'rejected', error: {
      code: 'command_storage_unavailable',
      message: 'The command was not sent because durable tab storage is unavailable.',
      remediation: 'Enable session storage or free browser storage, then try again.', retryable: false,
    } }
    const entry = { action, idempotencyKey, record }
    setTransportPending(null); setTransportFailure(entry)
    onToast?.('Command not sent — durable recovery storage is unavailable')
    return entry
  }
  const acceptTransportRecord = (action, record, idempotencyKey) => {
    const pendingState = transportPending
    const actualAction = verifiedTransportAction(action, record, pendingState?.protocolInvalid)
    if (!actualAction) return protocolTransportState(action, idempotencyKey, record,
      'Command identity does not match the requested action', pendingState?.lockIdentity)
    if (pendingState?.protocolInvalid) {
      const identity = pendingState.lockIdentity || {
        source: 'dock', idempotencyKey: pendingState.idempotencyKey,
        action: pendingState.action, commandId: pendingState.record?.id || '',
      }
      clearRunCommandLock(runId, identity)
    }
    const feedback = commandFeedback(record, transportLabels(actualAction))
    onToast?.(feedback.message)
    if (feedback.kind === 'pending') {
      const entry = { action: actualAction, idempotencyKey, record, statusUnavailable: false }
      if (!persistTransport(entry)) {
        return protocolTransportState(actualAction, idempotencyKey, record,
          'Command accepted, but its updated durable status could not be stored')
      }
      setTransportPending(entry)
      setTransportFailure(null)
    } else {
      setTransportPending(null)
      if (feedback.kind === 'error') {
        const entry = { action: actualAction, idempotencyKey, record }
        if (!persistTransport(entry)) clearRunTransport(runId)
        setTransportFailure(entry)
      } else {
        clearRunTransport(runId); setTransportFailure(null)
      }
    }
  }
  const unavailableTransport = (action, idempotencyKey, record, error, extra = {}) => {
    const kind = observationKind(error)
    let recoveryRecord = record || { status: 'submitting' }
    if (recoveryRecord.id && !recoveryRecord.event_type && TRANSPORT_INTENTS[action]) {
      recoveryRecord = { ...recoveryRecord, event_type: commandEventForAction(action, 'dock') }
    }
    const entry = {
      action, idempotencyKey, record: recoveryRecord,
      statusUnavailable: true, observationKind: kind,
      lastError: error?.message || String(error), ...extra,
    }
    if (!persistTransport(entry)) saveRunCommandLock(runId, { ...entry, source: 'dock' })
    setTransportPending(entry); setTransportFailure(null)
    return entry
  }
  const failTransport = (action, idempotencyKey, error, previous = null) => {
    const record = commandFailureRecord(error, previous)
    const entry = { action, idempotencyKey, record }
    if (!persistTransport(entry)) clearRunTransport(runId)
    setTransportPending(null); setTransportFailure(entry)
    onToast?.(commandFeedback(record, transportLabels(action)).message)
  }
  const runTransport = async (action, idempotencyKey = createIdempotencyKey(), { allowPending = false } = {}) => {
    if (!allowPending && (transportPending || loadRunCommandLock(runId))) return
    const intent = TRANSPORT_INTENTS[action]
    if (!intent) {
      protocolTransportState(action, idempotencyKey, transportPending?.record,
        'Stored command identity cannot be safely replayed', transportPending?.lockIdentity)
      return
    }
    const start = { action, idempotencyKey, record: { status: 'submitting' } }
    if (!persistTransport(start)) { storageTransportFailure(action, idempotencyKey); return }
    setTransportPending(start)
    setTransportFailure(null)
    try {
      const record = await runCommand(runId, intent.type, intent.data, {
        idempotencyKey, waitMs: 0,
        onRecord: next => {
          const visible = { action, idempotencyKey, record: next, statusUnavailable: false }
          if (!persistTransport(visible)) return
          setTransportPending(current => current?.action === action && current?.idempotencyKey === idempotencyKey
            ? visible : current)
        },
      })
      acceptTransportRecord(action, record, idempotencyKey)
    } catch (error) {
      const record = error?.commandRecord || (error?.commandId
        ? { id: error.commandId, status: 'accepted' } : null)
      const kind = observationKind(error)
      if (error?.commandUnknown || (record?.id && ['transport', 'access', 'protocol'].includes(kind))) {
        unavailableTransport(action, idempotencyKey, record, error)
        onToast?.(`${transportLabels(action).failure}: command status unavailable; the same intent was preserved`)
      } else failTransport(action, idempotencyKey, error, record)
    }
  }
  const onStop = () => runTransport('stop')
  const onFinalize = () => runTransport('finalize')
  const onResume = () => runTransport('resume')
  const onRetryTransport = async () => {
    const failure = transportFailure
    if (transportBusy || loadRunCommandLock(runId) || !commandCanRetry(failure?.record)) return
    const { action, record, idempotencyKey } = failure
    const retrying = { action, idempotencyKey, record, retrying: true }
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
          const visible = { action, idempotencyKey, record: value, retrying: true }
          persistTransport(visible)
          setTransportPending(current => current?.action === action && current?.idempotencyKey === idempotencyKey
            ? visible : current)
        },
      })
      acceptTransportRecord(action, next, idempotencyKey)
    } catch (error) {
      const kind = observationKind(error)
      if (['transport', 'access', 'protocol'].includes(kind)) {
        unavailableTransport(action, idempotencyKey, error?.commandRecord || record, error)
      } else failTransport(action, idempotencyKey, error, error?.commandRecord || record)
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
      await runTransport(pending.action, pending.idempotencyKey, { allowPending: true })
      return
    }
    try {
      const record = await getRunCommand(runId, pending.record.id)
      acceptTransportRecord(pending.action, record, pending.idempotencyKey)
    } catch (error) {
      const kind = observationKind(error)
      if (pending.protocolInvalid) {
        protocolTransportState(pending.action, pending.idempotencyKey, pending.record,
          error?.message || 'Stored command could not be verified', pending.lockIdentity)
      } else if (['transport', 'access', 'protocol'].includes(kind)) {
        unavailableTransport(pending.action, pending.idempotencyKey, pending.record, error)
      } else failTransport(pending.action, pending.idempotencyKey, error, pending.record)
    }
  }
  useEffect(() => {
    const command = transportPending?.record
    if (transportPending?.statusUnavailable || transportPending?.retrying || transportPending?.checking
        || !command?.id || (command.status !== 'accepted' && command.status !== 'executing')) return
    let active = true, timer = null
    let transientFailures = 0
    const { action, idempotencyKey } = transportPending
    const schedule = delay => { if (active) timer = setTimeout(poll, delay) }
    const poll = async () => {
      try {
        const record = await getRunCommand(runId, command.id)
        if (!active) return
        transientFailures = 0
        if (COMMAND_SUCCEEDED.has(record.status) || COMMAND_FAILED.has(record.status)) {
          acceptTransportRecord(action, record, idempotencyKey)
          return
        }
        const entry = { action, idempotencyKey, record, statusUnavailable: false }
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
          unavailableTransport(action, idempotencyKey, command, error)
          return
        }
        if (kind === 'access' || kind === 'protocol') {
          unavailableTransport(action, idempotencyKey, command, error); return
        }
        failTransport(action, idempotencyKey, error, command); return
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
      action: pending.action, commandId: pending.record?.id || '',
    }
    clearRunCommandLock(runId, identity)
    setTransportPending(null)
  }
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
                aria-label={collapsed ? 'Expand events and timeline' : 'Collapse events and timeline'}
                aria-expanded={!collapsed} aria-controls="run-events-timeline"
                onClick={onToggleCollapse}><OpIcon name={collapsed ? 'chevron-up' : 'chevron-down'} size={13} /></button>
      </div>
      {!collapsed && <div id="run-events-timeline" className="dock-body chat-body" style={{ height }}>
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
          {logStatus === 'loading' && log.length === 0 && <div className="muted" role="status">Loading timeline…</div>}
          {logStatus === 'error' && <div className="notice resource-error" role="alert"><span>{log.length ? 'Could not refresh the timeline; showing the last loaded events.' : 'Could not load the timeline.'}</span><button className="btn sm" onClick={() => setLogNonce(n => n + 1)}>Retry</button></div>}
          {feed.length === 0 && logStatus !== 'loading'
            ? <div className="muted">{(filter || kinds.size) ? 'nothing matches the filter' : 'no events yet'}</div>
            : feed.length > 0 ? feed.map(e =>
                <EventRow key={'e' + e.seq} e={e} trace={readOnly ? null : trace} onFocusEvent={focusEvent} runId={runId}
                    readOnly={readOnly}
                    autoOpen={false} />) : null}
          {(() => {
            // A short, honest "what's the agent doing now" strip at the foot of the feed.
            const pipeline = atLive ? agentStatus(live, log) : null
            if (!pipeline) return null
            return <div className="agent-status">
              <div className="as-line">
                <span className="as-dot" />
                <span className="as-seg">{pipeline}</span>
              </div>
              <LiveTrace runId={runId} active={atLive} />
            </div>
          })()}
        </div>
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
