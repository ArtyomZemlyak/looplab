// Pure projection: splice EVERY in-flight build (`node_building` marker) into `state.nodes` as a
// synthetic `status:'building'` node, so the DAG / list / panels render each the INSTANT work starts on
// it — before its node_created folds. Kept out of the real event-sourced node set on the backend (id
// allocation), so this transform is a pure UI-side overlay.
//
// Under parallel_build>1 several nodes build at once: the server sends them all in `state.buildings`
// (a node_id->marker object), with the singular `state.building` kept as the LAST-appended one for
// back-compat. We render every entry. Falls back to the singular marker for a serial-build run or an
// older server that doesn't send `buildings`. Extracted from hooks.js so it can be unit-tested without
// pulling in React.
// Keep legacy-singular and parallel marker decoding in one pure projection. Dock status,
// trace polling and synthetic DAG nodes previously disagreed at the edges because each decoded it.
export function buildingMarkers(state) {
  const bag = state?.buildings
  const markers = bag && typeof bag === 'object' ? Object.values(bag) : state?.building ? [state.building] : []
  return markers.filter(marker => marker?.node_id != null && typeof marker.node_id !== 'boolean')
}

export function buildingGenerations(state) {
  const generations = {}
  let found = false
  for (const marker of buildingMarkers(state)) {
    const nodeId = Number(marker.node_id)
    const generation = Object.hasOwn(marker, 'generation')
      ? marker.generation : (state?.nodes?.[nodeId]?.attempt ?? 0)
    if (!Number.isInteger(nodeId) || nodeId < 0 || !Number.isInteger(generation) || generation < 0) continue
    generations[nodeId] = generation
    found = true
  }
  return found ? generations : null
}

export function withBuilding(state) {
  // Don't splice phantom "building…" cards once the run is over: a finished run clears the marker
  // server-side, but a STALLED run (engine died mid-build, engine_running===false, not finished) would
  // otherwise leave a breathing card for a node that will never appear.
  if (!state || state.finished || state.engine_running === false || !state.nodes) return state
  const markers = buildingMarkers(state)
  if (!markers.length) return state
  const nodes = { ...state.nodes }
  let changed = false
  for (const b of markers) {
    // The cloned node map is also the duplicate set: after the first marker lands, the same id and any
    // real node are both already present. Never overwrite either with a ghost.
    if (nodes[b.node_id]) continue
    const operator = b.operator || 'improve'
    changed = true
    nodes[b.node_id] = {
      id: b.node_id, operator, parent_ids: b.parent_ids || [], status: 'building', building: true,
      activity: {
        schema: 1, status: 'building', generation: b.generation ?? 0,
        evidence: 'node_building', ...(Number(b.started) > 0 ? { started_at: Number(b.started) } : {}),
      },
      idea: { operator, rationale: 'building…' },
    }
  }
  return changed ? { ...state, nodes } : state
}

// ---------------------------------------------------------------- live PHASE of an in-flight build
//
// The marker above answers "which nodes are being built". It cannot answer "and what is happening
// inside that build right now", and for a long time nothing could: `node_building` is appended AFTER
// the Researcher's proposal returns, so the whole proposal — routinely the longest single wait in the
// loop — happens with no marker, no node and no signal at all. The strip fell through to "Planning
// next experiment…" and stayed there, which is the operator's report that a build is a blank panel.
//
// The engine now emits a `phase_progress` beacon at each step boundary of a build and of a resume
// (`looplab/events/types.py::EV_PHASE_PROGRESS`). This is the pure fold over those rows. It reads the
// EVENT LOG rather than `state`, deliberately: the beacon is a DIAGNOSTIC event, so `replay.fold`
// ignores it by design and it will never appear in the state snapshot — and it must not, because it
// is transient progress and a resume has to rebuild the same RunState with or without it.
//
// Keeping the decode here rather than in Dock is the same lesson the marker decode above records:
// the strip, the node card and the age clock each need this answer, and three decoders drift.

// A beacon's identity for open/close matching. The node id is part of it because parallel builds run
// the SAME phase for different nodes at once; a batch proposal legitimately carries no node id at all
// (it proposes several ideas before any id exists), and `null` is a real, distinct key — not a gap.
const phaseKey = (d) => `${d.stage}|${d.phase}|${d.node_id ?? ''}`

const asNodeId = (value) => {
  const n = Number(value)
  return Number.isInteger(n) && n >= 0 && typeof value !== 'boolean' ? n : null
}

// Every beacon that has STARTED and not yet finished, oldest first.
//
// Tolerates a windowed log in both directions, which is not a nicety — the Dock holds a bounded page
// of events, so at any moment the log can begin mid-build. A `finished` whose `started` scrolled out
// of the window closes nothing and is DROPPED rather than counted as an open phase (the opposite
// choice would invent a phase that already ended). A `started` whose `finished` has not arrived is
// exactly the live case this exists for.
export function openPhases(log) {
  const open = new Map()
  for (const event of Array.isArray(log) ? log : []) {
    if (event?.type !== 'phase_progress') continue
    const d = event.data
    if (!d || typeof d !== 'object' || typeof d.stage !== 'string' || typeof d.phase !== 'string') continue
    const key = phaseKey(d)
    if (d.status === 'started') {
      open.set(key, {
        stage: d.stage,
        phase: d.phase,
        nodeId: asNodeId(d.node_id),
        operator: typeof d.operator === 'string' ? d.operator : '',
        // A batch proposal's honest shape: how many ideas, no id for any of them yet.
        count: Number.isInteger(d.count) && d.count > 0 ? d.count : null,
        prospective: d.prospective === true,
        speculative: d.speculative === true,
        // The EVAL cursor's detail. A build phase carries none of it and keeps nulls, exactly as an
        // eval beacon keeps the build's `operator`/`count` empty — one record shape, each producer
        // filling its own half, so a consumer never has to know which stage it is looking at to
        // read the fields it cares about.
        //
        // `name` is the AGENT'S OWN word for the step (`mine`, `train`, `score`) and proves nothing
        // about what the step does; `role` is the authenticated half, resolved by the engine from
        // the manifest through `train_monitor.eval_log_plan`. A surface may SHOW the name and may
        // only CLAIM from the role — see `evalStageLabel`.
        name: typeof d.name === 'string' ? d.name : '',
        role: typeof d.role === 'string' ? d.role : '',
        // The lifecycle this beacon is about. Absent on a build phase and on any legacy row, which
        // `evalStageFor` treats as "no claim" rather than as a mismatch.
        generation: Number.isInteger(d.generation) && d.generation >= 0 ? d.generation : null,
        // 0-based. Each is validated on its own here; whether the PAIR is coherent is decided
        // where it is rendered (`evalStageLabel`), because that is the only place an incoherent
        // pair does harm — "step 4 of 3" reads as a bug in the run rather than in the strip.
        index: Number.isInteger(d.index) && d.index >= 0 ? d.index : null,
        total: Number.isInteger(d.total) && d.total > 0 ? d.total : null,
        ts: Number.isFinite(Number(event.ts)) && Number(event.ts) > 0 ? Number(event.ts) : null,
      })
    } else if (d.status === 'finished') {
      open.delete(key)
    }
  }
  return [...open.values()]
}

// The ONE phase a status surface should name, or null.
//
// The most recently STARTED open beacon wins, because build phases NEST: `novelty` runs inside
// `propose`, and while both are open the inner one is the true answer to "what is it doing right
// now". Under a parallel build several siblings are open at the same depth and any of them is an
// equally true answer; the caller already reports the parallel COUNT from the markers, so this only
// has to name the phase, and naming the newest keeps it moving rather than pinning to a straggler.
//
// Returns null once the run is over or the engine is gone, for the same reason `withBuilding` refuses
// to splice then: a run whose engine died mid-build leaves every beacon open forever, and a phase
// label ticking upward on a dead run is a worse lie than no label. `finished` alone is not enough —
// a STALLED run is not finished, which is exactly the case that was rendering a phantom.
// `stage` scopes the answer to one producer. The Dock needs that: it appends a build's PARALLEL
// COUNT (from `node_building` markers) to whatever this returns, and once eval beacons share the
// stream the newest-open record can be an eval stage — which would have read "Stage train · 2 of 3
// (3 in parallel)", a build's fan-out reported about an evaluation. Unfiltered stays the default so
// every existing caller is byte-identical.
export function livePhase(state, log, stage = null) {
  if (!state || state.finished || state.engine_running === false) return null
  const open = openPhases(log).filter(record => !stage || record.stage === stage)
  if (!open.length) return null
  let newest = open[0]
  for (const record of open) {
    // A beacon with no usable ts still counts as open — it just cannot win the recency comparison.
    if (record.ts != null && (newest.ts == null || record.ts >= newest.ts)) newest = record
  }
  return newest
}

// -------------------------------------------------------------- live STAGE of an in-flight eval
//
// The same question one level down, and the one the whole status vocabulary could not answer: a node
// whose evaluation owns it has been "Training / evaluating" for hours, and on a real repo pipeline
// (`mine` -> `train` -> `score`) that sentence is FALSE for two of the three stages. `stage_finished`
// is folded but lands only at a stage's COMPLETION, so between two rows the record said nothing at
// all — which is why the engine's own watchdog had to guess the running stage from freshest-mtime.
//
// `PROGRESS_STAGE_EVAL` beacons are that cursor. This is the node-keyed selector over them.
//
// Keyed by node AND generation: a reset's abandoned lifecycle and its replacement are different
// experiments, and `nodeActivity.js::markerFor` already refuses to let a stale build marker claim a
// new lifecycle. An eval cursor with no generation is a legacy row and is matched on node alone,
// which is the same tolerance that rule grants.
export function evalStages(log) {
  const byNode = new Map()
  for (const record of openPhases(log)) {
    if (record.stage !== 'eval' || record.phase !== 'stage' || record.nodeId == null) continue
    byNode.set(record.nodeId, record)
  }
  return byNode
}

export function evalStageFor(node, log) {
  const id = Number(node?.id)
  if (!Number.isInteger(id)) return null
  const record = (log instanceof Map ? log : evalStages(log)).get(id)
  if (!record) return null
  // The generation fence. `openPhases` carries no generation of its own, so it is read back off the
  // beacon's node key against the node's current attempt only when BOTH sides carry one — absent on
  // either side is a legacy row, and refusing it would silently retire the signal on an older server.
  const generation = record.generation
  if (Number.isInteger(generation) && Number.isInteger(Number(node?.attempt))
      && generation !== Number(node.attempt)) return null
  return record
}

// WHAT A SURFACE MAY SAY ABOUT A RUNNING STAGE, and the split is the whole point.
//
// `role` is authenticated — the engine resolved it from the manifest via `eval_log_plan`, which
// spends a page of its own docstring on why a stage NAME proves nothing. So "Training" is claimed
// ONLY from `role === 'training'`. Everything else shows the stage's own name, which is honest for
// a different reason: it is the word the agent chose and the word this operator already reads in the
// stage strip, the logs tab and the trace bands, so it names the step without asserting what it does.
//
// A pipeline that declares no role therefore reads "Stage `train` · 2 of 3" rather than "Training".
// That is deliberately weaker than it could be: `train` is the overwhelmingly common slug and
// guessing from it would be right most of the time, which is exactly what makes the guess dangerous
// — `eval_log_plan` refuses the same inference for kill authority, and a status surface that quietly
// applied a looser rule would let an operator read an engine claim into a string the agent picked.
// The trimmed name and the index/total coherence rule, decided ONCE for both widths. The two label
// functions below differ only in wording; when each carried its own copy of this guard, a change
// landing in one (say, admitting `total === 1`) made the strip and the node card disagree about the
// same running stage — the exact drift the shared-label design exists to prevent.
function stageParts(record) {
  if (!record || record.stage !== 'eval') return null
  const name = typeof record.name === 'string' ? record.name.trim() : ''
  const step = Number.isInteger(record.index) && Number.isInteger(record.total)
    && record.total > 1 && record.index < record.total
    ? { index: record.index, total: record.total } : null
  return { name, step }
}

export function evalStageLabel(record) {
  const parts = stageParts(record)
  if (!parts) return null
  const step = parts.step ? ` · ${parts.step.index + 1} of ${parts.step.total}` : ''
  if (record.role === 'training') {
    return `Training${parts.name && parts.name !== 'eval' ? ` (${parts.name})` : ''}${step}`
  }
  if (!parts.name) return `Evaluating${step}`
  return `Stage ${parts.name}${step}`
}

// The same answer at chip width. The name alone once the run has more than one stage to place it in,
// because a 10px chip that spends its width on the word "stage" says less than one that spends it on
// the word the operator is looking for.
export function evalStageShortLabel(record) {
  const parts = stageParts(record)
  if (!parts) return null
  const step = parts.step ? ` ${parts.step.index + 1}/${parts.step.total}` : ''
  if (record.role === 'training' && (!parts.name || parts.name === 'eval')) return `training${step}`
  return `${parts.name || 'evaluating'}${step}`
}

// The words. A closed table rather than string-building at the call site, so the vocabulary is
// reviewable in one place and a phase the engine adds without a label here renders as null (the
// caller's existing text) instead of as "undefined".
const PHASE_TEXT = {
  'build|propose': (r) => (r.count && r.count > 1
    ? `Proposing ${r.count} experiments…`
    : `Proposing experiment${r.nodeId != null ? ` #${r.nodeId}` : ''}…`),
  'build|novelty': (r) => `Checking experiment${r.nodeId != null ? ` #${r.nodeId}` : ''} is not a repeat…`,
  'build|reserve': (r) => `Reserving experiment${r.nodeId != null ? ` #${r.nodeId}` : ''}…`,
  'build|implement': (r) => `Writing code for experiment${r.nodeId != null ? ` #${r.nodeId}` : ''}…`,
  // The EVAL cursor's own sentence. It defers to `evalStageLabel` so the strip and the node card
  // cannot come to describe the same running stage differently — the rule about what may be CLAIMED
  // (`role`) versus merely SHOWN (`name`) is stated once, there.
  'eval|stage': (r) => `${evalStageLabel(r)}${r.nodeId != null ? ` — experiment #${r.nodeId}` : ''}…`,
  // NO `build|repair` row, and its absence is load-bearing rather than an oversight. That phase
  // existed for one day: it bracketed `developer.repair` inside the `debug` operator's build branch,
  // and F5 deleted that branch on 2026-08-13. `events/types.py::PROGRESS_PHASES` dropped it in the
  // same change and `assert_progress_phase` now REFUSES the triple at every append site — so the row
  // that stayed here was a label for a state nothing can emit, which is the same dead-vocabulary
  // shape as a styled CSS class no component renders. `tests/test_phase_progress.py` derives this
  // table from the registry, so the two can no longer drift in either direction: an unlabelled phase
  // would render as the caller's fallback text, and a labelled non-phase is dead code that reads as
  // coverage.
  // A RESUME is the other blank wait and it is deliberately absent, not forgotten. The engine cannot
  // emit a beacon from its run prologue: that is where the authorization fences, the finalize-scope
  // reconciliation and the width pins all read the raw log, and appended rows moved a PAID finalize
  // decision (see `events/types.py::PROGRESS_STAGES`). Making a resume visible needs a channel that
  // is not the event log. This table is the place that would grow a row for it, so the note lives
  // here too — a label added without that channel renders a phase nothing emits.
}

export function phaseLabel(record) {
  if (!record) return null
  const render = PHASE_TEXT[`${record.stage}|${record.phase}`]
  if (!render) return null
  const text = render(record)
  // A speculative build is real work on a node that may never be kept. Saying so is the difference
  // between "the run is busy" and "the run is busy on something it might throw away".
  return record.speculative ? `${text} (speculative)` : text
}
