// A server nine days behind the tree answers 200 with a smaller truth. The operator must be told.
import assert from 'node:assert/strict'
import test from 'node:test'
import { serverCodeNotice } from '../src/serverCode.js'

const fresh = { stale: false, changed_count: 0, changed: [], changed_truncated: false }

test('a current server draws no notice at all', () => {
  assert.equal(serverCodeNotice({ server_code: fresh }), null)
})

test('a server older than the tree says so, with the exact count', () => {
  const notice = serverCodeNotice({ server_code: {
    stale: true, changed_count: 12, changed: ['events/card_ledger.py'], changed_truncated: false } })
  assert.equal(notice.text, 'server code 12 files behind')
  assert.match(notice.detail, /Restart the UI server/)
  assert.match(notice.detail, /events\/card_ledger\.py/)
})

test('one file is not "1 files"', () => {
  const notice = serverCodeNotice({ server_code: {
    stale: true, changed_count: 1, changed: ['a.py'], changed_truncated: false } })
  assert.equal(notice.text, 'server code 1 file behind')
})

test('a clipped sample is marked, so the list is never read as the whole set', () => {
  const notice = serverCodeNotice({ server_code: {
    stale: true, changed_count: 40, changed: ['a.py', 'b.py'], changed_truncated: true } })
  assert.match(notice.detail, /a\.py, b\.py, …/)
})

test('a server too old to send the field is not reported as stale', () => {
  // The absence of the receipt is not evidence of freshness OR of staleness, and inventing either
  // would put a red strip on every older deployment — or hide a real one behind a missing key.
  assert.equal(serverCodeNotice({}), null)
  assert.equal(serverCodeNotice({ server_code: null }), null)
  assert.equal(serverCodeNotice(null), null)
})

test('a non-boolean stale value is not truthiness', () => {
  // `stale: "false"` and `stale: 0` both arrive from a hand-built payload; only `true` is stale.
  assert.equal(serverCodeNotice({ server_code: { ...fresh, stale: 'false' } }), null)
  assert.equal(serverCodeNotice({ server_code: { ...fresh, stale: 1 } }), null)
})
