import test from 'node:test'
import assert from 'node:assert/strict'

import { buildingGenerations, buildingMarkers, withBuilding } from '../src/buildingModel.js'

const baseNodes = { 5: { id: 5, status: 'evaluated' } }

test('renders EVERY concurrent build from the buildings collection (parallel_build>1)', () => {
  const state = {
    nodes: { ...baseNodes },
    building: { node_id: 2, operator: 'draft', parent_ids: [] },   // singular = last-appended
    buildings: {
      1: { node_id: 1, operator: 'improve', parent_ids: [5] },
      2: { node_id: 2, operator: 'draft', parent_ids: [] },
    },
  }
  const out = withBuilding(state)
  assert.equal(out.nodes[1].status, 'building')
  assert.equal(out.nodes[1].building, true)
  assert.equal(out.nodes[1].operator, 'improve')
  assert.deepEqual(out.nodes[1].parent_ids, [5])
  assert.equal(out.nodes[2].status, 'building')          // BOTH builds spliced, not just the singular
  assert.equal(out.nodes[5].status, 'evaluated')         // the real node is untouched
})

test('back-compat: falls back to the singular marker when no buildings collection', () => {
  const state = { nodes: { ...baseNodes }, building: { node_id: 3, operator: 'draft', parent_ids: [] } }
  const out = withBuilding(state)
  assert.equal(out.nodes[3].status, 'building')
  assert.equal(out.nodes[3].building, true)
})

test('empty buildings + no singular marker leaves state untouched (identity)', () => {
  const state = { nodes: { ...baseNodes }, buildings: {} }
  assert.equal(withBuilding(state), state)               // same reference — no needless re-render
})

test('never overwrites a real node that already landed, and dedupes building∈buildings', () => {
  const state = {
    nodes: { 1: { id: 1, status: 'pending' } },          // node 1 already created
    building: { node_id: 1, operator: 'draft', parent_ids: [] },
    buildings: { 1: { node_id: 1, operator: 'draft', parent_ids: [] } },
  }
  const out = withBuilding(state)
  assert.equal(out.nodes[1].status, 'pending')           // real node wins; not clobbered by the ghost
  assert.equal(out, state)                               // nothing changed -> identity
})

test('a finished or engine-dead run splices no phantom cards', () => {
  const mk = extra => ({ nodes: { ...baseNodes },
    buildings: { 1: { node_id: 1, operator: 'draft', parent_ids: [] } }, ...extra })
  assert.equal(withBuilding(mk({ finished: true })).nodes[1], undefined)
  assert.equal(withBuilding(mk({ engine_running: false })).nodes[1], undefined)
})

test('skips a malformed marker (no node_id) without throwing', () => {
  const state = { nodes: { ...baseNodes },
    buildings: { 0: { operator: 'draft' }, 1: { node_id: 1, operator: 'improve', parent_ids: [] } } }
  const out = withBuilding(state)
  assert.equal(out.nodes[1].status, 'building')          // the valid one still renders
  assert.equal(Object.keys(out.nodes).length, 2)         // base node 5 + node 1 only (the bad one skipped)
})

test('canonical marker projection validates ids and generations for live trace polling', () => {
  const state = {
    nodes: { 2: { attempt: 3 } },
    building: { node_id: 99, generation: 9 },
    buildings: {
      good: { node_id: '2' },
      explicit: { node_id: 4, generation: 1 },
      boolean: { node_id: true, generation: 7 },
      badGeneration: { node_id: 5, generation: -1 },
      missing: null,
    },
  }
  assert.equal(buildingMarkers(state).length, 3, 'parallel collection is authoritative and structural junk is removed')
  assert.deepEqual(buildingGenerations(state), { 2: 3, 4: 1 })
  assert.equal(buildingGenerations({}), null)
})
