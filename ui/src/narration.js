import { durationLabel, fmt, fmtCost, NODE_ACTIVITY, nodeActivityStatus } from './util.js'
import { stripMd } from './markdown.jsx'
import { crossRunPriorNarration } from './crossRunPrior.js'
import { evalStageFor, evalStageLabel, evalStageShortLabel, evalStages, livePhase } from './buildingModel.js'

// The event-feed NARRATION MODEL: pure data + pure functions that turn one raw run event into the
// line a human reads, the kind chip it files under, and the answer to "does this type belong in the
// curated feed at all". It lives beside Dock rather than inside it because none of it is a component
// — Dock only renders what this module decides — and because a table this size earns direct tests.
// One ENTRY PER EVENT TYPE carries both halves ({validate?, render}): the render/validate pair used
// to be two parallel objects keyed by the same types, which is a shape that only ever drifts (a
// validator keyed to a type with no renderer is silently dead — `eventNarration` consults it only
// when a renderer exists). doc 25 UI-08.
const note = (value, limit = 80) => value ? ` — ${String(value).slice(0, limit)}` : ''

const strategySummary = (strategy = {}) => {
  const s = strategy && typeof strategy === 'object' && !Array.isArray(strategy) ? strategy : {}
  const bits = []
  if (s.policy) bits.push(s.policy + (s.fidelity ? '/' + s.fidelity : ''))
  else if (s.fidelity) bits.push(s.fidelity)
  if (s.eval_parallel != null) bits.push(`eval=${s.eval_parallel}`)
  if (s.llm_parallel != null) bits.push(`LLM=${s.llm_parallel}`)
  const lanes = s.llm_lane_limits
  if (lanes && typeof lanes === 'object' && !Array.isArray(lanes)) {
    bits.push(`lanes ${Object.entries(lanes).slice(0, 5)
      .map(([lane, width]) => `${lane}:${width}`).join(',')}`)
  }
  const cardScoring = s.card_scoring
  if (cardScoring && typeof cardScoring === 'object' && !Array.isArray(cardScoring)) {
    bits.push(`cards ${cardScoring.stance || 'balanced'} n:${cardScoring.novelty_weight} c:${cardScoring.coverage_weight}`)
  }
  return bits.join(' / ') || 'no change'
}

const ownValue = (value, key) => value !== null && typeof value === 'object'
  && Object.hasOwn(value, key) && value[key] !== null && value[key] !== undefined
const ownAny = (value, keys) => keys.some(key => ownValue(value, key))
const nestedValue = (value, parent, key) => ownValue(value, parent) && ownValue(value[parent], key)
const objectValue = (value, key) => ownValue(value, key) && value[key] !== null
  && typeof value[key] === 'object' && !Array.isArray(value[key])
// Narration is a compatibility surface over append-only logs. Optional fields may enrich a line,
// but fields that define the claim itself must be present before a renderer interpolates them. This
// avoids coercing missing legacy/partial values into authoritative-looking "#undefined", "clean",
// or "FAILED (exit undefined)" prose while preserving legitimate user text containing that word.
// That is what an entry's `validate` is for; an entry without one accepts any object payload.
export const NARR = {
  run_started: {
    validate: d => ownAny(d, ['goal', 'task_id']) && ownValue(d, 'direction'),
    render: (d) => `run started — ${d.goal || d.task_id} (${d.direction})`,
  },
  node_building: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `building node #${d.node_id} via ${d.operator || 'improve'}…`,
  },
  // The live-progress beacon. It IS in the curated feed (an entry here is what `isCuratedType`
  // consults) because "every step has its own visible signal" is the whole point — and it is in
  // STATUS_NOISE at the same time, which is not a contradiction: NOISE governs what may RESTART the
  // status clock and what the strip's last-meaningful-event fallback keys on, not what a reader may
  // see. The two sets have always been independent; `node_building` is likewise in both.
  phase_progress: {
    validate: d => ownValue(d, 'stage') && ownValue(d, 'phase') && ownValue(d, 'status'),
    render: (d) => {
      const where = ownValue(d, 'node_id') ? ` #${d.node_id}` : ''
      // The duration is the reason a FINISHED beacon is worth a feed row at all — it is the only
      // place a completed step's cost is stated in words, and "propose took 11 minutes" is the fact
      // an operator scrolling back is looking for.
      const took = d.status === 'finished' && Number.isFinite(Number(d.seconds))
        ? ` — ${Number(d.seconds).toFixed(1)}s` : ''
      return `${d.stage} ${d.phase}${where} ${d.status}${took}`
    },
  },
  node_created: {
    validate: d => ownValue(d, 'node_id') && ownValue(d, 'operator'),
    render: (d) => `node #${d.node_id} via ${d.operator}${d.idea?.rationale ? ' — ' + stripMd(d.idea.rationale).slice(0, 80) : ''}`,
  },
  node_evaluated: {
    validate: d => ownValue(d, 'node_id') && ownValue(d, 'metric'),
    render: (d) => `node #${d.node_id} → ${fmt(d.metric)}`,
  },
  node_eval_started: { render: (d) => `node #${d.node_id} started evaluating` },
  node_failed: {
    validate: d => ownValue(d, 'node_id') && ownValue(d, 'reason'),
    render: (d) => `node #${d.node_id} failed (${d.reason})${d.triage_action === 'reject_idea' ? ' — idea rejected' + (d.triage_rationale ? ': ' + String(d.triage_rationale).slice(0, 70) : '') : ''}`,
  },
  node_repaired: {
    validate: d => ownValue(d, 'node_id') && ownValue(d, 'attempt'),
    render: (d) => `node #${d.node_id} repaired (attempt ${d.attempt})${note(d.rationale)}`,
  },
  node_confirmed: {
    validate: d => ['node_id', 'mean', 'std', 'seeds'].every(key => ownValue(d, key)),
    render: (d) => `node #${d.node_id} confirmed: ${fmt(d.mean)} ±${fmt(d.std)} (${d.seeds}×)`,
  },
  best_confirmed: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `robust winner: #${d.node_id}${d.significant ? ' (significant >1SE)' : ''}`,
  },
  ablate: {
    validate: d => ownValue(d, 'parent_id') && objectValue(d, 'impacts'),
    render: (d) => `ablated #${d.parent_id}: ${Object.entries(d.impacts || {}).map(([k, v]) => `${k}=${fmt(v, 2)}`).join(', ')}`,
  },
  data_leakage: {
    validate: d => typeof d?.leak === 'boolean',
    render: (d) => `leakage scan: ${d.leak ? 'LEAK DETECTED' : 'clean'}`,
  },
  approval_requested: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `awaiting approval of #${d.node_id}`,
  },
  approval_granted: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `approved #${d.node_id}`,
  },
  pause: { render: () => 'stopped (frozen — not finalized)' },
  // (`resume` / `run_abort` are defined once, below with the richer wording — the duplicate keys here
  // were dead: the later definitions always won. arch-review §5 P3.)
  node_abort: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `stop requested for #${d.node_id}`,
  },
  budget_extend: {
    render: (d) => {
      const bits = []
      if (d.add_nodes) bits.push(`+${d.add_nodes} experiment node${d.add_nodes === 1 ? '' : 's'}`)
      if (d.max_seconds != null) bits.push(`wall-clock ${d.max_seconds}s`)
      if (d.max_eval_seconds != null) bits.push(`per-eval ${d.max_eval_seconds}s`)
      return `run budget extended — ${bits.join(', ') || 'no change'}`
    },
  },
  hint: { validate: d => ownValue(d, 'text'), render: (d) => `hint: ${d.text}` },
  promote: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `promoted #${d.node_id} → ${d.alias || 'champion'}`,
  },
  policy_decision: {
    validate: d => ownValue(d, 'chosen') && objectValue(d, 'scores'),
    render: (d) => `chose #${d.chosen}${d.reason ? ' (' + d.reason + ')' : ''} over ${Object.keys(d.scores || {}).length} candidate(s)`,
  },
  strategy_decision: {
    validate: d => nestedValue(d, 'strategy', 'policy'),
    render: (d) => `strategy → ${strategySummary(d.strategy)}${d.strategy?.rationale ? ' — ' + d.strategy.rationale.slice(0, 70) : ''}`,
  },
  rung_promoted: {
    validate: d => ownValue(d, 'rung') && Array.isArray(d?.survivors),
    render: (d) => `ASHA rung ↑${d.rung}: promoted ${(d.survivors || []).map(s => '#' + s).join(', ')}`,
  },
  set_strategy: {
    validate: d => nestedValue(d, 'strategy', 'policy'),
    render: (d) => `operator pinned strategy → ${strategySummary(d.strategy)}`,
  },
  deep_research: { render: () => 'deep research requested' },
  research_completed: { render: (d) => `deep research (${d.trigger || 'auto'})${note(d.memo?.summary)}` },
  report_generated: { render: (d) => `run report updated${note(d.content?.headline, 90)}` },
  reflection_note: { render: (d) => `memory: ${d.n_lessons || 0} lesson${(d.n_lessons || 0) === 1 ? '' : 's'}${d.n_skills ? `, ${d.n_skills} skill${d.n_skills === 1 ? '' : 's'}` : ''}${note(d.note)}` },
  proxy_scored: {
    validate: d => ownValue(d, 'node_id') && ownValue(d, 'score'),
    render: (d) => `proxy scored #${d.node_id}: ${fmt(d.score)}${d.skipped ? ' (skipped full eval)' : ''}`,
  },
  reward_hack_suspected: {
    validate: d => ownValue(d, 'node_id') && Array.isArray(d?.signals)
      && d.signals.every(signal => ownValue(signal, 'signal')),
    render: (d) => `reward-hack suspected on #${d.node_id}: ${(d.signals || []).map(s => s.signal).join(', ')}`,
  },
  novelty_rejected: {
    validate: d => ownValue(d, 'near_node') && ownValue(d, 'distance'),
    render: (d) => `dedup: proposal near #${d.near_node} (dist ${fmt(d.distance, 3)}) nudged to diversify`,
  },
  hypothesis_ranked: {
    validate: d => ownValue(d, 'n') || Array.isArray(d?.order),
    render: (d) => `ranked ${d.n || (d.order || []).length} hypotheses by payoff${d.confidence != null ? ` (${Math.round(d.confidence * 100)}% conf)` : ''}${note(d.reason, 70)}`,
  },
  foresight_selected: {
    validate: d => ownValue(d, 'kind') && ownValue(d, 'chosen')
      && (ownValue(d, 'n') || Array.isArray(d?.order)),
    render: (d) => `foresight picked ${d.kind === 'solution' ? 'implementation' : 'idea'} ${(d.chosen ?? 0) + 1} of ${d.n || (d.order || []).length}${d.confidence != null ? ` (${Math.round(d.confidence * 100)}% conf)` : ''}${note(d.reason, 70)}`,
  },
  run_finished: {
    render: (d) => (d?.reason === 'aborted' || d?.reason === 'finalized') ? 'run finalized (wrapped up)'
      : `run finished${d.reason ? ' (' + d.reason + ')' : ''}`,
  },
  llm_cost: {
    validate: d => ownValue(d, 'total_tokens') && ownValue(d, 'cost'),
    render: (d) => `LLM: ${d.total_tokens} tokens, ${fmtCost(d)}`,
  },
  // --- operator/boss control INTENTS + their engine confirmations. Every event the agentic boss can
  // produce gets a plain-English line here, so an action never shows in the feed as a raw-JSON blob. ---
  force_confirm: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `requested a multi-seed confirm of #${d.node_id}`,
  },
  force_ablate: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `requested an ablation probe on #${d.node_id}`,
  },
  fork: {
    validate: d => ownValue(d, 'from_node_id'),
    render: (d) => `forked a fresh improve-branch from #${d.from_node_id}`,
  },
  inject_node: {
    validate: d => objectValue(d, 'idea'),
    render: (d) => { const i = d.idea || {}; return `added experiment: ${i.operator || 'improve'}${d.parent_id != null ? ' from #' + d.parent_id : ''}${i.rationale ? ' — ' + stripMd(i.rationale).slice(0, 70) : ''}` },
  },
  annotation: {
    validate: d => ownValue(d, 'node_id') && ownValue(d, 'text'),
    render: (d) => `note on #${d.node_id}: ${String(d.text || '').slice(0, 80)}`,
  },
  run_reopened: { render: () => 'run reopened to keep going' },
  resume: { render: () => 'run resumed — continuing' },
  run_abort: { render: (d) => `finalize requested${d.reason ? ' (' + d.reason + ')' : ''} — wrapping up` },
  command_ack: { render: (d) => `engine acknowledged command${d.event_seq != null ? ` at event ${d.event_seq}` : ''}` },
  hypothesis_added: {
    validate: d => ownValue(d, 'statement'),
    render: (d) => `hypothesis added${d.source ? ' (' + d.source + ')' : ''} — ${String(d.statement || '').slice(0, 90)}`,
  },
  // THE CARD LIFECYCLE. These were folded, durable and in the log — and invisible, because the feed
  // is an allow-list (`isCuratedType`) and none of them had an entry. Measured on
  // rubertlite-dr-unified-v5: the feed's last row was `setup_finished` at t=6.6 s, while `card_added`
  // (t=820 s), `card_build_requested` and `card_build_attempted` (t=822 s) all landed and were
  // dropped. Forty minutes of a run that had, as far as the operator could see, done nothing.
  card_added: {
    validate: d => ownValue(d, 'id'),
    render: (d) => `card ${d.id}${d.source ? ' (' + d.source + ')' : ''}${note(d.statement, 90)}`,
  },
  card_build_requested: {
    validate: d => ownValue(d, 'card_id'),
    render: (d) => `build requested for ${d.card_id} — the Developer is starting`,
  },
  card_build_attempted: {
    validate: d => ownValue(d, 'card_id'),
    render: (d) => `${d.card_id} build attempt${d.generation ? ` (generation ${d.generation})` : ''}`,
  },
  card_build_done: {
    validate: d => ownValue(d, 'card_id'),
    render: (d) => `${d.card_id} built${d.node_id != null ? ` → node #${d.node_id}` : ''}`
      + `${d.speculative ? ' (prefetched)' : ''}`,
  },
  card_enriched: {
    validate: d => ownValue(d, 'id'),
    render: (d) => `${d.id} enriched from node #${d.node_id}`,
  },
  card_ranked: {
    validate: d => ownValue(d, 'order'),
    render: (d) => `cards re-ranked — ${(d.order || []).slice(0, 4).join(', ')}`
      + `${(d.order || []).length > 4 ? ` +${d.order.length - 4} more` : ''}`,
  },
  // The START of deep research. `research_completed` was narrated and this was not, so a multi-minute
  // web+LLM investigation announced itself only once it was over.
  research_attempted: {
    render: (d) => `deep research started (${d.trigger || 'auto'})${d.manual ? ' — requested' : ''}`,
  },
  speculation_depth_settled: {
    validate: d => ownValue(d, 'depth'),
    render: (d) => `prefetch depth ${d.previous ?? '?'} → ${d.depth}${note(d.reason, 90)}`,
  },
  // docs/29 F1. The run CHANGED ITS EXECUTION WIDTH, which is exactly the kind of thing an operator
  // debugs as something else when it is silent ("why is my 4-GPU box running one experiment"). The
  // engine already says it at WARNING; this is the same fact in the feed. Either axis may be absent
  // — `llm_parallel` only moves when it was itself AUTO — so neither is assumed present.
  run_width_settled: {
    validate: d => ownAny(d, ['eval_parallel', 'llm_parallel']),
    render: (d) => {
      const before = (d.previous && typeof d.previous === 'object') ? d.previous : {}
      const axes = ['eval_parallel', 'llm_parallel']
        .filter(axis => ownValue(d, axis))
        .map(axis => `${axis} ${before[axis] ?? '?'} → ${d[axis]}`)
        .join(', ')
      const ev = (d.evidence && typeof d.evidence === 'object') ? d.evidence : {}
      const from = ev.widest_declared_gpus != null && ev.gpu_pool != null
        ? ` (${ev.open_proposals ?? '?'} open proposal(s) wanting up to ${ev.widest_declared_gpus} GPU(s) of ${ev.gpu_pool})`
        : ''
      return `width re-pinned from the proposals — ${axes}${from}`
    },
  },
  hypothesis_merged: {
    validate: d => ownValue(d, 'statement'),
    render: (d) => `hypotheses merged — ${String(d.statement || '').slice(0, 80)}${(d.aliases || []).length ? ` (${(d.aliases || []).length} paraphrase${(d.aliases || []).length === 1 ? '' : 's'} folded)` : ''}`,
  },
  lessons_distilled: {
    validate: d => ownValue(d, 'count'),
    render: (d) => `distilled ${d.count || 0} lesson${d.count === 1 ? '' : 's'}${d.trigger ? ' (' + d.trigger + ')' : ''}`,
  },
  lessons_refreshed: { render: (d) => d.skipped ? 'cross-run lessons refresh skipped' : 'cross-run lessons refreshed' },
  coverage_snapshot: {
    validate: d => ownValue(d, 'themes') && ownValue(d, 'niches'),
    render: (d) => `coverage — ${d.themes || 0} theme${d.themes === 1 ? '' : 's'} · ${d.niches || 0} niche${d.niches === 1 ? '' : 's'}${d.dominant_theme_frac != null ? ` · dominant ${Math.round(d.dominant_theme_frac * 100)}%` : ''}`,
  },
  deps_installed: { render: (d) => `dependencies installed${d.packages ? ': ' + (Array.isArray(d.packages) ? d.packages.slice(0, 6).join(', ') : String(d.packages).slice(0, 80)) : ''}` },
  fork_done: { render: () => 'fork fulfilled — branch added' },
  inject_done: { render: () => 'experiment injected into the tree' },
  confirm_done: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `multi-seed confirm finished for #${d.node_id}`,
  },
  confirm_eval: {
    validate: d => ['seed', 'node_id', 'metric'].every(key => ownValue(d, key)),
    render: (d) => `confirm seed ${d.seed} on #${d.node_id} → ${fmt(d.metric)}`,
  },
  agent_decision: {
    validate: d => nestedValue(d, 'chosen', 'kind') && Array.isArray(d?.legal),
    render: (d) => `agent chose ${d.chosen?.kind || '?'}${d.chosen?.node_id != null ? ' → #' + d.chosen.node_id : ''} (of ${(d.legal || []).length} legal move${(d.legal || []).length === 1 ? '' : 's'})${note(d.rationale, 70)}`,
  },
  agent_validated: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `developer validated #${d.node_id}${d.fell_back ? ' (fell back to a simpler build)' : d.ok === false ? ' (checks failed)' : ' ✓'}`,
  },
  spec_proposed: { render: () => 'eval spec proposed — awaiting ratification' },
  spec_approval_requested: { render: () => 'awaiting your approval of the eval spec' },
  spec_approved: { render: () => 'eval spec ratified' },
  spec_drift: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `spec drift on #${d.node_id}${d.seed != null ? ' (seed ' + d.seed + ')' : ''} — metric discarded`,
  },
  drift_unavailable: { render: (d) => `drift check unavailable${note(d.reason)}` },
  data_profiled: {
    validate: d => ownValue(d, 'columns') && d.columns !== null
      && typeof d.columns === 'object',
    render: (d) => { const c = d.columns; const n = Array.isArray(c) ? c.length : Object.keys(c || {}).length; return `dataset profiled (${n} column${n === 1 ? '' : 's'})` },
  },
  data_provenance: {
    validate: d => objectValue(d, 'assets'),
    render: (d) => { const n = Object.keys(d.assets || {}).length; return `dataset provenance pinned (${n} asset${n === 1 ? '' : 's'})` },
  },
  // Setup phase (task + data), made watchable: these appear live between run start and the first node.
  setup_started: { render: (d) => `setting up task & data${d.repo ? ' (repo)' : ''}…` },
  setup_step: {
    validate: d => ownValue(d, 'step'),
    render: (d) => `setup: ${d.step}${d.detail ? ' — ' + String(d.detail).slice(0, 80) : (d.sources?.length ? ' (' + d.sources.join(', ') + ')' : '')}`,
  },
  setup_finished: { render: (d) => `setup done${d.seconds != null ? ` (${d.seconds}s)` : ''}` },
  workspace_seeded: {
    validate: d => ownValue(d, 'node_id') && Array.isArray(d?.materialized),
    render: (d) => `seeded node #${d.node_id ?? '?'} workspace: ${(d.materialized || []).join(', ').slice(0, 90)}`,
  },
  run_setup_started: {
    validate: d => Array.isArray(d?.command),
    render: (d) => `run setup (once): ${(d.command || []).join(' ').slice(0, 80)}`,
  },
  run_setup_finished: {
    validate: d => ownValue(d, 'exit_code'),
    render: (d) => `run setup ${d.exit_code === 0 ? 'ok' : 'FAILED (exit ' + d.exit_code + ')'}`,
  },
  host_grading: { render: (d) => `host-side grading active${d.scorer ? ' (' + d.scorer + ')' : ''}${d.competition ? ' · ' + d.competition : ''}` },
  diversity_archive: { render: () => 'diversity archive updated' },
  workspace_changed: { render: () => 'workspace changed since the last run — re-grounding' },
  budget: {
    validate: d => ownValue(d, 'nodes') && ownValue(d, 'elapsed_s'),
    render: (d) => `checkpoint — ${d.nodes} node${d.nodes === 1 ? '' : 's'}, ${fmt(d.elapsed_s, 3)}s elapsed`,
  },
  // Meaningful events that previously LEAKED as raw JSON (no narration + not hidden). Narrated here so
  // they read cleanly. The high-volume / internal read-model events (node_concepts and the rest of the
  // concept-cadence sidecars, verifier scores, resume/restart plumbing) are deliberately left WITHOUT a
  // narration — the curated feed is now an allow-list (see `isCuratedType`), so anything unnarrated stays
  // out of the feed (still in events.jsonl + the raw Event Explorer) instead of rendering as a JSON blob.
  // Newly narrated types: guard the field that DEFINES the claim so a partial/legacy payload degrades to
  // the generic line instead of "#undefined"/"level undefined" (the file's documented anti-#undefined rule).
  node_reset: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `re-running node #${d.node_id} from ${d.from_stage || d.stage || 'propose'}`,
  },
  node_tombstoned: {
    validate: d => (Array.isArray(d?.node_ids) && d.node_ids.length > 0) || ownValue(d, 'node_id'),
    render: (d) => { const ids = Array.isArray(d.node_ids) ? d.node_ids : (d.node_id != null ? [d.node_id] : []); return ids.length ? `deleted node${ids.length === 1 ? '' : 's'} ${ids.map(n => '#' + n).join(', ')}` : 'deleted a node subtree' },
  },
  holdout_evaluated: {
    validate: d => ownValue(d, 'node_id') && ownValue(d, 'metric'),
    render: (d) => `holdout (final-exam) score for #${d.node_id} → ${fmt(d.metric)}`,
  },
  novelty_graded: {
    validate: d => ownValue(d, 'level'),
    render: (d) => `novelty graded ${d.node_id != null ? '#' + d.node_id + ' ' : ''}level ${d.level}${d.grade ? ' (' + d.grade + ')' : ''} → ${d.recommendation || 'allow'}`,
  },
  // A capsule's best metric is run-wide, not the matched concept's outcome. Only the
  // v2 retained-outcome row earns concept-level wording; every fallback says "run best" explicitly.
  cross_run_prior: {
    validate: d => Array.isArray(d?.matched_concepts) && Array.isArray(d?.prior_runs)
      && d.matched_concepts.length > 0 && d.prior_runs.length > 0,
    render: crossRunPriorNarration,
  },
  comment_created: {
    validate: d => ownValue(d, 'node_id') && ownValue(d, 'text'),
    render: (d) => `comment on #${d.node_id ?? '?'}: ${String(d.text || d.body || '').slice(0, 80)}`,
  },
  comment_edited: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `comment edited on #${d.node_id ?? '?'}`,
  },
  comment_resolution_changed: {
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `comment ${d.resolved === true ? 'resolved' : d.resolved === false ? 'reopened' : 'resolution changed'} on #${d.node_id ?? '?'}`,
  },
  concept_tag_edited: {
    validate: d => ownValue(d, 'node_id') && Array.isArray(d?.concepts),
    render: (d) => `operator re-tagged #${d.node_id}: ${(d.concepts || []).slice(0, 4).join(', ') || '(cleared)'}`,
  },
  // `d.priority` is the 0-BASED wire value while every operator surface is 1-based (the board chip
  // renders priority+1, the form says "1 is highest", savePriority submits visible-1) — so pinning
  // "1 (highest)" narrated as "pinned to 0". Narrate the operator's number, guarding a non-int.
  card_reprioritized: {
    validate: d => typeof d?.id === 'string' && d.id.length > 0 && ownValue(d, 'priority'),
    render: (d) => `Card ${d.id.slice(0, 80)} priority pinned to `
      + `${Number.isSafeInteger(d.priority) ? d.priority + 1 : d.priority}`,
  },
  card_edited: {
    validate: d => typeof d?.id === 'string' && d.id.length > 0
      && typeof d?.statement === 'string',
    render: (d) => `Card ${d.id.slice(0, 80)} display statement edited${note(d.statement, 70)}`,
  },
  card_resource_pinned: {
    validate: d => typeof d?.id === 'string' && d.id.length > 0
      && ownValue(d, 'gpus'),
    render: (d) => `Card ${d.id.slice(0, 80)} resource override: ${d.gpus} GPU${d.gpus === 1 ? '' : 's'}${d.gpu_mem_mib != null ? ` · ${d.gpu_mem_mib} MiB/GPU` : ''}`,
  },
  card_dropped: {
    validate: d => typeof d?.id === 'string' && d.id.length > 0,
    render: (d) => `Card ${d.id.slice(0, 80)} dropped${note(d.reason, 70)}`,
  },
  card_auto_dropped: {
    validate: d => typeof d?.id === 'string' && d.id.length > 0,
    render: (d) => `Card ${d.id.slice(0, 80)} automatically dropped${note(d.reason, 70)}`,
  },
  card_reopened: {
    validate: d => typeof d?.id === 'string' && d.id.length > 0,
    render: (d) => `Card ${d.id.slice(0, 80)} reopened${note(d.reason, 70)}`,
  },
  hypothesis_updated: {
    validate: d => ownValue(d, 'statement'),
    render: (d) => `hypothesis updated — ${String(d.statement || '').slice(0, 80)}`,
  },
  trust_gate_changed: { render: (d) => `trust gate changed${d.gate ? ` — ${d.gate}` : ''}` },
  inject_failed: { render: (d) => `experiment injection failed${note(d.reason)}` },
  env_changed: { render: () => 'environment changed since run start — re-grounding' },
  // Failure/audit + progress events whose SUCCESS or sibling twins are already narrated — hiding only the
  // failure/correction case was the wrong asymmetry, so surface them here too (found by the coverage audit).
  report_refresh_failed: { render: (d) => `report refresh failed${note(d.reason || d.error || d.message)}` },
  log_repaired: { render: (d) => `event log repaired${d.dropped_lines != null ? ` — dropped ${d.dropped_lines} corrupt line${d.dropped_lines === 1 ? '' : 's'}, kept ${d.good_records ?? '?'}` : ' at divergence'}` },
  stage_finished: {
    validate: d => ownAny(d, ['name', 'stage']),
    render: (d) => `stage ${d.name || d.stage || '?'} ${d.status === 'ok' || d.status === 'passed' || d.ok === true ? '✓' : (d.status || 'finished')}${d.node_id != null ? ` (#${d.node_id})` : ''}`,
  },
  lessons_reconciled: { render: (d) => `lessons reconciled${d.n_retired != null || d.n_added != null ? ` — ${d.n_retired || 0} retired, ${d.n_added || 0} re-derived` : ''}` },
  // The verdict is about ONE eval phase, and saying so is the difference between "the training is
  // broken" and "the data_prep stage printed something odd". `log_role`/`stage` are additive (rows
  // predating them render exactly as before); only `training` may have killed anything.
  train_monitor_alert: {
    validate: d => ownValue(d, 'node_id') && ownValue(d, 'status'),
    render: (d) => `training monitor: #${d.node_id} ${d.log_role && d.log_role !== 'training'
      ? `${d.stage ? `stage ${d.stage}` : 'a non-training phase'} looks ${d.status} (advisory)`
      : `looks ${d.status}`}${d.reason ? ' — ' + String(d.reason).slice(0, 90) : ''}${d.confidence != null ? ` (${Math.round(d.confidence * 100)}% conf)` : ''}`,
  },
  // asha_rank carries RECOVERY edges too — asha_monitor publishes both precisely so projections can
  // CLEAR the flag — so ignoring `d.underperforming` rendered a recovery as another warning.
  asha_rank: {
    validate: d => ownValue(d, 'node_id') && ownValue(d, 'intermediate'),
    render: (d) => `ASHA: #${d.node_id} ${fmt(d.intermediate)} ${d.underperforming === false
      ? 'rank recovered'
      : `${d.endpoint_underperforming === false ? 'same-resource' : 'endpoint'} rank warning`}`,
  },
  // asha_verdict is the STOP DECISION taken over a persistent rank flag — the rank test is only the
  // evidence. The COMMON row is a judge that kept the run alive, so render the DECISION (`stop_decided`)
  // and the CLAIM (`kill`) separately, which is what the row actually records: branching on `d.kill`
  // alone printed a superseded stop as `keep running (stop)`. Neither bit says the node stopped —
  // `_evaluate` reaches its kill branch only after a reset/abort/usable-result pre-emption — so the
  // wording stays "claimed", and the node's own terminal remains the authority. `unavailable` means
  // nobody answered (no client / capped), which never kills. Legacy rows carry no `stop_decided`, so
  // they fall back to the flag they do have.
  asha_verdict: {
    // `status` is NOT required, and requiring it was a real defect: the renderer below defaults it
    // (`d.status || 'unavailable'`) and the comment above says what that default MEANS — nobody
    // answered — so a status-less row is a case this narration was written to explain, not one it
    // has to refuse. The validator disagreed, and `eventNarration` consults it FIRST, so such a row
    // rendered as the opaque "details could not be summarized" fallback instead. `node_id` alone
    // defines the claim; every other field the line interpolates has a documented default.
    validate: d => ownValue(d, 'node_id'),
    render: (d) => `ASHA judge: #${d.node_id} ${(d.stop_decided ?? d.kill) === true
      ? (d.kill === false
        ? `STOP decided — superseded by ${d.kill_superseded_by || 'another watchdog'}`
        : 'STOP decided — early-kill claimed')
      : `keep running (${d.status || 'unavailable'})`}${d.reason ? ' — ' + String(d.reason).slice(0, 90) : ''}${d.confidence != null ? ` (${Math.round(d.confidence * 100)}% conf)` : ''}`,
  },
  restart: { render: () => 'run restart requested (pause-and-resume handoff)' },
}

// Coarse "kind" per event type: drives the icon, the accent color, and the filter chips. One place so
// the legend, the row, and the filter all agree.
// [key, label] — the icon is a monochrome glyph (GROUP_GLYPH), not an emoji (round-7 readability pass).
export const GROUPS = [
  // The card lifecycle is a PROPOSAL fact, not a lifecycle one: it is the run choosing what to
  // test and then handing it to the Developer, which is exactly what this chip filters for.
  ['proposal', 'proposals', 'node_building node_created card_added card_build_requested card_build_attempted card_build_done card_enriched card_ranked'],
  ['eval', 'results', 'node_eval_started node_evaluated node_failed node_repaired node_confirmed best_confirmed proxy_scored ablate deps_installed confirm_done confirm_eval agent_validated holdout_evaluated stage_finished'],
  ['decision', 'decisions', 'policy_decision strategy_decision rung_promoted agent_decision set_strategy hypothesis_ranked foresight_selected coverage_snapshot speculation_depth_settled run_width_settled'],
  ['research', 'research', 'research_completed research_attempted deep_research hypothesis_added hypothesis_merged lessons_refreshed lessons_distilled cross_run_prior hypothesis_updated lessons_reconciled'],
  ['report', 'report', 'report_generated reflection_note report_refresh_failed'],
  ['trust', 'trust', 'reward_hack_suspected data_leakage spec_drift novelty_rejected drift_unavailable workspace_changed novelty_graded train_monitor_alert asha_rank asha_verdict'],
  ['control', 'actions', 'hint pause resume run_abort node_abort fork promote annotation inject_node force_confirm force_ablate approval_requested approval_granted budget_extend run_reopened spec_approved spec_approval_requested spec_proposed command_ack fork_done inject_done node_reset node_tombstoned concept_tag_edited card_reprioritized card_edited card_resource_pinned card_dropped card_reopened inject_failed comment_created comment_edited comment_resolution_changed trust_gate_changed restart'],
  ['lifecycle', 'lifecycle', 'run_started run_finished llm_cost budget data_profiled data_provenance host_grading diversity_archive setup_started setup_step setup_finished workspace_seeded run_setup_started run_setup_finished env_changed log_repaired card_auto_dropped phase_progress'],
]
export const TYPE2GROUP = Object.fromEntries(GROUPS.flatMap(([group, , types]) =>
  types.split(' ').map(type => [type, group])))
// Monochrome glyph per kind (default) + a few high-signal per-type overrides. Names resolve in
// icons.jsx GLYPHS (unknown → 'dot' fallback). Color is carried by CSS (only user/trust/highlighted).
export const GROUP_GLYPH = {
  proposal: 'trending', eval: 'target', decision: 'gitbranch', research: 'search',
  report: 'doc', trust: 'alert', control: 'gear', lifecycle: 'flag',
}
export const TYPE_GLYPH = {
  node_failed: 'bug', node_repaired: 'gear', node_confirmed: 'target', best_confirmed: 'star',
  run_started: 'play', run_finished: 'stop', report_generated: 'doc', research_completed: 'search',
  deep_research: 'search', agent_decision: 'bot', run_reopened: 'play', inject_node: 'gitbranch',
  fork: 'gitbranch', agent_validated: 'target', hypothesis_ranked: 'bulb', foresight_selected: 'bulb',
}
export function kindOf(type) {
  // Own-property reads: a forward-compat event type equal to an Object.prototype key ("constructor",
  // "toString", …) must not resolve to an inherited function (which would corrupt the icon/group).
  const g = (Object.hasOwn(TYPE2GROUP, type) && TYPE2GROUP[type]) || 'lifecycle'
  const glyph = (Object.hasOwn(TYPE_GLYPH, type) && TYPE_GLYPH[type])
    || (Object.hasOwn(GROUP_GLYPH, g) && GROUP_GLYPH[g]) || 'dot'
  return { group: g, glyph }
}

// The curated feed is an ALLOW-LIST: an event shows iff it has a human-readable narration (a NARR
// entry). Everything else is internal bookkeeping — the finalize state-machine's per-step checklist
// (`finalize_step` {scope, step}) and exact-finish ack (`finalization_finished` {finish_seq}), the
// per-LLM-call cost delta `llm_usage` (the aggregate `llm_cost` "LLM: N tokens, $X" is the human total),
// the concept-cadence read-model sidecars (`node_concepts`, `concept_edge`, `concept_consolidation`,
// `concept_coverage_snapshot`, `hypothesis_concepts`), internal verifier scores, and resume/restart
// plumbing. These stay in events.jsonl and the raw Event Explorer, but never leak into the curated
// timeline as an opaque JSON blob. Promoting a type into the feed = giving it a NARR entry above.
// (Was an opt-OUT blacklist `FEED_HIDDEN`, which silently leaked every type nobody remembered to add —
// e.g. `node_concepts` rendered as raw `{"node_id":67,"concepts":[…]}`. The allow-list can't regress
// that way: a new event type is invisible-until-narrated instead of raw-JSON-until-hidden.)
export const isCuratedType = (type) => Object.hasOwn(NARR, type)

export function eventNarration(event) {
  const omittedBytes = event?._log_page?.truncated ? Number(event._log_page.raw_bytes || 0) : 0
  if (event?._log_page?.truncated === true) {
    return `${event.type || 'event'} — details omitted (${omittedBytes.toLocaleString()} source bytes exceed page limit)`
  }
  try {
    // Own-property read so an event type equal to an Object.prototype key ("toString", "constructor")
    // can't resolve `render`/`validate` to an inherited function (would emit "[object …]" search text).
    // One own-property lookup is enough now that both halves hang off the SAME entry.
    const type = event?.type
    const entry = Object.hasOwn(NARR, type) ? NARR[type] : undefined
    const render = entry?.render
    const data = event?.data
    if (render && (data === null || typeof data !== 'object' || Array.isArray(data))) {
      throw new TypeError('invalid event narration payload')
    }
    if (render && entry.validate && !entry.validate(data)) {
      throw new TypeError('incomplete event narration payload')
    }
    const value = render ? render(data) : JSON.stringify(data ?? {}).slice(0, 80)
    // Narration contains agent/user prose. Validate its structured input and renderer
    // result; never infer a template failure from a legitimate word inside the rendered data.
    if (!value) throw new TypeError('incomplete event narration')
    return String(value || event?.type || 'event')
  } catch {
    return `${event?.type || 'event'} — details could not be summarized`
  }
}


// HOW LONG HAS THIS BEEN GOING ON? The live status strip named the phase ("Writing experiment #3…")
// but never how long it had been in it, so a build that had been silent for forty minutes read
// exactly like one that started two seconds ago. That is the whole of the operator's report — "it
// hangs for a very long time with no logs (or they are not visible)": the logs existed, the *clock*
// did not, so there was nothing on screen to distinguish work from a stall.
//
// Build ages reuse `_on_node_building`'s existing `started: e.ts` marker. Evaluation ages use the
// generation-scoped activity projection because their one-shot start event can leave the UI window.
// Bookkeeping events that must not move the live status. They are excluded from the
// between-experiments label, else the strip flickers to "Thinking…" every time one lands right after
// a node (coverage/cost/lessons/reflection all fire post-eval) — and from the CLOCK below, so the
// two always describe the same moment.
export const STATUS_NOISE = new Set([
  'coverage_snapshot', 'llm_cost', 'reflection_note', 'diversity_archive',
  'lessons_distilled', 'lessons_refreshed', 'budget', 'node_building', 'command_ack',
  // `llm_usage` is the PER-CALL accounting row and it was missing here while its aggregate sibling
  // `llm_cost` was present. That one omission defeated the clock below completely: a long agentic
  // stage writes one `llm_usage` per provider call — 163 of them during
  // rubertlite-dr-unified-v5's silent 40 minutes — so `liveStatusStartedAt` restarted every few
  // seconds and never reached the 20-second floor. The clock this file exists to provide rendered
  // nothing at all for exactly the wait it was built for.
  'llm_usage',
  // `phase_progress` is here for EXACTLY the reason `llm_usage` above is, and it would have been the
  // same defect a second time: a build emits a beacon at every step boundary, so leaving it out of
  // this set would let the fallback scan below restart the clock on each one and the age would never
  // reach the 20-second floor during the very wait it measures. The clock does not lose the beacon by
  // excluding it — it reads it FIRST and far more precisely, from the still-OPEN phase's own start
  // (see below), rather than from whichever row happened to land last.
  'phase_progress',
])

const STATUS_AGE_MIN_S = 20      // below this the clock is noise, and a blinking number reads as churn

export function pendingWork(live, log = []) {
  // "pending" is THREE states, and the strip used to call all of them "running in parallel".
  // Measured on `rubertlite-dr-unified-v9` 2026-08-17: three pending nodes, of which ONE had a
  // training process on a GPU, one had announced its evaluation and was between stages with nothing
  // executing, and one had not begun — while the second card sat idle. "Running 3 experiments in
  // parallel…" is false about two of the three, and it is false in the direction that hides a stall:
  // an operator reading it sees a busy box.
  //
  // The API projection is authoritative and survives the Dock's bounded timeline window. The scan
  // remains only as compatibility for an older server; unlike the old implementation it is keyed by
  // BOTH node id and generation, so a reset can never inherit the abandoned lifecycle's start row.
  const pending = Object.values(live?.nodes || {}).filter(n => n?.status === 'pending'
    && nodeActivityStatus(n, live) !== NODE_ACTIVITY.BUILDING)
  const announced = new Set()
  for (const event of log) {
    if (event?.type !== 'node_eval_started') continue
    const id = event?.data?.node_id ?? event?.node_id
    const generation = event?.data?.generation ?? event?.generation
    if (id != null && Number.isInteger(Number(generation)) && typeof generation !== 'boolean') {
      announced.add(`${Number(id)}:${Number(generation)}`)
    }
  }
  const started = []
  const queued = []
  const unknown = []
  for (const node of pending) {
    const status = nodeActivityStatus(node, live)
    const generation = Number.isInteger(Number(node.attempt)) ? Number(node.attempt) : 0
    if (status === NODE_ACTIVITY.EVALUATING
        || announced.has(`${Number(node.id)}:${generation}`)) started.push(node)
    else if (status === NODE_ACTIVITY.QUEUED) queued.push(node)
    else unknown.push(node)
  }
  return { pending, started, queued, unknown }
}

export function pendingWorkLabel(live, log = [], stagesIn = null) {
  // One sentence for the strip, and it never claims more than `pendingWork` established.
  const { pending, started, queued, unknown } = pendingWork(live, log)
  if (!pending.length) return ''
  // The cursor Map can be handed in precomputed (`evalStages` is a full sweep of the retained
  // timeline window, and the Dock renders on every poll tick); decoded here only as the standalone
  // fallback, and only when something is actually EVALUATING — the all-queued case has no step to
  // name and must not pay the sweep for an answer it cannot use.
  const stages = stagesIn instanceof Map ? stagesIn
    : started.length ? evalStages(log) : new Map()
  // WHICH STEP, when the run's own cursor says. `training / evaluating` was one phrase for a whole
  // multi-hour pipeline and is false for every stage of it that is not the trainer; the cursor makes
  // the step nameable, and `evalStageShortLabel` is the ONE place those words are chosen so the strip
  // and the node card cannot disagree about the same running stage — which is also why the record is
  // read through `evalStageFor` and never by bare node id: the generation fence lives there, and a
  // raw `stages.get(id)` let an abandoned attempt's still-open beacon label the NEW lifecycle the
  // node card was correctly refusing to describe.
  const stepOf = node => {
    const label = evalStageShortLabel(evalStageFor(node, stages))
    return label ? ` · ${label}` : ''
  }
  if (pending.length === 1) {
    // The step REPLACES the flat phrase rather than being appended to it. Appended, a mining node
    // read "training / evaluating · mine 1/3" — the cursor's correction and the very claim it
    // corrects, in one sentence, with the false half first. `evalStageLabel` already refuses to say
    // "Training" for a step the manifest did not declare as one.
    const record = started.length ? evalStageFor(pending[0], stages) : null
    if (record) return `Experiment #${pending[0].id} · ${evalStageLabel(record)}…`
    if (started.length) return `Experiment #${pending[0].id} training / evaluating…`
    if (queued.length) return `Experiment #${pending[0].id} waiting for an evaluation slot…`
    return `Experiment #${pending[0].id} pending — evaluation start unknown…`
  }
  const parts = []
  // With several evaluating at once the strip has no room for each one's step, so it names them only
  // when they AGREE — "3 evaluating · train 2/3" is a fact about all three; listing three different
  // steps would not fit and averaging them would be a claim about none of them.
  const steps = new Set(started.map(node => stepOf(node)))
  const shared = steps.size === 1 ? [...steps][0] : ''
  if (started.length) parts.push(`${started.length} evaluating${shared}`)
  if (queued.length) parts.push(`${queued.length} waiting for a slot`)
  if (unknown.length) parts.push(`${unknown.length} start unknown`)
  return `${parts.join(', ')}…`
}

export function liveStatusStartedAt(live, log = []) {
  // An explicitly non-live state has no clock that can keep advancing. In particular, a durable
  // eval-start row survives a process crash; it explains "interrupted" but is not proof of work that
  // is still running now. An omitted liveness field remains the older-server compatibility shape.
  if (live?.finished || live?.paused || live?.stop_requested || live?.phase === 'finalizing'
      || live?.engine_running === false || live?.engine_running === null) return null
  // The CURRENT PHASE's own start wins over everything below. The two answer different questions and
  // the phase's is the one an operator is asking: with beacons, "40m" against "Writing code for
  // experiment #7…" means the Developer has been going 40 minutes, whereas the build-marker clock
  // below would report the age of the whole build — proposal, gate and all — under a label naming
  // only its last step. `livePhase` already refuses a finished or engine-less run, so this cannot
  // resurrect the phantom clock the guards below exist to prevent.
  // BUILD-scoped, with the SAME filter `Dock.agentStatus` gives the label — the label and its age
  // must never be able to describe different moments. Once eval|stage beacons share the stream the
  // unfiltered newest-open record is routinely an eval cursor, so the clock reset to a stage
  // boundary minutes old beside a build label forty minutes old. When no build phase is open, the
  // eval-led label gets its age from the eval-start ladder below, which is that lane's own clock.
  const phase = livePhase(live, log, 'build')
  if (phase?.ts != null) return phase.ts
  // The OLDEST in-flight build, not the newest: with several Developers writing at once the number
  // that matters is how long the slowest one has been going, which is the one that stalls the batch.
  const started = (Object.values(live?.buildings || {}).length
    ? Object.values(live.buildings) : live?.building ? [live.building] : [])
    .map(marker => Number(marker?.started))
    .filter(value => Number.isFinite(value) && value > 0)
  if (started.length) return Math.min(...started)
  // Evaluation starts are one-shot rows and routinely scroll out of the bounded Dock timeline. The
  // API carries their first timestamp on the generation-scoped activity projection, so a long silent
  // training run keeps its real age instead of restarting at whichever later event remains visible.
  const evalStarted = Object.values(live?.nodes || {})
    .filter(node => nodeActivityStatus(node, live) === NODE_ACTIVITY.EVALUATING)
    .map(node => Number(node?.activity?.started_at))
    .filter(value => Number.isFinite(value) && value > 0)
  if (evalStarted.length) return Math.min(...evalStarted)
  // Otherwise the phase began at the last event that MEANT something — the same noise filter the
  // label itself uses, so the clock and the label can never describe different moments.
  for (let i = log.length - 1; i >= 0; i--) {
    const ts = Number(log[i]?.ts)
    if (!STATUS_NOISE.has(log[i]?.type) && Number.isFinite(ts) && ts > 0) return ts
  }
  return null
}

export function liveStatusAgeLabel(live, log = [], now = Date.now() / 1000) {
  const started = liveStatusStartedAt(live, log)
  if (started == null) return ''
  const seconds = Math.floor(now - started)
  // A negative age is clock skew between the engine host and the browser, not a fact about the run.
  if (!Number.isFinite(seconds) || seconds < STATUS_AGE_MIN_S) return ''
  // The TIERING is `format.js::durationLabel`, shared with the node episode picker and the standing
  // watch strip — this surface's own rule is only the noise floor above, which says when an age is
  // worth printing at all. See that function for why the three stopped each keeping their own copy.
  return durationLabel(seconds)
}
