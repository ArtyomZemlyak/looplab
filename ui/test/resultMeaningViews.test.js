import test from 'node:test'
import assert from 'node:assert/strict'
import { fetchStub, mountLive, until } from './_mount.js'
import { resultMeasurement, runMeasurement } from '../src/resultMeasurement.js'

let harness, RunList, Report
test.before(async () => {
  harness = await mountLive()
  ;({ default: RunList } = await harness.load('/src/RunList.jsx'))
  ;({ default: Report } = await harness.load('/src/Report.jsx'))
})
test.after(async () => { await harness?.close() })

const selected = { node_id: 0, attempt: 0, value: 0, confirmed: true, seeds: 3 }
const row = (id, mean, summary = null) => ({ run_id: id, label: id, task_id: 'task',
  direction: 'min', phase: 'finished', finished: true, engine_running: false, nodes: 1,
  best_metric: 2, best_confirmed: mean, result_summary: summary,
  source_integrity: { complete: true }, best_metric_caveats: [] })

test('recorded means require a bound repeat count and never become certified outcomes', () => {
  const summary = { first: selected, selected }
  assert.match(runMeasurement(row('zero', 0, summary)).reliability, /3 repeat checks/)
  for (const changed of [null, { selected: { ...selected, confirmed: false } },
    { selected: { ...selected, value: 1 } }, { selected: { ...selected, node_id: null } },
    { selected: { ...selected, attempt: -1 } }]) {
    assert.match(runMeasurement(row('unknown', 0, changed)).reliability, /not established/)
  }
  for (const seeds of [null, undefined, 0, 1, -1, true, '3', 2.5, Infinity]) {
    assert.match(resultMeasurement(true, seeds).reliability, /not established/)
  }
  assert.equal(resultMeasurement(false, 3).label, 'evaluation score')
  assert.match(resultMeasurement(false, 3).reliability, /exploratory/)
  assert.equal(resultMeasurement(true, 3, 'ru').label, 'среднее повторных запусков')
  assert.match(resultMeasurement(true, 3, 'ru').reliability, /Повторных проверок: 3/)
})

test('RunList labels selected scores and means, and distinguishes repeats from missing evidence', async () => {
  localStorage.clear(); sessionStorage.clear()
  const rows = [row('score', null), row('mean', 0, { first: selected, selected }), row('legacy-mean', 0)]
  globalThis.fetch = fetchStub({
    'GET /api/runs': rows,
    'GET /api/projects': { projects: [], assignments: {} },
    'GET /api/supertasks': { supertasks: [], assignments: {} },
    'GET /api/runs/accesses': { accesses: [] },
  })
  const view = await harness.mount(RunList, { onOpen() {}, onGlobalNavigate() {} })
  try {
    await until(() => view.container.querySelectorAll('.run-card-metrics').length === 3, 'result cards loaded')
    for (const card of view.container.querySelectorAll('.run-card')) {
      const metrics = card.querySelector('.run-card-metrics').textContent
      const id = card.querySelector('[data-run-open-id]').getAttribute('data-run-open-id')
      assert.match(metrics, /selected/)
      if (id === 'score') assert.match(metrics, /evaluation score.*exploratory/)
      else if (id === 'mean') assert.match(metrics, /confirmation mean.*3 repeat checks/)
      else assert.match(metrics, /confirmation mean.*not established/)
      assert.doesNotMatch(metrics, /single-seed|robust|verified/)
    }
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally { await view.unmount() }
})

test('Report separates the confirmation mean from its score and never invents a repeat count or spread', () => {
  for (const confirmed of [false, true]) {
    const node = { id: 0, attempt: 0, status: 'evaluated', feasible: true, metric: 2,
      confirmed_mean: confirmed ? 0 : null, confirmed_seeds: null, confirmed_std: null,
      operator: 'draft', parent_ids: [], idea: { params: {} } }
    const state = { run_id: 'meaning', direction: 'min', phase: 'finished',
      best_node_id: 0, nodes: { 0: node } }
    const holder = document.createElement('div')
    holder.innerHTML = harness.render(Report, { state, runId: 'meaning', readOnly: true })
    const card = holder.querySelector('.champion-card')
    assert.ok(card)
    assert.doesNotMatch(card.textContent, /single-seed|undefined|over null|±—/)
    if (confirmed) {
      assert.match(card.textContent, /confirmation mean0.*spread not recorded.*evaluation score2/)
      assert.match(card.textContent, /multiple successful repeats not established/)
    } else assert.match(card.textContent, /evaluation score2.*exploratory result/)
  }
})
