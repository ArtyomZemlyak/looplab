import { useEffect, useMemo, useRef } from 'react'

import { inspectAuthoringRecoveryStorage } from './authoringRecoveryStorage.js'
import { listCommentOperationRecoveries } from './commentRecoveryStorage.js'
import { installNavigationLossGuard } from './navigationLossGuard.js'
import {
  AUTHORING_SCOPE, authoringRetention, commentDraftEntryUnsafe, commentRetention,
  configDraftScope, panelRetention, retainedNavigationShouldBlock, retainedNavigationTarget,
  runLeaveMessage,
} from './retainedWorkModel.js'

// The React half of the retained-work machinery (doc 25 UI-03's named residue). Everything that
// DERIVES lives in `retainedWorkModel.js`; this file is the choreography a pure module cannot own:
// the store reads, the memo keys that decide when to redo them, the navigation-loss guard's
// subscription, the two `window.confirm` gestures and the memory clears they authorize.
//
// Two things are deliberately NOT here. `submitStartOver`'s preflight stays in RunView: it re-reads
// the stores SYNCHRONOUSLY at confirm time rather than using these derivations, because the
// confirmation dialog can outlive the render that opened it and a just-created comment command must
// never race Start over. And the panel navigation GUARD REGISTRY (`publishPanelNavigationGuard`)
// stays in RunView because the lazy panels are handed that callback as a prop; this hook only reads
// the registration.

export function useRetainedWork({
  runId, generation, reviewMode, panel, routeFenceActive, inspectorDraftStore,
  activePanelNavigationGuard, panelNavigationGuardRef, setPanelNavigationGuard,
  commentRecoveryRevision, inspectorDraftRevision, onBack,
}) {
  const comments = useMemo(() => {
    // A review capability has no composer, no recovery storage and no way to retain anything, so it
    // reads as EMPTY rather than as "unknown" — otherwise a reviewer would be refused navigation by
    // work they cannot see or clear.
    const entries = reviewMode ? []
      : inspectorDraftStore.entries()
        .filter(entry => commentDraftEntryUnsafe(entry, String(runId)))
    const recovery = reviewMode
      ? { available: true, valid: [], damaged: [] }
      : listCommentOperationRecoveries(String(runId))
    return commentRetention({ runId, generation, entries, recovery })
  }, [reviewMode, runId, generation, inspectorDraftRevision, commentRecoveryRevision,
      inspectorDraftStore])

  // The panel reads are keyed on the OPEN panel: a closed panel retains nothing that this component
  // can act on, and reading its scope anyway would make an unsafe draft in a panel nobody has open
  // block navigation with a message about a surface that is not on screen.
  const authoringOpen = panel === 'authoring'
  const authoring = authoringRetention({
    documents: authoringOpen
      ? inspectorDraftStore.readField(AUTHORING_SCOPE, 'documents', null) : null,
    uncertainSaves: authoringOpen
      ? inspectorDraftStore.readField(AUTHORING_SCOPE, 'uncertainSaves', null) : null,
    damagedRecoveries: authoringOpen
      ? inspectorDraftStore.readField(AUTHORING_SCOPE, 'damagedRecoveries', null) : null,
    // The storage scan runs only behind the generation/history fence — see the model's note on why
    // the count degrades outside it rather than scanning session storage on every render.
    fenceActive: authoringOpen && routeFenceActive,
    storageRecovery: authoringOpen && routeFenceActive
      ? inspectAuthoringRecoveryStorage() : undefined,
    guardUnsafe: activePanelNavigationGuard?.route === 'authoring'
      && activePanelNavigationGuard.unsafe === true,
  })
  const panelWork = panelRetention({
    runId,
    configDraft: panel === 'config'
      ? inspectorDraftStore.readField(configDraftScope(runId), 'draft', null) : null,
    guard: activePanelNavigationGuard,
    authoring,
  })
  const leaveMessage = runLeaveMessage({
    panelDraftUnsafe: panelWork.draftUnsafe,
    panelLeaveSummary: panelWork.leaveSummary,
    commentWorkUnsafe: comments.unsafe,
    commentLeaveMessage: comments.leaveMessage,
  })

  const allowRef = useRef(false)
  const clearCommentMemory = () => {
    for (const scope of comments.scopes) inspectorDraftStore.clear(scope)
  }
  const clearPanelMemory = () => {
    const controller = panelNavigationGuardRef.current
    if (controller?.route === panelWork.route) {
      controller.dispose()
      if (panelNavigationGuardRef.current === controller) {
        panelNavigationGuardRef.current = null
        setPanelNavigationGuard(current => current === controller ? null : current)
      }
    }
    if (panelWork.draftUnsafe && panelWork.scope) {
      inspectorDraftStore.clear(panelWork.scope)
    }
  }

  const navigationContext = {
    runId, generation, panelRoute: panelWork.route,
    panelDraftUnsafe: panelWork.draftUnsafe, commentWorkUnsafe: comments.unsafe,
  }
  const guardedHash = location.hash
  const guardedHistoryState = window.history.state
  useEffect(() => {
    allowRef.current = false
    if (!panelWork.draftUnsafe && !comments.unsafe) return undefined
    return installNavigationLossGuard({
      allowRef,
      guardedHash,
      guardedState: guardedHistoryState,
      message: targetHash => retainedNavigationTarget(targetHash, navigationContext).sameRun
        ? panelWork.closeMessage : leaveMessage,
      shouldBlock: targetHash => retainedNavigationShouldBlock(targetHash, navigationContext),
      onAllow: targetHash => {
        const target = retainedNavigationTarget(targetHash, navigationContext)
        if (panelWork.draftUnsafe && !target.keepsMutablePanel) {
          clearPanelMemory()
        }
        if (!target.sameRun && comments.unsafe) clearCommentMemory()
      },
    })
    // The dependency list is the one this effect had inline in RunView, unchanged — including
    // `comments.scopes.join('\u0000')`, which is what makes a scope SET change re-arm the guard
    // without re-arming it on every unrelated draft keystroke.
  }, [runId, generation, comments.unsafe, leaveMessage, panelWork.closeMessage,
      comments.scopes.join('\u0000'), panelWork.draftUnsafe, panelWork.route,
      panelWork.scope, guardedHash, guardedHistoryState])

  const confirmPanelClose = () => {
    if (!panelWork.draftUnsafe) return true
    if (!window.confirm(panelWork.closeMessage)) return false
    allowRef.current = true
    clearPanelMemory()
    return true
  }
  const leaveRoute = () => {
    if (!panelWork.draftUnsafe && !comments.unsafe) { onBack?.(); return }
    if (!window.confirm(leaveMessage)) return
    if (panelWork.draftUnsafe) {
      allowRef.current = true
      clearPanelMemory()
    }
    if (comments.unsafe) {
      allowRef.current = true
      clearCommentMemory()
    }
    onBack?.()
  }

  // Returned FLAT and under the names the component body already used, the way
  // `useStartOverRecovery` returns `startOverMutationBlocked` and friends. The alternative — a
  // nested `{comments, authoring, panel}` the caller re-spells at 23 read sites — would have made
  // this a rename of the fence screen's notices and the Comments discard path at the same time as
  // an extraction, and those two reviews want to be separable.
  return {
    retainedCommentEntries: comments.entries,
    retainedCommentRecovery: comments.recovery,
    retainedCommentDrafts: comments.drafts,
    retainedCommentDurableCount: comments.durableCount,
    retainedCommentRecoveryUnavailable: comments.recoveryUnavailable,
    retainedCommentProtectedCreates: comments.protectedCreates,
    retainedCommentDamagedProtectedCreateCount: comments.damagedProtectedCreateCount,
    retainedCommentValidProtectedCreateCount: comments.validProtectedCreateCount,
    retainedCommentReleasableEntries: comments.releasableEntries,
    retainedCommentReleasableIntents: comments.releasableIntents,
    retainedCommentReleasableDamaged: comments.releasableDamaged,
    retainedCommentReleasableCount: comments.releasableCount,
    retainedCommentWorkUnsafe: comments.unsafe,
    retainedAuthoringDraftCount: authoring.draftCount,
    retainedAuthoringDurableRecoveryCount: authoring.durableRecoveryCount,
    retainedAuthoringMemoryOnlyRecoveryCount: authoring.memoryOnlyRecoveryCount,
    retainedAuthoringDraftUnsafe: authoring.draftUnsafe,
    retainedConfigStoredDraftUnsafe: panelWork.configStoredDraftUnsafe,
    retainedConfigDraftUnsafe: panelWork.configDraftUnsafe,
    retainedPanelDraftUnsafe: panelWork.draftUnsafe,
    retainedPanelRoute: panelWork.route,
    retainedRunLeaveMessage: leaveMessage,
    confirmRetainedPanelClose: confirmPanelClose,
    leaveRetainedPanelRoute: leaveRoute,
  }
}
