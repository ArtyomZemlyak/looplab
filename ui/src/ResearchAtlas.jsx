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
const curationOutcomeLabel = entry => {
  if (entry.applied > 0) return `${entry.applied} applied`
  if (entry.outcome === 'empty') return 'no changes proposed'
  if (entry.outcome === 'unavailable') return 'steward unavailable'
  if (entry.outcome === 'error') return 'steward error logged'
  if (entry.outcome === 'already-governed') return 'already governed'
  return 'proposal only'
}

export function AtlasRunReference({ run }) {
  const context = [run.task && `task: ${run.task}`, run.scope && `scope: ${run.scope}`]
    .filter(Boolean).join(' · ')
  const disclosure = run.metricSuppressed
    ? 'Metric hidden · task, scope, or objective orientation missing.'
    : run.metric != null && run.optimizationOrientation
    ? `Not cross-run comparable · metric name/unit unknown · ${run.optimizationOrientation} objective · ${run.metric.toLocaleString(undefined, { maximumSignificantDigits: 6 })}`
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
  const displayState = pending ? 'loading' : state
  const loadedAt = source.loadedAt
  return <p className={`atlas-source-note atlas-source-${displayState}`}>
    <strong>{label}</strong> · <span>{pending ? 'loading'
      : state === 'retained-stale' ? 'stale' : state === 'failed' ? 'unavailable' : 'loaded'}</span>
    {loadedAt && <> · loaded <time dateTime={loadedAt}>{loadedAt}</time></>}
    {' · '}revision {source.revision || 'not reported'} · {children}
    {state !== 'current' && <> · <button type="button" className="btn sm" disabled={busy}
      onClick={() => retry(sourceKey)} aria-label={`Retry ${label}`}>
      {busy ? 'Refreshing…' : 'Retry'}</button></>}
  </p>
}

export function ConceptSourceNotice({ source }) {
  if (source.status === 'complete') return null
  const partial = source.status === 'partial'
  const quarantined = partial && source.store?.quarantined > 0 ? source.store : null
  return <div className="notice resource-warning atlas-degraded" role="status">
    <b>Concept source {partial ? 'partial.' : 'unknown.'}</b>
    <span>{partial
      ? `Bounds (partial/legacy/concepts/outcomes): ${source.counts.join('/')}.`
      : 'Receipt missing/invalid.'}
      {quarantined && ` Durable rows quarantined: ${quarantined.quarantined} (malformed/invalid/duplicate: ${quarantined.malformed}/${quarantined.invalid}/${quarantined.duplicates}).`}
      {' Absence unknown.'}</span>
  </div>
}

export function ResearchSourceNotice({ source }) {
  if (source.status === 'complete') return null
  const counts = source.counts
  const partial = source.status === 'partial'
  return <div className="notice resource-warning atlas-degraded" role="status">
    <b>Research-claim source {partial ? 'partial.' : 'unknown.'}</b>
    <span>{counts
      ? `${counts.producer_claims_omitted} claim(s) known omitted across ${counts.producer_partial_runs} capped run(s); ${counts.producer_unknown_runs} run receipt(s) unknown.`
      : 'Receipt missing/invalid.'} Retained evidence is a lower bound; one-sided state is withheld.</span>
  </div>
}

export function AtlasEmptyState({ sourceStates, conceptSource,
  researchSource = { status: 'unknown', counts: null },
  pending = [], retry, busy, onBack }) {
  const pendingSources = new Set(pending)
  const allLoaded = pending.length === 0
    && Object.values(sourceStates).every(source => source.state === 'current')
  const atlasState = conceptSource.status === 'complete' ? 'Empty'
    : conceptSource.status === 'partial' ? 'Partial' : 'Unknown'
  const claimState = researchSource.status === 'complete' ? 'Empty'
    : researchSource.status === 'partial' ? 'Partial' : 'Unknown'
  const completeEmpty = allLoaded && conceptSource.status === 'complete'
    && researchSource.status === 'complete'
  return <section className="atlas-empty" aria-labelledby="atlas-empty-title" role="status">
    <div className="atlas-empty-copy">
      <p className="atlas-eyebrow">Source readiness</p>
      <h2 id="atlas-empty-title">{completeEmpty
        ? 'No cross-run evidence'
        : allLoaded ? 'No retained cross-run evidence' : 'No current Atlas records'}</h2>
      <p>{completeEmpty
        ? 'No shared-memory evidence returned; runs may still exist.'
        : allLoaded
        ? 'Partial/unknown receipt: empty rows do not prove absence.'
        : 'Retry each unavailable or stale source below.'}</p>
      <div className="atlas-empty-actions">
        <button type="button" className="btn primary" onClick={onBack}>Back to runs</button>
        <a className="btn" href="#/settings">Memory settings</a>
      </div>
    </div>
    <ul className="atlas-source-readiness" aria-label="Atlas source readiness">
      {SOURCE_READINESS.map(([key, label]) => {
        const state = sourceStates[key]?.state || 'failed'
        const loading = pendingSources.has(key)
        const status = loading ? 'Loading' : state === 'current'
          ? key === 'atlas' ? atlasState : key === 'claims' ? claimState : 'Empty'
          : state === 'retained-stale' ? 'Stale' : 'Unavailable'
        const retryable = !loading && state !== 'current'
        return <li key={key}
        className={`atlas-empty-source atlas-empty-source-${loading ? 'loading' : state}`}>
        <span className="atlas-readiness-dot" aria-hidden="true" />
        <div className="atlas-empty-source-head">
            <strong>{label}</strong>
            <span className="atlas-readiness-state">{status}</span>
        </div>
        {retryable && <button type="button" className="btn sm" disabled={busy}
          onClick={() => retry(key)} aria-label={`Retry ${label}`}>
          {busy ? 'Refreshing…' : 'Retry'}
        </button>}
      </li>})}
    </ul>
  </section>
}

export function ClaimCard({ claim, compact = false }) {
  const evidence = claim.support.length + claim.oppose.length + claim.unverified.length
    + claim.contradicts.length
  const hiddenEvidence = Math.max(0, claim.nSupport - claim.support.length)
    + Math.max(0, claim.nOppose - claim.oppose.length)
    + Math.max(0, claim.nUnverified - claim.unverified.length)
    + Math.max(0, claim.nContradicts - claim.contradicts.length)
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
      <span>support refs <b>{claim.nSupport}</b></span>
      <span>oppose refs <b>{claim.nOppose}</b></span>
      {claim.nUnverified > 0 && <span>unverified refs <b>{claim.nUnverified}</b></span>}
      {claim.nContradicts > 0 && <span>contradicting claims <b>{claim.nContradicts}</b></span>}
      {claim.scopes.length > 0 && <span title={claim.scopes.join(', ')}>
        {countLabel(claim.scopes.length, 'claim grouping')}
      </span>}
    </div>
    {context.length > 0 && <div className="atlas-claim-context" aria-label="Claim groups and runs">
      {context.slice(0, 3).map((value, index) => <span className="pill" key={index}>{value}</span>)}
      {context.length > 3 && <span className="muted">+{context.length - 3} more</span>}
    </div>}
    {!compact && (evidence > 0 || hiddenEvidence > 0) && <details>
      <summary>Show evidence context</summary>
      <div className="atlas-evidence">
        {evidence === 0 && <span className="atlas-evidence-boundary">No evidence context returned.</span>}
        {claim.support.map((ref, index) => <code key={`s-${index}`}>support · {ref}</code>)}
        {claim.oppose.map((ref, index) => <code key={`o-${index}`}>oppose · {ref}</code>)}
        {claim.unverified.map((ref, index) => <code key={`u-${index}`}>unverified · {ref}</code>)}
        {claim.contradicts.map((statement, index) => <code key={`c-${index}`}>
          contradiction · {statement}
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
    <h1>Loading Research Atlas preview</h1>
  </div>
  const memoryMissing = errors.length > 0 && errors.every(error => error.status === 400)
  return <div className="run-resource-state" role={memoryMissing ? 'status' : 'alert'}>
    <div className="resource-state-icon" aria-hidden="true">×</div>
    <h1>{memoryMissing ? 'Research Atlas is not configured' : 'Research Atlas preview unavailable'}</h1>
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
              <p>Bounded read-only observations; experimental claim identity and clipping prevent coverage claims.</p>
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

          <ConceptSourceNotice source={view.conceptSource} />
          <ResearchSourceNotice source={view.researchSource} />

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
            researchSource={view.researchSource}
            pending={resource.pending}
            retry={retry} busy={busy} onBack={onBack} />}

          {!view.empty && <div className="atlas-grid">
            <section className="atlas-panel atlas-coverage" aria-labelledby="atlas-coverage-title">
              <div className="atlas-panel-head">
                <div><p className="atlas-eyebrow">Concept observations</p><h2 id="atlas-coverage-title">Concepts seen across runs</h2></div>
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
              <SourceWatermark sourceKey="atlas" label="Atlas concept/evidence projection"
                source={sourceStates.atlas} retry={retry} busy={busy}
                pending={resource.pending.includes('atlas')}>
                bounded observations, not coverage.
              </SourceWatermark>
            </section>

            <section className="atlas-panel atlas-contradictions" aria-labelledby="atlas-contradictions-title">
              <div className="atlas-panel-head">
                <div><p className="atlas-eyebrow">Evidence balance</p><h2 id="atlas-contradictions-title">Mixed-evidence claim records</h2></div>
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
              <SourceWatermark sourceKey="atlas" label="Atlas claim/evidence projection"
                source={sourceStates.atlas} retry={retry} busy={busy}
                pending={resource.pending.includes('atlas')}>
                not a verdict or applicability decision.
              </SourceWatermark>
            </section>

            <section className="atlas-panel atlas-all-claims" aria-labelledby="atlas-claims-title">
              <div className="atlas-panel-head">
                <div><p className="atlas-eyebrow">Claim projection</p><h2 id="atlas-claims-title">Claim records</h2></div>
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
                <div><p className="atlas-eyebrow">Steward invocation log</p><h2 id="atlas-curation-title">Recent proposals + outcomes</h2></div>
                <span className="muted">{curationCurrent
                  ? `showing ${view.curation.length} of ${view.totals.curation}`
                  : 'incomplete merge'}</span>
              </div>
              {view.curation.length > 0
                ? <ol className="atlas-curation-list">{view.curation.map((entry, index) => {
                    const proposals = entry.merges + entry.splits + entry.purges + entry.decisions
                    const timeLabel = entry.at || (entry.revision
                      ? `revision ${entry.revision}` : 'time not recorded')
                    return <li key={`${entry.runId}-${entry.at}-${index}`}>
                      <div className="atlas-curation-meta"><strong className="atlas-curation-run">{entry.runId
                        ? <a href={`#/run/${encodeURIComponent(entry.runId)}`}
                            title={entry.runId}>{entry.runId}</a>
                        : 'Portfolio steward'}</strong><span className="atlas-curation-time"
                          title={timeLabel}>{timeLabel}</span></div>
                      <p><span className="atlas-curation-kind">{entry.kind} steward</span> · {countLabel(proposals, 'proposal')}
                        {entry.kind === 'claim'
                          ? ` · ${countLabel(entry.decisions, 'claim decision')}`
                          : ` · ${countLabel(entry.merges, 'merge')} · ${countLabel(entry.splits, 'split')} · ${countLabel(entry.purges, 'purge')}`}</p>
                      <div className="atlas-curation-status">
                        <span className={`pill atlas-outcome-${entry.outcome} ${entry.applied > 0 ? 'atlas-applied' : ''}`}>
                          {curationOutcomeLabel(entry)}{entry.skipped > 0 ? ` · ${entry.skipped} skipped` : ''}
                        </span>
                        {entry.autoRequested && <span className="pill atlas-auto-requested">legacy auto requested · not applied</span>}
                      </div>
                    </li>
                  })}</ol>
                : <p className="atlas-section-empty">{curationCurrent
                  ? 'No steward records returned.'
                  : 'No records shown; merge incomplete.'}</p>}
              {view.hiddenCuration > 0 && <p className="atlas-boundary-note">
                {countLabel(view.hiddenCuration, 'older entry', 'older entries')} omitted by the client render limit.
              </p>}
              {[['conceptCuration', 'Concept'], ['claimCuration', 'Claim']].map(([sourceKey, kind]) =>
                <SourceWatermark key={sourceKey} sourceKey={sourceKey}
                  label={`${kind} steward invocation log`} source={sourceStates[sourceKey]} retry={retry}
                  busy={busy} pending={resource.pending.includes(sourceKey)}>
                  history, not current governance.
                </SourceWatermark>)}
            </section>
          </div>}
        </div>}
    </main>
  </div>
}
