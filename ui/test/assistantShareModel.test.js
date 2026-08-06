import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import {
  SHARE_ACTIONS, shareActionBlock, shareActionFailure, shareSnapshotAudience,
} from '../src/assistantShareModel.js'

const assistantSource = () => readFile(new URL('../src/AssistantBar.jsx', import.meta.url), 'utf8')

const idle = {
  shareActionActive: false, forkingSession: false, deletingSession: false, turnIncomplete: false,
}

test('both public-link mutations are refused by the same facts, in the same order', () => {
  assert.deepEqual(SHARE_ACTIONS, ['snapshot', 'revoke'])
  for (const action of SHARE_ACTIONS) {
    assert.equal(shareActionBlock(action, idle), null, `${action}: an idle chat may act`)
    assert.equal(shareActionBlock(action, { ...idle, shareActionActive: true }).message,
      'Another share action is still in progress')
    assert.equal(shareActionBlock(action, { ...idle, deletingSession: true }).message,
      'This chat is being deleted')
    assert.match(shareActionBlock(action, { ...idle, forkingSession: true }).message, /finish forking/)
    // The first true fact wins, and the order is the property: "another share action" outranks a fork,
    // which outranks a deletion, because the operator can only usefully act on the innermost cause.
    assert.equal(
      shareActionBlock(action, { shareActionActive: true, forkingSession: true, deletingSession: true,
        turnIncomplete: true }).message,
      'Another share action is still in progress')
    assert.match(
      shareActionBlock(action, { ...idle, forkingSession: true, deletingSession: true }).message,
      /finish forking/)
  }
})

test('only a snapshot waits for a complete turn, and only a snapshot names the fork it waits on', () => {
  // A snapshot is a durable public artifact frozen at the turns that exist right now, so publishing
  // a half-streamed reply would expose text the model has not finished saying. Revoking only ever
  // takes access away, so it must stay available mid-turn — that asymmetry is the whole rule.
  assert.equal(shareActionBlock('snapshot', { ...idle, turnIncomplete: true })?.message,
    'Wait for the Assistant reply to finish before creating a snapshot')
  assert.equal(shareActionBlock('revoke', { ...idle, turnIncomplete: true }), null,
    'an incomplete turn must never block revoking a public link')
  assert.equal(shareActionBlock('snapshot', { ...idle, forkingSession: true }).message,
    'Wait for this chat to finish forking before creating a snapshot')
  assert.equal(shareActionBlock('revoke', { ...idle, forkingSession: true }).message,
    'Wait for this chat to finish forking before changing its public links')
})

test('an unrecognised share action is refused rather than defaulted', () => {
  // The default here would be "mint a public link", so the closed vocabulary has to fail closed.
  for (const action of ['', null, undefined, 'share', 'unshare', 'SNAPSHOT', 'toString']) {
    assert.equal(shareActionBlock(action, idle)?.message, 'Unknown share action', String(action))
  }
  assert.equal(shareActionBlock('snapshot')?.message, undefined,
    'omitting the facts entirely means nothing is known to be blocking')
})

test('only a 4xx is authoritative about a public link; everything else stays uncertain', () => {
  for (const action of SHARE_ACTIONS) {
    for (const error of [null, undefined, {}, { status: null }, { status: 'boom' }, { status: 399 },
      { status: 500 }, { status: 502 }, { name: 'TimeoutError' }, { name: 'AbortError' }]) {
      assert.equal(shareActionFailure(action, error).uncertain, true, `${action} ${JSON.stringify(error)}`)
    }
    for (const status of [400, 401, 403, 404, 409, 413, 422, 429, 499]) {
      assert.equal(shareActionFailure(action, { status }).uncertain, false, `${action} ${status}`)
    }
  }
  assert.equal(shareActionFailure('snapshot', { status: 500 }).notice,
    'Share uncertain · revoke before retrying')
  assert.equal(shareActionFailure('revoke', { status: 500 }).notice, 'Revoke uncertain · retry to confirm')
  assert.equal(shareActionFailure('revoke', { status: 403 }).notice, 'Revoke failed')
})

test('an authoritative snapshot refusal names the cause the operator can act on', () => {
  assert.equal(shareActionFailure('snapshot', {
    status: 413, code: 'assistant_share_snapshot_too_large',
  }).notice, 'This chat is too large for a complete snapshot · fork or share a shorter chat')
  for (const code of ['assistant_share_in_progress', 'assistant_share_incomplete',
    'assistant_share_turn_active', 'assistant_share_turn_incomplete']) {
    assert.equal(shareActionFailure('snapshot', { status: 409, code }).notice,
      'Wait for a complete Assistant reply before sharing', code)
  }
  assert.equal(shareActionFailure('snapshot', { status: 409, code: 'assistant_share_unknown' }).notice,
    'Share failed')
  // The refusal codes are a LIST, not a prefix match: a future `assistant_share_*` code has to be
  // adopted deliberately instead of silently inheriting "wait for a complete reply".
  assert.equal(shareActionFailure('snapshot', { status: 409, code: 'assistant_share_turn' }).notice,
    'Share failed')
  // The exception's own message never reaches the operator.
  assert.equal(shareActionFailure('snapshot', { status: 400, message: 'ECONNRESET at 0x1' }).notice,
    'Share failed')
})

test('a snapshot that lands after the operator moved on is recorded but not announced as theirs', () => {
  assert.equal(shareSnapshotAudience(true, 'a1b2c3d4e5f60718', 'a1b2c3d4e5f60718'), 'current')
  assert.equal(shareSnapshotAudience(true, 'ffffffffffffffff', 'a1b2c3d4e5f60718'), 'departed')
  assert.equal(shareSnapshotAudience(true, null, 'a1b2c3d4e5f60718'), 'departed')
  assert.equal(shareSnapshotAudience(false, 'a1b2c3d4e5f60718', 'a1b2c3d4e5f60718'), 'gone')
  assert.equal(shareSnapshotAudience(false, 'ffffffffffffffff', 'a1b2c3d4e5f60718'), 'gone',
    'an unmounted tab has no audience at all, whichever chat it left behind')
})

test('the Assistant renders no public-link control flow inside its JSX', async () => {
  const source = await assistantSource()
  // Doc 25 UI-05. Both handlers were `onClick={async () => { … }}` props with refs, clipboard
  // fallbacks and a deletion tombstone check inside the render tree. They are named handlers now, and
  // the whole point is that no equivalent grows back: an `await` reachable from a JSX attribute is
  // control flow no reviewer reads.
  const jsx = source.slice(source.indexOf('const renderThread = () =>'))
  assert.doesNotMatch(jsx, /onClick=\{async \(\) => \{/,
    'an inline async onClick is a handler hiding in the render tree; give it a name')
  assert.match(source, /onClick=\{createShareSnapshot\}/)
  assert.match(source, /onClick=\{copyShareFallbackLink\}/)
  assert.match(source, /onClick=\{revokeCurrentShares\}/)
  // Both mutations must reach the shared decisions rather than re-deriving them; the snapshot's own
  // turn facts are the synchronous REF reads, not the render-time `shareTurnIncomplete` gate, because
  // a click settles before React publishes the flag it would otherwise consult.
  const snapshot = source.slice(source.indexOf('const createShareSnapshot ='),
    source.indexOf('const copyShareFallbackLink ='))
  assert.match(snapshot, /beginShareAction\('snapshot', shareSid, \{\s*\n\s*turnIncomplete: runningRef\.current \|\| turnCaptureRef\.current \|\| busy \|\| commandBusy\s*\n\s*\|\| pending\.length > 0 \|\| msgs\[msgs\.length - 1\]\?\.role === 'user',/)
  assert.match(snapshot, /reportShareFailure\('snapshot', shareSid, error\)/)
  const revoke = source.slice(source.indexOf('const revokeCurrentShares ='))
  assert.match(revoke, /beginShareAction\('revoke', shareSid\)/)
  assert.match(revoke, /reportShareFailure\('revoke', shareSid, error\)/)
})
