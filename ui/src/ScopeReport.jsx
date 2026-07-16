import React, { useEffect, useRef, useState } from 'react'
import { getScopeReport, genScopeReport, fmt } from './util.js'
import { scopeObservationRows, scopeReportAuthority, scopeReportKey } from './scopeReportModel.js'
import { useDialogFocus } from './useDialogFocus.js'

const list = value => Array.isArray(value) ? value : []
const text = value => typeof value === 'string' ? value : ''
const count = value => Number.isInteger(value) && value >= 0 ? value : null
const status = value => text(value).slice(0, 64).replaceAll('_', ' ')
const valid = value => typeof value?.exists === 'boolean'
  && (!value.exists || (value.content && typeof value.content === 'object' && !Array.isArray(value.content)))

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
  const generatingScopes = useRef(new Set())
  const key = scopeReportKey(scope)
  const keyRef = useRef(key)
  keyRef.current = key
  const [view, setView] = useState({ key, data: null, busy: false, err: null })
  // Never render the previous scope's response during the render before its replacement effect runs.
  const shown = view.key === key ? view : { data: null, busy: false, err: null }
  const { data, busy, err } = shown

  useEffect(() => {
    const epoch = ++requestEpoch.current
    const controller = new AbortController()
    readAbort.current = controller
    setView({ key, data: null, busy: false, err: null })
    getScopeReport(scope.type, scope.id, { signal: controller.signal })
      .then(value => {
        // # CODEX AGENT: A late GET must not overwrite a regeneration or a newly selected scope.
        if (requestEpoch.current !== epoch || keyRef.current !== key) return
        setView({ key, data: valid(value) ? value : null, busy: false,
          err: valid(value) ? null : 'Invalid report response.' })
      })
      .catch(error => {
        if (requestEpoch.current !== epoch || keyRef.current !== key || error?.name === 'AbortError') return
        setView({ key, data: null, busy: false, err: 'Report unavailable.' })
      })
    return () => {
      controller.abort()
      if (readAbort.current === controller) readAbort.current = null
      requestEpoch.current += 1
    }
  }, [key, scope.type, scope.id])

  const generate = async () => {
    // # CODEX AGENT: generating can incur a paid provider call. React's disabled state is not a
    // synchronous lock, so fence double-clicks by exact scope identity before starting transport.
    if (generatingScopes.current.has(key)) return
    generatingScopes.current.add(key)
    readAbort.current?.abort()
    const epoch = ++requestEpoch.current
    setView(previous => ({ key,
      data: previous.key === key ? previous.data : null, busy: true, err: null }))
    try {
      const value = { ...await genScopeReport(scope.type, scope.id), exists: true }
      if (!valid(value)) throw 0
      if (requestEpoch.current === epoch && keyRef.current === key) {
        setView({ key, data: value, busy: false, err: null })
      }
    }
    catch (error) {
      if (requestEpoch.current === epoch && keyRef.current === key) {
        setView(previous => ({ ...previous, busy: false,
          err: error?.status === 400 ? 'No runs in this scope yet.' : 'Generation failed.' }))
      }
    }
    finally { generatingScopes.current.delete(key) }
  }

  const c = data?.content
  const authority = scopeReportAuthority(data)
  const groups = authority.current ? list(c?.comparison_groups) : []
  const observations = authority.current ? list(c?.metric_observations) : []
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
        {data?.exists && <button className="btn sm" disabled={busy} onClick={generate}>{busy ? '… generating' : '↻ Regenerate'}</button>}
        <button className="btn sm ghost" onClick={onClose} aria-label="Close report">✕</button>
      </div>
      <div className="panel-b">
        {err && <div className="notice resource-error" role="alert">{err}</div>}
        {data == null && !err && <div className="notice" role="status">Loading…</div>}

        {data && !data.exists && <div className="sr-empty">
          <div className="muted">
            No report — <b>{runCount}</b> runs. Evidence is bounded.
          </div>
          <button className="btn primary" disabled={busy || !runCount} onClick={generate}>
            {busy ? '… generating' : '✦ Generate report'}</button>
        </div>}

        {data?.exists && c && <div className="sr-body">
          <div className="sr-meta">
            {evidenceRuns != null && sourceRuns != null
              ? <span>· evidence {evidenceRuns}/{sourceRuns} runs{c.coverage.incomplete === true ? ' (incomplete)' : ''}</span>
              : <span>· snapshot: {Array.isArray(data.run_ids) ? data.run_ids.length : '?'} runs</span>}
            {data.stale && <span className="sr-stale"> · stale snapshot — regenerate</span>}
          </div>
          {!authority.current && <div className="notice sr-quarantine" role="status">
            Stored report content is quarantined because its authority or freshness is not current. Regenerate to inspect it.
          </div>}
          {authority.current && !authority.verdict && <div className="notice sr-quarantine" role="status">
            The server did not provide a current authoritative verdict. Report observations remain unranked.
          </div>}
          {authority.verdict && verdict && <div className="sr-verdict">{verdict}</div>}
          {groups.length > 0 && <div className="sr-sec">
            <div className="sr-h">Comparable cohorts</div>
            {groups.map((group, i) => {
              const declaredRows = scopeObservationRows(group)
              const trusted = authority.current && declaredRows !== null
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
          {authority.current && !authority.narrative && <div className="notice sr-quarantine" role="status">
            The narrative is withheld because it has no current model-advisory authority marker.
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
