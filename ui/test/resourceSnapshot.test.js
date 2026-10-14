// doc 77 §4 / UI review 2026-10-14 section 7.8: what one attempt used — measured, derived and
// not-recorded kept apart, the current lifecycle only.
import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { fmtSeconds, resourceSnapshot } from '../src/resourceSnapshotModel.js'
import { sharedVite } from './_mount.js'

const node = {
  id: 0, attempt: 1, repairs: 1, eval_seconds: 11460.2,
  idea: { footprint: { gpus: 8 } },
  stage_attempts: [
    { name: 'train', seconds: 999, status: 'failed', generation: 0 },          // the reset lifecycle
    { name: 'train', seconds: 10901.534, status: 'ok', generation: 1 },
    { name: 'score', seconds: 552.305, status: 'ok', generation: 1 },
  ],
  extra_metrics: { train_samples_per_second: 1364.174, train_steps_per_second: 8.975, recall: 0.25 },
}

test('the snapshot counts only this lifecycle and labels every derived number', () => {
  const snap = resourceSnapshot(node, { effectiveConfig: { effective: { world_size: 8, max_steps: 96952 } } })
  assert.equal(snap.attempt, 1)
  assert.deepEqual(snap.stages.map(stage => [stage.name, stage.seconds, stage.runs]),
    [['train', 10901.534, 1], ['score', 552.305, 1]], 'generation 0 is not this attempt')
  assert.equal(snap.gpus, 8)
  assert.equal(snap.worldSize, 8)
  // The MiniOneRec D3 receipt: 8 × (10901.534 + 552.305) / 3600 = 25.453 GPU-hours.
  assert.equal(snap.gpuHours.toFixed(3), '25.453')
  assert.equal(snap.stepsPerSecond.toFixed(3), (96952 / 10901.534).toFixed(3))
  assert.deepEqual(snap.trainerRates.map(row => row.key), ['train_samples_per_second', 'train_steps_per_second'])
  assert.deepEqual(snap.peakMemory, [], 'not recorded is empty, never an estimate')
})

test('absent inputs stay absent', () => {
  const snap = resourceSnapshot({ id: 1, stages: [{ name: 'run', status: 'ok' }] })
  assert.equal(snap.gpus, null)
  assert.equal(snap.gpuHours, null)
  assert.equal(snap.stageSeconds, null)
  assert.equal(snap.stepsPerSecond, null)
  assert.equal(snap.wallSeconds, null)
  assert.equal(resourceSnapshot(null), null)
  assert.equal(fmtSeconds(30), '30 s')
  assert.equal(fmtSeconds(10901), '3.03 h')
})

test('the panel is closed until opened and reads nothing before that', async () => {
  const vite = await sharedVite()
  try {
    const { default: ResourceSnapshotPanel } = await vite.ssrLoadModule('/src/ResourceSnapshotPanel.jsx')
    const markup = renderToStaticMarkup(React.createElement(ResourceSnapshotPanel, { runId: 'r', node }))
    assert.match(markup, /<details class="resource-snapshot">/)
    assert.match(markup, /Resources of this attempt/)
    assert.doesNotMatch(markup, /GPU-hours/, 'nothing is drawn or fetched before the operator opens it')
  } finally { await vite.close() }
})

test('opened, the panel reads the attempt effective config once and shows each kind of number', async () => {
  const { mountLive, until } = await import('./_mount.js')
  const h = await mountLive({ visible: true, routes: {
    '/api/runs/r/code': { attempts: [{ node_id: 0, generation: 1, effective_config: { effective: { world_size: 8, max_steps: 96952 } } }] },
  } })
  localStorage.setItem('looplab.language', 'en')
  try {
    const { default: ResourceSnapshotPanel } = await h.load('/src/ResourceSnapshotPanel.jsx')
    const mounted = await h.mount(ResourceSnapshotPanel, { runId: 'r', node })
    assert.equal(h.fetch.calls.filter(call => call.path === '/api/runs/r/code').length, 0)
    const details = mounted.container.querySelector('details')
    await React.act(async () => { details.open = true; details.dispatchEvent(new window.Event('toggle')) })
    await until(() => /world size 8/.test(mounted.container.textContent), 'effective config read')
    const text = mounted.container.textContent
    assert.match(text, /25\.45/)
    assert.match(text, /derived: declared GPUs × stage wall time/)
    assert.match(text, /8\.89 steps\/s/)
    assert.match(text, /train_samples_per_second/)
    assert.match(text, /peak memory\s*not recorded/)
    assert.equal(h.fetch.calls.filter(call => call.path === '/api/runs/r/code').length, 1)
  } finally { await h.close() }
})
