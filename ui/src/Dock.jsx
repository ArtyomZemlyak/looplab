import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { get, fmt, workingId, resetRun, getRunCommand, retryRunCommand, runCommand,
  commandFeedback, commandErrorMessage, commandFailureRecord, commandCanRetry, createIdempotencyKey,
  commandActionForEvent, commandRecordMatchesAction, commandEventForAction,
  loadRunTransport, saveRunTransport, clearRunTransport, isTransientCommandReadError,
  clearRunCommandLock, loadRunCommandLock, saveRunCommandLock, subscribeRunCommandLock,
  COMMAND_SUCCEEDED, COMMAND_FAILED, storageGet, storageSet, runApiPath, runNodeApiPath } from './util.js'
import { usePoll } from './hooks.js'
import Markdown, { stripMd } from './markdown.jsx'
import { NodeTrace, TraceUnavailable } from './Inspector.jsx'
import { OpIcon } from './icons.jsx'
import { runLifecycle } from './runIndex.js'
import VirtualTimeline from './VirtualTimeline.jsx'
import { timelineEventKey } from './timelineModel.js'
import { DataTable } from './accessibility.jsx'
import { tracePartial, traceUnavailable } from './traceProjection.js'
import { crossRunPriorNarration } from './crossRunPrior.js'
import { buildingGenerations, buildingMarkers } from './buildingModel.js'


// The run's EVENTS window (round-9): one scrubbable, filterable feed that renders every run event
// as a differentiated message. The per-run "boss" chat moved to the single persistent assistant, so
// there is no composer here — just the timeline, scrubber, filters, and transport.
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

const NARR = {
  run_started: (d) => `run started — ${d.goal || d.task_id} (${d.direction})`,
  node_building: (d) => `building node #${d.node_id} via ${d.operator || 'improve'}…`,
  node_created: (d) => `node #${d.node_id} via ${d.operator}${d.idea?.rationale ? ' — ' + stripMd(d.idea.rationale).slice(0, 80) : ''}`,
  node_evaluated: (d) => `node #${d.node_id} → ${fmt(d.metric)}`,
  node_failed: (d) => `node #${d.node_id} failed (${d.reason})${d.triage_action === 'reject_idea' ? ' — idea rejected' + (d.triage_rationale ? ': ' + String(d.triage_rationale).slice(0, 70) : '') : ''}`,
  node_repaired: (d) => `node #${d.node_id} repaired (attempt ${d.attempt})${note(d.rationale)}`,
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
  strategy_decision: (d) => `strategy → ${strategySummary(d.strategy)}${d.strategy?.rationale ? ' — ' + d.strategy.rationale.slice(0, 70) : ''}`,
  rung_promoted: (d) => `ASHA rung ↑${d.rung}: promoted ${(d.survivors || []).map(s => '#' + s).join(', ')}`,
  set_strategy: (d) => `operator pinned strategy → ${strategySummary(d.strategy)}`,
  deep_research: () => 'deep research requested',
  research_completed: (d) => `deep research (${d.trigger || 'auto'})${note(d.memo?.summary)}`,
  report_generated: (d) => `run report updated${note(d.content?.headline, 90)}`,
  reflection_note: (d) => `memory: ${d.n_lessons || 0} lesson${(d.n_lessons || 0) === 1 ? '' : 's'}${d.n_skills ? `, ${d.n_skills} skill${d.n_skills === 1 ? '' : 's'}` : ''}${note(d.note)}`,
  proxy_scored: (d) => `proxy scored #${d.node_id}: ${fmt(d.score)}${d.skipped ? ' (skipped full eval)' : ''}`,
  reward_hack_suspected: (d) => `reward-hack suspected on #${d.node_id}: ${(d.signals || []).map(s => s.signal).join(', ')}`,
  novelty_rejected: (d) => `dedup: proposal near #${d.near_node} (dist ${fmt(d.distance, 3)}) nudged to diversify`,
  hypothesis_ranked: (d) => `ranked ${d.n || (d.order || []).length} hypotheses by payoff${d.confidence != null ? ` (${Math.round(d.confidence * 100)}% conf)` : ''}${note(d.reason, 70)}`,
  foresight_selected: (d) => `foresight picked ${d.kind === 'solution' ? 'implementation' : 'idea'} ${(d.chosen ?? 0) + 1} of ${d.n || (d.order || []).length}${d.confidence != null ? ` (${Math.round(d.confidence * 100)}% conf)` : ''}${note(d.reason, 70)}`,
  run_finished: (d) => (d?.reason === 'aborted' || d?.reason === 'finalized') ? 'run finalized (wrapped up)'
    : `run finished${d.reason ? ' (' + d.reason + ')' : ''}`,
  llm_cost: (d) => `LLM: ${d.total_tokens} tokens, $${fmt(d.cost)}`,
  // --- operator/boss control INTENTS + their engine confirmations. Every event the agentic boss can
  // produce gets a plain-English line here, so an action never shows in the feed as a raw-JSON blob. ---
  force_confirm: (d) => `requested a multi-seed confirm of #${d.node_id}`,
  force_ablate: (d) => `requested an ablation probe on #${d.node_id}`,
  fork: (d) => `forked a fresh improve-branch from #${d.from_node_id}`,
  inject_node: (d) => { const i = d.idea || {}; return `added experiment: ${i.operator || 'improve'}${d.parent_id != null ? ' from #' + d.parent_id : ''}${i.rationale ? ' — ' + stripMd(i.rationale).slice(0, 70) : ''}` },
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
  agent_decision: (d) => `agent chose ${d.chosen?.kind || '?'}${d.chosen?.node_id != null ? ' → #' + d.chosen.node_id : ''} (of ${(d.legal || []).length} legal move${(d.legal || []).length === 1 ? '' : 's'})${note(d.rationale, 70)}`,
  agent_validated: (d) => `developer validated #${d.node_id}${d.fell_back ? ' (fell back to a simpler build)' : d.ok === false ? ' (checks failed)' : ' ✓'}`,
  spec_proposed: () => 'eval spec proposed — awaiting ratification',
  spec_approval_requested: () => 'awaiting your approval of the eval spec',
  spec_approved: () => 'eval spec ratified',
  spec_drift: (d) => `spec drift on #${d.node_id}${d.seed != null ? ' (seed ' + d.seed + ')' : ''} — metric discarded`,
  drift_unavailable: (d) => `drift check unavailable${note(d.reason)}`,
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
  // Meaningful events that previously LEAKED as raw JSON (no narration + not hidden). Narrated here so
  // they read cleanly. The high-volume / internal read-model events (node_concepts and the rest of the
  // concept-cadence sidecars, verifier scores, resume/restart plumbing) are deliberately left WITHOUT a
  // narration — the curated feed is now an allow-list (see `isCuratedType`), so anything unnarrated stays
  // out of the feed (still in events.jsonl + the raw Event Explorer) instead of rendering as a JSON blob.
  node_reset: (d) => `re-running node #${d.node_id} from ${d.from_stage || d.stage || 'propose'}`,
  node_tombstoned: (d) => { const ids = Array.isArray(d.node_ids) ? d.node_ids : (d.node_id != null ? [d.node_id] : []); return ids.length ? `deleted node${ids.length === 1 ? '' : 's'} ${ids.map(n => '#' + n).join(', ')}` : 'deleted a node subtree' },
  holdout_evaluated: (d) => `holdout (final-exam) score for #${d.node_id} → ${fmt(d.metric)}`,
  novelty_graded: (d) => `novelty graded ${d.node_id != null ? '#' + d.node_id + ' ' : ''}level ${d.level}${d.grade ? ' (' + d.grade + ')' : ''} → ${d.recommendation || 'allow'}`,
  // # CODEX AGENT: A capsule's best metric is run-wide, not the matched concept's outcome. Only the
  // v2 retained-outcome row earns concept-level wording; every fallback says "run best" explicitly.
  cross_run_prior: crossRunPriorNarration,
  comment_created: (d) => `comment on #${d.node_id ?? '?'}: ${String(d.text || d.body || '').slice(0, 80)}`,
  comment_edited: (d) => `comment edited on #${d.node_id ?? '?'}`,
  comment_resolution_changed: (d) => `comment ${d.resolved === true ? 'resolved' : d.resolved === false ? 'reopened' : 'resolution changed'} on #${d.node_id ?? '?'}`,
  concept_tag_edited: (d) => `operator re-tagged #${d.node_id}: ${(d.concepts || []).slice(0, 4).join(', ') || '(cleared)'}`,
  card_reprioritized: (d) => `Card ${d.id.slice(0, 80)} priority pinned to ${d.priority}`,
  card_edited: (d) => `Card ${d.id.slice(0, 80)} display statement edited${note(d.statement, 70)}`,
  card_resource_pinned: (d) => `Card ${d.id.slice(0, 80)} resource override: ${d.gpus} GPU${d.gpus === 1 ? '' : 's'}${d.gpu_mem_mib != null ? ` · ${d.gpu_mem_mib} MiB/GPU` : ''}`,
  card_dropped: (d) => `Card ${d.id.slice(0, 80)} dropped${note(d.reason, 70)}`,
  hypothesis_updated: (d) => `hypothesis updated — ${String(d.statement || '').slice(0, 80)}`,
  trust_gate_changed: (d) => `trust gate changed${d.gate ? ` — ${d.gate}` : ''}`,
  inject_failed: (d) => `experiment injection failed${note(d.reason)}`,
  env_changed: () => 'environment changed since run start — re-grounding',
  // Failure/audit + progress events whose SUCCESS or sibling twins are already narrated — hiding only the
  // failure/correction case was the wrong asymmetry, so surface them here too (found by the coverage audit).
  report_refresh_failed: (d) => `report refresh failed${note(d.reason || d.error || d.message)}`,
  log_repaired: (d) => `event log repaired${d.dropped_lines != null ? ` — dropped ${d.dropped_lines} corrupt line${d.dropped_lines === 1 ? '' : 's'}, kept ${d.good_records ?? '?'}` : ' at divergence'}`,
  stage_finished: (d) => `stage ${d.name || d.stage || '?'} ${d.status === 'ok' || d.status === 'passed' || d.ok === true ? '✓' : (d.status || 'finished')}${d.node_id != null ? ` (#${d.node_id})` : ''}`,
  lessons_reconciled: (d) => `lessons reconciled${d.n_retired != null || d.n_added != null ? ` — ${d.n_retired || 0} retired, ${d.n_added || 0} re-derived` : ''}`,
  train_monitor_alert: (d) => `training monitor: #${d.node_id} looks ${d.status}${d.reason ? ' — ' + String(d.reason).slice(0, 90) : ''}${d.confidence != null ? ` (${Math.round(d.confidence * 100)}% conf)` : ''}`,
  asha_rank: (d) => `ASHA: #${d.node_id} ${fmt(d.intermediate)} ${d.endpoint_underperforming === false ? 'same-resource' : 'endpoint'} rank warning`,
  restart: () => 'run restart requested (pause-and-resume handoff)',
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
const NARR_VALID = {
  run_started: d => ownAny(d, ['goal', 'task_id']) && ownValue(d, 'direction'),
  node_building: d => ownValue(d, 'node_id'),
  node_created: d => ownValue(d, 'node_id') && ownValue(d, 'operator'),
  node_evaluated: d => ownValue(d, 'node_id') && ownValue(d, 'metric'),
  node_failed: d => ownValue(d, 'node_id') && ownValue(d, 'reason'),
  node_repaired: d => ownValue(d, 'node_id') && ownValue(d, 'attempt'),
  node_confirmed: d => ['node_id', 'mean', 'std', 'seeds'].every(key => ownValue(d, key)),
  best_confirmed: d => ownValue(d, 'node_id'),
  ablate: d => ownValue(d, 'parent_id') && objectValue(d, 'impacts'),
  data_leakage: d => typeof d?.leak === 'boolean',
  approval_requested: d => ownValue(d, 'node_id'),
  approval_granted: d => ownValue(d, 'node_id'),
  node_abort: d => ownValue(d, 'node_id'),
  hint: d => ownValue(d, 'text'),
  promote: d => ownValue(d, 'node_id'),
  policy_decision: d => ownValue(d, 'chosen') && objectValue(d, 'scores'),
  strategy_decision: d => nestedValue(d, 'strategy', 'policy'),
  rung_promoted: d => ownValue(d, 'rung') && Array.isArray(d?.survivors),
  set_strategy: d => nestedValue(d, 'strategy', 'policy'),
  proxy_scored: d => ownValue(d, 'node_id') && ownValue(d, 'score'),
  reward_hack_suspected: d => ownValue(d, 'node_id') && Array.isArray(d?.signals)
    && d.signals.every(signal => ownValue(signal, 'signal')),
  novelty_rejected: d => ownValue(d, 'near_node') && ownValue(d, 'distance'),
  hypothesis_ranked: d => ownValue(d, 'n') || Array.isArray(d?.order),
  foresight_selected: d => ownValue(d, 'kind') && ownValue(d, 'chosen')
    && (ownValue(d, 'n') || Array.isArray(d?.order)),
  llm_cost: d => ownValue(d, 'total_tokens') && ownValue(d, 'cost'),
  force_confirm: d => ownValue(d, 'node_id'),
  force_ablate: d => ownValue(d, 'node_id'),
  fork: d => ownValue(d, 'from_node_id'),
  inject_node: d => objectValue(d, 'idea'),
  annotation: d => ownValue(d, 'node_id') && ownValue(d, 'text'),
  train_monitor_alert: d => ownValue(d, 'node_id') && ownValue(d, 'status'),
  asha_rank: d => ownValue(d, 'node_id') && ownValue(d, 'intermediate'),
  hypothesis_added: d => ownValue(d, 'statement'),
  hypothesis_merged: d => ownValue(d, 'statement'),
  lessons_distilled: d => ownValue(d, 'count'),
  coverage_snapshot: d => ownValue(d, 'themes') && ownValue(d, 'niches'),
  confirm_done: d => ownValue(d, 'node_id'),
  confirm_eval: d => ['seed', 'node_id', 'metric'].every(key => ownValue(d, key)),
  agent_decision: d => nestedValue(d, 'chosen', 'kind') && Array.isArray(d?.legal),
  agent_validated: d => ownValue(d, 'node_id'),
  spec_drift: d => ownValue(d, 'node_id'),
  data_profiled: d => ownValue(d, 'columns') && d.columns !== null
    && typeof d.columns === 'object',
  data_provenance: d => objectValue(d, 'assets'),
  setup_step: d => ownValue(d, 'step'),
  workspace_seeded: d => ownValue(d, 'node_id') && Array.isArray(d?.materialized),
  run_setup_started: d => Array.isArray(d?.command),
  run_setup_finished: d => ownValue(d, 'exit_code'),
  budget: d => ownValue(d, 'nodes') && ownValue(d, 'elapsed_s'),
  // Newly narrated types: guard the field that DEFINES the claim so a partial/legacy payload degrades to
  // the generic line instead of "#undefined"/"level undefined" (the file's documented anti-#undefined rule).
  node_reset: d => ownValue(d, 'node_id'),
  node_tombstoned: d => (Array.isArray(d?.node_ids) && d.node_ids.length > 0) || ownValue(d, 'node_id'),
  holdout_evaluated: d => ownValue(d, 'node_id') && ownValue(d, 'metric'),
  novelty_graded: d => ownValue(d, 'level'),
  cross_run_prior: d => Array.isArray(d?.matched_concepts) && Array.isArray(d?.prior_runs)
    && d.matched_concepts.length > 0 && d.prior_runs.length > 0,
  comment_created: d => ownValue(d, 'node_id') && ownValue(d, 'text'),
  comment_edited: d => ownValue(d, 'node_id'),
  comment_resolution_changed: d => ownValue(d, 'node_id'),
  concept_tag_edited: d => ownValue(d, 'node_id') && Array.isArray(d?.concepts),
  card_reprioritized: d => typeof d?.id === 'string' && d.id.length > 0 && ownValue(d, 'priority'),
  card_edited: d => typeof d?.id === 'string' && d.id.length > 0
    && typeof d?.statement === 'string',
  card_resource_pinned: d => typeof d?.id === 'string' && d.id.length > 0
    && ownValue(d, 'gpus'),
  card_dropped: d => typeof d?.id === 'string' && d.id.length > 0,
  hypothesis_updated: d => ownValue(d, 'statement'),
  stage_finished: d => ownAny(d, ['name', 'stage']),
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
  ['proposal', 'proposals', 'node_building node_created'],
  ['eval', 'results', 'node_evaluated node_failed node_repaired node_confirmed best_confirmed proxy_scored ablate deps_installed confirm_done confirm_eval agent_validated holdout_evaluated stage_finished'],
  ['decision', 'decisions', 'policy_decision strategy_decision rung_promoted agent_decision set_strategy hypothesis_ranked foresight_selected coverage_snapshot'],
  ['research', 'research', 'research_completed deep_research hypothesis_added hypothesis_merged lessons_refreshed lessons_distilled cross_run_prior hypothesis_updated lessons_reconciled'],
  ['report', 'report', 'report_generated reflection_note report_refresh_failed'],
  ['trust', 'trust', 'reward_hack_suspected data_leakage spec_drift novelty_rejected drift_unavailable workspace_changed novelty_graded train_monitor_alert asha_rank'],
  ['control', 'actions', 'hint pause resume run_abort node_abort fork promote annotation inject_node force_confirm force_ablate approval_requested approval_granted budget_extend run_reopened spec_approved spec_approval_requested spec_proposed command_ack fork_done inject_done node_reset node_tombstoned concept_tag_edited card_reprioritized card_edited card_resource_pinned card_dropped inject_failed comment_created comment_edited comment_resolution_changed trust_gate_changed restart'],
  ['lifecycle', 'lifecycle', 'run_started run_finished llm_cost budget data_profiled data_provenance host_grading diversity_archive setup_started setup_step setup_finished workspace_seeded run_setup_started run_setup_finished env_changed log_repaired'],
]
const TYPE2GROUP = Object.fromEntries(GROUPS.flatMap(([group, , types]) =>
  types.split(' ').map(type => [type, group])))
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
const isCuratedType = (type) => Object.hasOwn(NARR, type)

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

// Pull retained, bounded thinking text for a node out of the trace projection so the feed can surface
// "what was the Researcher thinking" inline. Returns [{op, text}] for the node.
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
// a coarse label. Polls /trace/tail only while OPEN + live (cheap when collapsed). Both this feed and
// per-observation detail are bounded/redacted projections with explicit omission receipts.
// Live-trace paging: the Dock polls a small window; "load earlier spans" raises the requested tail
// limit toward the server ceiling so a user can page back through history on demand instead of just
// reading a dead "projection is partial" notice. TRACE_LIMIT_MAX matches the /trace/tail server cap.
const TRACE_LIMIT_DEFAULT = 40
const TRACE_LIMIT_MAX = 400
const NODE_TRACE_CAP_MAX = 4096

function LiveTrace({ runId, generation, active }) {
  const scope = `${runId}:${generation || 'pending'}`
  const [tailState, setTailState] = useState({ scope, items: [], projection: {}, loaded: false })
  const [open, setOpen] = useState(false)
  const [limit, setLimit] = useState(TRACE_LIMIT_DEFAULT)
  const bodyRef = useRef(null)
  // # CODEX AGENT: Keep one scalar bottom offset while paging upward. Storing both height and top
  // duplicated the same invariant and made this hot owner-route code larger than its bundle budget.
  const stickRef = useRef(true)
  const preserveRef = useRef(null)
  // A new run/generation resets the paging window so a long prior scope doesn't over-fetch here.
  useEffect(() => {
    setLimit(TRACE_LIMIT_DEFAULT); stickRef.current = true; preserveRef.current = null
  }, [scope])
  usePoll((alive) => get(runApiPath(runId, `/trace/tail?limit=${limit}`))
    .then(r => { if (alive()) setTailState({
      scope, items: Array.isArray(r?.tail) ? r.tail : [], projection: r?.projection || {}, loaded: true,
    }) })
    .catch(() => { if (alive()) setTailState({
      scope, items: [], projection: { unavailable: true }, loaded: true,
    }) }),
    3000, [scope, active, open, limit], { enabled: active && open })
  const current = tailState.scope === scope ? tailState : { items: [], projection: {}, loaded: false }
  const tail = current.items
  const unavailable = traceUnavailable(current.projection)
  const partial = tracePartial(current.projection)
  const canLoadEarlier = partial && limit < TRACE_LIMIT_MAX
  const loadEarlier = () => {
    const el = bodyRef.current
    preserveRef.current = el ? el.scrollHeight - el.scrollTop : null
    setLimit(value => Math.min(value * 2, TRACE_LIMIT_MAX))
  }
  // A "load earlier" button at the TOP of the feed (replaces the dead partial notice); a terminal note
  // only when partial but the server ceiling is reached (older spans live in the node's full trace).
  const partialControl = !partial ? null : canLoadEarlier
    ? <button type="button" className="lt-loadmore disclosure-button" onClick={loadEarlier}>↑ load earlier spans</button>
    : <div className="lt-note" role="status">Earlier history is in the node's full trace.</div>
  useLayoutEffect(() => {
    const el = bodyRef.current
    if (!el || !open) return
    if (preserveRef.current != null) {    // just loaded earlier: keep the viewport on the same rows
      el.scrollTop = el.scrollHeight - preserveRef.current
      preserveRef.current = null
    } else if (stickRef.current) {        // parked at the bottom: follow the newest span
      el.scrollTop = el.scrollHeight
    }
  }, [tail, open])
  return (
    <div className={'live-trace' + (open ? ' open' : '')}>
      {/* Standard inline disclosure — caret ▸ left of the label, expands IN PLACE (not a popup). */}
      <button type="button" className="lt-toggle disclosure-button" aria-expanded={open}
           onClick={() => setOpen(o => !o)} title="stream the agent's thoughts + tool calls">
        <span className="lt-caret">{open ? '▾' : '▸'}</span>trace
      </button>
      {open && <div className="lt-body" ref={bodyRef} onScroll={event => {
        const el = event.currentTarget
        stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 8
      }}>
        {!current.loaded
          ? <div className="muted lt-empty" role="status">loading trace…</div>
          : unavailable
          ? <TraceUnavailable label="Trace unavailable; retrying automatically." />
          : <>{partialControl}
            {!tail.length && !partial
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
              </div>)}</>}
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
  if (lifecycle.mode === 'finishing') return 'Finishing write-out…'
  if (lifecycle.mode === 'finalization-stalled') return 'Finalization stalled — recovery required.'
  if (lifecycle.mode === 'finalizing') return 'Finalizing report, memory, and cost…'
  if (live.paused) return 'Paused'
  // Zombie guard: the run isn't finished but no engine process holds the lock (engine_running===false,
  // server-probed). Without this the strip would pulse "Thinking about the next step…" forever even
  // though nothing is running — the exact symptom of a resume that died without emitting run_finished.
  if (live.engine_running === false) return 'Engine stopped — resume to continue'
  const phase = live.phase
  if (phase === 'grounding' || phase === 'onboarding') return 'Setting up task and data…'
  if (phase === 'approval') return 'Waiting for approval…'
  if (phase === 'spec_approval') return 'Waiting for eval-spec approval…'
  // WRITING vs RUNNING are distinct and were conflated before (both said "Running experiment"):
  //   • `building` is set from node_building until node_created folds → the Developer is WRITING code;
  //   • a node with status 'pending' → its code is written and the sandbox is TRAINING it.
  // Parallel builds (parallel_build>1): several Developers write at once. Show the count — mirroring the
  // parallel-eval strip below — instead of naming only the last-appended build. Derive the label from the
  // `buildings` marker LIST (node_id->marker object) so the single-build label is right even after the
  // last-appended build (the singular `live.building`) finishes but a sibling survives. Fall back to the
  // singular `building` for a serial-build run or an old server that doesn't send `buildings`.
  const buildMarkers = buildingMarkers(live)
  if (buildMarkers.length > 1) {
    return `Writing ${buildMarkers.length} experiments in parallel…`
  }
  if (buildMarkers.length === 1) {
    const op = buildMarkers[0].operator || ''
    const id = buildMarkers[0].node_id
    const action = /repair|debug/.test(op) ? 'Repairing' : /merge/.test(op) ? 'Merging into' : 'Writing'
    return `${action} experiment #${id}…`
  }
  const pend = Object.values(live.nodes || {}).filter(n => n.status === 'pending')
  // Surface parallelism: with max_parallel>1 several nodes train at once (each pinned to its own GPU).
  // The strip named only the highest id before, hiding the fan-out — show the count when >1.
  if (pend.length > 1) return `Running ${pend.length} experiments in parallel…`
  if (pend.length) return `Running experiment #${pend[0].id}… (training)`
  // Between experiments: infer from the last MEANINGFUL event (skip the bookkeeping noise above), so the
  // label stays put on "Planning…" instead of blinking every time a coverage/cost event lands.
  let last = null
  for (let i = log.length - 1; i >= 0; i--) { if (!STATUS_NOISE.has(log[i].type)) { last = log[i].type; break } }
  if (last === 'setup_started' || last === 'setup_step' || last === 'workspace_seeded') return 'Setting up task and data…'
  if (last === 'run_setup_started') return 'Installing dependencies…'
  if (last === 'strategy_decision' || last === 'set_strategy') return 'Choosing a strategy…'
  if (last === 'research_completed' || last === 'deep_research') return 'Reading the literature…'
  if (last === 'node_created') return 'Writing and running experiment…'
  // node_evaluated / node_failed / policy_decision / agent_decision → the loop is picking what's next.
  return 'Planning next experiment…'
}

const Disclosure = ({ label, children }) => {
  const [open, setOpen] = useState(false)
  return <div className="think-debug trace-disclosure">
    <button type="button" className="role-think disclosure-button trace-disclosure-toggle" aria-expanded={open}
         onClick={() => setOpen(v => !v)}>
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
      {idea.rationale ? <Markdown className="rationale-md" text={idea.rationale} /> : <div className="v">—</div>}
      <div className="ev-meta">
        <span>operator <b>{idea.operator || d.operator}</b></span>
        {(d.parent_ids || []).length > 0 && <span>built from {d.parent_ids.map(p => '#' + p).join(', ')}</span>}
        {Object.keys(params).length > 0 && <span>params {Object.entries(params).map(([k, v]) => `${k}=${fmt(v, 3)}`).join(', ')}</span>}
        {Object.keys(space).length > 0 && <span>sweep {Object.entries(space).map(([k, v]) => `${k}∈[${(Array.isArray(v) ? v : [v]).join(', ')}]`).join('; ')}</span>}
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
        {s.eval_parallel != null && <span>eval parallel {s.eval_parallel}</span>}
        {s.llm_parallel != null && <span>LLM total {s.llm_parallel}</span>}
        {s.llm_lane_limits && typeof s.llm_lane_limits === 'object'
          && !Array.isArray(s.llm_lane_limits)
          && <span>LLM lanes {Object.entries(s.llm_lane_limits).slice(0, 5)
            .map(([lane, width]) => `${lane}:${width}`).join(', ')}</span>}
        {s.card_scoring && typeof s.card_scoring === 'object' && !Array.isArray(s.card_scoring)
          && <span>Card scoring {s.card_scoring.stance || 'balanced'} · novelty {s.card_scoring.novelty_weight}
            {' · '}coverage {s.card_scoring.coverage_weight}</span>}
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

// The available projected text behind a feed row whose one-line narration clamped it (node_failed's
// triage, node_repaired's rationale, the report headline, a hint, …). The page-level omission receipt
// remains authoritative when a source event exceeded the response cap. Returns [] when absent.
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
  if (!rows.length) return <div className="ev-detail"><pre className="code event-json">{JSON.stringify(e.data || {}, null, 2)}</pre></div>
  return <div className="ev-detail">{rows.map(([k, v], i) =>
    <React.Fragment key={i}><div className="section-h">{k}</div><div className="v">{v}</div></React.Fragment>)}</div>
}

export function eventNarration(event) {
  const omittedBytes = event?._log_page?.truncated ? Number(event._log_page.raw_bytes || 0) : 0
  if (event?._log_page?.truncated === true) {
    return `${event.type || 'event'} — details omitted (${omittedBytes.toLocaleString()} source bytes exceed page limit)`
  }
  try {
    // Own-property reads so an event type equal to an Object.prototype key ("toString", "constructor")
    // can't resolve `render`/`NARR_VALID` to an inherited function (would emit "[object …]" search text).
    const type = event?.type
    const render = Object.hasOwn(NARR, type) ? NARR[type] : undefined
    const data = event?.data
    if (render && (data === null || typeof data !== 'object' || Array.isArray(data))) {
      throw new TypeError('invalid event narration payload')
    }
    if (render && Object.hasOwn(NARR_VALID, type) && !NARR_VALID[type](data)) {
      throw new TypeError('incomplete event narration payload')
    }
    const value = render ? render(data) : JSON.stringify(data ?? {}).slice(0, 80)
    // # CODEX AGENT: Narration contains agent/user prose. Validate its structured input and renderer
    // result; never infer a template failure from a legitimate word inside the rendered data.
    if (!value) throw new TypeError('incomplete event narration')
    return String(value || event?.type || 'event')
  } catch {
    return `${event?.type || 'event'} — details could not be summarized`
  }
}

// The trace of ONE sub-operation (strategy_consult / hypothesis_merge …), fetched lazily by the
// event's own trace_id — so a strategy_decision row shows only the strategist's reasoning, not the
// whole node. Rendered with the same span-tree component as a node's trace.
function OpTrace({ runId, traceId }) {
  const [trace, setTrace] = useState(null)
  const [retryNonce, setRetryNonce] = useState(0)
  useEffect(() => {
    let on = true
    setTrace(null)
    get(runApiPath(runId, `/trace/by_trace/${encodeURIComponent(traceId)}`))
      .then(d => on && setTrace({
        spans: Array.isArray(d?.spans) ? d.spans : [], projection: d?.projection || {},
      }))
      .catch(() => on && setTrace({ spans: [], projection: { unavailable: true } }))
    return () => { on = false }
  }, [runId, traceId, retryNonce])
  const retry = () => { setTrace(null); setRetryNonce(value => value + 1) }
  if (trace === null) return <div className="muted trace-loading" role="status">loading trace…</div>
  return <NodeTrace spans={trace.spans} projection={trace.projection} runId={runId} onRetry={retry} />
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
  // LAZY: fetch only THIS node's bounded/redacted trace projection (/nodes/{nid}/trace — reads just
  // the node's spans via the index, O(node)), and only when the row is expanded. Per-observation
  // bounded/redacted detail is fetched on demand via /spans/{sid}.
  const [nodeTrace, setNodeTrace] = useState(null)
  const [nodeTraceError, setNodeTraceError] = useState(false)
  const [nodeTraceNonce, setNodeTraceNonce] = useState(0)
  // # CODEX AGENT: Use the server's documented default explicitly, then double to its bounded ceiling.
  // This removes the special zero/default request path without changing the first response window.
  const [nodeTraceLimit, setNodeTraceLimit] = useState(512)
  const loadMoreNodeTrace = () => setNodeTraceLimit(value => Math.min(value * 2, NODE_TRACE_CAP_MAX))
  const rawTraceGeneration = Object.hasOwn(e.data || {}, 'generation')
    ? e.data.generation : (e.type === 'node_repaired' ? e.data?.attempt : 0)
  const traceGeneration = Number.isInteger(rawTraceGeneration) && rawTraceGeneration >= 0
    ? rawTraceGeneration : null
  // `liveBuilding` is a Map<nodeId, generation> of every concurrent build; this row live-polls its trace
  // only when it IS one of those exact building lifecycles (right node AND right generation).
  const exactBuilding = liveBuilding != null && traceNid != null && traceGeneration != null
    && liveBuilding[traceNid] === traceGeneration
  // Clear the error flag only on a SUCCESSFUL load (not eagerly at the start of every poll tick):
  // clearing then re-setting each 4s tick made the error/Retry banner flicker on a persistent failure.
  const loadNodeTrace = (alive) => get(runNodeApiPath(runId, traceNid, `/trace?limit=${nodeTraceLimit}`))
    .then(d => { if (alive()) { setNodeTrace(d); setNodeTraceError(false) } })
    .catch(() => { if (alive()) { setNodeTrace(null); setNodeTraceError(true) } })
  const retryNodeTrace = () => {
    setNodeTrace(null)
    setNodeTraceError(false)
    setNodeTraceNonce(value => value + 1)
  }
  usePoll((alive) => loadNodeTrace(alive), 4000,
    [open, readOnly, runId, traceNid, exactBuilding, nodeTraceNonce, nodeTraceLimit],
    { enabled: open && !readOnly && traceNid != null && exactBuilding })
  useEffect(() => {
    if (!open || readOnly || traceNid == null || exactBuilding) return undefined
    let alive = true
    loadNodeTrace(() => alive)
    return () => { alive = false }
  }, [open, readOnly, runId, traceNid, exactBuilding, nodeTraceNonce, nodeTraceLimit])
  const nodeSpans = Array.isArray(nodeTrace?.nodes) ? nodeTrace.nodes : []
  const hasTrace = !readOnly && traceNid != null
  // A sub-operation event the engine wrapped in its OWN named trace (strategy_decision, hypothesis_
  // merged) carries a trace_id — expand to ONLY that operation's trace (lazily fetched by trace_id),
  // never the node's whole Researcher+Developer trace. Old events (no trace_id) fall through to detail.
  const opTraceId = (!readOnly && OP_TRACE_TYPES.has(e.type) && e.trace_id) ? e.trace_id : null
  // A row whose one-line narration clamped text (or used the projected JSON fallback) remains
  // expandable to the detail retained in this bounded event page.
  const isRawFallback = !hasReason && !NARR[e.type]
  const hasGeneric = !hasReason && (genericRows(e).length > 0 || isRawFallback)
  const omittedBytes = e?._log_page?.truncated ? Number(e._log_page.raw_bytes || 0) : 0
  const hasOmittedDetail = e?._log_page?.truncated === true
  const expandable = hasReason || hasTrace || !!opTraceId || hasGeneric || hasOmittedDetail
  const { group, glyph } = kindOf(e.type)
  const narr = eventNarration(e)
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
              onClick={retryNodeTrace}>Retry</button>
          </div>}
          {hasTrace && nodeTrace != null && <NodeTrace spans={nodeSpans}
            projection={nodeTrace.projection} runId={runId} onRetry={retryNodeTrace}
            onLoadMore={nodeTraceLimit < NODE_TRACE_CAP_MAX ? loadMoreNodeTrace : undefined} />}
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
  // without this guard its node_created row would auto-expand the retained span projection forever.
  // A Map<nodeId, generation> of EVERY node building right now (parallel_build>1 builds several at
  // once), so each concurrent build's feed row live-polls its own trace — not just the singular
  // last-appended one. `buildings` is a node_id->marker object; fall back to the singular `building`
  // for a serial-build / old server. null when nothing is live-building (keeps the poll disabled).
  // The marker bag is bounded by parallel_build; projecting it directly is cheaper than memo state
  // and guarantees the generation fence is evaluated on every live render.
  const liveBuilding = readOnly || !atLiveView || timeline.generation !== expectedGeneration
    ? null : buildingGenerations(live)

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
    const narration = eventNarration(event)
    // Unknown/forward-compatible event types are rendered from their projected JSON fallback. Include the
    // same bounded source in search so text the user can plainly see is not reported as "0 matching".
    // Keep the projection capped: Event Explorer owns deeper projected payload inspection, and Timeline
    // may retain 5,000 rows.
    let rawPreview = ''
    try { rawPreview = JSON.stringify(event.data ?? {}).slice(0, 500) } catch { /* cyclic/malformed data */ }
    return { event, search: `${event.type || ''} ${narration} ${rawPreview}`.toLowerCase() }
  }), [log])
  const filterQuery = filter.trim().toLowerCase()
  const kindMatch = (e) => kinds.size === 0 || kinds.has(TYPE2GROUP[e.type] || 'lifecycle')

  // The chronological feed: events, filtered + time-scrubbed.
  const feed = useMemo(() =>
    searchableLog.filter(({ event, search }) => isCuratedType(event.type)
      && (atLiveView || event.seq <= viewSeq)
      && kindMatch(event) && (!filterQuery || search.includes(filterQuery))).map(item => item.event),
    [searchableLog, atLiveView, viewSeq, filterQuery, kinds])
  // Non-curated bookkeeping (per-call cost, finalize gates, concept-cadence sidecars, …) is kept out of
  // the feed but still counted in `timeline.totalEvents`/`timeline.unread` (server counts the raw log).
  // When any such row is in the loaded window, those counts over-state what actually renders — so the
  // pagebar shows a separate "shown" figure and the unread badge falls back to the numberless "new
  // activity" affordance rather than promising a precise count the feed can't honor (allow-list desync).
  const hiddenPresent = useMemo(() => log.some((e) => !isCuratedType(e.type)), [log])
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
    try { await resetRun(runId); onToast?.('replaying from scratch') } catch { onToast?.('Reset could not be submitted. Try again.') }
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
              title={visiblyLive ? '' : 'Jump to latest verified event'}>
          {atLiveView
            ? visiblyLive ? `live · ${liveSeq}` : 'reading · jump latest'
            : `replay ${sliderVal}/${liveSeq} → live`}</button>
        {/* a left-over filter is invisible once controls collapse — surface it so the feed never looks empty for no reason */}
        {!showControls && (filter.trim() || kinds.size > 0) &&
          <button type="button" className="hist-tag-mini hist"
                title="Open active filters" onClick={toggleControls}>⌕ filtered</button>}
        <span className="spacer" />
        <button className={'btn sm ghost' + (showControls ? ' on' : '')} title="Timeline filters"
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
            <input className="text feed-filter" aria-label="Filter loaded events" placeholder="filter events…" value={filter} onChange={e => onFilterChange?.(e.target.value)} />
          </div>
        </div>}
        <div className="timeline-pagebar">
          <button type="button" className="btn sm ghost" disabled={!timeline.hasMore.older || timeline.loading.older}
            onClick={timeline.loadOlder}>{timeline.loading.older ? 'Loading…' : 'Load older'}</button>
          <span className="muted">
            {timeline.totalEvents != null ? `${log.length} loaded of ${timeline.totalEvents}` : `${log.length} loaded`}
            {feed.length !== log.length
              ? ` · ${feed.length} ${(filter.trim() || kinds.size) ? 'matching' : 'shown'}`
              : ''}
          </span>
          {timeline.hasMore.newer && <button type="button" className="btn sm ghost"
            disabled={timeline.loading.newer} onClick={timeline.loadNewer}>
            {timeline.loading.newer ? 'Loading…' : 'Load newer'}</button>}
        </div>
        {(filter.trim() !== '' || kinds.size > 0) && timeline.totalEvents != null && timeline.totalEvents > log.length &&
          <div className="timeline-window-note" role="note">Filters search loaded events only; page for more.</div>}
        {timeline.status === 'loading' && log.length === 0 && <div className="timeline-resource muted" role="status">Loading timeline…</div>}
        {timeline.loading.around && <div className="timeline-resource muted" role="status">Loading around seq {viewSeq}…</div>}
        {timeline.errors.tail && <div className="notice resource-error" role="alert">
          <span>{log.length ? 'Refresh failed; window unchanged.' : timeline.errors.tail}</span>
          <button className="btn sm" onClick={() => timeline.retry('tail')}>Retry</button></div>}
        {timeline.errors.older && <div className="notice resource-error compact" role="alert">
          <span>Older events unavailable.</span><button className="btn sm" onClick={timeline.loadOlder}>Retry</button></div>}
        {timeline.errors.newer && <div className="notice resource-error compact" role="alert">
          <span>Live refresh failed; events may lag.</span><button className="btn sm" onClick={timeline.loadNewer}>Retry</button></div>}
        {timeline.errors.around && <div className="notice resource-error compact" role="alert">
          <span>Replay seq {viewSeq} unavailable.</span>
          <button className="btn sm" onClick={() => timeline.retry('around')}>Retry</button></div>}
        {timeline.tornTail && <div className="timeline-window-note warning" role="status">
          {timeline.sourceTailLimited
            ? 'The raw log tail exceeds the safety limit; showing the last verified canonical prefix.'
            : 'The final source row is incomplete or non-canonical; showing the last verified event prefix.'}
        </div>}
        {feed.length === 0 && timeline.status === 'ready' && !timeline.loading.around
          ? <div className="timeline-resource muted">{(filter.trim() || kinds.size)
              ? 'nothing matches the loaded window'
              : log.length > 0 ? 'only background bookkeeping in this window — page older for run events' : 'no events yet'}</div>
          : <VirtualTimeline rows={feed} getKey={timelineEventKey}
              identity={`${runId}:${timeline.generation || 'pending'}`}
              className="feed chat-feed" ariaLabel="Run events"
              followingTail={atLiveView && timeline.followingTail}
              windowAtTail={atLiveView && timeline.windowAtTail}
              unread={atLiveView && !hiddenPresent ? timeline.unread : 0}
              unreadUnknown={atLiveView && (timeline.unreadUnknown || (hiddenPresent && timeline.unread > 0))}
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
            <LiveTrace runId={runId} generation={timeline.generation} active={atLiveView} />
          </div>
        })()}
        <div className="dock-foot">
          <span className="muted dock-foot-hint">
            {readOnly ? 'Historical timeline — live controls and sidecar trace details are disabled.' : <>
              Steer below by chat or <code className="cmd-hint">/stop · /finalize · /resume · /approve #id</code>.
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
                <span className="transport-detail">{transportPending.observationKind === 'access'
                  ? 'Verify owner access, then check again.'
                  : transportPending.observationKind === 'protocol'
                    ? 'Saved command response unverifiable.'
                    : 'Reconnect and check before another action.'}</span>
                <button className="btn sm" onClick={onCheckTransport}
                  aria-label={`Check preserved ${transportPending.action} command`}>
                  Check command</button>
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
                title="Retry the same durable command">Retry command</button>}
              <button className="btn sm ghost" onClick={dismissTransportFailure}
                title="Dismiss result">Dismiss</button>
            </>}
            {!transportBusy && !transportFailure && mode === 'finalizing' && <span className="muted" role="status">Finalizing…</span>}
            {!transportBusy && !transportFailure && mode === 'finishing' && <span className="muted" role="status">Finishing write-out…</span>}
            {!transportBusy && !transportFailure && mode === 'finalization-stalled' && <>
              <span className="muted" role="alert">Finalization stalled</span>
              <button className="btn sm" onClick={onFinalize}
                title="Resume pending finalization">Reattach finalization</button>
            </>}
            {!transportBusy && !transportFailure && mode === 'running' && <>
              <button className="btn sm" aria-label="Stop run without finalizing"
                title="Stop now; resume or finalize later" onClick={onStop}><OpIcon name="pause" size={13} /></button>
              <button className="btn sm danger" aria-label="Finalize run"
                title="Finalize: stop, report, lessons and cost" onClick={onFinalize}><OpIcon name="stop" size={13} /></button></>}
            {!transportBusy && !transportFailure && (mode === 'paused' || mode === 'stalled') && <>
              <button className="btn sm primary" aria-label="Resume run" title="Continue run" onClick={onResume}><OpIcon name="play" size={13} /></button>
              <button className="btn sm danger" aria-label="Finalize run"
                title="Finalize: stop, report, lessons and cost" onClick={onFinalize}><OpIcon name="stop" size={13} /></button></>}
            {!transportBusy && !transportFailure && mode === 'finished' && <>
              <button className="btn sm primary" aria-label="Resume finished run" title="Reopen and continue" onClick={onResume}><OpIcon name="play" size={13} /></button>
              <button className="btn sm" aria-label="Replay run from scratch" title="Restart from scratch" onClick={onReplay}><OpIcon name="replay" size={13} /></button></>}
          </div>}
        </div>
      </div>}
    </div>
  )
}
