// The Research view's React half. The pure decisions are driven in `questionLattice.test.js`; what
// is left here is the one rule this half owns (`addedConcepts`) and the fact that the component
// COMPILES — a dropped brace once left `vite build` refusing the tree while the whole suite passed,
// which is why `AssistantBar` gained an SSR load and why this file has one from the start.
import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

import { latticeRows } from '../src/questionLattice.js'

const q = (id, tags) => ({ id, concept_tags: tags, card_kind: 'direction' })

// ONE vite dev server for the whole file, created on first use. Each test spinning up its own cost
// ~30 s of server start, and two of them pushed the file past the harness timeout on a loaded box —
// the module under test is the same in every case, so the server is shared and closed once.
let _server = null
const loadView = async () => {
  if (!_server) {
    _server = await createServer({
      server: { middlewareMode: true }, appType: 'custom', logLevel: 'silent',
    })
  }
  const mod = await _server.ssrLoadModule('/src/ResearchView.jsx')
  return mod
}
after(async () => { if (_server) await _server.close() })


test('ResearchView compiles, and addedConcepts reports only the narrowing concepts', async () => {
  {
    const mod = await loadView()
    assert.equal(typeof mod.default, 'function', 'the component must load')
    const { addedConcepts } = mod
    const rows = latticeRows([q('q1', ['distill']), q('q2', ['distill', 'llm'])])
    const byRowKey = new Map(rows.map(r => [r.rowKey, r]))
    // The root states its whole set; there is nothing above it to inherit from.
    assert.deepEqual(addedConcepts(rows[0], byRowKey), ['distill'])
    // The rung states only `llm` — repeating `distill` down the chain is the mess the ladder exists
    // to avoid.
    assert.deepEqual(addedConcepts(rows[1], byRowKey), ['llm'])
  }
})

test('the ladder RENDERS: nesting, the added concept, the best, and the mixed-comparability mark', async () => {
  // Stronger than "it compiles". The rules are driven in `questionLattice.test.js`; what this adds
  // is that they SURVIVE the render — a number derived correctly and then dropped by the JSX is the
  // failure mode this repo has measured more than once (`knowledge dies in the last inch`).
  {
    const { default: ResearchView } = await loadView()
    const cards = [
      { id: 'q1', card_kind: 'direction', statement: 'distillation raises recall',
        concept_tags: ['distill'], child_card_ids: ['e1'],
        child_rollup: { children: 1, best_delta: 0.02, best_card_id: 'e1' } },
      { id: 'q2', card_kind: 'direction', statement: 'from an LLM it raises it more',
        concept_tags: ['distill', 'llm'], child_card_ids: ['e2'],
        child_rollup: { children: 1, best_delta: 0.05, best_card_id: 'e2' } },
      { id: 'e1', card_kind: 'experiment', parent_card_id: 'q1', best_delta: 0.02,
        evidence: ['n1'], statement: 'teacher logits' },
      { id: 'e2', card_kind: 'experiment', parent_card_id: 'q2', best_delta: 0.05,
        evidence: ['n2'], statement: 'llm rationales' },
    ]
    const key = k => ({ metric_provenance: { comparability: { keys: { measured: k } } } })
    const render = state => renderToStaticMarkup(React.createElement(ResearchView, {
      cards, state, renderCard: card => React.createElement('span', { key: card.id }, card.id),
    }))

    const agreed = render({ nodes: { n1: key('SAME'), n2: key('SAME') } })
    assert.ok(agreed.includes('distillation raises recall'))
    assert.ok(agreed.includes('from an LLM it raises it more'))
    // The rung shows what it ADDS, so `llm` is on the child and `distill` is not repeated there.
    // Counted on the CHIP and not on the bare text: the concept FILTER lists every id as an
    // <option> too, so a naive `>distill<` count finds two and the assertion measures the dropdown.
    const chips = agreed.match(/class="chip chip-concept">([^<]+)</g) || []
    assert.deepEqual(chips.map(c => c.slice(c.indexOf('>') + 1, -1)), ['distill', 'llm'])
    // The broad question wears the best of its SUBTREE (0.05, from the sharper one), and says what
    // its own experiments reached separately.
    assert.ok(agreed.includes('best +0.05'), agreed.slice(0, 400))
    assert.ok(agreed.includes('own +0.02'))
    assert.ok(!agreed.includes('mixed comparability'))
    // Both experiments are drawn, each under its own question.
    assert.ok(agreed.includes('>e1<') && agreed.includes('>e2<'))

    // Provably different keys MARK the number; they do not remove it.
    const mixed = render({ nodes: { n1: key('LEFT'), n2: key('RIGHT') } })
    assert.ok(mixed.includes('mixed comparability'))
    assert.ok(mixed.includes('best +0.05'), 'the number must survive the caveat')
  }
})

test('before the opening memo the view says so, rather than reading as an empty board', async () => {
  {
    const { default: ResearchView } = await loadView()
    const markup = renderToStaticMarkup(React.createElement(ResearchView, {
      cards: [{ id: 'e1', card_kind: 'experiment' }], state: { nodes: {} }, renderCard: () => null,
    }))
    assert.ok(markup.includes('no research question registered yet'))
  }
})

test('a closed question is DIMMED in place, and a closure on nothing narrower says so', async () => {
  const { default: ResearchView } = await loadView()
  const render = cards => renderToStaticMarkup(React.createElement(ResearchView, {
    cards, state: { nodes: { n1: { metric: 1 } } }, renderCard: () => null,
  }))

  const alone = render([{ id: 'q1', card_kind: 'direction', statement: 'distillation helps',
    concept_tags: ['distill'], status: 'dropped' }])
  // Still on the ladder — the chain that explains its neighbours must stay readable.
  assert.ok(alone.includes('distillation helps'))
  assert.ok(alone.includes('research-closed'))
  assert.ok(alone.includes('research-closed-unsupported'))
  assert.ok(alone.includes('nothing narrower'))

  const narrowed = render([
    { id: 'q1', card_kind: 'direction', statement: 'distillation helps',
      concept_tags: ['distill'], status: 'dropped' },
    { id: 'q2', card_kind: 'direction', statement: 'from an LLM it helps more',
      concept_tags: ['distill', 'llm'] },
  ])
  assert.ok(narrowed.includes('research-closed'), 'still dimmed — it really is closed')
  assert.ok(!narrowed.includes('research-closed-unsupported'),
    'a sharper question below it is exactly what makes the discard supported')
  assert.ok(!narrowed.includes('nothing narrower'))
})
