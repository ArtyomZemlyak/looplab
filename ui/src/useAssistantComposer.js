// The Assistant composer and its per-chat drafts (review 2026-09-22, UI-06), moved out of the
// 4,000-line `AssistantBar.jsx` with its callbacks VERBATIM. The component owns WHEN a draft is
// activated, bound or sent; this hook owns what a draft IS — its text, attachments, pending reads,
// mode and run scope — and the only two things it reads from the component come in as functions:
// the open run (`currentRunId()`) and what else resets when a chat's composer is activated
// (`onActivate()`, the suggestion strip). The pure rules are `assistantComposerModel.js`.
import React, { useRef, useState } from 'react'

import {
  NEW_CHAT_COMPOSER_KEY, composerRunKey, composerUsesRun, newComposerDraft, nextRunScope,
  normalizeComposerMode,
} from './assistantComposerModel.js'

export function useAssistantComposer({ currentRunId, onActivate }) {
  const [input, setInputState] = useState('')
  const [draftRunScope, setDraftRunScope] = useState(null)
  const [mode, setMode] = useState('plan')
  const [files, setFilesState] = useState([])     // attached text files [{name,size,content,truncated}]
  const [pendingFileReads, setPendingFileReads] = useState(0)
  // A composer belongs to the chat it was written in. Keep text and attachments in memory per session
  // so selecting another transcript cannot silently send the previous chat's draft. The unsaved
  // new-chat composer has its own slot; "+ New" deliberately resets that slot.
  const composerDraftsRef = useRef(new Map([
    [NEW_CHAT_COMPOSER_KEY, newComposerDraft()],
  ]))
  const composerKeyRef = useRef(NEW_CHAT_COMPOSER_KEY)
  const composerDraftRef = useRef(composerDraftsRef.current.get(NEW_CHAT_COMPOSER_KEY))
  const setInput = React.useCallback(update => {
    const draft = composerDraftRef.current
    const usedRun = composerUsesRun(draft.input, draft.files, draft.pendingFileReads)
    const next = typeof update === 'function' ? update(draft.input) : update
    draft.input = next
    const usesRun = composerUsesRun(next, draft.files, draft.pendingFileReads)
    draft.runScope = nextRunScope(draft.runScope, {
      usedRun, usesRun, fallback: () => composerRunKey(currentRunId()),
    })
    setInputState(next)
    setDraftRunScope(draft.runScope)
  }, [])
  const updateComposerFiles = React.useCallback((draft, update, runScope = undefined) => {
    const usedRun = composerUsesRun(draft.input, draft.files, draft.pendingFileReads)
    const next = typeof update === 'function' ? update(draft.files) : update
    draft.files = next
    const usesRun = composerUsesRun(draft.input, next, draft.pendingFileReads)
    draft.runScope = nextRunScope(draft.runScope, {
      usedRun, usesRun,
      fallback: () => (runScope === undefined ? composerRunKey(currentRunId()) : runScope),
    })
    if (composerDraftRef.current === draft) {
      setFilesState(next)
      setDraftRunScope(draft.runScope)
    }
  }, [])
  const updatePendingFileReads = React.useCallback((draft, update) => {
    const current = Math.max(0, Number(draft.pendingFileReads) || 0)
    const nextValue = typeof update === 'function' ? update(current) : update
    const next = Math.max(0, Number(nextValue) || 0)
    draft.pendingFileReads = next
    if (!composerUsesRun(draft.input, draft.files, next)) draft.runScope = null
    if (composerDraftRef.current === draft) {
      setPendingFileReads(next)
      setDraftRunScope(draft.runScope)
    }
    return next
  }, [])
  const setFiles = React.useCallback(update => {
    updateComposerFiles(composerDraftRef.current, update)
  }, [updateComposerFiles])
  const setComposerMode = React.useCallback(value => {
    const next = normalizeComposerMode(value)
    composerDraftRef.current.mode = next
    setMode(next)
  }, [])
  const activateComposer = React.useCallback((key, { clear = false, seedMode = 'plan' } = {}) => {
    const draft = clear
      ? newComposerDraft()
      : composerDraftsRef.current.get(key) || newComposerDraft(seedMode)
    draft.mode = normalizeComposerMode(draft.mode ?? seedMode)
    draft.pendingFileReads = Math.max(0, Number(draft.pendingFileReads) || 0)
    composerDraftsRef.current.set(key, draft)
    composerKeyRef.current = key
    composerDraftRef.current = draft
    if (draft.runScope == null && composerUsesRun(
      draft.input, draft.files, draft.pendingFileReads,
    )) {
      draft.runScope = composerRunKey(currentRunId())
    }
    setInputState(draft.input)
    setFilesState(draft.files)
    setPendingFileReads(draft.pendingFileReads)
    setDraftRunScope(draft.runScope)
    setMode(draft.mode)
    onActivate()
    return draft
  }, [])
  const bindComposerToSession = React.useCallback(id => {
    const previousKey = composerKeyRef.current
    const draft = composerDraftRef.current
    composerDraftsRef.current.set(id, draft)
    if (previousKey === NEW_CHAT_COMPOSER_KEY) {
      composerDraftsRef.current.set(NEW_CHAT_COMPOSER_KEY, newComposerDraft())
    }
    composerKeyRef.current = id
  }, [])
  return {
    input, setInputState, draftRunScope, setDraftRunScope, mode, files, pendingFileReads,
    composerDraftsRef, composerKeyRef, composerDraftRef, setInput, updateComposerFiles,
    updatePendingFileReads, setFiles, setComposerMode, activateComposer, bindComposerToSession,
  }
}
