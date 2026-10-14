import test from 'node:test'
import assert from 'node:assert/strict'
import {
  amendmentView, authorLabel, campaignView, comparisonView, contrastRow, fmtCI, fmtP, fmtStat,
  leaderboardView, protocolView,
} from '../src/campaignModel.js'

const contrast = (extra = {}) => ({
  id: 'D4-D3', status: 'computed', family: 'primary', family_size: 2, metric: 'FUR@200',
  test: 'original_ic', treatment: 'D4', control: 'D3', delta: 0.000475171, se: 0.000987,
  ci95_low: -0.0014585, ci95_high: 0.0024088, p_two_sided: 0.63006, p_adjusted: 0.63006,
  significant: false, clusters: 40000, registered_before_result: true,
  treatment_source: { run_id: 'run-d4', node_id: 0, stale: false, consistency: 'matches_measured' },
  control_source: { run_id: 'run-d3', node_id: 0, stale: false, consistency: 'matches_measured' },
  ...extra,
})

test('numbers are the server\'s, formatted without inventing precision or zeros', () => {
  assert.equal(fmtStat(0.000475171), '0.0004752')
  assert.equal(fmtStat(null), '—')
  assert.equal(fmtStat(Number.NaN), '—')
  assert.equal(fmtStat(1.2e-6), '1.20e-6')
  assert.equal(fmtP(1.486e-28), '1.5e-28')
  assert.equal(fmtP(0.63006), '0.6301')
  assert.equal(fmtCI(-0.0014586, 0.0024088), '[-0.001459; 0.002409]')
  assert.equal(fmtCI(null, 1), '—')
})

test('authorship is always named, and an unknown author is not promoted to the operator', () => {
  assert.equal(authorLabel({ kind: 'agent', credential: 'agent_token' }), 'agent')
  assert.equal(authorLabel({ kind: 'operator' }), 'operator')
  assert.equal(authorLabel('rule'), 'rule')
  assert.equal(authorLabel({ kind: 'forged' }), 'unknown')
  assert.equal(authorLabel(null), 'unknown')
})

test('a contrast row carries its provenance flags beside the number', () => {
  const ok = contrastRow(contrast())
  assert.equal(ok.treatmentWhere, 'run-d4#0')
  assert.deepEqual(ok.flags.map(f => f.label), ['pre-registered'])
  const bad = contrastRow(contrast({
    registered_before_result: false,
    treatment_source: { run_id: 'run-d4', node_id: 0, stale: true, consistency: 'differs_from_measured' },
  }))
  assert.deepEqual(bad.flags.map(f => f.label),
    ['post hoc', 'treatment stale', 'treatment ≠ measured'])
  const missing = contrastRow({ id: 'x', status: 'unavailable', reason: 'arm has no node yet' })
  assert.equal(missing.delta, null)
  assert.equal(missing.flags[0].label, 'not computed')
  assert.equal(missing.flags[0].title, 'arm has no node yet')
})

test('the protocol view keeps experiments, the audit trail and its diffs', () => {
  const view = protocolView({
    protocol_version: 3,
    policy: { amendments: 'inputs', max_agent_added_nodes: 4, source: 'harness_policy.json' },
    experiments: [{ experiment_id: 'D4', hypothesis: 'h', arms: { D4: {}, D3: { run_id: 'r3', node_id: 0 } },
      contrasts: [{}, {}], superseded: true,
      registration: { recorded_at: 1791937080, at_event_seq: 12, author: { kind: 'agent' }, version: 1 } },
    { experiment_id: '' }],
    amendments: [{ amendment_id: 'am2-x', version: 2, kind: 'files', author: { kind: 'agent' },
      reason: 'fix max_steps', summary: 'base files amended: sft.py', recorded_at: 1791937090,
      payload: { files: [{ path: 'sft.py', diff: '-a\n+b\n', protected: true }] } },
    { amendment_id: 'am3-y', kind: 'task', payload: { diff: '+"x"' } }, { nothing: true }],
  })
  assert.equal(view.version, 3)
  assert.deepEqual(view.policy, { amendments: 'inputs', cap: 4, source: 'harness_policy.json' })
  assert.equal(view.experiments.length, 1)
  assert.equal(view.experiments[0].arms[1].runId, 'r3')
  assert.equal(view.experiments[0].author, 'agent')
  assert.equal(view.experiments[0].superseded, true)
  assert.equal(view.amendments.length, 2)
  assert.deepEqual(view.amendments[0].diffs, [{ path: 'sft.py', diff: '-a\n+b\n', protected: true }])
  assert.equal(view.amendments[1].diffs[0].path, 'task.snapshot.json')
  assert.equal(amendmentView(null), null)
})

test('comparison, leaderboard and campaign payloads degrade to explicit gaps', () => {
  const comp = comparisonView({ contrasts: [contrast()], caveats: ['post hoc'],
    computed_by: { method: 'looplab-paired-v1', ci: 'normal', correction: 'holm', computed_at: 1 } })
  assert.equal(comp.rows[0].pAdj, 0.63006)
  assert.equal(comp.method, 'looplab-paired-v1')
  const board = leaderboardView({ test_id: 'qs1-abc', metric: 'FUR@200', entries: [
    { run_id: 'r', node_id: 1, value: 0.4, ci95_low: 0.39, ci95_high: 0.41, author: { kind: 'agent' } },
    { run_id: 'r', node_id: 'x', value: 'bad' }] })
  assert.equal(board.rows[1].nodeId, null)
  assert.equal(board.rows[1].value, null)
  const campaign = campaignView({ supertask: { name: 'scaling' },
    runs: [{ run_id: 'r', experiments: [{ experiment_id: 'E', contrasts: [contrast()] }], amendments: [{}] }],
    narrative: { body_markdown: '## x', revision: 2, author: { kind: 'agent' }, key_findings: ['a', 3] } })
  assert.equal(campaign.runs[0].experiments[0].rows.length, 1)
  assert.equal(campaign.narrative.author, 'agent')
  assert.deepEqual(campaign.narrative.findings, ['a'])
  assert.equal(campaignView({}).narrative, null)
})
