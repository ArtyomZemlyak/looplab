import React, { useEffect, useState } from 'react'
import {
  apiPrefix, createRunReview, listRunReviews, revokeRunReview,
} from './util.js'
import { hashWithRunRouteState, reviewRouteStateForScope } from './runRouteState.js'
import { OpIcon } from './icons.jsx'
import CommentsThread from './CommentsThread.jsx'
import PanelShell from './PanelShell.jsx'
import { deadlineRequest } from './requestDeadline.js'

const REVIEW_LINK_TIMEOUT_MS = 12_000
const boundedLinkRequest = read => deadlineRequest(read, REVIEW_LINK_TIMEOUT_MS)
const activeLink = link => link?.status === 'active' || link?.status === 'stale'
const authoritativeFailure = ({ status } = {}) =>
  status >= 400 && status < 500 && status !== 408
const reviewLinkFailure = error => error?.status === 409
  ? 'Run changed during link creation. Refresh and retry.'
  : error?.status === 400
    ? 'Link options were rejected. Check them and retry.'
    : 'Link request was not confirmed.'

/**
 * Comments and review-link management intentionally live outside the owner panel hub. A read-only
 * review may open this panel, but must never download charts, settings, raw events, or owner tools.
 */
export default function CollabPanel({
  runId, onSelect, onOpenComment, onClose, onToast, reviewRouteState = null,
  reviewMode = false, expectedGeneration = null, refreshKey = null,
  PanelComponent = PanelShell,
}) {
  const [ttl, setTtl] = useState(7 * 24 * 60 * 60)
  const [includeEvidence, setIncludeEvidence] = useState(false)
  const [links, setLinks] = useState([])
  const [linksStatus, setLinksStatus] = useState('loading')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [createdUrl, setCreatedUrl] = useState('')
  const [unknownCreate, setUnknownCreate] = useState(false)
  const refreshLinks = async () => {
    if (reviewMode) return
    setLinksStatus('loading')
    try {
      const result = await boundedLinkRequest(
        signal => listRunReviews(runId, { signal })).promise
      const next = result.links || []
      setLinks(next); setLinksStatus('ready'); setError('')
      return next
    } catch (caught) {
      setLinksStatus('error'); setError('Review links are unavailable.')
      return null
    }
  }
  const reconcileUnknownCreate = async () => {
    const refreshed = await refreshLinks()
    const unresolved = refreshed == null || refreshed.some(activeLink)
    setUnknownCreate(unresolved)
    setError(unresolved
      ? 'Link creation is uncertain. Revoke active links before retrying.'
      : '')
  }
  useEffect(() => {
    setCreatedUrl(''); setUnknownCreate(false)
    if (reviewMode) {
      setLinks([]); setLinksStatus('ready'); setError('')
      return undefined
    }
    let active = true
    setLinksStatus('loading'); setError('')
    const timed = boundedLinkRequest(signal => listRunReviews(runId, { signal }))
    timed.promise
      .then(result => { if (active) { setLinks(result.links || []); setLinksStatus('ready') } })
      .catch(() => {
        if (active) { setLinksStatus('error'); setError('Review links are unavailable.') }
      })
    return () => { active = false; timed.controller.abort() }
  }, [runId, reviewMode])
  const copy = async (url) => {
    try { await navigator.clipboard.writeText(url); onToast?.('review link copied') }
    catch { setCreatedUrl(url); onToast?.('Copy the visible link manually') }
  }
  const create = async () => {
    if (busy || unknownCreate) return
    setBusy(true); setError(''); setCreatedUrl('')
    try {
      const result = await boundedLinkRequest(signal => createRunReview(
        runId, { ttl_seconds: ttl, include_evidence: includeEvidence }, { signal })).promise
      const base = `${location.origin}${apiPrefix()}/`
      const target = new URL(result.path, base)
      const scopedState = reviewRouteStateForScope({ ...(reviewRouteState || {}),
        generation: result.generation }, { evidence: includeEvidence })
      target.hash = hashWithRunRouteState(target.hash, scopedState,
        { reviewMode: true, forceGeneration: true })
      const url = target.href
      setCreatedUrl(url)
      await copy(url)
      await refreshLinks()
    } catch (caught) {
      if (authoritativeFailure(caught)) setError(reviewLinkFailure(caught))
      else await reconcileUnknownCreate()
    }
    finally { setBusy(false) }
  }
  const revoke = async (id) => {
    setBusy(true); setError('')
    try {
      await boundedLinkRequest(signal => revokeRunReview(runId, id, { signal })).promise
      const refreshed = await refreshLinks()
      if (refreshed && !refreshed.some(activeLink)) setUnknownCreate(false)
      onToast?.('review link revoked')
    } catch { setError('Revoke not confirmed. Refresh links before retrying.') }
    finally { setBusy(false) }
  }
  return <PanelComponent title="Comments & sharing" onClose={onClose}>
    {!reviewMode && <div className="review-link-builder">
      <div className="section-h">Create a read-only review link</div>
      <p className="muted">The link is bound to this run, expires automatically, can be revoked, and never carries owner controls.</p>
      <div className="review-link-options">
        <label>Expires
          <select value={ttl} onChange={event => setTtl(Number(event.target.value))}>
            <option value={60 * 60}>1 hour</option><option value={24 * 60 * 60}>1 day</option>
            <option value={7 * 24 * 60 * 60}>7 days</option>
            <option value={30 * 24 * 60 * 60}>30 days</option>
          </select>
        </label>
        <label className="review-evidence-option"><input type="checkbox" checked={includeEvidence}
          onChange={event => setIncludeEvidence(event.target.checked)} /> Include redacted source evidence</label>
      </div>
      {includeEvidence && <div className="notice warn">Source and result details can still contain sensitive project information. Known credential patterns are redacted; raw logs, prompts, traces, and artifacts remain excluded.</div>}
      {error && <div className="notice resource-error" role="alert">{error}</div>}
      <button className="btn sm primary" disabled={busy || unknownCreate} onClick={create}>
        <OpIcon name="link" size={12} /> {busy ? 'Creating…'
          : unknownCreate ? 'Resolve prior link' : 'Create & copy link'}
      </button>
      {createdUrl && <div className="review-created"><label htmlFor="created-review-url">New link (shown once)</label>
        <div><input id="created-review-url" readOnly value={createdUrl} onFocus={event => event.target.select()} />
          <button className="btn sm" onClick={() => copy(createdUrl)}>Copy</button></div></div>}
      <div className="section-h">Existing links</div>
      {linksStatus === 'loading' ? <div className="muted" role="status">Loading review links…</div>
        : links.length ? <div className="review-link-list">{links.map(link => <div key={link.id} className="review-link-row">
          <div><b>{link.status}</b> · {(link.scopes || []).includes('evidence') ? 'summary + evidence' : 'summary'}
            <div className="muted">expires {new Date(link.expires_at * 1000).toLocaleString()}</div></div>
          {activeLink(link) && <button className="btn sm danger" disabled={busy}
            onClick={() => revoke(link.id)}>Revoke</button>}
        </div>)}</div> : linksStatus === 'ready' ? <div className="muted">No review links created yet.</div>
          : <div className="review-links-error"><span className="muted">Review links unavailable.</span>
            <button className="btn sm" disabled={busy}
              onClick={unknownCreate ? reconcileUnknownCreate : refreshLinks}>Retry</button></div>}
    </div>}
    {!reviewMode && <div className="muted" style={{ margin: '16px 0 8px' }}>
      Comments are append-only run events. Review-link recipients can read redacted current comments,
      but cannot add, edit, resolve, reopen, or inspect owner-only version history.
    </div>}
    <CommentsThread runId={runId} expectedGeneration={expectedGeneration} refreshKey={refreshKey}
      readOnly={reviewMode} reviewMode={reviewMode} global
      onOpenComment={comment => {
        if (onOpenComment) { onOpenComment(comment); return }
        onSelect?.(comment.nodeId)
        onClose?.()
      }} />
  </PanelComponent>
}
