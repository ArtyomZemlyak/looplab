// THE PANEL half of fork-to-branch: the exception carved out of the historical view's blanket
// refusal, the two fences a branch has to be admitted through, the shapes a refusal arrives in, and
// the menu that offers the gesture. `forkFromSeqModel.test.js` owns what a branch SENDS; this owns
// who may send one, what the browser does when the answer comes back, and — the reason it exists at
// all — that the ~3,000-line component carrying it still compiles. A dropped brace in `RunView.jsx`
// would leave `vite build` refusing the whole tree while every other test in this suite stayed
// green; nothing in the suite MOUNTS RunView, so `vite.ssrLoadModule` is the rung that can be paid.
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const vite = await createServer({
  root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true },
})
test.after(() => vite.close())

const model = await vite.ssrLoadModule('/src/forkFromSeqModel.js')
const {
  FORK_ACCESS_REASONS, FORK_FROM_SEQ_ACTION, classifyForkFailure, forkGestureAccess, forkRetryable,
  readOnlyNodeActionRefused,
} = model

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

// A live owner view: nothing is refusing, and there is no snapshot either.
const LIVE = {
  historyActive: false, reviewMode: false, routeFenceBlocked: false,
  runAuthorityBlocked: false, startOverMutationBlocked: false,
}
const SNAPSHOT = { ...LIVE, historyActive: true }

test('the exception is exactly one gesture in exactly one kind of read-only view', () => {
  assert.deepEqual(forkGestureAccess(SNAPSHOT), { ok: true, reason: null })
  // Every OTHER reason a view can be read-only still refuses the branch, and each names itself so
  // the panel can print a sentence rather than grey a button out.
  for (const [field, reason] of [
    ['reviewMode', 'review'],
    ['startOverMutationBlocked', 'start_over'],
    ['routeFenceBlocked', 'stale_link'],
    ['runAuthorityBlocked', 'unavailable'],
  ]) {
    const access = forkGestureAccess({ ...SNAPSHOT, [field]: true })
    assert.deepEqual(access, { ok: false, reason }, `${field} must keep refusing the branch`)
    assert.ok(FORK_ACCESS_REASONS[reason], `${reason} owes the operator an explanation`)
  }
  // And it is refused from the other side too: a branch records the vantage point it was formed at,
  // so a LIVE view — which has none — is not merely unnecessary here, it has nothing to record.
  assert.deepEqual(forkGestureAccess(LIVE), { ok: false, reason: 'live' })
  assert.deepEqual(forkGestureAccess(), { ok: false, reason: 'live' })
})

test('the blanket refusal still refuses everything else, and the carve-out is the whole exception', () => {
  const access = forkGestureAccess(SNAPSHOT)
  // The nine other menu actions plus the two reached from elsewhere. Not a sample: this is the whole
  // vocabulary `onNodeAction` dispatches on, and every one of them must still bounce.
  for (const action of ['explore', 'merge', 'ablate', 'diff', 'inspect', 'kill',
    'reset:eval', 'reset:implement', 'reset:propose']) {
    assert.equal(readOnlyNodeActionRefused(action, { mutationReadOnlyMode: true, forkAccess: access }),
      true, `${action} must stay refused in a read-only view`)
  }
  assert.equal(
    readOnlyNodeActionRefused(FORK_FROM_SEQ_ACTION, { mutationReadOnlyMode: true, forkAccess: access }),
    false, 'the one carve-out')
  // The carve-out is not "the fork action", it is "the fork action WITH access": a review link, a
  // stale generation and an unresolved start-over refuse the branch like everything else.
  for (const blocker of ['reviewMode', 'routeFenceBlocked', 'startOverMutationBlocked',
    'runAuthorityBlocked']) {
    assert.equal(readOnlyNodeActionRefused(FORK_FROM_SEQ_ACTION, {
      mutationReadOnlyMode: true, forkAccess: forkGestureAccess({ ...SNAPSHOT, [blocker]: true }),
    }), true, `${blocker} must refuse the branch too`)
  }
  assert.equal(readOnlyNodeActionRefused(FORK_FROM_SEQ_ACTION, { mutationReadOnlyMode: true }),
    true, 'a missing access decision must fail closed')
  // A view that is not read-only is not this function's business at all.
  for (const action of ['explore', 'kill', FORK_FROM_SEQ_ACTION]) {
    assert.equal(readOnlyNodeActionRefused(action, { mutationReadOnlyMode: false }), false)
  }
})

test('RunView asks the model for that rule instead of re-deriving it, and mutationReadOnlyMode is untouched', async () => {
  const runView = await source('RunView.jsx')
  // `mutationReadOnlyMode` is what everything else in the file gates on — the Dock, the Inspector,
  // the merge bar, the settings button, the published run-access envelope. Its definition is the one
  // line this change was not allowed to touch.
  assert.match(runView,
    /const mutationReadOnlyMode = readOnlyMode \|\| runAuthorityBlocked \|\| startOverMutationBlocked/)
  // Tier 3 (CLAUDE.md): this shows the guard is spelled as a call to the rule above rather than as a
  // second, drifting copy of it. It cannot show the call executes — the truth table above is what
  // covers the rule itself, and the compile check below is what covers the file being real code.
  assert.match(runView,
    /if \(readOnlyNodeActionRefused\(action, \{ mutationReadOnlyMode, forkAccess \}\)\) \{/)
  assert.equal(runView.includes('if (mutationReadOnlyMode) {\n      showToast'), false,
    'the old inline guard must not survive beside the new one')
  // The panel is seeded from the SNAPSHOT. `live2` is `hist || live`, so reaching for it here would
  // read the live idea whenever the snapshot had not loaded, and the operator would branch from
  // something they never saw.
  const panelMount = runView.slice(runView.indexOf("panel === 'fork'"), runView.indexOf("onOpenLive"))
  assert.match(panelMount, /node=\{hist\?\.nodes\?\.\[selectedId\] \?\? null\}/)
  assert.equal(panelMount.includes('live2'), false, 'the branch subject must never come from live2')
  assert.match(panelMount, /viewSeq=\{currentHistory\?\.resolvedSeq \?\? null\}/)
})

test('the historical node menu offers the branch and NOTHING else; the live one offers everything but', async () => {
  const dag = await vite.ssrLoadModule('/src/Dag.jsx')
  const { NODE_MENU_ITEMS, nodeMenuEntries } = dag

  const live = nodeMenuEntries(null)
  const liveActions = live.filter(row => row.kind === 'item').map(row => row.id)
  assert.equal(liveActions.includes(FORK_FROM_SEQ_ACTION), false,
    'a live view has no vantage point to branch from, so the item must not be offered there')
  assert.deepEqual(liveActions, ['explore', 'merge', 'ablate', 'diff', 'inspect',
    'reset:eval', 'reset:implement', 'reset:propose', 'kill'],
    'the live menu is exactly what it always was')
  assert.equal(live.filter(row => row.kind === 'heading').length, 1)

  const snapshot = nodeMenuEntries([FORK_FROM_SEQ_ACTION])
  assert.deepEqual(snapshot.map(row => row.kind), ['item'],
    'one item, and no section heading over an empty region')
  assert.equal(snapshot[0].id, FORK_FROM_SEQ_ACTION)
  // Every row is a real action id and a real label — a menu built from a table is one typo away from
  // emitting an action `onNodeAction` has no branch for, which does nothing and reports nothing.
  for (const item of NODE_MENU_ITEMS) {
    assert.equal(typeof item.label, 'string')
    assert.ok(item.label.length > 0)
    assert.ok(item.icon)
  }
})

test('`fork` is a real route panel, or the menu item would silently do nothing', async () => {
  // The trap this covers, found by reading rather than by a red test: `sanitizeRunRouteState` DROPS a
  // panel that is not in `RUN_ROUTE_PANELS`, so `route.update({ panel: 'fork' })` from the menu would
  // have left the route untouched — a click that opens nothing and reports nothing, on both the
  // in-memory path and the deep link.
  const route = await vite.ssrLoadModule('/src/runRouteState.js')
  assert.equal(route.RUN_ROUTE_PANELS.includes('fork'), true)
  assert.equal(route.sanitizeRunRouteState({ panel: 'fork' }).panel, 'fork')
  // A deep link needs its generation fence like every other diagnostic link; the panel name is the
  // part under test.
  const parsed = route.parseRunRouteState(`#/?gen=${'a'.repeat(64)}&panel=fork`)
  assert.equal(parsed.state.panel, 'fork')
  assert.deepEqual(parsed.issues, [], 'a link to the branch form is honoured, not diagnosed')
  // Membership in the route's vocabulary is not permission. A review capability reaches no steering
  // surface, and the panel must be refused there by name rather than opened and then argued with.
  assert.equal(route.REVIEW_SAFE_PANEL_NAMES.includes('fork'), false)
  assert.equal(route.reviewPanelAllowed('fork', true), false)
  assert.equal(route.sanitizeRunRouteState({ panel: 'fork' }, { reviewMode: true }).panel, 'fork',
    'the route still carries it; RunView is what closes it with a stated reason')
})

test('the transport fence admits the branch and nothing else, and only from a snapshot', async () => {
  const runMode = await vite.ssrLoadModule('/src/runMode.js')
  const path = '/api/runs/demo/commands'
  runMode.setRunAccess('demo', { readOnly: true, seq: 12, mode: 'history' })
  // The SECOND fence: RunView decides whether the click is allowed, this decides whether the request
  // may leave the browser at all, and it holds for callers RunView never saw.
  assert.throws(() => runMode.assertRunMutationAllowed(path), /read-only/)
  assert.doesNotThrow(() => runMode.assertRunMutationAllowed(path, { allowModes: ['history'] }))
  for (const mode of ['review', 'stale-link', 'start-over']) {
    runMode.setRunAccess('demo', { readOnly: true, mode })
    assert.throws(() => runMode.assertRunMutationAllowed(path, { allowModes: ['history'] }),
      `${mode} must not be admitted by the branch's own exception`)
  }
  runMode.clearRunAccess('demo')

  // …and exactly one RUN COMMAND names it. A second one is how "this gesture is admissible because
  // of its PAYLOAD" quietly becomes "read-only views can mutate". The seam predates this change and
  // has one other caller, `resetRun`, on a different route and deliberately wider — Start over is
  // the operation that RESOLVES a stale link and an unresolved start-over, so it must run in them.
  const api = await source('api.js')
  const protocol = await source('commandProtocol.js')
  const control = api.slice(api.indexOf('export const CONTROL = {'),
    api.indexOf('export async function appendAction'))
  const named = [...control.matchAll(/allowRunMutationModes: (\[[^\]]*\])/g)].map(m => m[1])
  assert.deepEqual(named, ["['history']"],
    'exactly one durable run command may name the read-only exception')
  assert.match(api, /export const resetRun[\s\S]{0,400}allowRunMutationModes: \['start-over', 'stale-link', 'history'\]/,
    'the pre-existing wider caller is Start over, and it is not on the command route')
  assert.match(api, /forkFrom: \(rid, payload, options = \{\}\) => runCommand\([\s\S]{0,120}allowRunMutationModes: \['history'\]/)
  assert.match(protocol,
    /assertRunMutationAllowed\(path, \{ allowModes: allowRunMutationModes \}\)/)
  assert.match(protocol, /allowRunMutationModes = \[\]/,
    'every other command keeps the historical behaviour by default')
})

test('a stale branch is refused in TWO shapes and the panel must read both the same way', () => {
  // Measured against a real paused run through the real HTTP surface (tests/test_fork_from_seq.py):
  // the legacy /control route answers 409, and the durable /commands route answers 200 with a
  // REJECTED RECORD. Both prove the intake refused before appending; only one of them throws.
  const thrown = Object.assign(new Error('stale parent #3: current generation is 2'), {
    status: 409, message: 'stale parent #3: current generation is 2',
  })
  const record = {
    id: 'cmd_' + '0'.repeat(32),
    status: 'rejected',
    error: { code: 'command_target_not_found',
      message: 'stale parent #3: current generation is 2' },
  }
  const fromThrow = classifyForkFailure(thrown)
  const fromRecord = classifyForkFailure(record)
  for (const failure of [fromThrow, fromRecord]) {
    assert.equal(failure.moved, true)
    assert.equal(failure.applied, false, 'both shapes prove nothing was queued')
    assert.equal(failure.nodeId, 3)
    assert.equal(failure.currentGeneration, 2)
    assert.match(failure.message, /attempt 2/)
  }
  assert.equal(fromThrow.message, fromRecord.message,
    'one refusal, one sentence — the route it arrived by is not the operator’s business')
  // And the rendered consequence: "the run moved" fences the form and offers a re-read, because
  // resubmitting these same bytes earns the same refusal forever.
  assert.equal(forkRetryable(fromRecord), false)
})

test('the three outcomes get three different affordances', () => {
  // A payload the validator refused: nothing queued, and fixable right here.
  const refused = classifyForkFailure({ status: 400,
    code: 'fork_receipt_forged', message: 'forked_from.changed_fields is derived by the server' })
  assert.equal(refused.applied, false)
  assert.equal(refused.moved, false)
  assert.equal(forkRetryable(refused), true, 'the form stays live for a refusal the operator can fix')
  // The run moved: equally un-queued, but not fixable from this snapshot.
  assert.equal(forkRetryable({ applied: false, moved: true }), false)
  // Outcome unknown — a timeout, a 5xx, a record that was accepted and then failed. This one must
  // NEVER re-arm: the branch may already be queued and it is a PAID unit of work.
  const unknown = classifyForkFailure(Object.assign(new Error('network down'), {
    code: 'COMMAND_REQUEST_TIMEOUT' }))
  assert.equal(unknown.applied, null)
  assert.equal(forkRetryable(unknown), false)
  assert.match(unknown.message, /may or may not/)
  const acceptedThenFailed = classifyForkFailure({
    id: 'cmd_' + '1'.repeat(32), status: 'timed_out',
    error: { code: 'command_timed_out', message: 'the engine did not acknowledge' },
  })
  assert.equal(acceptedThenFailed.applied, null,
    'a record that was ACCEPTED before it failed is not proof that nothing was appended')
  assert.equal(forkRetryable(acceptedThenFailed), false)
  assert.equal(forkRetryable(null), true, 'a fresh form submits')
})

test('empty parameters mean ABSENT, not {}', async () => {
  const { parseForkParams } = await vite.ssrLoadModule('/src/ForkFromSeqPanel.jsx')
  // `{}` would count as an edit against a parent that carries no params at all — `forkIdeaEdited`
  // compares the serialized field — so an operator who typed nothing would be told their unchanged
  // copy was a new experiment.
  assert.deepEqual(parseForkParams(''), { ok: true, value: undefined })
  assert.deepEqual(parseForkParams('   \n '), { ok: true, value: undefined })
  assert.deepEqual(parseForkParams('{"lr": 0.001}'), { ok: true, value: { lr: 0.001 } })
  for (const bad of ['{', '[1,2]', '4', 'null', '"x"']) {
    assert.equal(parseForkParams(bad).ok, false, `${bad} is not a parameter mapping`)
  }
})

// The rung that could not be paid when the durable half shipped. It is not a mount — the panel and
// RunView reach storage, timers and the API at first render — it is the property the near-miss it
// guards against actually violates: the module compiles and its whole import graph resolves.
test('the panel and the component that carries it are still modules that compile', async () => {
  const panel = await vite.ssrLoadModule('/src/ForkFromSeqPanel.jsx')
  assert.equal(typeof panel.default, 'function')
  assert.equal(panel.default.name, 'ForkFromSeqPanel')
  const runView = await vite.ssrLoadModule('/src/RunView.jsx')
  assert.equal(typeof runView.default, 'function')
  assert.equal(runView.default.name, 'RunView')
  const dag = await vite.ssrLoadModule('/src/Dag.jsx')
  assert.equal(typeof dag.default, 'function')
})
