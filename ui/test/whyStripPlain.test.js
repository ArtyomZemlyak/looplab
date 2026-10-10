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
import { policyPlainLine, strategyPlainLine } from '../src/whyStripModel.js'

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

test('the all-withheld Concepts badge no longer says "Membership withheld … not empty"', async () => {
  // A NEGATIVE pin, on purpose a substring: what must not come back is the text (CLAUDE.md).
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../src/ConceptChipBar.jsx', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /Membership withheld for all/)
  assert.match(source, /the run is tagged/)
})
