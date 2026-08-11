// The Operations listing model. The property that matters is the one the panel exists for: an
// operation with NO node is run-level work and must survive every step — filter, grouping, labels —
// because that is the Researcher, and it was unreachable from the product until this surface.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  AGENTIC_OPERATIONS, groupOperations, isRunLevel, operationLabel, operationRows, operationsNotice,
} from '../src/operationsModel.js'

const op = (name, extra = {}) => ({
  name, kind: 'operation', trace_id: `t-${name}-${extra.i ?? 0}`, span_id: `s-${name}-${extra.i ?? 0}`,
  start: extra.start ?? 100, duration_s: 1, status: 'OK', node_id: null,
  spans: 10, generations: 3, tools: 4, tokens: { prompt: 1, completion: 1, total: 2, context: 1 },
  errors: 0, trace_roots: 1, ...extra,
})

test('a Researcher operation is run-level, kept by the default filter, and grouped first', () => {
  const payload = { operations: [op('propose', { i: 1 }), op('evaluate', { i: 2, node_id: 5 })] }
  const { rows } = operationRows(payload)
  const propose = rows.find(r => r.name === 'propose')
  assert.ok(propose, 'the Researcher must survive the default filter')
  assert.equal(propose.runLevel, true)
  assert.equal(propose.label, 'Researcher · propose')
  const { runLevel, nodes } = groupOperations(rows)
  assert.deepEqual(runLevel.map(r => r.name), ['propose'])
  assert.deepEqual(nodes.map(n => n.node_id), ['5'])
})

test('node_id 0 is a NODE, not run-level — falsy ids must not read as absent', () => {
  // The defect this pins: `node_id || 'run-level'` puts node 0's whole trace in the run-level group.
  const { rows } = operationRows({ operations: [op('evaluate', { node_id: 0 })] })
  assert.equal(rows[0].runLevel, false)
  assert.equal(isRunLevel({ node_id: 0 }), false)
  const { runLevel, nodes } = groupOperations(rows)
  assert.equal(runLevel.length, 0)
  assert.deepEqual(nodes.map(n => n.node_id), ['0'])
})

test('the bookkeeping filter hides only non-agentic work, and states how much it hid', () => {
  const payload = { operations: [
    op('propose', { i: 1 }), op('lessons_refresh', { i: 2 }), op('lessons_distill', { i: 3 }),
  ] }
  const filtered = operationRows(payload, { agenticOnly: true })
  assert.deepEqual(filtered.rows.map(r => r.name), ['propose'])
  assert.equal(filtered.hiddenByFilter, 2)
  assert.equal(filtered.total, 3)
  assert.match(operationsNotice(payload, filtered.hiddenByFilter), /2 bookkeeping operations hidden/)

  const unfiltered = operationRows(payload, { agenticOnly: false })
  assert.equal(unfiltered.rows.length, 3)
  assert.equal(unfiltered.hiddenByFilter, 0)
  assert.ok(AGENTIC_OPERATIONS.has('propose') && !AGENTIC_OPERATIONS.has('lessons_refresh'))
})

test('an unknown operation name renders verbatim instead of vanishing', () => {
  // A new engine span must appear the day it ships, not the day someone updates the label table.
  assert.equal(operationLabel('brand_new_op'), 'brand_new_op')
  const { rows } = operationRows({ operations: [op('brand_new_op')] }, { agenticOnly: false })
  assert.equal(rows.length, 1)
  assert.equal(rows[0].label, 'brand_new_op')
})

test('an operation with no sub-spans is not openable — the click would land on an empty tree', () => {
  const { rows } = operationRows({ operations: [
    op('propose', { i: 1, spans: 1 }), op('propose', { i: 2, spans: 40 }),
  ] })
  assert.deepEqual(rows.map(r => r.openable), [false, true])
})

test('the omission receipt is silent when nothing was dropped, and explicit when it was', () => {
  assert.equal(operationsNotice({ operations: [], projection: { omitted_operations: 0 } }, 0), '')
  assert.match(
    operationsNotice({ operations: [], projection: { omitted_operations: 7 } }, 0),
    /7 older operations not listed/)
  assert.match(operationsNotice({ projection: { unavailable: true } }, 0), /unavailable/)
})

test('rows come back newest-first and a node filter narrows without reordering', () => {
  const payload = { operations: [
    op('propose', { i: 1, start: 300 }), op('evaluate', { i: 2, start: 200, node_id: 2 }),
    op('evaluate', { i: 3, start: 100, node_id: 2 }),
  ] }
  const scoped = operationRows(payload, { agenticOnly: false, nodeId: 2 })
  assert.deepEqual(scoped.rows.map(r => r.start), [200, 100])
  assert.equal(scoped.total, 3)
})
