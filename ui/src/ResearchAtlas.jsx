import React, { useEffect, useRef, useState } from 'react'
import {
  getCrossRunAtlas, getCrossRunClaims, getCrossRunCurationLog, getCrossRunClaimCurationLog,
} from './api.js'
import {
  buildResearchAtlasView, mergeCurationLogs, mergeResearchAtlasPayload,
  isValidAtlasSourceEnvelope, reconcileAtlasSourceStatuses,
} from './researchAtlasModel.js'
import './research-atlas.css'
import { deadlineRequest } from './requestDeadline.js'

const SOURCES = [
  { key: 'atlas', read: signal => getCrossRunAtlas(24, { signal }) },
  { key: 'claims', read: signal => getCrossRunClaims(40, 0, { signal }) },
  { key: 'conceptCuration', read: signal => getCrossRunCurationLog(20, { signal }) },
  { key: 'claimCuration', read: signal => getCrossRunClaimCurationLog(20, { signal }) },
]
const SOURCE_TIMEOUT_MS = 15_000
const SOURCE_READINESS = [
  ['atlas', 'Concept + evidence'],
  ['claims', 'Claim records'],
  ['conceptCuration', 'Concept steward log'],
  ['claimCuration', 'Claim steward log'],
]

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

function SourceWatermark({ sourceKey, label, source, retry, busy, pending, children }) {
  const state = source.state
  return <p className={`atlas-source-note atlas-source-${pending ? 'loading' : state}`}>
    <strong>{label}</strong> · <span>{pending ? 'loading'
      : state === 'retained-stale' ? 'stale' : state === 'failed' ? 'unavailable' : 'loaded'}</span>
    {' · '}revision {source.revision || 'not reported'} · {children}
    {state !== 'current' && <> · <button type="button" className="btn sm" disabled={busy}
      onClick={() => retry(sourceKey)} aria-label={`Retry ${label}`}>
      {busy ? 'Refreshing…' : 'Retry'}</button></>}
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
  pending = [], retry, busy, onBack }) {
  const pendingSources = new Set(pending)
  const allCurrent = pending.length === 0
    && Object.values(sourceStates).every(source => source.state === 'current')
  const completeEmpty = allCurrent && conceptSource.status === 'complete'
    && claimSource.status === 'complete'
  return <section className="atlas-empty" aria-labelledby="atlas-empty-title" role="status">
    <div className="atlas-empty-copy">
      <h2 id="atlas-empty-title">{completeEmpty
        ? 'No cross-run evidence'
        : allCurrent ? 'No retained evidence' : 'Atlas evidence unavailable'}</h2>
      <p>{completeEmpty
        ? 'No shared-memory evidence returned; runs may still exist.'
        : allCurrent ? 'Incomplete receipts: empty rows do not prove absence.'
        : 'Retry unavailable or stale sources.'}</p>
      <div className="atlas-empty-actions">
        <button type="button" className="btn primary" onClick={onBack}>Back to runs</button>
        <a className="btn" href="#/settings">Memory settings</a>
      </div>
    </div>
    <ul className="atlas-source-readiness" aria-label="Atlas source readiness">
      {SOURCE_READINESS.map(([key, label]) => {
        const state = sourceStates[key]?.state || 'failed'
        const loading = pendingSources.has(key)
        const status = loading ? 'loading' : state === 'current'
          ? key === 'atlas' ? conceptSource.status : key === 'claims' ? claimSource.status : 'complete'
          : state === 'retained-stale' ? 'stale' : 'unavailable'
        const retryable = !loading && state !== 'current'
        return <li key={key}
        className={`atlas-empty-source atlas-empty-source-${loading ? 'loading' : state}`}>
        <strong>{label}</strong><span className="atlas-readiness-state">{status}</span>
        {retryable && <button type="button" className="btn sm" disabled={busy}
          onClick={() => retry(key)} aria-label={`Retry ${label}`}>
          {busy ? 'Refreshing…' : 'Retry'}
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
        {evidence.length === 0 && <span className="atlas-evidence-boundary">No evidence context returned.</span>}
        {evidence.map(([kind, value], index) => <code key={`${kind}-${index}`}>
          {kind} · {value}
        </code>)}
        {hiddenEvidence > 0 && <span className="atlas-evidence-boundary">
          {countLabel(hiddenEvidence, 'additional reference')} omitted by the claim limit.
        </span>}
      </div>
    </details>}
  </article>
}

function RouteState({ kind, errors, onRetry }) {
  if (kind === 'loading') return <div className="run-resource-state" role="status" aria-live="polite">
    <span className="dag-empty-spinner" aria-hidden="true" />
    <h1>Loading Atlas</h1>
  </div>
  const memoryMissing = errors.length > 0 && errors.every(error => error.status === 400)
  return <div className="run-resource-state" role={memoryMissing ? 'status' : 'alert'}>
    <h1>{memoryMissing ? 'Atlas not configured' : 'Atlas unavailable'}</h1>
    <p>{memoryMissing
      ? 'Set Memory dir in Settings.'
      : 'Atlas sources unavailable.'}</p>
    <div className="resource-state-actions">
      {memoryMissing && <a className="btn primary" href="#/settings">Open Settings</a>}
      <button type="button" className={`btn ${memoryMissing ? '' : 'primary'}`} onClick={onRetry}>Refresh all</button>
    </div>
  </div>
}

export default function ResearchAtlas({ onBack }) {
  const [request, setRequest] = useState({ key: '' })
  const requestId = useRef(0)
  const busyRef = useRef(true)
  const [resource, setResource] = useState({
    status: 'loading', view: null, payload: null, errors: [], pending: [],
    sourceStates: reconcileAtlasSourceStatuses({}, {}, ''),
  })

  useEffect(() => {
    let active = true
    const controllers = []
    const id = ++requestId.current
    const requestedSources = request.key ? SOURCES.filter(source => source.key === request.key) : SOURCES
    const keys = requestedSources.map(source => source.key)
    let remaining = requestedSources.length
    busyRef.current = true
    setResource(current => ({ ...current, status: current.view ? 'refreshing' : 'loading',
      errors: current.errors.filter(error => !keys.includes(error.key)), pending: keys }))
    const settle = (source, value, error) => {
      if (!active || id !== requestId.current) return
      const valid = isValidAtlasSourceEnvelope(source.key, value)
      const successful = valid ? { [source.key]: value } : {}
      const failed = valid ? [] : [{ key: source.key, status: error?.status }]
      const last = --remaining === 0
      if (last) busyRef.current = false
      setResource(current => {
        const payload = valid ? mergeResearchAtlasPayload(current.payload, successful) : current.payload
        const view = valid ? buildResearchAtlasView(payload.atlas, payload.claims,
          mergeCurationLogs(payload.conceptCuration, payload.claimCuration)) : current.view
        return {
          ...current, payload, view,
          status: last ? (view ? 'ready' : 'error') : (view ? 'refreshing' : 'loading'),
          errors: [...current.errors.filter(item => item.key !== source.key), ...failed],
          pending: current.pending.filter(key => key !== source.key),
          sourceStates: reconcileAtlasSourceStatuses(current.sourceStates, successful,
            new Date().toISOString(), [source.key]),
        }
      })
    }
    requestedSources.forEach(source => {
      const timed = deadlineRequest(source.read, SOURCE_TIMEOUT_MS)
      controllers.push(timed.controller)
      timed.promise.then(value => settle(source, value), error => settle(source, null, error))
    })
    return () => {
      active = false
      controllers.forEach(controller => controller.abort())
    }
  }, [request])

  const retry = (key = '') => {
    if (busyRef.current) return
    busyRef.current = true
    setRequest({ key })
  }
  const refresh = () => retry()
  const view = resource.view
  const sourceStates = resource.sourceStates
  const atlasLoaded = sourceStates.atlas.state !== 'failed'
  const claimsLoaded = sourceStates.claims.state !== 'failed'
  const states = Object.values(sourceStates)
  const curationCurrent = sourceStates.conceptCuration.state === 'current'
    && sourceStates.claimCuration.state === 'current'
  const hasRetainedStale = states.some(source => source.state === 'retained-stale')
  const hasMissing = SOURCES.some(source => sourceStates[source.key].state === 'failed'
    && !resource.pending.includes(source.key))
  const busy = busyRef.current
  return <div className="app atlas-route">
    <div className="topbar">
      <span className="brand"><span className="dot">◉</span> LoopLab</span>
      <button type="button" className="btn sm ghost" aria-label="Back to runs" onClick={onBack}>← runs</button>
      <span className="ttl">Research Atlas preview</span>
      <span className="chip xs warn">Experimental · bounded · read-only</span>
      <span className="spacer" />
      <button type="button" className="btn sm" aria-label="Refresh all Research Atlas sources"
              disabled={busy} onClick={refresh}>
        {busy ? 'Refreshing…' : 'Refresh all'}
      </button>
    </div>

    <main className="research-atlas-page" data-route-main tabIndex={-1}
      aria-busy={busy}>
      {!resource.view
        ? <RouteState kind={resource.status} errors={resource.errors} onRetry={refresh} />
        : <div className="atlas-content">
          <header className="atlas-intro">
            <div>
              <p className="atlas-eyebrow">Experimental Part IV/V</p>
              <h1>Bounded portfolio evidence preview</h1>
              <p>Read-only bounded records cannot establish coverage. D8 receipts cover only explicitly
                processed durable rows, not every portfolio run.</p>
            </div>
          </header>

          {resource.errors.length > 0 && <div className="notice resource-warning atlas-degraded" role="status">
            <b>{hasRetainedStale
              ? `Refresh incomplete; showing stale last-good data${hasMissing
                ? '; some sources unavailable' : ''}.`
              : 'Some sources unavailable.'}</b>
            <span>{countLabel(resource.errors.length, 'source refresh', 'source refreshes')} failed.</span>
            <button type="button" className="btn sm" disabled={busy} onClick={refresh}>Refresh all</button>
          </div>}

          {view.invalidRows.total > 0 && <div className="notice resource-warning atlas-degraded" role="alert">
            <b>Some portfolio records were ignored.</b>
            <span>{countLabel(view.invalidRows.total, 'record')}; server totals may still include them.</span>
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
            retry={retry} busy={busy} onBack={onBack} />}

          {!view.empty && <div className="atlas-grid">
            <section className="atlas-panel atlas-coverage" aria-labelledby="atlas-coverage-title">
              <div className="atlas-panel-head">
                <div><h2 id="atlas-coverage-title">Concepts seen across runs</h2></div>
                <span className="muted">{atlasLoaded
                  ? `showing ${view.concepts.length} of ${view.totals.concepts}` : 'not loaded'}</span>
              </div>
              {atlasLoaded && <>
                  {view.concepts.length === 0
                    ? <p className="atlas-section-empty">No retained concepts returned.</p>
                    : <ul className="atlas-concepts" tabIndex={0} aria-label="Bounded explored concepts">
                      {view.concepts.map((concept, index) => <li key={`${concept.concept}-${index}`}>
                        <div><strong>{concept.concept}</strong><span>{countLabel(concept.nRuns, 'run')}</span></div>
                        {concept.runs.length > 0 && <div className="atlas-runrefs">
                          {concept.runs.map((run, runIndex) => run.runId
                            ? <AtlasRunReference key={`${run.runId}-${runIndex}`} run={run} />
                            : null)}
                        </div>}
                      </li>)}
                    </ul>}
                  {view.hiddenConcepts > 0 && <p className="atlas-boundary-note">
                    {countLabel(view.hiddenConcepts, 'additional concept')} omitted by the bounded projection.
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
                pending={resource.pending.includes('atlas')}>
                bounded observations, not coverage.
              </SourceWatermark>
            </section>

            <section className="atlas-panel atlas-contradictions" aria-labelledby="atlas-contradictions-title">
              <div className="atlas-panel-head">
                <div><h2 id="atlas-contradictions-title">Mixed-evidence claim records</h2></div>
                <span className="chip xs warn">{atlasLoaded ? `${view.totals.contested} mixed` : 'not loaded'}</span>
              </div>
              {atlasLoaded && (view.contradictions.length > 0
                ? <div className="atlas-claim-list compact" role="region" tabIndex={0}
                    aria-label="Bounded mixed-evidence claim records">{view.contradictions.map((claim, index) =>
                    <ClaimCard key={`${claim.uid || claim.statement}-${index}`} claim={claim} compact />)}</div>
                : <p className="atlas-section-empty">None returned.</p>)}
              {atlasLoaded && view.hiddenContradictions > 0 && <p className="atlas-boundary-note">
                {countLabel(view.hiddenContradictions, 'additional mixed-evidence record')} omitted by the bounded projection.
              </p>}
              <SourceWatermark sourceKey="atlas" label="Mixed claims"
                source={sourceStates.atlas} retry={retry} busy={busy}
                pending={resource.pending.includes('atlas')}>
                not a verdict or applicability decision.
              </SourceWatermark>
            </section>

            <section className="atlas-panel atlas-all-claims" aria-labelledby="atlas-claims-title">
              <div className="atlas-panel-head">
                <div><h2 id="atlas-claims-title">Claim records</h2></div>
                <span className="muted">{claimsLoaded
                  ? `showing ${view.claims.length} of ${view.totals.claims}` : 'not loaded'}</span>
              </div>
              {claimsLoaded && (view.claims.length > 0
                ? <div className="atlas-claim-list" role="region" tabIndex={0}
                    aria-label="Bounded portfolio claims">{view.claims.map((claim, index) =>
                    <ClaimCard key={`${claim.uid || claim.statement}-${index}`} claim={claim} />)}</div>
                : <p className="atlas-section-empty">No claims returned.</p>)}
              {claimsLoaded && view.hiddenClaims > 0 && <p className="atlas-boundary-note">
                {countLabel(view.hiddenClaims, 'additional claim')} omitted by the client render limit.
              </p>}
              <SourceWatermark sourceKey="claims" label="Claim records"
                source={sourceStates.claims} retry={retry} busy={busy}
                pending={resource.pending.includes('claims')}>
                maturity differs from evidence.
              </SourceWatermark>
            </section>

            <section className="atlas-panel atlas-curation" aria-labelledby="atlas-curation-title">
              <div className="atlas-panel-head">
                <div><h2 id="atlas-curation-title">Recent proposals + outcomes</h2></div>
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
                {countLabel(view.hiddenCuration, 'older entry', 'older entries')} omitted by the client render limit.
              </p>}
              {[['conceptCuration', 'Concept'], ['claimCuration', 'Claim']].map(([sourceKey, kind]) =>
                <SourceWatermark key={sourceKey} sourceKey={sourceKey}
                  label={`${kind} steward log`} source={sourceStates[sourceKey]} retry={retry}
                  busy={busy} pending={resource.pending.includes(sourceKey)}>
                  history, not current governance.
                </SourceWatermark>)}
            </section>
          </div>}
        </div>}
    </main>
  </div>
}
