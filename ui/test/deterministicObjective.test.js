// doc 75 UX-13: a task that DECLARED a deterministic objective is not told to repeat itself.
//
// The engine writes `repeat_checks: "not_applicable"` on the node's comparability record when the
// task's contract declares `deterministic: true`; the result notices carry it on their rows. Every
// surface that advised "repeat the selected experiment with multiple seeds" reads it — the Report's
// verdict and its repeat-evidence row, the chat brief, the run list — and nothing else changes:
// without the record (an undeclared task, an old log) the advice stands exactly as before.
import test from 'node:test'
import assert from 'node:assert/strict'

import { analyze, verdict } from '../src/report.js'
import { resultMeasurement, runMeasurement } from '../src/resultMeasurement.js'
import { resultNoticeBrief } from '../src/resultNoticeModel.js'

const record = deterministic => ({ keys: { declared: 'demo-contract' },
  ...(deterministic ? { repeat_checks: 'not_applicable' } : {}) })
const node = (id, metric, deterministic) => ({ id, metric, status: 'evaluated', feasible: true,
  parent_ids: id ? [0] : [], idea: { params: {} },
  metric_provenance: { comparability: record(deterministic) } })
const run = deterministic => ({ run_id: 'demo', direction: 'min', best_node_id: 1,
  nodes: { 0: node(0, 17.18, deterministic), 1: node(1, 8.51, deterministic) } })

test('the Report verdict of a declared-deterministic run asks for no repeat', () => {
  const v = verdict(run(true), analyze(run(true)))
  assert.equal(v.robustness, 'deterministic')
  assert.doesNotMatch(v.nextStep, /[Rr]epeat|seeds/)
  assert.match(v.nextStep, /solution/)
  assert.doesNotMatch(v.headline, /Detector coverage/, 'doc 75 UX-17: the trust label says that')
})

test('without the record the advice stands (an undeclared task, an old log)', () => {
  const v = verdict(run(false), analyze(run(false)))
  assert.equal(v.robustness, 'unconfirmed')
  assert.match(v.nextStep, /Repeat the selected experiment with multiple seeds/)
})

test('the repeat-evidence row and the run list say why no repeat is needed', () => {
  assert.match(resultMeasurement(false, null, 'en', true).reliability, /^Deterministic objective/)
  assert.match(resultMeasurement(false, null, 'en', false).reliability, /No multi-seed confirmation/)
  assert.match(resultMeasurement(true, 3, 'en', true).reliability, /3 repeat checks/,
    'a recorded confirmation is still reported as one')
  const row = { best_confirmed: null, best_metric_comparability: record(true) }
  assert.match(runMeasurement(row).reliability, /^Deterministic objective/)
})

test('the chat brief stops advising repeat runs of a deterministic objective', () => {
  const base = { id: 'node:1:0', kind: 'node', node_id: 1, attempt: 0, status: 'evaluated',
    score: 8.51, confirmed_mean: null, confirmed_std: null, confirmed_seeds: null, feasible: true,
    violations: 0, trust_flagged: false, trust_advisory: false, parent_trust_advisory: false,
    salvaged: false, parents: [], score_comparison: { status: 'no_parent' }, direction: 'min',
    objective: 'task metric', failure: '' }
  assert.match(resultNoticeBrief(base), /repeat runs/)
  const brief = resultNoticeBrief({ ...base, repeat_checks: 'not_applicable' })
  assert.doesNotMatch(brief, /repeat runs|preliminary/)
  assert.match(brief, /^Experiment #1 finished: evaluation score 8\.51\./)
})
