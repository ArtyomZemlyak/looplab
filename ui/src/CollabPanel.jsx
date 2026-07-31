import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  apiPrefix, createRunReview, listRunReviews, revokeRunReview,
} from './util.js'
import { encodeRunRouteState, reviewRouteStateForScope } from './runRouteState.js'
import {
  assertReviewLinkCrypto, beginReviewCreateIntent, clearReviewCreateIntent, discardInvalidReviewCreateIntent,
  deriveReviewLinkId, readReviewCreateIntent, reviewCreateBody, reviewRecoveryGenerationValid,
  reviewRecoveryLinkId, reviewRecoveryScope, reviewRecoveryTerminalStatus,
  reviewUrlForIntent, transitionReviewCreateIntent, validateReviewCreateReceipt,
  validateReviewReplayTerminal, validateStoredReviewCreateIntent,
} from './reviewLinkRecovery.js'
import { OpIcon } from './icons.jsx'
import CommentsThread from './CommentsThread.jsx'
import PanelShell from './PanelShell.jsx'
import { deadlineRequest } from './requestDeadline.js'

const REVIEW_LINK_TIMEOUT_MS = 12_000
const boundedLinkRequest = read => deadlineRequest(read, REVIEW_LINK_TIMEOUT_MS)
const activeLink = link => link?.status === 'active' || link?.status === 'stale'
const terminalCopy = status => `The saved review link is ${status} and can no longer be shared. Create a new link.`
const createFailureCopy = error => error?.code === 'review_generation_changed'
  ? 'The run changed before the link was created. Refresh the run and create a new link.'
  : error?.status === 409 && /LOOPLAB_UI_TOKEN/i.test(error?.message || '')
    ? 'Owner authentication is required for sharing. The exact request remains saved in this tab.'
  : error?.status === 400 || error?.status === 422
    ? 'The server rejected the recovery request. Its exact identity remains saved because an earlier attempt may have succeeded.'
    : error?.status === 401 || error?.status === 403
      ? 'Owner access was rejected. Restore access, then recover this exact saved link.'
      : error?.status === 404
        ? 'Review-link recovery is unavailable on this server. The exact request remains saved.'
        : 'Link creation was not confirmed. Recovering replays this exact link and cannot create a duplicate.'
const provenNoWriteFailure = error => error?.code === 'review_generation_changed'
const dateLabel = epoch => {
  const value = Number(epoch)
  if (!Number.isFinite(value) || value <= 0) return 'at an unknown time'
  try {
    const date = new Date(value * 1000)
    return Number.isFinite(date.getTime()) ? date.toLocaleString() : 'at an unknown time'
  } catch { return 'at an unknown time' }
}
const emptyRecovery = key => ({
  key, loaded: false, intent: null, invalid: null, busy: false, error: '', message: '',
})

/**
 * Comments and review-link management intentionally live outside the owner panel hub. A read-only
 * review may open this panel, but must never download charts, settings, raw events, or owner tools.
 */
export default function CollabPanel({
  runId, onSelect, onOpenComment, onClose, onToast, reviewRouteState = null,
  reviewMode = false, expectedGeneration = null, refreshKey = null,
  PanelComponent = PanelShell, draftStore = null,
}) {
  const [ttl, setTtl] = useState(7 * 24 * 60 * 60)
  const [includeEvidence, setIncludeEvidence] = useState(false)
  const [linksResource, setLinksResource] = useState({
    key: '', links: [], status: 'loading', error: '',
  })
  const [recoveryResource, setRecoveryResource] = useState(() => emptyRecovery(''))
  const [revokeResource, setRevokeResource] = useState({ key: '', ids: new Set(), error: '' })
  const listRequestRef = useRef(null)
  const createRequestRef = useRef(null)
  const revokeRequestsRef = useRef(new Map())
  const createdInputRef = useRef(null)
  const createButtonRef = useRef(null)
  const prefix = apiPrefix()
  const origin = typeof location === 'undefined' ? '' : location.origin
  const recoveryScope = reviewRecoveryScope(origin, prefix)
  const viewKey = `${reviewMode ? 'review' : 'owner'}\u0000${recoveryScope}\u0000${String(runId)}\u0000${String(expectedGeneration || '')}`
  const renderRef = useRef({ key: viewKey, runId: String(runId) })
  renderRef.current = { key: viewKey, runId: String(runId) }

  const recovery = recoveryResource.key === viewKey
    ? recoveryResource : emptyRecovery(viewKey)
  const linksView = linksResource.key === viewKey
    ? linksResource : { key: viewKey, links: [], status: 'loading', error: '' }
  const revokeView = revokeResource.key === viewKey
    ? revokeResource : { key: viewKey, ids: new Set(), error: '' }

  const updateRecovery = useCallback((key, update) => {
    setRecoveryResource(previous => previous.key === key ? update(previous) : previous)
  }, [])

  const refreshLinks = useCallback(async () => {
    if (reviewMode) return null
    const key = viewKey
    const previousRequest = listRequestRef.current
    previousRequest?.timed.controller.abort()
    const timed = boundedLinkRequest(signal => listRunReviews(runId, { signal }))
    const operation = { key, timed }
    listRequestRef.current = operation
    setLinksResource(previous => {
      const links = previous.key === key ? previous.links : []
      return { key, links, status: links.length ? 'refreshing' : 'loading', error: '' }
    })
    try {
      const result = await timed.promise
      if (listRequestRef.current !== operation || renderRef.current.key !== key) return null
      if (!Array.isArray(result?.links)) throw new Error('invalid review-link list')
      setLinksResource({ key, links: result.links, status: 'ready', error: '' })
      return result.links
    } catch (caught) {
      if (listRequestRef.current !== operation || renderRef.current.key !== key) return null
      setLinksResource(previous => {
        const links = previous.key === key ? previous.links : []
        return {
          key, links, status: links.length ? 'stale' : 'error',
          error: links.length
            ? 'Showing the last loaded links. Refresh failed.'
            : 'Review links are unavailable.',
        }
      })
      return null
    } finally {
      if (listRequestRef.current === operation) listRequestRef.current = null
    }
  }, [reviewMode, runId, viewKey])

  useEffect(() => {
    let active = true
    createRequestRef.current?.timed.controller.abort()
    createRequestRef.current = null
    for (const operation of revokeRequestsRef.current.values()) operation.timed.controller.abort()
    revokeRequestsRef.current.clear()
    setRevokeResource({ key: viewKey, ids: new Set(), error: '' })
    if (reviewMode) {
      setRecoveryResource({ ...emptyRecovery(viewKey), loaded: true })
      return () => { active = false }
    }
    const restored = readReviewCreateIntent(recoveryScope, runId)
    if (restored?.invalid) {
      setRecoveryResource({
        ...emptyRecovery(viewKey), loaded: true, invalid: restored.code,
        error: restored.code === 'REVIEW_RECOVERY_STORAGE_UNAVAILABLE'
          ? 'Session recovery storage is unavailable. No review-link request will be sent.'
          : 'Saved review-link recovery data is unreadable. Creating another link is blocked until it is deliberately discarded.',
      })
    } else {
      if (restored) {
        setTtl(restored.ttlSeconds)
        setIncludeEvidence(restored.includeEvidence)
      }
      if (restored && ['confirmed', 'revoking', 'conflict'].includes(restored.phase)) {
        setRecoveryResource({ ...emptyRecovery(viewKey), intent: restored })
        void validateStoredReviewCreateIntent(restored).then(validated => {
          if (active && renderRef.current.key === viewKey) {
            setRecoveryResource({ ...emptyRecovery(viewKey), loaded: true, intent: validated })
          }
        }).catch(caught => {
          if (!active || renderRef.current.key !== viewKey) return
          const code = caught?.code === 'REVIEW_RECOVERY_CRYPTO_UNAVAILABLE'
            ? caught.code : 'REVIEW_RECOVERY_INVALID'
          setRecoveryResource({
            ...emptyRecovery(viewKey), loaded: true, invalid: code,
            error: caught?.message || 'Saved review-link recovery could not be verified.',
          })
        })
      } else setRecoveryResource({ ...emptyRecovery(viewKey), loaded: true, intent: restored })
    }
    return () => { active = false }
  }, [recoveryScope, reviewMode, runId, viewKey])

  useEffect(() => {
    if (reviewMode) {
      setLinksResource({ key: viewKey, links: [], status: 'ready', error: '' })
      return undefined
    }
    void refreshLinks()
    return () => {
      const current = listRequestRef.current
      if (current?.key === viewKey) current.timed.controller.abort()
    }
  }, [refreshLinks, reviewMode, viewKey])

  useEffect(() => () => {
    renderRef.current = { key: '__unmounted__', runId: '' }
    listRequestRef.current?.timed.controller.abort()
    createRequestRef.current?.timed.controller.abort()
    for (const operation of revokeRequestsRef.current.values()) operation.timed.controller.abort()
  }, [])

  const copy = async (url, key = viewKey) => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
      await navigator.clipboard.writeText(url)
      if (renderRef.current.key === key) onToast?.('review link copied')
      return true
    } catch {
      if (renderRef.current.key === key) {
        setTimeout(() => {
          if (renderRef.current.key !== key) return
          createdInputRef.current?.focus()
          createdInputRef.current?.select()
        }, 0)
        onToast?.('Copy the visible link manually')
      }
      return false
    }
  }

  const finishIntent = (intent, key, message) => {
    if (!clearReviewCreateIntent(intent)) {
      if (renderRef.current.key === key) updateRecovery(key, previous => ({
        ...previous, busy: false,
        error: 'The link reached a terminal state, but its saved recovery could not be cleared.',
      }))
      return false
    }
    if (renderRef.current.key === key) setRecoveryResource({
      ...emptyRecovery(key), loaded: true, message,
    })
    return true
  }

  const submitIntent = async (intent, { copyOnSuccess = false } = {}) => {
    if (!intent || !['pending', 'confirmed'].includes(intent.phase)) return
    const key = viewKey
    try { assertReviewLinkCrypto() } catch (caught) {
      updateRecovery(key, previous => ({ ...previous, error: caught.message }))
      return
    }
    const current = createRequestRef.current
    if (current?.intent.requestId === intent.requestId) return
    current?.timed.controller.abort()
    const timed = boundedLinkRequest(signal => createRunReview(
      intent.runId, reviewCreateBody(intent), { signal }))
    const operation = { key, intent, timed }
    createRequestRef.current = operation
    updateRecovery(key, previous => ({ ...previous, intent, busy: true, error: '', message: '' }))
    try {
      const raw = await timed.promise
      const receipt = await validateReviewCreateReceipt(raw, intent)
      if (reviewRecoveryTerminalStatus(receipt.status)) {
        finishIntent(intent, key, terminalCopy(receipt.status))
        if (renderRef.current.key === key) void refreshLinks()
        return
      }
      const confirmed = transitionReviewCreateIntent(intent, {
        phase: 'confirmed', linkId: receipt.id, token: receipt.token,
        expiresAt: receipt.expiresAt,
      })
      const url = reviewUrlForIntent(confirmed, { origin, prefix })
      if (!url) throw Object.assign(new Error('Review URL could not be reconstructed.'), {
        code: 'REVIEW_RECOVERY_PROTOCOL_ERROR',
      })
      if (renderRef.current.key === key) {
        setRecoveryResource({
          ...emptyRecovery(key), loaded: true, intent: confirmed,
          message: receipt.replayed ? 'The same review link was recovered.' : 'Review link created.',
        })
        if (copyOnSuccess) await copy(url, key)
        void refreshLinks()
      }
    } catch (caught) {
      if (createRequestRef.current !== operation || renderRef.current.key !== key) return
      if (caught?.status === 410 && caught?.code === 'review_replay_terminal') {
        try {
          const terminal = await validateReviewReplayTerminal(caught, intent)
          finishIntent(intent, key, terminalCopy(terminal.kind))
          void refreshLinks()
          return
        } catch { /* a generic or malformed 410 cannot release the durable identity */ }
      }
      if (caught?.code === 'review_idempotency_conflict') {
        const linkId = reviewRecoveryLinkId(caught?.detail?.existing_link_id)
        let expectedLinkId = null
        try { expectedLinkId = await deriveReviewLinkId(intent.runId, intent.requestId) }
        catch { /* the exact intent remains pending and no unrelated link is exposed */ }
        if (linkId && linkId === expectedLinkId) {
          try {
            const conflicted = transitionReviewCreateIntent(intent, {
              phase: 'conflict', linkId, token: null, expiresAt: null,
            })
            setRecoveryResource({
              ...emptyRecovery(key), loaded: true, intent: conflicted,
              error: 'A different saved request already owns this recovery identity. Revoke that link before creating another.',
            })
          } catch (storageError) {
            updateRecovery(key, previous => ({
              ...previous, busy: false, error: storageError.message,
            }))
          }
          void refreshLinks()
          return
        }
      }
      if (provenNoWriteFailure(caught)) {
        const message = createFailureCopy(caught)
        if (!finishIntent(intent, key, message)) return
        void refreshLinks()
        return
      }
      updateRecovery(key, previous => ({
        ...previous, intent, busy: false, error: createFailureCopy(caught),
      }))
    } finally {
      if (createRequestRef.current === operation) {
        createRequestRef.current = null
        if (renderRef.current.key === key) updateRecovery(key, previous => ({ ...previous, busy: false }))
      }
    }
  }

  const create = () => {
    if (!recovery.loaded || recovery.busy || recovery.intent || recovery.invalid) return
    if (!reviewRecoveryGenerationValid(expectedGeneration)) {
      updateRecovery(viewKey, previous => ({
        ...previous, error: 'Wait for the current run version before creating a review link.',
      }))
      return
    }
    const scopedState = reviewRouteStateForScope({ ...(reviewRouteState || {}),
      generation: expectedGeneration }, { evidence: includeEvidence })
    const routeQuery = encodeRunRouteState(scopedState,
      { reviewMode: true, forceGeneration: true })
    try {
      const acquired = beginReviewCreateIntent({
        scope: recoveryScope, runId, expectedGeneration,
        ttlSeconds: ttl, includeEvidence, routeQuery,
      })
      setRecoveryResource({
        ...emptyRecovery(viewKey), loaded: true, intent: acquired.intent,
      })
      void submitIntent(acquired.intent, { copyOnSuccess: true })
    } catch (caught) {
      const code = caught?.code
      const blocksRecovery = code === 'REVIEW_RECOVERY_STORAGE_UNAVAILABLE'
        || code === 'REVIEW_RECOVERY_INVALID'
      updateRecovery(viewKey, previous => ({
        ...previous, loaded: true, invalid: blocksRecovery ? code : null,
        error: caught?.message || 'Review-link recovery could not be saved. No request was sent.',
      }))
    }
  }

  const dismissConfirmed = () => {
    const key = viewKey
    const intent = recovery.intent
    if (!intent || intent.phase !== 'confirmed') return
    if (!clearReviewCreateIntent(intent)) {
      updateRecovery(viewKey, previous => ({
        ...previous, error: 'The saved link could not be cleared. It remains available for recovery.',
      }))
      return
    }
    setRecoveryResource({
      ...emptyRecovery(viewKey), loaded: true,
      message: 'Saved recovery cleared. The existing review link remains active until expiry or revocation.',
    })
    setTimeout(() => {
      if (renderRef.current.key === key) createButtonRef.current?.focus()
    }, 0)
  }

  const discardInvalid = () => {
    if (!discardInvalidReviewCreateIntent(recoveryScope, runId)) {
      updateRecovery(viewKey, previous => ({
        ...previous, error: 'Saved recovery data could not be discarded. Session storage may be unavailable.',
      }))
      return
    }
    setRecoveryResource({
      ...emptyRecovery(viewKey), loaded: true,
      message: 'Unreadable recovery data discarded. Check Existing links before creating another link.',
    })
  }

  const revoke = async id => {
    const linkId = reviewRecoveryLinkId(id)
    if (!linkId) return
    const key = viewKey
    const requestKey = `${key}\u0000${linkId}`
    if (revokeRequestsRef.current.has(requestKey)) return
    let durableIntent = recovery.intent?.linkId === linkId ? recovery.intent : null
    if (durableIntent?.phase === 'confirmed') {
      try {
        durableIntent = transitionReviewCreateIntent(durableIntent, {
          phase: 'revoking', linkId, token: durableIntent.token,
          expiresAt: durableIntent.expiresAt,
        })
        setRecoveryResource({
          ...emptyRecovery(key), loaded: true, intent: durableIntent,
          message: 'Revoking the recovered review link…',
        })
      } catch (caught) {
        updateRecovery(key, previous => ({ ...previous, error: caught.message }))
        return
      }
    } else if (durableIntent) updateRecovery(key, previous => ({
      ...previous, error: '', message: durableIntent.phase === 'revoking'
        ? 'Revoking the recovered review link…' : previous.message,
    }))
    const timed = boundedLinkRequest(signal => revokeRunReview(runId, linkId, { signal }))
    const operation = { key, linkId, intent: durableIntent, timed }
    revokeRequestsRef.current.set(requestKey, operation)
    setRevokeResource(previous => {
      const ids = new Set(previous.key === key ? previous.ids : [])
      ids.add(linkId)
      return { key, ids, error: '' }
    })
    try {
      const result = await timed.promise
      if (result?.ok !== true || result.id !== linkId || result.run_id !== String(runId)
          || !Number.isFinite(Number(result.revoked_at))) {
        throw new Error('The server returned an invalid revocation receipt.')
      }
      if (durableIntent && !clearReviewCreateIntent(durableIntent)) {
        throw new Error('The link was revoked, but its saved recovery could not be cleared.')
      }
      if (renderRef.current.key === key) {
        if (durableIntent) setRecoveryResource({
          ...emptyRecovery(key), loaded: true, message: 'Review link revoked.',
        })
        setLinksResource(previous => previous.key === key ? {
          ...previous,
          links: previous.links.map(link => link.id === linkId
            ? { ...link, status: 'revoked', revoked_at: result.revoked_at } : link),
        } : previous)
        onToast?.('review link revoked')
        void refreshLinks()
      }
    } catch {
      if (revokeRequestsRef.current.get(requestKey) !== operation || renderRef.current.key !== key) return
      const message = 'Revocation was not confirmed. Retry the same revoke before sharing or creating another link.'
      if (durableIntent) updateRecovery(key, previous => ({
        ...previous, intent: durableIntent, error: message,
      }))
      else setRevokeResource(previous => previous.key === key
        ? { ...previous, error: message } : previous)
    } finally {
      if (revokeRequestsRef.current.get(requestKey) === operation) {
        revokeRequestsRef.current.delete(requestKey)
        if (renderRef.current.key === key) setRevokeResource(previous => {
          if (previous.key !== key) return previous
          const ids = new Set(previous.ids)
          ids.delete(linkId)
          return { ...previous, ids }
        })
      }
    }
  }

  const intent = recovery.intent
  const controlsLocked = !recovery.loaded || !!recovery.invalid || !!intent || recovery.busy
    || !reviewRecoveryGenerationValid(expectedGeneration)
  const createdUrl = intent?.phase === 'confirmed'
    ? reviewUrlForIntent(intent, { origin, prefix }) : ''
  const recovering = intent?.phase === 'pending'
  const recoveryConflict = intent?.phase === 'conflict'
  const revokePending = intent?.phase === 'revoking'

  return <PanelComponent title="Comments & sharing" onClose={onClose}>
    {!reviewMode && <div className="review-link-builder" aria-busy={recovery.busy ? 'true' : 'false'}>
      <div className="section-h">Create a read-only review link</div>
      <p className="muted">The link is bound to this run, expires automatically, can be revoked, and never carries owner controls.</p>
      <div className="review-link-options">
        <label>Expires
          <select value={ttl} disabled={controlsLocked}
            onChange={event => setTtl(Number(event.target.value))}>
            <option value={60 * 60}>1 hour</option><option value={24 * 60 * 60}>1 day</option>
            <option value={7 * 24 * 60 * 60}>7 days</option>
            <option value={30 * 24 * 60 * 60}>30 days</option>
          </select>
        </label>
        <label className="review-evidence-option"><input type="checkbox" checked={includeEvidence}
          disabled={controlsLocked}
          onChange={event => setIncludeEvidence(event.target.checked)} /> Include redacted source evidence</label>
      </div>
      {includeEvidence && <div className="notice warn">Source and result details can still contain sensitive project information. Known credential patterns are redacted; raw logs, prompts, traces, and artifacts remain excluded.</div>}
      {!recovery.loaded && <div className="muted" role="status">Checking this tab for a saved review link…</div>}
      {recovery.loaded && !intent && !recovery.invalid
        && !reviewRecoveryGenerationValid(expectedGeneration)
        && <div className="muted" role="status">Waiting for the current run version before sharing…</div>}
      {recovery.invalid && <div className="notice resource-error review-recovery" role="alert">
        <span>{recovery.error}</span>
        {!['REVIEW_RECOVERY_STORAGE_UNAVAILABLE', 'REVIEW_RECOVERY_CRYPTO_UNAVAILABLE']
          .includes(recovery.invalid)
          && <button type="button" className="btn sm danger" onClick={discardInvalid}>
            Discard unreadable recovery
          </button>}
      </div>}
      {recovering && <div className="notice warn review-recovery" role="status" aria-live="polite">
        <b>{recovery.busy ? 'Recovering the same review link…' : 'Review-link creation was not confirmed.'}</b>
        <span>The exact run version, expiry, evidence scope, and secret are saved in this tab. Recovery replays them without minting a second identity.</span>
        {!recovery.busy && <button type="button" className="btn sm primary"
          onClick={() => submitIntent(intent, { copyOnSuccess: true })}>
          Recover the same review link
        </button>}
      </div>}
      {recoveryConflict && <div className="notice resource-error review-recovery" role="alert">
        <b>Saved link identity conflict</b>
        <span>{recovery.error
          || `A different request already owns ${intent.linkId}. Revoke it before creating another link.`}</span>
        <button type="button" className="btn sm danger"
          disabled={revokeView.ids.has(intent.linkId)} onClick={() => revoke(intent.linkId)}>
          {revokeView.ids.has(intent.linkId) ? 'Revoking…' : 'Revoke conflicting link'}
        </button>
      </div>}
      {revokePending && <div className="notice warn review-recovery" role="status" aria-live="polite">
        <b>Revocation is not confirmed.</b>
        <span>{recovery.error
          || 'Retrying targets the same recovered link and cannot revoke a different link.'}</span>
        <button type="button" className="btn sm danger"
          disabled={revokeView.ids.has(intent.linkId)} onClick={() => revoke(intent.linkId)}>
          {revokeView.ids.has(intent.linkId) ? 'Revoking…' : 'Retry revoke'}
        </button>
      </div>}
      {recovery.error && !recovery.invalid && !recoveryConflict && !revokePending
        && <div className="notice resource-error" role="alert">{recovery.error}</div>}
      {recovery.message && <div className="notice" role="status" aria-live="polite">{recovery.message}</div>}
      {!intent && !recovery.invalid && recovery.loaded && <button ref={createButtonRef}
        type="button" className="btn sm primary" disabled={controlsLocked} onClick={create}>
        <OpIcon name="link" size={12} /> Create and copy review link
      </button>}
      {createdUrl && <div className="review-created" role="status" aria-live="polite">
        <label htmlFor="created-review-url">Review link ready</label>
        <div><input ref={createdInputRef} id="created-review-url" readOnly value={createdUrl}
          aria-describedby="created-review-url-note" onFocus={event => event.target.select()} />
          <button type="button" className="btn sm" aria-label="Copy recovered review link"
            onClick={() => copy(createdUrl)}>Copy</button></div>
        <div id="created-review-url-note" className="muted">
          Expires {dateLabel(intent.expiresAt)}. Available for recovery in this browser tab until you confirm it is saved.
        </div>
        <div className="review-recovery-actions">
          <button type="button" className="btn sm" onClick={dismissConfirmed}>I’ve saved it</button>
          <button type="button" className="btn sm danger"
            disabled={revokeView.ids.has(intent.linkId)} onClick={() => revoke(intent.linkId)}>
            {revokeView.ids.has(intent.linkId) ? 'Revoking…' : 'Revoke this link'}
          </button>
        </div>
      </div>}
      <div className="section-h">Existing links</div>
      {linksView.status === 'loading' && !linksView.links.length
        && <div className="muted" role="status">Loading review links…</div>}
      {linksView.status === 'refreshing'
        && <div className="muted" role="status">Refreshing review links…</div>}
      {linksView.links.length > 0 && <div className="review-link-list">{linksView.links.map(link => {
        const expires = dateLabel(link.expires_at)
        const evidence = (link.scopes || []).includes('evidence')
        const rowBusy = revokeView.ids.has(link.id)
        return <div key={link.id} className="review-link-row">
          <div><b>{link.status}</b> · {evidence ? 'summary + evidence' : 'summary'}
            <div className="muted">expires {expires}</div></div>
          {activeLink(link) && <button type="button" className="btn sm danger" disabled={rowBusy}
            aria-label={`Revoke ${evidence ? 'summary and evidence' : 'summary'} review link expiring ${expires}`}
            onClick={() => revoke(link.id)}>{rowBusy ? 'Revoking…' : 'Revoke'}</button>}
        </div>
      })}</div>}
      {linksView.status === 'ready' && !linksView.links.length
        && <div className="muted">No review links created yet.</div>}
      {['error', 'stale'].includes(linksView.status) && <div className="review-links-error"
        role={linksView.status === 'error' ? 'alert' : 'status'}>
        <span className="muted">{linksView.error}</span>
        <button type="button" className="btn sm" onClick={refreshLinks}>Retry</button>
      </div>}
      {revokeView.error && <div className="notice resource-error" role="alert">{revokeView.error}</div>}
    </div>}
    {!reviewMode && <div className="muted" style={{ margin: '16px 0 8px' }}>
      Comments are append-only run events. Review-link recipients can read redacted current comments,
      but cannot add, edit, resolve, reopen, or inspect owner-only version history.
    </div>}
    <CommentsThread runId={runId} expectedGeneration={expectedGeneration} refreshKey={refreshKey}
      readOnly={reviewMode} reviewMode={reviewMode} global
      draftStore={draftStore} draftSurface="collab"
      onOpenComment={comment => {
        if (onOpenComment) { onOpenComment(comment); return }
        onSelect?.(comment.nodeId)
        onClose?.()
      }} />
  </PanelComponent>
}
