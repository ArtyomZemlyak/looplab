// Unit tests for the New-run (Genesis) card's launch-gating logic. Pure, framework-free — run with
// `node --test test/` from ui/. The card delegates its "can Start" decision to these helpers, so a
// blank/under-specified plan the boss couldn't fill is recoverable in-card (pick a kind, fill the
// fields) rather than a dead end behind the manual form — these lock down that per-kind contract.
import test from 'node:test'
import assert from 'node:assert/strict'
import { genesisTaskReady, genesisLaunchReady, GENESIS_TASK_KINDS, genesisDefaultDirection } from '../src/util.js'

test('a blank plan is not launchable (the boss-left-it-empty case the user hit)', () => {
  assert.equal(genesisTaskReady(null), false)
  assert.equal(genesisTaskReady({}), false)
  assert.equal(genesisTaskReady({ task: {} }), false)          // no kind -> show the picker, not Start
  assert.equal(genesisLaunchReady({ run_id: 'r', task: {} }), false)
})

test('a catalogue task_file is always ready', () => {
  assert.equal(genesisTaskReady({ task_file: 'examples/regression_task.json' }), true)
  assert.equal(genesisLaunchReady({ run_id: 'reg', task_file: 'examples/regression_task.json' }), true)
})

test('the synthetic kinds need only a kind', () => {
  for (const kind of ['quadratic', 'classification', 'regression', 'timeseries', 'code_regression', 'mlebench'])
    assert.equal(genesisTaskReady({ task: { kind } }), true, kind)
})

test('mlebench_real needs a non-empty competition', () => {
  assert.equal(genesisTaskReady({ task: { kind: 'mlebench_real' } }), false)
  assert.equal(genesisTaskReady({ task: { kind: 'mlebench_real', competition: '  ' } }), false)
  assert.equal(genesisTaskReady({ task: { kind: 'mlebench_real', competition: 'spooky-author-identification' } }), true)
})

test('dataset needs a data path (data_path or a named data entry)', () => {
  assert.equal(genesisTaskReady({ task: { kind: 'dataset' } }), false)
  assert.equal(genesisTaskReady({ task: { kind: 'dataset', data_path: '/d/train.csv' } }), true)
  assert.equal(genesisTaskReady({ task: { kind: 'dataset', data: { train: '/d/train.csv' } } }), true)
  assert.equal(genesisTaskReady({ task: { kind: 'dataset', data: {} } }), false)
})

test('repo needs an editable path AND a way to score it (eval command or onboard)', () => {
  assert.equal(genesisTaskReady({ task: { kind: 'repo' } }), false)
  assert.equal(genesisTaskReady({ task: { kind: 'repo', editable_path: '/my/repo' } }), false)   // no scorer
  assert.equal(genesisTaskReady({ task: { kind: 'repo', editable_path: '/my/repo', eval: { command: ['python', 'train.py'] } } }), true)
  assert.equal(genesisTaskReady({ task: { kind: 'repo', editable_path: '/my/repo', eval: { command: [] } } }), false)
  assert.equal(genesisTaskReady({ task: { kind: 'repo', editable_path: '/my/repo', onboard: true } }), true)
})

test('launchability also requires a run name', () => {
  const task = { kind: 'mlebench_real', competition: 'spooky-author-identification' }
  assert.equal(genesisLaunchReady({ run_id: '', task }), false)
  assert.equal(genesisLaunchReady({ run_id: '  ', task }), false)
  assert.equal(genesisLaunchReady({ run_id: 'spooky', task }), true)
})

test('the Direction default matches each task model default (so the select never lies)', () => {
  // toytask.py / regression.py / timeseries.py default direction='min'; the rest 'max'. The card shows
  // this default without persisting it, so a mismatch would launch a run optimizing the opposite way.
  for (const kind of ['quadratic', 'regression', 'code_regression', 'timeseries'])
    assert.equal(genesisDefaultDirection(kind), 'min', kind)
  for (const kind of ['classification', 'dataset', 'mlebench', 'mlebench_real', 'repo'])
    assert.equal(genesisDefaultDirection(kind), 'max', kind)
  assert.equal(genesisDefaultDirection(undefined), 'max')   // catch-all for an unpicked/unknown kind
})

test('every advertised kind is one the readiness gate understands', () => {
  // The picker offers exactly these; each must reach a launchable state once its fields are filled.
  assert.ok(GENESIS_TASK_KINDS.includes('dataset') && GENESIS_TASK_KINDS.includes('repo'))
  assert.equal(GENESIS_TASK_KINDS.length, 9)
})
