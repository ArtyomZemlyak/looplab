// UI review 2026-10-14 section 7.7: experiments × parameters × metrics for any number of experiments.
import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'

import { compareColumns, compareCsv, compareRows, sortRows } from '../src/compareTableModel.js'
import { mountLive, until } from './_mount.js'

const state = {
  run_id: 'r', direction: 'max',
  nodes: {
    0: { id: 0, attempt: 0, status: 'evaluated', metric: 0.31, idea: { params: { lr: 0.001, epochs: 1 } }, extra_metrics: { recall: 0.2, ndcg: 0.1 } },
    1: { id: 1, attempt: 0, status: 'evaluated', metric: 0.35, idea: { params: { lr: 0.002, epochs: 1 } }, extra_metrics: { recall: 0.25 } },
    2: { id: 2, attempt: 1, status: 'evaluated', metric: 0.33, idea: { params: { lr: 0.002, epochs: 1, warmup: 100 } }, extra_metrics: { recall: 0.22, ndcg: 0.12 } },
    3: { id: 3, attempt: 0, status: 'failed', idea: { params: { lr: 0.01, nested: { a: 1 } } } },
  },
}
const configs = new Map([['2:1', { effective: { world_size: 8, max_steps: 900 } }], ['0:0', { effective: { world_size: 8 } }]])

test('rows keep absence as absence and the asked/ran split', () => {
  const rows = compareRows(state, { effectiveConfigs: configs })
  assert.deepEqual(rows.map(row => row.id), [0, 1, 2, 3])
  assert.equal(rows[3].metric, undefined, 'a failed experiment has no metric, not 0')
  assert.equal(rows[3].params.nested, undefined, 'a nested value is not a table cell')
  assert.deepEqual(rows[2].config, { world_size: 8, max_steps: 900 })
  assert.deepEqual(rows[1].config, {}, 'no recorded config: empty, not inherited from a neighbour')
})

test('only-differing hides a column only when every selected row agrees, absence included', () => {
  const rows = compareRows(state, { nodeIds: [0, 1, 2], effectiveConfigs: configs })
  const names = cols => cols.columns.map(col => `${col.group}:${col.name}`)
  assert.deepEqual(names(compareColumns(rows, { onlyDiffering: true })), [
    'params:lr', 'params:warmup', 'config:max_steps', 'config:world_size', 'metrics:ndcg', 'metrics:recall'])
  assert.ok(names(compareColumns(rows)).includes('params:epochs'), 'shown when not filtering')
  assert.deepEqual(names(compareColumns(rows, { metricFilter: 'rec' })).filter(n => n.startsWith('metrics')), ['metrics:recall'])
})

test('sorting puts absent values last both ways, and the CSV keeps empty cells empty', () => {
  const rows = compareRows(state)
  assert.deepEqual(sortRows(rows, { group: 'metric' }, true).map(row => row.id), [1, 2, 0, 3])
  assert.deepEqual(sortRows(rows, { group: 'metric' }, false).map(row => row.id), [0, 2, 1, 3])
  const csv = compareCsv(rows.slice(2), [{ group: 'params', name: 'warmup' }, { group: 'metrics', name: 'ndcg' }])
  assert.equal(csv, 'experiment,attempt,status,metric,params:warmup,metrics:ndcg\n2,1,evaluated,0.33,100,0.12\n3,0,failed,,,\n')
})

test('the Compare panel draws the table for more than two experiments, read on open', async () => {
  const h = await mountLive({ visible: true, routes: {
    '/api/runs/r/code': { attempts: [{ node_id: 2, generation: 1, current: true, effective_config: { effective: { world_size: 8 } } }] },
  } })
  localStorage.setItem('looplab.language', 'en')
  try {
    const { default: CompareTable } = await h.load('/src/CompareTable.jsx')
    const mounted = await h.mount(CompareTable, { state, runId: 'r' })
    await until(() => mounted.container.querySelectorAll('tbody tr').length === 3, 'three evaluated rows')
    await until(() => /world_size/.test(mounted.container.textContent), 'effective config column')
    const header = [...mounted.container.querySelectorAll('thead th')].map(th => th.textContent)
    assert.ok(header.some(text => /idea\s*lr/.test(text)))
    assert.ok(header.some(text => /ran with\s*world_size/.test(text)))
    const firstRow = mounted.container.querySelector('tbody tr th').textContent
    assert.match(firstRow, /#1/, 'sorted by the metric, best first on a max run')
  } finally { await h.close() }
})
