// The RETAINED-WORK truth table (doc 25 UI-03's named residue, extracted 2026-09-08).
//
// Every consumer of these derivations is a REFUSAL or a warning — the navigation-loss guard, the
// panel-close confirm, the fence screen's notices, the Comments discard path — so a wrong count is
// either silent data loss or a run the operator cannot start. While the 250 lines lived inside
// RunView.jsx none of it had a test that could reach it: the only way in was to render a whole run
// route with a populated draft store behind a generation fence. Driven here over the cases that
// actually decide something.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const vite = await createServer({
  root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true },
})
const model = await vite.ssrLoadModule('/src/retainedWorkModel.js')
const focus = await vite.ssrLoadModule('/src/workspaceFocusModel.js')
test.after(() => vite.close())

const {
  AUTHORING_SCOPE, authoringRetention, commentDraftEntryUnsafe, commentDraftText,
  commentRetention, configDraftScope, panelRetention, retainedNavigationShouldBlock,
  retainedNavigationTarget, runLeaveMessage,
} = model
const { WORKSPACE_FOCUS_OWNERS, workspaceFocusOwner, workspaceRefocusSlots } = focus

const RUN = 'demo'
const GEN = 'gen-a'
const composerScope = (nodeId = 3, generation = GEN, nodeGeneration = 1) =>
  `comment-composer:${RUN}@${generation}:${nodeId}:${nodeGeneration}`
const createIntent = (over = {}) => ({
  kind: 'create', expectedGeneration: GEN, nodeId: 3, nodeGeneration: 1,
  operationId: 'op-1', text: 'saved text', ...over,
})

// ------------------------------------------------------------------- what counts as retained work

test('an entry is unsafe only when it is this run\'s composer AND holds something', () => {
  assert.equal(commentDraftEntryUnsafe([composerScope(), { text: 'hi' }], RUN), true)
  assert.equal(commentDraftEntryUnsafe([`comment-card:${RUN}@x:1:1`, { busy: 'saving' }], RUN), true)
  assert.equal(
    commentDraftEntryUnsafe([composerScope(), { dirty: true, draftText: '' }], RUN), true,
    'a dirty empty draft is still an edit the operator made')
  assert.equal(commentDraftEntryUnsafe([composerScope(), { damagedRecovery: {} }], RUN), true)
  // ...and the three ways it is NOT
  assert.equal(commentDraftEntryUnsafe([composerScope(), {}], RUN), false)
  assert.equal(commentDraftEntryUnsafe([composerScope(), null], RUN), false)
  assert.equal(commentDraftEntryUnsafe(['comment-composer:other@x:1:1', { text: 'hi' }], RUN), false,
    'another run\'s draft may not block this run\'s navigation')
})

test('draft text prefers the live text over a dirty saved draft, and is empty otherwise', () => {
  assert.equal(commentDraftText([composerScope(), { text: 'live', draftText: 'old' }]), 'live')
  assert.equal(commentDraftText([composerScope(), { dirty: true, draftText: 'old' }]), 'old')
  assert.equal(commentDraftText([composerScope(), { draftText: 'not dirty' }]), '')
  assert.equal(commentDraftText([composerScope(), { busy: true }]), '')
})

// --------------------------------------------------------- the protected / releasable split

test('a current-generation create is PROTECTED and everything else is releasable', () => {
  const retained = commentRetention({
    runId: RUN,
    generation: GEN,
    entries: [[composerScope(), { text: 'unsent' }], [composerScope(9), { text: 'other node' }]],
    recovery: {
      available: true,
      valid: [
        createIntent(),                                    // protected: append-only, this gen
        createIntent({ expectedGeneration: 'gen-old' }),    // releasable: superseded generation
        createIntent({ kind: 'edit', operationId: 'op-2' }),  // releasable: re-issuing is safe
      ],
      damaged: [],
    },
  })
  assert.equal(retained.protectedCreates.length, 1)
  assert.equal(retained.validProtectedCreateCount, 1)
  assert.equal(retained.damagedProtectedCreateCount, 0)
  assert.deepEqual(retained.releasableIntents.map(i => i.expectedGeneration + ':' + i.kind),
    ['gen-old:create', `${GEN}:edit`])
  // the entry whose SCOPE belongs to the protected create is held back; the other one is not
  assert.deepEqual(retained.releasableEntries.map(([scope]) => scope), [composerScope(9)])
  assert.equal(retained.releasableCount, 1 + 2)
  assert.equal(retained.unsafe, true)
})

test('a protected create with no operation id counts as DAMAGED, not as releasable', () => {
  const retained = commentRetention({
    runId: RUN,
    generation: GEN,
    entries: [],
    recovery: {
      available: true,
      valid: [],
      damaged: [{ identity: createIntent({ operationId: null }) }],
    },
  })
  assert.equal(retained.damagedProtectedCreateCount, 1)
  assert.equal(retained.validProtectedCreateCount, 0)
  assert.equal(retained.releasableDamaged.length, 0,
    'a damaged current-generation create may not be discarded either')
  assert.equal(retained.durableCount, 1)
  assert.equal(retained.unsafe, true)
})

test('unreadable recovery storage releases NOTHING and still reads as unsafe', () => {
  const retained = commentRetention({
    runId: RUN,
    generation: GEN,
    entries: [[composerScope(), { text: 'unsent' }]],
    recovery: { available: false, valid: [], damaged: [] },
  })
  assert.equal(retained.recoveryUnavailable, true)
  assert.equal(retained.unsafe, true)
  assert.deepEqual(retained.releasableEntries, [], 'storage we cannot read may still hold a create')
  assert.match(retained.leaveMessage, /recovery storage cannot be inspected/)
})

test('an empty tab retains nothing and says nothing', () => {
  const retained = commentRetention({
    runId: RUN, generation: GEN, entries: [],
    recovery: { available: true, valid: [], damaged: [] },
  })
  assert.equal(retained.unsafe, false)
  assert.equal(retained.releasableCount, 0)
  assert.equal(retained.leaveMessage, '')
})

test('the leave message counts drafts, durable records and other active state separately', () => {
  const retained = commentRetention({
    runId: RUN,
    generation: GEN,
    entries: [[composerScope(), { text: 'one' }], [composerScope(9), { busy: true }]],
    recovery: { available: true, valid: [createIntent({ kind: 'edit' })], damaged: [] },
  })
  assert.match(retained.leaveMessage, /1 unsaved comment draft will leave/)
  assert.match(retained.leaveMessage, /1 exact comment recovery record will remain protected/)
  assert.match(retained.leaveMessage, /1 other active Comments state will leave/)
})

// ------------------------------------------------------------------------- authoring retention

const storageKeyFor = (kind, name) =>
  'll.authoring-operation.' + encodeURIComponent(`${kind}\u0000${name}`)

test('one recovery seen through three channels is counted ONCE, and durable wins', () => {
  const key = storageKeyFor('skill', 'a')
  const raw = '{"operationId":"op-1"}'
  const retained = authoringRetention({
    documents: { 'doc:a': { kind: 'skill', name: 'a', draftText: 'x', savedText: 'x',
                            recoveryOperationId: 'op-1', recoveryStorageRaw: raw } },
    uncertainSaves: { 'doc:a': { storageKey: key, storageRaw: raw } },
    damagedRecoveries: {},
    fenceActive: true,
    storageRecovery: { available: true, count: 1, records: [{ key, raw }] },
  })
  assert.equal(retained.durableRecoveryCount, 1)
  assert.equal(retained.memoryOnlyRecoveryCount, 0,
    'a record browser storage also holds is not lost by leaving')
  assert.equal(retained.recoveryCount, 1)
  assert.match(retained.discardStatement, /No in-memory Authoring draft will be discarded\./)
})

test('a memory-only recovery is reported as lost, and a draft is counted beside it', () => {
  const key = storageKeyFor('skill', 'b')
  const retained = authoringRetention({
    documents: { 'doc:b': { kind: 'skill', name: 'b', draftText: 'new', savedText: 'old' } },
    uncertainSaves: { 'doc:b': { storageKey: key, storageRaw: '{"operationId":"op-2"}' } },
    damagedRecoveries: {},
    fenceActive: true,
    storageRecovery: { available: true, count: 0, records: [] },
  })
  assert.equal(retained.draftCount, 1)
  assert.equal(retained.durableRecoveryCount, 0)
  assert.equal(retained.memoryOnlyRecoveryCount, 1)
  assert.equal(retained.draftUnsafe, true)
  assert.match(retained.discardStatement,
    /1 unsaved in-memory Authoring draft and 1 recovery snapshot that exists only in this tab/)
})

test('outside the fence the recovery count degrades to the coarse bit, and no storage is read', () => {
  const retained = authoringRetention({
    documents: { 'doc:c': { kind: 'skill', name: 'c', draftText: 'x', savedText: 'x',
                            recoveryOperationId: 'op-3' } },
    uncertainSaves: null,
    damagedRecoveries: null,
    fenceActive: false,
    storageRecovery: undefined,
  })
  assert.equal(retained.memoryOnlyRecoveryCount, 1)
  assert.equal(retained.draftCount, 0)
  assert.equal(retained.draftUnsafe, true)
})

test('a registered guard alone makes Authoring unsafe even with nothing in the store', () => {
  const clean = authoringRetention({ documents: {}, uncertainSaves: {}, damagedRecoveries: {} })
  assert.equal(clean.draftUnsafe, false)
  const guarded = authoringRetention({
    documents: {}, uncertainSaves: {}, damagedRecoveries: {}, guardUnsafe: true })
  assert.equal(guarded.draftUnsafe, true)
})

// ------------------------------------------------------------------------------ panel retention

test('the config draft schema is checked, not just the unsafe flag', () => {
  const unsafe = panelRetention({ runId: RUN,
    configDraft: { schema: 'looplab.config-draft/v1', unsafe: true } })
  assert.equal(unsafe.configStoredDraftUnsafe, true)
  assert.equal(unsafe.route, 'config')
  assert.equal(unsafe.scope, configDraftScope(RUN))
  const foreign = panelRetention({ runId: RUN, configDraft: { schema: 'other/v9', unsafe: true } })
  assert.equal(foreign.configStoredDraftUnsafe, false)
  assert.equal(foreign.draftUnsafe, false)
  assert.equal(foreign.route, null)
})

test('a live panel controller may phrase its own refusal, but only for the unsafe route', () => {
  const authoring = authoringRetention({
    documents: { 'doc:d': { kind: 'skill', name: 'd', draftText: 'a', savedText: 'b' } },
    uncertainSaves: {}, damagedRecoveries: {} })
  const owned = panelRetention({ runId: RUN, authoring, configDraft: null,
    guard: { route: 'authoring', unsafe: true, leaveSummary: 'Two skills are unsaved.',
             closeMessage: 'Close Authoring and lose two skills?' } })
  assert.equal(owned.leaveSummary, 'Two skills are unsaved.')
  assert.equal(owned.leaveMessage, 'Two skills are unsaved. Leave this run?')
  assert.equal(owned.closeMessage, 'Close Authoring and lose two skills?')
  assert.equal(owned.scope, AUTHORING_SCOPE)
  // a controller for the OTHER panel may not phrase this one's refusal
  const stale = panelRetention({ runId: RUN, authoring, configDraft: null,
    guard: { route: 'config', unsafe: true, leaveSummary: 'Run settings are unsaved.' } })
  assert.equal(stale.route, 'config', 'a config guard makes config the unsafe route')
  assert.equal(stale.leaveSummary, 'Run settings are unsaved.')
})

test('config wins over authoring when both look unsafe, and the durable clause is appended once', () => {
  const authoring = authoringRetention({
    documents: { 'doc:e': { kind: 'skill', name: 'e', draftText: 'a', savedText: 'b',
                            recoveryOperationId: 'op', recoveryStorageRaw: '{}' } },
    uncertainSaves: {}, damagedRecoveries: {}, fenceActive: true,
    storageRecovery: { available: true, count: 1,
                       records: [{ key: storageKeyFor('skill', 'e'), raw: '{}' }] } })
  assert.equal(authoring.durableRecoveryCount, 1)
  const both = panelRetention({ runId: RUN, authoring,
    configDraft: { schema: 'looplab.config-draft/v1', unsafe: true } })
  assert.equal(both.route, 'config')
  assert.equal(both.leaveSummary, 'Leaving this run will discard an unsaved Run settings draft.')
  const authoringOnly = panelRetention({ runId: RUN, authoring, configDraft: null })
  assert.equal(
    (authoringOnly.closeMessage.match(/durable recovery record/g) || []).length, 1)
  assert.match(authoringOnly.closeMessage, /will remain protected in browser storage\./)
})

test('the run leave message joins only the halves that are actually unsafe', () => {
  assert.equal(runLeaveMessage({ panelDraftUnsafe: false, panelLeaveSummary: 'P',
                                 commentWorkUnsafe: false, commentLeaveMessage: 'C' }),
    ' Leave this run?')
  assert.equal(runLeaveMessage({ panelDraftUnsafe: true, panelLeaveSummary: 'P',
                                 commentWorkUnsafe: true, commentLeaveMessage: 'C' }),
    'P C Leave this run?')
})

// ------------------------------------------------------------------------ navigation blocking

const ctx = (over = {}) => ({
  runId: RUN, generation: GEN, panelRoute: 'config',
  panelDraftUnsafe: true, commentWorkUnsafe: false, ...over,
})

test('moving within the same run, panel and generation is NOT leaving the draft', () => {
  const hash = `#/run/${RUN}?panel=config&gen=${GEN}`
  assert.equal(retainedNavigationTarget(hash, ctx()).keepsMutablePanel, true)
  assert.equal(retainedNavigationShouldBlock(hash, ctx()), false,
    'blocking this would freeze the address bar while a draft is open')
})

test('a different panel, generation, run or a history seq all leave it', () => {
  for (const hash of [
    `#/run/${RUN}?panel=authoring&gen=${GEN}`,
    `#/run/${RUN}?panel=config&gen=other`,
    `#/run/${RUN}?panel=config&gen=${GEN}&seq=12`,
    '#/run/other?panel=config',
    '#/runs',
  ]) {
    assert.equal(retainedNavigationShouldBlock(hash, ctx()), true, hash)
  }
})

test('a repeated panel or gen parameter is ambiguous and may not read as "the same place"', () => {
  const doubled = `#/run/${RUN}?panel=config&panel=config&gen=${GEN}`
  assert.equal(retainedNavigationTarget(doubled, ctx()).keepsMutablePanel, false)
  assert.equal(retainedNavigationShouldBlock(doubled, ctx()), true)
})

test('retained COMMENT work blocks only leaving the run, never moving inside it', () => {
  const commentsOnly = ctx({ panelDraftUnsafe: false, commentWorkUnsafe: true, panelRoute: null })
  assert.equal(retainedNavigationShouldBlock('#/runs', commentsOnly), true)
  assert.equal(
    retainedNavigationShouldBlock(`#/run/${RUN}?panel=authoring&gen=${GEN}`, commentsOnly), false,
    'a comment draft follows the operator around its own run')
})

test('a run id needing encoding still matches its own hash', () => {
  const encoded = ctx({ runId: 'a b/c' })
  assert.equal(retainedNavigationTarget('#/run/a%20b%2Fc?panel=config', encoded).sameRun, true)
  assert.equal(retainedNavigationTarget('#/run/a b/c', encoded).sameRun, false)
})

// --------------------------------------------------------------------- the focus switchyard

const fakeElement = (contains = [], closest = []) => ({
  contains: node => contains.includes(node),
  closest: selector => (closest.includes(selector) ? {} : null),
})

test('the focus owner is classified by containment first, then by ancestor selector', () => {
  const target = { closest: () => null }
  const trigger = fakeElement([target])
  assert.equal(workspaceFocusOwner(target, { compactInspectorTrigger: trigger }),
    'compact-inspector-trigger')
  assert.equal(workspaceFocusOwner(target, { sideRail: fakeElement([target]) }),
    'desktop-side-rail')
  assert.equal(workspaceFocusOwner({ closest: s => (s === '.splitter.h' ? {} : null) }, {}),
    'timeline-splitter')
  assert.equal(workspaceFocusOwner({ closest: s => (s === '#run-events-timeline' ? {} : null) }, {}),
    'timeline-body')
  assert.equal(workspaceFocusOwner(target, {}), null, 'the canvas and panels are "not my business"')
  assert.equal(workspaceFocusOwner(null, {}), null)
})

test('the scrim beats the compact inspector that visually contains it', () => {
  // The sheet CONTAINS the scrim, so a containment-first reading would classify a scrim click as
  // the inspector surface by the wrong rule. The order in the model is what keeps them distinct.
  const scrimTarget = { closest: s => (s === '.workspace-scrim' ? {} : null) }
  assert.equal(
    workspaceFocusOwner(scrimTarget, { compactInspector: fakeElement([scrimTarget]) }),
    'inspector-surface')
  const splitterTarget = { closest: s => (s === '.splitter.v' ? {} : null) }
  assert.equal(
    workspaceFocusOwner(splitterTarget, { compactInspector: fakeElement([splitterTarget]) }),
    'side-splitter', 'a splitter inside the sheet is still a splitter')
})

test('every owner the classifier can return is in the declared vocabulary', () => {
  const produced = [
    workspaceFocusOwner({ closest: () => null }, { compactInspectorTrigger: fakeElement([1]) }),
  ].filter(Boolean)
  for (const owner of produced) assert.ok(WORKSPACE_FOCUS_OWNERS.includes(owner))
  assert.equal(new Set(WORKSPACE_FOCUS_OWNERS).size, WORKSPACE_FOCUS_OWNERS.length)
})

test('the post-swap focus target depends on the direction, not just the owner', () => {
  const compactToDesktop = { focusOwner: 'inspector-surface', wasCompact: true }
  assert.deepEqual(workspaceRefocusSlots({ ...compactToDesktop, sideC: true }), ['side-rail'])
  assert.deepEqual(workspaceRefocusSlots({ ...compactToDesktop, sideC: false }),
    ['compact-inspector-close', 'compact-inspector'])
  assert.deepEqual(
    workspaceRefocusSlots({ focusOwner: 'inspector-surface', wasCompact: false }),
    ['compact-inspector-trigger', 'selected-node'])
})

test('the timeline branch is direction-independent and only fires when collapsed', () => {
  for (const wasCompact of [true, false]) {
    assert.deepEqual(workspaceRefocusSlots({ focusOwner: 'timeline-splitter', wasCompact }),
      ['timeline-collapse'])
    assert.deepEqual(
      workspaceRefocusSlots({ focusOwner: 'timeline-body', wasCompact, timelineCollapsed: true }),
      ['timeline-collapse'])
    assert.deepEqual(
      workspaceRefocusSlots({ focusOwner: 'timeline-body', wasCompact, timelineCollapsed: false }),
      [], 'an open timeline keeps its own focus across the swap')
  }
})

test('an owner outside the workspace leaves focus alone', () => {
  assert.deepEqual(workspaceRefocusSlots({ focusOwner: null, wasCompact: true }), [])
  assert.deepEqual(workspaceRefocusSlots({ focusOwner: 'desktop-side-rail', wasCompact: true }), [],
    'the side rail does not exist on the layout that was just left')
})

// ------------------------------------------------------- the extraction itself, not a comment

test('RunView declares neither the derivations nor the switchyard any more', async () => {
  const source = await readFile(new URL('../src/RunView.jsx', import.meta.url), 'utf8')
  for (const gone of [
    'workspaceFocusOwnerRef',            // the switchyard's remembering ref
    'const retainedCommentProtectedCreateCandidates',
    'const retainedAuthoringRecoveryIdentities',
    'installNavigationLossGuard',        // the guard is armed by the hook now
    'listCommentOperationRecoveries(String(runId))',  // ...except in the Start-over preflight
  ]) {
    const hits = source.split(gone).length - 1
    if (gone === 'listCommentOperationRecoveries(String(runId))') {
      assert.equal(hits, 1,
        'the Start-over preflight keeps its own synchronous re-read and must be the only one')
      continue
    }
    assert.equal(hits, 0, `${gone} is back in RunView.jsx`)
  }
  // and it reaches the extracted pair rather than re-deriving
  assert.match(source, /useRetainedWork\(\{/)
  assert.match(source, /useWorkspaceFocusOwner\(\{/)
})
