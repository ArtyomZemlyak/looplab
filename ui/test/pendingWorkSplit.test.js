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
  live: { nodes: {
    4: { id: 4, attempt: 2, status: 'pending',
      activity: { schema: 1, status: 'evaluating', generation: 2, evidence: 'node_eval_started' } },
    5: { id: 5, attempt: 0, status: 'pending',
      activity: { schema: 1, status: 'evaluating', generation: 0, evidence: 'node_eval_started' } },
    6: { id: 6, attempt: 0, status: 'pending',
      activity: { schema: 1, status: 'queued', generation: 0, evidence: 'node_created_boundary' } },
  } },
  // Deliberately empty: the one-shot start rows have scrolled out of the bounded timeline window.
  log: [],
}

test('the live shape is not called three-in-parallel', async () => {
  await withNarration(({ pendingWork, pendingWorkLabel }) => {
  const label = pendingWorkLabel(V9.live, V9.log)
  assert.ok(!/3 experiments in parallel/.test(label), label)
  assert.match(label, /2 evaluating, 1 waiting for a slot/)
  })
})

test('started and queued survive after start rows leave the bounded timeline', async () => {
  await withNarration(({ pendingWork, pendingWorkLabel }) => {
  const { started, queued, unknown } = pendingWork(V9.live, V9.log)
  assert.deepEqual(started.map(n => n.id), [4, 5])
  assert.deepEqual(queued.map(n => n.id), [6])
  assert.deepEqual(unknown, [])
  })
})

test('a node whose evaluation was never announced is queued, never running', async () => {
  await withNarration(({ pendingWork, pendingWorkLabel }) => {
  const live = { nodes: { 9: { id: 9, attempt: 0, status: 'pending',
    activity: { schema: 1, status: 'queued', generation: 0 } } } }
  assert.match(pendingWorkLabel(live, []), /#9 waiting for an evaluation slot/)
  })
})

test('one admitted node is explicitly training or evaluating', async () => {
  await withNarration(({ pendingWork, pendingWorkLabel }) => {
  const live = { nodes: { 9: { id: 9, attempt: 0, status: 'pending',
    activity: { schema: 1, status: 'evaluating', generation: 0 } } } }
  assert.equal(pendingWorkLabel(live, []), 'Experiment #9 training / evaluating…')
  })
})

test('no pending node says nothing, so the caller falls through as it always did', async () => {
  await withNarration(({ pendingWork, pendingWorkLabel }) => {
  assert.equal(pendingWorkLabel({ nodes: { 1: { id: 1, status: 'evaluated' } } }, []), '')
  })
})

test('all-queued and all-started each get their own sentence', async () => {
  await withNarration(({ pendingWork, pendingWorkLabel }) => {
  const nodes = { 1: { id: 1, attempt: 0, status: 'pending', activity: { status: 'queued', generation: 0 } },
    2: { id: 2, attempt: 0, status: 'pending', activity: { status: 'queued', generation: 0 } } }
  assert.match(pendingWorkLabel({ nodes }, []), /2 waiting for a slot/)
  nodes[1].activity.status = 'evaluating'
  nodes[2].activity.status = 'evaluating'
  assert.match(pendingWorkLabel({ nodes }, []), /2 evaluating/)
  })
})

test('old-server log fallback is generation scoped after a reset', async () => {
  await withNarration(({ pendingWork }) => {
  const live = { nodes: { 9: { id: 9, attempt: 2, status: 'pending' } } }
  const stale = [{ type: 'node_eval_started', data: { node_id: 9, generation: 1 } }]
  assert.deepEqual(pendingWork(live, stale).started, [])
  assert.deepEqual(pendingWork(live, stale).unknown.map(n => n.id), [9])
  const current = [{ type: 'node_eval_started', data: { node_id: 9, generation: 2 } }]
  assert.deepEqual(pendingWork(live, current).started.map(n => n.id), [9])
  })
})

test('legacy pending without a receipt is called unknown, not guessed queued', async () => {
  await withNarration(({ pendingWorkLabel }) => {
  const live = { nodes: { 3: { id: 3, attempt: 0, status: 'pending' } } }
  assert.match(pendingWorkLabel(live, []), /evaluation start unknown/)
  })
})

test('a dead attempt\'s still-open stage beacon never labels the NEW lifecycle', async () => {
  await withNarration(({ pendingWorkLabel }) => {
    // Engine SIGKILLed mid-`train` of attempt 0 (the closing beacon never landed; `_stage_cursor`'s
    // finally died with the process), run resumed, node reset to attempt 1 and evaluating again —
    // the gen-0 beacon is still open in the bounded window. The strip must refuse it exactly as the
    // node card does (`evalStageFor`'s generation fence); a bare `stages.get(id)` read let the
    // abandoned lifecycle's cursor label the new one, and the two surfaces contradicted each other.
    const cursor = generation => [{ type: 'phase_progress', ts: 100, data: {
      stage: 'eval', phase: 'stage', status: 'started', node_id: 5, generation,
      name: 'train', index: 1, total: 3, role: 'work' } }]
    const live = { nodes: { 5: { id: 5, attempt: 1, status: 'pending',
      activity: { schema: 1, status: 'evaluating', generation: 1, evidence: 'node_eval_started' } } } }
    const stale = pendingWorkLabel(live, cursor(0))
    assert.match(stale, /training \/ evaluating/, stale)
    assert.ok(!/train/.test(stale.replace('training / evaluating', '')), stale)
    // The negative control: a beacon of the CURRENT generation still names the step — the fence
    // refuses stale, not everything.
    assert.match(pendingWorkLabel(live, cursor(1)), /Stage train · 2 of 3/)
  })
})

test('the status clock reads the SAME build filter as the label', async () => {
  await withNarration(({ liveStatusStartedAt }) => {
    // Build `implement` opened at 1000; an eval stage beacon opened at 9000. The label leads with
    // the build (Dock.agentStatus filters livePhase to 'build'), so its age must be the build's —
    // unfiltered, the clock latched the newest open record and reset to the eval's stage boundary
    // under an unchanged build label, describing a different moment than the words beside it.
    const live = { engine_running: true, nodes: {} }
    const log = [
      { type: 'phase_progress', ts: 1000, data: {
        stage: 'build', phase: 'implement', status: 'started', node_id: 9 } },
      { type: 'phase_progress', ts: 9000, data: {
        stage: 'eval', phase: 'stage', status: 'started', node_id: 5, generation: 0,
        name: 'train', index: 1, total: 3, role: 'work' } },
    ]
    assert.equal(liveStatusStartedAt(live, log), 1000)
    // With NO build phase open, the eval-led label gets the eval lane's own clock: the projection's
    // durable eval start, not the stage beacon's boundary.
    const evalLive = { engine_running: true, nodes: { 5: { id: 5, attempt: 0, status: 'pending',
      activity: { schema: 1, status: 'evaluating', generation: 0, started_at: 500 } } } }
    assert.equal(liveStatusStartedAt(evalLive, [log[1]]), 500)
  })
})
