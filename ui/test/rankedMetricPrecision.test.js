// A column that RANKS prints enough digits to justify the order it shows.
//
// `fmt`'s four significant figures are right for a number an operator READS and wrong for one an
// operator COMPARES, and the cross-run table does both at once: it ranks at full precision and
// renders through `fmt`.
//
// MEASURED on the shipped corpus — the two best numbers on this box are 0.793426 and 0.793411. Both
// print `0.7934`, under ranks 1 and 2, with no tie marker, while the claim sentence beside the same
// table prints the unrounded value. An operator sees two identical strings ordered, which reads as
// an arbitrary ordering of equals.
//
// The walk itself is not new: it lived inside `RunCompare.jsx`, where one screen had solved this
// privately. `format.js::distinctMetricFormatter` is that code hoisted, so every ranking surface can
// ask for it — this file pins the behaviour that move must not change.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { distinctMetricFormatter, fmt } from '../src/format.js'

// The corpus's two best, and the third value keeps the column realistic.
const BEST = 0.793426
const SECOND = 0.793411

test('the incident: the two best numbers no longer render identically', () => {
  // MUTATION: return `fmt` at a fixed 4 -> both print 0.7934 and the ranking looks like a coin toss.
  assert.equal(fmt(BEST), fmt(SECOND), 'the premise: plain fmt cannot separate them')

  const format = distinctMetricFormatter([BEST, SECOND, 0.5])

  assert.notEqual(format(BEST), format(SECOND))
  assert.ok(format(BEST).startsWith('0.7934'), format(BEST))
})

test('values that are genuinely equal never widen the column', () => {
  // A true tie is a fact, and printing more digits of it says nothing. MUTATION: widen until every
  // VALUE is distinct rather than every distinct value -> a column of ties runs to 17 digits.
  const format = distinctMetricFormatter([0.5, 0.5, 0.25])

  assert.deepEqual([0.5, 0.5, 0.25].map(format), ['0.5', '0.5', '0.25'])
})

test('an ordinary column keeps the short rendering it always had', () => {
  // The regression this could most easily cause: making every table wider for no reason.
  const values = [0.91, 0.72, 0.5]
  const format = distinctMetricFormatter(values)

  assert.deepEqual(values.map(format), values.map(v => fmt(v)))
})

test('non-numbers render as the same em dash the rest of the UI uses', () => {
  const format = distinctMetricFormatter([0.1, 0.2])

  for (const value of [null, undefined, NaN, Infinity, 'x']) {
    assert.equal(format(value), '—')
  }
})

test('an empty or junk column is safe to format', () => {
  assert.equal(distinctMetricFormatter([])(0.5), '0.5')
  assert.equal(distinctMetricFormatter(null)(0.5), '0.5')
  assert.equal(distinctMetricFormatter(['a', null, NaN])(0.5), '0.5')
})

test('values indistinguishable at 17 digits fall back to the raw number', () => {
  // Past 17 the extra characters are the binary representation talking, so the walk stops and says
  // what it has. MUTATION: loop forever -> `toPrecision` throws past 100 and the table blanks.
  const near = [1, 1 + Number.EPSILON / 2]
  const format = distinctMetricFormatter(near)

  for (const value of near) assert.equal(typeof format(value), 'string')
})

test('the widening is per COLUMN, which is the information a per-value formatter lacks', () => {
  // The same number renders differently depending on what it is being compared against, and that is
  // correct: how many digits are honest is a property of the comparison, not of the value.
  const alone = distinctMetricFormatter([BEST, 0.5])
  const beside = distinctMetricFormatter([BEST, SECOND, 0.5])

  assert.equal(alone(BEST), '0.7934')
  assert.notEqual(beside(BEST), alone(BEST))
})
