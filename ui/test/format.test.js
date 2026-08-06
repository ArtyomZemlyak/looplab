import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import { costPricing, fmtElapsedSeconds } from '../src/format.js'

test('elapsed seconds never rounds positive sub-second work down to zero', () => {
  assert.equal(fmtElapsedSeconds(0), '0s')
  assert.equal(fmtElapsedSeconds(Number.MIN_VALUE), '<1s')
  assert.equal(fmtElapsedSeconds(0.49), '<1s')
  assert.equal(fmtElapsedSeconds(0.999), '<1s')
  assert.equal(fmtElapsedSeconds(1), '1s')
  assert.equal(fmtElapsedSeconds(1.49), '1s')
  assert.equal(fmtElapsedSeconds(1.5), '2s')
  assert.equal(fmtElapsedSeconds(123.6), '124s')
})

test('elapsed seconds rejects invalid or negative measurements', () => {
  for (const value of [null, undefined, '0.5', NaN, Infinity, -Infinity, -0.1]) {
    assert.equal(fmtElapsedSeconds(value), '—')
  }
})

test('run summary surfaces share the elapsed formatter and retain a zero budget', async () => {
  const [runView, panels] = await Promise.all([
    readFile(new URL('../src/RunView.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/panels.jsx', import.meta.url), 'utf8'),
  ])

  for (const source of [runView, panels]) {
    assert.match(source, /fmtElapsedSeconds\(evalSec\)/)
    assert.match(source, /maxEval != null/)
    assert.match(source, /fmtElapsedSeconds\(maxEval\)/)
    assert.doesNotMatch(source, /Math\.round\(evalSec\)/)
  }
})

// The absent-is-not-zero rule this function exists for, applied to its OWN inputs. Every case below
// is a payload a first-party writer really produces: `narration.js` renders RAW `llm_cost` events
// (its `validate` requires only `total_tokens` and `cost`), and `routers/runs.py::run_cost` /
// `routers/reviews.py::_review_cost` return zero-filled defaults stamped `recorded: false`.
test('cost pricing never reads an absent measurement as a measured zero', () => {
  // Not measured at all: the two routes that zero-fill say so, and the flag is what settles it —
  // the zero-filled body is otherwise indistinguishable from a real "$0, nothing was spent" run.
  assert.equal(costPricing({ cost: 0, calls: 0, total_tokens: 0, recorded: false }).text, '—')
  assert.match(costPricing({ cost: 0, calls: 0, recorded: false }).title, /No LLM cost roll-up/)

  // A stated zero IS a measurement, and still reads as one.
  const free = costPricing({ cost: 0, calls: 0, recorded: true })
  assert.equal(free.text, '$0')
  assert.match(free.title, /nothing was spent/)

  // Money with no call count: show the money, do not claim the count. This is the case that used to
  // print "$0 — No model calls were made, so nothing was spent" over real spend.
  const noCalls = costPricing({ cost: 8.26, total_tokens: 1000 })
  assert.equal(noCalls.text, '$8.26')
  assert.doesNotMatch(noCalls.title, /nothing was spent/)
  assert.match(noCalls.title, /does not record how many calls/)

  // The already-covered halves stay covered: no cost at all, and a stated priced/unpriced split.
  assert.equal(costPricing({ calls: 12 }).text, '—')
  assert.equal(costPricing({ cost: 1.96, calls: 156, priced_calls: 156 }).text, '$1.96')
  assert.equal(costPricing({ cost: 8.26, calls: 313, priced_calls: 209 }).text, '$8.26+')
  assert.equal(costPricing({ cost: 0, calls: 354, priced_calls: 0 }).text, 'unpriced')
})
