import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { get, fmt, fmtAgo, fmtCost, fmtElapsedSeconds, normalizeRunGeneration, runApiPath } from './util.js'
import { effectiveRunStatus } from './runIndex.js'
import { comparableRunRanking, COMPARE_COLUMNS, configDifferences } from './portfolioModel.js'
import { deadlineRequest } from './requestDeadline.js'
import { hashWithRunRouteState } from './runRouteState.js'

const path = runApiPath
const COMPARE_DETAIL_TIMEOUT_MS = 8000

const captureToken = run => normalizeRunGeneration(run?.generation)
  || normalizeRunGeneration(run?.deletion_generation)
const compareIdentity = runs => runs.map(run => {
  const fallback = `unverified:${Number.isSafeInteger(run?.seq) ? run.seq : '?'}:${run?.mtime ?? '?'}`
  return `${run.run_id}@${captureToken(run) || fallback}`
}).join('\n')
const freezeRuns = runs => runs.map(run => ({ ...run }))
const freezeNames = names => ({
  projects: { ...(names?.projects || {}) },
  supertasks: { ...(names?.supertasks || {}) },
})
const sameRunIdentity = (left, right) => {
  const token = captureToken(left)
  return left?.run_id === right?.run_id && !!token && token === captureToken(right)
}
const retainedCapture = (resource, nextRuns) => {
  if (!resource.loadedAt) return null
  const prior = new Map(resource.runs.map(run => [run.run_id, run]))
  const retainedRuns = nextRuns.map(run => prior.get(run.run_id))
  if (retainedRuns.some((run, index) => !sameRunIdentity(run, nextRuns[index]))) return null
  const retainedIds = new Set(retainedRuns.map(run => run.run_id))
  return {
    ...resource,
    runs: retainedRuns,
    details: resource.details.filter(detail => retainedIds.has(detail.runId)),
  }
}

const comparisonMetricFormatter = runs => {
  const values = runs.map(run => run.best_confirmed ?? run.best_metric)
    .filter(value => typeof value === 'number' && Number.isFinite(value))
  const distinct = [...new Set(values)]
  for (let precision = 4; precision <= 17; precision += 1) {
    const labels = distinct.map(value => fmt(value, precision))
    if (new Set(labels).size === distinct.length) {
      const byValue = new Map(distinct.map((value, index) => [value, labels[index]]))
      return value => typeof value === 'number' && Number.isFinite(value)
        ? byValue.get(value) ?? fmt(value, precision) : '—'
    }
  }
  const byValue = new Map(distinct.map(value => [value, String(value)]))
  return value => typeof value === 'number' && Number.isFinite(value)
    ? byValue.get(value) ?? String(value) : '—'
}

export async function loadDetail(run, signal, timeoutMs = COMPARE_DETAIL_TIMEOUT_MS) {
  let snapshot = null, config = null, probe = null, configReady = false
  const expectedGeneration = normalizeRunGeneration(run?.generation)
  const expectedSequence = Number.isSafeInteger(run?.seq) && run.seq >= -1 ? run.seq : null
  const unavailable = () => ({
    runId: run.run_id,
    generation: null,
    sequence: null,
    state: null,
    config: null,
    partial: true,
  })
  if (!expectedGeneration || expectedSequence == null) return unavailable()
  const timed = deadlineRequest(async requestSignal => {
    snapshot = await get(path(run.run_id, `/state?seq=${expectedSequence}`), {
      signal: requestSignal, cache: 'no-store',
    })
    try {
      config = await get(path(run.run_id, '/config'), { signal: requestSignal, cache: 'no-store' })
      configReady = true
    } catch (error) {
      if (error?.name === 'AbortError') throw error
    }
    try {
      // This is deliberately AFTER config: a reset during either read makes the compound snapshot
      // unverifiable, so config is withheld instead of being joined to another run generation.
      probe = await get(path(run.run_id, '/lifecycle'), { signal: requestSignal, cache: 'no-store' })
    } catch (error) {
      if (error?.name === 'AbortError') throw error
    }
  }, timeoutMs)
  const abort = () => timed.controller.abort()
  if (signal?.aborted) abort()
  else signal?.addEventListener('abort', abort, { once: true })
  try {
    await timed.promise
    const stateStable = snapshot?.generation === expectedGeneration
      && snapshot?.seq === expectedSequence
    const stable = stateStable && probe?.generation === expectedGeneration
      && probe?.seq === expectedSequence
    return {
      runId: run.run_id,
      generation: stateStable ? snapshot.generation : null,
      sequence: stateStable && Number.isSafeInteger(snapshot?.seq) ? snapshot.seq : null,
      state: stateStable ? snapshot.state || null : null,
      config: stable && configReady ? config : null,
      partial: !stable || !configReady,
    }
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    return unavailable()
  } finally {
    signal?.removeEventListener('abort', abort)
  }
}

export function championRunHref(run, detail) {
  const generation = normalizeRunGeneration(detail?.generation)
  const nodeId = detail?.state?.best_node_id
  const sequence = detail?.sequence
  const nodeGeneration = detail?.state?.nodes?.[nodeId]?.attempt
  if (!generation || !Number.isSafeInteger(nodeId) || nodeId < 0
      || !Number.isSafeInteger(sequence) || sequence < 0
      || !Number.isSafeInteger(nodeGeneration) || nodeGeneration < 0) return null
  return hashWithRunRouteState(
    `#/run/${encodeURIComponent(run.run_id)}`, {
      generation, nodeId, nodeGeneration, sequence,
    })
}

const valueFor = (id, run, detail, names, formatMetric) => {
  const state = detail?.state
  if (id === 'status') return effectiveRunStatus(run)
  if (id === 'task') return run.task_id || '—'
  if (id === 'best') return formatMetric(run.best_confirmed ?? run.best_metric)
  if (id === 'objective') return run.direction || '—'
  if (id === 'nodes') return run.nodes ?? '—'
  if (id === 'eval') return fmtElapsedSeconds(state?.total_eval_seconds)
  if (id === 'cost') return state?.llm_cost?.cost == null ? '—' : fmtCost(state.llm_cost)
  if (id === 'trust') return state?.trust_gate || '—'
  if (id === 'project') return names.projects[run.project_id] || '—'
  if (id === 'supertask') return names.supertasks[run.supertask_id] || '—'
  if (id === 'updated') return fmtAgo(run.mtime) || '—'
  return state?.best_node_id == null ? '—' : `#${state.best_node_id}`
}

const openComparisonRoute = (onOpen, runId, href) => {
  if (onOpen) onOpen(runId, href)
  else location.hash = href
}

export default function RunCompare({
  runs, columns, names, onColumns, onRemove, onOpen = null, headingRef = null,
  onFocusCapture = null,
}) {
  const identity = compareIdentity(runs)
  const [resource, setResource] = useState(() => ({
    identity,
    status: 'loading',
    runs: freezeRuns(runs),
    names: freezeNames(names),
    details: [],
    capturedAt: Date.now(),
    loadedAt: 0,
    error: '',
  }))
  const [retry, setRetry] = useState(0)
  const columnsDetailsRef = useRef(null)
  const requestEpochRef = useRef(0)
  const surfaceRef = useRef(null)
  const lastSurfaceFocusRef = useRef(null)
  const closeColumns = () => {
    const details = columnsDetailsRef.current
    if (!details?.open) return
    details.open = false
    requestAnimationFrame(() => details.querySelector('summary')?.focus({ preventScroll: true }))
  }
  useEffect(() => {
    const controller = new AbortController()
    const requestEpoch = ++requestEpochRef.current
    const capture = {
      identity,
      runs: freezeRuns(runs),
      names: freezeNames(names),
      capturedAt: Date.now(),
    }
    setResource(current => {
      const retained = retainedCapture(current, capture.runs)
      return retained
        ? { ...retained, identity, status: 'refreshing', error: '' }
        : { ...capture, status: 'loading', details: [], loadedAt: 0, error: '' }
    })
    Promise.all(capture.runs.map(run => loadDetail(run, controller.signal))).then(details => {
      if (!controller.signal.aborted && requestEpochRef.current === requestEpoch) {
        setResource(current => {
          if (!details.some(detail => detail.generation)) {
            const retained = retainedCapture(current, capture.runs)
            return retained
              ? { ...retained, identity, status: 'stale',
                  error: 'Snapshot refresh could not verify any run details.' }
              : { ...capture, status: 'partial', details, loadedAt: Date.now(), error: '' }
          }
          return { ...capture, status: 'ready', details, loadedAt: Date.now(), error: '' }
        })
      }
    }).catch(error => {
      if (error?.name === 'AbortError' || requestEpochRef.current !== requestEpoch) return
      setResource(current => {
        const retained = retainedCapture(current, capture.runs)
        const message = error?.name === 'TimeoutError'
          ? 'Snapshot refresh timed out.' : 'Snapshot refresh failed.'
        return retained
          ? { ...retained, identity, status: 'stale', error: message }
          : { ...capture, status: 'error', details: [], loadedAt: 0, error: message }
      })
    })
    return () => controller.abort()
    // The live list summaries and names are intentionally omitted. Polling must not
    // mutate an already displayed comparison capture; Refresh takes a new one.
  }, [identity, retry])

  const resourceRunById = new Map(resource.runs.map(run => [run.run_id, run]))
  const compatibleCapture = !!resource.loadedAt && runs.every(run =>
    sameRunIdentity(resourceRunById.get(run.run_id), run))
  const resourceMatches = resource.identity === identity
  const captureVisible = compatibleCapture || (resourceMatches && !!resource.loadedAt)
  const snapshotRuns = captureVisible
    ? runs.map(run => resourceRunById.get(run.run_id)).filter(Boolean) : []
  const selectedIds = new Set(snapshotRuns.map(run => run.run_id))
  const snapshotDetails = captureVisible
    ? resource.details.filter(detail => selectedIds.has(detail.runId)) : []
  const snapshotNames = captureVisible ? resource.names : freezeNames(names)
  const snapshotStatus = resourceMatches ? resource.status
    : compatibleCapture ? 'refreshing' : 'loading'
  const detailById = Object.fromEntries(snapshotDetails.map(detail => [detail.runId, detail]))
  const ranking = comparableRunRanking(snapshotRuns)
  const formatMetric = comparisonMetricFormatter(snapshotRuns)
  const bestRunIds = new Set(ranking.bestRunIds)
  const tiedBest = ranking.bestRunIds.length > 1
  const config = configDifferences(snapshotRuns.map(run => detailById[run.run_id]))
  const snapshotTime = resource.loadedAt
    ? new Date(resource.capturedAt || resource.loadedAt).toLocaleTimeString() : ''
  const snapshotBusy = snapshotStatus === 'loading' || snapshotStatus === 'refreshing'
  const snapshotPartial = snapshotStatus !== 'partial'
    && snapshotDetails.some(detail => detail.partial)
  const snapshotReceipt = snapshotStatus === 'loading'
    ? 'Loading a run comparison capture…'
    : snapshotStatus === 'refreshing'
      ? `Refreshing… Showing capture from ${snapshotTime}.`
      : snapshotStatus === 'stale'
        ? `${resource.error} Showing capture from ${snapshotTime}.`
        : snapshotStatus === 'error'
          ? 'Run comparison capture is unavailable.'
          : snapshotStatus === 'partial'
            ? `Summary capture completed ${snapshotTime}; run details could not be verified.`
            : `Comparison captured ${snapshotTime}.`
  const rankingReceipt = ranking.status === 'ranked'
    ? ranking.bestRunIds.length > 1
      ? ` ${ranking.bestRunIds.length} runs tied for best ${ranking.phase === 'confirmed'
        ? 'confirmed mean' : 'raw metric'} in this capture: ${formatMetric(ranking.bestValue)}.`
      : ` Best ${ranking.phase === 'confirmed' ? 'confirmed mean' : 'raw metric'} in this capture: ${formatMetric(ranking.bestValue)}.`
    : ''
  const rankingWarning = ranking.status === 'incompatible'
    ? 'Metrics are shown but not ranked; selected runs use different tasks or objectives.'
    : ranking.status === 'mixed-phase'
      ? 'Metrics are shown but not ranked because confirmed means and raw metrics are mixed.'
      : ranking.status === 'missing-metric'
        ? 'Metrics are shown but not ranked because one or more selected runs lack a finite metric.'
        : ''
  const refreshCapture = () => {
    if (snapshotBusy) return
    setRetry(value => value + 1)
  }
  const toggleColumn = id => onColumns(columns.includes(id)
    ? columns.filter(column => column !== id) : [...columns, id])
  const captureFocus = event => {
    lastSurfaceFocusRef.current = event.target
    onFocusCapture?.(event)
  }
  const removeRun = (runId, owner) => {
    // RunList owns the intentional Remove handoff to the next row or back to List.
    lastSurfaceFocusRef.current = null
    onRemove(runId, owner)
  }
  useLayoutEffect(() => {
    const owner = lastSurfaceFocusRef.current
    const active = document.activeElement
    const browserLostFocus = !active || active === document.body
      || active === document.documentElement || !active.isConnected
    if (owner && !owner.isConnected && browserLostFocus) {
      surfaceRef.current?.querySelector('#run-compare-title')?.focus({ preventScroll: true })
    }
  }, [identity, resource.loadedAt, snapshotStatus])

  return <section ref={surfaceRef} className="run-compare" aria-labelledby="run-compare-title"
    onFocusCapture={captureFocus}>
    <div className="compare-head">
      <div>
        <h2 ref={headingRef} id="run-compare-title" tabIndex={-1}>Run comparison</h2>
        <p>{runs.length} runs · The Run column stays pinned; choose the evidence columns you need.</p>
      </div>
      <details ref={columnsDetailsRef} className="compare-columns" onKeyDown={event => {
        if (event.key !== 'Escape' || !event.currentTarget.open) return
        event.preventDefault()
        event.stopPropagation()
        closeColumns()
      }}>
        <summary>Columns · {columns.length}</summary>
        <fieldset><legend className="sr-only">Comparison columns</legend>
          <button type="button" className="btn sm compare-columns-close" onClick={closeColumns}>
            Done
          </button>
          {COMPARE_COLUMNS.map(([id, label]) => <label key={id}>
            <input type="checkbox" checked={columns.includes(id)} onChange={() => toggleColumn(id)} /> {label}
          </label>)}
        </fieldset>
      </details>
      <button className="btn sm" aria-disabled={snapshotBusy || undefined}
        aria-busy={snapshotBusy || undefined} onClick={refreshCapture}>
        {snapshotStatus === 'loading' ? 'Loading…'
          : snapshotStatus === 'refreshing' ? 'Refreshing…' : 'Refresh snapshot'}
      </button>
    </div>
    <div id="run-compare-receipt" className="compare-receipt" role="status" aria-live="polite">
      {snapshotReceipt}
      {snapshotPartial && ' Some detail was unavailable or could not be verified for this capture.'}
      {rankingReceipt}
    </div>
    {snapshotRuns.length > 0 && rankingWarning && <div id="run-compare-ranking-warning"
      className="notice resource-warning" role="status">
      {rankingWarning}
    </div>}
    <div className="data-table-region">
      <div className="data-table-scroll" role="region" aria-label="Selected run comparison"
        aria-describedby={`run-compare-receipt${rankingWarning
          ? ' run-compare-ranking-warning' : ''}`} aria-busy={snapshotBusy} tabIndex={0}>
        <table className="tbl data-table compare-table">
          <caption className="sr-only">Comparison of selected runs</caption>
          <thead><tr><th scope="col" className="compare-pinned">Run</th>
            {columns.map(id => <th scope="col" key={id}>{COMPARE_COLUMNS.find(column => column[0] === id)?.[1]}</th>)}
            <th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>{snapshotRuns.map(run => {
            const label = run.label || run.run_id
            const detail = detailById[run.run_id]
            const isBest = ranking.status === 'ranked' && bestRunIds.has(run.run_id)
            const championHref = championRunHref(run, detail)
            return <tr key={run.run_id} className={isBest ? 'best-row' : ''}>
              <th scope="row" className="compare-pinned">
                <button type="button" className="compare-link" data-run-open-id={run.run_id}
                  onClick={() => openComparisonRoute(onOpen, run.run_id,
                    `#/run/${encodeURIComponent(run.run_id)}`)}>{label}</button>
                {isBest && <span className="pill compare-best">{tiedBest ? 'Tied best' : 'Best'}</span>}
                {run.label && <small>{run.run_id}</small>}
                {detail?.partial && <small>partial detail</small>}
              </th>
              {columns.map(id => <td key={id}>{id === 'champion' && championHref
                ? <button type="button" className="compare-link"
                    onClick={() => openComparisonRoute(onOpen, run.run_id, championHref)}
                    aria-label={`Open captured champion experiment ${detail.state.best_node_id} in ${label}`}>
                    #{detail.state.best_node_id}</button>
                : valueFor(id, run, detail, snapshotNames, formatMetric)}</td>)}
              <td><button className="btn xs ghost" data-compare-remove-id={run.run_id}
                onClick={event => removeRun(run.run_id, event.currentTarget)}
                aria-label={`Remove ${label} from comparison`}>Remove</button></td>
            </tr>
          })}
          {!snapshotRuns.length && <tr><td colSpan={columns.length + 2} className="muted">
            {snapshotStatus === 'error'
              ? 'No verified run capture is available.' : 'Loading selected run capture…'}
          </td></tr>}
          </tbody>
        </table>
      </div>
    </div>
    <details className="compare-config">
      <summary>Configuration differences · {config.total}</summary>
      {snapshotStatus !== 'loading' && config.rows.length
        ? <div className="data-table-scroll" role="region" aria-label="Configuration differences" tabIndex={0}>
            <table className="tbl data-table compare-config-table">
              <thead><tr><th scope="col">Setting</th>{snapshotRuns.map(run =>
                <th scope="col" key={run.run_id}>{run.label || run.run_id}</th>)}</tr></thead>
              <tbody>{config.rows.map(row => <tr key={row.key}><th scope="row">{row.key}</th>
                {row.values.map((value, index) => <td key={snapshotRuns[index].run_id}>{value}</td>)}</tr>)}</tbody>
            </table>
            {config.total > config.rows.length && <p className="muted">Showing the first {config.rows.length} of {config.total} differing settings.</p>}
          </div>
        : <p className="muted">{snapshotStatus === 'loading'
            ? 'Loading configuration capture…'
            : 'No verified configuration differences are available for this capture.'}</p>}
    </details>
  </section>
}
