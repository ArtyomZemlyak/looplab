// Earlier attempts of a repaired node — the trace an operator opens the Inspector to read.
//
// The routes have taken `?attempt=` all along and `usePagedNodeTrace` already fenced on it; the
// Inspector simply always sent the CURRENT generation and rejected any response carrying an older
// one as stale. So on a node that was repaired five times, the five traces that actually crashed
// were unreachable and only the last one — often the abandon — could be read.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  attemptReadRequired, nodeAttemptOptions, traceForAttempt,
} from '../src/traceProjection.js'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src')

test('a repaired node offers every generation, newest labelled current', () => {
  assert.deepEqual(nodeAttemptOptions(2), [
    { attempt: 0, label: 'attempt 0', current: false },
    { attempt: 1, label: 'attempt 1', current: false },
    { attempt: 2, label: 'attempt 2 (current)', current: true },
  ])
})

test('a node that was never repaired offers nothing to choose between', () => {
  // The picker is gated on `length > 1`: an always-present control implies history that is not there.
  assert.equal(nodeAttemptOptions(0).length, 1)
  assert.equal(nodeAttemptOptions(null).length, 1)
  assert.equal(nodeAttemptOptions(-3).length, 1, 'a junk attempt settles to the first generation')
  assert.equal(nodeAttemptOptions(1.5).length, 1)
})

test('a historical attempt never falls back to the detail payload', () => {
  // The node-DETAIL payload always describes the CURRENT attempt. Rendering it under an older
  // attempt's label would show the newest trace as if it were the crash — worse than showing nothing.
  const detail = { attempt: 3, nodes: ['current'] }
  const paged = { attempt: 1, nodes: ['older'] }

  assert.equal(traceForAttempt({ selected: 1, current: 3, paged: null, detail }), null)
  assert.equal(traceForAttempt({ selected: 1, current: 3, paged, detail }), paged)
  // …while the current attempt keeps the existing behaviour exactly: paged first, detail as fallback.
  assert.equal(traceForAttempt({ selected: 3, current: 3, paged: null, detail }), detail)
  assert.equal(traceForAttempt({ selected: 3, current: 3, paged, detail }), paged)
})

test('a historical attempt must be fetched; the current one only when paging further', () => {
  assert.equal(attemptReadRequired({ selected: 1, current: 3, canPageFurther: false }), true)
  assert.equal(attemptReadRequired({ selected: 3, current: 3, canPageFurther: false }), false)
  assert.equal(attemptReadRequired({ selected: 3, current: 3, canPageFurther: true }), true)
})

test('the Inspector sends the SELECTED attempt and follows the node forward by default', () => {
  const source = readFileSync(join(SRC, 'Inspector.jsx'), 'utf8')
  assert.ok(source.includes('attempt: selectedAttempt'),
    'the paged read must send the selected attempt, not the node’s current one')
  assert.ok(!source.includes('attempt: nodeGeneration,'), 'the pinned-to-current read came back')
  // `null` means "follow the node": a live node that repairs mid-read must not strand the operator
  // on the generation that happened to be current when they opened the tab.
  assert.ok(source.includes('viewAttempt == null ? (nodeGeneration ?? 0) : viewAttempt'))
})

test('the destructive clear stays bound to the CURRENT generation, never the browsed one', () => {
  // Browsing history must not be able to erase it. `useTraceClear` takes `nodeGeneration`.
  const source = readFileSync(join(SRC, 'Inspector.jsx'), 'utf8')
  const clearCall = source.slice(source.indexOf('} = useTraceClear({'))
  const args = clearCall.slice(0, clearCall.indexOf('})'))
  assert.ok(args.includes('nodeGeneration,'), 'the clear must target the current generation')
  assert.ok(!args.includes('selectedAttempt'), 'a historical selection must never reach the clear')
})
