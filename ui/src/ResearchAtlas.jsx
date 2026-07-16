import React, { useEffect, useState } from 'react'
import {
  getCrossRunAtlas, getCrossRunClaims, getCrossRunCurationLog, getCrossRunClaimCurationLog,
} from './api.js'
import {
  buildResearchAtlasView, mergeCurationLogs, mergeResearchAtlasPayload,
  isValidAtlasSourceEnvelope, reconcileAtlasSourceStatuses,
} from './researchAtlasModel.js'
import './research-atlas.css'

const SOURCES = [
  { key: 'atlas', read: signal => getCrossRunAtlas(24, { signal }) },
  { key: 'claims', read: signal => getCrossRunClaims(40, 0, { signal }) },
  { key: 'conceptCuration', read: signal => getCrossRunCurationLog(20, { signal }) },
  { key: 'claimCuration', read: signal => getCrossRunClaimCurationLog(20, { signal }) },
]

const metricText = value => value == null ? '—' : Number(value).toLocaleString(undefined, { maximumSignificantDigits: 6 })
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

function SourceWatermark({ sourceKey, label, source, retry, children }) {
  const state = source.state
  const loadedAt = source.loadedAt
  return <p className={`atlas-source-note atlas-source-${state}`}>
    <strong>{label}</strong> · <span>{state === 'retained-stale' ? 'stale'
      : state === 'failed' ? 'unavailable' : 'current'}</span>
    {loadedAt && <> · loaded <time dateTime={loadedAt}>{loadedAt}</time></>}
    {' · '}revision {source.revision || 'not reported'} · {children}
    {state !== 'current' && <> · <button type="button" className="btn sm" onClick={() => retry(sourceKey)}
      aria-label={`Retry ${label}`}>Retry</button></>}
  </p>
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
      <span>supporting attempt refs <b>{claim.nSupport}</b></span>
      <span>opposing attempt refs <b>{claim.nOppose}</b></span>
      {claim.nUnverified > 0 && <span>unverified attempt refs <b>{claim.nUnverified}</b></span>}
      {claim.nContradicts > 0 && <span>contradicting claim records <b>{claim.nContradicts}</b></span>}
      {claim.scopes.length > 0 && <span title={claim.scopes.join(', ')}>
        {countLabel(claim.scopes.length, 'claim grouping')}
      </span>}
    </div>
    {context.length > 0 && <div className="atlas-claim-context" aria-label="Bounded claim grouping and run context">
      {context.slice(0, 3).map((value, index) => <span className="pill" key={index}>{value}</span>)}
      {context.length > 3 && <span className="muted">+{context.length - 3} more</span>}
    </div>}
    {!compact && (evidence > 0 || hiddenEvidence > 0) && <details>
      <summary>Show evidence context</summary>
      <div className="atlas-evidence">
        {evidence === 0 && <span className="atlas-evidence-boundary">No evidence context returned.</span>}
        {claim.support.map((ref, index) => <code key={`s-${index}`}>support attempt · {ref}</code>)}
        {claim.oppose.map((ref, index) => <code key={`o-${index}`}>oppose attempt · {ref}</code>)}
        {claim.unverified.map((ref, index) => <code key={`u-${index}`}>unverified attempt · {ref}</code>)}
        {claim.contradicts.map((statement, index) => <code key={`c-${index}`}>
          contradicting claim · {statement}
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
      <button type="button" className={`btn ${memoryMissing ? '' : 'primary'}`} onClick={onRetry}>Retry</button>
    </div>
  </div>
}

export default function ResearchAtlas({ onBack }) {
  const [request, setRequest] = useState({ key: '' })
  const [resource, setResource] = useState({
    status: 'loading', view: null, payload: null, errors: [],
    sourceStates: reconcileAtlasSourceStatuses({}, {}, ''),
  })

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    const requestedSources = request.key ? SOURCES.filter(source => source.key === request.key) : SOURCES
    const attemptedKeys = requestedSources.map(source => source.key)
    setResource(current => ({ ...current, status: current.view ? 'refreshing' : 'loading' }))
    Promise.allSettled(requestedSources.map(source => source.read(controller.signal))).then(results => {
      if (!active) return
      const successful = {}
      const errors = []
      results.forEach((result, index) => {
        const source = requestedSources[index]
        if (result.status === 'fulfilled' && isValidAtlasSourceEnvelope(source.key, result.value)) {
          successful[source.key] = result.value
        } else {
          // HTTP success is not schema success. Quarantine malformed envelopes so they cannot replace
          // last-good evidence or falsely advance a source watermark to "current".
          const malformed = result.status === 'fulfilled'
          errors.push({
            key: source.key,
            status: malformed ? null : result.reason?.status,
          })
        }
      })
      const loadedAt = new Date().toISOString()
      // Evidence and both audit ledgers are independent. Keep an already rendered view
      // when an entire refresh fails; a partial first load stays explicit instead of erasing good data.
      setResource(current => {
        const sourceStates = reconcileAtlasSourceStatuses(
          current.sourceStates, successful, loadedAt, attemptedKeys)
        const nextErrors = [...current.errors.filter(error => !attemptedKeys.includes(error.key)), ...errors]
        if (errors.length === requestedSources.length) {
          return current.view
            ? { ...current, status: 'ready', errors: nextErrors, sourceStates }
            : { ...current, status: 'error', errors: nextErrors, sourceStates }
        }
        const payload = mergeResearchAtlasPayload(current.payload, successful)
        const curation = mergeCurationLogs(payload.conceptCuration, payload.claimCuration)
        return {
          status: 'ready', payload,
          view: buildResearchAtlasView(payload.atlas, payload.claims, curation),
          errors: nextErrors,
          sourceStates,
        }
      })
    })
    return () => { active = false; controller.abort() }
  }, [request])

  const retry = (key = '') => setRequest({ key })
  const refresh = () => retry()
  const view = resource.view
  const sourceStates = resource.sourceStates
  const atlasLoaded = sourceStates.atlas.state !== 'failed'
  const claimsLoaded = sourceStates.claims.state !== 'failed'
  const states = Object.values(sourceStates)
  const allCurrent = states.every(source => source.state === 'current')
  const curationCurrent = sourceStates.conceptCuration.state === 'current'
    && sourceStates.claimCuration.state === 'current'
  const hasRetainedStale = states.some(source => source.state === 'retained-stale')
  return <div className="app atlas-route">
    <div className="topbar">
      <span className="brand"><span className="dot">◉</span> LoopLab</span>
      <button type="button" className="btn sm ghost" aria-label="Back to runs" onClick={onBack}>← runs</button>
      <span className="ttl">Research Atlas preview</span>
      <span className="chip xs warn">Experimental · bounded · read-only</span>
      <span className="spacer" />
      <button type="button" className="btn sm" aria-label="Refresh Research Atlas preview"
              disabled={resource.status === 'loading' || resource.status === 'refreshing'} onClick={refresh}>
        {resource.status === 'refreshing' ? 'Refreshing…' : 'Refresh'}
      </button>
    </div>

    <main className="research-atlas-page" data-route-main tabIndex={-1}
      aria-busy={resource.status === 'loading' || resource.status === 'refreshing'}>
      {!resource.view
        ? <RouteState kind={resource.status} errors={resource.errors} onRetry={refresh} />
        : <div className="atlas-content">
          <header className="atlas-intro">
            <div>
              <p className="atlas-eyebrow">Experimental Part IV/V</p>
              <h1>Bounded portfolio evidence preview</h1>
              <p>Read-only bounded projection, not a complete Atlas. Claim identity is experimental;
                clipped rows and one-run observations do not establish coverage.</p>
            </div>
          </header>

          {resource.errors.length > 0 && <div className="notice resource-warning atlas-degraded" role="status">
            <b>{hasRetainedStale
              ? 'Refresh incomplete; showing stale last-good data.'
              : 'Some sources unavailable.'}</b>
            <span>{countLabel(resource.errors.length, 'source')} unavailable.</span>
            <button type="button" className="btn sm" onClick={refresh}>Retry missing data</button>
          </div>}

          {view.invalidRows.total > 0 && <div className="notice resource-warning atlas-degraded" role="alert">
            <b>Some portfolio records were ignored.</b>
            <span>{countLabel(view.invalidRows.total, 'record')}; server totals may still include them.</span>
          </div>}

          <section className="atlas-summary" aria-label="Portfolio summary">
            {[
              ['Runs', view.totals.runs, atlasLoaded],
              ['Concepts', view.totals.concepts, atlasLoaded],
              ['Claims', view.totals.claims, claimsLoaded],
              ['Mixed evidence', view.totals.contested, atlasLoaded],
            ].map(([label, value, loaded]) => <div className="atlas-stat" key={label}>
              <span>{label}</span><strong>{loaded ? value : 'not loaded'}</strong>
            </div>)}
          </section>

          {view.empty && allCurrent && <div className="notice resource-empty atlas-empty" role="status">
            <div><b>{view.invalidRows.total > 0 || resource.errors.length > 0
              ? 'No valid records are available.'
              : 'No cross-run memory yet.'}</b><br />
              {view.invalidRows.total > 0 || resource.errors.length > 0
                ? 'Retry missing sources or repair shared-memory records.'
                : 'Configure Memory dir and finalize Part IV runs.'}</div>
          </div>}

          {(!view.empty || !allCurrent) && <div className="atlas-grid">
            <section className="atlas-panel atlas-coverage" aria-labelledby="atlas-coverage-title">
              <div className="atlas-panel-head">
                <div><p className="atlas-eyebrow">Concept observations</p><h2 id="atlas-coverage-title">Concepts seen across runs</h2></div>
                <span className="muted">{atlasLoaded
                  ? `showing ${view.concepts.length} of ${view.totals.concepts}` : 'not loaded'}</span>
              </div>
              {atlasLoaded && <>
                  {view.concepts.length === 0
                    ? <p className="atlas-section-empty">No concepts returned.</p>
                    : <ul className="atlas-concepts" tabIndex={0} aria-label="Bounded explored concepts">
                      {view.concepts.map((concept, index) => <li key={`${concept.concept}-${index}`}>
                        <div><strong>{concept.concept}</strong><span>{countLabel(concept.nRuns, 'run')}</span></div>
                        {concept.runs.length > 0 && <div className="atlas-runrefs">
                          {concept.runs.map((run, runIndex) => run.runId
                            ? <a key={`${run.runId}-${runIndex}`} href={`#/run/${encodeURIComponent(run.runId)}`}
                                 aria-label={`Open run ${run.runId}`}>
                                {run.runId} · {run.direction} {metricText(run.metric)}
                              </a>
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
                source={sourceStates.atlas} retry={retry}>
                not a CoverageFrame, frozen snapshot, or completeness estimate.
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
                source={sourceStates.atlas} retry={retry}>
                not a proposition verdict or an applicability decision.
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
                source={sourceStates.claims} retry={retry}>
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
                             aria-label={`Open run ${entry.runId}`} title={entry.runId}>{entry.runId}</a>
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
                  label={`${kind} steward invocation log`} source={sourceStates[sourceKey]} retry={retry}>
                  logged proposals and outcomes; not a current governance snapshot.
                </SourceWatermark>)}
            </section>
          </div>}
        </div>}
    </main>
  </div>
}
