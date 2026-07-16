import React, { useEffect, useMemo, useRef, useState } from 'react'
import { get, fmt, runApiPath } from './util.js'
import {
  experimentsByConcept, visibleConceptRows, conceptLeaf, deltaTone, fmtCell,
  CONCEPT_COLUMNS, DEFAULT_COLUMNS,
} from './conceptViewModel.js'

const TIMEOUT_MS = 12_000
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const metric = value => value === null || (typeof value === 'number' && Number.isFinite(value))

// A malformed HTTP 200 is unavailable data, never an authoritative empty projection.
export function validateConceptPayload(value) {
  if (!record(value) || typeof value.lens !== 'string' || !Array.isArray(value.lenses)
      || !value.lenses.length || typeof value.edges_present !== 'boolean'
      || !record(value.tree) || !Array.isArray(value.tree.roots) || !record(value.tree.nodes)
      || !record(value.metrics) || !record(value.metrics.rows) || !metric(value.metrics.baseline)) {
    throw new TypeError('Invalid concept projection')
  }
  if (!value.lenses.every(item => record(item) && typeof item.name === 'string' && item.name
      && (item.label == null || typeof item.label === 'string'))
      || !value.tree.roots.every(id => typeof id === 'string')) throw new TypeError('Invalid concept projection')
  for (const node of Object.values(value.tree.nodes)) {
    if (!record(node) || !Array.isArray(node.children)
        || !node.children.every(id => typeof id === 'string')
        || (node.tagged != null && typeof node.tagged !== 'boolean')) throw new TypeError('Invalid concept projection')
  }
  for (const row of Object.values(value.metrics.rows)) {
    if (!record(row) || !CONCEPT_COLUMNS.every(column => metric(row[column.key]))) {
      throw new TypeError('Invalid concept projection')
    }
  }
  return value
}

const entries = value => record(value) ? Object.keys(value).sort().map(key => [key, value[key]]) : []

// Mirrors all inputs used by projection + concept_metrics. Same-count retags, renames, typed edges,
// feasibility and robust metrics refresh; an engine-liveness-only SSE tick does not.
export function conceptProjectionKey(state) {
  const nodes = entries(state?.nodes).map(([key, node]) => [
    key, node?.id, node?.metric, node?.confirmed_mean, node?.feasible, !!node?.idea,
  ])
  const edges = entries(state?.concept_edges).map(([key, edge]) => [
    key, edge?.src, edge?.rel, edge?.dst, edge?.confidence,
  ])
  return JSON.stringify([state?.direction || 'max', entries(state?.node_concepts),
    entries(state?.concept_consolidation), edges, nodes])
}

const initial = { scope: '', status: 'loading', data: null, timeout: false }

function StateCard({ tone, title, body, action, pending = false, stale = false }) {
  return <section className={`cv-state-card ${tone}`} role={tone === 'error' || stale ? 'alert' : 'status'}
    aria-live={tone === 'error' || stale ? 'assertive' : 'polite'} aria-atomic="true">
    <span className="cv-state-mark" aria-hidden="true">{tone === 'loading' ? '' : tone === 'error' ? '!' : '◇'}</span>
    <span className="cv-state-eyebrow">Concept map</span><h2>{title}</h2><p>{body}</p>
    {tone === 'empty' && <div className="cv-empty-flow" aria-label="How the concept view is built">
      <span>Experiments</span><i aria-hidden="true">→</i><span>Concept hierarchy</span>
      <i aria-hidden="true">→</i><span>Outcome comparison</span>
    </div>}
    {stale && <p className="cv-state-warning">Refresh failed; this is the last loaded empty result.</p>}
    {action && <button type="button" className="btn primary" onClick={action} disabled={pending}>
      {pending ? 'Refreshing…' : tone === 'error' ? 'Retry' : 'Refresh concepts'}
    </button>}
  </section>
}

export default function ConceptView({ runId, state, onPickNode }) {
  const [lens, setLens] = useState('is_a')
  const [resource, setResource] = useState(initial)
  const [retry, setRetry] = useState(0)
  const [expanded, setExpanded] = useState(() => new Set())
  const [columns, setColumns] = useState(DEFAULT_COLUMNS)
  const request = useRef({ id: 0, controller: null })
  const nodeConcepts = state?.node_concepts || {}
  const rename = state?.concept_consolidation || {}
  const projectionKey = useMemo(() => conceptProjectionKey(state), [state])
  const scope = `${runId}\u0000${lens}`

  useEffect(() => {
    const id = request.current.id + 1
    request.current.controller?.abort()
    const controller = new AbortController()
    request.current = { id, controller }
    setResource(previous => previous.scope === scope && previous.data
      ? { ...previous, status: 'refreshing', timeout: false }
      : { scope, status: 'loading', data: null, timeout: false })
    let timer
    let timedOut = false
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => {
        timedOut = true
        controller.abort()
        reject(new Error('timeout'))
      }, TIMEOUT_MS)
    })
    Promise.race([
      get(`${runApiPath(runId, '/concepts')}?lens=${encodeURIComponent(lens)}`, { signal: controller.signal }),
      deadline,
    ]).then(validateConceptPayload).then(data => {
      if (request.current.id === id && !controller.signal.aborted) {
        setResource({ scope, status: 'ready', data, timeout: false })
      }
    }).catch(() => {
      if (request.current.id !== id || (controller.signal.aborted && !timedOut)) return
      setResource(previous => previous.scope === scope && previous.data
        ? { ...previous, status: 'stale', timeout: timedOut }
        : { scope, status: 'error', data: null, timeout: timedOut })
    }).finally(() => clearTimeout(timer))
    return () => { clearTimeout(timer); controller.abort() }
  }, [runId, lens, projectionKey, retry])

  const current = resource.scope === scope ? resource : initial
  const data = current.data
  useEffect(() => {
    if (data?.edges_present === false && lens !== 'is_a') {
      setExpanded(new Set())
      setLens('is_a')
    }
  }, [data, lens])

  const byConcept = useMemo(() => experimentsByConcept(nodeConcepts, rename), [nodeConcepts, rename])
  const rows = useMemo(() => visibleConceptRows(data?.tree, expanded), [data, expanded])
  const roots = data?.tree?.roots || []
  const empty = !!data && roots.length === 0
  const refreshing = current.status === 'refreshing'
  const refresh = () => setRetry(value => value + 1)
  const toggle = id => setExpanded(previous => {
    const next = new Set(previous); next.has(id) ? next.delete(id) : next.add(id); return next
  })
  const toggleColumn = key => setColumns(value => value.includes(key)
    ? value.length === 1 ? value : value.filter(item => item !== key) : [...value, key])
  const cols = CONCEPT_COLUMNS.filter(column => columns.includes(column.key))

  if (current.status === 'loading') return <div className="concept-view cv-state-layout"
    data-route-main tabIndex={-1} aria-label="Concept tree"><StateCard tone="loading"
      title="Building the concept view" body="Loading the latest hierarchy and outcome rollups for this run." /></div>
  if (current.status === 'error') return <div className="concept-view cv-state-layout"
    data-route-main tabIndex={-1} aria-label="Concept tree"><StateCard tone="error"
      title="Concepts are unavailable" action={refresh} body={current.timeout
        ? 'The concept projection did not respond in time. The run is unchanged; retry this read.'
        : 'The concept projection could not be read. The run is unchanged; retry when the server is reachable.'} /></div>
  if (empty) return <div className="concept-view cv-state-layout"
    data-route-main tabIndex={-1} aria-label="Concept tree"><StateCard tone="empty"
      title="No concepts have been tagged yet" action={refresh} pending={refreshing}
      stale={current.status === 'stale'} body="This view fills automatically after the Researcher assigns concepts to experiments. Until then, LoopLab keeps the canvas honest instead of inventing a taxonomy." /></div>

  const metricRows = data.metrics.rows
  const experimentCount = new Set(Object.values(byConcept).flat()).size
  const availableLenses = data.edges_present ? data.lenses : data.lenses.filter(item => item.name === 'is_a')
  const robust = node => node && (node.confirmed_mean != null ? node.confirmed_mean : node.metric)
  return <div className="concept-view" data-route-main tabIndex={-1} aria-label="Concept tree">
    <header className="cv-bar">
      <div className="cv-heading"><strong>Concept tree</strong><span>{Object.keys(data.tree.nodes).length} concepts · {experimentCount} tagged experiments</span></div>
      <label className="cv-lensctl"><span>Hierarchy lens</span><select className="text" value={lens}
        onChange={event => { setExpanded(new Set()); setLens(event.target.value) }} aria-label="Concept hierarchy lens">
        {availableLenses.map(item => <option key={item.name} value={item.name}>{item.label || item.name}</option>)}
      </select></label>
      <div className="cv-tree-actions">
        <button type="button" className="btn sm ghost" onClick={() => setExpanded(new Set(Object.keys(data.tree.nodes)))}>Expand all</button>
        <button type="button" className="btn sm ghost" onClick={() => setExpanded(new Set())}>Collapse all</button>
        <button type="button" className="btn sm" onClick={refresh} disabled={refreshing}>{refreshing ? 'Refreshing…' : 'Refresh'}</button>
      </div>
      <div className="cv-cols" role="group" aria-label="Visible metric columns"><span>Metrics</span>
        {CONCEPT_COLUMNS.map(column => <button key={column.key} type="button" aria-pressed={columns.includes(column.key)}
          disabled={columns.includes(column.key) && columns.length === 1}
          className={'cv-col' + (columns.includes(column.key) ? ' on' : '')}
          onClick={() => toggleColumn(column.key)}>{column.label}</button>)}
        {data.metrics.baseline != null && <span className="cv-baseline">run baseline {fmt(data.metrics.baseline)}</span>}
      </div>
    </header>
    {refreshing && <div className="cv-resource-note" role="status" aria-live="polite"><span className="cv-inline-spinner" aria-hidden="true" />Refreshing concepts… Last loaded view remains visible.</div>}
    {current.status === 'stale' && <div className="cv-resource-note stale" role="alert"><span>Showing the last loaded concept view; refresh {current.timeout ? 'timed out' : 'failed'}.</span><button type="button" className="btn sm" onClick={refresh}>Retry</button></div>}
    {rows.projectionStatus?.state === 'partial' && <div className="cv-resource-note partial" role="status">Some malformed relationships were omitted; the safe portion remains visible.</div>}
    <div className="cv-table-wrap"><table className="cv-table"><thead><tr><th className="cv-name" scope="col">Concept / experiment</th>
      {cols.map(column => <th key={column.key} className="cv-num" scope="col">{column.label}</th>)}</tr></thead><tbody>
      {rows.map(({ id, depth, hasChildren }) => {
        const node = data.tree.nodes[id]
        const experiments = byConcept[id] || []
        const open = expanded.has(id)
        return <React.Fragment key={id}><tr className={'cv-crow' + (node?.tagged ? ' tagged' : ' ghost')}>
          <td className="cv-name" style={{ paddingLeft: 12 + depth * 18 }}>
            {hasChildren || experiments.length ? <button type="button" className="cv-chev" onClick={() => toggle(id)}
              aria-expanded={open} aria-label={`${open ? 'Collapse' : 'Expand'} ${id}`}>{open ? '▾' : '▸'}</button>
              : <span className="cv-chev-placeholder" aria-hidden="true">·</span>}
            <span className="cv-cid" title={id}>{conceptLeaf(id)}</span>
            {!!experiments.length && <span className="cv-badge" aria-label={`${experiments.length} tagged experiments`}>{experiments.length}</span>}
          </td>{cols.map(column => {
            const value = metricRows[id]?.[column.key]
            const tone = column.delta ? deltaTone(value) : ''
            return <td key={column.key} className={'cv-num' + (tone ? ` d-${tone}` : '')}>{fmtCell(value)}</td>
          })}</tr>
          {open && experiments.map(nodeId => {
            const experiment = state.nodes?.[nodeId]
            return <tr key={`${id}:${nodeId}`} className="cv-erow"><td className="cv-name" style={{ paddingLeft: 12 + (depth + 1) * 18 }}>
              <button type="button" className="cv-exp-button" onClick={() => onPickNode?.(nodeId)} aria-label={`Open experiment ${nodeId} in Inspector`}>
                <span className="cv-exp">Experiment #{nodeId}</span>{experiment?.id === state.best_node_id
                  && <span className="cv-best" title="Run champion" aria-label="Run champion">★</span>}</button></td>
              <td className="cv-num cv-expmetric" colSpan={cols.length}>{fmt(robust(experiment))}</td></tr>
          })}</React.Fragment>
      })}
    </tbody></table></div>
  </div>
}
