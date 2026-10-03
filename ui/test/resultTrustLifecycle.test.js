import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { click, fetchStub, jsonResponse, mountLive, until } from './_mount.js'
import { analyze, buildModelCard, toMarkdown, verdict } from '../src/report.js'
import { currentRewardHacks, rewardHackNodeCount } from '../src/nodeProjection.js'
import { rewardHackStatus } from '../src/trustSemantics.js'

const fixture = JSON.parse(await readFile(new URL('../../tests/fixtures/trust_attempt_cases_v1.json', import.meta.url), 'utf8'))
const stateFor = item => ({ run_id: 'trust-history', direction: 'min', best_node_id: 1,
  nodes: Object.fromEntries(Object.entries(fixture.nodes).map(([id, node]) => [id, {
    ...node, operator: id === '0' ? 'draft' : 'improve', idea: { params: {}, rationale: '' },
    metric_provenance: { comparability: { keys: { measured: 'a'.repeat(16) } } },
  }])), reward_hacks: item.records, phase: 'finished' })

test('current Trust signals bind to active attempts and count nodes rather than ledger rows', () => {
  for (const item of fixture.cases) {
    const state = stateFor(item)
    const records = currentRewardHacks(state)
    assert.equal(rewardHackNodeCount(records), item.current_nodes)
    assert.equal(state.reward_hacks.length, item.records.length, 'history is not rewritten')
    assert.equal(verdict(state, analyze(state)).trust, item.current_nodes ? 'suspect' : 'unverified')
    if (item.current_nodes) assert.equal(rewardHackStatus(records, {}, 2).label, '1 suspicious node flagged')
  }
  const item = fixture.cases[1], state = stateFor(item)
  for (const changed of [
    { aborted_nodes: [1] }, { nodes: { 0: state.nodes[0] } },
    { nodes: { ...state.nodes, 1: { ...state.nodes[1], tombstoned: true } } },
    ...[null, '1', true, -1, 1.5, 2].map(generation => ({ reward_hacks: [{ ...item.records[1], generation }] })),
    { reward_hacks: [{ node_id: 1 }] }, { reward_hacks: null },
    { reward_hacks: [{ node_id: 'constructor', generation: 0 }] },
    { nodes: { 1: { ...state.nodes[1], id: 2 } } },
  ]) assert.deepEqual(currentRewardHacks({ ...state, ...changed }), [])
  const legacy = { nodes: { 1: { id: 1 } }, reward_hacks: [{ node_id: 1 }] }
  assert.equal(currentRewardHacks(legacy).length, 1, 'a legacy row binds to attempt zero')
  assert.equal(currentRewardHacks({ ...legacy, nodes: { 1: { id: 1, attempt: 1 } } }).length, 0)
})

test('quarantine preserves the signal attempt across a reset during the lifecycle probe', async () => {
  const h = await mountLive()
  let releaseProbe
  const backend = fetchStub({
    'GET /api/runs/trust-history/config': {},
    'GET /api/runs/trust-history/lifecycle': () => new Promise(resolve => { releaseProbe = resolve }),
    'POST /api/runs/trust-history/commands': () => jsonResponse({ detail: 'Node generation changed' }, 409),
  })
  globalThis.fetch = backend
  localStorage.clear(); sessionStorage.clear()
  try {
    const { TrustPanel } = await h.load('/src/panels.jsx')
    const state = stateFor(fixture.cases[1])
    const view = await h.mount(TrustPanel, { state, runId: 'trust-history', onClose() {}, onToast() {} })
    try {
      const action = [...view.container.querySelectorAll('button')].find(button => button.textContent === 'quarantine' && !button.disabled)
      assert.ok(action)
      await click(action)
      await until(() => releaseProbe, 'generation probe before submission')
      await view.rerender({ state: { ...state, nodes: { ...state.nodes, 1: { ...state.nodes[1], attempt: 2 } } } })
      releaseProbe(jsonResponse({ schema: 1, seq: 0, event_count: 1, generation: 'a'.repeat(64), engine_running: false }))
      await until(() => backend.calls.some(call => call.method === 'POST'), 'original attempt submission')
      const writes = backend.calls.filter(call => call.method === 'POST')
      assert.equal(writes.length, 1)
      const body = JSON.parse(writes[0].body)
      assert.equal(body.type, 'node_abort')
      assert.deepEqual(body.data, { node_id: 1, generation: 1, reason: 'ui' })
      assert.equal(body.expected_generation, 'a'.repeat(64))
      assert.ok([...view.container.querySelectorAll('button')].filter(button => button.textContent === 'quarantine').every(button => button.disabled))
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('Report, Overview, chart and exports agree after a flagged node is reset and re-evaluated', async () => {
  const h = await mountLive()
  try {
    const { default: Report } = await h.load('/src/Report.jsx')
    const { OverviewPanel } = await h.load('/src/panels.jsx')
    const { Trajectory } = await h.load('/src/charts.jsx')
    for (const item of fixture.cases) {
      const state = stateFor(item), flagged = item.current_nodes > 0
      const result = verdict(state, analyze(state))
      assert.equal(result.outcome, 'improved', 'Trust caveats do not rewrite measured values or selection under audit')
      assert.equal(result.gain, -.75)
      assert.match(result.headline, /confirmation mean 0.2.*evaluation score is better by 0.75/)
      for (const Component of [Report, OverviewPanel]) {
        const holder = document.createElement('div')
        holder.innerHTML = h.render(Component, { state, runId: state.run_id, readOnly: true })
        const headline = holder.querySelector(Component === Report ? '.verdict-headline' : '[aria-label="Result interpretation"] p')
        assert.ok(headline)
        assert.equal(headline.textContent, result.headline)
        assert.equal(/result is flagged/.test(headline.textContent), flagged)
        if (Component === OverviewPanel) assert.equal(!!holder.querySelector('.ov-signal-alert'), flagged)
      }
      const chart = await h.mount(Trajectory, { state, nodes: Object.values(state.nodes), direction: 'min' })
      try {
        assert.equal(/eligible frontier:.*flagged/.test(chart.container.querySelector('svg').textContent), flagged)
      } finally { await chart.unmount() }
      assert.equal(buildModelCard(state).deterministic_trust.status, result.trust)
      assert.equal(toMarkdown(state).includes('champion flagged as a possible reward-hack'), flagged)
      assert.ok(toMarkdown(state).includes(result.headline))
    }
    assert.equal(h.fetch.calls.length, 0)
  } finally { await h.close() }
})

test('Trust keeps attempt history, disables stale quarantine and preserves read-only actions', async () => {
  const h = await mountLive()
  const backend = fetchStub({ 'GET /api/runs/trust-history/config': { reward_hack_detect: true } })
  globalThis.fetch = backend
  try {
    const { TrustPanel } = await h.load('/src/panels.jsx')
    const view = await h.mount(TrustPanel, { state: stateFor(fixture.cases[0]), runId: 'trust-history',
      onClose() {}, onSelect() {}, onToast() {} })
    try {
      await until(() => view.container.textContent.includes('No current suspicious signals recorded'), 'coverage read')
      const rows = () => [...view.container.querySelectorAll('table caption')]
        .find(caption => caption.textContent === 'Reward-hacking signal history')?.closest('table').querySelectorAll('tbody tr')
      assert.equal(rows().length, 1)
      assert.match(rows()[0].textContent, /Historical or unavailable/)
      const oldAction = rows()[0].querySelector('td:last-child button')
      assert.equal(oldAction.disabled, true)
      await click(oldAction)
      assert.equal(backend.calls.some(call => call.method !== 'GET'), false)
      assert.match(view.container.textContent, /does not prove each attempt was inspected/)
      await view.rerender({ state: stateFor(fixture.cases[2]) })
      assert.equal(rows().length, 3, 'all signal records remain available')
      assert.equal(rows()[0].querySelector('td:last-child button').disabled, true)
      assert.equal(rows()[1].querySelector('td:last-child button').disabled, false)
      assert.equal(rows()[2].querySelector('td:last-child button').disabled, false)
      assert.match(view.container.textContent, /1 currently flagged/)
      const legacy = stateFor(fixture.cases[0])
      legacy.nodes[1].attempt = 0
      legacy.reward_hacks = [{ node_id: 1, signals: [{ signal: 'protected_missing' }] }]
      await view.rerender({ state: legacy })
      assert.match(view.container.textContent, /1 currently flagged/)
      assert.equal(rows()[0].querySelector('td:last-child button').disabled, true, 'legacy warnings cannot authorize an unbound quarantine')
      await view.rerender({ state: stateFor(fixture.cases[2]), readOnly: true })
      assert.equal([...rows()].some(row => row.querySelector('td:last-child button')), false)
      assert.equal(backend.calls.some(call => call.method !== 'GET'), false)
    } finally { await view.unmount() }
  } finally { await h.close() }
})
