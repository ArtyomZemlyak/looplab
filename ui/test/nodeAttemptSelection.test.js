// Earlier attempts of a RESET node — the trace an operator opens the Inspector to read.
//
// The routes have taken `?attempt=` all along and `usePagedTrace` already fenced on it; the
// Inspector simply always sent the CURRENT generation and rejected any response carrying an older
// one as stale. So on a node that was reset five times, the five traces that actually crashed
// were unreachable and only the last one — often the abandon — could be read.
//
// This file said "a repaired node" until 2026-08-13, and that was the misreading that left F6 half
// done: an INLINE repair does not open a lifecycle generation, so this picker is not the control
// that reaches one. `runs/rubert-dr-0804` node 1 was repaired 2,345 times inside attempt 0, where
// this picker renders nothing at all — those are reached by the window ANCHOR instead
// (`traceEpisodeModel.js`), and the last two tests here hold the boundary between the two.
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
  // The trace SUBJECT is what carries the attempt into every read the surface makes (the span tree
  // and the conversation both derive their query from it), so this is where the selection has to
  // land. Driven end-to-end in traceSurfaceReuse.test.js, which mounts the tab, picks an earlier
  // attempt and reads the attempt off the request that goes out.
  // The third argument arrived with the window ANCHOR (`?before=`) and does not weaken this pin: the
  // attempt is still what the subject carries, and the anchor is a position INSIDE that attempt.
  assert.ok(source.includes('nodeTraceSubject(n.id, selectedAttempt, viewBefore)'),
    'the read must be scoped to the selected attempt, not the node’s current one')
  assert.ok(!source.includes('nodeTraceSubject(n.id, nodeGeneration)'),
    'the pinned-to-current read came back')
  // `null` means "follow the node": a live node that repairs mid-read must not strand the operator
  // on the generation that happened to be current when they opened the tab.
  assert.ok(source.includes('viewAttempt == null ? (nodeGeneration ?? 0) : viewAttempt'))
})

test('the two pickers answer different questions, and the anchor is reset with the lifecycle', () => {
  // The trap F6 measured: an ATTEMPT is a lifecycle generation, bumped by `node_reset` only, so a
  // node repaired 2,345 times inline has exactly ONE attempt and the picker above cannot reach any
  // of them. The episode anchor is the other axis, and it must be released whenever the axis it
  // lives on moves — a span id is meaningless outside the lifecycle it came from, and sending a
  // stale one asks the server to place a window it will refuse.
  const source = readFileSync(join(SRC, 'Inspector.jsx'), 'utf8')
  assert.ok(source.includes(
    'useEffect(() => { setViewBefore(null) }, [n.id, expectedGeneration, selectedAttempt])'),
    'the window anchor must be released on a node, run-generation or attempt change')
  // And it is the SUBJECT that carries it, so the fence, the poll scope and the React key all see it.
  assert.ok(source.includes('nodeTraceSubject(n.id, selectedAttempt, viewBefore)'))
})

test('a seek may not fall back to the node-detail payload, which is always the newest window', () => {
  // The same rule as a historical attempt, for the same reason: the detail payload describes the
  // CURRENT attempt at the NEWEST window, so rendering it under a chosen episode's label would show
  // the last 512 spans of the node while the caption says "repair 1".
  const detail = { nodes: [1] }
  const paged = { nodes: [2] }
  assert.equal(traceForAttempt({ selected: 3, current: 3, paged: null, detail, anchored: true }),
    null)
  assert.equal(traceForAttempt({ selected: 3, current: 3, paged, detail, anchored: true }), paged)
  assert.equal(attemptReadRequired(
    { selected: 3, current: 3, canPageFurther: false, anchored: true }), true)
})

test('the destructive clear stays bound to the CURRENT generation, never the browsed one', () => {
  // Browsing history must not be able to erase it. `useTraceClear` takes `nodeGeneration`.
  const source = readFileSync(join(SRC, 'Inspector.jsx'), 'utf8')
  const clearCall = source.slice(source.indexOf('} = useTraceClear({'))
  const args = clearCall.slice(0, clearCall.indexOf('})'))
  assert.ok(args.includes('nodeGeneration,'), 'the clear must target the current generation')
  assert.ok(!args.includes('selectedAttempt'), 'a historical selection must never reach the clear')
})
