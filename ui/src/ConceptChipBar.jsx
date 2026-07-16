import React, { useEffect, useMemo, useState } from 'react'
import { chipsAtPath, breadcrumb, matchingNodeIds } from './conceptChips.js'

// View 2 — the concept chip bar riding OVER the lineage graph. Breadcrumb-navigable (drill into a
// concept to reveal its next level) and multi-selectable (OR): selecting concepts highlights every
// graph node that touches any selected concept OR a descendant of it, dimming the rest. Reads the
// folded `node_concepts` + consolidation rename straight off the run state (same source as View 1 —
// no extra fetch). Pure presentation; the selection drives the graph via `onHighlight(set | null)`.
// The pure model (chip counts, breadcrumb, matching set) lives in conceptChips.js and is unit-tested;
// this component is only wiring + markup.
export default function ConceptChipBar({ state, onHighlight }) {
  const nodeConcepts = state?.node_concepts || {}
  const rename = state?.concept_consolidation || {}
  const [path, setPath] = useState('')                 // breadcrumb drill position ('' = roots)
  const [selected, setSelected] = useState([])         // ordered selected concept ids (OR)

  const hasConcepts = useMemo(
    () => Object.values(nodeConcepts).some(v => v && v.length), [nodeConcepts])
  const chips = useMemo(() => chipsAtPath(nodeConcepts, rename, path), [nodeConcepts, rename, path])
  const crumbs = useMemo(() => breadcrumb(path), [path])

  // The highlighted node set is a pure function of (nodeConcepts, selected, rename). Push it up whenever
  // its VALUE changes — keyed on a stable signature so a live SSE tick that only re-refs node_concepts
  // (same matching ids) doesn't churn the parent, while a genuinely new/vanished match still updates.
  const highlight = useMemo(
    () => matchingNodeIds(nodeConcepts, selected, rename), [nodeConcepts, selected, rename])
  const sig = highlight ? [...highlight].sort((a, b) => a - b).join(',') : ''
  useEffect(() => { onHighlight && onHighlight(highlight) }, [sig])       // onHighlight is a stable setter
  useEffect(() => () => { onHighlight && onHighlight(null) }, [])         // clear the dim when unmounted

  const toggleSelect = (id) =>
    setSelected(s => s.includes(id) ? s.filter(x => x !== id) : [...s, id])
  const removeSelected = (id) => setSelected(s => s.filter(x => x !== id))
  const clearSelection = () => setSelected([])

  if (!hasConcepts) return null
  const leaf = (id) => String(id).split('/').pop()

  return (
    <div className="concept-bar" role="group" aria-label="Concept filter">
      <div className="cb-head">
        <strong>Concepts</strong>
        <nav className="cb-crumbs" aria-label="Concept breadcrumb">
          <button type="button" className={'cb-crumb' + (path ? '' : ' on')}
            onClick={() => setPath('')} aria-current={path ? undefined : 'true'}>All</button>
          {crumbs.map((c, i) => <React.Fragment key={c.id}>
            <span className="cb-sep" aria-hidden="true">›</span>
            <button type="button" className={'cb-crumb' + (i === crumbs.length - 1 ? ' on' : '')}
              onClick={() => setPath(c.id)}
              aria-current={i === crumbs.length - 1 ? 'true' : undefined}>{c.label}</button>
          </React.Fragment>)}
        </nav>
        <span className="spacer" style={{ flex: 1 }} />
        {selected.length > 0 &&
          <button type="button" className="btn sm ghost" onClick={clearSelection}>
            clear ({selected.length})</button>}
      </div>

      {selected.length > 0 &&
        <div className="cb-selected" aria-label="Selected concepts">
          {selected.map(id => <button key={id} type="button" className="cb-pill"
            onClick={() => removeSelected(id)} title={`${id} — click to remove`}>
            {leaf(id)}<span className="cb-x" aria-hidden="true">×</span>
            <span className="sr-only"> remove filter {id}</span>
          </button>)}
        </div>}

      <div className="cb-chips" aria-label={path ? `Concepts under ${path}` : 'Top-level concepts'}>
        {chips.length === 0
          ? <span className="muted cb-empty">No concepts at this level.</span>
          : chips.map(chip => {
            const on = selected.includes(chip.id)
            return (
              <span key={chip.id + (chip.atLevel ? '#here' : '')}
                className={'cb-chip' + (on ? ' on' : '') + (chip.atLevel ? ' here' : '')}>
                <button type="button" className="cb-chip-main" aria-pressed={on}
                  onClick={() => toggleSelect(chip.id)}
                  title={`${chip.id} · ${chip.count} experiment(s)${chip.atLevel ? ' tagged here (not deeper)' : ''}`}>
                  {chip.atLevel && <span className="cb-here" aria-hidden="true">·</span>}
                  <span className="cb-name">{chip.label}</span>
                  <span className="cb-count">{chip.count}</span>
                </button>
                {!chip.atLevel &&
                  <button type="button" className="cb-drill" aria-label={`Open ${chip.id}`}
                    onClick={() => setPath(chip.id)} title={`Drill into ${chip.label}`}>›</button>}
              </span>
            )
          })}
      </div>
    </div>
  )
}
