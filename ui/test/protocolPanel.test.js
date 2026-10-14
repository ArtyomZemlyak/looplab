import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { click, fetchStub, mountLive, settle, until } from './_mount.js'

// The run's Protocol panel and the super-task Campaign block (doc 81), mounted live against stubbed
// routes: experiments and the amendment trail render with their AUTHORS, the paired comparison is
// fetched only when asked for, and the narrative is labelled as authored text beside LoopLab's table.
const protocol = {
  protocol_version: 2, policy: { amendments: 'all', max_agent_added_nodes: 3, source: 'harness_policy.json' },
  experiments: [{ experiment_id: 'D4', title: 'repeat vs fresh', hypothesis: 'D4 matches D3',
    arms: { D4: {}, D3: { run_id: 'run-d3', node_id: 0 } }, contrasts: [{}],
    registration: { recorded_at: 1791937080, at_event_seq: 12, author: { kind: 'agent' }, version: 1 } }],
  amendments: [{ amendment_id: 'am2-x', version: 2, kind: 'files', author: { kind: 'operator' },
    reason: 'fix max_steps in trainer', summary: 'base files amended: sft.py', recorded_at: 1791937090,
    payload: { files: [{ path: 'sft.py', diff: '-steps=1\n+steps=2\n', protected: true }] } }],
}
const comparison = {
  contrasts: [{ id: 'D4-D3', status: 'computed', family: 'primary', family_size: 2, metric: 'FUR@200',
    test: 'original_ic', treatment: 'D4', control: 'D3', delta: 0.000475171, ci95_low: -0.0014586,
    ci95_high: 0.0024088, p_two_sided: 0.63006, p_adjusted: 0.63006, clusters: 40000,
    registered_before_result: false,
    treatment_source: { run_id: 'run-d4', node_id: 0 }, control_source: { run_id: 'run-d3', node_id: 0 } }],
  caveats: ['some contrasts were registered AFTER their treatment result existed (post hoc)'],
  computed_by: { method: 'looplab-paired-v1', ci: 'normal', correction: 'holm', computed_at: 1791937100 },
}

test('the Protocol panel shows experiments, authors and the trail, and computes on request', async () => {
  const harness = await mountLive(); localStorage.clear(); localStorage.setItem('looplab.language', 'en')
  const stub = fetchStub({
    '/api/runs/run-d4/protocol': protocol,
    '/api/runs/run-d4/experiments/D4/comparison': comparison,
  })
  globalThis.fetch = stub
  try {
    const { default: ProtocolPanel } = await harness.load('/src/ProtocolPanel.jsx')
    const view = await harness.mount(ProtocolPanel, { runId: 'run-d4', onClose() {} })
    await until(() => view.container.textContent.includes('repeat vs fresh'), 'experiment rendered')
    const text = view.container.textContent
    assert.ok(text.includes('by agent'), text)
    assert.ok(text.includes('fix max_steps in trainer') && text.includes('operator'))
    assert.ok(text.includes('node cap 3'))
    assert.ok(!stub.calls.some(c => c.path.endsWith('/comparison')), 'comparison is not fetched unasked')
    const button = [...view.container.querySelectorAll('button')].find(b => b.textContent.includes('Compute'))
    await click(button)
    await until(() => view.container.textContent.includes('0.0004752'), 'contrast rendered')
    assert.ok(view.container.textContent.includes('post hoc'))
    assert.ok(view.container.textContent.includes('run-d4#0 / run-d3#0'))
  } finally { await harness.close() }
})

test('the Campaign block keeps the authored narrative apart from the computed table', async () => {
  const harness = await mountLive(); localStorage.clear(); localStorage.setItem('looplab.language', 'en')
  globalThis.fetch = fetchStub({
    '/api/supertasks/st_0123456789/campaign': {
      supertask: { id: 'st_0123456789', name: 'scaling' },
      runs: [{ run_id: 'run-d4', amendments: [], experiments: [{ ...protocol.experiments[0], contrasts: comparison.contrasts }] }],
      leaderboards: [{ test_id: 'qs1-abc', test_label: 'original_ic', metric: 'FUR@200',
        entries: [{ run_id: 'run-d4', node_id: 0, value: 0.4018, ci95_low: 0.397, ci95_high: 0.406, author: { kind: 'agent' } }] }],
      narrative: { revision: 1, title: 'Data scaling', body_markdown: 'Repetition **matches** fresh data.',
        key_findings: ['D4 ≈ D3'], author: { kind: 'agent' }, recorded_at: 1791937200 },
    },
  })
  try {
    const { default: CampaignReport } = await harness.load('/src/CampaignReport.jsx')
    const view = await harness.mount(CampaignReport, { supertaskId: 'st_0123456789' })
    await until(() => view.container.textContent.includes('Data scaling'), 'narrative rendered')
    await settle()
    const text = view.container.textContent
    assert.ok(text.includes('authored text, not a measurement'))
    assert.ok(text.includes('matches') && text.includes('D4 ≈ D3'))
    assert.ok(text.includes('0.0004752') && text.includes('0.4018'))
  } finally { await harness.close() }
})
