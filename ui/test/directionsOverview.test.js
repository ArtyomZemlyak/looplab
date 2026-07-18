import assert from 'node:assert/strict'
import test from 'node:test'

import { directionProfit, optimizationLabel, toMarkdown } from '../src/report.js'
import { GROUP_MODES, computeGroups } from '../src/grouping.js'

const node = (id, metric, direction, parentIds = []) => ({
  id, metric, parent_ids: parentIds, feasible: true, status: 'evaluated', operator: 'improve',
  idea: { theme: direction, params: {} },
})
const conceptNode = (id, metric, concept, parentIds = []) => ({
  id, metric, parent_ids: parentIds, feasible: true, status: 'evaluated', operator: 'improve',
  idea: { theme: null, concepts: [concept], params: {} },
})

test('direction controls keep first-discovery order while live performance changes', () => {
  const state = {
    direction: 'min',
    // Deliberately use non-numeric object keys: node id, not insertion/key order, defines discovery.
    nodes: {
      late: node(2, 1, 'late winner', [1]),
      first: node(1, 10, 'first explored'),
      revisit: node(3, 9, 'first explored', [1]),
    },
  }
  const before = directionProfit(state)
  assert.deepEqual(before.map(row => row.direction), ['first explored', 'late winner'])
  assert.ok(before[1].gain > before[0].gain, 'the stronger result must not silently move its control')

  const after = directionProfit({
    ...state,
    nodes: { ...state.nodes, late: { ...state.nodes.late, metric: 20 } },
  })
  assert.deepEqual(after.map(row => row.direction), ['first explored', 'late winner'])
})

test('direction projection safely accepts agent-authored property-like names', () => {
  const rows = directionProfit({ direction: 'max', nodes: {
    a: node(1, 1, '__proto__'), b: node(2, 2, 'constructor', [1]),
  } })
  assert.deepEqual(rows.map(row => row.direction), ['__proto__', 'constructor'])
  assert.deepEqual(rows.map(row => row.count), [1, 1])
})

test('concept-authored nodes keep directions and theme grouping alive without legacy themes', () => {
  const nodes = {
    first: conceptNode(1, 10, 'loss/contrastive'),
    second: conceptNode(2, 8, 'architecture/moe', [1]),
    revisit: conceptNode(3, 7, 'loss/distillation', [2]),
  }
  assert.deepEqual(directionProfit({ direction: 'min', nodes }).map(row => row.direction),
    ['loss', 'architecture'])
  assert.deepEqual([...computeGroups(nodes, 'theme')], [
    ['loss', [1, 3]], ['architecture', [2]],
  ])
})

test('optimization and grouping labels use user-facing language without changing wire values', () => {
  assert.equal(optimizationLabel('min'), 'minimize')
  assert.equal(optimizationLabel('max'), 'maximize')
  assert.equal(optimizationLabel('legacy'), 'unknown')
  assert.deepEqual(GROUP_MODES[0], ['theme', 'primary concept axis'])
  assert.match(toMarkdown({ direction: 'max', nodes: {}, task_id: 't', run_id: 'r' }), /\*\*Optimization orientation:\*\* maximize/)
})
