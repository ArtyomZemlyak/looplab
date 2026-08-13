import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  getCrossRunAtlas, getCrossRunClaims, getCrossRunCurationLog, getCrossRunClaimCurationLog,
  projectLedgerSource,
} from './api.js'
import {
  sourcePortfolioId,
  buildClaimsCurationView, mergeCurationLogs, mergeLedgerPayload,
  isValidLedgerSourceEnvelope, reconcileLedgerSourceStatuses, ledgerPayloadPortfolioId,
} from './claimsCurationModel.js'
import './claims-curation.css'
import GlobalMenu from './GlobalMenu.jsx'
import { deadlineRequest } from './requestDeadline.js'

// The four independently fetched slices. `atlas` is the ENDPOINT name (`/api/cross-run/atlas`), which
// F7 (doc 29) deliberately did not rename with the surface; what it contributes here is the
// mixed-evidence claim records, and its concept sections are no longer read at all.
const SOURCES = [
  ['atlas', signal => getCrossRunAtlas(24, { signal }), 'Mixed-evidence claims'],
  ['claims', signal => getCrossRunClaims(40, 0, { signal }), 'Claim records'],
  ['conceptCuration', signal => getCrossRunCurationLog(20, { signal }), 'Concept steward log'],
  ['claimCuration', signal => getCrossRunClaimCurationLog(20, { signal }), 'Claim steward log'],
]
const SOURCE_TIMEOUT_MS = 15_000

const countLabel = (count, singular, plural = `${singular}s`) => `${count} ${count === 1 ? singular : plural}`
const EPISTEMIC_COPY = {
  supported: 'support-only evidence',
  refuted: 'opposition-only evidence',
  mixed: 'mixed evidence',
  inconclusive: 'insufficient evidence',
}

function SourceWatermark({ sourceKey, label, source, retry, busy, pending,
  activeRetry = false, retryable = true, children }) {
  const state = source.state
  return <p className={`ledger-source-note ledger-source-focus-target ledger-source-${pending ? 'loading' : state}`}
    data-ledger-source={sourceKey} tabIndex={-1}>
    <strong>{label}</strong> · {pending ? 'loading'
      : state === 'retained-stale' ? 'stale' : state === 'failed' ? 'unavailable' : 'loaded'}
    {' · '}revision {source.revision || 'unknown'} · {children}
    {retryable && state !== 'current' && <> · <button type="button" className="btn sm ledger-retry-action"
      disabled={busy && !activeRetry} aria-disabled={activeRetry || undefined}
      aria-busy={activeRetry || undefined}
      onClick={event => retry(sourceKey, 'watermark', event.currentTarget)}
      aria-label={`${activeRetry ? 'Retrying' : 'Retry'} ${label}`}>
      {activeRetry ? 'Retrying…' : 'Retry'}</button></>}
  </p>
}

// The claim source is the ONLY read-completeness authority this surface has left. It used to report a
// concept-capsule status beside it; that receipt described `concept_capsules.jsonl`, which fed the
// concepts section F7 removed, and reporting it here would have made a healthy claim ledger read as
// degraded because a store this screen no longer touches was partial.
export function EvidenceSourceNotice({ claims }) {
  if (claims.status === 'complete') return null
  return <div className="notice resource-warning ledger-degraded" role="status">
    <b>Evidence source incomplete.</b>
    <span>Claims {claims.status}.
      {' Absence and one-sided claim state withheld.'}</span>
  </div>
}

export function LedgerEmptyState({ sourceStates,
  claimSource = { status: 'unknown' },
  pending = [], retry, busy, activeRetryKey = '', memorySettingsNeeded = false }) {
  const pendingSources = new Set(pending)
  const evidenceCurrent = ['atlas', 'claims'].every(key => sourceStates[key]?.state === 'current'
    && !pendingSources.has(key))
  const completeEmpty = evidenceCurrent && claimSource.status === 'complete'
  return <section className="ledger-empty" aria-labelledby="ledger-empty-title">
    <div className="ledger-empty-copy">
      <div className="ledger-empty-message" role="status" aria-atomic="true">
        <h2 id="ledger-empty-title">{completeEmpty
          ? 'No cross-run evidence'
          : evidenceCurrent ? 'No retained evidence' : 'Claim evidence unavailable'}</h2>
        <p>{completeEmpty
          ? 'No evidence returned; runs may still exist.'
          : evidenceCurrent ? 'Empty rows do not prove absence.'
          : 'Retry unavailable or stale sources.'}</p>
      </div>
      {memorySettingsNeeded && <div className="ledger-empty-actions">
        <a className="btn" href="#/settings">Memory settings</a>
      </div>}
    </div>
    <ul className="ledger-source-readiness" aria-label="Claim and curation source readiness">
      {SOURCES.map(([key, , label]) => {
        const state = sourceStates[key]?.state || 'failed'
        const loading = pendingSources.has(key)
        const status = loading ? 'loading' : state === 'current'
          ? ['atlas', 'claims'].includes(key) ? claimSource.status : 'complete'
          : state === 'retained-stale' ? 'stale' : 'unavailable'
        const retryable = state !== 'current'
        const activeRetry = loading && activeRetryKey === key
        return <li key={key}
        className={`ledger-empty-source ledger-source-focus-target ledger-empty-source-${loading ? 'loading' : state}`}
        data-ledger-source={key} tabIndex={-1}>
        <span className="ledger-readiness-dot" aria-hidden="true" />
        <span className="ledger-empty-source-head">
          <strong>{label}</strong><span className="ledger-readiness-state">{status}</span>
        </span>
        {retryable && <button type="button" className="btn sm ledger-retry-action"
          disabled={busy && !activeRetry} aria-disabled={activeRetry || undefined}
          aria-busy={activeRetry || undefined}
          onClick={event => retry(key, 'empty-source', event.currentTarget)}
          aria-label={`${activeRetry ? 'Retrying' : 'Retry'} ${label}`}>
          {activeRetry ? 'Retrying…' : 'Retry'}
        </button>}
      </li>})}
    </ul>
  </section>
}

export function ClaimCard({ claim, compact = false }) {
  const groups = [
    ['support', claim.support, claim.nSupport], ['oppose', claim.oppose, claim.nOppose],
    ['unverified', claim.unverified, claim.nUnverified],
    ['contradiction', claim.contradicts, claim.nContradicts],
  ]
  const evidence = groups.flatMap(([kind, values]) => values.map(value => [kind, value]))
  const hiddenEvidence = groups.reduce((sum, [, values, total]) =>
    sum + Math.max(0, total - values.length), 0)
  const context = [
    ...claim.scopes.map(value => `claim grouping · ${value}`),
    ...claim.runs.map(value => `run · ${value}`),
  ]
  const epistemicCopy = EPISTEMIC_COPY[claim.epistemic] || EPISTEMIC_COPY.inconclusive
  const decisionWarning = claim.maturity !== 'machine-proposed' && claim.decisionFresh !== true
  const safeSource = value => {
    try {
      if (typeof value !== 'string' || value.length > 2_000) return ''
      const url = new URL(value)
      return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password
        ? url.href : ''
    } catch { return '' }
  }
  return <article className={`ledger-claim ledger-state-${claim.epistemic}`}>
    <div className="ledger-claim-head">
      <span className={`chip xs ledger-epistemic ${claim.epistemic}`}>
        {epistemicCopy}
      </span>
      <span className="pill">
        {claim.maturity.replaceAll('-', ' ')}
        {decisionWarning && ` · ⚠ ${claim.decisionFresh === false ? 'stale' : 'freshness unknown'}`}
      </span>
    </div>
    <p>{claim.statement}</p>
    {(claim.metric || claim.polarity != null) && <div className="muted">
      {claim.metric && <>metric · {claim.metric}</>}
      {claim.metric && claim.polarity != null && <> · </>}
      {claim.polarity != null && <>polarity · {claim.polarity > 0 ? 'supporting' : 'opposing'}</>}
    </div>}
    {claim.decision && <div className="muted">
      Decision {claim.decision.action}
      {claim.decision.by && <> by {claim.decision.by}</>}
      {claim.decision.at && <> at {claim.decision.at}</>}
      {claim.decision.note && <> · {claim.decision.note}</>}
    </div>}
    <div className="ledger-claim-counts" aria-label="Claim evidence counts">
      {groups.map(([kind, , total], index) => (index < 2 || total > 0)
        && <span key={kind}>{kind}{kind === 'contradiction' ? 's' : ' refs'} <b>{total}</b></span>)}
      {claim.scopes.length > 0 && <span title={claim.scopes.join(', ')}>
        {countLabel(claim.scopes.length, 'claim grouping')}
      </span>}
    </div>
    {context.length > 0 && <div className="ledger-claim-context" aria-label="Claim groups and runs">
      {context.slice(0, 3).map((value, index) => <span className="pill" key={index}>{value}</span>)}
      {context.length > 3 && <span className="muted">+{context.length - 3} more</span>}
    </div>}
    {!compact && (evidence.length > 0 || hiddenEvidence > 0) && <details>
      <summary>Show evidence context</summary>
      <div className="ledger-evidence">
        {evidence.length === 0 && <span className="ledger-evidence-boundary">No context returned.</span>}
        {evidence.map(([kind, value], index) => <code key={`${kind}-${index}`}>
          {kind} · {value}
        </code>)}
        {hiddenEvidence > 0 && <span className="ledger-evidence-boundary">
          {countLabel(hiddenEvidence, 'additional reference')} not shown (claim limit).
        </span>}
      </div>
    </details>}
    {!compact && (claim.sources.length > 0 || claim.verification.length > 0) && <details>
      <summary>Sources and verification</summary>
      <div className="ledger-evidence">
        {claim.sources.map((value, index) => safeSource(value)
          ? <a key={`source-${index}`} href={safeSource(value)} target="_blank" rel="noreferrer">{value}</a>
          : <code key={`source-${index}`}>{value}</code>)}
        {claim.verification.map((value, index) => <code key={`verification-${index}`}>
          verification · {value}</code>)}
        {claim.evidenceDigest && <code>evidence digest · {claim.evidenceDigest}</code>}
      </div>
    </details>}
  </article>
}

function RouteState({ kind, errors, errorKind, onRetry, busy }) {
  if (kind === 'loading') return <div className="run-resource-state" role="status">
    <span className="dag-empty-spinner" aria-hidden="true" />
    <h1 id="ledger-route-state-title">Loading claims and curation</h1>
  </div>
  const memoryMissing = errorKind
    ? errorKind === 'memory'
    : errors.length > 0 && errors.every(error => error.status === 400)
  return <div className="run-resource-state">
    <div className="ledger-route-state-message" role={memoryMissing ? 'status' : 'alert'} aria-atomic="true">
      <h1 id="ledger-route-state-title">{memoryMissing
        ? 'Claim ledger not configured' : 'Claims & Curation couldn’t load'}</h1>
      <p>{memoryMissing
        ? 'Set Memory dir in Settings, then try again. Your runs were not changed.'
        : 'None of the claim or curation sources returned usable data. Your runs were not changed.'}</p>
    </div>
    <div className="resource-state-actions">
      {memoryMissing && <a className="btn primary" href="#/settings">Open Settings</a>}
      <button type="button" className={`btn ${memoryMissing ? '' : 'primary'}`}
        data-ledger-retry="route" aria-disabled={busy || undefined} aria-busy={busy || undefined}
        onClick={onRetry}>{busy ? 'Trying again…' : 'Try again'}</button>
    </div>
  </div>
}

export default function ClaimsCuration({ onBack }) {
  const [request, setRequest] = useState({ key: '', attempt: 0 })
  const requestId = useRef(0)
  const busyRef = useRef(true)
  const focusAttemptRef = useRef(0)
  const retryFocusRef = useRef(null)
  const ledgerMainRef = useRef(null)
  const [resource, setResource] = useState({
    status: 'loading', view: null, payload: null, errors: [], pending: [], attempt: 0,
    routeErrorKind: '',
    sourceStates: reconcileLedgerSourceStatuses({}, {}, ''),
  })

  const clearRetryFocus = () => {
    retryFocusRef.current?.cleanup?.()
    retryFocusRef.current = null
  }

  useEffect(() => {
    let active = true
    const controllers = []
    const id = ++requestId.current
    const requestedSources = request.key ? SOURCES.filter(([key]) => key === request.key) : SOURCES
    const keys = requestedSources.map(([key]) => key)
    const fullRefresh = keys.length === SOURCES.length
    let batchPortfolioId = ''
    let remaining = requestedSources.length
    busyRef.current = true
    setResource(current => ({ ...current,
      status: current.view ? 'refreshing' : current.errors.length > 0 ? 'retrying' : 'loading',
      errors: current.errors.filter(error => !keys.includes(error.key)),
      routeErrorKind: !current.view && current.errors.length > 0
        ? current.errors.every(error => error.status === 400) ? 'memory' : 'generic'
        : current.routeErrorKind,
      pending: keys, attempt: request.attempt,
    }))
    const settle = (key, value, error) => {
      if (!active || id !== requestId.current) return
      let valid = isValidLedgerSourceEnvelope(key, value)
      const incomingPortfolioId = valid ? sourcePortfolioId(value) : ''
      if (valid && batchPortfolioId && incomingPortfolioId !== batchPortfolioId) valid = false
      if (valid && !batchPortfolioId) batchPortfolioId = incomingPortfolioId
      const projected = valid ? projectLedgerSource(key, value) : null
      const last = --remaining === 0
      if (last) busyRef.current = false
      setResource(current => {
        const priorPortfolioId = ledgerPayloadPortfolioId(current.payload)
        const candidate = valid ? mergeLedgerPayload(
          current.payload, { [key]: projected }, { allowPortfolioSwitch: fullRefresh }) : current.payload
        const accepted = valid && candidate?.[key] === projected
        const successful = accepted ? { [key]: projected } : {}
        const failed = accepted ? [] : [{ key, status: error?.status }]
        const switched = accepted && priorPortfolioId && priorPortfolioId !== incomingPortfolioId
        const payload = accepted ? candidate : current.payload
        const view = accepted ? buildClaimsCurationView(payload.atlas, payload.claims,
          mergeCurationLogs(payload.conceptCuration, payload.claimCuration)) : current.view
        const nextErrors = [...current.errors.filter(item => item.key !== key), ...failed]
        return {
          ...current, payload, view,
          status: last ? (view ? 'ready' : 'error')
            : (view ? 'refreshing' : current.status === 'retrying' ? 'retrying' : 'loading'),
          errors: nextErrors,
          routeErrorKind: last && !view
            ? nextErrors.length > 0 && nextErrors.every(item => item.status === 400)
              ? 'memory' : 'generic'
            : current.routeErrorKind,
          pending: current.pending.filter(item => item !== key),
          sourceStates: reconcileLedgerSourceStatuses(switched ? {} : current.sourceStates, successful,
            new Date().toISOString(), [key]),
        }
      })
    }
    requestedSources.forEach(([key, read]) => {
      const timed = deadlineRequest(read, SOURCE_TIMEOUT_MS)
      controllers.push(timed.controller)
      timed.promise.then(value => settle(key, value), error => settle(key, null, error))
    })
    return () => {
      active = false
      controllers.forEach(controller => controller.abort())
    }
  }, [request])

  useEffect(() => () => clearRetryFocus(), [])

  useLayoutEffect(() => {
    const intent = retryFocusRef.current
    if (!intent || resource.attempt !== intent.attempt) return
    const routeBecameUsable = intent.origin === 'route' && Boolean(resource.view)
    if (!routeBecameUsable && resource.pending.length > 0) return
    const active = document.activeElement
    const focusStillOwned = active === intent.triggerNode || active === document.body
      || active === document.documentElement || !active?.isConnected
    if (intent.yielded || !focusStillOwned || !ledgerMainRef.current?.isConnected
      || document.querySelector('[aria-modal="true"]')) {
      clearRetryFocus()
      return
    }
    if (routeBecameUsable) {
      clearRetryFocus()
      ledgerMainRef.current.focus({ preventScroll: true })
      return
    }
    const failed = intent.keys.some(key => resource.errors.some(error => error.key === key))
    let target = null
    if (failed) {
      target = intent.triggerNode?.isConnected ? intent.triggerNode
        : document.querySelector(resource.view
          ? '[data-ledger-retry="topbar"]' : '[data-ledger-retry="route"]')
    } else if (intent.key) {
      target = intent.surfaceNode?.isConnected ? intent.surfaceNode : ledgerMainRef.current
    } else if (intent.origin === 'topbar' && intent.triggerNode?.isConnected) {
      target = intent.triggerNode
    } else {
      target = ledgerMainRef.current
    }
    clearRetryFocus()
    target?.focus({ preventScroll: true })
  }, [resource.attempt, resource.pending, resource.errors, resource.view])

  const retry = (key = '', origin = 'topbar', triggerNode = null) => {
    if (busyRef.current) return
    const attempt = ++focusAttemptRef.current
    clearRetryFocus()
    const intent = {
      attempt, key, origin, triggerNode,
      keys: key ? [key] : SOURCES.map(([sourceKey]) => sourceKey),
      surfaceNode: triggerNode?.closest('[data-ledger-source]') || null,
      yielded: false,
      cleanup: null,
    }
    const markYielded = event => {
      if (event.type === 'focusin'
        && (event.target === document.body || event.target === document.documentElement)) return
      if (event.target !== triggerNode && !triggerNode?.contains(event.target)) intent.yielded = true
    }
    document.addEventListener('focusin', markYielded, true)
    document.addEventListener('pointerdown', markYielded, true)
    intent.cleanup = () => {
      document.removeEventListener('focusin', markYielded, true)
      document.removeEventListener('pointerdown', markYielded, true)
    }
    retryFocusRef.current = intent
    busyRef.current = true
    setRequest({ key, attempt })
  }
  const view = resource.view
  const sourceStates = resource.sourceStates
  const mixedLoaded = sourceStates.atlas.state !== 'failed'
  const claimsLoaded = sourceStates.claims.state !== 'failed'
  const curationCurrent = sourceStates.conceptCuration.state === 'current'
    && sourceStates.claimCuration.state === 'current'
  const hasRetainedStale = Object.values(sourceStates).some(source => source.state === 'retained-stale')
  const hasMissing = SOURCES.some(([key]) => sourceStates[key].state === 'failed'
    && !resource.pending.includes(key))
  const evidenceErrors = resource.errors.filter(error => error.key === 'atlas' || error.key === 'claims')
  const memorySettingsNeeded = evidenceErrors.length > 0
    && evidenceErrors.every(error => error.status === 400)
  const busy = busyRef.current
  const topbarOwnsRefresh = busy && request.key === '' && retryFocusRef.current?.origin === 'topbar'
  return <div className="app claims-curation-route">
    <div className="topbar">
      <GlobalMenu current="claims" />
      <button type="button" className="btn sm ghost" aria-label="Back to runs" onClick={onBack}>← runs</button>
      <span className="ttl">Claims &amp; Curation</span>
      <span className="chip xs warn">Experimental · bounded · read-only</span>
      <span className="spacer" />
      {view && <button type="button" className="btn sm" data-ledger-retry="topbar"
        aria-label="Refresh all claim and curation sources" disabled={busy && !topbarOwnsRefresh}
        aria-disabled={topbarOwnsRefresh || undefined} aria-busy={busy || undefined}
        onClick={event => retry('', 'topbar', event.currentTarget)}>
        {busy ? 'Refreshing…' : 'Refresh all'}
      </button>}
    </div>

    <main ref={ledgerMainRef} className="claims-curation-page" data-route-main tabIndex={-1}
      aria-busy={busy} aria-labelledby={view ? 'ledger-title' : 'ledger-route-state-title'}>
      {!view
        ? <RouteState kind={resource.status} errors={resource.errors}
          errorKind={resource.routeErrorKind} busy={busy}
          onRetry={event => retry('', 'route', event.currentTarget)} />
        : <div className="ledger-content">
          <header className="ledger-intro">
            <h1 id="ledger-title">Claims &amp; Curation</h1>
            {/* What this surface IS, before what it is not. Claims are DERIVED here — a projection
                over the same lessons the Memory panel lists plus the deep-research memo claims — so an
                operator who reads a claim and then cannot find it verbatim in Memory is not looking at
                a bug. */}
            <p>What the portfolio has <b>claimed</b>, and what the paid stewards <b>proposed</b> and what
              came of it — rolled up across the <b>retained, readable run rows</b> in the shared memory
              dir, where Lab → Cross-run memory shows the stores themselves. A claim groups the same
              lessons by statement and records what supports and what opposes each one.</p>
            <p>Rows do not prove coverage. D8 receipts cover processed rows,
              not every run.</p>
            {/* This screen was the "Research Atlas" and its first section was a concept list. The run
                list's Concepts view is strictly richer — the full `is_a` forest, co-occurrence, a
                per-concept detail pane and the lessons/cases/notes carrying each concept — so the
                weaker copy was removed rather than kept in parallel (doc 29 F7). The link is what is
                left of it: the old NAME is what made an operator come here expecting a concept map,
                and one who still does must be told where the real one is. */}
            <p className="ledger-elsewhere">Looking for concepts? The run list's{' '}
              <a href="#/concepts">Concepts view</a> is the cross-run concept map — the <code>is_a</code>{' '}
              forest, co-occurrence, and the lessons, cases and notes that carry each concept.</p>
          </header>

          {resource.errors.length > 0 && <div className="notice resource-warning ledger-degraded" role="status">
            <b>{hasRetainedStale
              ? `Refresh incomplete; showing stale last-good data${hasMissing
                ? '; some sources unavailable' : ''}.`
              : 'Some sources unavailable.'}</b>
            <span>{countLabel(resource.errors.length, 'source refresh', 'source refreshes')} failed.</span>
          </div>}

          {view.invalidRows.total > 0 && <div className="notice resource-warning ledger-degraded" role="alert">
            <b>Some portfolio records were ignored.</b>
            <span>{countLabel(view.invalidRows.total, 'record')}; totals may include them.</span>
          </div>}

          <EvidenceSourceNotice claims={view.claimSource} />

          <section className="ledger-summary" aria-label="Claim and curation summary">
            {[
              ['Referenced runs', view.totals.runs, claimsLoaded || mixedLoaded],
              ['Claims', view.totals.claims, claimsLoaded],
              ['Mixed evidence', view.totals.contested, mixedLoaded],
              ['Steward records', view.totals.curation, curationCurrent],
            ].map(([label, value, loaded]) => <div className="ledger-stat" key={label}>
              <span>{label}</span><strong>{loaded ? value : 'not loaded'}</strong>
            </div>)}
          </section>

          {view.empty && <LedgerEmptyState sourceStates={sourceStates}
            claimSource={view.claimSource}
            pending={resource.pending}
            retry={retry} busy={busy} activeRetryKey={busy ? request.key : ''}
            memorySettingsNeeded={memorySettingsNeeded} />}

          {!view.empty && <div className="ledger-grid">
            <section className="ledger-panel ledger-contradictions" aria-labelledby="ledger-mixed">
              <div className="ledger-panel-head">
                <h2 id="ledger-mixed">Mixed-evidence claim records</h2>
                <span className="chip xs warn">{mixedLoaded ? `${view.totals.contested} mixed` : 'not loaded'}</span>
              </div>
              {mixedLoaded && (view.contradictions.length > 0
                ? <div className="ledger-claim-list compact" role="region" tabIndex={0}
                    aria-label="Bounded mixed-evidence claim records">{view.contradictions.map((claim, index) =>
                    <ClaimCard key={`${claim.uid || claim.statement}-${index}`} claim={claim} compact />)}</div>
                : <p className="ledger-section-empty">None returned.</p>)}
              {mixedLoaded && view.hiddenContradictions > 0 && <p className="ledger-boundary-note">
                {countLabel(view.hiddenContradictions, 'additional mixed-evidence record')} not shown (bounded projection).
              </p>}
              {/* This watermark carries the atlas slice's retry now. It used to opt OUT of one,
                  because the concepts panel above it owned the single retry for that same source;
                  that panel is gone, so without this the operator could not retry a failed
                  mixed-evidence read at all outside the empty state. */}
              <SourceWatermark sourceKey="atlas" label="Mixed claims"
                source={sourceStates.atlas} retry={retry} busy={busy}
                pending={resource.pending.includes('atlas')}
                activeRetry={busy && request.key === 'atlas'}>
                not a verdict or applicability decision.
              </SourceWatermark>
            </section>

            <section className="ledger-panel ledger-all-claims" aria-labelledby="ledger-claims">
              <div className="ledger-panel-head">
                <h2 id="ledger-claims">Claim records</h2>
                <span className="muted">{claimsLoaded
                  ? `showing ${view.claims.length} of ${view.totals.claims}` : 'not loaded'}</span>
              </div>
              {claimsLoaded && (view.claims.length > 0
                ? <div className="ledger-claim-list" role="region" tabIndex={0}
                    aria-label="Bounded portfolio claims">{view.claims.map((claim, index) =>
                    <ClaimCard key={`${claim.uid || claim.statement}-${index}`} claim={claim} />)}</div>
                : <p className="ledger-section-empty">No claims returned.</p>)}
              {claimsLoaded && view.hiddenClaims > 0 && <p className="ledger-boundary-note">
                {countLabel(view.hiddenClaims, 'additional claim')} not shown (render limit).
              </p>}
              <SourceWatermark sourceKey="claims" label="Claim records"
                source={sourceStates.claims} retry={retry} busy={busy}
                pending={resource.pending.includes('claims')}
                activeRetry={busy && request.key === 'claims'}>
                maturity differs from evidence.
              </SourceWatermark>
            </section>

            <section className="ledger-panel ledger-curation" aria-labelledby="ledger-curation">
              <div className="ledger-panel-head">
                <h2 id="ledger-curation">Recent proposals + outcomes</h2>
                <span className="muted">{curationCurrent
                  ? `showing ${view.curation.length} of ${view.totals.curation}`
                  : 'incomplete merge'}</span>
              </div>
              {view.curation.length > 0
                ? <ol className="ledger-curation-list">{view.curation.map((entry, index) =>
                    <li key={`${entry.kind}-${index}`}>
                      <b>{entry.kind} steward</b> · {countLabel(entry.proposals, 'proposal')} ·
                      {' '}{entry.applied ? `${entry.applied} applied` : entry.outcome.replaceAll('-', ' ')}
                    </li>)}</ol>
                : <p className="ledger-section-empty">{curationCurrent
                  ? 'No steward records returned.'
                  : 'No records shown; merge incomplete.'}</p>}
              {view.hiddenCuration > 0 && <p className="ledger-boundary-note">
                {countLabel(view.hiddenCuration, 'older entry', 'older entries')} not shown (render limit).
              </p>}
              {[['conceptCuration', 'Concept'], ['claimCuration', 'Claim']].map(([sourceKey, kind]) =>
                <SourceWatermark key={sourceKey} sourceKey={sourceKey}
                  label={`${kind} steward log`} source={sourceStates[sourceKey]} retry={retry}
                  busy={busy} pending={resource.pending.includes(sourceKey)}
                  activeRetry={busy && request.key === sourceKey}>
                  history, not current governance.
                </SourceWatermark>)}
            </section>
          </div>}
        </div>}
    </main>
  </div>
}
