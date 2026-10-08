import { uiMessage, uiPlural, uiText } from './uiLanguage.js'
import { authoringRecoveryStorageKey } from './authoringRecoveryStorage.js'

// Doc 25 UI-03's named residue: the ~250 lines of `retainedComment*` / `retainedAuthoring*` /
// `retainedPanel*` derivations that stayed in RunView.jsx when the Start-over saga and the page
// shell were extracted ("the next mergeable entity … but it feeds the navigation-loss guard, the
// Start-over preflight and the fence screen's notices, so it wants its own finding").
//
// RETAINED WORK is everything this browser TAB is still holding on the operator's behalf and cannot
// prove is safe to drop: an unsent comment draft, a comment command whose outcome is unknown, a
// damaged recovery record, an unsaved Run settings or Authoring draft, an Authoring recovery
// snapshot that exists only in memory. Every consumer of it is a REFUSAL or a warning — the
// navigation-loss guard, the panel-close confirm, the fence screen's notices, the Start-over
// preflight — so a mis-derived count is either a silent data loss or a run the operator cannot
// start. That is why it is worth having as a pure model a `node --test` can drive over a truth
// table, instead of 250 lines of component body reachable only by rendering a whole run route.
//
// The split is the house pattern: transitions here, choreography in `useRetainedWork.js`. The
// boundary is the STORE READ — every input below is a plain value the hook has already read out of
// `inspectorDraftStore`, `commentRecoveryStorage` or `authoringRecoveryStorage`, so nothing in this
// file touches React, `window` or storage. Bodies are verbatim from RunView.jsx; only the names of
// the inputs changed, from a `retained*` local to a parameter.

// --------------------------------------------------------------------------- comment retention

export const commentDraftEntryUnsafe = ([scope, fields], runId) => {
  if ((!scope.startsWith(`comment-composer:${runId}@`)
      && !scope.startsWith(`comment-card:${runId}@`))
      || !fields || typeof fields !== 'object' || Array.isArray(fields)) return false
  const busy = fields.busy === true || (typeof fields.busy === 'string' && !!fields.busy)
  const draft = (typeof fields.text === 'string' && fields.text.length > 0)
    || (fields.dirty === true && typeof fields.draftText === 'string')
  const recovery = !!fields.retryIntent || !!fields.uncertainIntent
    || !!fields.editRetryIntent || !!fields.uncertainEdit
    || !!fields.resolutionRetryIntent || !!fields.uncertainResolution
    || !!fields.damagedRecovery
  return busy || draft || recovery
}

export const commentDraftText = ([, fields]) => {
  if (typeof fields?.text === 'string' && fields.text.length > 0) return fields.text
  if (fields?.dirty === true && typeof fields.draftText === 'string') return fields.draftText
  return ''
}

const commentScopeBase = (runId, identity) =>
  `comment-composer:${String(runId)}@${identity.expectedGeneration}:${identity.nodeId}:${identity.nodeGeneration}`

/**
 * What this tab is still holding for Comments, and which of it may be RELEASED.
 *
 * The releasable/protected split is the load-bearing part. A `create` command for the CURRENT
 * generation is append-only: discarding its recovery record without a terminal server outcome can
 * lose a comment that was actually written, so it stays protected and is deliberately excluded from
 * every "discard retained work" path. Everything else — an edit, a resolution, a create belonging to
 * a superseded generation — is releasable, because re-issuing it is safe.
 *
 * `recoveryUnavailable` is a third state and not a synonym for "nothing retained": storage that
 * cannot be inspected means an exact saved command may still be protected in this tab, so the work
 * reads as UNSAFE and nothing may be released.
 */
export function commentRetention({ runId, generation, entries = [], recovery }) {
  const store = recovery || { available: true, valid: [], damaged: [] }
  const scopes = [...new Set(entries.map(([scope]) => scope))]
  const drafts = entries.map(commentDraftText).filter(Boolean)
  const durableCount = store.valid.length + store.damaged.length
  const recoveryUnavailable = !store.available
  const protectedCreateCandidates = [
    ...store.valid.filter(intent => intent.kind === 'create'
      && (!generation || intent.expectedGeneration === generation)),
    ...store.damaged.filter(recoveryRecord => recoveryRecord.identity?.kind === 'create'
      && (!generation || recoveryRecord.identity.expectedGeneration === generation))
      .map(recoveryRecord => recoveryRecord.identity),
    ...entries.flatMap(([, fields]) => {
      const candidates = [fields?.uncertainIntent?.recovery, fields?.damagedRecovery?.identity]
      return candidates.filter(identity => identity?.kind === 'create'
        && (!generation || identity.expectedGeneration === generation))
    }),
  ]
  const protectedCreates = [...new Map(
    protectedCreateCandidates.map(identity => [[
      identity.expectedGeneration, identity.nodeId, identity.nodeGeneration,
    ].join('\u0000'), identity]),
  ).values()]
  const damagedProtectedCreateCount = protectedCreates
    .filter(identity => !identity.operationId).length
  const validProtectedCreateCount = protectedCreates.length - damagedProtectedCreateCount
  const scopeHasProtectedCreate = scope => protectedCreates.some(identity => {
    const base = commentScopeBase(runId, identity)
    return scope === base || scope.startsWith(`${base}:`)
  })
  const releasableEntries = recoveryUnavailable ? []
    : entries.filter(([scope]) => !scopeHasProtectedCreate(scope))
  const releasableIntents = store.valid.filter(
    intent => intent.kind !== 'create' || (generation && intent.expectedGeneration !== generation))
  const releasableDamaged = store.damaged.filter(
    recoveryRecord => recoveryRecord.identity?.kind !== 'create'
      || (generation && recoveryRecord.identity.expectedGeneration !== generation))
  const unsafe = entries.length > 0 || durableCount > 0 || recoveryUnavailable
  return {
    entries,
    recovery: store,
    scopes,
    drafts,
    durableCount,
    recoveryUnavailable,
    protectedCreates,
    damagedProtectedCreateCount,
    validProtectedCreateCount,
    releasableEntries,
    releasableIntents,
    releasableDamaged,
    releasableCount:
      releasableEntries.length + releasableIntents.length + releasableDamaged.length,
    unsafe,
    leaveMessage: commentLeaveMessage({
      drafts, durableCount, entryCount: entries.length, recoveryUnavailable }),
  }
}

export function commentLeaveMessage({ drafts, durableCount, entryCount, recoveryUnavailable }) {
  return [
    drafts.length > 0
      ? uiPlural(drafts.length, '{0} unsaved comment draft will leave this in-memory workspace', '{0} unsaved comment drafts will leave this in-memory workspace')
      : '',
    durableCount > 0
      ? uiPlural(durableCount, '{0} exact comment recovery record will remain protected in this browser tab', '{0} exact comment recovery records will remain protected in this browser tab')
      : '',
    entryCount > drafts.length
      ? uiPlural(entryCount - drafts.length, '{0} other active Comments state will leave the in-memory workspace', '{0} other active Comments states will leave the in-memory workspace')
      : '',
    recoveryUnavailable
      ? uiText('Comments recovery storage cannot be inspected, so an exact saved command may still be protected in this tab')
      : '',
  ].filter(Boolean).join('; ')
}

// -------------------------------------------------------------------------- authoring retention

const plainObject = value => value && typeof value === 'object' && !Array.isArray(value)

/**
 * What Authoring is still holding, split into DURABLE recovery (browser storage survives this tab)
 * and MEMORY-ONLY recovery (leaving discards it).
 *
 * The identity walk exists because one recovery can be visible through up to three channels — the
 * storage scan, the in-memory `uncertainSaves`/`damagedRecoveries` maps, and a document's own
 * `recoveryStorageRaw` — and counting it once per channel would tell the operator they are about to
 * lose three things when they are about to lose one. `(storageKey, raw)` is the identity: same key
 * and same bytes is the same record, and `durable` wins over `memory` for it, because a record that
 * storage also holds is not lost by leaving.
 *
 * `fenceActive` is the generation/history fence on the Authoring panel. Only then is the storage
 * scan performed at all, so outside the fence the count degrades to the coarse "there is memory
 * recovery" bit the notices used before — deliberately, because scanning session storage on every
 * render of every run route is not free.
 */
export function authoringRetention({ documents, uncertainSaves, damagedRecoveries,
                                     fenceActive = false, storageRecovery, guardUnsafe = false }) {
  const storage = storageRecovery || { available: true, count: 0, records: [] }
  const documentEntries = plainObject(documents)
    ? Object.entries(documents).filter(([, document]) => plainObject(document))
    : []
  const documentValues = documentEntries.map(([, document]) => document)
  const draftCount = documentValues
    .filter(document => document.draftText !== document.savedText).length
  const uncertainSaveEntries = plainObject(uncertainSaves)
    ? Object.entries(uncertainSaves).filter(([, recovery]) => plainObject(recovery)) : []
  const damagedRecoveryValues = plainObject(damagedRecoveries)
    ? Object.values(damagedRecoveries).filter(recovery => plainObject(recovery)) : []

  const recoveryIdentities = new Map()
  const addRecovery = (key, raw, source) => {
    if (typeof key !== 'string' || typeof raw !== 'string') return false
    let byRaw = recoveryIdentities.get(key)
    if (!byRaw) {
      byRaw = new Map()
      recoveryIdentities.set(key, byRaw)
    }
    const previous = byRaw.get(raw) || { durable: false, memory: false }
    byRaw.set(raw, {
      durable: previous.durable || source === 'storage',
      memory: previous.memory || source === 'memory',
    })
    return true
  }
  const memoryRecoveryDocumentScopes = new Set()
  const hasMemoryRecovery = uncertainSaveEntries.length > 0
    || damagedRecoveryValues.length > 0
    || documentEntries.some(([, document]) =>
      document.recoveryOperationId || document.recoveryStorageRaw)
  let opaqueMemoryRecoveryCount = 0
  if (fenceActive) {
    for (const recovery of storage.records) {
      addRecovery(recovery.key, recovery.raw, 'storage')
    }
    for (const [scope, recovery] of uncertainSaveEntries) {
      memoryRecoveryDocumentScopes.add(scope)
      addRecovery(recovery.storageKey, recovery.storageRaw, 'memory')
    }
    for (const recovery of damagedRecoveryValues) {
      if (typeof recovery.identity?.scope === 'string') {
        memoryRecoveryDocumentScopes.add(recovery.identity.scope)
      }
      addRecovery(recovery.key, recovery.raw, 'memory')
    }
    for (const [scope, document] of documentEntries) {
      if (!document.recoveryOperationId && !document.recoveryStorageRaw) continue
      const represented = addRecovery(
        authoringRecoveryStorageKey(document.kind, document.name),
        document.recoveryStorageRaw,
        'memory',
      )
      if (!represented && !memoryRecoveryDocumentScopes.has(scope)) {
        opaqueMemoryRecoveryCount += 1
      }
    }
  }
  let durableRecoveryCount = 0
  let memoryOnlyRecoveryCount = fenceActive
    ? opaqueMemoryRecoveryCount
    : hasMemoryRecovery ? 1 : 0
  for (const byRaw of recoveryIdentities.values()) {
    for (const recovery of byRaw.values()) {
      if (recovery.durable) durableRecoveryCount += 1
      else if (recovery.memory) memoryOnlyRecoveryCount += 1
    }
  }
  const recoveryCount = durableRecoveryCount + memoryOnlyRecoveryCount
  const discardItems = [
    draftCount > 0
      ? uiPlural(draftCount, '{0} unsaved in-memory Authoring draft', '{0} unsaved in-memory Authoring drafts') : '',
    memoryOnlyRecoveryCount > 0
      ? uiPlural(memoryOnlyRecoveryCount, '{0} recovery snapshot that exists only in this tab', '{0} recovery snapshots that exist only in this tab') : '',
  ].filter(Boolean)
  return {
    documentEntries,
    draftCount,
    durableRecoveryCount,
    memoryOnlyRecoveryCount,
    recoveryCount,
    draftUnsafe: draftCount > 0 || recoveryCount > 0 || guardUnsafe,
    discardItems,
    discardStatement: discardItems.length > 0
      ? uiMessage('Leaving this run will discard {0}.', [discardItems.join(` ${uiText('and')} `)])
      : uiText('No in-memory Authoring draft will be discarded.'),
  }
}

// ------------------------------------------------------------------------------ panel retention

export const CONFIG_DRAFT_SCHEMA = 'looplab.config-draft/v1'
export const configDraftScope = runId => `panel:config:${String(runId)}`
export const AUTHORING_SCOPE = 'panel:authoring'

const durableRecoveryClause = durableRecoveryCount => durableRecoveryCount > 0
  ? ' ' + uiPlural(durableRecoveryCount, '{0} durable recovery record will remain protected in browser storage.', '{0} durable recovery records will remain protected in browser storage.')
  : ''

/**
 * The ONE unsafe panel, and the exact sentences the operator is shown about it.
 *
 * Only `config` and `authoring` can retain work, and only one of them is open at a time, so this
 * collapses to a single route/scope pair rather than a set. Config wins when both look unsafe, which
 * is unreachable today (one panel is open) and is stated rather than assumed.
 *
 * A registered panel CONTROLLER may override both messages: a live panel knows what it is holding in
 * more detail than a store read can, and the guard's own `leaveSummary`/`closeMessage` are how it
 * says so. The override applies only while the controller's route IS the unsafe route — a stale
 * controller for the other panel may not phrase this panel's refusal.
 */
export function panelRetention({ runId, configDraft, guard, authoring }) {
  const guardRoute = guard?.unsafe === true ? guard.route : null
  const configStoredDraftUnsafe = configDraft?.schema === CONFIG_DRAFT_SCHEMA
    && configDraft?.unsafe === true
  const configDraftUnsafe = configStoredDraftUnsafe || guardRoute === 'config'
  const authoringDraftUnsafe = !!authoring?.draftUnsafe
  const draftUnsafe = configDraftUnsafe || authoringDraftUnsafe
  const route = configDraftUnsafe ? 'config' : authoringDraftUnsafe ? 'authoring' : null
  const scope = configDraftUnsafe ? configDraftScope(runId)
    : authoringDraftUnsafe ? AUTHORING_SCOPE : ''
  const overrides = guardRoute && guardRoute === route ? guard : null
  const durable = durableRecoveryClause(authoring?.durableRecoveryCount || 0)
  const leaveSummary = overrides?.leaveSummary
    ? overrides.leaveSummary
    : configDraftUnsafe
      ? uiText('Leaving this run will discard an unsaved Run settings draft.')
      : `${authoring?.discardStatement || ''}${durable}`
  const closeMessage = overrides?.closeMessage
    ? overrides.closeMessage
    : configDraftUnsafe
      ? uiText('This tab is retaining an unsaved Run settings draft. Close the panel and discard it?')
      // The items are already in the UI language, so the sentence around them is too.
      : ((authoring?.discardItems || []).length > 0
        ? uiMessage('Closing Authoring will discard {0}.', [authoring.discardItems.join(` ${uiText('and')} `)])
        : uiText('No in-memory Authoring draft will be discarded.')) + durable + ' ' + uiText('Close Authoring?')
  return {
    configStoredDraftUnsafe,
    configDraftUnsafe,
    authoringDraftUnsafe,
    draftUnsafe,
    route,
    scope,
    leaveSummary,
    leaveMessage: `${leaveSummary} ${uiText('Leave this run?')}`,
    closeMessage,
  }
}

export function runLeaveMessage({ panelDraftUnsafe, panelLeaveSummary,
                                 commentWorkUnsafe, commentLeaveMessage: commentMessage }) {
  return [
    panelDraftUnsafe ? panelLeaveSummary : '',
    commentWorkUnsafe ? commentMessage : '',
  ].filter(Boolean).join(' ') + ' ' + uiText('Leave this run?')
}

// -------------------------------------------------------------------------- navigation blocking

/**
 * What a target hash means for the run this tab is on.
 *
 * `keepsMutablePanel` is the whole reason navigation is not simply blocked while anything is unsafe:
 * moving WITHIN the same run, to the same panel, at the same generation, with no `?seq` is not
 * leaving the draft — it is the panel's own URL updating. Blocking that would make an unsaved draft
 * freeze the address bar. Both `panel` and `gen` must appear exactly once: a repeated parameter is
 * ambiguous, and an ambiguous route may not be treated as "the same place".
 */
export function retainedNavigationTarget(targetHash, { runId, generation, panelRoute }) {
  const runHash = `#/run/${encodeURIComponent(String(runId))}`
  const sameRun = targetHash === runHash || targetHash.startsWith(`${runHash}?`)
  if (!sameRun) return { sameRun: false, panel: null, keepsMutablePanel: false }
  const queryIndex = targetHash.indexOf('?')
  const params = queryIndex < 0
    ? new URLSearchParams() : new URLSearchParams(targetHash.slice(queryIndex + 1))
  const targetPanels = params.getAll('panel')
  const targetGenerations = params.getAll('gen')
  const targetPanel = targetPanels.length === 1 ? targetPanels[0] : null
  const targetGeneration = targetGenerations.length === 1 ? targetGenerations[0] : null
  const keepsMutablePanel = targetPanels.length === 1 && targetGenerations.length === 1
    && targetPanel === panelRoute && !params.has('seq')
    && targetGeneration === generation
  return { sameRun: true, panel: targetPanel, keepsMutablePanel }
}

export function retainedNavigationShouldBlock(targetHash, context) {
  const { panelDraftUnsafe, commentWorkUnsafe } = context
  const target = retainedNavigationTarget(targetHash, context)
  if (!target.sameRun) return panelDraftUnsafe || commentWorkUnsafe
  return panelDraftUnsafe && !target.keepsMutablePanel
}
