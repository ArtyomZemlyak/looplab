import React, { useEffect, useId, useMemo, useRef, useState } from 'react'

import {
  COMMAND_FAILED, COMMAND_SUCCEEDED, CONTROL, commandCanRetry, commentHistory,
  createIdempotencyKey, retryRunCommand,
} from './api.js'
import {
  COMMENT_MAX_BYTES, commentConflict, commentDraftState, commentMutationError,
  filterComments, normalizeCommentHistory,
} from './commentsModel.js'
import { fmtAgo, fmtDate } from './format.js'
import { OpIcon } from './icons.jsx'
import { useComments } from './useComments.js'

const terminalCommentRecord = record => {
  if (record && COMMAND_SUCCEEDED.has(record.status)) return record
  if (record && COMMAND_FAILED.has(record.status)) {
    const error = new Error(record.error?.message || 'The comment command failed.')
    error.code = record.error?.code || 'comment_command_failed'
    error.detail = record.error || null
    error.commandRecord = record
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
const retryableCommentRecord = error => {
  if (commandCanRetry(error?.commandRecord)) return error.commandRecord
  const detail = error?.detail
  if (!COMMAND_ID_RE.test(String(error?.commandId || ''))
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

const mutationOptions = (expectedGeneration, idempotencyKey) => ({
  expectedGeneration,
  idempotencyKey,
  waitMs: 12_000,
})

const domId = id => `run-comment-${id}`

function DraftCounter({ draft }) {
  const invalid = draft.tooLarge || draft.invalidUnicode
  return <span className={'comment-byte-count' + (invalid ? ' over' : '')}
    aria-live={invalid ? 'polite' : 'off'}>
    {draft.invalidUnicode
      ? 'Unsupported Unicode sequence'
      : `${draft.bytes.toLocaleString()} / ${COMMENT_MAX_BYTES.toLocaleString()} bytes`}
  </span>
}

function CommentComposer({ runId, nodeId, nodeGeneration, expectedGeneration, onRefresh, onAnnounce }) {
  const fieldId = useId()
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [retryIntent, setRetryIntent] = useState(null)
  const [uncertainText, setUncertainText] = useState(null)
  const draft = useMemo(() => commentDraftState(text), [text])
  const normalizedText = text.trim()
  const exactRetry = retryIntent?.text === normalizedText
  const outcomeUnknown = uncertainText === normalizedText

  const submit = async event => {
    event?.preventDefault?.()
    if (busy || !draft.valid || outcomeUnknown) return
    const submittedText = normalizedText
    setBusy(true)
    setError('')
    try {
      if (exactRetry) await retryCommentCommand(runId, retryIntent.record)
      else terminalCommentRecord(await CONTROL.createComment(runId, {
        nodeId, nodeGeneration, text: submittedText,
      }, mutationOptions(expectedGeneration, createIdempotencyKey())))
      setText('')
      setRetryIntent(null)
      setUncertainText(null)
      onAnnounce?.(`Comment added to experiment #${nodeId}.`)
      onRefresh?.()
    } catch (caught) {
      const record = retryableCommentRecord(caught)
      if (record) setRetryIntent({ record, text: submittedText })
      else if (caught?.commandRecord) setRetryIntent(null)
      if (commentCommandOutcomeUnknown(caught) && !record) setUncertainText(submittedText)
      setError(commentMutationError(caught, 'Comment could not be added. Your draft is preserved.'))
    } finally { setBusy(false) }
  }

  return <form className="comment-composer" onSubmit={submit} aria-busy={busy ? 'true' : 'false'}>
    <label htmlFor={fieldId}>Add a comment to experiment #{nodeId}</label>
    <textarea id={fieldId} className="text" rows={4} value={text} disabled={busy} maxLength={8192}
      placeholder="Record a decision, question, or review note…"
      aria-describedby={`${fieldId}-hint ${fieldId}-count${error ? ` ${fieldId}-error` : ''}`}
      onChange={event => {
        const next = event.target.value
        setText(next)
        if (retryIntent && next.trim() !== retryIntent.text) setRetryIntent(null)
        if (uncertainText != null && next.trim() !== uncertainText) setUncertainText(null)
      }}
      onKeyDown={event => {
        if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') submit(event)
      }} />
    <div className="comment-composer-meta">
      <span id={`${fieldId}-hint`} className="muted">Plain text · Ctrl/⌘+Enter posts · visible in read-only review links after redaction</span>
      <span id={`${fieldId}-count`}><DraftCounter draft={draft} /></span>
    </div>
    {error && <div id={`${fieldId}-error`} className="notice resource-error comment-inline-error" role="alert">
      <span>{error}</span>
      {outcomeUnknown && <button type="button" className="btn sm" onClick={onRefresh}>Refresh comments</button>}
    </div>}
    <div className="comment-composer-actions">
      <button type="submit" className="btn sm primary" disabled={busy || !draft.valid || outcomeUnknown}
        title={exactRetry ? 'Retry this exact durable command; no new comment intent is created' : undefined}>
        <OpIcon name="chat" size={12} /> {busy ? (exactRetry ? 'Retrying…' : 'Posting…')
          : exactRetry ? 'Retry same command' : 'Post comment'}
      </button>
    </div>
  </form>
}

function History({ runId, comment, expectedGeneration, onAnnounce }) {
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
      aria-controls={`${domId(comment.id)}-history`}
      onClick={() => {
        const next = !open
        setOpen(next)
        if (next && status === 'idle') load()
      }}>
      {open ? 'Hide history' : `History (${comment.version})`}
    </button>
    {open && <div id={`${domId(comment.id)}-history`} className="comment-history-body">
      {status === 'loading' && <div className="muted" role="status">Loading history…</div>}
      {error && <div className="notice resource-error comment-inline-error" role="alert">
        <span>{error}</span><button type="button" className="btn xs" onClick={() => load()}>Retry</button>
      </div>}
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
  onOpenComment, onRefresh, onAnnounce,
}) {
  const [editing, setEditing] = useState(false)
  const [draftText, setDraftText] = useState(comment.text)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [conflictVersion, setConflictVersion] = useState(null)
  const [editRetryIntent, setEditRetryIntent] = useState(null)
  const [uncertainEdit, setUncertainEdit] = useState(null)
  const [resolutionRetryIntent, setResolutionRetryIntent] = useState(null)
  const [uncertainResolution, setUncertainResolution] = useState(null)
  const editorRef = useRef(null)
  const editButtonRef = useRef(null)
  const draft = useMemo(() => commentDraftState(draftText), [draftText])
  const normalizedDraft = draftText.trim()
  const draftChanged = normalizedDraft !== comment.text
  const canMutate = !readOnly && comment.editable && !comment.legacy
  const canViewHistory = !readOnly && !comment.legacy
  const exactEditRetry = editRetryIntent?.text === normalizedDraft
    && editRetryIntent?.version === comment.version
  const editOutcomeUnknown = uncertainEdit?.text === normalizedDraft
    && uncertainEdit?.version === comment.version
  const resolutionTarget = !comment.resolved
  const exactResolutionRetry = resolutionRetryIntent?.resolved === resolutionTarget
    && resolutionRetryIntent?.version === comment.version
  const resolutionOutcomeUnknown = uncertainResolution?.resolved === resolutionTarget
    && uncertainResolution?.version === comment.version
  const restoreEditFocus = () => requestAnimationFrame(() => editButtonRef.current?.focus())

  useEffect(() => {
    if (conflictVersion != null && comment.version !== conflictVersion) {
      setConflictVersion(null)
      setError('Latest version loaded. Your draft remains in the editor.')
    }
  }, [comment.version, conflictVersion])

  useEffect(() => {
    if (editRetryIntent && editRetryIntent.version !== comment.version) setEditRetryIntent(null)
    if (uncertainEdit && uncertainEdit.version !== comment.version) setUncertainEdit(null)
    if (resolutionRetryIntent && (resolutionRetryIntent.version !== comment.version
        || resolutionRetryIntent.resolved === comment.resolved)) setResolutionRetryIntent(null)
    if (uncertainResolution && (uncertainResolution.version !== comment.version
        || uncertainResolution.resolved === comment.resolved)) setUncertainResolution(null)
  }, [comment.version, comment.resolved, editRetryIntent, uncertainEdit,
    resolutionRetryIntent, uncertainResolution])

  const save = async () => {
    if (!canMutate || busy || !draft.valid || !draftChanged || editOutcomeUnknown) return
    const submitted = { text: normalizedDraft, version: comment.version }
    setBusy('edit'); setError('')
    try {
      if (exactEditRetry) await retryCommentCommand(runId, editRetryIntent.record)
      else terminalCommentRecord(await CONTROL.editComment(runId, {
        commentId: comment.id, nodeId: comment.nodeId, nodeGeneration: comment.nodeGeneration,
        expectedVersion: submitted.version, text: submitted.text,
      }, mutationOptions(expectedGeneration, createIdempotencyKey())))
      setEditRetryIntent(null); setUncertainEdit(null); setEditing(false); setConflictVersion(null)
      restoreEditFocus()
      onAnnounce?.(`Comment on experiment #${comment.nodeId} updated.`)
      onRefresh?.()
    } catch (caught) {
      const record = retryableCommentRecord(caught)
      if (record) setEditRetryIntent({ record, ...submitted })
      else if (caught?.commandRecord) setEditRetryIntent(null)
      if (commentCommandOutcomeUnknown(caught) && !record) setUncertainEdit(submitted)
      if (commentConflict(caught)) setConflictVersion(comment.version)
      setError(commentMutationError(caught, 'Comment could not be updated. Your draft is preserved.'))
    } finally { setBusy('') }
  }

  const changeResolution = async resolved => {
    if (!canMutate || busy || (uncertainResolution?.resolved === resolved
        && uncertainResolution?.version === comment.version)) return
    const submitted = { resolved, version: comment.version }
    const retrying = resolutionRetryIntent?.resolved === resolved
      && resolutionRetryIntent?.version === comment.version
    setBusy('resolution'); setError('')
    try {
      if (retrying) await retryCommentCommand(runId, resolutionRetryIntent.record)
      else terminalCommentRecord(await CONTROL.setCommentResolved(runId, {
        commentId: comment.id, nodeId: comment.nodeId, nodeGeneration: comment.nodeGeneration,
        expectedVersion: submitted.version, resolved,
      }, mutationOptions(expectedGeneration, createIdempotencyKey())))
      setResolutionRetryIntent(null); setUncertainResolution(null); setConflictVersion(null)
      onAnnounce?.(`${resolved ? 'Resolved' : 'Reopened'} comment on experiment #${comment.nodeId}.`)
      onRefresh?.()
    } catch (caught) {
      const record = retryableCommentRecord(caught)
      if (record) setResolutionRetryIntent({ record, ...submitted })
      else if (caught?.commandRecord) setResolutionRetryIntent(null)
      if (commentCommandOutcomeUnknown(caught) && !record) setUncertainResolution(submitted)
      if (commentConflict(caught)) setConflictVersion(comment.version)
      setError(commentMutationError(caught,
        resolved ? 'Comment could not be resolved. The requested state is preserved.'
          : 'Comment could not be reopened. The requested state is preserved.'))
    } finally { setBusy('') }
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

  return <article id={domId(comment.id)} data-comment-id={comment.id} tabIndex={-1}
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

    {editing ? <div className="comment-editor">
      <label className="sr-only" htmlFor={`${domId(comment.id)}-editor`}>Edit comment on experiment #{comment.nodeId}</label>
      <textarea ref={editorRef} id={`${domId(comment.id)}-editor`} className="text" rows={4} maxLength={8192}
        value={draftText} disabled={!!busy} autoFocus onChange={event => {
          const next = event.target.value
          setDraftText(next)
          if (editRetryIntent && next.trim() !== editRetryIntent.text) setEditRetryIntent(null)
          if (uncertainEdit && next.trim() !== uncertainEdit.text) setUncertainEdit(null)
        }}
        onKeyDown={event => {
          if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); save() }
          if (event.key === 'Escape') {
            event.preventDefault(); setEditing(false); setError(''); setConflictVersion(null)
            setEditRetryIntent(null); setUncertainEdit(null)
            restoreEditFocus()
          }
        }} />
      <div className="comment-editor-audit" role="note">Saving creates a new audit version. Prior text remains in the run log and backups.</div>
      <div className="comment-editor-meta"><span className="muted">Plain text · Esc cancels</span><DraftCounter draft={draft} /></div>
      {error && <div className="notice resource-error comment-inline-error" role="alert">
        <span>{error}</span>
        {conflictVersion != null && <span className="comment-conflict-actions">
          <button type="button" className="btn xs" onClick={onRefresh}>Reload current</button>
          <button type="button" className="btn xs" onClick={copyDraft}>Copy my draft</button>
        </span>}
        {editOutcomeUnknown && <span className="comment-conflict-actions">
          <button type="button" className="btn xs" onClick={onRefresh}>Refresh comments</button>
          <button type="button" className="btn xs" onClick={copyDraft}>Copy my draft</button>
        </span>}
      </div>}
      <div className="comment-editor-actions">
        <button type="button" className="btn sm ghost" disabled={!!busy}
          onClick={() => {
            setEditing(false); setError(''); setConflictVersion(null)
            setEditRetryIntent(null); setUncertainEdit(null)
            restoreEditFocus()
          }}>Cancel</button>
        <button type="button" className="btn sm primary"
          disabled={!!busy || !draft.valid || !draftChanged || editOutcomeUnknown}
          title={exactEditRetry ? 'Retry this exact durable command; no new edit intent is created' : undefined}
          onClick={save}>{busy === 'edit' ? (exactEditRetry ? 'Retrying…' : 'Saving…')
            : exactEditRetry ? 'Retry same command' : 'Save comment'}</button>
      </div>
    </div> : <div className="comment-text">{comment.text}</div>}

    {error && !editing && <div className="notice resource-error comment-inline-error" role="alert">
      <span>{error}</span>
      {(conflictVersion != null || resolutionOutcomeUnknown) && <span className="comment-conflict-actions">
        <button type="button" className="btn xs" onClick={onRefresh}>Refresh comments</button>
      </span>}
    </div>}
    <footer className="comment-card-actions">
      {canMutate && !editing && <>
        <button ref={editButtonRef} type="button" className="btn xs ghost" disabled={!!busy}
          onClick={() => {
            setDraftText(comment.text); setError(''); setConflictVersion(null)
            setEditRetryIntent(null); setUncertainEdit(null); setEditing(true)
          }}>
          <OpIcon name="pencil" size={11} /> Edit
        </button>
        <button type="button" className="btn xs ghost" disabled={!!busy || resolutionOutcomeUnknown}
          title={exactResolutionRetry ? 'Retry this exact durable command; no new resolution intent is created' : undefined}
          onClick={() => changeResolution(resolutionTarget)}>
          <OpIcon name={comment.resolved ? 'replay' : 'check'} size={11} />
          {busy === 'resolution' ? (exactResolutionRetry ? 'Retrying…' : 'Applying…')
            : exactResolutionRetry ? `Retry ${resolutionTarget ? 'resolve' : 'reopen'}`
              : comment.resolved ? 'Reopen' : 'Resolve'}
        </button>
      </>}
      {canViewHistory && !editing && <History
        key={`${runId}:${expectedGeneration || 'unknown'}:${comment.id}:${comment.version}`}
        runId={runId} comment={comment}
        expectedGeneration={expectedGeneration} onAnnounce={onAnnounce} />}
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
}) {
  // Review mode is an authority boundary even if a future caller forgets the redundant readOnly
  // prop. The request layer also rejects mutations, but controls/history must never be rendered.
  const immutable = readOnly || reviewMode
  const [filter, setFilter] = useState(immutable ? 'all' : 'open')
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
      const element = document.getElementById(domId(focusCommentId))
      if (!element) return
      element.scrollIntoView?.({ block: 'center' })
      element.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [focusCommentId, feed.comments, filter])

  const hasExactGeneration = /^[0-9a-f]{64}$/.test(expectedGeneration || '')
  return <section className={'comments-thread' + (global ? ' global' : '')}
    aria-label={global ? 'Run comments' : `Comments for experiment ${nodeId}`}
    aria-busy={feed.loading || feed.refreshing ? 'true' : 'false'}>
    <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
    {reviewMode && <div className="notice comment-review-note" role="note">
      <b>Read-only comments.</b> Comments are attributed to generic run actors; LoopLab does not identify individual people.
    </div>}
    {!immutable && !global && hasExactGeneration && Number.isSafeInteger(nodeGeneration)
      && <CommentComposer runId={runId} nodeId={nodeId} nodeGeneration={nodeGeneration}
        expectedGeneration={expectedGeneration} onRefresh={feed.refresh} onAnnounce={setAnnouncement} />}

    <div className="comment-filter-bar" role="group" aria-label="Filter comments">
      {[
        ['open', 'Open'], ['resolved', 'Resolved'], ['all', 'All'],
      ].map(([key, label]) => <button type="button" key={key} className="btn sm ghost"
        aria-pressed={filter === key} onClick={() => setFilter(key)}>{label} <span>{counts[key]}</span></button>)}
      {feed.refreshing && <span className="muted" role="status">Refreshing…</span>}
    </div>

    {feed.loading && <div className="notice" role="status">Loading comments…</div>}
    {feed.error && <div className={'notice resource-error comment-feed-error' + (feed.stale ? ' stale' : '')}
      role={feed.stale ? 'status' : 'alert'}>
      <span>{feed.stale ? 'Showing the last received comments. ' : ''}{feed.error}</span>
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
        onRefresh={feed.refresh} onAnnounce={setAnnouncement} />)}
    </div>
    {feed.loadMoreError && <div className="notice resource-error comment-feed-error" role="alert">
      <span>{feed.loadMoreError}</span><button type="button" className="btn sm" onClick={feed.loadMore}>Retry</button>
    </div>}
    {feed.hasMore && <button type="button" className="btn sm comment-load-more"
      disabled={feed.loadingMore} onClick={feed.loadMore}>{feed.loadingMore ? 'Loading…' : 'Load older comments'}</button>}
  </section>
}
