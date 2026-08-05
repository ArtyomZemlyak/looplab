import React, { useEffect, useId, useMemo, useRef, useState } from 'react'

import {
  COMMAND_FAILED, COMMAND_SUCCEEDED, CONTROL, commandCanRetry, commentHistory,
  createIdempotencyKey, getRunCommand, retryRunCommand,
} from './api.js'
import {
  COMMENT_MAX_BYTES, commentConflict, commentDraftState, commentMutationError,
  filterComments, normalizeCommentHistory,
} from './commentsModel.js'
import { fmtAgo, fmtDate } from './format.js'
import { OpIcon } from './icons.jsx'
import { useComments } from './useComments.js'
import { createInspectorDraftStore, useInspectorDraftField } from './inspectorDraftStore.js'
import {
  clearCommentOperationIntent, clearDamagedCommentOperation,
  commentOperationIntentStored, createCommentOperationIntent, inspectCommentOperation,
  refreshCommentOperationRecoveries, saveCommentOperationIntent,
} from './commentRecoveryStorage.js'

const terminalCommentRecord = record => {
  if (record && COMMAND_SUCCEEDED.has(record.status)) return record
  if (record && COMMAND_FAILED.has(record.status)) {
    const error = new Error(record.error?.message || 'The comment command failed.')
    error.code = record.error?.code || 'comment_command_failed'
    error.detail = record.error || null
    error.commandRecord = record
    error.commandTerminal = true
    throw error
  }
  const error = new Error('The comment is still being applied. Refresh before retrying it.')
  error.code = 'comment_command_pending'
  error.commandUnknown = true
  error.commandRecord = record || null
  throw error
}

const COMMAND_ID_RE = /^cmd_[0-9a-f]{32}$/

// A strict-lock failure is returned as HTTP 503 with the durable command id in the error body,
// while ordinary retryable failures arrive as terminal command records. Normalize both forms so
// the UI can re-arm that exact server-side intent through /retry. Never use this for an id-less or
// merely pending submission: its outcome is not authoritative enough to permit another write.
const retryableCommentRecord = (error, expectedCommandId = null) => {
  if (error?.commandTerminal === true && commandCanRetry(error.commandRecord)
      && (!expectedCommandId || error.commandRecord.id === expectedCommandId)) {
    return error.commandRecord
  }
  const detail = error?.detail
  if (!COMMAND_ID_RE.test(String(error?.commandId || ''))
      || (expectedCommandId && error.commandId !== expectedCommandId)
      || !detail || typeof detail !== 'object' || detail.retryable !== true) return null
  return {
    id: String(error.commandId),
    status: 'failed',
    error: { code: String(detail.code || error.code || 'comment_command_failed'), retryable: true },
  }
}

const retryCommentCommand = async (runId, record) => terminalCommentRecord(
  await retryRunCommand(runId, record.id, { waitMs: 12_000 }),
)

const commentCommandOutcomeUnknown = error => error?.commandUnknown === true
  || (!!error?.commandRecord
    && !COMMAND_SUCCEEDED.has(error.commandRecord.status)
    && !COMMAND_FAILED.has(error.commandRecord.status))
const commentCommandPending = record => !!record
  && !COMMAND_SUCCEEDED.has(record.status)
  && !COMMAND_FAILED.has(record.status)
const recoveryCommentRecord = (error, boundRecord) => {
  const observed = error?.commandRecord
  if (!observed || error?.code === 'COMMAND_PROTOCOL_ERROR'
      || !COMMAND_ID_RE.test(String(observed.id || ''))) return boundRecord
  if (boundRecord?.id && observed.id !== boundRecord.id) return boundRecord
  return observed
}
// A failed status read is never proof that the observed write did not apply. Only a validated
// terminal command record may release its fence; access, abort, missing and malformed responses
// all keep the same durable identity available for another check.
const commandRecoveryOutcomeUnresolved = error => error?.commandTerminal !== true

const mutationOptions = (expectedGeneration, idempotencyKey) => ({
  expectedGeneration,
  idempotencyKey,
  waitMs: 12_000,
})
const domId = (id, surface = 'inspector') => `run-comment-${surface}-${id}`

const submissionFromRecovery = recovery => recovery ? {
  text: recovery.text,
  version: recovery.expectedVersion,
  resolved: recovery.resolved,
  idempotencyKey: recovery.operationId,
  record: null,
  unknown: true,
  recovery,
} : null

function useStoredCommentOperation(draftStore, scope, identity, enabled = true) {
  const initialRef = useRef(null)
  const initialScope = `${scope}:${enabled ? 'enabled' : 'disabled'}`
  if (!initialRef.current || initialRef.current.scope !== initialScope) {
    const inspected = enabled ? inspectCommentOperation(identity) : { kind: 'none', key: null }
    initialRef.current = {
      scope: initialScope,
      inspected,
      restored: inspected.kind === 'valid' ? submissionFromRecovery(inspected.intent) : null,
    }
  }
  const initial = initialRef.current.inspected
  const [damaged, setDamaged] = useInspectorDraftField(
    draftStore, scope, 'damagedRecovery', null)
  const [storageUnavailable, setStorageUnavailable] = useInspectorDraftField(
    draftStore, scope, 'storageUnavailable', false)
  const hydrationToken = initial.kind === 'damaged'
    ? `${initialScope}:${initial.key || ''}:${initial.raw || ''}`
    : `${initialScope}:${initial.kind}`
  const hydratedInitialRef = useRef(null)
  const initialHydrationPending = hydratedInitialRef.current !== hydrationToken
  useEffect(() => {
    if (hydratedInitialRef.current === hydrationToken) return
    hydratedInitialRef.current = hydrationToken
    if (!enabled) return
    if (initial.kind === 'damaged') {
      const current = inspectCommentOperation(identity)
      setDamaged(current.kind === 'damaged' && current.raw === initial.raw ? current : null)
      setStorageUnavailable(current.kind === 'unavailable')
      return
    }
    if (initial.kind === 'unavailable') setStorageUnavailable(true)
  }, [enabled, hydrationToken, setDamaged, setStorageUnavailable])
  const visibleDamaged = damaged || (initialHydrationPending && initial.kind === 'damaged'
    ? initial : null)
  const visibleStorageUnavailable = storageUnavailable
    || (initialHydrationPending && initial.kind === 'unavailable')
  const inspect = () => {
    if (!enabled) return { kind: 'none', key: null }
    const next = inspectCommentOperation(identity)
    setStorageUnavailable(next.kind === 'unavailable')
    setDamaged(next.kind === 'damaged' ? next : null)
    refreshCommentOperationRecoveries()
    return next
  }
  const discardDamaged = () => {
    if (!enabled || !visibleDamaged || !clearDamagedCommentOperation(visibleDamaged)) return false
    setDamaged(null)
    setStorageUnavailable(false)
    return true
  }
  return {
    restored: initialRef.current.restored,
    damaged: visibleDamaged,
    storageUnavailable: visibleStorageUnavailable,
    inspect,
    discardDamaged,
    setStorageUnavailable,
  }
}

const createStoredCommentSubmission = fields => {
  const intent = createCommentOperationIntent({
    ...fields, operationId: createIdempotencyKey(),
  })
  const recovery = intent && saveCommentOperationIntent(intent)
  return recovery ? submissionFromRecovery(recovery) : null
}

const recoveryStorageMessage = 'Recovery storage is unavailable. Nothing was sent. Free browser storage or enable it, then retry.'
const recoveryChangedMessage = 'The saved recovery identity changed. Nothing was sent; refresh this Comments view before retrying.'

function DraftCounter({ draft }) {
  const invalid = draft.tooLarge || draft.invalidUnicode
  return <span className={'comment-byte-count' + (invalid ? ' over' : '')}
    aria-live={invalid ? 'polite' : 'off'}>
    {draft.invalidUnicode
      ? 'Unsupported Unicode sequence'
      : `${draft.bytes.toLocaleString()} / ${COMMENT_MAX_BYTES.toLocaleString()} bytes`}
  </span>
}

function CommentComposer({
  runId, nodeId, nodeGeneration, expectedGeneration, onRefresh, onAnnounce,
  draftStore, draftScope,
}) {
  const fieldId = useId()
  const recoveryIdentity = {
    kind: 'create', runId, expectedGeneration, nodeId, nodeGeneration, commentId: null,
  }
  const recovery = useStoredCommentOperation(
    draftStore, `${draftScope}:recovery`, recoveryIdentity)
  const [text, setText] = useInspectorDraftField(
    draftStore, draftScope, 'text', '')
  const [busy, setBusy] = useInspectorDraftField(draftStore, draftScope, 'busy', false)
  const [error, setError] = useInspectorDraftField(draftStore, draftScope, 'error', '')
  const [retryIntent, setRetryIntent] = useInspectorDraftField(
    draftStore, draftScope, 'retryIntent', null)
  const [uncertainIntent, setUncertainIntent] = useInspectorDraftField(
    draftStore, draftScope, 'uncertainIntent', null)
  const [messageKind, setMessageKind] = useInspectorDraftField(
    draftStore, draftScope, 'messageKind', 'error')
  const restoredIntent = recovery.restored
  const hydratedIntentRef = useRef(null)
  useEffect(() => {
    if (!restoredIntent || hydratedIntentRef.current === restoredIntent.recovery.storageRaw) return
    hydratedIntentRef.current = restoredIntent.recovery.storageRaw
    const current = recovery.inspect()
    if (current.kind !== 'valid' || current.raw !== restoredIntent.recovery.storageRaw) {
      if (current.kind === 'valid') recovery.setStorageUnavailable(true)
      return
    }
    // Hydrate into real store fields exactly once. A recovered value must never be a hook fallback:
    // clearing a completed scope would otherwise reveal that fallback again after its CAS record is gone.
    setText(current => current || restoredIntent.text)
    setUncertainIntent(current => current || restoredIntent)
  }, [restoredIntent, setText, setUncertainIntent])
  const draft = useMemo(() => commentDraftState(text), [text])
  const normalizedText = text.trim()
  // The owner below keys this composer by run + node + node attempt + run generation, so retry and
  // unknown-outcome state cannot survive any mutation-scope change.
  const exactRetry = retryIntent?.text === normalizedText
  const retryIntentMismatch = !!retryIntent && !exactRetry
  // Creating a comment is append-only. Once one submission has an unknown outcome, changing or
  // clearing the textarea must not silently permit a second POST that could create a duplicate.
  const recoveryNeedsHydration = !!restoredIntent
    && hydratedIntentRef.current !== restoredIntent.recovery.storageRaw
  const pendingIntent = uncertainIntent || (recoveryNeedsHydration ? restoredIntent : null)
  const outcomeUnknown = pendingIntent != null
  const copyPendingSubmission = async () => {
    try {
      await navigator.clipboard.writeText(pendingIntent?.text || '')
      onAnnounce?.('Pending comment submission copied.')
    } catch {
      onAnnounce?.('Clipboard is unavailable. The pending submission remains visible below.')
    }
  }

  const submit = async event => {
    event?.preventDefault?.()
    if (busy || recovery.damaged || recovery.storageUnavailable || retryIntentMismatch
        || (!outcomeUnknown && !draft.valid)) return
    const checking = outcomeUnknown
    const retrying = !checking && exactRetry
    const submission = outcomeUnknown
      ? pendingIntent
      : retrying
        ? retryIntent
        : createStoredCommentSubmission({
            ...recoveryIdentity, expectedVersion: null,
            text: normalizedText, resolved: null,
          })
    if (!submission) {
      const inspected = recovery.inspect()
      if (inspected.kind === 'valid') {
        const restored = submissionFromRecovery(inspected.intent)
        setText(current => current || restored.text)
        setUncertainIntent(restored)
        setError('Another saved operation already owns this comment subject. Check that exact command before posting again.')
        setMessageKind('status')
      } else {
        setError(inspected.kind === 'damaged'
          ? 'A damaged recovery record blocks this comment subject. Nothing was sent.'
          : recoveryStorageMessage)
        setMessageKind('error')
      }
      return
    }
    if (!commentOperationIntentStored(submission.recovery)) {
      setError(recoveryChangedMessage)
      setMessageKind('error')
      return
    }
    const observing = outcomeUnknown && submission.record?.id
      && commentCommandPending(submission.record)
    let completed = false
    let retainNewerDraft = false
    setBusy(true)
    setError('')
    setMessageKind('error')
    try {
      let terminalRecord
      if (observing) {
        terminalRecord = terminalCommentRecord(await getRunCommand(runId, submission.record.id))
      } else if (retrying) {
        terminalRecord = await retryCommentCommand(runId, submission.record)
      } else {
        terminalRecord = terminalCommentRecord(await CONTROL.createComment(runId, {
          nodeId, nodeGeneration, text: submission.text,
        }, mutationOptions(expectedGeneration, submission.idempotencyKey)))
      }
      retainNewerDraft = normalizedText !== submission.text
      onAnnounce?.(`Comment added to experiment #${nodeId}.`)
      onRefresh?.()
      if (!clearCommentOperationIntent(submission.recovery)) {
        setUncertainIntent({ ...submission, record: terminalRecord, unknown: false })
        setError('The comment was posted, but its saved recovery record could not be cleared. Check the same command to clean it up safely.')
        setMessageKind('status')
        return
      }
      completed = true
    } catch (caught) {
      const record = retryableCommentRecord(caught, submission.record?.id)
      const unknown = !record && (commentCommandOutcomeUnknown(caught)
        || (checking && commandRecoveryOutcomeUnresolved(caught)))
      const releaseFailed = !record && !unknown
        && !clearCommentOperationIntent(submission.recovery)
      setRetryIntent(record ? { ...submission, record, unknown: false } : null)
      setUncertainIntent(unknown || releaseFailed
        ? { ...submission, record: recoveryCommentRecord(caught, submission.record),
            unknown: caught?.commandUnknown === true }
        : null)
      const message = commentMutationError(caught, 'Comment could not be added. Your draft is preserved.')
      setError(releaseFailed
        ? `${message} Its saved recovery record could not be cleared; check the same command before posting again.`
        : message)
      setMessageKind('error')
    } finally {
      // Do not recreate an empty store entry by writing `busy=false` after a successful clear.
      if (completed && !retainNewerDraft) {
        draftStore.clear(draftScope)
      } else {
        if (completed) {
          setUncertainIntent(null)
          setRetryIntent(null)
          setError('The earlier comment was posted. Review this newer draft before posting it.')
          setMessageKind('status')
        }
        setBusy(false)
      }
    }
  }

  return <form className="comment-composer" onSubmit={submit} aria-busy={busy ? 'true' : 'false'}>
    <label htmlFor={fieldId}>Add a comment to experiment #{nodeId}</label>
    <textarea id={fieldId} className="text" rows={4} value={text} disabled={busy} maxLength={8192}
      placeholder="Record a decision, question, or review note…"
      aria-describedby={`${fieldId}-hint ${fieldId}-count${error ? ` ${fieldId}-error` : ''}`}
      onChange={event => {
        const next = event.target.value
        if (next === '' && !outcomeUnknown) {
          if (retryIntent && !clearCommentOperationIntent(retryIntent.recovery)) {
            setText(next)
            setError('The failed command is still saved. Restore its text or discard that exact recovery before posting again.')
            return
          }
          draftStore.clear(draftScope)
          return
        }
        setText(next)
      }}
      onKeyDown={event => {
        if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') submit(event)
      }} />
    <div className="comment-composer-meta">
      <span id={`${fieldId}-hint`} className="muted">Plain text · Ctrl/⌘+Enter posts · visible in read-only review links after redaction</span>
      <span id={`${fieldId}-count`}><DraftCounter draft={draft} /></span>
    </div>
    {recovery.storageUnavailable && <div className="notice resource-error comment-inline-error" role="alert">
      <span>{recoveryStorageMessage}</span>
      <button type="button" className="btn xs" onClick={() => {
        const inspected = recovery.inspect()
        if (inspected.kind === 'none') {
          setError('Recovery storage is available again. You can post this draft.')
          setMessageKind('status')
        } else if (inspected.kind === 'valid') {
          const restored = submissionFromRecovery(inspected.intent)
          setText(current => current || restored.text)
          setUncertainIntent(restored)
          setError('A saved comment operation was restored. Check that exact command before posting again.')
          setMessageKind('status')
        }
      }}>Retry storage</button>
    </div>}
    {recovery.damaged && <div className="notice resource-error comment-inline-error" role="alert">
      <span>A saved new-comment recovery record is invalid. Posting stays blocked because discarding an
        unverified append-only command could create a duplicate. Restore this tab's recovery data or
        reload Comments; nothing will be sent automatically.</span>
    </div>}
    {error && <div id={`${fieldId}-error`}
      className={`notice comment-inline-error ${messageKind === 'status' ? 'warn' : 'resource-error'}`}
      role={messageKind === 'status' ? 'status' : 'alert'}><span>{error}</span></div>}
    {outcomeUnknown && <div className="comment-recovery-panel"
      aria-label="Uncertain comment submission recovery">
      <details>
        <summary>View submission to check</summary>
        <div className="comment-recovery-payload">{pendingIntent.text}</div>
      </details>
      <div className="comment-recovery-actions">
        <button type="button" className="btn sm" onClick={onRefresh}>Refresh comments</button>
        <button type="button" className="btn sm" onClick={copyPendingSubmission}>
          Copy pending submission
        </button>
      </div>
      <div className="muted">Check command safely resubmits the exact saved idempotency key. This
        append-only recovery cannot be discarded until the server returns a terminal outcome.</div>
    </div>}
    {retryIntentMismatch && <div className="comment-recovery-panel" role="status">
      <span>The saved failed command belongs to different text. Restore it to retry, or discard that terminal failure before posting a new intent.</span>
      <div className="comment-recovery-actions">
        <button type="button" className="btn sm" onClick={() => setText(retryIntent.text)}>
          Restore failed submission
        </button>
        <button type="button" className="btn sm ghost" onClick={() => {
          if (!clearCommentOperationIntent(retryIntent.recovery)) {
            setError('The saved failed-command record changed and was not discarded. Refresh Comments.')
            return
          }
          setRetryIntent(null)
          setError('Failed command discarded. Review this draft before posting a new comment.')
          setMessageKind('status')
        }}>Discard failed command</button>
      </div>
    </div>}
    <div className="comment-composer-actions">
      <button type="submit" className="btn sm primary"
        disabled={busy || recovery.damaged || recovery.storageUnavailable || retryIntentMismatch
          || (!outcomeUnknown && !draft.valid)}
        title={exactRetry ? 'Retry this exact durable command; no new comment intent is created'
          : retryIntentMismatch ? 'Resolve the saved failed command before posting new text'
            : recovery.damaged || recovery.storageUnavailable
              ? 'Working recovery storage is required before posting' : undefined}>
        <OpIcon name="chat" size={12} /> {busy
          ? outcomeUnknown ? 'Checking…' : exactRetry ? 'Retrying…' : 'Posting…'
          : outcomeUnknown ? 'Check command' : exactRetry ? 'Retry same command' : 'Post comment'}
      </button>
    </div>
  </form>
}

function History({ runId, comment, expectedGeneration, onAnnounce, domPrefix }) {
  const [open, setOpen] = useState(false)
  const [pages, setPages] = useState([])
  const [status, setStatus] = useState('idle')
  const [error, setError] = useState('')
  const [nextCursor, setNextCursor] = useState(null)
  const [hasMore, setHasMore] = useState(false)
  const loadingRef = useRef(false)

  const load = async (cursor = null) => {
    if (loadingRef.current) return
    loadingRef.current = true
    setStatus(cursor ? 'loading-more' : 'loading')
    setError('')
    try {
      const page = normalizeCommentHistory(
        await commentHistory(runId, comment.id, { limit: 100, cursor }),
        comment,
        expectedGeneration,
      )
      if (!page) throw new Error('Comment history returned an invalid response.')
      setPages(previous => cursor ? [...previous, page.versions] : [page.versions])
      setNextCursor(page.nextCursor)
      setHasMore(page.hasMore)
      setStatus('ready')
    } catch (caught) {
      setError(caught?.message || 'Comment history could not be loaded.')
      setStatus(pages.length ? 'ready' : 'error')
      onAnnounce?.('Comment history could not be loaded.')
    } finally { loadingRef.current = false }
  }

  const versions = pages.flat()
  return <div className="comment-history">
    <button type="button" className="btn xs ghost" aria-expanded={open}
      aria-controls={`${domPrefix}-history`}
      onClick={() => {
        const next = !open
        setOpen(next)
        if (next && status === 'idle') load()
      }}>
      {open ? 'Hide history' : `History (${comment.version})`}
    </button>
    {open && <div id={`${domPrefix}-history`} className="comment-history-body">
      {status === 'loading' && <div className="muted" role="status">Loading history…</div>}
      {error && <>
        <div className="notice resource-error comment-inline-error" role="alert"><span>{error}</span></div>
        <div className="comment-recovery-actions">
          <button type="button" className="btn xs" onClick={() => load()}>Retry</button>
        </div>
      </>}
      {versions.length > 0 && <ol>
        {versions.map((version, index) => <li key={`${version.version}:${index}`}>
          <div className="comment-history-meta">
            <b>{version.action}</b> · {version.actorLabel} · <time
              dateTime={new Date(version.updatedAt * 1000).toISOString()}
              title={fmtDate(version.updatedAt)}>{fmtAgo(version.updatedAt)}</time>
          </div>
          <div className="comment-history-text">{version.text}</div>
          {version.resolved && <span className="pill">resolved</span>}
        </li>)}
      </ol>}
      {hasMore && <button type="button" className="btn sm" disabled={status === 'loading-more'}
        onClick={() => load(nextCursor)}>{status === 'loading-more' ? 'Loading…' : 'Load older history'}</button>}
    </div>}
  </div>
}

function CommentCard({
  runId, comment, expectedGeneration, readOnly, global, focused,
  onOpenComment, onRefresh, onAnnounce, draftStore, draftScope, draftSurface,
}) {
  const editScope = `${draftScope}:edit`
  const resolutionScope = `${draftScope}:resolution`
  const observationScope = `${draftScope}:observation`
  const editRecoveryIdentity = {
    kind: 'edit', runId, expectedGeneration, nodeId: comment.nodeId,
    nodeGeneration: comment.nodeGeneration, commentId: comment.id,
  }
  const resolutionRecoveryIdentity = { ...editRecoveryIdentity, kind: 'resolution' }
  const mutableCommentSubject = !readOnly && comment.editable && !comment.legacy
    && /^[0-9a-f]{64}$/.test(expectedGeneration || '')
    && Number.isSafeInteger(comment.nodeId) && Number.isSafeInteger(comment.nodeGeneration)
  const editRecovery = useStoredCommentOperation(
    draftStore, `${editScope}:recovery`, editRecoveryIdentity, mutableCommentSubject)
  const resolutionRecovery = useStoredCommentOperation(
    draftStore, `${resolutionScope}:recovery`, resolutionRecoveryIdentity, mutableCommentSubject)
  const commentDomId = domId(comment.id, draftSurface)
  const [inspectorEditing, setInspectorEditing] = useInspectorDraftField(
    draftStore, editScope, 'editing:inspector', false)
  const [collabEditing, setCollabEditing] = useInspectorDraftField(
    draftStore, editScope, 'editing:collab', false)
  const editing = draftSurface === 'collab' ? collabEditing : inspectorEditing
  const setEditing = draftSurface === 'collab' ? setCollabEditing : setInspectorEditing
  const [draftText, setDraftText] = useInspectorDraftField(
    draftStore, editScope, 'draftText', comment.text)
  const [editBaseText, setEditBaseText] = useInspectorDraftField(
    draftStore, editScope, 'editBaseText', comment.text)
  const [editBaseVersion, setEditBaseVersion] = useInspectorDraftField(
    draftStore, editScope, 'editBaseVersion', null)
  const [dirty, setDirty] = useInspectorDraftField(
    draftStore, editScope, 'dirty', false)
  const [editBusy, setEditBusy] = useInspectorDraftField(draftStore, editScope, 'busy', '')
  const [editError, setEditError] = useInspectorDraftField(draftStore, editScope, 'error', '')
  const [editMessageKind, setEditMessageKind] = useInspectorDraftField(
    draftStore, editScope, 'messageKind', 'error')
  const [editConflictVersion, setEditConflictVersion] = useInspectorDraftField(
    draftStore, editScope, 'conflictVersion', null)
  const [editRetryIntent, setEditRetryIntent] = useInspectorDraftField(
    draftStore, editScope, 'editRetryIntent', null)
  const [uncertainEdit, setUncertainEdit] = useInspectorDraftField(
    draftStore, editScope, 'uncertainEdit', null)
  const [resolutionBusy, setResolutionBusy] = useInspectorDraftField(
    draftStore, resolutionScope, 'busy', '')
  const [resolutionError, setResolutionError] = useInspectorDraftField(
    draftStore, resolutionScope, 'error', '')
  const [resolutionConflictVersion, setResolutionConflictVersion] = useInspectorDraftField(
    draftStore, resolutionScope, 'conflictVersion', null)
  const [resolutionRetryIntent, setResolutionRetryIntent] = useInspectorDraftField(
    draftStore, resolutionScope, 'resolutionRetryIntent', null)
  const [uncertainResolution, setUncertainResolution] = useInspectorDraftField(
    draftStore, resolutionScope, 'uncertainResolution', null)
  const [latestSeenVersion, setLatestSeenVersion] = useInspectorDraftField(
    draftStore, observationScope, 'latestSeenVersion', 0, { disposable: true })
  const editorRef = useRef(null)
  const editButtonRef = useRef(null)
  const focusEditorRef = useRef(false)
  const draft = useMemo(() => commentDraftState(draftText), [draftText])
  const normalizedDraft = draftText.trim()
  const draftChanged = normalizedDraft !== comment.text
  const surfaceStale = comment.version < latestSeenVersion
  const versionChanged = editBaseVersion != null && comment.version > editBaseVersion
  const canMutate = !readOnly && comment.editable && !comment.legacy && !surfaceStale
  const canViewHistory = !readOnly && !comment.legacy
  const exactEditRetry = editRetryIntent?.text === normalizedDraft
    && editRetryIntent?.version === comment.version
  const editRetryMismatch = !!editRetryIntent && !exactEditRetry
  const editOutcomeUnknown = uncertainEdit?.version === comment.version
  const editIntentMismatch = (!!uncertainEdit && !editOutcomeUnknown)
    || (!!editRetryIntent && editRetryIntent.version !== comment.version)
  const resolutionTarget = !comment.resolved
  const exactResolutionRetry = resolutionRetryIntent?.resolved === resolutionTarget
    && resolutionRetryIntent?.version === comment.version
  const resolutionOutcomeUnknown = uncertainResolution?.resolved === resolutionTarget
    && uncertainResolution?.version === comment.version
  const resolutionIntentMismatch = (!!uncertainResolution && !resolutionOutcomeUnknown)
    || (!!resolutionRetryIntent && !exactResolutionRetry)
  const restoredEdit = editRecovery.restored
  const restoredResolution = resolutionRecovery.restored
  const hydratedEditRef = useRef(null)
  const hydratedResolutionRef = useRef(null)
  const editRecoveryNeedsHydration = !!restoredEdit
    && hydratedEditRef.current !== restoredEdit.recovery.storageRaw
  const resolutionRecoveryNeedsHydration = !!restoredResolution
    && hydratedResolutionRef.current !== restoredResolution.recovery.storageRaw
  // A clean editor in the other surface is still active work. Block resolution globally so it cannot
  // disable an unseen Inspector/Collab editor with a command the operator cannot recover there.
  const hasEditDraft = inspectorEditing || collabEditing
    || dirty || !!editRetryIntent || !!uncertainEdit || editRecoveryNeedsHydration
    || !!editRecovery.damaged || editRecovery.storageUnavailable
  const resolutionFence = resolutionConflictVersion != null
    || !!resolutionRetryIntent || !!uncertainResolution || resolutionRecoveryNeedsHydration
    || !!resolutionRecovery.damaged || resolutionRecovery.storageUnavailable
  const editRecoveryBlocked = !!editRecovery.damaged || editRecovery.storageUnavailable
  const busy = !!editBusy || !!resolutionBusy
  const editorVisible = editing && canMutate
  const restoreEditFocus = () => requestAnimationFrame(() => editButtonRef.current?.focus())

  const retainInspectedEdit = inspected => {
    if (inspected.kind !== 'valid') return null
    const restored = submissionFromRecovery(inspected.intent)
    setDraftText(current => current === comment.text ? restored.text : current)
    setEditBaseVersion(current => current ?? restored.version)
    setDirty(true)
    setUncertainEdit(current => current || restored)
    return restored
  }
  const retainInspectedResolution = inspected => {
    if (inspected.kind !== 'valid') return null
    const restored = submissionFromRecovery(inspected.intent)
    setUncertainResolution(current => current || restored)
    return restored
  }
  const retryEditRecoveryStorage = () => {
    const inspected = editRecovery.inspect()
    if (retainInspectedEdit(inspected)) {
      setEditError('A saved edit operation was found. Check that exact command; any newer draft remains retained.')
      setEditMessageKind('status')
    } else if (inspected.kind === 'none') {
      setEditError('Recovery storage is available again. You can edit this comment.')
      setEditMessageKind('status')
    }
  }
  const retryResolutionRecoveryStorage = () => {
    const inspected = resolutionRecovery.inspect()
    if (retainInspectedResolution(inspected)) {
      setResolutionError('A saved resolution operation was found. Check that exact command before acting again.')
    } else if (inspected.kind === 'none') {
      setResolutionError('Recovery storage is available again. Review the current state before acting.')
    }
  }

  useEffect(() => {
    if (!mutableCommentSubject || !restoredEdit
        || hydratedEditRef.current === restoredEdit.recovery.storageRaw) return
    hydratedEditRef.current = restoredEdit.recovery.storageRaw
    const current = editRecovery.inspect()
    if (current.kind !== 'valid' || current.raw !== restoredEdit.recovery.storageRaw) {
      if (current.kind === 'valid') editRecovery.setStorageUnavailable(true)
      return
    }
    setDraftText(current => current === comment.text ? restoredEdit.text : current)
    setEditBaseVersion(current => current ?? restoredEdit.version)
    setDirty(true)
    setUncertainEdit(current => current || restoredEdit)
  }, [comment.text, mutableCommentSubject, restoredEdit, setDraftText,
    setEditBaseVersion, setDirty, setUncertainEdit])
  useEffect(() => {
    if (!mutableCommentSubject || !restoredResolution
        || hydratedResolutionRef.current === restoredResolution.recovery.storageRaw) return
    hydratedResolutionRef.current = restoredResolution.recovery.storageRaw
    const current = resolutionRecovery.inspect()
    if (current.kind !== 'valid' || current.raw !== restoredResolution.recovery.storageRaw) {
      if (current.kind === 'valid') resolutionRecovery.setStorageUnavailable(true)
      return
    }
    setUncertainResolution(current => current || restoredResolution)
  }, [mutableCommentSubject, restoredResolution, setUncertainResolution])

  useEffect(() => {
    if (comment.version > latestSeenVersion) {
      setLatestSeenVersion(previous => Math.max(previous, comment.version))
    }
  }, [comment.version, latestSeenVersion])

  useEffect(() => {
    if (!surfaceStale && editConflictVersion != null && comment.version > editConflictVersion) {
      setEditConflictVersion(null)
      setEditError('Latest version loaded. Your draft remains in the editor.')
      setEditMessageKind('status')
    }
    if (!surfaceStale && resolutionConflictVersion != null
        && comment.version > resolutionConflictVersion) {
      setResolutionConflictVersion(null)
      setResolutionError('')
    }
  }, [comment.version, editConflictVersion, resolutionConflictVersion, surfaceStale])

  useEffect(() => {
    if (!mutableCommentSubject || surfaceStale) return
    if (editRetryIntent && comment.version > editRetryIntent.version
        && clearCommentOperationIntent(editRetryIntent.recovery)) setEditRetryIntent(null)
    if (uncertainEdit && comment.version > uncertainEdit.version
        && clearCommentOperationIntent(uncertainEdit.recovery)) setUncertainEdit(null)
    if (resolutionRetryIntent && comment.version > resolutionRetryIntent.version) {
      if (clearCommentOperationIntent(resolutionRetryIntent.recovery)) {
        setResolutionRetryIntent(null)
        setResolutionError('')
      }
    }
    if (uncertainResolution && comment.version > uncertainResolution.version) {
      if (clearCommentOperationIntent(uncertainResolution.recovery)) {
        setUncertainResolution(null)
        setResolutionError('')
      }
    }
  }, [comment.version, comment.resolved, editRetryIntent, mutableCommentSubject, uncertainEdit,
    resolutionRetryIntent, uncertainResolution, surfaceStale])

  useEffect(() => {
    if (!mutableCommentSubject || surfaceStale || !versionChanged) return
    if (!dirty && !editRetryIntent && !uncertainEdit) {
      setDraftText(comment.text)
      setEditBaseText(comment.text)
      setEditBaseVersion(comment.version)
      setEditError('')
      return
    }
    if (editRetryIntent && clearCommentOperationIntent(editRetryIntent.recovery)) {
      setEditRetryIntent(null)
    }
    if (uncertainEdit && clearCommentOperationIntent(uncertainEdit.recovery)) {
      setUncertainEdit(null)
    }
    setEditError('This comment changed after your draft started. Review the latest version before saving.')
  }, [comment.text, comment.version, dirty, editRetryIntent, mutableCommentSubject, uncertainEdit,
    surfaceStale, versionChanged])

  const save = async () => {
    const checking = editOutcomeUnknown
    if (busy || resolutionFence || editRecoveryBlocked || editRetryMismatch || editIntentMismatch
        || (!checking && (!canMutate || editConflictVersion != null
        || !draft.valid || !draftChanged || versionChanged))) return
    const retrying = !checking && exactEditRetry
    const submitted = checking
      ? uncertainEdit
      : retrying
        ? editRetryIntent
        : createStoredCommentSubmission({
            ...editRecoveryIdentity, expectedVersion: comment.version,
            text: normalizedDraft, resolved: null,
          })
    if (!submitted) {
      const inspected = editRecovery.inspect()
      if (inspected.kind === 'valid') {
        retainInspectedEdit(inspected)
        setEditError('Another saved operation already owns this edit. Check that exact command; any newer draft remains retained.')
        setEditMessageKind('status')
      } else {
        setEditError(inspected.kind === 'damaged'
          ? 'A damaged recovery record blocks this edit. Nothing was sent.'
          : recoveryStorageMessage)
        setEditMessageKind('error')
      }
      return
    }
    if (!commentOperationIntentStored(submitted.recovery)) {
      setEditError(recoveryChangedMessage)
      setEditMessageKind('error')
      return
    }
    const observing = checking && submitted.record?.id
      && commentCommandPending(submitted.record)
    let completed = false
    let retainNewerDraft = false
    setEditBusy('edit'); setEditError(''); setEditMessageKind('error')
    try {
      let terminalRecord
      if (observing) {
        terminalRecord = terminalCommentRecord(await getRunCommand(runId, submitted.record.id))
      } else if (retrying) {
        terminalRecord = await retryCommentCommand(runId, submitted.record)
      } else {
        terminalRecord = terminalCommentRecord(await CONTROL.editComment(runId, {
          commentId: comment.id, nodeId: comment.nodeId, nodeGeneration: comment.nodeGeneration,
          expectedVersion: submitted.version, text: submitted.text,
        }, mutationOptions(expectedGeneration, submitted.idempotencyKey)))
      }
      retainNewerDraft = normalizedDraft !== submitted.text
      restoreEditFocus()
      onAnnounce?.(`Comment on experiment #${comment.nodeId} updated.`)
      onRefresh?.()
      if (!clearCommentOperationIntent(submitted.recovery)) {
        setUncertainEdit({ ...submitted, record: terminalRecord, unknown: false })
        setEditError('The edit completed, but its saved recovery record could not be cleared. Check the same command to clean it up safely.')
        setEditMessageKind('status')
        return
      }
      completed = true
    } catch (caught) {
      const record = retryableCommentRecord(caught, submitted.record?.id)
      const unknown = !record && (commentCommandOutcomeUnknown(caught)
        || (checking && commandRecoveryOutcomeUnresolved(caught)))
      const releaseFailed = !record && !unknown
        && !clearCommentOperationIntent(submitted.recovery)
      setEditRetryIntent(record ? { ...submitted, record, unknown: false } : null)
      setUncertainEdit(unknown || releaseFailed
        ? { ...submitted, record: recoveryCommentRecord(caught, submitted.record),
            unknown: caught?.commandUnknown === true }
        : null)
      if (commentConflict(caught)) setEditConflictVersion(comment.version)
      const message = commentMutationError(caught, 'Comment could not be updated. Your draft is preserved.')
      setEditError(releaseFailed
        ? `${message} Its saved recovery record could not be cleared; check the same command before saving again.`
        : message)
      setEditMessageKind('error')
    } finally {
      if (completed && !retainNewerDraft) {
        draftStore.clear(editScope)
      } else {
        if (completed) {
          setUncertainEdit(null)
          setEditRetryIntent(null)
          setEditConflictVersion(submitted.version)
          setEditError('The earlier edit completed. Review this newer draft against the latest comment.')
          setEditMessageKind('status')
        }
        setEditBusy('')
      }
    }
  }

  const changeResolution = async resolved => {
    const checking = uncertainResolution?.resolved === resolved
      && uncertainResolution?.version === comment.version
    const retrying = !checking && resolutionRetryIntent?.resolved === resolved
      && resolutionRetryIntent?.version === comment.version
    if (busy || hasEditDraft || resolutionConflictVersion != null || resolutionIntentMismatch
        || resolutionRecovery.damaged || resolutionRecovery.storageUnavailable
        || (!checking && !retrying && !canMutate)) return
    const submitted = checking
      ? uncertainResolution
      : retrying
        ? resolutionRetryIntent
        : createStoredCommentSubmission({
            ...resolutionRecoveryIdentity, expectedVersion: comment.version,
            text: null, resolved,
          })
    if (!submitted) {
      const inspected = resolutionRecovery.inspect()
      if (inspected.kind === 'valid') {
        retainInspectedResolution(inspected)
        setResolutionError('Another saved operation already owns this resolution change. Check that exact command before acting again.')
      } else setResolutionError(inspected.kind === 'damaged'
        ? 'A damaged recovery record blocks this resolution change. Nothing was sent.'
        : recoveryStorageMessage)
      return
    }
    if (!commentOperationIntentStored(submitted.recovery)) {
      setResolutionError(recoveryChangedMessage)
      return
    }
    const observing = checking && submitted.record?.id
      && commentCommandPending(submitted.record)
    let completed = false
    setResolutionBusy('resolution'); setResolutionError('')
    try {
      let terminalRecord
      if (observing) {
        terminalRecord = terminalCommentRecord(await getRunCommand(runId, submitted.record.id))
      } else if (retrying) {
        terminalRecord = await retryCommentCommand(runId, submitted.record)
      } else {
        terminalRecord = terminalCommentRecord(await CONTROL.setCommentResolved(runId, {
          commentId: comment.id, nodeId: comment.nodeId, nodeGeneration: comment.nodeGeneration,
          expectedVersion: submitted.version, resolved: submitted.resolved,
        }, mutationOptions(expectedGeneration, submitted.idempotencyKey)))
      }
      onAnnounce?.(`${submitted.resolved ? 'Resolved' : 'Reopened'} comment on experiment #${comment.nodeId}.`)
      onRefresh?.()
      if (!clearCommentOperationIntent(submitted.recovery)) {
        setUncertainResolution({ ...submitted, record: terminalRecord, unknown: false })
        setResolutionError('The resolution change completed, but its saved recovery record could not be cleared. Check the same command to clean it up safely.')
        return
      }
      completed = true
    } catch (caught) {
      const record = retryableCommentRecord(caught, submitted.record?.id)
      const unknown = !record && (commentCommandOutcomeUnknown(caught)
        || (checking && commandRecoveryOutcomeUnresolved(caught)))
      const releaseFailed = !record && !unknown
        && !clearCommentOperationIntent(submitted.recovery)
      setResolutionRetryIntent(record
        ? { ...submitted, record, unknown: false }
        : null)
      setUncertainResolution(unknown || releaseFailed
        ? { ...submitted, record: recoveryCommentRecord(caught, submitted.record),
            unknown: caught?.commandUnknown === true }
        : null)
      if (commentConflict(caught)) setResolutionConflictVersion(comment.version)
      const message = commentMutationError(caught,
        submitted.resolved ? 'Comment could not be resolved. The requested state is preserved.'
          : 'Comment could not be reopened. The requested state is preserved.')
      setResolutionError(releaseFailed
        ? `${message} Its saved recovery record could not be cleared; check the same command before changing resolution again.`
        : message)
    } finally {
      if (completed) draftStore.clear(resolutionScope)
      else setResolutionBusy('')
    }
  }

  const copyDraft = async () => {
    try {
      await navigator.clipboard.writeText(draftText)
      onAnnounce?.('Draft copied.')
    } catch {
      editorRef.current?.focus()
      editorRef.current?.select()
      onAnnounce?.('Clipboard is unavailable. The draft is selected for manual copying.')
    }
  }
  const copyPendingEdit = async () => {
    try {
      await navigator.clipboard.writeText(uncertainEdit?.text || '')
      onAnnounce?.('Pending comment edit copied.')
    } catch {
      onAnnounce?.('Clipboard is unavailable. The pending edit remains visible below.')
    }
  }

  return <article id={commentDomId} data-comment-id={comment.id} tabIndex={-1}
    className={'comment-card' + (comment.resolved ? ' resolved' : '') + (focused ? ' focused' : '')}
    aria-label={`Comment on experiment ${comment.nodeId} by ${comment.actorLabel}`}>
    <header className="comment-card-head">
      {global && (comment.legacy
        ? <span className="comment-node-label">Experiment #{comment.nodeId} · attempt unknown</span>
        : <button type="button" className="btn xs ghost comment-node-link"
            onClick={() => onOpenComment?.(comment)}>
            Experiment #{comment.nodeId} · attempt {comment.nodeGeneration}
          </button>)}
      <span className="comment-actor"><OpIcon name={comment.actorKind === 'assistant' ? 'bot' : 'user'} size={12} /> {comment.actorLabel}</span>
      <time dateTime={new Date(comment.updatedAt * 1000).toISOString()}
        title={`${comment.updatedAt === comment.createdAt ? 'Created' : 'Updated'} ${fmtDate(comment.updatedAt)}`}>
        {fmtAgo(comment.updatedAt)}
      </time>
      {comment.version > 1 && <span className="muted">edited</span>}
      {comment.resolved && <span className="pill ok">Resolved</span>}
    </header>

    {surfaceStale && <div className="notice warn compact" role="status">
      A newer comment version is open in another view. This copy is read-only until comments refresh.
    </div>}
    {editorVisible ? <div className="comment-editor">
      <label className="sr-only" htmlFor={`${commentDomId}-editor`}>Edit comment on experiment #{comment.nodeId}</label>
      <textarea ref={editorRef} id={`${commentDomId}-editor`} className="text" rows={4} maxLength={8192}
        value={draftText} disabled={busy || resolutionFence}
        aria-describedby={`${commentDomId}-editor-audit${resolutionError
          ? ` ${commentDomId}-resolution-error` : ''}`}
        autoFocus={focusEditorRef.current}
        onFocus={() => { focusEditorRef.current = false }}
        onChange={event => {
          const next = event.target.value
          setDraftText(next)
          setDirty(next.trim() !== editBaseText)
        }}
        onKeyDown={event => {
          if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); save() }
          if (event.key === 'Escape') {
            event.preventDefault()
            if (editBusy || editOutcomeUnknown || editRetryIntent) {
              onAnnounce?.(editBusy
                ? 'Wait for the current edit request to finish before closing this draft.'
                : 'Resolve the saved edit command before closing this draft.')
            } else {
              draftStore.clear(editScope)
              restoreEditFocus()
            }
          }
        }} />
      <div id={`${commentDomId}-editor-audit`} className="comment-editor-audit" role="note">
        Saving creates a new audit version. Prior text remains in the run log and backups.
      </div>
      <div className="comment-editor-meta"><span className="muted">Plain text · Esc cancels</span><DraftCounter draft={draft} /></div>
      {editRecovery.storageUnavailable && <div className="notice resource-error comment-inline-error" role="alert">
        <span>{recoveryStorageMessage}</span>
        <button type="button" className="btn xs" onClick={() => {
          const inspected = editRecovery.inspect()
          if (inspected.kind === 'none') {
            setEditError('Recovery storage is available again. You can save this draft.')
            setEditMessageKind('status')
          } else if (inspected.kind === 'valid') {
            retainInspectedEdit(inspected)
            setEditError('A saved edit operation was restored. Check that exact command; any newer draft remains retained.')
            setEditMessageKind('status')
          }
        }}>Retry storage</button>
      </div>}
      {editRecovery.damaged && <div className="notice resource-error comment-inline-error" role="alert">
        <span>A saved edit recovery record is invalid. Editing is blocked until that browser-only record is discarded.</span>
        <button type="button" className="btn xs" onClick={() => {
          if (editRecovery.discardDamaged()) {
            setEditError('Damaged edit recovery discarded. Review this draft against the current comment.')
            setEditMessageKind('status')
          } else setEditError('The damaged edit recovery changed and was not discarded. Refresh Comments.')
        }}>Discard damaged recovery</button>
      </div>}
      {editError && <div
        className={`notice comment-inline-error ${editMessageKind === 'status' ? 'warn' : 'resource-error'}`}
        role={editMessageKind === 'status' ? 'status' : 'alert'}><span>{editError}</span></div>}
      {versionChanged && <div className="comment-recovery-panel">
        <details>
          <summary>View latest comment</summary>
          <div className="comment-recovery-payload">{comment.text}</div>
        </details>
        <div className="comment-recovery-actions">
          <button type="button" className="btn xs" disabled={busy || editOutcomeUnknown} onClick={() => {
            setDraftText(comment.text)
            setEditBaseVersion(comment.version)
            setEditBaseText(comment.text)
            setDirty(false)
            setEditConflictVersion(null)
            setEditError('')
          }}>Use latest</button>
          <button type="button" className="btn xs" onClick={copyDraft}>Copy my draft</button>
          <button type="button" className="btn xs" disabled={busy || editOutcomeUnknown} onClick={() => {
            setEditBaseVersion(comment.version)
            setEditBaseText(comment.text)
            setDirty(normalizedDraft !== comment.text)
            setEditConflictVersion(null)
            setEditError('')
            onAnnounce?.('Latest comment version acknowledged. Your draft remains in the editor.')
          }}>Continue with my draft</button>
        </div>
      </div>}
      {editConflictVersion != null && <div className="comment-recovery-panel">
        <div className="comment-recovery-actions">
          <button type="button" className="btn xs" onClick={onRefresh}>Reload current</button>
          <button type="button" className="btn xs" onClick={copyDraft}>Copy my draft</button>
        </div>
      </div>}
      {editOutcomeUnknown && <div className="comment-recovery-panel">
        <details>
          <summary>View edit submission to check</summary>
          <div className="comment-recovery-payload">{uncertainEdit.text}</div>
        </details>
        <div className="comment-recovery-actions">
          <button type="button" className="btn xs" onClick={onRefresh}>Refresh comments</button>
          <button type="button" className="btn xs" onClick={copyPendingEdit}>Copy pending edit</button>
          <button type="button" className="btn xs ghost" disabled={!!editBusy}
            title="Only discard this after confirming that the edit was not applied"
            onClick={() => {
              if (!clearCommentOperationIntent(uncertainEdit.recovery)) {
                setEditError('The saved edit recovery changed and was not discarded. Refresh Comments.')
                return
              }
              setUncertainEdit(null)
              setEditError('Pending edit discarded. Review your draft against the current comment.')
              setEditMessageKind('status')
            }}>Discard pending edit</button>
        </div>
      </div>}
      {editRetryMismatch && <div className="comment-recovery-panel" role="status">
        <span>The saved failed edit belongs to different text or an older version. Restore its text to retry when valid, or discard that terminal failure.</span>
        <div className="comment-recovery-actions">
          <button type="button" className="btn xs" onClick={() => setDraftText(editRetryIntent.text)}>
            Restore failed edit
          </button>
          <button type="button" className="btn xs ghost" onClick={() => {
            if (!clearCommentOperationIntent(editRetryIntent.recovery)) {
              setEditError('The saved failed-edit record changed and was not discarded. Refresh Comments.')
              return
            }
            setEditRetryIntent(null)
            setEditError('Failed edit command discarded. Review this draft before saving a new edit.')
            setEditMessageKind('status')
          }}>Discard failed edit</button>
        </div>
      </div>}
      <div className="comment-editor-actions">
        <button type="button" className="btn sm ghost"
          disabled={!!editBusy || editOutcomeUnknown || !!editRetryIntent}
          title={editOutcomeUnknown || editRetryIntent
            ? 'Resolve the saved edit command before closing this draft' : undefined}
          onClick={() => {
            if (editBusy || editOutcomeUnknown || editRetryIntent) return
            draftStore.clear(editScope)
            restoreEditFocus()
          }}>Cancel</button>
        <button type="button" className="btn sm primary"
          disabled={busy || resolutionFence || editRecoveryBlocked || editRetryMismatch
            || (!editOutcomeUnknown && (editConflictVersion != null || editIntentMismatch
              || !draft.valid || !draftChanged || versionChanged))}
          title={exactEditRetry ? 'Retry this exact durable command; no new edit intent is created'
            : editRetryMismatch ? 'Resolve the saved failed edit before saving new text'
              : editRecoveryBlocked ? 'Working recovery storage is required before saving' : undefined}
          onClick={save}>{editBusy === 'edit'
            ? editOutcomeUnknown ? 'Checking…' : exactEditRetry ? 'Retrying…' : 'Saving…'
            : editOutcomeUnknown ? 'Check command'
              : exactEditRetry ? 'Retry same command' : 'Save comment'}</button>
      </div>
    </div> : <div className="comment-text">{comment.text}</div>}

    {!editorVisible && editRecovery.storageUnavailable && <div className="notice resource-error comment-inline-error" role="alert">
      <span>Edit recovery storage is unavailable. Editing is blocked.</span>
      <button type="button" className="btn xs" onClick={retryEditRecoveryStorage}>Retry storage</button>
    </div>}
    {!editorVisible && editRecovery.damaged && <div className="notice resource-error comment-inline-error" role="alert">
      <span>A saved edit recovery record is invalid. Editing is blocked.</span>
      <button type="button" className="btn xs" onClick={() => {
        if (!editRecovery.discardDamaged()) {
          setEditError('The damaged edit recovery changed and was not discarded. Refresh Comments.')
        }
      }}>Discard damaged recovery</button>
    </div>}
    {resolutionRecovery.storageUnavailable && <div className="notice resource-error comment-inline-error" role="alert">
      <span>Resolution recovery storage is unavailable. Resolve/reopen is blocked.</span>
      <button type="button" className="btn xs" onClick={retryResolutionRecoveryStorage}>Retry storage</button>
    </div>}
    {resolutionRecovery.damaged && <div className="notice resource-error comment-inline-error" role="alert">
      <span>A saved resolution recovery record is invalid. Resolve/reopen is blocked.</span>
      <button type="button" className="btn xs" onClick={() => {
        if (!resolutionRecovery.discardDamaged()) {
          setResolutionError('The damaged resolution recovery changed and was not discarded. Refresh Comments.')
        }
      }}>Discard damaged recovery</button>
    </div>}

    {resolutionError && <div id={`${commentDomId}-resolution-error`}
      className="notice resource-error comment-inline-error"
      role="alert"><span>{resolutionError}</span></div>}
    {(resolutionConflictVersion != null || resolutionOutcomeUnknown || resolutionIntentMismatch) &&
      <div className="comment-recovery-panel">
        <div className="comment-recovery-actions">
        <button type="button" className="btn xs" onClick={onRefresh}>Refresh comments</button>
        {(uncertainResolution || resolutionRetryIntent) && <button type="button" className="btn xs ghost"
          title="Discard only after reviewing the current comment state"
          onClick={() => {
            const intent = uncertainResolution || resolutionRetryIntent
            if (!clearCommentOperationIntent(intent.recovery)) {
              setResolutionError('The saved resolution recovery changed and was not discarded. Refresh Comments.')
              return
            }
            setUncertainResolution(null)
            setResolutionRetryIntent(null)
            setResolutionConflictVersion(null)
            setResolutionError('Saved resolution command discarded. Review the current state before acting again.')
          }}>Discard saved resolution command</button>}
        </div>
    </div>}
    <footer className="comment-card-actions">
      {canMutate && !editorVisible && <>
        <button ref={editButtonRef} type="button" className="btn xs ghost"
          disabled={busy || resolutionFence || editRecoveryBlocked}
          onClick={() => {
            if (!dirty && !editRetryIntent && !uncertainEdit) {
              setDraftText(comment.text); setEditBaseText(comment.text)
              setEditBaseVersion(comment.version); setEditError(''); setEditConflictVersion(null)
              setEditRetryIntent(null); setUncertainEdit(null)
            }
            focusEditorRef.current = true
            setEditing(true)
          }}>
          <OpIcon name="pencil" size={11} /> {dirty || editRetryIntent || uncertainEdit ? 'Resume edit' : 'Edit'}
        </button>
        <button type="button" className="btn xs ghost"
          disabled={busy || hasEditDraft || resolutionConflictVersion != null
            || resolutionIntentMismatch || resolutionRecovery.damaged
            || resolutionRecovery.storageUnavailable}
          title={exactResolutionRetry ? 'Retry this exact durable command; no new resolution intent is created' : undefined}
          onClick={() => changeResolution(resolutionTarget)}>
          <OpIcon name={comment.resolved ? 'replay' : 'check'} size={11} />
          {resolutionBusy === 'resolution'
            ? resolutionOutcomeUnknown ? 'Checking…' : exactResolutionRetry ? 'Retrying…' : 'Applying…'
            : resolutionOutcomeUnknown ? 'Check command'
              : exactResolutionRetry ? `Retry ${resolutionTarget ? 'resolve' : 'reopen'}`
                : comment.resolved ? 'Reopen' : 'Resolve'}
        </button>
      </>}
      {canViewHistory && !editorVisible && <History
        key={`${runId}:${expectedGeneration || 'unknown'}:${comment.id}:${comment.version}`}
        runId={runId} comment={comment}
        expectedGeneration={expectedGeneration} onAnnounce={onAnnounce}
        domPrefix={commentDomId} />}
      {comment.legacy && <span className="muted comment-legacy-note">Legacy notes are read-only.</span>}
      {!readOnly && !comment.legacy && !comment.editable &&
        <span className="muted comment-legacy-note">This comment is read-only. Its audit history remains available.</span>}
    </footer>
  </article>
}

export default function CommentsThread({
  runId,
  nodeId = null,
  nodeGeneration = null,
  expectedGeneration,
  refreshKey = null,
  readOnly = false,
  reviewMode = false,
  focusCommentId = null,
  onOpenComment = null,
  global = false,
  draftStore: sharedDraftStore = null,
  draftSurface = global ? 'global' : 'inspector',
}) {
  // Review mode is an authority boundary even if a future caller forgets the redundant readOnly
  // prop. The request layer also rejects mutations, but controls/history must never be rendered.
  const immutable = readOnly || reviewMode
  const fallbackDraftStoreRef = useRef(null)
  if (!fallbackDraftStoreRef.current) fallbackDraftStoreRef.current = createInspectorDraftStore()
  // Public reviews must not inspect, render, or mutate owner-only in-memory recovery even if a caller
  // accidentally reuses the owner's RunView store while switching authority modes in the same tab.
  const draftStore = immutable
    ? fallbackDraftStoreRef.current
    : sharedDraftStore || fallbackDraftStoreRef.current
  const threadDraftScope = `comments-thread:${draftSurface}:${runId}@${expectedGeneration || '?'}:${global
    ? 'global' : `${nodeId}:${nodeGeneration ?? '?'}`}`
  const [filter, setFilter] = useInspectorDraftField(
    draftStore, threadDraftScope, 'filter', immutable ? 'all' : 'open', { disposable: true })
  const [announcement, setAnnouncement] = useState('')
  const feed = useComments({
    runId,
    nodeId,
    nodeGeneration,
    expectedGeneration,
    includeResolved: true,
    enabled: !!runId && (global || nodeId != null),
    refreshKey,
  })
  const visible = useMemo(() => filterComments(feed.comments, filter), [feed.comments, filter])
  const counts = useMemo(() => ({
    open: feed.comments.filter(comment => !comment.resolved).length,
    resolved: feed.comments.filter(comment => comment.resolved).length,
    all: feed.comments.length,
  }), [feed.comments])

  useEffect(() => {
    if (!focusCommentId) return
    const target = feed.comments.find(comment => comment.id === focusCommentId)
    if (target?.resolved && filter === 'open') setFilter('all')
    const frame = requestAnimationFrame(() => {
      const element = document.getElementById(domId(focusCommentId, draftSurface))
      if (!element) return
      element.scrollIntoView?.({ block: 'center' })
      element.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [draftSurface, focusCommentId, feed.comments, filter])

  const hasExactGeneration = /^[0-9a-f]{64}$/.test(expectedGeneration || '')
  return <section className={'comments-thread' + (global ? ' global' : '')}
    aria-label={global ? 'Run comments' : `Comments for experiment ${nodeId}`}
    aria-busy={feed.loading || feed.refreshing ? 'true' : 'false'}>
    <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
    {reviewMode && <div className="notice comment-review-note" role="note">
      <b>Read-only comments.</b> Comments are attributed to generic run actors; LoopLab does not identify individual people.
    </div>}
    {!immutable && !global && hasExactGeneration && Number.isSafeInteger(nodeGeneration)
      // KEYED so a node switch remounts it. Inspector swaps `nodeId` on this same element position,
      // so without a key the composer's `retryIntent` survives into the new node — and `exactRetry`
      // matches on draft TEXT alone, then replays the durable record.id. Node A's comment posted
      // while the announcement named node B.
      && <CommentComposer key={`${runId}:${nodeId}:${nodeGeneration}:${expectedGeneration}`}
        runId={runId} nodeId={nodeId} nodeGeneration={nodeGeneration}
        expectedGeneration={expectedGeneration} onRefresh={feed.refresh} onAnnounce={setAnnouncement}
        draftStore={draftStore}
        draftScope={`comment-composer:${runId}@${expectedGeneration}:${nodeId}:${nodeGeneration}`} />}

    <div className="comment-filter-bar" role="group" aria-label="Filter comments">
      {[
        ['open', 'Open'], ['resolved', 'Resolved'], ['all', 'All'],
      ].map(([key, label]) => <button type="button" key={key} className="btn sm ghost"
        aria-pressed={filter === key} onClick={() => setFilter(key)}>{label} <span>{counts[key]}</span></button>)}
      {feed.refreshing && <span className="muted" role="status">Refreshing…</span>}
    </div>

    {feed.loading && <div className="notice" role="status">Loading comments…</div>}
    {feed.error && <div className={'notice resource-error comment-feed-error' + (feed.stale ? ' stale' : '')}>
      <span role={feed.stale ? 'status' : 'alert'}>
        {feed.stale ? 'Showing the last received comments. ' : ''}{feed.error}
      </span>
      <button type="button" className="btn sm" onClick={feed.refresh}>Retry</button>
    </div>}
    {!feed.loading && feed.initialized && !feed.error && feed.comments.length === 0
      && <div className="comments-empty muted">{immutable
        ? 'No comments are available in this review.'
        : global ? 'No comments yet. Add one from an experiment’s Comments tab.' : 'No comments on this experiment yet.'}</div>}
    {!feed.loading && feed.comments.length > 0 && visible.length === 0
      && <div className="comments-empty muted">No {filter} comments.</div>}

    <div className="comment-list">
      {visible.map(comment => <CommentCard key={comment.id} runId={runId} comment={comment}
        expectedGeneration={expectedGeneration} readOnly={immutable} global={global}
        focused={focusCommentId === comment.id} onOpenComment={onOpenComment}
        onRefresh={feed.refresh} onAnnounce={setAnnouncement} draftStore={draftStore}
        draftScope={`comment-card:${runId}@${expectedGeneration || '?'}:${comment.nodeId}:${comment.nodeGeneration ?? '?'}:${comment.id}`}
        draftSurface={draftSurface} />)}
    </div>
    {feed.loadMoreError && <div className="notice resource-error comment-feed-error">
      <span role="alert">{feed.loadMoreError}</span>
      <button type="button" className="btn sm" onClick={feed.loadMore}>Retry</button>
    </div>}
    {feed.hasMore && <button type="button" className="btn sm comment-load-more"
      disabled={feed.loadingMore} onClick={feed.loadMore}>{feed.loadingMore ? 'Loading…' : 'Load older comments'}</button>}
  </section>
}
