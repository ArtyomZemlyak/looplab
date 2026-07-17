import React, { useEffect, useRef, useState } from 'react'
import { getScopeReport, genScopeReport, fmt } from './util.js'
import {
  scopeObservationRows, scopeReportAuthority, scopeReportGenerationError, scopeReportKey,
} from './scopeReportModel.js'
import { useDialogFocus } from './useDialogFocus.js'

const list = value => Array.isArray(value) ? value : []
const text = value => typeof value === 'string' ? value : ''
const count = value => Number.isInteger(value) && value >= 0 ? value : null
const status = value => text(value).slice(0, 64).replaceAll('_', ' ')
const valid = value => typeof value?.exists === 'boolean'
  && (!value.exists || (value.content && typeof value.content === 'object' && !Array.isArray(value.content)))
const generationFlights = new Map()
const generationAmbiguous = error => error?.ambiguous === true
  || error?.submissionMayHaveSucceeded === true
const generatedAt = value => Number.isInteger(value?.generated_at) && value.generated_at >= 0
  ? value.generated_at : null
const reportAdvanced = (value, baseline) => value?.exists === true && generatedAt(value) !== null
  && (baseline === null || generatedAt(value) !== baseline)

// REVIEW(2026-07-16): an `uncertain` flight has NO exit besides a later GET observing generated_at
// ADVANCE (reportAdvanced) — but the ambiguous outcome this quarantine models is precisely the case
// where the job may have FAILED and never persists a new report: generated_at never advances, the
// flight stays in this module-level Map for the rest of the SPA session, generate() early-returns on
// existing?.uncertain, and both Generate/Regenerate render disabled — the scope's paid feature is
// locked with no recovery short of a full page reload. Made MORE likely by the backend's
// consume_on_poll receipt (see reports.py REVIEW note): a concurrent observer's poll retires the
// shared receipt, this client sees status:"unknown", marks the flight uncertain, and locks. The
// quarantine needs a resolution path for the not-advanced case too: an explicit "check status /
// unlock retry" action, a bounded TTL, or a server status probe that distinguishes failed-and-gone
// from still-running.
function beginGeneration(key, baseline, start) {
  const existing = generationFlights.get(key)
  if (existing) return existing
  const flight = { baseline, uncertain: false, error: null, promise: null }
  flight.promise = Promise.resolve().then(start)
  generationFlights.set(key, flight)
  flight.promise.then(
    () => { if (generationFlights.get(key) === flight) generationFlights.delete(key) },
    error => {
      if (generationFlights.get(key) !== flight) return
      if (generationAmbiguous(error)) {
        flight.uncertain = true
        flight.error = error
      } else generationFlights.delete(key)
    },
  )
  return flight
}

function Section({ title, items }) {
  items = list(items).filter(x => typeof x === 'string')
  if (!items.length) return null
  return <div className="sr-sec">
    <div className="sr-h">{title}</div>
    <ul className="sr-list">{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
  </div>
}

function MetricRun({ item, note, onOpen }) {
  const id = text(item?.run_id)
  return <button type="button" className="sr-best" disabled={!id} onClick={() => onOpen?.(id)}>
    <b className="sr-m">{Number.isFinite(item?.metric) ? fmt(item.metric) : '—'}</b><span className="sr-rid">{id}</span>
    {note && <span className="muted"> · {note}</span>}
  </button>
}

// Cross-run aggregate report for a scope (project folder | task | super-task). On open it fetches the
// stored report (if any); you can Generate when there's none, or Regenerate when it's gone stale. The
// report is authored from a bounded/redacted projection. Numeric ranking exists only inside an exact
// server-validated comparison contract; legacy best_runs are counted but their unverified rows stay hidden.
export default function ScopeReport({ scope, onOpen, onClose }) {
  const dialogRef = useRef(null)
  const requestEpoch = useRef(0)
  const readAbort = useRef(null)
  const key = scopeReportKey(scope)
  const keyRef = useRef(key)
  keyRef.current = key
  const [readRevision, setReadRevision] = useState(0)
  const [view, setView] = useState({
    key, data: null, busy: false, uncertain: false, err: null,
  })
  // Never render the previous scope's response during the render before its replacement effect runs.
  const shown = view.key === key ? view
    : { data: null, busy: false, uncertain: false, err: null }
  const { data, busy, uncertain, err } = shown

  const observeGeneration = (flight, epoch) => {
    flight.promise.then(value => {
      if (requestEpoch.current !== epoch || keyRef.current !== key) return
      setView({ key, data: value, busy: false, uncertain: false, err: null })
    }).catch(error => {
      if (requestEpoch.current !== epoch || keyRef.current !== key) return
      const ambiguous = generationAmbiguous(error)
      const inputsChanged = error?.code === 'scope_report_inputs_changed'
      setView(previous => {
        const previousData = previous.key === key ? previous.data : null
        const guardedData = !previousData ? previousData
          : inputsChanged ? { ...previousData, stale: true }
            : ambiguous ? { ...previousData, stale: null } : previousData
        return {
          key, data: guardedData, busy: false, uncertain: ambiguous,
          err: scopeReportGenerationError(error),
        }
      })
      if (inputsChanged) setReadRevision(value => value + 1)
    })
  }

  useEffect(() => {
    const epoch = ++requestEpoch.current
    const flight = generationFlights.get(key)
    if (flight && !flight.uncertain) {
      readAbort.current?.abort()
      setView(previous => ({
        key, data: previous.key === key ? previous.data : null,
        busy: true, uncertain: false, err: null,
      }))
      observeGeneration(flight, epoch)
      return () => { requestEpoch.current += 1 }
    }
    const controller = new AbortController()
    readAbort.current = controller
    setView(previous => ({
      key, data: previous.key === key ? previous.data : null,
      busy: true, uncertain: flight?.uncertain === true,
      err: previous.key === key ? previous.err : null,
    }))
    getScopeReport(scope.type, scope.id, { signal: controller.signal })
      .then(value => {
        // # CODEX AGENT: A late GET must not overwrite a regeneration or a newly selected scope.
        if (requestEpoch.current !== epoch || keyRef.current !== key) return
        if (!valid(value)) {
          setView({ key, data: null, busy: false, uncertain: false,
            err: 'Invalid report response.' })
          return
        }
        const unresolved = generationFlights.get(key)
        if (unresolved?.uncertain && reportAdvanced(value, unresolved.baseline)) {
          generationFlights.delete(key)
        }
        const uncertainFlight = generationFlights.get(key)?.uncertain === true
        setView({ key,
          // # CODEX AGENT: an unresolved paid outcome cannot lend the previous receipt fresh
          // authority. Keep it visible only through the unknown-freshness quarantine.
          data: uncertainFlight && value.exists ? { ...value, stale: null } : value,
          busy: false, uncertain: uncertainFlight,
          err: uncertainFlight
            ? scopeReportGenerationError(generationFlights.get(key)?.error) : null })
      })
      .catch(error => {
        if (requestEpoch.current !== epoch || keyRef.current !== key || error?.name === 'AbortError') return
        setView(previous => ({ key, data: previous.key === key ? previous.data : null,
          busy: false, uncertain: flight?.uncertain === true, err: 'Report unavailable.' }))
      })
    return () => {
      controller.abort()
      if (readAbort.current === controller) readAbort.current = null
      requestEpoch.current += 1
    }
  }, [key, scope.type, scope.id, readRevision])

  const generate = () => {
    const existing = generationFlights.get(key)
    if (existing?.uncertain) return
    readAbort.current?.abort()
    const epoch = ++requestEpoch.current
    setView(previous => ({
      key, data: previous.key === key ? previous.data : null,
      busy: true, uncertain: false, err: null,
    }))
    // # CODEX AGENT: module-owned single-flight survives scope navigation and modal remounts. The
    // backend also coalesces the exact unobserved job, so no UI lifecycle can duplicate paid work.
    const flight = beginGeneration(key, generatedAt(view.key === key ? view.data : null), async () => {
      const value = { ...await genScopeReport(scope.type, scope.id), exists: true }
      if (!valid(value)) {
        const error = new Error('invalid report response')
        error.code = 'scope_report_invalid_response'
        throw error
      }
      return value
    })
    observeGeneration(flight, epoch)
  }

  const c = data?.content
  const authority = scopeReportAuthority(data)
  const groups = authority.inspectable ? list(c?.comparison_groups) : []
  const observations = authority.inspectable ? list(c?.metric_observations) : []
  const evidenceRuns = count(c?.coverage?.prompt_runs) ?? count(c?.coverage?.model_runs)
  const sourceRuns = count(c?.coverage?.source_runs)
  const label = text(data?.label) || text(data?.scope?.label) || text(scope.label) || `${scope.type} ${scope.id}`
  const runCount = count(data?.run_count) ?? 0
  const headline = text(c?.headline)
  const verdict = text(c?.verdict)
  useDialogFocus(dialogRef, onClose)

  return <div className="overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose?.() }}>
    <div ref={dialogRef} className="panel sr-panel" role="dialog" aria-modal="true"
      aria-label={`Report for ${label}`} tabIndex={-1}>
      <div className="panel-h">
        <span className="ttl">Cross-run report · {label}</span>
        <span className="right" />
        {data?.exists && <button className="btn sm" disabled={busy || uncertain} onClick={generate}>
          {uncertain ? '… outcome unknown' : busy ? '… generating' : '↻ Regenerate'}</button>}
        <button className="btn sm ghost" onClick={onClose} aria-label="Close report">✕</button>
      </div>
      <div className="panel-b">
        {err && <div className="notice resource-error" role="alert">
          <span>{err}</span>{' '}
          <button type="button" className="btn sm ghost" disabled={busy}
            onClick={() => setReadRevision(value => value + 1)}>Retry read</button>
        </div>}
        {data == null && !err && <div className="notice" role="status">Loading…</div>}

        {data && !data.exists && <div className="sr-empty">
          <div className="muted">
            No report — <b>{runCount}</b> runs. Evidence is bounded.
          </div>
          <button className="btn primary" disabled={busy || uncertain || !runCount} onClick={generate}>
            {uncertain ? '… outcome unknown' : busy ? '… generating' : '✦ Generate report'}</button>
        </div>}

        {data?.exists && c && <div className="sr-body">
          <div className="sr-meta">
            {evidenceRuns != null && sourceRuns != null
              ? <span>· evidence {evidenceRuns}/{sourceRuns} runs{c.coverage.incomplete === true ? ' (incomplete)' : ''}</span>
              : <span>· snapshot: {Array.isArray(data.run_ids) ? data.run_ids.length : '?'} runs</span>}
            {data.stale === true && <span className="sr-stale"> · stale snapshot — regenerate</span>}
            {authority.freshness === 'unknown' && <span className="sr-stale"> · snapshot freshness unknown</span>}
          </div>
          {!authority.authoritative && <div className="notice sr-quarantine" role="status">
            Stored report content is quarantined because its authority is unavailable. Regenerate to inspect it.
          </div>}
          {authority.authoritative && authority.freshness === 'unknown' && <div className="notice sr-quarantine" role="status">
            Stored report freshness cannot be verified. Narrative, observations, and outcome claims are withheld.
          </div>}
          {authority.freshness === 'stale' && <div className="notice sr-quarantine" role="status">
            This is a stale historical snapshot. Advisory narrative and observations remain inspectable; snapshot outcome claims are withheld.
          </div>}
          {authority.fresh && !authority.verdict && <div className="notice sr-quarantine" role="status">
            The server did not provide a current authoritative verdict. Report observations remain unranked.
          </div>}
          {authority.verdict && verdict && <div className="sr-verdict">{verdict}</div>}
          {groups.length > 0 && <div className="sr-sec">
            <div className="sr-h">Comparable cohorts</div>
            {groups.map((group, i) => {
              const declaredRows = scopeObservationRows(group)
              const trusted = authority.inspectable && declaredRows !== null
              const rows = declaredRows || []
              const reason = !trusted ? 'unverified' : status(group?.indeterminate) || 'unavailable'
              return <div className="sr-group" key={text(group?.contract_id) || i}>
                <div className="muted">{text(group?.metric_uid) || 'metric'} · {text(group?.direction) || '?'} · {trusted ? 'declared observations' : 'unverified'}</div>
                <div className="muted" role="status">{trusted
                  ? `No winner — ${reason}.` : 'Cohort withheld — unverified observation contract.'}</div>
                <div className="sr-bests">{rows.map((item, j) => {
                  const id = text(item?.run_id)
                  const note = list(group?.incomplete_runs).includes(id) ? 'run incomplete' : ''
                  return <MetricRun key={j} item={item} onOpen={onOpen} note={note} />
                })}</div>
              </div>
            })}
          </div>}
          {observations.length > 0 && <div className="sr-sec">
            <div className="sr-h">Unranked metrics</div>
            <div className="sr-bests">{observations.map((item, i) =>
              <MetricRun key={`${item?.run_id}-${i}`} item={item} onOpen={onOpen}
                note={status(item?.comparison_status) || 'unranked'} />)}</div>
          </div>}
          {authority.inspectable && !authority.narrative && <div className="notice sr-quarantine" role="status">
            The narrative is withheld because it has no model-advisory authority marker.
          </div>}
          {authority.narrative && <div className="sr-narrative">
            <div className="sr-advisory">Model-advisory narrative · not a selection decision</div>
            {headline && <div className="sr-headline">{headline}</div>}
            <Section title="What worked" items={c.what_worked} />
            <Section title="What didn’t" items={c.what_didnt} />
            <Section title="Learnings" items={c.learnings} />
            <Section title="Next directions" items={c.next_directions} />
            <Section title="Caveats" items={c.caveats} />
          </div>}
        </div>}
      </div>
    </div>
  </div>
}
