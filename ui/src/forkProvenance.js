// Fork-to-branch, READ side: what a later reader is allowed to conclude about a branched node's
// idea. The WRITE side is `forkFromSeqModel.js` (what a branch SENDS); this is the pure model behind
// the two surfaces that show one back — `Dag.jsx`'s node card and `Inspector.jsx`'s Overview — the
// same split, and for the same reason, as `stageAttribution.js` / `Inspector.jsx::StagePipeline`.
//
// WHY IT EXISTS. A branched node's idea is part operator-authored and part inherited from the node it
// was branched off. Until this module landed, nothing in the browser read `Node.forked_from` at all:
// the DAG drew a chip for a cross-run `origin` and one for a `research_origin`, and drew NOTHING for
// an operator branch, while the Inspector rendered the branch's `Rationale` under a bare heading. So
// a rationale the operator never wrote — carried across verbatim from the parent — read as this
// experiment's own justification, and one they did write read as the Researcher's. That is the exact
// misreading the receipt was stamped to prevent, and the record was the only place it was prevented.
//
// THE ONE DECISION THIS MODULE MUST NOT GET WRONG is how much the receipt licenses saying, because
// every wrong answer here is a confident lie about who authored an experiment:
//
//  * `changed_fields` IS NOT "what the operator changed". It is a raw diff of two Ideas, and a branch
//    differs from its parent for two unrelated reasons — the operator edited something, and the
//    gesture deliberately does not carry the parent's engine bookkeeping across (`card_id`,
//    `hypothesis`, `footprint`, `theme`, the concept envelope; see `forkFromSeqModel.js`'s
//    `FORK_IDEA_FIELDS` for why each is left behind). Measured against the toy run in
//    `tests/test_fork_from_seq.py`, an operator who edits exactly two things gets a three-field diff;
//    against a Researcher-built parent it is eight fields for the same two edits.
//  * so the SERVER splits it (`control_validation.py::_normalize_fork_receipt` stamps
//    `authored_fields` / `not_carried_fields`) and this module reads the split rather than
//    re-deriving one. It cannot be re-derived here: the node's idea DRIFTS after intake — the
//    Developer's footprint finalization mints a `footprint` the submission had none of — and the
//    parent may since have been reset out of the state on screen.
//  * a receipt written before that split existed carries neither key. That is `legacy`, and it is not
//    the same as "the operator changed nothing": the complement of `changed_fields` is still sound
//    (a field this node carries that the receipt does not list as different from its base IS the
//    base's value), so `inherited` survives while `edited` does not. The same ladder
//    `nodeConceptLanes` uses for "concepts unavailable, not empty".

// How much of the split the receipt on this node actually carries. Ordered weakest-last on purpose:
// a reader that only checks `=== 'stamped'` degrades to showing nothing rather than to guessing.
export const FORK_ATTRIBUTION_STAMPED = 'stamped'
export const FORK_ATTRIBUTION_LEGACY = 'legacy'
export const FORK_ATTRIBUTION_UNRECORDED = 'unrecorded'

/**
 * Does this idea actually PUT SOMETHING in the field?
 *
 * Mirrors `looplab/core/cards.py::idea_field_carried`, which is what stamped the receipt being read
 * — the two must agree or a field the server called "not carried" reads here as inherited. Not "is
 * the key present": the wire dump of an `Idea` always holds every field, so the key set answers
 * nothing. `0` / `0.0` / `false` are values an operator can mean and ARE carried.
 */
export function forkIdeaFieldCarried(value) {
  if (value === undefined || value === null || value === '') return false
  if (Array.isArray(value)) return value.length > 0
  if (typeof value === 'object') return Object.keys(value).length > 0
  return true
}

const strings = value => (Array.isArray(value) && value.every(f => typeof f === 'string')
  ? value : null)

/**
 * Read `node.forked_from` into the split a reader may act on, or `null` for a node nobody branched.
 *
 * `null` is load-bearing: every caller uses it to mean "make NO claim about this node's authorship",
 * which is right for a Researcher-proposed node and for one whose receipt is unreadable alike.
 */
export function forkProvenance(node) {
  const receipt = node && typeof node === 'object' ? node.forked_from : null
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)) return null
  const parentId = Number.isInteger(receipt.node_id) ? receipt.node_id : null
  if (parentId === null) return null
  const idea = (node.idea && typeof node.idea === 'object') ? node.idea : {}
  const carried = Object.keys(idea).filter(f => forkIdeaFieldCarried(idea[f]))
  const base = {
    parentId,
    generation: Number.isInteger(receipt.generation) ? receipt.generation : null,
    observedSeq: Number.isInteger(receipt.observed_seq) ? receipt.observed_seq : null,
    baseDigest: typeof receipt.base_idea_digest === 'string' ? receipt.base_idea_digest : null,
  }
  const changed = strings(receipt.changed_fields)
  if (!changed) {
    // The branch is proven and its attribution is not. Saying "nothing changed" here would be a
    // stronger statement than the record makes, and it is the one that reads as the Researcher's.
    return { ...base, attribution: FORK_ATTRIBUTION_UNRECORDED,
             changed: [], edited: [], inherited: [], notCarried: [], unattributed: carried }
  }
  const changedSet = new Set(changed)
  // Sound at every attribution level: a field this node carries that the receipt does not list as
  // different from its base holds the BASE's own value. Complement of `changed_fields` and never of
  // `authored_fields`, so a field the branch left behind can never read as inherited.
  const inherited = carried.filter(f => !changedSet.has(f))
  const authored = strings(receipt.authored_fields)
  const notCarried = strings(receipt.not_carried_fields)
  if (!authored || !notCarried) {
    return { ...base, attribution: FORK_ATTRIBUTION_LEGACY,
             changed, edited: [], inherited, notCarried: [],
             unattributed: carried.filter(f => changedSet.has(f)) }
  }
  const authoredSet = new Set(authored)
  return {
    ...base,
    attribution: FORK_ATTRIBUTION_STAMPED,
    changed,
    // Intersected with what the node still carries: a `not_carried` field the ENGINE later filled in
    // (`footprint`) is neither the operator's nor the parent's, and belongs in neither list.
    edited: carried.filter(f => authoredSet.has(f)),
    inherited,
    notCarried: notCarried.filter(f => !carried.includes(f)),
    unattributed: carried.filter(f => changedSet.has(f) && !authoredSet.has(f)),
  }
}

/**
 * Who put THIS idea field's value there — the question a heading beside the value is asking.
 *
 * `null` means "no claim is available", and every caller must render exactly nothing for it rather
 * than fall through to a default that names somebody.
 */
export function forkFieldAttribution(prov, field) {
  if (!prov || typeof field !== 'string') return null
  if (prov.edited.includes(field)) return 'edited'
  if (prov.inherited.includes(field)) return 'inherited'
  if (prov.unattributed.includes(field)) return 'unattributed'
  return null
}

// What a heading says next to a value, in the operator's words. `inherited` names the PARENT NODE and
// never "the Researcher": a branch can be taken from another branch, so the node this value came from
// may itself be operator-authored, and this surface cannot tell — nor does it need to.
export function forkFieldNote(prov, field) {
  const who = forkFieldAttribution(prov, field)
  if (who === 'edited') return 'edited by the operator'
  if (who === 'inherited') return `carried over from #${prov.parentId}`
  if (who === 'unattributed') return 'authorship not recorded'
  return ''
}

const list = fields => fields.join(', ')

/**
 * The whole receipt as one sentence, for the node card's tooltip and the Inspector's block.
 *
 * Stops at what the record proves, in the shape `stageSupersessionNotice` established: it states the
 * branch and the vantage point, then makes only the attribution claim its level supports.
 */
export function forkProvenanceSentence(prov) {
  if (!prov) return ''
  const where = prov.observedSeq === null ? '' : `, read at seq ${prov.observedSeq}`
  const from = `Branched by an operator from #${prov.parentId}`
    + (prov.generation === null ? '' : ` generation ${prov.generation}`) + where + '.'
  if (prov.attribution === FORK_ATTRIBUTION_UNRECORDED) {
    return `${from} Which parts of its idea the operator wrote was not recorded.`
  }
  if (prov.attribution === FORK_ATTRIBUTION_LEGACY) {
    return `${from} This receipt predates the authored/inherited split, so it can only say the idea`
      + ` differs from #${prov.parentId} at: ${list(prov.changed) || 'nothing'}.`
      + (prov.inherited.length ? ` Carried over unchanged: ${list(prov.inherited)}.` : '')
  }
  return `${from}`
    + (prov.edited.length ? ` The operator wrote: ${list(prov.edited)}.` : ' The operator changed'
      + ' nothing this branch still carries.')
    + (prov.inherited.length ? ` Carried over unchanged from #${prov.parentId}:`
      + ` ${list(prov.inherited)}.` : '')
    + (prov.notCarried.length ? ` Left behind: ${list(prov.notCarried)}.` : '')
}

// The node card's chip. A SPRITE name rather than a literal glyph, like the deep-research chip's
// `bulb` — a bare character depends on the viewer's font having it, and the whole point of this chip
// is that it is never missed. `user` and not `gitbranch`: every node in the DAG is already a branch,
// so what this chip has to say — and the only thing no other element on the card says — is that a
// HUMAN wrote this experiment's idea rather than the engine.
export const FORK_CHIP_ICON = 'user'

export function forkChip(node) {
  const prov = forkProvenance(node)
  if (!prov) return null
  return {
    icon: FORK_CHIP_ICON,
    label: `Branched by an operator from experiment ${prov.parentId}`,
    title: forkProvenanceSentence(prov),
  }
}
