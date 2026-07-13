import test from 'node:test'
import assert from 'node:assert/strict'

import { queuedGenerationControls } from '../src/queue.js'

test('a completed generation zero confirm does not hide a generation one request', () => {
  const queued = queuedGenerationControls({
    confirm_requests: [0],
    confirmed_forced: [0],
    confirm_request_generations: [{ node_id: 0, generation: 1 }],
    confirmed_forced_generations: [{ node_id: 0, generation: 0 }],
  })
  assert.deepEqual(queued.confirms, [{ node_id: 0, generation: 1 }])
})

test('confirm and ablate gates close only for the exact lifecycle generation', () => {
  const base = {
    confirm_requests: [2],
    confirm_request_generations: [{ node_id: 2, generation: 3 }],
    confirmed_forced_generations: [{ node_id: 2, generation: 2 }],
    ablate_requests: [2],
    ablate_request_generations: [{ node_id: 2, generation: 3 }],
    ablations: [{ parent_id: 2, generation: 2 }],
  }
  assert.deepEqual(queuedGenerationControls(base), {
    confirms: [{ node_id: 2, generation: 3 }],
    ablates: [{ node_id: 2, generation: 3 }],
  })

  assert.deepEqual(queuedGenerationControls({
    ...base,
    confirmed_forced_generations: [
      { node_id: 2, generation: 2 }, { node_id: 2, generation: 3 },
    ],
    ablations: [
      { parent_id: 2, generation: 2 }, { parent_id: 2, generation: 3, skipped: 'repo_or_eval_spec' },
    ],
  }), { confirms: [], ablates: [] })
})

test('duplicate clicks are one logical pair and legacy id-only requests remain visible', () => {
  assert.deepEqual(queuedGenerationControls({
    confirm_requests: [4, 4, 7, 7],
    confirm_request_generations: [
      { node_id: 4, generation: 1 }, { node_id: 4, generation: 1 },
    ],
    ablate_requests: [5, 5, 8, 8],
    ablate_request_generations: [
      { node_id: 5, generation: 2 }, { node_id: 5, generation: 2 },
    ],
  }), {
    confirms: [{ node_id: 4, generation: 1 }, { node_id: 7, generation: null }],
    ablates: [{ node_id: 5, generation: 2 }, { node_id: 8, generation: null }],
  })
})
