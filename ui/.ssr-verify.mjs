// Verify, without vite/jsdom, every assertion the vite-based conceptChipBar test now makes.
// node 20.8.1 cannot load vite (rolldown needs node:util styleText, 20.12+) nor jsdom (require of ESM),
// so the repo's 30 vite tests cannot execute on this box; this re-checks the same properties directly.
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const HARNESS = process.argv[2]
const { default: ConceptChipBar } = await import(`${HARNESS}/ConceptChipBar.mjs`)
const { matchingNodeIds } = await import(`${HARNESS}/conceptChips.mjs`)
const { authoritativeNodeConcepts } = await import(`${HARNESS}/nodeProjection.mjs`)

const STATE = {
  nodes: Object.fromEntries(Array.from({ length: 5 }, (_, id) => [id, { id }])),
  node_concepts: {
    0: ['loss/contrastive/dcl', 'architecture/moe'],
    1: ['loss/contrastive/mnr'],
    2: ['loss/mnr'],
    3: ['data/synth'],
    4: ['loss'],
  },
}
const render = state => renderToStaticMarkup(React.createElement(ConceptChipBar, {
  state, onHighlight() {},
}))
const names = html => [...html.matchAll(/class="cb-name">([^<]*)</g)].map(m => m[1])
const role = html => (html.match(/class="concept-bar" role="([^"]+)"/) || [])[1]
const text = html => html.replace(/<[^>]*>/g, ' ').replace(/&#x27;/g, "'").replace(/\s+/g, ' ')

// --- degraded row is withheld; the rest of the run stays filterable -----------------------------
for (const receipt of [{ status: 'partial', reasons: ['concepts_per_node_cap'] },
  { status: 'unavailable', reasons: ['delta_dependency_unknown_parent_membership'] }]) {
  const html = render({ ...STATE, node_concept_materialization_receipts: { 0: receipt } })
  assert.equal(role(html), 'group')
  assert.deepEqual(names(html), ['data', 'loss'], 'the four exact rows still drive chips')
  assert.ok(!names(html).includes('architecture'), 'the withheld row reaches no chip/count/filter')
  assert.match(text(html), /PARTIAL.*1 withheld/s)
  assert.ok(/aria-label="Search concepts"/.test(html), 'search stays reachable')
}

// --- every row withheld: visible refusal, never silence -----------------------------------------
const allWithheld = render({
  nodes: { 0: { id: 0 } }, node_concepts: { 0: [] },
  node_concept_materialization_receipts: {
    0: { status: 'unavailable', reasons: ['delta_dependency_cycle'] },
  },
})
assert.equal(role(allWithheld), 'status')
assert.match(text(allWithheld), /withheld for all 1 tagged experiment.*not empty/s)
assert.ok(!/class="cb-chip/.test(allWithheld))

// --- run-scoped refusal unchanged ----------------------------------------------------------------
const unavailable = render({ nodes: {}, node_concepts: {}, run_base_concept_receipt: {
  status: 'unavailable', reasons: ['delta_dependency_cycle'],
} })
assert.equal(role(unavailable), 'alert')
assert.match(text(unavailable), /UNAVAILABLE.*not empty/s)
assert.ok(!/class="cb-chip/.test(unavailable))

// --- an untagged run still renders nothing at all ------------------------------------------------
assert.equal(render({ node_concepts: {} }), '')

// --- the live-tick highlight the interaction test asserts -----------------------------------------
const degradedState = { ...STATE, node_concept_materialization_receipts: {
  0: { status: 'partial', reasons: ['concepts_per_node_cap'] },
} }
const { concepts } = authoritativeNodeConcepts(degradedState)
const highlight = matchingNodeIds(concepts, ['loss/contrastive'], {})
assert.deepEqual([...highlight].sort((a, b) => a - b), [1],
  'the withheld node leaves the highlight; its membership can no longer be claimed')
const degradedHtml = render(degradedState)
assert.match(text(degradedHtml), /PARTIAL.*1 withheld/s)
assert.ok(/class="cb-chip/.test(degradedHtml))

// --- the unchanged baseline: complete state, chip counts unchanged --------------------------------
const complete = render(STATE)
assert.deepEqual(names(complete), ['architecture', 'data', 'loss'])
assert.equal(role(complete), 'group')
assert.ok(!/withheld/.test(text(complete)), 'a clean run gains no disclosure noise')

console.log('OK — every assertion in the (unrunnable-here) vite component test verified directly')
