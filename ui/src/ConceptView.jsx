import React, { useEffect, useMemo, useState } from 'react'
import { get, fmt } from './util.js'
import {
  experimentsByConcept, visibleConceptRows, conceptLeaf, deltaTone, fmtCell,
  CONCEPT_COLUMNS, DEFAULT_COLUMNS,
} from './conceptViewModel.js'

// View 1 — the in-run CONCEPT tree/table. Concepts are folders (any depth, any lens); experiments
// nest under the EXACT concept they were tagged with (many-to-many: a node appears under each of its
// concepts, never divided); a configurable, ClearML-style metric table with Δ-from-baseline coloring
// rides each concept row. The tree + per-concept metrics are single-sourced from the /concepts
// endpoint (project_hierarchy / project_lens / concept_metrics); experiment placement is joined
// client-side from the streamed node_concepts. Click an experiment to open the Inspector.
export default function ConceptView({ runId, state, onPickNode }) {
  const [lens, setLens] = useState('is_a')
  const [data, setData] = useState(null)
  const [error, setError] = useState(false)
  const [expanded, setExpanded] = useState(() => new Set())
  const [columns, setColumns] = useState(DEFAULT_COLUMNS)

  const nodeConcepts = state?.node_concepts || {}
  const rename = state?.concept_consolidation || {}
  // Refetch the projection when the lens changes or the run grows (a cheap tag-count signal keeps the
  // view live under SSE without re-fetching on every unrelated state tick).
  // REVIEW(2026-07-16): the total-tag-COUNT signal is lossy in exactly the cases the concept cadence
  // produces: (1) a B1 staleness re-tag REPLACES a node's ids with the same number of new ids
  // (count unchanged -> no refetch, tree/metrics stay stale); (2) a consolidation rename changes ids
  // without touching counts (same); (3) node_evaluated ticks change metrics the endpoint would report
  // while counts stay flat, so Δ/best columns lag until the next tagging burst. The signal also
  // ignores `concept_edges`, so a fresh Phase 2c edge emission never refreshes an edge-lens view.
  // A cheap content signal (e.g. state.seq / events length, or a join of node_concepts ids +
  // rename size + edge count) refetches correctly for the same effort — count alone under-triggers.
  const ncSize = useMemo(
    () => Object.values(nodeConcepts).reduce((a, v) => a + (v ? v.length : 0), 0), [nodeConcepts])

  useEffect(() => {
    let active = true
    setError(false)
    get(`/api/runs/${encodeURIComponent(runId)}/concepts?lens=${encodeURIComponent(lens)}`)
      .then(v => { if (active) setData(v) })
      .catch(() => { if (active) { setData(null); setError(true) } })
    return () => { active = false }
  }, [runId, lens, ncSize])

  const expsByConcept = useMemo(() => experimentsByConcept(nodeConcepts, rename), [nodeConcepts, rename])
  const rows = useMemo(() => visibleConceptRows(data && data.tree, expanded), [data, expanded])
  const metricRows = (data && data.metrics && data.metrics.rows) || {}
  const lenses = (data && data.lenses) || []
  const baseline = data && data.metrics && data.metrics.baseline

  const toggle = (id) => setExpanded(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const expandAll = () => setExpanded(new Set(Object.keys((data && data.tree && data.tree.nodes) || {})))
  const collapseAll = () => setExpanded(new Set())
  const toggleCol = (k) => setColumns(cs => cs.includes(k) ? cs.filter(x => x !== k) : [...cs, k])
  const cols = CONCEPT_COLUMNS.filter(c => columns.includes(c.key))
  const robust = (n) => (n && (n.confirmed_mean != null ? n.confirmed_mean : n.metric))

  if (!data && !error) return <div className="concept-view" data-route-main tabIndex={-1}>
    <div className="cv-empty">Loading concepts…</div></div>
  const empty = !data || !((data.tree && data.tree.roots) || []).length

  return (
    <div className="concept-view" data-route-main tabIndex={-1} aria-label="Concept tree">
      <div className="cv-bar">
        <span className="cv-lensctl">
          <span className="muted">lens</span>
          <select className="text" value={lens} onChange={e => setLens(e.target.value)} aria-label="Concept lens">
            {(lenses.length ? lenses : [{ name: 'is_a', label: 'Family · is-a' }]).map(l =>
              <option key={l.name} value={l.name}>{l.label || l.name}</option>)}
          </select>
        </span>
        <span className="cv-cols" role="group" aria-label="Metric columns">
          <span className="muted">columns</span>
          {CONCEPT_COLUMNS.map(c =>
            <button key={c.key} type="button" aria-pressed={columns.includes(c.key)}
              className={'cv-col' + (columns.includes(c.key) ? ' on' : '')}
              onClick={() => toggleCol(c.key)}>{c.label}</button>)}
        </span>
        <span className="spacer" style={{ flex: 1 }} />
        {baseline != null && <span className="muted cv-baseline">baseline {fmt(baseline)}</span>}
        <button className="btn sm ghost" onClick={expandAll} disabled={empty}>⊞ all</button>
        <button className="btn sm ghost" onClick={collapseAll} disabled={empty}>⊟ all</button>
      </div>
      {empty
        ? <div className="cv-empty">{error
            ? 'Could not load concepts.'
            : 'No concepts tagged yet — experiments carry concepts once the Researcher tags them.'}</div>
        : <div className="cv-table-wrap">
          <table className="cv-table">
            <thead><tr><th className="cv-name">concept</th>
              {cols.map(c => <th key={c.key} className="cv-num">{c.label}</th>)}</tr></thead>
            <tbody>
              {rows.map(({ id, depth, hasChildren }) => {
                const m = metricRows[id] || {}
                const node = data.tree.nodes[id]
                const exps = expanded.has(id) ? (expsByConcept[id] || []) : []
                return <React.Fragment key={id}>
                  <tr className={'cv-crow' + (node && node.tagged ? ' tagged' : ' ghost')}>
                    <td className="cv-name" style={{ paddingLeft: 8 + depth * 16 }}>
                      {hasChildren
                        ? <button className="cv-chev" onClick={() => toggle(id)} aria-expanded={expanded.has(id)}
                            aria-label={(expanded.has(id) ? 'Collapse ' : 'Expand ') + id}>
                            {expanded.has(id) ? '▾' : '▸'}</button>
                        : <button className="cv-chev" onClick={() => toggle(id)} aria-expanded={expanded.has(id)}
                            aria-label={(expanded.has(id) ? 'Hide' : 'Show') + ' experiments for ' + id}>
                            {expanded.has(id) ? '▾' : '·'}</button>}
                      <span className="cv-cid" title={id}>{conceptLeaf(id)}</span>
                      {(expsByConcept[id] && expsByConcept[id].length)
                        ? <span className="cv-badge">{expsByConcept[id].length}</span> : null}
                    </td>
                    {cols.map(c => {
                      const v = m[c.key]
                      const tone = c.delta ? deltaTone(v) : ''
                      return <td key={c.key}
                        className={'cv-num' + (tone ? ' d-' + tone : '')}>{fmtCell(v)}</td>
                    })}
                  </tr>
                  {exps.map(nid => {
                    const n = state.nodes && state.nodes[nid]
                    return <tr key={id + ':' + nid} className="cv-erow" onClick={() => onPickNode && onPickNode(nid)}
                      tabIndex={0} role="button"
                      onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); onPickNode && onPickNode(nid) } }}>
                      <td className="cv-name" style={{ paddingLeft: 8 + (depth + 1) * 16 }}>
                        <span className="cv-exp">#{nid}</span>
                        {n && n.id === state.best_node_id ? <span className="cv-best" title="champion">★</span> : null}
                      </td>
                      <td className="cv-num cv-expmetric" colSpan={Math.max(1, cols.length)}>{fmt(robust(n))}</td>
                    </tr>
                  })}
                </React.Fragment>
              })}
            </tbody>
          </table>
        </div>}
    </div>
  )
}
