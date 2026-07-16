import test from 'node:test'
import assert from 'node:assert/strict'

import {
  emptyRunRouteState, encodeRunRouteState, hashWithRunRouteState, hrefWithRunRouteState,
  parseRunRouteState, reconcileRunRouteStateUpdate, reviewRouteStateForScope, routeHashPath,
  runRouteStateHasTarget,
  sanitizeRunRouteState, splitRouteHash,
} from '../src/runRouteState.js'

const GEN = 'a'.repeat(64)

test('timeline filter preserves live spaces (typing) but writes a trimmed canonical URL', () => {
  // Regression: the Dock filter <input> binds directly to this state, so trimming per keystroke
  // dropped the trailing space of a multi-word filter ("node failed" collapsed to "nodefailed").
  const live = sanitizeRunRouteState({ timelineFilter: 'node failed ' })
  assert.equal(live.timelineFilter, 'node failed ')          // interior + trailing spaces kept live
  const url = encodeRunRouteState({ timelineFilter: '  node failed  ' })
  assert.match(url, /(^|&)q=node\+failed(&|$)/)               // shareable URL is canonical (trimmed)
})

test('owner diagnostic state round-trips canonically inside the fragment', () => {
  const state = {
    ...emptyRunRouteState(), generation: GEN, view: 'report', nodeId: 14, inspectTab: 'Code',
    panel: 'trust', directionFilter: 'robust & small/#1', sequence: 29,
    timelineFilter: ' timeout / β? ', timelineKinds: ['trust', 'eval'],
  }
  const hash = hashWithRunRouteState('#/run/demo', state)
  assert.equal(hash,
    `#/run/demo?gen=${GEN}&view=report&node=14&tab=code&panel=trust&focus=robust+%26+small%2F%231&seq=29&q=timeout+%2F+%CE%B2%3F&kinds=eval%2Ctrust`)
  const parsed = parseRunRouteState(hash)
  assert.deepEqual(parsed.issues, [])
  assert.deepEqual(parsed.state, { ...state, timelineFilter: 'timeout / β?', timelineKinds: ['eval', 'trust'] })
  assert.equal(hashWithRunRouteState(hash, parsed.state), hash)
})

test('owner Concepts view survives copy, reload, and history route round-trips', () => {
  const current = { ...emptyRunRouteState(), generation: GEN, view: 'concepts' }
  const hash = hashWithRunRouteState('#/run/demo', current)
  assert.equal(hash, `#/run/demo?gen=${GEN}&view=concepts`)
  const parsed = parseRunRouteState(hash)
  assert.deepEqual(parsed.issues, [])
  assert.deepEqual(parsed.state, current)
  assert.equal(hashWithRunRouteState(hash, parsed.state), hash)
})

test('route parsing separates encoded run ids from diagnostic parameters', () => {
  const hash = '#/run/a%20b%2F%25%3F%23?node=2'
  assert.deepEqual(splitRouteHash(hash), { path: '/run/a%20b%2F%25%3F%23', query: 'node=2' })
  assert.equal(routeHashPath(hash), '#/run/a%20b%2F%25%3F%23')
})

test('invalid, duplicate, unsafe, and dependent fields fail closed and canonicalize', () => {
  const hash = '#/run/demo?gen=BAD&node=-1&node=4&tab=trace&panel=shell&seq=1e3&q=%00oops&kinds=eval,evil,eval&future=x'
  const parsed = parseRunRouteState(hash)
  assert.deepEqual(parsed.state, emptyRunRouteState())
  assert.ok(parsed.issues.length >= 8)
  assert.equal(hashWithRunRouteState(hash, parsed.state), '#/run/demo')

  const unsafe = parseRunRouteState(`#/run/demo?gen=${GEN}&node=9007199254740992&seq=01`)
  assert.equal(unsafe.state.nodeId, null)
  assert.equal(unsafe.state.sequence, null)
})

test('historical sequence requires an exact generation fence', () => {
  const missing = parseRunRouteState('#/run/demo?seq=29')
  assert.equal(missing.state.sequence, null)
  assert.match(missing.issues.join(' '), /generation fence/)
  const exact = parseRunRouteState(`#/run/demo?gen=${GEN}&seq=29`)
  assert.equal(exact.state.sequence, 29)
})

test('node, panel, view, and filters also fail closed without a generation fence', () => {
  for (const query of ['node=4', 'panel=trust', 'view=report', 'view=concepts', 'focus=quality', 'q=timeout', 'kinds=eval']) {
    const parsed = parseRunRouteState(`#/run/demo?${query}`)
    assert.deepEqual(parsed.state, emptyRunRouteState(), query)
    assert.match(parsed.issues.join(' '), /generation fence/, query)
  }
})

test('review links preserve safe context but strip history and raw timeline filters', () => {
  const hash = `#/${'rv_token'}?gen=${GEN}&view=report&node=4&tab=code&panel=compare&focus=quality&seq=29&q=secret&kinds=eval`
  const parsed = parseRunRouteState(hash, { reviewMode: true })
  assert.deepEqual(parsed.state, {
    ...emptyRunRouteState(), generation: GEN, view: 'report', nodeId: 4, inspectTab: 'Code',
    panel: 'compare', directionFilter: 'quality',
  })
  assert.match(parsed.issues.join(' '), /unavailable in review links/)
  assert.equal(hashWithRunRouteState(hash, parsed.state, { reviewMode: true }),
    `#/rv_token?gen=${GEN}&view=report&node=4&tab=code&panel=compare&focus=quality`)
})

test('review links fail closed on the owner-only Concepts view', () => {
  const hash = `#/rv_token?gen=${GEN}&view=concepts`
  const parsed = parseRunRouteState(hash, { reviewMode: true })
  assert.equal(parsed.state.view, 'dag')
  assert.equal(parsed.state.generation, GEN)
  assert.match(parsed.issues.join(' '), /Concept view is unavailable in review links/)
  assert.equal(hashWithRunRouteState(hash, parsed.state,
    { reviewMode: true, forceGeneration: true }), `#/rv_token?gen=${GEN}`)
})

test('tab depends on a node and defaults are omitted', () => {
  const parsed = parseRunRouteState(`#/run/demo?gen=${GEN}&tab=code&view=dag`)
  assert.equal(parsed.state.inspectTab, 'Overview')
  assert.match(parsed.issues.join(' '), /without a node/)
  assert.equal(encodeRunRouteState({ ...emptyRunRouteState(), generation: GEN }), '')
  assert.equal(encodeRunRouteState({ ...emptyRunRouteState(), generation: GEN }, { forceGeneration: true }), `gen=${GEN}`)
})

test('comment deep links round-trip only with an exact node attempt and the Comments tab', () => {
  const id = `cmt_${'1'.repeat(32)}`
  const hash = `#/run/demo?gen=${GEN}&node=7&attempt=2&tab=comments&comment=${id}`
  const parsed = parseRunRouteState(hash)
  assert.deepEqual(parsed.issues, [])
  assert.equal(parsed.state.nodeId, 7)
  assert.equal(parsed.state.nodeGeneration, 2)
  assert.equal(parsed.state.inspectTab, 'Comments')
  assert.equal(parsed.state.commentId, id)
  assert.equal(hashWithRunRouteState(hash, parsed.state), hash)

  for (const invalid of [
    `#/run/demo?gen=${GEN}&comment=${id}`,
    `#/run/demo?gen=${GEN}&attempt=2&tab=comments&comment=${id}`,
    `#/run/demo?gen=${GEN}&node=7&tab=comments&comment=${id}`,
    `#/run/demo?gen=${GEN}&node=7&attempt=2&tab=trust&comment=${id}`,
    `#/run/demo?gen=${GEN}&node=7&attempt=2&tab=comments&comment=bad`,
  ]) {
    const rejected = parseRunRouteState(invalid)
    assert.equal(rejected.state.commentId, null)
    assert.match(rejected.issues.join(' '), /comment|attempt/i)
  }
  const danglingAttempt = parseRunRouteState(`#/run/demo?gen=${GEN}&node=7&attempt=2`)
  assert.equal(danglingAttempt.state.nodeGeneration, null)
  assert.match(danglingAttempt.issues.join(' '), /attempt/i)
  assert.equal(hashWithRunRouteState(`#/run/demo?gen=${GEN}&node=7&attempt=2`,
    danglingAttempt.state), `#/run/demo?gen=${GEN}&node=7`)
})

test('an explicit generation-only exact view survives canonical hydration', () => {
  const source = `#/run/demo?gen=${GEN}`
  const parsed = parseRunRouteState(source)
  assert.equal(parsed.state.generation, GEN)
  assert.equal(hashWithRunRouteState(source, parsed.state,
    { forceGeneration: !!parsed.state.generation }), source)
})

test('a semantic no-op preserves an explicit generation-only fence', () => {
  const current = { ...emptyRunRouteState(), generation: GEN }
  const unchanged = reconcileRunRouteStateUpdate(current, { ...current, view: 'dag' }, {
    generation: GEN,
  })
  assert.equal(unchanged, current)
  assert.equal(encodeRunRouteState(unchanged, { forceGeneration: true }), `gen=${GEN}`)

  const report = { ...current, view: 'report' }
  const returnedToDefault = reconcileRunRouteStateUpdate(report, {
    ...report, view: 'dag',
  }, { generation: GEN })
  assert.equal(returnedToDefault.generation, null)
})

test('programmatic sanitizer applies review and allow-list boundaries', () => {
  const state = sanitizeRunRouteState({
    generation: GEN, nodeId: 0, inspectTab: 'Trace', panel: 'events', sequence: 3,
    timelineFilter: 'needle', timelineKinds: ['eval', 'evil'], view: 'concepts',
  }, { reviewMode: true })
  assert.equal(state.view, 'dag')
  assert.equal(state.nodeId, 0)
  assert.equal(state.inspectTab, 'Trace')
  assert.equal(state.panel, 'events')
  assert.equal(state.sequence, null)
  assert.equal(state.timelineFilter, '')
  assert.deepEqual(state.timelineKinds, [])
})

test('review scope sanitizer shares only evidence the minted capability permits', () => {
  const source = {
    ...emptyRunRouteState(), generation: GEN, nodeId: 4, inspectTab: 'Code', panel: 'compare',
    sequence: 29, timelineFilter: 'private needle', timelineKinds: ['eval'],
  }
  const summary = reviewRouteStateForScope(source, { evidence: false })
  assert.equal(summary.inspectTab, 'Overview')
  assert.equal(summary.panel, null)
  assert.equal(summary.sequence, null)
  assert.equal(summary.timelineFilter, '')
  const evidence = reviewRouteStateForScope(source, { evidence: true })
  assert.equal(evidence.inspectTab, 'Code')
  assert.equal(evidence.panel, 'compare')
})

test('a sequence counts as route intent before its generation is attached', () => {
  assert.equal(runRouteStateHasTarget({ ...emptyRunRouteState(), sequence: 29 }), true)
})

test('href writer preserves the HTTP path/query and keeps all diagnostic state in hash', () => {
  const href = hrefWithRunRouteState({
    pathname: '/user/a/proxy/8765/review', search: '?theme=paper', hash: '#/rv_token',
  }, { ...emptyRunRouteState(), generation: GEN, nodeId: 4 }, { reviewMode: true })
  assert.equal(href, `/user/a/proxy/8765/review?theme=paper#/rv_token?gen=${GEN}&node=4`)
})
