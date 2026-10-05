import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { analyze, directionProfit, verdict, toMarkdown, reportOutcomeEvidence } from '../src/report.js'
import { scoreDifference, parentScoreDifference } from '../src/scoreComparison.js'
import { mountHarness, mountLive, click } from './_mount.js'

const reference = JSON.parse(await readFile(new URL('../../tests/fixtures/parent_comparison_v1.json', import.meta.url), 'utf8'))

const node = (id, metric, extra = {}) => ({ id, attempt: 0, metric, status: 'evaluated', feasible: true,
  operator: id ? 'improve' : 'draft', parent_ids: id ? [0] : [],
  ...(id ? reference : { parent_comparison: null }),
  idea: { theme: id ? 'candidate' : 'initial', params: {} },
  metric_provenance: { comparability: { keys: { measured: 'matching-inputs' } } }, ...extra })
const state = (child = node(1, 7), extra = {}) => ({ run_id: 'coverage', direction: 'min',
  best_node_id: 1, nodes: { 0: node(0, 10), 1: child }, ...extra })
const stamped = (n, digest = 'a'.repeat(64)) => ({ ...n, metric_provenance: { ...n.metric_provenance,
  base_revision: { version: 1, complete: true, digest, node_id: n.id, generation: n.attempt,
    scope: 'seeded_editables_before_mounts_and_overlay', file_count: 1, bytes: 10, seed_event_seq: 2,
    archive: { version: 1, status: 'stored', path: `base_snapshots/${digest}` } } } })

test('rollups and regressions compare primary scores, while the numeric frontier retains its measurement type', () => {
  for (const direction of ['min', 'max']) {
    const parent = node(0, 10, { confirmed_mean: direction === 'min' ? 100 : -100 })
    const child = node(1, direction === 'min' ? 12 : 8, { confirmed_mean: direction === 'min' ? 1 : 200 })
    const run = state(child, { direction, nodes: { 0: parent, 1: child } })
    const a = analyze(run), operator = a.operators.find(row => row.key === 'improve')
    assert.deepEqual([a.compared, a.uncompared], [1, 1], 'the root is not a parent comparison')
    assert.deepEqual([operator.compared, operator.improved, operator.uncompared], [1, 0, 0])
    assert.equal(operator.best, child.confirmed_mean)
    assert.equal(operator.bestConfirmed, true)
    assert.deepEqual(reportOutcomeEvidence(run).worse.map(row => [row.node.id, row.gain]), [[1, -2]])
    assert.deepEqual(a.regressions.map(row => [row.id, row.metric, row.parentMetric]), [[1, child.metric, 10]])
    assert.match(verdict(run, a).headline, /confirmation mean.*worse by 2/)
    const candidate = directionProfit(run).find(row => row.direction === 'candidate')
    assert.equal(candidate.gain, -2)
    assert.equal(candidate.baseline, 10, 'do not use the parent confirmation mean as baseline')
    assert.equal(candidate.comparisonMetric, child.metric)
    assert.equal(candidate.gainBasis, 'evaluation_score')
    const md = toMarkdown(run)
    assert.match(md, /Worse evaluation scores.*1.*matching recorded parent conditions/)
    assert.match(md, /\(confirmation mean\)/)
    assert.doesNotMatch(md, /didn't pay off/)
  }
})

test('matching primary scores count improvements and ties without using repeat means', () => {
  for (const [direction, metric] of [['min', 7], ['max', 13]]) {
    const run = state(node(1, metric), { direction })
    const a = analyze(run)
    assert.equal(a.operators.find(row => row.key === 'improve').improved, 1)
    assert.equal(a.regressions.length, 0)
    assert.equal(reportOutcomeEvidence(run).better.length, 1)
    assert.equal(directionProfit(run).find(row => row.direction === 'candidate').gain, 3)
  }
  const tie = analyze(state(node(1, 10, { confirmed_mean: -100 })))
  assert.equal(tie.compared, 1)
  assert.equal(tie.operators.find(row => row.key === 'improve').improved, 0)
  assert.equal(tie.regressions.length, 0)
  assert.equal(reportOutcomeEvidence(state(node(1, 10))).unchanged.length, 1)
})

test('missing comparison evidence, wrong attempts, merges and exclusions never become a win or a regression', () => {
  const changedKey = { comparability: { keys: { measured: 'other-inputs' } } }
  const cases = [
    state(node(1, 20, { metric_provenance: null })), state(node(1, 20, { metric_provenance: changedKey })),
    state(node(1, 20, { parent_comparison: { version: 1, node_id: 0, attempt: 1 } })),
    state(node(1, 20, { parent_comparison: null })), state(node(1, 20, { parent_ids: '0' })),
    state(node(1, 20, { parent_comparison: { version: 2, node_id: 0, attempt: 0 } })),
    state(node(1, 20, { parent_comparison: { version: 1, node_id: 99, attempt: 0 } })),
    state(node(1, 20, { parent_ids: [0, 2] })), state(node(1, 20, { parent_ids: [99] })),
    state(node(1, 20, { feasible: false })), state(node(1, 20, { metric: null, confirmed_mean: 1 })),
    state(undefined, { direction: null }), state(undefined, { objective_key: 'accuracy' }),
    state(undefined, { source_integrity: { complete: false } }),
    state(undefined, { breed_excluded: [0] }), state(undefined, { breed_excluded: [1] }),
    state(undefined, { aborted_nodes: [0] }),
    state(undefined, { nodes: { 0: node(0, 10, { status: 'failed' }), 1: node(1, 20) } }),
  ]
  for (const run of cases) {
    const a = analyze(run)
    assert.equal(a.compared, 0)
    assert.equal(a.regressions.length, 0)
    assert.equal(reportOutcomeEvidence(run).better.length, 0)
    assert.equal(reportOutcomeEvidence(run).worse.length, 0)
    assert.ok(a.operators.every(row => row.improved === 0))
    assert.equal(parentScoreDifference(run.nodes[1], run.nodes, run), null)
    assert.doesNotMatch(toMarkdown(run), /didn't pay off|node\(s\) ran but did not beat/)
  }
  const reset = state(undefined, { nodes: { 0: node(0, 10, { attempt: 1 }), 1: node(1, 7) } })
  assert.equal(analyze(reset).compared, 0, 'a reset parent is not the referenced attempt')
  assert.equal(analyze(state(undefined, { direction: null })).steps.length, 0, 'unknown direction is not minimize')
})

test('upstream/base receipts and finite arithmetic fence all score comparisons, including the selected verdict', () => {
  const parent = stamped(node(0, 10)), child = stamped(node(1, 7))
  const same = state(child, { upstream_enabled: true, nodes: { 0: parent, 1: child } })
  assert.equal(scoreDifference(child, parent, same), -3)
  assert.equal(verdict(same, analyze(same)).outcome, 'improved')
  for (const other of [stamped(child, 'b'.repeat(64)), node(1, 7),
    { ...child, metric_provenance: { ...child.metric_provenance, base_revision: { complete: false } } }]) {
    for (const upstream_enabled of [false, true]) {
      const run = { ...same, upstream_enabled, nodes: { 0: parent, 1: other } }
      assert.equal(scoreDifference(other, parent, run), null)
      assert.equal(analyze(run).compared, 0)
      assert.equal(verdict(run, analyze(run)).outcome, 'uncompared')
      assert.equal(directionProfit(run).find(row => row.direction === 'candidate').gain, null)
    }
  }
  const overflow = state(node(1, 1e308), { nodes: { 0: node(0, -1e308), 1: node(1, 1e308) } })
  assert.equal(parentScoreDifference(overflow.nodes[1], overflow.nodes, overflow), null)
})

test('Report and Markdown expose unavailable comparisons as coverage rather than failure', async () => {
  const harness = await mountHarness()
  try {
    const { default: Report } = await harness.load('/src/Report.jsx')
    for (const run of [state(), state(node(1, 20, { metric_provenance: null }))]) {
      const a = analyze(run)
      const holder = document.createElement('div')
      holder.innerHTML = harness.render(Report, { state: run, runId: run.run_id, readOnly: true })
      const heading = holder.querySelector('#report-section-comparisons')
      assert.equal(heading.textContent, 'Parent comparison coverage')
      const table = heading.nextElementSibling.nextElementSibling.querySelector('table')
      const childRow = [...table.querySelectorAll('tbody tr')].find(row => row.cells[0].textContent === 'improve')
      assert.deepEqual([...childRow.cells].slice(1, 5).map(cell => Number(cell.textContent)),
        [1, a.compared, a.compared, 1 - a.compared])
      assert.match(heading.nextElementSibling.textContent, /does not mean failure/)
      assert.doesNotMatch(holder.textContent, /nothing notably failed|didn't pay off/)
      assert.match(toMarkdown(run), new RegExp(`${a.compared} compared; ${a.uncompared} not compared`))
    }
    assert.deepEqual(harness.fetch.calls, [], 'rendering comparison evidence sends no model or command request')
  } finally { await harness.close() }
})


test('outcome navigation uses measured comparisons and withdraws a reset parent from successes', async () => {
  const harness = await mountLive()
  try {
    const { default: Outcomes } = await harness.load('/src/ReportOutcomes.jsx')
    const run = state(undefined, { nodes: {
      0: node(0, 10), 1: node(1, 7), 2: node(2, 12), 3: node(3, 10),
      4: node(4, 1, { parent_comparison: null }),
      5: node(5, null, { status: 'failed', error_reason: 'crash' }),
    } })
    const evidence = reportOutcomeEvidence(run)
    assert.deepEqual([evidence.better.length, evidence.worse.length, evidence.unchanged.length,
      evidence.unknown.length], [1, 1, 1, 2])
    const picked = []
    const mounted = await harness.mount(Outcomes, {
      evidence, state: run, onPickNode: id => picked.push(id),
    })
    const better = mounted.container.querySelector('[aria-label="Better evaluation scores"]')
    const worse = mounted.container.querySelector('[aria-label="Worse evaluation scores"]')
    assert.match(better.textContent, /10 → 7; parent #0/)
    assert.match(worse.textContent, /10 → 12; parent #0/)
    assert.doesNotMatch(better.textContent + worse.textContent, /#4|#5/)
    await click(better.querySelector('button'))
    assert.deepEqual(picked, [1])
    const reset = { ...run, nodes: { ...run.nodes, 0: node(0, 10, { attempt: 1 }) } }
    await mounted.rerender({ evidence: reportOutcomeEvidence(reset), state: reset })
    assert.match(mounted.container.textContent, /No comparable score improvement is established/)
    assert.match(mounted.container.textContent, /Comparison not established: 5/)
    assert.deepEqual(harness.fetch.calls, [], 'outcome reading and navigation need no model call')
  } finally { await harness.close() }
})
