// doc 75 UX-14: the run's first screen says what the search is doing in words that need no glossary.
//
// The strip used to show the strategist's and the policy's own sentences — "race candidates with
// ASHA (smoke rung -> promote survivors to full)", "endgame (inside the plan's reserve, …): … a
// champion sweep", "exploit best (rungs collapsed) -> #3". Those stay, whole, in each entry's title
// and disclosure (they are the decision's record); what is SHOWN comes from the structured fields.
import test from 'node:test'
import assert from 'node:assert/strict'
import { JSDOM } from 'jsdom'

import { mountHarness } from './_mount.js'
import { planStanding, policyPlainLine, strategyPlainLine } from '../src/whyStripModel.js'

const ENGINE_WORDS = /ASHA|rung|endgame|reserve|sweep/i
let harness
let WhyStrip
let ConceptChipBar

test.before(async () => {
  harness = await mountHarness({ routes: {} })
  ;({ default: WhyStrip } = await harness.load('/src/WhyStrip.jsx'))
  ;({ default: ConceptChipBar } = await harness.load('/src/ConceptChipBar.jsx'))
})
test.after(async () => { await harness?.close() })

const shown = markup => {
  const dom = new JSDOM(markup)
  for (const hidden of dom.window.document.querySelectorAll('[hidden]')) hidden.remove()
  return dom.window.document.body.textContent
}

for (const [name, strategy] of [
  ['exploring', { policy: 'asha', fidelity: 'adaptive',
    rationale: 'exploring breadth: race candidates with ASHA (smoke rung -> promote survivors to full)' }],
  ['endgame', { policy: 'greedy', fidelity: 'full',
    operators: { merge_mode: 'ensemble', ablate_every: 0, endgame_sweep: true },
    rationale: "endgame (inside the plan's reserve, 100% of node budget spent): reserve for a final "
      + 'ensemble of the top solutions and a champion sweep, no new breadth' }],
]) {
  test(`the ${name} strategy is shown in plain words, its rationale kept whole`, () => {
    const state = { strategy_history: [{ strategy, at_node: 4 }],
      policy_reason: 'exploit best (rungs collapsed)', policy_chosen: 3 }
    const markup = harness.render(WhyStrip, { state })
    assert.doesNotMatch(shown(markup), ENGINE_WORDS, shown(markup))
    assert.match(shown(markup), /building on experiment #3/)
    const titles = [...new JSDOM(markup).window.document.querySelectorAll('.why-item')].map(el => el.title)
    assert.ok(titles.includes(strategy.rationale), 'the rationale is still the entry\'s title')
    assert.ok(new JSDOM(markup).window.document.querySelector('.why-detail').textContent
      .includes(strategy.rationale), 'and its disclosure')
  })
}

test('the plain line follows the structured fields, never the prose', () => {
  assert.equal(strategyPlainLine({ policy: 'greedy', rationale: 'race candidates with ASHA' }),
    'Refining the best result so far')
  assert.equal(strategyPlainLine({ policy: 'asha' }), 'Trying several directions quickly, then refining the best')
  assert.equal(strategyPlainLine(null), '')
  assert.deepEqual(policyPlainLine(null), ['choosing the next experiment', []])
})

test('a finished run\'s strip names no next step and says the budget is spent', () => {
  const strategy = { policy: 'greedy', operators: { merge_mode: 'ensemble', endgame_sweep: true },
    rationale: 'endgame: reserve for a final ensemble' }
  const nodes = Object.fromEntries([0, 1, 2, 3, 4, 5].map(id => [id, { id }]))
  const plan = { max_nodes: 6, endgame_start: 4 }
  const live = { strategy_history: [{ strategy, at_node: 5 }], plan,
    nodes: Object.fromEntries([0, 1, 2, 3, 4].map(id => [id, { id }])),
    policy_reason: 'exploit best', policy_chosen: 3 }
  assert.match(shown(harness.render(WhyStrip, { state: live })), /Budget nearly spent.*building on experiment #3/s)
  const text = shown(harness.render(WhyStrip, { state: { ...live, nodes, finished: true } }))
  assert.match(text, /Budget spent: combined the best results/)
  assert.doesNotMatch(text, /next|building on|nearly/, text)
  assert.equal(strategyPlainLine({ policy: 'greedy' }, { finished: true, budgetSpent: true }),
    'Refining the best result so far', 'only the endgame line has a tense to change')
})

test('the budget lines follow the plan, not the endgame switch (code review)', () => {
  // The Strategist may turn `endgame_sweep` on at any consult; a run can finish for reasons other
  // than its budget. The strip says "Budget …" only where the plan says it is true.
  const sweep = { policy: 'greedy', operators: { endgame_sweep: true } }
  const plan = { max_nodes: 20, endgame_start: 16 }
  const at = n => ({ plan, nodes: Object.fromEntries(Array.from({ length: n }, (_, id) => [id, { id }])) })
  assert.deepEqual(planStanding(at(2)), { inEndgame: false, budgetSpent: false })
  assert.equal(strategyPlainLine(sweep, { ...planStanding(at(2)) }), 'Refining the best result so far',
    'node 2 of 20 is not "budget nearly spent"')
  assert.equal(strategyPlainLine(sweep, { ...planStanding(at(17)) }),
    'Budget nearly spent: combining the best results')
  assert.equal(strategyPlainLine(sweep, { ...planStanding(at(9)), finished: true }),
    'Refining the best result so far', 'stopped at 9 of 20: no "Budget spent"')
  assert.equal(strategyPlainLine(sweep, { ...planStanding(at(20)), finished: true }),
    'Budget spent: combined the best results')
  assert.deepEqual(planStanding({ nodes: {} }), { inEndgame: false, budgetSpent: false }, 'no plan row')
})

test('the all-withheld Concepts badge no longer says "Membership withheld … not empty"', async () => {
  // A NEGATIVE pin, on purpose a substring: what must not come back is the text (CLAUDE.md).
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../src/ConceptChipBar.jsx', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /Membership withheld for all/)
  assert.match(source, /the run is tagged/)
})
