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
  const [text, setText] = useInspectorDraftField(draftStore, draftScope, 'text', '')
  const [busy, setBusy] = useInspectorDraftField(draftStore, draftScope, 'busy', false)
  const [error, setError] = useInspectorDraftField(draftStore, draftScope, 'error', '')
  const [retryIntent, setRetryIntent] = useInspectorDraftField(
    draftStore, draftScope, 'retryIntent', null)
  const [uncertainIntent, setUncertainIntent] = useInspectorDraftField(
    draftStore, draftScope, 'uncertainIntent', null)
  const [messageKind, setMessageKind] = useInspectorDraftField(
    draftStore, draftScope, 'messageKind', 'error')
  const draft = useMemo(() => commentDraftState(text), [text])
  const normalizedText = text.trim()
  // The owner below keys this composer by run + node + node attempt + run generation, so retry and
  // unknown-outcome state cannot survive any mutation-scope change.
  const exactRetry = retryIntent?.text === normalizedText
  // Creating a comment is append-only. Once one submission has an unknown outcome, changing or
  // clearing the textarea must not silently permit a second POST that could create a duplicate.
  const outcomeUnknown = uncertainIntent != null
  const copyPendingSubmission = async () => {
    try {
      await navigator.clipboard.writeText(uncertainIntent?.text || '')
      onAnnounce?.('Pending comment submission copied.')
    } catch {
      onAnnounce?.('Clipboard is unavailable. The pending submission remains visible below.')
    }
  }

  const submit = async event => {
    event?.preventDefault?.()
    if (busy || (!outcomeUnknown && !draft.valid)) return
    const checking = outcomeUnknown
    const retrying = !checking && exactRetry
    const submission = outcomeUnknown
      ? uncertainIntent
      : retrying
        ? retryIntent
        : {
            text: normalizedText,
            idempotencyKey: createIdempotencyKey(),
            record: null,
            unknown: false,
          }
    const observing = outcomeUnknown && submission.record?.id
      && commentCommandPending(submission.record)
    let completed = false
    let retainNewerDraft = false
    setBusy(true)
    setError('')
    setMessageKind('error')
    try {
      if (observing) {
        terminalCommentRecord(await getRunCommand(runId, submission.record.id))
      } else if (retrying) {
        await retryCommentCommand(runId, submission.record)
      } else {
        terminalCommentRecord(await CONTROL.createComment(runId, {
          nodeId, nodeGeneration, text: submission.text,
        }, mutationOptions(expectedGeneration, submission.idempotencyKey)))
      }
      completed = true
      retainNewerDraft = normalizedText !== submission.text
      onAnnounce?.(`Comment added to experiment #${nodeId}.`)
      onRefresh?.()
    } catch (caught) {
      const record = retryableCommentRecord(caught, submission.record?.id)
      const unknown = !record && (commentCommandOutcomeUnknown(caught)
        || (checking && commandRecoveryOutcomeUnresolved(caught)))
      const sameDraft = normalizedText === submission.text
      setRetryIntent(record && sameDraft ? { ...submission, record, unknown: false } : null)
      setUncertainIntent(unknown
        ? { ...submission, record: recoveryCommentRecord(caught, submission.record),
            unknown: caught?.commandUnknown === true }
        : null)
      setError(commentMutationError(caught, 'Comment could not be added. Your draft is preserved.'))
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
          draftStore.clear(draftScope)
          return
        }
        setText(next)
        if (retryIntent && next.trim() !== retryIntent.text) setRetryIntent(null)
      }}
      onKeyDown={event => {
        if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') submit(event)
      }} />
    <div className="comment-composer-meta">
      <span id={`${fieldId}-hint`} className="muted">Plain text · Ctrl/⌘+Enter posts · visible in read-only review links after redaction</span>
      <span id={`${fieldId}-count`}><DraftCounter draft={draft} /></span>
    </div>
    {error && <div id={`${fieldId}-error`}
      className={`notice comment-inline-error ${messageKind === 'status' ? 'warn' : 'resource-error'}`}
      role={messageKind === 'status' ? 'status' : 'alert'}><span>{error}</span></div>}
    {outcomeUnknown && <div className="comment-recovery-panel"
      aria-label="Uncertain comment submission recovery">
      <details>
        <summary>View submission to check</summary>
        <div className="comment-recovery-payload">{uncertainIntent.text}</div>
      </details>
      <div className="comment-recovery-actions">
        <button type="button" className="btn sm" onClick={onRefresh}>Refresh comments</button>
        <button type="button" className="btn sm" onClick={copyPendingSubmission}>
          Copy pending submission
        </button>
        <button type="button" className="btn sm ghost" disabled={busy}
          title="Only discard this after confirming that the comment was not posted"
          onClick={() => {
            if (busy) return
            onAnnounce?.('The uncertain comment submission was discarded. Review the thread before posting again.')
            if (!text.trim() && !retryIntent) draftStore.clear(draftScope)
            else {
              setUncertainIntent(null)
              setError('')
            }
          }}>Discard pending submission</button>
      </div>
    </div>}
    <div className="comment-composer-actions">
      <button type="submit" className="btn sm primary"
        disabled={busy || (!outcomeUnknown && !draft.valid)}
        title={exactRetry ? 'Retry this exact durable command; no new comment intent is created' : undefined}>
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
  const [dirty, setDirty] = useInspectorDraftField(draftStore, editScope, 'dirty', false)
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
  // A clean editor in the other surface is still active work. Block resolution globally so it cannot
  // disable an unseen Inspector/Collab editor with a command the operator cannot recover there.
  const hasEditDraft = inspectorEditing || collabEditing
    || dirty || !!editRetryIntent || !!uncertainEdit
  const resolutionFence = resolutionConflictVersion != null
    || !!resolutionRetryIntent || !!uncertainResolution
  const busy = !!editBusy || !!resolutionBusy
  const editorVisible = editing && canMutate
  const restoreEditFocus = () => requestAnimationFrame(() => editButtonRef.current?.focus())

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
    if (surfaceStale) return
    if (editRetryIntent && comment.version > editRetryIntent.version) setEditRetryIntent(null)
    if (uncertainEdit && comment.version > uncertainEdit.version) setUncertainEdit(null)
    if (resolutionRetryIntent && comment.version > resolutionRetryIntent.version) {
      setResolutionRetryIntent(null)
      setResolutionError('')
    }
    if (uncertainResolution && comment.version > uncertainResolution.version) {
      setUncertainResolution(null)
      setResolutionError('')
    }
  }, [comment.version, comment.resolved, editRetryIntent, uncertainEdit,
    resolutionRetryIntent, uncertainResolution, surfaceStale])

  useEffect(() => {
    if (surfaceStale || !versionChanged) return
    if (!dirty && !editRetryIntent && !uncertainEdit) {
      setDraftText(comment.text)
      setEditBaseText(comment.text)
      setEditBaseVersion(comment.version)
      setEditError('')
      return
    }
    setEditRetryIntent(null)
    setUncertainEdit(null)
    setEditError('This comment changed after your draft started. Review the latest version before saving.')
  }, [comment.text, comment.version, dirty, editRetryIntent, uncertainEdit,
    surfaceStale, versionChanged])

  const save = async () => {
    const checking = editOutcomeUnknown
    if (busy || resolutionFence || editIntentMismatch
        || (!checking && (!canMutate || editConflictVersion != null
        || !draft.valid || !draftChanged || versionChanged))) return
    const retrying = !checking && exactEditRetry
    const submitted = checking
      ? uncertainEdit
      : retrying
        ? editRetryIntent
        : {
            text: normalizedDraft,
            version: comment.version,
            idempotencyKey: createIdempotencyKey(),
            record: null,
            unknown: false,
          }
    const observing = checking && submitted.record?.id
      && commentCommandPending(submitted.record)
    let completed = false
    let retainNewerDraft = false
    setEditBusy('edit'); setEditError(''); setEditMessageKind('error')
    try {
      if (observing) {
        terminalCommentRecord(await getRunCommand(runId, submitted.record.id))
      } else if (retrying) {
        await retryCommentCommand(runId, submitted.record)
      } else {
        terminalCommentRecord(await CONTROL.editComment(runId, {
          commentId: comment.id, nodeId: comment.nodeId, nodeGeneration: comment.nodeGeneration,
          expectedVersion: submitted.version, text: submitted.text,
        }, mutationOptions(expectedGeneration, submitted.idempotencyKey)))
      }
      completed = true
      retainNewerDraft = normalizedDraft !== submitted.text
      restoreEditFocus()
      onAnnounce?.(`Comment on experiment #${comment.nodeId} updated.`)
      onRefresh?.()
    } catch (caught) {
      const record = retryableCommentRecord(caught, submitted.record?.id)
      const unknown = !record && (commentCommandOutcomeUnknown(caught)
        || (checking && commandRecoveryOutcomeUnresolved(caught)))
      const sameDraft = normalizedDraft === submitted.text
      setEditRetryIntent(record && sameDraft
        ? { ...submitted, record, unknown: false }
        : null)
      setUncertainEdit(unknown
        ? { ...submitted, record: recoveryCommentRecord(caught, submitted.record),
            unknown: caught?.commandUnknown === true }
        : null)
      if (commentConflict(caught)) setEditConflictVersion(comment.version)
      setEditError(commentMutationError(caught, 'Comment could not be updated. Your draft is preserved.'))
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
        || (!checking && !retrying && !canMutate)) return
    const submitted = checking
      ? uncertainResolution
      : retrying
        ? resolutionRetryIntent
        : {
            resolved,
            version: comment.version,
            idempotencyKey: createIdempotencyKey(),
            record: null,
            unknown: false,
          }
    const observing = checking && submitted.record?.id
      && commentCommandPending(submitted.record)
    let completed = false
    setResolutionBusy('resolution'); setResolutionError('')
    try {
      if (observing) {
        terminalCommentRecord(await getRunCommand(runId, submitted.record.id))
      } else if (retrying) {
        await retryCommentCommand(runId, submitted.record)
      } else {
        terminalCommentRecord(await CONTROL.setCommentResolved(runId, {
          commentId: comment.id, nodeId: comment.nodeId, nodeGeneration: comment.nodeGeneration,
          expectedVersion: submitted.version, resolved: submitted.resolved,
        }, mutationOptions(expectedGeneration, submitted.idempotencyKey)))
      }
      completed = true
      onAnnounce?.(`${submitted.resolved ? 'Resolved' : 'Reopened'} comment on experiment #${comment.nodeId}.`)
      onRefresh?.()
    } catch (caught) {
      const record = retryableCommentRecord(caught, submitted.record?.id)
      const unknown = !record && (commentCommandOutcomeUnknown(caught)
        || (checking && commandRecoveryOutcomeUnresolved(caught)))
      setResolutionRetryIntent(record
        ? { ...submitted, record, unknown: false }
        : null)
      setUncertainResolution(unknown
        ? { ...submitted, record: recoveryCommentRecord(caught, submitted.record),
            unknown: caught?.commandUnknown === true }
        : null)
      if (commentConflict(caught)) setResolutionConflictVersion(comment.version)
      setResolutionError(commentMutationError(caught,
        submitted.resolved ? 'Comment could not be resolved. The requested state is preserved.'
          : 'Comment could not be reopened. The requested state is preserved.'))
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
          if (editRetryIntent && next.trim() !== editRetryIntent.text) setEditRetryIntent(null)
        }}
        onKeyDown={event => {
          if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); save() }
          if (event.key === 'Escape') {
            event.preventDefault()
            if (editBusy || editOutcomeUnknown) {
              onAnnounce?.(editBusy
                ? 'Wait for the current edit request to finish before closing this draft.'
                : 'Check the pending edit before closing this draft.')
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
        </div>
      </div>}
      <div className="comment-editor-actions">
        <button type="button" className="btn sm ghost" disabled={!!editBusy || editOutcomeUnknown}
          title={editOutcomeUnknown ? 'Check the pending edit before closing this draft' : undefined}
          onClick={() => {
            if (editBusy || editOutcomeUnknown) return
            draftStore.clear(editScope)
            restoreEditFocus()
          }}>Cancel</button>
        <button type="button" className="btn sm primary"
          disabled={busy || resolutionFence || (!editOutcomeUnknown
            && (editConflictVersion != null || editIntentMismatch
              || !draft.valid || !draftChanged || versionChanged))}
          title={exactEditRetry ? 'Retry this exact durable command; no new edit intent is created' : undefined}
          onClick={save}>{editBusy === 'edit'
            ? editOutcomeUnknown ? 'Checking…' : exactEditRetry ? 'Retrying…' : 'Saving…'
            : editOutcomeUnknown ? 'Check command'
              : exactEditRetry ? 'Retry same command' : 'Save comment'}</button>
      </div>
    </div> : <div className="comment-text">{comment.text}</div>}

    {resolutionError && <div id={`${commentDomId}-resolution-error`}
      className="notice resource-error comment-inline-error"
      role="alert"><span>{resolutionError}</span></div>}
    {(resolutionConflictVersion != null || resolutionOutcomeUnknown) &&
      <div className="comment-recovery-panel">
        <div className="comment-recovery-actions">
        <button type="button" className="btn xs" onClick={onRefresh}>Refresh comments</button>
        </div>
    </div>}
    <footer className="comment-card-actions">
      {canMutate && !editorVisible && <>
        <button ref={editButtonRef} type="button" className="btn xs ghost"
          disabled={busy || resolutionFence}
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
          disabled={busy || hasEditDraft || resolutionConflictVersion != null || resolutionIntentMismatch}
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
  const draftStore = sharedDraftStore || fallbackDraftStoreRef.current
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
