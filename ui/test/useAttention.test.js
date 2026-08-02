import assert from 'node:assert/strict'
import test from 'node:test'

import { JSDOM } from 'jsdom'
import React from 'react'

import { useAttention } from '../src/useAttention.js'

const GENERATION = 'f'.repeat(64)

const runItem = (hex, overrides = {}) => ({
  id: hex.repeat(64),
  kind: 'finished',
  severity: 'success',
  run_id: `run-${hex}`,
  generation: GENERATION,
  seq: 10,
  created: 1_700_000_000,
  active: false,
  browser: true,
  derived: false,
  node_id: null,
  node_generation: null,
  ...overrides,
})

const permission = (idHex, sessionHex, secret) => ({
  id: idHex.repeat(16),
  session: sessionHex.repeat(16),
  created: 1_700_000_000,
  expires_at: 4_000_000_000,
  action: { command: secret, scope: `private/${secret}` },
  preview: `preview:${secret}`,
})

// A snapshot id identifies the source scan a page came from; the hook preserves archival pages only
// while it is UNCHANGED, so it defaults to one stable value here and every case that means "a
// different scan" passes its own. `stale` and `active_action_count` are part of the same envelope:
// a page missing any of the three is protocol-invalid and is dropped whole, which is how their
// absence used to empty every list in this file rather than fail at the assertion that meant it.
const SNAPSHOT = 'f'.repeat(64)
const attentionPage = (items, {
  truncated = false, cursor = null, partial = false, stale = false,
  snapshotId = SNAPSHOT, activeActionCount = 0,
} = {}) => ({
  schema: 1,
  generated_at: 1_700_000_100,
  snapshot_id: snapshotId,
  items,
  stale,
  truncated,
  next_cursor: cursor,
  partial,
  active_action_count: activeActionCount,
})

const tick = () => new Promise(resolve => setTimeout(resolve, 0))

async function waitFor(predicate, message) {
  for (let attempt = 0; attempt < 50; attempt++) {
    if (predicate()) return
    await tick()
  }
  assert.fail(message)
}

function response(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  }
}

async function withAttentionHook(t, initial, exercise) {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const queues = {
    attention: [...initial.attention],
    permissions: [...initial.permissions],
  }
  const calls = []
  const fetchStub = async input => {
    const url = String(input)
    const source = url.includes('/api/attention?') ? 'attention'
      : url.includes('/api/assistant/permissions') ? 'permissions' : ''
    assert.ok(source, `unexpected request: ${url}`)
    calls.push(url)
    assert.ok(queues[source].length, `missing queued ${source} response for ${url}`)
    const next = queues[source].shift()
    if (next instanceof Error) throw next
    return response(next.body ?? next, next.status ?? 200)
  }
  const installed = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement,
    Node: dom.window.Node,
    Event: dom.window.Event,
    location: dom.window.location,
    fetch: fetchStub,
    IS_REACT_ACT_ENVIRONMENT: true,
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  for (const [key, value] of Object.entries(installed)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }

  let root
  try {
    const { createRoot } = await import('react-dom/client')
    let latest = null
    const Harness = () => {
      latest = useAttention({ intervalMs: 2_147_483_647 })
      return null
    }
    root = createRoot(document.querySelector('#root'))
    await React.act(async () => { root.render(React.createElement(Harness)) })
    await React.act(async () => {
      await waitFor(() => latest?.initialized, 'initial attention poll did not settle')
    })
    await exercise({
      get latest() { return latest },
      calls,
      enqueue(source, value) { queues[source].push(value) },
      async refresh(predicate) {
        const callCount = calls.length
        await React.act(async () => { latest.refresh() })
        await React.act(async () => {
          await waitFor(() => calls.length >= callCount + 2 && predicate(),
            'refreshed attention poll did not settle')
        })
      },
      async loadMore() {
        await React.act(async () => { await latest.loadMore() })
      },
    })
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor === undefined) delete globalThis[key]
      else Object.defineProperty(globalThis, key, descriptor)
    }
  }
}

test('useAttention preserves each source independently and never retains permission payloads', async t => {
  const firstRun = runItem('a')
  const secondRun = runItem('b')
  const thirdRun = runItem('c')
  const firstPermission = permission('1', '2', 'RAW_FIRST_SECRET')
  const secondPermission = permission('3', '4', 'RAW_SECOND_SECRET')

  await withAttentionHook(t, {
    attention: [attentionPage([firstRun], { partial: true })],
    permissions: [{ pending: [firstPermission] }],
  }, async hook => {
    assert.deepEqual(hook.latest.runs.map(item => item.id), [firstRun.id])
    assert.equal(hook.latest.partial, true)
    assert.equal(hook.latest.runStale, false)
    assert.equal(hook.latest.permissionsStale, false)
    assert.equal(hook.latest.permissions[0].requestId, firstPermission.id)
    assert.doesNotMatch(JSON.stringify(hook.latest), /RAW_FIRST_SECRET/)
    for (const key of ['action', 'scope', 'preview']) {
      assert.equal(Object.hasOwn(hook.latest.permissions[0], key), false)
    }

    hook.enqueue('attention', new Error('run source unavailable'))
    hook.enqueue('permissions', { pending: [secondPermission] })
    await hook.refresh(() => hook.latest.runStale
      && hook.latest.permissions[0]?.requestId === secondPermission.id)
    assert.deepEqual(hook.latest.runs.map(item => item.id), [firstRun.id],
      'a failed run source retains its last safe snapshot')
    assert.equal(hook.latest.runStale, true)
    assert.equal(hook.latest.permissionsStale, false)
    assert.equal(hook.latest.partial, true, 'stale run data retains its partial provenance')
    assert.doesNotMatch(JSON.stringify(hook.latest), /RAW_SECOND_SECRET/)
    for (const key of ['action', 'scope', 'preview']) {
      assert.equal(Object.hasOwn(hook.latest.permissions[0], key), false)
    }

    hook.enqueue('attention', attentionPage([secondRun]))
    hook.enqueue('permissions', new Error('permission source unavailable'))
    await hook.refresh(() => !hook.latest.runStale && hook.latest.permissionsStale
      && hook.latest.runs[0]?.id === secondRun.id)
    assert.deepEqual(hook.latest.runs.map(item => item.id), [secondRun.id])
    assert.equal(hook.latest.permissions[0].requestId, secondPermission.id,
      'a failed permission source retains its independently verified snapshot')
    assert.equal(hook.latest.partial, false)

    hook.enqueue('attention', attentionPage([thirdRun]))
    hook.enqueue('permissions', { pending: 'protocol-corrupt' })
    await hook.refresh(() => hook.latest.runs[0]?.id === thirdRun.id
      && hook.latest.permissionsStale)
    assert.equal(hook.latest.permissions[0].requestId, secondPermission.id,
      'a malformed fulfilled response is stale, not an authoritative empty list')

    hook.enqueue('attention', attentionPage([{ ...thirdRun, id: 'invalid' }]))
    hook.enqueue('permissions', { pending: [] })
    await hook.refresh(() => hook.latest.runStale && !hook.latest.permissionsStale)
    assert.deepEqual(hook.latest.runs.map(item => item.id), [thirdRun.id],
      'a malformed run page cannot erase the last safe run snapshot')
    assert.deepEqual(hook.latest.permissions, [], 'a valid empty source is authoritative')
  })
})

test('useAttention paginates, preserves pages on transport failure, and resets a stale cursor', async t => {
  const first = runItem('a')
  const older = runItem('b', { created: 1_600_000_000 })
  const replacement = runItem('c', { created: 1_500_000_000 })

  await withAttentionHook(t, {
    attention: [attentionPage([first], { truncated: true, cursor: first.id })],
    permissions: [{ pending: [] }],
  }, async hook => {
    assert.equal(hook.latest.hasMore, true)
    assert.equal(hook.latest.nextCursor, first.id)

    hook.enqueue('attention', attentionPage([older], { truncated: true, cursor: older.id }))
    await hook.loadMore()
    assert.deepEqual(hook.latest.runs.map(item => item.id), [first.id, older.id])
    assert.deepEqual(hook.latest.currentItems.map(item => item.id), [first.id],
      'loading an older page cannot relabel archival items as live arrivals')
    assert.equal(hook.latest.nextCursor, older.id)
    assert.equal(hook.latest.hasMore, true)
    assert.equal(new URL(hook.calls.at(-1), 'https://looplab.test').searchParams.get('cursor'),
      first.id)

    hook.enqueue('attention', new Error('temporary network failure'))
    await hook.loadMore()
    assert.deepEqual(hook.latest.runs.map(item => item.id), [first.id, older.id],
      'a transient older-page failure preserves every verified page')
    assert.equal(hook.latest.nextCursor, older.id)
    assert.equal(hook.latest.hasMore, true)
    assert.match(hook.latest.loadMoreError, /could not be loaded/i)

    // A definitive stale cursor is a different class of failure from a transport blip: the pages
    // after the first are not retryable, they are WRONG. The first page is kept but demoted to
    // stale, which is what makes `hasMore` false — `loadMore` early-returns while the run source
    // reads stale, so keeping the affordance visible would offer a button that does nothing. The
    // pairing is the invariant worth pinning: the control disappears exactly when it would no-op,
    // AND the operator is told why, AND recovery is scheduled rather than left to the poll tick.
    // The 409 also ARMS a refresh, so a first page must be queued for it. This one is itself stale,
    // which keeps the feed in the post-409 state long enough to observe it.
    hook.enqueue('attention', { status: 409, body: { detail: 'stale cursor' } })
    hook.enqueue('attention', attentionPage([first], {
      truncated: true, cursor: first.id, stale: true,
    }))
    hook.enqueue('permissions', { pending: [] })
    await hook.loadMore()
    assert.deepEqual(hook.latest.runs.map(item => item.id), [first.id],
      'a definitive stale cursor discards only pages after the current first page')
    assert.equal(hook.latest.nextCursor, first.id)
    assert.equal(hook.latest.hasMore, false,
      'while the run source reads stale `loadMore` is a no-op, so advertising more pages would '
      + 'offer a button that does nothing')
    assert.match(hook.latest.loadMoreError, /list changed/i,
      'the 409 path used to blank the message, so the operator saw the control vanish with no '
      + 'explanation at all')

    // Recovery: the armed refresh is what brings the feed back — the operator does not have to
    // click anything — and the "try again" instruction must not outlive the moment it came true.
    hook.enqueue('attention', attentionPage([first], { truncated: true, cursor: first.id }))
    hook.enqueue('permissions', { pending: [] })
    await hook.refresh(() => !hook.latest.runStale)
    assert.equal(hook.latest.hasMore, true)
    assert.equal(hook.latest.nextCursor, first.id)
    assert.equal(hook.latest.loadMoreError, '')

    hook.enqueue('attention', attentionPage([replacement]))
    await hook.loadMore()
    assert.deepEqual(hook.latest.runs.map(item => item.id), [first.id, replacement.id])
    assert.deepEqual(hook.latest.currentItems.map(item => item.id), [first.id])
    assert.equal(hook.latest.nextCursor, null)
    assert.equal(hook.latest.hasMore, false)

    const cursorCalls = hook.calls.filter(url => url.includes('&cursor='))
    assert.deepEqual(cursorCalls.map(url => new URL(url, 'https://looplab.test').searchParams.get('cursor')),
      [first.id, older.id, older.id, first.id])
  })
})

test('useAttention invalidates archival pages across partial-source transitions', async t => {
  const first = runItem('a')
  const older = runItem('b', { created: 1_600_000_000 })

  await withAttentionHook(t, {
    attention: [attentionPage([first], { truncated: true, cursor: first.id })],
    permissions: [{ pending: [] }],
  }, async hook => {
    hook.enqueue('attention', attentionPage([older]))
    await hook.loadMore()
    assert.deepEqual(hook.latest.runs.map(item => item.id), [first.id, older.id])

    hook.enqueue('attention', attentionPage([first], {
      truncated: true, cursor: first.id, partial: true,
    }))
    hook.enqueue('permissions', { pending: [] })
    await hook.refresh(() => hook.latest.partial && hook.latest.runs.length === 1)
    assert.deepEqual(hook.latest.runs.map(item => item.id), [first.id],
      'a newly partial scan cannot retain archival cards from a different source universe')

    // Paging is not merely unhelpful during a partial scan, it is incoherent: the assertion above
    // is that a partial first page cannot keep archival cards from a different source universe, so
    // fetching MORE of that universe would reintroduce exactly what was just discarded. The hook
    // refuses at both ends — no affordance, and a no-op if called anyway.
    assert.equal(hook.latest.hasMore, false,
      'a partial scan must not advertise more pages; its cursor points into a universe the first '
      + 'page no longer agrees with')
    const beforePartialLoadMore = hook.calls.length
    await hook.loadMore()
    assert.equal(hook.calls.length, beforePartialLoadMore,
      'loadMore issued a request while the scan was partial')
    assert.deepEqual(hook.latest.runs.map(item => item.id), [first.id])

    hook.enqueue('attention', attentionPage([first], {
      truncated: true, cursor: first.id, partial: false,
    }))
    hook.enqueue('permissions', { pending: [] })
    await hook.refresh(() => !hook.latest.partial && hook.latest.runs.length === 1)
    assert.deepEqual(hook.latest.runs.map(item => item.id), [first.id],
      'a recovered complete scan must not reuse pages fetched while the source was partial')
    assert.equal(hook.latest.hasMore, true)
    assert.equal(hook.latest.nextCursor, first.id)
  })
})

// The second stale-cursor detection — the feed moved between issuing the older-page request and
// applying its response — is a defensive re-check inside the `setState` reducer, where `previous`
// can be newer than the state the request was planned against. It is not reachable from the hook's
// public surface: every route that would change the state mid-flight goes through `cancelLoadMore`,
// which drops the response before the reducer runs. So it gets a structural guard instead of a
// behavioural one, and the guard pins the exact thing that was wrong: the two detections producing
// the same state but disagreeing about what the operator sees and whether recovery is scheduled.
test('both stale-cursor detections produce one state and both schedule recovery', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../src/useAttention.js', import.meta.url), 'utf8')
  // Ends at the recovery effect, not at `const refresh` — the effect now sits between them, and a
  // slice that swallowed it would count its `setRefreshToken` as an imperative arming in loadMore.
  const loadMore = source.slice(source.indexOf('const loadMore = useCallback'),
    source.indexOf('// Stale-cursor recovery is armed HERE'))
  assert.ok(loadMore.length > 500, 'the loadMore slice anchors moved; this guard is reading nothing')

  assert.equal((loadMore.match(/staleCursorState\(previous\)/g) || []).length, 2,
    'both stale-cursor detections must build the state through the shared helper — two inline '
    + 'copies is how they drifted apart in the first place')
  // Recovery is armed by ONE effect on the observable state, not by either detection site. That is
  // a correctness rule, not a style one: a `setState` updater must be pure and React runs it during
  // a later render phase, so a flag written inside an updater and read on the next line is not
  // reliably set — the refresh could simply never be armed, and no behavioural test can catch it
  // because the client-side detection is unreachable from the hook's public surface.
  assert.equal((loadMore.match(/setRefreshToken/g) || []).length, 0,
    'loadMore must not arm recovery imperatively; the effect below keys on the resulting state')
  assert.match(source, /const staleCursorArmed = state\.runStale && state\.loadMoreError === STALE_CURSOR_MESSAGE/)
  assert.match(source, /useEffect\(\(\) => \{\s*if \(staleCursorArmed\) setRefreshToken\(value => value \+ 1\)\s*\}, \[staleCursorArmed\]\)/,
    'one effect, keyed on the state both detections converge on, so it cannot double-fire')

  // The exact shape of the bug that motivated the above — a flag declared immediately before an
  // updater, so it can only be there to be written from inside it and read after. Deliberately
  // narrow: updaters legitimately declare and assign their OWN locals (`let runPages` in the poll
  // reducer), and no regex can tell those apart from an outer binding, so this pins the one
  // spelling that is always wrong instead of guessing at scope.
  assert.doesNotMatch(source, /\blet\s+\w+\s*=[^\n]*\n\s*setState\(/,
    'a flag is declared immediately before a setState updater — if it is written inside the updater '
    + 'and read after it, React may run the updater during a later render phase and the read sees '
    + 'the initial value. Key an effect on the resulting state instead.')
  assert.doesNotMatch(loadMore, /loadMoreError: ''\s*,?\s*\}\)\s*:\s*\(\{/,
    'a stale-cursor branch is blanking the message again — the operator needs to be told why the '
    + '"Load more" control disappeared under their click')
})
