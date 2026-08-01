import React, { useEffect, useMemo, useState } from 'react'
import { get, fmt, fmtAgo, fmtElapsedSeconds, normalizeRunGeneration } from './util.js'
import { effectiveRunStatus, metricComparable } from './runIndex.js'
import { bestComparableRun, COMPARE_COLUMNS, configDifferences } from './portfolioModel.js'
import { deadlineRequest } from './requestDeadline.js'
import { hashWithRunRouteState } from './runRouteState.js'

const path = (id, suffix) => `/api/runs/${encodeURIComponent(id)}${suffix}`
const COMPARE_DETAIL_TIMEOUT_MS = 8000

export async function loadDetail(run, signal, timeoutMs = COMPARE_DETAIL_TIMEOUT_MS) {
  let snapshot = null, config = null, probe = null, configReady = false
  const expectedGeneration = normalizeRunGeneration(run?.generation)
  const timed = deadlineRequest(async requestSignal => {
    snapshot = await get(path(run.run_id, '/state'), { signal: requestSignal, cache: 'no-store' })
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
    const stable = !!expectedGeneration && snapshot?.generation === expectedGeneration
      && probe?.generation === expectedGeneration
    return {
      runId: run.run_id,
      generation: snapshot?.generation || null,
      state: snapshot?.state || null,
      config: stable && configReady ? config : null,
      partial: !stable || !configReady,
    }
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    return {
      runId: run.run_id,
      generation: snapshot?.generation || null,
      state: snapshot?.state || null,
      config: null,
      partial: true,
    }
  } finally {
    signal?.removeEventListener('abort', abort)
  }
}

export function championRunHref(run, detail) {
  const generation = normalizeRunGeneration(detail?.generation)
  const nodeId = detail?.state?.best_node_id
  if (!generation || !Number.isSafeInteger(nodeId) || nodeId < 0) return null
  return hashWithRunRouteState(
    `#/run/${encodeURIComponent(run.run_id)}`, { generation, nodeId })
}

const valueFor = (id, run, detail, names) => {
  const state = detail?.state
  if (id === 'status') return effectiveRunStatus(run)
  if (id === 'task') return run.task_id || '—'
  if (id === 'best') return fmt(run.best_confirmed ?? run.best_metric)
  if (id === 'objective') return run.direction || '—'
  if (id === 'nodes') return run.nodes ?? '—'
  if (id === 'eval') return fmtElapsedSeconds(state?.total_eval_seconds)
  if (id === 'cost') return state?.llm_cost?.cost == null ? '—' : `$${fmt(state.llm_cost.cost)}`
  if (id === 'trust') return state?.trust_gate || '—'
  if (id === 'project') return names.projects[run.project_id] || '—'
  if (id === 'supertask') return names.supertasks[run.supertask_id] || '—'
  if (id === 'updated') return fmtAgo(run.mtime) || '—'
  return state?.best_node_id == null ? '—' : `#${state.best_node_id}`
}

const openComparisonRoute = href => { location.hash = href }

export default function RunCompare({ runs, columns, names, onColumns, onRemove }) {
  const [resource, setResource] = useState({ status: 'loading', details: [], loadedAt: 0 })
  const [retry, setRetry] = useState(0)
  const identity = runs.map(run => `${run.run_id}@${normalizeRunGeneration(run.generation) || '?'}`)
    .join('\n')
  useEffect(() => {
    const controller = new AbortController()
    setResource(current => ({ ...current, status: 'loading' }))
    Promise.all(runs.map(run => loadDetail(run, controller.signal))).then(details => {
      if (!controller.signal.aborted) setResource({ status: 'ready', details, loadedAt: Date.now() })
    }).catch(error => {
      if (error?.name !== 'AbortError') setResource({ status: 'error', details: [], loadedAt: 0 })
    })
    return () => controller.abort()
  }, [identity, retry])

  const detailById = useMemo(
    () => Object.fromEntries(resource.details.map(detail => [detail.runId, detail])),
    [resource.details])
  const best = bestComparableRun(runs)?.run_id
  const comparable = metricComparable(runs)
  const config = useMemo(() => configDifferences(
    runs.map(run => detailById[run.run_id])), [identity, detailById])
  const toggleColumn = id => onColumns(columns.includes(id)
    ? columns.filter(column => column !== id) : [...columns, id])

  return <section className="run-compare" aria-labelledby="run-compare-title">
    <div className="compare-head">
      <div>
        <h2 id="run-compare-title">Run comparison</h2>
        <p>{runs.length} runs · Run stays pinned; choose the evidence columns you need.</p>
      </div>
      <details className="compare-columns">
        <summary>Columns · {columns.length}</summary>
        <fieldset><legend className="sr-only">Comparison columns</legend>
          {COMPARE_COLUMNS.map(([id, label]) => <label key={id}>
            <input type="checkbox" checked={columns.includes(id)} onChange={() => toggleColumn(id)} /> {label}
          </label>)}
        </fieldset>
      </details>
      <button className="btn sm" onClick={() => setRetry(value => value + 1)}>Refresh snapshot</button>
    </div>
    <div className="compare-receipt" role="status" aria-live="polite">
      {resource.status === 'loading' ? 'Loading generation-fenced run details…'
        : resource.status === 'error' ? 'Run details are unavailable. Summary fields remain visible.'
        : `Snapshot loaded ${new Date(resource.loadedAt).toLocaleTimeString()}`
          + (resource.details.some(detail => detail.partial) ? ' · some detail is unavailable or changed generation' : '')}
    </div>
    {!comparable && <div className="notice resource-warning" role="status">
      Best metrics are shown but not ranked because the selected runs do not share one task and objective.
    </div>}
    <div className="data-table-region">
      <div className="data-table-scroll" role="region" aria-label="Selected run comparison" tabIndex={0}>
        <table className="tbl data-table compare-table">
          <caption className="sr-only">Comparison of selected runs</caption>
          <thead><tr><th scope="col" className="compare-pinned">Run</th>
            {columns.map(id => <th scope="col" key={id}>{COMPARE_COLUMNS.find(column => column[0] === id)?.[1]}</th>)}
            <th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>{runs.map(run => {
            const label = run.label || run.run_id
            const detail = detailById[run.run_id]
            const isBest = comparable && run.run_id === best
            const championHref = championRunHref(run, detail)
            return <tr key={run.run_id} className={isBest ? 'best-row' : ''}>
              <th scope="row" className="compare-pinned">
                <button type="button" className="compare-link"
                  onClick={() => openComparisonRoute(
                    `#/run/${encodeURIComponent(run.run_id)}`)}>{label}</button>
                {isBest && <span className="pill compare-best">Best</span>}
                {run.label && <small>{run.run_id}</small>}
                {detail?.partial && <small>partial detail</small>}
              </th>
              {columns.map(id => <td key={id}>{id === 'champion' && championHref
                ? <button type="button" className="compare-link"
                    onClick={() => openComparisonRoute(championHref)}
                    aria-label={`Open champion experiment ${detail.state.best_node_id} in ${label}`}>
                    #{detail.state.best_node_id}</button>
                : valueFor(id, run, detail, names)}</td>)}
              <td><button className="btn xs ghost" onClick={() => onRemove(run.run_id)}
                aria-label={`Remove ${label} from comparison`}>Remove</button></td>
            </tr>
          })}</tbody>
        </table>
      </div>
    </div>
    <details className="compare-config">
      <summary>Configuration differences · {config.total}</summary>
      {resource.status === 'ready' && config.rows.length
        ? <div className="data-table-scroll" role="region" aria-label="Configuration differences" tabIndex={0}>
            <table className="tbl data-table compare-config-table">
              <thead><tr><th scope="col">Setting</th>{runs.map(run =>
                <th scope="col" key={run.run_id}>{run.label || run.run_id}</th>)}</tr></thead>
              <tbody>{config.rows.map(row => <tr key={row.key}><th scope="row">{row.key}</th>
                {row.values.map((value, index) => <td key={runs[index].run_id}>{value}</td>)}</tr>)}</tbody>
            </table>
            {config.total > config.rows.length && <p className="muted">Showing the first {config.rows.length} of {config.total} differing settings.</p>}
          </div>
        : <p className="muted">{resource.status === 'loading'
            ? 'Loading configuration snapshots…'
            : 'No verified configuration differences are available for this snapshot.'}</p>}
    </details>
  </section>
}
