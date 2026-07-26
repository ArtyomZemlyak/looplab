import assert from 'node:assert/strict'
import test from 'node:test'

import { dagCollapsedKey, dagLayoutProjection } from '../src/dagProjection.js'

const state = () => ({
  nodes: {
    1: {
      id: 1, parent_ids: [], status: 'evaluated', metric: 0.2, operator: 'draft',
      idea: { theme: 'search', params: { lr: 1.1, width: 1 } },
    },
    2: {
      id: 2, parent_ids: [1], status: 'pending', operator: 'improve',
      idea: { theme: 'search', params: { lr: 1.4, width: 2 } },
    },
  },
  aborted_nodes: [],
  direction: 'max',
  engine_running: true,
})

test('DAG layout key ignores volatile frames while retaining the latest render projection', () => {
  const before = state()
  const first = dagLayoutProjection(before, 'theme')
  const after = {
    ...before,
    engine_running: false,
    report: { markdown: 'new report' },
    logs: ['new log'],
    nodes: {
      ...before.nodes,
      2: {
        ...before.nodes[2], status: 'evaluated', metric: 0.9,
        agent_report: { ok: true }, trials_summary: { count: 3 },
      },
    },
  }
  const next = dagLayoutProjection(after, 'theme')

  assert.equal(next.key, first.key)
  assert.notEqual(next.nodes, first.nodes)
  assert.equal(next.nodes[2].metric, 0.9,
    'card rendering still receives the latest volatile node snapshot')
})

test('DAG layout key changes for every geometry input', () => {
  const before = state()
  const key = dagLayoutProjection(before, 'theme').key

  assert.notEqual(dagLayoutProjection({
    ...before,
    nodes: { ...before.nodes, 2: { ...before.nodes[2], parent_ids: [] } },
  }, 'theme').key, key, 'parent topology changes geometry')
  assert.notEqual(dagLayoutProjection({ ...before, aborted_nodes: [2] }, 'theme').key, key,
    'active membership changes geometry')
  assert.notEqual(dagLayoutProjection({
    ...before,
    nodes: { ...before.nodes, 2: {
      ...before.nodes[2], idea: { ...before.nodes[2].idea, theme: 'memory' },
    } },
  }, 'theme').key, key, 'group membership changes geometry')

  const metricKey = dagLayoutProjection(before, 'metric').key
  assert.notEqual(dagLayoutProjection({
    ...before,
    nodes: { ...before.nodes, 2: { ...before.nodes[2], metric: 99 } },
  }, 'metric').key, metricKey, 'metric grouping remains layout-authoritative')

  const nicheKey = dagLayoutProjection(before, 'niche').key
  assert.notEqual(dagLayoutProjection({
    ...before,
    nodes: { ...before.nodes, 1: {
      ...before.nodes[1],
      idea: { ...before.nodes[1].idea, params: { lr: 1.49, width: 1 } },
    } },
  }, 'niche').key, nicheKey,
  'raw niche rank input is retained even when its rounded group label is unchanged')
})

test('collapsed layout key is order-stable and detects in-place Set changes', () => {
  const collapsed = new Set(['search', 'memory'])
  const key = dagCollapsedKey(collapsed)
  assert.equal(key, dagCollapsedKey(new Set(['memory', 'search'])))
  collapsed.add('optimizer')
  assert.notEqual(dagCollapsedKey(collapsed), key)
})
