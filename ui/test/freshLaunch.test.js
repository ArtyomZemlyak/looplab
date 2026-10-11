// A run this tab just started is "starting", not "not found" (`freshLaunch.js`, doc 74).
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  FRESH_LAUNCH_WINDOW_MS, clearFreshLaunch, isFreshLaunch, markFreshLaunch,
} from '../src/freshLaunch.js'

const memory = () => {
  const values = new Map()
  return {
    getItem: key => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

test('only the run this tab started, and only inside the window, counts as starting', () => {
  const store = memory()
  markFreshLaunch('demo', 1_000, store)
  assert.equal(isFreshLaunch('demo', 1_000, store), true)
  assert.equal(isFreshLaunch('demo', 1_000 + FRESH_LAUNCH_WINDOW_MS, store), true)
  assert.equal(isFreshLaunch('demo', 1_001 + FRESH_LAUNCH_WINDOW_MS, store), false, 'the window closes')
  assert.equal(isFreshLaunch('other', 1_000, store), false, 'a mistyped id is still "not found" at once')
  assert.equal(isFreshLaunch('demo', 999, store), false, 'a clock that went backwards proves nothing')
})

test('clearing is scoped to its run, and broken or blocked storage never throws', () => {
  const store = memory()
  markFreshLaunch('demo', 1_000, store)
  clearFreshLaunch('other', store)
  assert.equal(isFreshLaunch('demo', 1_000, store), true)
  clearFreshLaunch('demo', store)
  assert.equal(isFreshLaunch('demo', 1_000, store), false)
  store.setItem('looplab.freshLaunch', '{not json')
  assert.equal(isFreshLaunch('demo', 1_000, store), false)
  const blocked = { getItem() { throw new Error('denied') }, setItem() { throw new Error('denied') },
    removeItem() { throw new Error('denied') } }
  assert.doesNotThrow(() => markFreshLaunch('demo', 1_000, blocked))
  assert.equal(isFreshLaunch('demo', 1_000, blocked), false)
  assert.doesNotThrow(() => clearFreshLaunch('demo', blocked))
  assert.equal(isFreshLaunch('demo', 1_000, null), false)
})

test('two runs started from one tab are both starting; the older one is not overwritten', () => {
  const store = memory()
  markFreshLaunch('first', 1_000, store)
  markFreshLaunch('second', 2_000, store)
  assert.equal(isFreshLaunch('first', 2_000, store), true, 'a second start used to overwrite the first')
  assert.equal(isFreshLaunch('second', 2_000, store), true)
  clearFreshLaunch('second', store)
  assert.equal(isFreshLaunch('first', 2_000, store), true, 'clearing one run keeps the other')
  markFreshLaunch('third', 1_000 + FRESH_LAUNCH_WINDOW_MS + 1, store)
  assert.deepEqual(Object.keys(JSON.parse(store.getItem('looplab.freshLaunch'))), ['third'],
    'expired entries are dropped on write')
})

test('the first not_found of a fresh launch already shows the starting screen', async () => {
  // Code review: the latch is set by an effect AFTER the render that first sees `not_found`, so that
  // render must read the marker itself or it paints "Run not found" for one frame.
  const { freshLaunchShown } = await import('../src/freshLaunch.js')
  const base = { reviewMode: false, live: null, runStatus: 'not_found', runId: 'r', latchedFor: '' }
  assert.equal(freshLaunchShown({ ...base, fresh: true }), true, 'first sight, no latch yet')
  assert.equal(freshLaunchShown({ ...base, fresh: false }), false, 'not ours: "not found" is true')
  assert.equal(freshLaunchShown({ ...base, runStatus: 'loading', latchedFor: 'r', fresh: false }), true,
    'the latch holds across a retry')
  assert.equal(freshLaunchShown({ ...base, runStatus: 'loading', fresh: true }), false,
    'loading alone, unlatched, is the ordinary opening screen')
  assert.equal(freshLaunchShown({ ...base, live: {}, fresh: true }), false)
  assert.equal(freshLaunchShown({ ...base, reviewMode: true, fresh: true }), false)
})
