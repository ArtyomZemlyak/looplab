import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  getCrossRunAtlas, getCrossRunClaims, getCrossRunCurationLog, getCrossRunClaimCurationLog,
  projectResearchAtlasSource,
} from './api.js'
import {
  atlasPortfolioId,
  buildResearchAtlasView, mergeCurationLogs, mergeResearchAtlasPayload,
  isValidAtlasSourceEnvelope, reconcileAtlasSourceStatuses, researchAtlasPayloadPortfolioId,
} from './researchAtlasModel.js'
import './research-atlas.css'
import GlobalMenu from './GlobalMenu.jsx'
import { deadlineRequest } from './requestDeadline.js'

const SOURCES = [
  ['atlas', signal => getCrossRunAtlas(24, { signal }), 'Concept + evidence'],
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
export function AtlasRunReference({ run }) {
  const context = [run.task && `task: ${run.task}`, run.scope && `scope: ${run.scope}`]
    .filter(Boolean).join(' · ')
  const disclosure = run.metricSuppressed
    ? 'Metric hidden · missing task/scope or objective direction.'
    : run.metric != null && run.optimizationOrientation
    ? `Not comparable across runs · metric name/unit unknown · ${run.optimizationOrientation} · ${run.metric.toLocaleString(undefined, { maximumSignificantDigits: 6 })}`
    : ''
  return <a href={`#/run/${encodeURIComponent(run.runId)}`}>
    <span className="atlas-runref-id">{run.runId}</span>
    {context && <span className="atlas-runref-context">{context}</span>}
    {disclosure && <span className={run.metricSuppressed
      ? 'atlas-runref-suppressed' : 'atlas-runref-warning'}>{disclosure}</span>}
  </a>
}

function SourceWatermark({ sourceKey, label, source, retry, busy, pending,
  activeRetry = false, retryable = true, children }) {
  const state = source.state
  return <p className={`atlas-source-note atlas-source-focus-target atlas-source-${pending ? 'loading' : state}`}
    data-atlas-source={sourceKey} tabIndex={-1}>
    <strong>{label}</strong> · {pending ? 'loading'
      : state === 'retained-stale' ? 'stale' : state === 'failed' ? 'unavailable' : 'loaded'}
    {' · '}revision {source.revision || 'unknown'} · {children}
    {retryable && state !== 'current' && <> · <button type="button" className="btn sm atlas-retry-action"
      disabled={busy && !activeRetry} aria-disabled={activeRetry || undefined}
      aria-busy={activeRetry || undefined}
      onClick={event => retry(sourceKey, 'watermark', event.currentTarget)}
      aria-label={`${activeRetry ? 'Retrying' : 'Retry'} ${label}`}>
      {activeRetry ? 'Retrying…' : 'Retry'}</button></>}
  </p>
}

export function EvidenceSourceNotice({ concept, claims }) {
  if (concept.status === 'complete' && claims.status === 'complete') return null
  return <div className="notice resource-warning atlas-degraded" role="status">
    <b>Evidence source incomplete.</b>
    <span>Concepts {concept.status}; claims {claims.status}.
      {concept.status === 'partial' && ` Concept bounds ${concept.counts.join('/')}.`}
      {' Absence and one-sided claim state withheld.'}</span>
  </div>
}

export function AtlasEmptyState({ sourceStates, conceptSource,
  claimSource = { status: 'unknown' },
  pending = [], retry, busy, activeRetryKey = '', memorySettingsNeeded = false }) {
  const pendingSources = new Set(pending)
  const evidenceCurrent = ['atlas', 'claims'].every(key => sourceStates[key]?.state === 'current'
    && !pendingSources.has(key))
  const completeEmpty = evidenceCurrent && conceptSource.status === 'complete'
    && claimSource.status === 'complete'
  return <section className="atlas-empty" aria-labelledby="atlas-empty-title">
    <div className="atlas-empty-copy">
      <div className="atlas-empty-message" role="status" aria-atomic="true">
        <h2 id="atlas-empty-title">{completeEmpty
          ? 'No cross-run evidence'
          : evidenceCurrent ? 'No retained evidence' : 'Atlas evidence unavailable'}</h2>
        <p>{completeEmpty
          ? 'No evidence returned; runs may still exist.'
          : evidenceCurrent ? 'Empty rows do not prove absence.'
          : 'Retry unavailable or stale sources.'}</p>
      </div>
      {memorySettingsNeeded && <div className="atlas-empty-actions">
        <a className="btn" href="#/settings">Memory settings</a>
      </div>}
    </div>
    <ul className="atlas-source-readiness" aria-label="Atlas source readiness">
      {SOURCES.map(([key, , label]) => {
        const state = sourceStates[key]?.state || 'failed'
        const loading = pendingSources.has(key)
        const status = loading ? 'loading' : state === 'current'
          ? key === 'atlas' ? conceptSource.status : key === 'claims' ? claimSource.status : 'complete'
          : state === 'retained-stale' ? 'stale' : 'unavailable'
        const retryable = state !== 'current'
        const activeRetry = loading && activeRetryKey === key
        return <li key={key}
        className={`atlas-empty-source atlas-source-focus-target atlas-empty-source-${loading ? 'loading' : state}`}
        data-atlas-source={key} tabIndex={-1}>
        <span className="atlas-readiness-dot" aria-hidden="true" />
        <span className="atlas-empty-source-head">
          <strong>{label}</strong><span className="atlas-readiness-state">{status}</span>
        </span>
        {retryable && <button type="button" className="btn sm atlas-retry-action"
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
  return <article className={`atlas-claim atlas-state-${claim.epistemic}`}>
    <div className="atlas-claim-head">
      <span className={`chip xs atlas-epistemic ${claim.epistemic}`}>
        {epistemicCopy}
      </span>
      <span className="pill">
        {claim.maturity.replaceAll('-', ' ')}
        {decisionWarning && ` · ⚠ ${claim.decisionFresh === false ? 'stale' : 'freshness unknown'}`}
      </span>
    </div>
    <p>{claim.statement}</p>
    <div className="atlas-claim-counts" aria-label="Claim evidence counts">
      {groups.map(([kind, , total], index) => (index < 2 || total > 0)
        && <span key={kind}>{kind}{kind === 'contradiction' ? 's' : ' refs'} <b>{total}</b></span>)}
      {claim.scopes.length > 0 && <span title={claim.scopes.join(', ')}>
        {countLabel(claim.scopes.length, 'claim grouping')}
      </span>}
    </div>
    {context.length > 0 && <div className="atlas-claim-context" aria-label="Claim groups and runs">
      {context.slice(0, 3).map((value, index) => <span className="pill" key={index}>{value}</span>)}
      {context.length > 3 && <span className="muted">+{context.length - 3} more</span>}
    </div>}
    {!compact && (evidence.length > 0 || hiddenEvidence > 0) && <details>
      <summary>Show evidence context</summary>
      <div className="atlas-evidence">
        {evidence.length === 0 && <span className="atlas-evidence-boundary">No context returned.</span>}
        {evidence.map(([kind, value], index) => <code key={`${kind}-${index}`}>
          {kind} · {value}
        </code>)}
        {hiddenEvidence > 0 && <span className="atlas-evidence-boundary">
          {countLabel(hiddenEvidence, 'additional reference')} not shown (claim limit).
        </span>}
      </div>
    </details>}
  </article>
}

function RouteState({ kind, errors, errorKind, onRetry, busy }) {
  if (kind === 'loading') return <div className="run-resource-state" role="status">
    <span className="dag-empty-spinner" aria-hidden="true" />
    <h1 id="atlas-route-state-title">Loading Atlas</h1>
  </div>
  const memoryMissing = errorKind
    ? errorKind === 'memory'
    : errors.length > 0 && errors.every(error => error.status === 400)
  return <div className="run-resource-state">
    <div className="atlas-route-state-message" role={memoryMissing ? 'status' : 'alert'} aria-atomic="true">
      <h1 id="atlas-route-state-title">{memoryMissing ? 'Atlas not configured' : 'Research Atlas couldn’t load'}</h1>
      <p>{memoryMissing
        ? 'Set Memory dir in Settings, then try again. Your runs were not changed.'
        : 'None of the Atlas sources returned usable data. Your runs were not changed.'}</p>
    </div>
    <div className="resource-state-actions">
      {memoryMissing && <a className="btn primary" href="#/settings">Open Settings</a>}
      <button type="button" className={`btn ${memoryMissing ? '' : 'primary'}`}
        data-atlas-retry="route" aria-disabled={busy || undefined} aria-busy={busy || undefined}
        onClick={onRetry}>{busy ? 'Trying again…' : 'Try again'}</button>
    </div>
  </div>
}

export default function ResearchAtlas({ onBack }) {
  const [request, setRequest] = useState({ key: '', attempt: 0 })
  const requestId = useRef(0)
  const busyRef = useRef(true)
  const focusAttemptRef = useRef(0)
  const retryFocusRef = useRef(null)
  const atlasMainRef = useRef(null)
  const [resource, setResource] = useState({
    status: 'loading', view: null, payload: null, errors: [], pending: [], attempt: 0,
    routeErrorKind: '',
    sourceStates: reconcileAtlasSourceStatuses({}, {}, ''),
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
      let valid = isValidAtlasSourceEnvelope(key, value)
      const incomingPortfolioId = valid ? atlasPortfolioId(value) : ''
      if (valid && batchPortfolioId && incomingPortfolioId !== batchPortfolioId) valid = false
      if (valid && !batchPortfolioId) batchPortfolioId = incomingPortfolioId
      const projected = valid ? projectResearchAtlasSource(key, value) : null
      const last = --remaining === 0
      if (last) busyRef.current = false
      setResource(current => {
        const priorPortfolioId = researchAtlasPayloadPortfolioId(current.payload)
        const candidate = valid ? mergeResearchAtlasPayload(
          current.payload, { [key]: projected }, { allowPortfolioSwitch: fullRefresh }) : current.payload
        const accepted = valid && candidate?.[key] === projected
        const successful = accepted ? { [key]: projected } : {}
        const failed = accepted ? [] : [{ key, status: error?.status }]
        const switched = accepted && priorPortfolioId && priorPortfolioId !== incomingPortfolioId
        const payload = accepted ? candidate : current.payload
        const view = accepted ? buildResearchAtlasView(payload.atlas, payload.claims,
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
          sourceStates: reconcileAtlasSourceStatuses(switched ? {} : current.sourceStates, successful,
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
    if (intent.yielded || !focusStillOwned || !atlasMainRef.current?.isConnected
      || document.querySelector('[aria-modal="true"]')) {
      clearRetryFocus()
      return
    }
    if (routeBecameUsable) {
      clearRetryFocus()
      atlasMainRef.current.focus({ preventScroll: true })
      return
    }
    const failed = intent.keys.some(key => resource.errors.some(error => error.key === key))
    let target = null
    if (failed) {
      target = intent.triggerNode?.isConnected ? intent.triggerNode
        : document.querySelector(resource.view
          ? '[data-atlas-retry="topbar"]' : '[data-atlas-retry="route"]')
    } else if (intent.key) {
      target = intent.surfaceNode?.isConnected ? intent.surfaceNode : atlasMainRef.current
    } else if (intent.origin === 'topbar' && intent.triggerNode?.isConnected) {
      target = intent.triggerNode
    } else {
      target = atlasMainRef.current
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
      surfaceNode: triggerNode?.closest('[data-atlas-source]') || null,
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
  const atlasLoaded = sourceStates.atlas.state !== 'failed'
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
  return <div className="app atlas-route">
    <div className="topbar">
      <GlobalMenu current="research-atlas" />
      <button type="button" className="btn sm ghost" aria-label="Back to runs" onClick={onBack}>← runs</button>
      <span className="ttl">Research Atlas preview</span>
      <span className="chip xs warn">Experimental · bounded · read-only</span>
      <span className="spacer" />
      {view && <button type="button" className="btn sm" data-atlas-retry="topbar"
        aria-label="Refresh all Research Atlas sources" disabled={busy && !topbarOwnsRefresh}
        aria-disabled={topbarOwnsRefresh || undefined} aria-busy={busy || undefined}
        onClick={event => retry('', 'topbar', event.currentTarget)}>
        {busy ? 'Refreshing…' : 'Refresh all'}
      </button>}
    </div>

    <main ref={atlasMainRef} className="research-atlas-page" data-route-main tabIndex={-1}
      aria-busy={busy} aria-labelledby={view ? 'atlas-title' : 'atlas-route-state-title'}>
      {!view
        ? <RouteState kind={resource.status} errors={resource.errors}
          errorKind={resource.routeErrorKind} busy={busy}
          onRetry={event => retry('', 'route', event.currentTarget)} />
        : <div className="atlas-content">
          <header className="atlas-intro">
            <h1 id="atlas-title">Portfolio evidence</h1>
            {/* What this surface IS, before what it is not. Concepts and claims are DERIVED here —
                claims are a projection over the same lessons the Memory panel lists plus the
                deep-research memo claims — so an operator who reads a claim and then cannot find it
                in Memory is not looking at a bug. */}
            <p>Rolled up across <b>every</b> run in the shared memory dir, where Lab → Memory shows the
              stores themselves. <b>Concepts</b> are the per-run capsules of what was tried;{' '}
              <b>claims</b> are a derived view that groups the same lessons by statement and records
              what supports and what opposes each one.</p>
            <p>Rows do not prove coverage. D8 receipts cover processed rows,
              not every run.</p>
          </header>

          {resource.errors.length > 0 && <div className="notice resource-warning atlas-degraded" role="status">
            <b>{hasRetainedStale
              ? `Refresh incomplete; showing stale last-good data${hasMissing
                ? '; some sources unavailable' : ''}.`
              : 'Some sources unavailable.'}</b>
            <span>{countLabel(resource.errors.length, 'source refresh', 'source refreshes')} failed.</span>
          </div>}

          {view.invalidRows.total > 0 && <div className="notice resource-warning atlas-degraded" role="alert">
            <b>Some portfolio records were ignored.</b>
            <span>{countLabel(view.invalidRows.total, 'record')}; totals may include them.</span>
          </div>}

          <EvidenceSourceNotice concept={view.conceptSource} claims={view.claimSource} />

          <section className="atlas-summary" aria-label="Portfolio summary">
            {[
              ['Referenced runs', view.totals.runs, atlasLoaded],
              ['Concepts', view.totals.concepts, atlasLoaded],
              ['Claims', view.totals.claims, claimsLoaded],
              ['Mixed evidence', view.totals.contested, atlasLoaded],
            ].map(([label, value, loaded]) => <div className="atlas-stat" key={label}>
              <span>{label}</span><strong>{loaded ? value : 'not loaded'}</strong>
            </div>)}
          </section>

          {view.empty && <AtlasEmptyState sourceStates={sourceStates} conceptSource={view.conceptSource}
            claimSource={view.claimSource}
            pending={resource.pending}
            retry={retry} busy={busy} activeRetryKey={busy ? request.key : ''}
            memorySettingsNeeded={memorySettingsNeeded} />}

          {!view.empty && <div className="atlas-grid">
            <section className="atlas-panel atlas-coverage" aria-labelledby="atlas-concepts">
              <div className="atlas-panel-head">
                <h2 id="atlas-concepts">Concepts seen across runs</h2>
                <span className="muted">{atlasLoaded
                  ? `showing ${view.concepts.length} of ${view.totals.concepts}` : 'not loaded'}</span>
              </div>
              {atlasLoaded && <>
                  {view.concepts.length === 0
                    ? <p className="atlas-section-empty">No retained concepts returned.</p>
                    : <ul className="atlas-concepts" tabIndex={0} aria-label="Bounded explored concepts">
                      {view.concepts.map((concept, index) => <li key={`${concept.concept}-${index}`}>
                        <div><strong>{concept.concept}</strong><span>{countLabel(concept.nRuns, 'run')}
                          {' · '}helped {concept.nHelped} · neutral {concept.nNeutral} · hurt {concept.nHurt}
                        </span></div>
                        {concept.runs.length > 0 && <div className="atlas-runrefs">
                          {concept.runs.map((run, runIndex) => run.runId
                            ? <AtlasRunReference key={`${run.runId}-${runIndex}`} run={run} />
                            : null)}
                        </div>}
                      </li>)}
                    </ul>}
                  {view.hiddenConcepts > 0 && <p className="atlas-boundary-note">
                    {countLabel(view.hiddenConcepts, 'additional concept')} not shown (bounded projection).
                  </p>}
                  <div className="atlas-thin">
                    <h3>Observed in one run <span>{view.thin.length}</span></h3>
                    {view.thin.length > 0
                      ? <div className="atlas-tags">{view.thin.map((name, index) => <span className="pill" key={`${name}-${index}`}>{name}</span>)}</div>
                      : <p className="atlas-section-empty">None returned.</p>}
                    {view.hiddenThin > 0 && <p className="atlas-inline-boundary">
                      +{view.hiddenThin} more single-run concept{view.hiddenThin === 1 ? '' : 's'}
                    </p>}
                  </div>
              </>}
              <SourceWatermark sourceKey="atlas" label="Concept projection"
                source={sourceStates.atlas} retry={retry} busy={busy}
                pending={resource.pending.includes('atlas')}
                activeRetry={busy && request.key === 'atlas'}>
                bounded observations, not coverage.
              </SourceWatermark>
            </section>

            <section className="atlas-panel atlas-contradictions" aria-labelledby="atlas-mixed">
              <div className="atlas-panel-head">
                <h2 id="atlas-mixed">Mixed-evidence claim records</h2>
                <span className="chip xs warn">{atlasLoaded ? `${view.totals.contested} mixed` : 'not loaded'}</span>
              </div>
              {atlasLoaded && (view.contradictions.length > 0
                ? <div className="atlas-claim-list compact" role="region" tabIndex={0}
                    aria-label="Bounded mixed-evidence claim records">{view.contradictions.map((claim, index) =>
                    <ClaimCard key={`${claim.uid || claim.statement}-${index}`} claim={claim} compact />)}</div>
                : <p className="atlas-section-empty">None returned.</p>)}
              {atlasLoaded && view.hiddenContradictions > 0 && <p className="atlas-boundary-note">
                {countLabel(view.hiddenContradictions, 'additional mixed-evidence record')} not shown (bounded projection).
              </p>}
              <SourceWatermark sourceKey="atlas" label="Mixed claims"
                source={sourceStates.atlas} retry={retry} busy={busy}
                pending={resource.pending.includes('atlas')} retryable={false}>
                not a verdict or applicability decision.
              </SourceWatermark>
            </section>

            <section className="atlas-panel atlas-all-claims" aria-labelledby="atlas-claims">
              <div className="atlas-panel-head">
                <h2 id="atlas-claims">Claim records</h2>
                <span className="muted">{claimsLoaded
                  ? `showing ${view.claims.length} of ${view.totals.claims}` : 'not loaded'}</span>
              </div>
              {claimsLoaded && (view.claims.length > 0
                ? <div className="atlas-claim-list" role="region" tabIndex={0}
                    aria-label="Bounded portfolio claims">{view.claims.map((claim, index) =>
                    <ClaimCard key={`${claim.uid || claim.statement}-${index}`} claim={claim} />)}</div>
                : <p className="atlas-section-empty">No claims returned.</p>)}
              {claimsLoaded && view.hiddenClaims > 0 && <p className="atlas-boundary-note">
                {countLabel(view.hiddenClaims, 'additional claim')} not shown (render limit).
              </p>}
              <SourceWatermark sourceKey="claims" label="Claim records"
                source={sourceStates.claims} retry={retry} busy={busy}
                pending={resource.pending.includes('claims')}
                activeRetry={busy && request.key === 'claims'}>
                maturity differs from evidence.
              </SourceWatermark>
            </section>

            <section className="atlas-panel atlas-curation" aria-labelledby="atlas-curation">
              <div className="atlas-panel-head">
                <h2 id="atlas-curation">Recent proposals + outcomes</h2>
                <span className="muted">{curationCurrent
                  ? `showing ${view.curation.length} of ${view.totals.curation}`
                  : 'incomplete merge'}</span>
              </div>
              {view.curation.length > 0
                ? <ol className="atlas-curation-list">{view.curation.map((entry, index) =>
                    <li key={`${entry.kind}-${index}`}>
                      <b>{entry.kind} steward</b> · {countLabel(entry.proposals, 'proposal')} ·
                      {' '}{entry.applied ? `${entry.applied} applied` : entry.outcome.replaceAll('-', ' ')}
                    </li>)}</ol>
                : <p className="atlas-section-empty">{curationCurrent
                  ? 'No steward records returned.'
                  : 'No records shown; merge incomplete.'}</p>}
              {view.hiddenCuration > 0 && <p className="atlas-boundary-note">
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
