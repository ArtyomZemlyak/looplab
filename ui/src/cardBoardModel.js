// Pure decisions for the Card board — no React, no I/O, so `node --test` can drive them directly
// (the `ui/` house pattern: a pure model beside its React half).
//
// Everything here exists to state ONE fact the board's React half could not: **a Card is not a
// node.** `looplab/core/cards.py`'s class docstring says "The Card IS the research-direction
// aggregate now (1 card = 1 hypothesis)", and `cards.py:809` declares
// `evidence: list[int]  # node ids that tested it (== node_ids)` — a LIST. A Card can hold zero
// nodes (`engine/card_reservation.py::_record_node_less_card` mints and immediately closes a
// rejected proposal that never gets a Node), and a node can hold no Card (`Idea.card_id` is
// `Optional[str] = None`, `core/models.py:349`). So the relation is
//
//     Card 1 ——— 0..N Node        Node 0..1 Card
//
// A Card is one level ABOVE a node: it is the work item / hypothesis, and its nodes are the
// attempts that tested it — retries, debug children and repeats included. A detail pane that
// rendered one card as one experiment would be lying, which is precisely what `cardAttempts` below
// exists to prevent.

import { isRecord } from './panelPrimitives.js'

// The lifecycle lanes, moved out of CardBoard.jsx so the board and its tests read ONE table.
// `coded` was retained here for a year as a RESERVED lane the fold did not emit. It is now REAL
// (2026-08-14): `card_ledger.py::_apply_card_status` splits the pending branch on the durable
// eval-start boundary, so a speculatively pre-built node that is admitted but not dispatched lands
// here instead of claiming Running. Keeping the row through all that time is why occupying it needed
// no board change — which is the argument for keeping `speculating`/`built-awaiting-commit` too.
export const CARD_COLUMNS = [
  ['proposed', 'Proposed', 'work item is open and has not started'],
  // `speculating` is occupied by the derived `state.card_authoring` overlay (see `cardAuthoring`
  // below), never by the folded status: the requested/attempted receipts are a LIVENESS fact about a
  // process running right now, which `Card.status` — a replay fact — deliberately cannot carry.
  // `built-awaiting-commit` is still unreachable: nothing publishes a commit-buffered owner state.
  ['speculating', 'Speculating', 'speculative build requested'],
  ['building', 'Building', 'code is being produced'],
  ['built-awaiting-commit', 'Awaiting commit', 'build finished; durable node commit is pending'],
  ['coded', 'Coded', 'an experiment is built and waiting to run — it has NOT started'],
  ['running', 'Running', 'evaluation is in flight'],
  ['evaluated', 'Evaluated', 'evidence has reached a verdict'],
  // NOT "the hypothesis was refuted" — that is the verdict column's job (`tested`). This lane says
  // every experiment this card owns ended without producing a result: it crashed, or it was a
  // speculative build discarded before it ever ran (its node budget was refunded).
  ['failed', 'No result', 'every experiment ended without producing one'],
  ['gated', 'Gated', 'trust or breeding gates exclude the available evidence'],
  ['dropped', 'Dropped', 'operator or engine removed the work item'],
]
export const CARD_FROZEN_STATUSES = new Set(
  ['proposed', 'building', 'coded', 'running', 'evaluated', 'failed', 'gated', 'dropped'])
// Rendered only when a card is actually in them. `coded` and `failed` stay here after becoming
// derivable: a serial run reaches neither, and an empty column is a question the operator has to
// answer before they can ignore it.
export const CARD_OPTIONAL_STATUSES = new Set(
  ['speculating', 'built-awaiting-commit', 'coded', 'failed'])
export const CARD_RENDER_LIMIT = 256 // mirrors PUBLIC_CARD_MAX_COUNT at the wire boundary

// WHY a card is not selectable — and whether that is news.
//
// The board used to render one amber `blocked` chip for every value of `selection_ready === false`,
// with the reason only in a `title`. The operator read it as a STATUS ("I thought it was some kind
// of state") and it is not: it says the Card queue will not pick this card up next, which for most
// of these reasons is simply what a card that has already done its work looks like. Measured on the
// live board, all three blocked cards were blocked for one of two reasons — `work_terminal` (its
// experiment finished) and `work_in_flight` (it is running right now) — neither of which is a
// problem, both painted the same amber as an ambiguous ownership receipt.
//
// So the split is LIFECYCLE vs FAULT, and it is the whole point: a card resting after its
// experiment ran needs no colour at all, while a card whose action receipt is incomplete or whose
// provenance cannot be read is a real defect in the ledger and should look like one. The blocker
// names come from `events/card_ledger.py` (`c.selection_blockers`) and the two tables below must
// cover it — `tests`/`cardSelectionBlockers.test.js` derives the vocabulary from this file, so an
// unmapped blocker degrades to a plain "not selectable" rather than vanishing.
const BLOCKER_LIFECYCLE = {
  work_in_flight: 'an experiment is running',
  work_terminal: 'its experiment has finished',
  card_terminal: 'closed',
  merged_work_items: 'merged into another work item',
  freshness_stale: 'superseded by a newer proposal',
  // A RESEARCH IDEA, not a defect and not a legacy row. `identity_not_native` is the DEFAULT
  // `selection_blockers` value (`core/cards.py`), carried by every pure-belief card the Researcher
  // and deep research put on the board: it owns no action, so the Card queue cannot pick it up until
  // something mints a work item for it. Measured on rubertlite-dr-unified-v6, ten of eleven cards
  // were in exactly this state — and reading "legacy work item" (this label's first wording, mine)
  // about the run's own live research would have been ten lies on one screen.
  identity_not_native: 'a research idea, not yet a work item',
}
const BLOCKER_FAULT = {
  action_owner_missing: 'no ownership receipt',
  action_owner_ambiguous: 'two ownership receipts',
  action_receipt_incomplete: 'incomplete ownership receipt',
  freshness_unknown: 'provenance could not be read',
  work_owner_unknown: 'owner unknown',
}
// A pure belief carries all three of these TOGETHER and none of them is news: it owns no action, so
// of course there is no ownership receipt and no action freshness to read. Reporting the pair as
// FAULTS put an amber chip on every research idea on the board. The fault wording stays for a card
// that is missing a receipt it should HAVE — i.e. one whose identity IS native.
const BELIEF_ONLY_BLOCKERS = new Set(
  ['identity_not_native', 'action_owner_missing', 'freshness_unknown'])

// ONE BLOCKER, TWO TRUTHS — and the board was telling both at once, on one card, on one screen.
//
// `work_in_flight` means the card is owned by an experiment that has not reached a terminal. That
// covers two lifecycle states and they are opposites to an operator: an experiment that is RUNNING,
// and one that has been built and admitted but not dispatched. `card_ledger.py::_apply_card_status`
// split those on the durable eval-start boundary on 2026-08-14 (`coded` stopped being a reserved
// lane and became real); this table did not move with it. Measured on the live board 2026-08-19,
// `runs/e5small-dr-unified-v2` card-10: `status: "coded"`, `selection_blockers: ["work_in_flight"]`,
// `selection_provenance.owner_state: "in_flight"`, and node 10 `pending` with `eval_started` false
// — so the lane hint read "an experiment is built and waiting to run — it has NOT started" while
// the chip on that same card read "an experiment is running".
//
// The override is keyed on the CARD'S OWN STATUS rather than on the provenance owner state, because
// the lane hint the chip must agree with is keyed on exactly that. A status with no row here falls
// through to the plain wording, so a lane this build does not know cannot mint a sentence.
const BLOCKER_LIFECYCLE_BY_STATUS = {
  work_in_flight: { coded: 'its experiment is built and has not started' },
}

/**
 * The chip for a card the queue will not pick up: `{tone, label, title}`, or null when it will.
 *
 * `tone` is `'lifecycle'` (this is what a card looks like after it has run — no alarm) or `'fault'`
 * (the ledger says something is wrong). A card with BOTH reports the fault: a real defect is not
 * made less real by also being finished.
 */
export function cardSelectionBlock(card) {
  if (!isRecord(card) || card.selection_ready !== false) return null
  const blockers = Array.isArray(card.selection_blockers) ? card.selection_blockers : []
  // A belief row's three blockers are one fact, not three problems.
  const belief = blockers.length > 0 && blockers.every(name => BELIEF_ONLY_BLOCKERS.has(name))
    && blockers.includes('identity_not_native')
  const faults = belief ? [] : blockers.filter(name => BLOCKER_FAULT[name])
  const lifecycle = blockers.filter(name => BLOCKER_LIFECYCLE[name])
  const unmapped = blockers.filter(name => !BLOCKER_FAULT[name] && !BLOCKER_LIFECYCLE[name])
  const detail = blockers.length
    ? `not selectable: ${blockers.join(', ')}`
    : 'not selectable, and the ledger recorded no reason'
  if (faults.length) {
    return { tone: 'fault', label: BLOCKER_FAULT[faults[0]], title: detail }
  }
  if (lifecycle.length) {
    const name = lifecycle[0]
    const label = BLOCKER_LIFECYCLE_BY_STATUS[name]?.[cardStatus(card)] || BLOCKER_LIFECYCLE[name]
    return { tone: 'lifecycle', label, title: detail }
  }
  // No reason, or only reasons this build does not know: say the honest minimum rather than
  // inventing one. An unknown blocker is a FAULT — a card the queue refuses for a reason nothing can
  // name is exactly the case an operator must be able to see.
  // Distinguish the two in the LABEL, not only the tooltip: "we do not know why" and "nothing was
  // recorded" send an operator to different places.
  return { tone: 'fault', title: detail,
    label: unmapped.length ? 'not selectable (unrecognised reason)' : 'not selectable' }
}

export const cardText = value => typeof value === 'string' && value.trim() ? value.trim() : null
export const cardNumber = value => typeof value === 'number' && Number.isFinite(value) ? value : null
export const cardInt = value => Number.isSafeInteger(value) && value >= 0 ? value : null
export const cardNodes = value => Array.isArray(value)
  ? value.filter(item => Number.isSafeInteger(item) && item >= 0).slice(0, 4096) : []

export function cardStatus(card) {
  return cardText(card?.status) || 'unknown'
}

export function cardStatusLabel(status) {
  return String(status).split(/[-_]/).filter(Boolean)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ') || 'Other'
}

// The in-flight AUTHORING overlay: `state.card_authoring`, produced by
// `looplab/events/authoring_projection.py::card_authoring` — a derived, never-folded projection of
// the open `card_build_requested` head(s). It is the answer to "does a card appear on the board when
// work on it STARTS, or only once it is fully formed?" for the SPECULATIVE lane, where `node_building`
// (the only thing `card_ledger.py::_card_building_ids` stamps `status:'building'` from) is appended
// AFTER the Developer returns. Measured on runs/rubertlite-dr-unified-v5: card-0's build ran 2,128 s
// and the fold said `proposed` for 2,130.7 s of it — the Building lane was occupied for 0.3 s.
//
// This is the bounded speculative owner state the CARD_COLUMNS comment above has been asking for, and
// it is why `speculating` is in that table already: nothing in CardBoard.jsx changes, the optional
// lane simply becomes occupied.
export const AUTHORING_PHASES = new Set(['speculating', 'building'])

export function cardAuthoring(state) {
  // Same liveness rule as `buildingModel.js::withBuilding`, and for the same reason: a run whose
  // engine died mid-build keeps its open head forever, so a "building…" card would breathe for work
  // that will never finish. A PAUSED run deliberately still shows it — `speculation.py::_produce_card_build`
  // runs the Developer under `abandon_on_cancel=False`, so pause/abort waits for the whole provider
  // call and the code really is still being written.
  if (!state || state.finished || state.engine_running === false) return new Map()
  const rows = Array.isArray(state.card_authoring) ? state.card_authoring : []
  const out = new Map()
  for (const row of rows.slice(0, CARD_RENDER_LIMIT)) {
    if (!isRecord(row)) continue
    const cardId = cardText(row.card_id)
    const phase = cardText(row.phase)
    // Unknown phase = a newer server naming a lane this build has no column for. Drop the row rather
    // than inventing a lane: `cardLanes` would happily render it, under a label nobody designed.
    if (!cardId || !AUTHORING_PHASES.has(phase) || out.has(cardId)) continue
    out.set(cardId, { phase, started: cardNumber(row.started), index: cardInt(row.index) })
  }
  return out
}

export function cardRows(state) {
  if (!isRecord(state?.cards)) return []
  const authoring = cardAuthoring(state)
  return Object.entries(state.cards)
    .filter(([id, card]) => typeof id === 'string' && id && isRecord(card))
    .slice(0, CARD_RENDER_LIMIT)
    // The mapping key is authoritative. Never let a malformed/spoofed body id change joins or receipts.
    .map(([id, card]) => {
      const live = authoring.get(id)
      // THE FOLD WINS whenever it has anything to say. The overlay may only move a card OUT of
      // `proposed` — the one lane that is a lie while a Developer is writing the card's code. It must
      // never pull a card back out of building/running/evaluated/gated/dropped, because those are
      // replay facts and this one is a statement about a process that is running right now.
      if (!live || cardStatus(card) !== 'proposed') return { ...card, id }
      // `status` is overlaid, `selection_ready` is NOT: it stays true on purpose while the head is
      // open (see `card_ledger.py::_card_building_ids` — the servicer of that very head re-folds and
      // requires it), so flipping it here would contradict the engine to make a chip look tidier.
      return { ...card, id, status: live.phase, authoring: { ...live, folded_status: cardStatus(card) } }
    })
}

export function cardLanes(cards) {
  const occupied = new Set(cards.map(cardStatus))
  const configured = CARD_COLUMNS.filter(
    ([status]) => !CARD_OPTIONAL_STATUSES.has(status) || occupied.has(status))
  const known = new Set(CARD_COLUMNS.map(([status]) => status))
  const extra = [...occupied].filter(status => !known.has(status)).sort()
    .map(status => [status, cardStatusLabel(status), 'new derived lifecycle status'])
  const dropped = configured.find(([status]) => status === 'dropped')
  return [...configured.filter(([status]) => status !== 'dropped'), ...extra, dropped].filter(Boolean)
}

export function cardOrder(a, b) {
  const ap = cardNumber(a.priority) ?? cardNumber(a.foresight_rank) ?? Infinity
  const bp = cardNumber(b.priority) ?? cardNumber(b.foresight_rank) ?? Infinity
  if (ap !== bp) return ap - bp
  const an = cardInt(a.created_at_node) ?? Infinity
  const bn = cardInt(b.created_at_node) ?? Infinity
  return an - bn || a.id.localeCompare(b.id)
}

// ---------------------------------------------------------------------------------------------
// The 1-Card -> N-Node join. This is the whole point of the module.
// ---------------------------------------------------------------------------------------------

// The node -> card edge as the wire actually carries it. NOTE the nesting: the durable stamp lives
// on the IDEA (`core/models.py:349` is `Idea.card_id`, inside the `Idea` class that opens at
// models.py:282) — there is NO top-level `card_id` on the node DTO. Measured against
// runs/spec-live-0804 on 2026-08-06: every node dumped `card_id` absent at the top level and
// `idea.card_id = "card-N"` one level down. Reading `node.card_id` therefore returns `undefined`
// for every node in every run, which is exactly the kind of join that fails silently — it does not
// throw, it just reports that no node belongs to any card.
export function nodeCardId(node) {
  if (!isRecord(node)) return null
  const idea = isRecord(node.idea) ? node.idea : null
  return cardText(idea?.card_id)
}

/**
 * Every node attempt that belongs to `card`, as an ordered list of records.
 *
 * The union of TWO sources, because neither is complete on its own:
 *
 *   - `card.evidence` — `cards.py:809`, "node ids that tested it". The fold appends a node here
 *     only from `events/card_ledger.py`'s node-linking pass, and the verdict/`best_delta`/status
 *     roll-ups all read exactly this list. It is the AUDIT set.
 *   - every node whose `idea.card_id` names the card — the durable stamp the engine writes when it
 *     mints the Card's action. A node that is still BUILDING, or that failed before producing
 *     evidence, or whose card was skipped by the ledger's ambiguity gate, carries this and only
 *     this. `card_ledger.py:1757` is explicit that "a build reservation is not evidence yet".
 *
 * Reporting only `evidence` would tell the operator a card has no attempts while a node for it is
 * running in front of them; reporting only the stamp would drop every hash-joined legacy card,
 * whose nodes predate `Idea.card_id` entirely. So: union, with the provenance of each edge kept
 * (`evidence` / `owned`) rather than flattened, because "this node produced the verdict" and "this
 * node was reserved for the card" are different claims and the pane must not conflate them.
 *
 * `present: false` means the id resolves to no node in `state.nodes`. That happens for real — a
 * historical fold does not contain nodes created after its sequence, and the live state trims. Such
 * an id is RETAINED and marked, never silently dropped: a pane that quietly shrank a card's
 * attempt list would misreport how much work the card cost.
 */
export function cardAttempts(state, card) {
  if (!isRecord(card)) return []
  const cardId = cardText(card.id)
  const nodes = isRecord(state?.nodes) ? state.nodes : {}
  const byId = new Map()
  const touch = id => {
    if (!byId.has(id)) {
      const node = isRecord(nodes[id]) ? nodes[id] : null
      byId.set(id, { nodeId: id, evidence: false, owned: false, present: !!node, node })
    }
    return byId.get(id)
  }
  for (const id of cardNodes(card.evidence)) touch(id).evidence = true
  if (cardId) {
    for (const [key, node] of Object.entries(nodes)) {
      if (nodeCardId(node) !== cardId) continue
      // The mapping KEY is authoritative for the node id, exactly as it is for the card id above:
      // a body whose `id` disagrees with its key must not be able to redirect the join.
      const id = Number(key)
      if (!Number.isSafeInteger(id) || id < 0) continue
      touch(id).owned = true
    }
  }
  return [...byId.values()].sort((a, b) => a.nodeId - b.nodeId)
}

// A one-line honest summary of a card's node population, for the lane card and the pane header.
// `missing` is called out separately because "3 attempts" and "3 attempts, 1 of them unavailable in
// this snapshot" are different facts and only the second one is true in a historical fold.
export function cardAttemptSummary(attempts) {
  const list = Array.isArray(attempts) ? attempts : []
  const present = list.filter(entry => entry.present)
  const counts = {}
  for (const entry of present) {
    const status = cardText(entry.node?.status) || 'unknown'
    counts[status] = (counts[status] || 0) + 1
  }
  return {
    total: list.length,
    missing: list.length - present.length,
    evidence: list.filter(entry => entry.evidence).length,
    ownedOnly: list.filter(entry => entry.owned && !entry.evidence).length,
    statuses: counts,
  }
}

// ---------------------------------------------------------------------------------------------
// Item 6: what else is worth showing for a Card. The survey behind these two is in the commit that
// added them — every `Card` field is already ON the wire (`serve/public_cards.py::_FIELDS` publishes
// all 41 and receipts every omission), so "what else to show" is a rendering question, not a
// projection one, with exactly one exception: the per-card event history lives in
// `INTERNAL_CARD_STATE_FIELDS` and is excluded at `appstate.py`, so a card TIMELINE is the one
// feature that would need a new wire field. These two are the ones with real content behind them.
// ---------------------------------------------------------------------------------------------

// The 17 structured cue kinds `core/cards.py:193-219` closes over, in the operator's words. This is a
// LABEL table, not a validator: an unknown kind renders its own id rather than being dropped, because
// the vocabulary is versioned server-side and a silently-hidden new cue is worse than an ugly one.
const STEERING_CUES = {
  complexity: 'task complexity', eval_budget: 'evaluation budget',
  experiment_time_budget: 'experiment time budget', gpu_constraint: 'GPU constraint',
  failure_reflection: 'a failure reflection', watchdog_reflection: 'a watchdog reflection',
  trust_reflection: 'a trust reflection', fault_localization: 'fault localization',
  feature_engineering: 'feature engineering', reflection_prior: 'a reflection prior',
  cross_run_advisory: 'a cross-run advisory', cross_run_tools: 'cross-run tools',
  concept_authoring: 'concept authoring', concept_slug_reuse: 'concept slug reuse',
  research_memo: 'a research memo', strategy: 'the Strategist', sweep: 'a sweep',
}

/**
 * Why this work item was proposed — `steering_context`, plus the provenance fields beside it.
 *
 * `steering_context` is a compact STRUCTURED snapshot of the cues that were in scope when the card
 * was minted (deliberately no verbatim capture). It is fully modelled, fully serialized, receipted
 * for completeness — and rendered nowhere, which makes "why is this card here?" unanswerable from the
 * UI even though the answer shipped. Each cue's extra keys travel as `detail` rather than being
 * flattened into the label, so a cue that grows a field shows it instead of losing it.
 *
 * `paraphrased` is the other half: `statement` is an operator-editable display overlay and
 * `seed_statement` is the immutable value the whole ledger joins on. Showing only the paraphrase
 * hides that an operator rewrote the question.
 */
export function cardOrigin(card) {
  const seed = cardText(card?.seed_statement)
  const shown = cardText(card?.statement)
  return {
    seed,
    paraphrased: !!seed && !!shown && seed !== shown,
    rationale: cardText(card?.rationale),
    createdAtNode: cardInt(card?.created_at_node),
    // Ids folded INTO this canonical card by a merge. Never shown before, so a card that absorbed
    // three sibling proposals looked identical to one that was minted alone.
    aliases: Array.isArray(card?.aliases) ? card.aliases.filter(id => cardText(id)) : [],
    cues: (Array.isArray(card?.steering_context) ? card.steering_context : [])
      .filter(isRecord)
      .map(cue => {
        const kind = cardText(cue.kind)
        if (!kind) return null
        const detail = Object.entries(cue)
          .filter(([key, value]) => key !== 'kind' && (typeof value === 'string' || typeof value === 'number'))
          .map(([key, value]) => `${key} ${value}`)
        return { kind, label: STEERING_CUES[kind] || kind, detail }
      })
      .filter(Boolean),
  }
}

/**
 * What this work item TAUGHT — `Card.lesson_refs` resolved to real statements.
 *
 * `lesson_refs` holds opaque `lesson:sha256:…` ids (stamped by `engine/research_cadence.py` onto
 * `card_enriched`) and there is NO resolver from one back to a row in `lessons.jsonl` — the store
 * never writes the id. That made the field unrenderable, and it has been carried unused since it
 * shipped. But the SAME id is minted into this run's own `lessons_distilled` beside its statement, so
 * the run's event log resolves it exactly and for free.
 *
 * `unresolved` is returned rather than swallowed: an id whose lesson was distilled in an EARLIER run
 * (cross-run priors can carry them) has no entry in this log, and a card claiming fewer lessons than
 * it references would be quietly wrong.
 */
export function cardLessons(state, card) {
  const refs = (Array.isArray(card?.lesson_refs) ? card.lesson_refs : []).filter(id => cardText(id))
  if (!refs.length) return { lessons: [], unresolved: [] }
  const byId = new Map()
  for (const batch of Array.isArray(state?.lessons_distilled) ? state.lessons_distilled : []) {
    for (const lesson of Array.isArray(batch?.lessons) ? batch.lessons : []) {
      const id = isRecord(lesson) ? cardText(lesson.lesson_id) : null
      const statement = isRecord(lesson) ? cardText(lesson.statement) : null
      if (id && statement && !byId.has(id)) {
        byId.set(id, {
          lessonId: id, statement, outcome: cardText(lesson.outcome),
          evidence: cardNodes(lesson.evidence),
        })
      }
    }
  }
  const seen = new Set()
  const lessons = []
  const unresolved = []
  for (const id of refs) {
    if (seen.has(id)) continue
    seen.add(id)
    const found = byId.get(id)
    if (found) lessons.push(found)
    else unresolved.push(id)
  }
  return { lessons, unresolved }
}

// Which card the route's `card=` target resolves to. Deliberately NEVER auto-picks a fallback: a
// shared link that silently opened a DIFFERENT card than the one it names would be worse than an
// empty pane, and the empty pane is recoverable by clicking a lane card.
export function resolveSelectedCard(cards, cardId) {
  const wanted = cardText(cardId)
  if (!wanted) return null
  return (Array.isArray(cards) ? cards : []).find(card => card.id === wanted) || null
}
