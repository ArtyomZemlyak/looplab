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
// `coded` is deliberately retained: `core/cards.py:784-793` documents it as a RESERVED lane the
// fold does not currently emit (every pending Node collapses to `running`), and dropping it here
// would silently change the board's contract the day the projection starts producing it.
export const CARD_COLUMNS = [
  ['proposed', 'Proposed', 'work item is open and has not started'],
  // these speculative lanes are unreachable from the production Card projection:
  // requested/done receipts live only in recovery journals, while public status derives from
  // proposed/building/running/gated/evaluated/dropped. Paid in-flight or commit-buffered work therefore
  // appears Proposed; project a bounded speculative owner state before advertising these lanes.
  ['speculating', 'Speculating', 'speculative build requested'],
  ['building', 'Building', 'code is being produced'],
  ['built-awaiting-commit', 'Awaiting commit', 'build finished; durable node commit is pending'],
  ['coded', 'Coded', 'code exists and is waiting to run'],
  ['running', 'Running', 'evaluation is in flight'],
  ['evaluated', 'Evaluated', 'evidence has reached a verdict'],
  ['gated', 'Gated', 'trust or breeding gates exclude the available evidence'],
  ['dropped', 'Dropped', 'operator or engine removed the work item'],
]
export const CARD_FROZEN_STATUSES = new Set(
  ['proposed', 'building', 'coded', 'running', 'evaluated', 'gated', 'dropped'])
export const CARD_OPTIONAL_STATUSES = new Set(['speculating', 'built-awaiting-commit', 'coded'])
export const CARD_RENDER_LIMIT = 256 // mirrors PUBLIC_CARD_MAX_COUNT at the wire boundary

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

export function cardRows(state) {
  if (!isRecord(state?.cards)) return []
  return Object.entries(state.cards)
    .filter(([id, card]) => typeof id === 'string' && id && isRecord(card))
    .slice(0, CARD_RENDER_LIMIT)
    // The mapping key is authoritative. Never let a malformed/spoofed body id change joins or receipts.
    .map(([id, card]) => ({ ...card, id }))
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
