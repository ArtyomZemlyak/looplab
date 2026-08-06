// Pure decisions for "what else is this experiment attached to" — no React, no I/O, so `node --test`
// can drive them directly (the `ui/` house pattern: a pure model beside its React half).
//
// The Inspector answered exactly one question — "what is this node?" — and a node is not a
// self-contained thing. It is one attempt at a Card's question, tagged with concepts, and it may have
// produced cross-run lessons. Those three links are three DIFFERENT KINDS OF CLAIM, and the whole
// reason this module exists is that flattening them into one "related" list would be a lie:
//
//   node -> Card ......... folded run state. EXACT and stable. `Card 1 —— 0..N Node`.
//   node -> concepts ..... folded run state. EXACT. (Already rendered by `Inspector.jsx::ConceptTags`;
//                          what was missing is the CARD's own tags, which are a different set.)
//   node -> lessons ...... NOT folded state and NOT stable. See `nodeLessons` below — the run's event
//                          log is immutable history, the cross-run store is mutable and consolidates,
//                          and the two disagree ON PURPOSE.
//
// `cardBoardModel.js` already owns the Card<->Node join in the other direction (`cardAttempts`,
// `nodeCardId`); this module reuses it rather than re-deriving the edge, because a second spelling of
// "which node belongs to which card" is how the board and the Inspector start disagreeing.

import { isRecord } from './panelPrimitives.js'
import { cardAttempts, cardAttemptSummary, cardText, nodeCardId } from './cardBoardModel.js'

const list = value => Array.isArray(value) ? value : []

/**
 * The Card this experiment was an attempt at — item 3's "links to hypotheses", which since the Card
 * absorbed Hypothesis IS the hypothesis (`core/cards.py`: "The Card IS the research-direction
 * aggregate now (1 card = 1 hypothesis)"; it carries `seed_statement`/`verdict`/`evidence`).
 *
 * Four outcomes, and the middle two are why this is not `state.cards[node.idea.card_id]`:
 *   • `none`     — the node carries no card stamp. Legitimate: `Idea.card_id` is `Optional[str]`, so
 *                  pre-Card runs and externally-created nodes have none. NOT an error.
 *   • `unknown`  — the node names a card the displayed snapshot does not publish. Real: the public
 *                 board is capped at 256 cards (`PUBLIC_CARD_MAX_COUNT`) and a historical fold does
 *                 not contain cards minted after its sequence. Saying "no hypothesis" here would be
 *                 false; saying which id is missing is the honest answer.
 *   • `card`     — resolved.
 *
 * `siblings` is the point of showing it at all: the operator's standing mistake is reading one node
 * as one hypothesis. The count of OTHER attempts at the same question is the cheapest correction, and
 * it comes from the same union `cardAttempts` uses on the board (evidence ∪ card-stamped nodes), so
 * an in-flight build counts even though it is not evidence yet.
 */
export function nodeCardLink(state, node) {
  const cardId = nodeCardId(node)
  if (!cardId) return { kind: 'none', cardId: null, card: null, siblings: [], summary: null }
  const card = isRecord(state?.cards) ? state.cards[cardId] : null
  if (!isRecord(card)) {
    return { kind: 'unknown', cardId, card: null, siblings: [], summary: null }
  }
  const attempts = cardAttempts(state, { ...card, id: cardId })
  const nodeId = Number(node?.id)
  return {
    kind: 'card',
    cardId,
    card,
    // Every OTHER attempt at this Card's question. Ordered and provenance-tagged by `cardAttempts`.
    siblings: attempts.filter(entry => entry.nodeId !== nodeId),
    summary: cardAttemptSummary(attempts),
  }
}

/**
 * Every concept id in play for this experiment, and WHERE each one comes from.
 *
 * Item 5 asked for "all the concepts in the Inspector, maybe that already exists". Half of it did:
 * `Inspector.jsx::ConceptTags` already renders the node's own canonical memberships (and lets an
 * operator re-tag them). What did not exist is the Card's `concept_tags` — a SEPARATE set, derived by
 * `card_ledger.py` from the card's FIRST linked node and then carried across merges, so it can name
 * concepts this particular attempt never carried. Rendering the two in one undifferentiated cloud
 * would let a card-level tag read as a claim about this node's own evidence, which is exactly the
 * kind of quiet upgrade `concept_source` exists to prevent.
 *
 * So: two lanes, never merged, and `cardOnly` is what the Inspector adds beside the existing list.
 */
export function nodeConceptLanes(nodeConcepts, card) {
  const own = new Set(list(nodeConcepts).filter(id => typeof id === 'string' && id))
  const cardTags = list(card?.concept_tags).filter(id => typeof id === 'string' && id)
  return {
    own: [...own],
    card: cardTags,
    cardOnly: cardTags.filter(id => !own.has(id)),
    // The card's tag-ownership receipt, flattened to the one word an operator can act on. `node`
    // means the tags were read off a real node's folded memberships; anything else is weaker.
    cardTagOrigin: cardText(card?.concept_source?.kind) || cardText(card?.provenance_tier) || null,
  }
}
