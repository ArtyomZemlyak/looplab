// Fork-to-branch, READ side. `forkFromSeqModel.test.js` owns what a branch SENDS; this owns what a
// later reader is TOLD about one, which is a different failure mode and a worse one: a wrong answer
// here is a confident, durable claim about who authored an experiment.
//
// The fixtures are the real wire shapes. `/state` serves `RunState.model_dump(mode="json")`, so a
// node's idea always holds every `Idea` field (empty ones included) and its receipt is whatever
// `serve/control_validation.py::_normalize_fork_receipt` stamped at the time — which is not the same
// set of keys for every log on disk, and that is the whole point of the attribution ladder.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  FORK_ATTRIBUTION_LEGACY, FORK_ATTRIBUTION_STAMPED, FORK_ATTRIBUTION_UNRECORDED,
  forkChip, forkFieldAttribution, forkFieldNote, forkIdeaFieldCarried, forkProvenance,
  forkProvenanceSentence,
} from '../src/forkProvenance.js'

// An idea as the fold dumps it: every field present, most of them empty.
const idea = (over = {}) => ({
  operator: 'improve', params: { x: 0.125 }, rationale: '', eval_profile: null, eval_timeout: null,
  theme: null, concepts: [], concept_mode: null, concepts_added: [], concepts_removed: [],
  space: {}, hypothesis: null, card_id: null, footprint: null, ...over,
})

// The node a branch produces against a Researcher-built parent: two edits, and the parent's engine
// bookkeeping deliberately left behind.
const branched = (over = {}) => ({
  id: 9,
  idea: idea({ params: { x: 0.125 }, rationale: 'operator: halve the step' }),
  forked_from: {
    node_id: 3, generation: 1, observed_seq: 412, base_idea_digest: 'idea:v1:abcd',
    changed_fields: ['card_id', 'footprint', 'hypothesis', 'params', 'rationale', 'theme'],
    authored_fields: ['params', 'rationale'],
    not_carried_fields: ['card_id', 'footprint', 'hypothesis', 'theme'],
  },
  ...over,
})

// ------------------------------------------------------------------ the claim, at each level

test('a branch says what the operator wrote and what the parent gave it, separately', () => {
  const prov = forkProvenance(branched())
  assert.equal(prov.attribution, FORK_ATTRIBUTION_STAMPED)
  assert.deepEqual([prov.parentId, prov.generation, prov.observedSeq], [3, 1, 412])
  assert.deepEqual(prov.edited, ['params', 'rationale'])
  // `operator` is the one field this branch carries that the receipt does NOT list as different from
  // its base, so it is the base's own value — inherited, and the only thing that may read that way.
  assert.deepEqual(prov.inherited, ['operator'])
  assert.deepEqual(prov.notCarried, ['card_id', 'footprint', 'hypothesis', 'theme'])
  // The raw diff is six fields for two edits. Rendering THAT as "what the operator changed" is the
  // misreading the split exists to prevent, so it stays available and is never the answer.
  assert.equal(prov.changed.length, 6)
  assert.deepEqual(prov.edited, prov.changed.filter(f => prov.edited.includes(f)))
})

test('inherited is the complement of the DIFF, never of the operator’s edits', () => {
  // The trap: `changed - authored` is `not_carried`, and a reader computing "everything else came
  // from the parent" that way would present four fields the branch dropped as the parent's
  // contribution to it. Only the complement of `changed_fields` is sound.
  const prov = forkProvenance(branched())
  for (const dropped of ['card_id', 'footprint', 'hypothesis', 'theme']) {
    assert.equal(prov.inherited.includes(dropped), false, `${dropped} was left behind, not inherited`)
    assert.equal(forkFieldAttribution(prov, dropped), null)
  }
})

test('a field the ENGINE filled in after intake is neither the operator’s nor the parent’s', () => {
  // `_finalize_developer_footprint` mints a `footprint` the submission had none of, so the node's
  // idea drifts away from the one the receipt compared. The receipt says `footprint` was not carried
  // over; the node visibly HAS one. Claiming either author would be wrong, so nothing is claimed.
  const prov = forkProvenance(branched({
    idea: idea({ rationale: 'operator: halve the step', footprint: { gpus: 1 } }) }))
  assert.equal(forkFieldAttribution(prov, 'footprint'), 'unattributed')
  assert.equal(prov.inherited.includes('footprint'), false)
  assert.equal(prov.notCarried.includes('footprint'), false)
  assert.match(forkFieldNote(prov, 'footprint'), /not recorded/)
})

test('a receipt written before the split degrades to legacy, not to "changed nothing"', () => {
  const { authored_fields, not_carried_fields, ...old } = branched().forked_from
  const prov = forkProvenance(branched({ forked_from: old }))
  assert.equal(prov.attribution, FORK_ATTRIBUTION_LEGACY)
  assert.deepEqual(prov.edited, [], 'the split is not re-derivable here and must not be guessed')
  // ...but the sound half survives: the complement of the diff is still the base's own value.
  assert.deepEqual(prov.inherited, ['operator'])
  assert.equal(forkFieldAttribution(prov, 'rationale'), 'unattributed')
  assert.match(forkProvenanceSentence(prov), /predates the authored\/inherited split/)
})

test('an unreadable receipt proves the branch and nothing else', () => {
  const prov = forkProvenance(branched({
    forked_from: { node_id: 3, generation: 1, observed_seq: 412 } }))
  assert.equal(prov.attribution, FORK_ATTRIBUTION_UNRECORDED)
  assert.deepEqual([prov.edited, prov.inherited, prov.notCarried], [[], [], []])
  // Every field the node carries falls to "no claim". Silence is the only honest answer, and it is
  // not the same as saying the operator changed nothing — that would read as the Researcher's.
  assert.equal(forkFieldAttribution(prov, 'rationale'), 'unattributed')
  assert.match(forkProvenanceSentence(prov), /was not recorded/)
  // A malformed list is unreadable too, not partially trusted.
  assert.equal(forkProvenance(branched({
    forked_from: { ...branched().forked_from, changed_fields: ['params', 7] },
  })).attribution, FORK_ATTRIBUTION_UNRECORDED)
})

test('a node nobody branched gains no label anywhere', () => {
  // The default path, and the one that must stay silent: `null` means make NO claim, and every
  // caller renders exactly nothing for it rather than falling through to a default that names
  // somebody. An ordinary Researcher-proposed node's Overview is byte-identical to what it was.
  for (const node of [null, undefined, {}, { idea: idea() },
    { idea: idea(), forked_from: null }, { idea: idea(), forked_from: [] },
    { idea: idea(), forked_from: { generation: 1 } }]) {
    assert.equal(forkProvenance(node), null)
  }
  assert.equal(forkFieldAttribution(null, 'rationale'), null)
  assert.equal(forkFieldNote(null, 'rationale'), '')
  assert.equal(forkProvenanceSentence(null), '')
  assert.equal(forkChip({ idea: idea() }), null)
})

// ------------------------------------------------------------------ what it says out loud

test('an inherited field is attributed to the PARENT NODE, never to the Researcher', () => {
  // A branch can be taken from another branch, so the node a value came from may itself be
  // operator-authored. This surface cannot tell and must not guess — naming the node is both the
  // honest statement and the useful one, since it is where a reader goes next.
  const prov = forkProvenance(branched())
  assert.equal(forkFieldNote(prov, 'operator'), 'carried over from #3')
  assert.equal(forkFieldNote(prov, 'rationale'), 'edited by the operator')
  const sentence = forkProvenanceSentence(prov)
  assert.match(sentence, /Branched by an operator from #3 generation 1, read at seq 412\./)
  assert.match(sentence, /The operator wrote: params, rationale\./)
  assert.match(sentence, /Carried over unchanged from #3: operator\./)
  assert.doesNotMatch(sentence, /Researcher|proposed/i)
})

test('the emptiness rule matches the one that stamped the receipt', () => {
  // Mirrors `looplab/core/cards.py::idea_field_carried`. If the two disagree, a field the SERVER
  // called "not carried" reads here as inherited — the record and its reader contradicting each
  // other about the same branch.
  for (const empty of [undefined, null, '', [], {}]) {
    assert.equal(forkIdeaFieldCarried(empty), false)
  }
  for (const carried of [0, 0.0, false, 'x', [0], { a: 1 }]) {
    assert.equal(forkIdeaFieldCarried(carried), true, `${JSON.stringify(carried)} is a value`)
  }
})

// ------------------------------------------------------------------ the two surfaces that read it

test('the node card and the Inspector both read the model rather than the receipt', async () => {
  const chip = forkChip(branched())
  // A sprite name, never a bare character: the chip that says a human wrote this must not depend on
  // the viewer's font, and `looplab-icons-v1.svg` has to actually contain it.
  assert.equal(chip.icon, 'user')
  const sprite = await readFile(new URL('../src/looplab-icons-v1.svg', import.meta.url), 'utf8')
  assert.ok(sprite.includes(`id="${chip.icon}"`), 'the chip icon must exist in the sprite')
  assert.match(chip.label, /Branched by an operator from experiment 3/)
  assert.equal(chip.title, forkProvenanceSentence(forkProvenance(branched())))

  // Neither surface may reach into `forked_from` itself: the ladder above is the whole rule, and a
  // component that read `changed_fields` directly would print the six-field diff as the operator's
  // work. Before this module existed NOTHING in the browser read the receipt at all — the DAG drew
  // a chip for a cross-run origin and one for deep research, and none for an operator branch.
  for (const file of ['Dag.jsx', 'Inspector.jsx']) {
    const source = await readFile(new URL(`../src/${file}`, import.meta.url), 'utf8')
    assert.match(source, /from '\.\/forkProvenance\.js'/, `${file} must ask the model`)
    assert.doesNotMatch(source, /forked_from/, `${file} must not re-derive attribution`)
  }
  const inspector = await readFile(new URL('../src/Inspector.jsx', import.meta.url), 'utf8')
  // The headings over the idea are where the misreading actually happened: an inherited rationale
  // under a bare "Rationale" reads as this experiment's own justification.
  assert.match(inspector, /Idea params\{ideaNote\('params'\)\}/)
  assert.match(inspector, /Rationale\{ideaNote\('rationale'\)\}/)
})
