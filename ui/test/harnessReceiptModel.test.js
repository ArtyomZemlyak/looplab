import test from 'node:test'
import assert from 'node:assert/strict'
import { COMMAND_STATUSES, COMMAND_PENDING } from '../src/commandModel.js'
import { RECEIPT_CONTROL_EVENTS, validReceipt } from '../src/harnessReceiptModel.js'

const generation = 'a'.repeat(64)
const receipt = { version: 1, generation, terminal: true,
  command: { id: `cmd_${'b'.repeat(32)}`, event_type: 'inject_node', status: 'succeeded',
    event_seq: 4, error_code: '', retryable: false } }

test('saved receipts accept every control/status partition, null sequence and extra fields', () => {
  for (const event_type of RECEIPT_CONTROL_EVENTS) {
    for (const status of COMMAND_STATUSES) {
      const value = { ...receipt, terminal: !COMMAND_PENDING.has(status), future: 'compatible',
        command: { ...receipt.command, event_type, status, event_seq: null, future: true } }
      assert.equal(validReceipt(value, generation, receipt.command.id), true, `${event_type}/${status}`)
      assert.equal(validReceipt({ ...value, terminal: !value.terminal }, generation), false)
    }
  }
})

test('incomplete or inconsistent HTTP 200 receipts grant no saved verdict', () => {
  const badRows = [
    { id: undefined }, { id: 'cmd_x' }, { status: undefined }, { status: 'completed' },
    { event_type: undefined }, { event_type: '' }, { event_type: 'node_evaluated' },
    { event_type: 'future_control' }, { event_type: 1 },
    { event_seq: undefined }, { event_seq: -1 }, { event_seq: true }, { event_seq: '4' },
    { event_seq: 0.5 }, { event_seq: Number.MAX_SAFE_INTEGER + 1 },
    { error_code: undefined }, { error_code: null }, { error_code: 'x'.repeat(257) },
    { retryable: undefined }, { retryable: 1 }, { retryable: 'false' },
  ]
  for (const patch of badRows) {
    assert.equal(validReceipt({ ...receipt, command: { ...receipt.command, ...patch } }, generation),
      false, JSON.stringify(patch))
  }
  for (const patch of [{ version: undefined }, { version: true }, { version: 2 },
    { terminal: undefined }, { terminal: 1 }, { generation: undefined }, { generation: 'c'.repeat(64) },
    { command: null }, { command: [] }]) {
    assert.equal(validReceipt({ ...receipt, ...patch }, generation), false)
  }
  for (const value of [null, undefined, [], {}, 'receipt']) assert.equal(validReceipt(value, generation), false)
})

test('the expected generation and requested command ID must be bound before showing a receipt', () => {
  for (const expected of [undefined, '', 'not-a-generation', 'A'.repeat(64)]) {
    assert.equal(validReceipt({ ...receipt, generation: expected }, expected), false)
  }
  assert.equal(validReceipt(receipt, generation, `cmd_${'c'.repeat(32)}`), false)
  assert.equal(validReceipt(receipt, generation, receipt.command.id), true)
})
