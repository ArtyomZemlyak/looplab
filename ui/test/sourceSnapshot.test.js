import assert from 'node:assert/strict'
import test from 'node:test'
import { dirtyInputRows, runtimeParamsView, workspaceRows } from '../src/sourceSnapshot.js'

// doc 77 §1: a copy without .git reads as "commit unknown" with its digest, never as clean.
test('workspace fingerprints become captured commits or explicit unknowns', () => {
  const rows = workspaceRows({ 'editable:.': 'hash:bcd7f3f603da97b3', 'editable:lib': 'git:a92ba534deadbeef',
    'data:train': 'absent' })
  assert.deepEqual(rows.map(r => [r.source, r.kind, r.status]),
    [['.', 'copy', 'unknown'], ['lib', 'git', 'captured'], ['data: train', 'absent', 'unknown']])
  assert.equal(rows[0].digest, 'bcd7f3f603da97b3')
  assert.equal(rows[1].commit, 'a92ba534deadbeef')
  assert.deepEqual(workspaceRows(null), [])
  assert.deepEqual(dirtyInputRows([{ path: 'a.py', status: ' M' }, 'b.py', 3]), [
    { path: 'a.py', status: ' M' }, { path: 'b.py', status: '' }])
})

test('the runtime-params view keeps carrier statuses and comparisons', () => {
  const view = runtimeParamsView({ carriers: [{ path: '.env', status: 'withheld' }, { path: 'experiment.env', status: 'read' }],
    params: [{ key: 'LR', value: '2e-4', source: 'experiment.env' }], comparisons: [{ param: 'lr', match: 'same' }] })
  assert.equal(view.carriers.length, 2)
  assert.equal(view.params[0].key, 'LR')
  assert.equal(view.comparisons[0].match, 'same')
  assert.equal(runtimeParamsView(null), null)
})

test('the attempt row of /code is picked by node and generation, current first', async () => {
  const { attemptCodeRow, effectiveConfigRows } = await import('../src/sourceSnapshot.js')
  const code = { attempts: [{ node_id: 3, generation: 0, current: false, commit: 'old' },
    { node_id: 3, generation: 1, current: true, commit: 'abc', effective_config: { effective: { lr: { value: 2e-4, source: 'env' }, seed: 42 } } }] }
  assert.equal(attemptCodeRow(code, 3, 1).commit, 'abc')
  assert.equal(attemptCodeRow(code, 3, 0).commit, 'old')
  assert.equal(attemptCodeRow(code, 4, 0), null)
  assert.deepEqual(effectiveConfigRows(code.attempts[1].effective_config),
    [{ key: 'lr', value: '0.0002', source: 'env' }, { key: 'seed', value: '42', source: '' }])
})
