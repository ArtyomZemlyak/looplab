import React, { useEffect, useState } from 'react'
import {
  apiPrefix, createRunReview, listRunReviews, revokeRunReview,
} from './util.js'
import { hashWithRunRouteState, reviewRouteStateForScope } from './runRouteState.js'
import { OpIcon } from './icons.jsx'
import CommentsThread from './CommentsThread.jsx'
import PanelShell from './PanelShell.jsx'

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
  const refreshLinks = async () => {
    if (reviewMode) return
    setLinksStatus('loading')
    try {
      const result = await listRunReviews(runId)
      setLinks(result.links || []); setLinksStatus('ready'); setError('')
    } catch (caught) {
      setLinksStatus('error'); setError(caught.message || 'Could not load review links')
    }
  }
  useEffect(() => {
    if (reviewMode) {
      setLinks([]); setLinksStatus('ready'); setError(''); setCreatedUrl('')
      return undefined
    }
    let active = true
    setLinksStatus('loading'); setError('')
    listRunReviews(runId)
      .then(result => { if (active) { setLinks(result.links || []); setLinksStatus('ready') } })
      .catch(caught => {
        if (active) { setLinksStatus('error'); setError(caught.message || 'Could not load review links') }
      })
    return () => { active = false }
  }, [runId, reviewMode])
  const copy = async (url) => {
    try { await navigator.clipboard.writeText(url); onToast?.('review link copied') }
    catch { setCreatedUrl(url); onToast?.('Copy the visible link manually') }
  }
  const create = async () => {
    if (busy) return
    setBusy(true); setError(''); setCreatedUrl('')
    try {
      const result = await createRunReview(runId, { ttl_seconds: ttl, include_evidence: includeEvidence })
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
    } catch (caught) { setError(caught.message || 'Could not create review link') }
    finally { setBusy(false) }
  }
  const revoke = async (id) => {
    setBusy(true); setError('')
    try { await revokeRunReview(runId, id); await refreshLinks(); onToast?.('review link revoked') }
    catch (caught) { setError(caught.message || 'Could not revoke link') }
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
      <button className="btn sm primary" disabled={busy} onClick={create}>
        <OpIcon name="link" size={12} /> {busy ? 'Creating…' : 'Create & copy link'}
      </button>
      {createdUrl && <div className="review-created"><label htmlFor="created-review-url">New link (shown once)</label>
        <div><input id="created-review-url" readOnly value={createdUrl} onFocus={event => event.target.select()} />
          <button className="btn sm" onClick={() => copy(createdUrl)}>Copy</button></div></div>}
      <div className="section-h">Existing links</div>
      {linksStatus === 'loading' ? <div className="muted" role="status">Loading review links…</div>
        : links.length ? <div className="review-link-list">{links.map(link => <div key={link.id} className="review-link-row">
          <div><b>{link.status}</b> · {(link.scopes || []).includes('evidence') ? 'summary + evidence' : 'summary'}
            <div className="muted">expires {new Date(link.expires_at * 1000).toLocaleString()}</div></div>
          {['active', 'stale'].includes(link.status) && <button className="btn sm danger" disabled={busy}
            onClick={() => revoke(link.id)}>Revoke</button>}
        </div>)}</div> : linksStatus === 'ready' ? <div className="muted">No review links created yet.</div>
          : <div className="review-links-error"><span className="muted">Existing links could not be loaded.</span>
            <button className="btn sm" disabled={busy} onClick={refreshLinks}>Retry</button></div>}
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
