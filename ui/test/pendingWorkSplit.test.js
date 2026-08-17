import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

// `narration.js` reaches a `.jsx` module, so it loads through vite the way its sibling model tests
// do — a plain import fails with ERR_UNKNOWN_FILE_EXTENSION and says nothing about this model.
const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const withNarration = async (body) => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try { await body(await vite.ssrLoadModule('/src/narration.js')) } finally { await vite.close() }
}

// The shape that produced the false label, taken from `rubertlite-dr-unified-v9` on 2026-08-17:
// three pending nodes, one training on a GPU, one announced and between stages with nothing
// executing, one not begun — while the second card sat idle.
const V9 = {
  live: { nodes: { 4: { id: 4, status: 'pending' }, 5: { id: 5, status: 'pending' },
                   6: { id: 6, status: 'pending' } } },
  log: [{ type: 'node_eval_started', data: { node_id: 4 } },
        { type: 'node_eval_started', data: { node_id: 5 } }],
}

test('the live shape is not called three-in-parallel', async () => {
  await withNarration(({ pendingWork, pendingWorkLabel }) => {
  const label = pendingWorkLabel(V9.live, V9.log)
  assert.ok(!/3 experiments in parallel/.test(label), label)
  assert.match(label, /2 experiment\(s\) evaluating, 1 queued/)
  })
})

test('started and queued are split from the log, not from the node payload', async () => {
  await withNarration(({ pendingWork, pendingWorkLabel }) => {
  const { started, queued } = pendingWork(V9.live, V9.log)
  assert.deepEqual(started.map(n => n.id), [4, 5])
  assert.deepEqual(queued.map(n => n.id), [6])
  })
})

test('a node whose evaluation was never announced is queued, never running', async () => {
  await withNarration(({ pendingWork, pendingWorkLabel }) => {
  const live = { nodes: { 9: { id: 9, status: 'pending' } } }
  assert.match(pendingWorkLabel(live, []), /#9 queued/)
  })
})

test('one announced node still reads as running, byte for byte as before', async () => {
  await withNarration(({ pendingWork, pendingWorkLabel }) => {
  const live = { nodes: { 9: { id: 9, status: 'pending' } } }
  const log = [{ type: 'node_eval_started', data: { node_id: 9 } }]
  assert.equal(pendingWorkLabel(live, log), 'Running experiment #9… (training)')
  })
})

test('no pending node says nothing, so the caller falls through as it always did', async () => {
  await withNarration(({ pendingWork, pendingWorkLabel }) => {
  assert.equal(pendingWorkLabel({ nodes: { 1: { id: 1, status: 'evaluated' } } }, []), '')
  })
})

test('all-queued and all-started each get their own sentence', async () => {
  await withNarration(({ pendingWork, pendingWorkLabel }) => {
  const nodes = { 1: { id: 1, status: 'pending' }, 2: { id: 2, status: 'pending' } }
  assert.match(pendingWorkLabel({ nodes }, []), /2 experiments queued/)
  const log = [{ type: 'node_eval_started', data: { node_id: 1 } },
               { type: 'node_eval_started', data: { node_id: 2 } }]
  assert.match(pendingWorkLabel({ nodes }, log), /2 experiments evaluating/)
  })
})
