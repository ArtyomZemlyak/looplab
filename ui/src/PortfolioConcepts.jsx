import React, { useEffect, useMemo, useRef, useState } from 'react'
import { OpIcon } from './icons.jsx'
import {
  buildConceptForest, forestCoverage, forestPathTo, visibleForestRows,
} from './conceptForest.js'
import './portfolio-concepts.css'

// The GLOBAL concept view — the run list's `Concepts` representation, beside List / Lineage / Compare.
//
// This is the choreography half of the `conceptForest.js` pair (the house pattern: the decisions live
// in a plain ES module `node --test` can drive; this file keeps expansion state, selection and focus).
//
// SCOPE IS NOT NEGOTIATED HERE. The component takes the run array as a prop — the SAME `mapRuns` the
// Map draws and the same rows the List shows, already narrowed by the project folder and every list
// filter. There is deliberately no fetch and no second filter set in this file: two code paths that
// each know how to scope runs is exactly how a view ends up quietly showing a different population
// than the list behind it. The only scope choice this view adds is a restriction to the runs the
// operator has CHECKED for Compare, which is a subset of what is already on screen.
//
// Names are lower case ids on purpose. `MapView.jsx` made the same call for its run-card chips: "what
// makes the map and the concept tree the SAME vocabulary" is showing the id a run actually authored.

const MAX_DETAIL_RUNS = 40

const runLabel = run => run?.label || run?.run_id || ''

// A tree node's metric line. Present only when `conceptForest.nodeBest` found ONE task and ONE
// direction across the contributing runs, and it always names them — an unlabelled 0.94 beside an
// unlabelled 0.0 invites a comparison between an accuracy and a loss.
function BestMetric({ best }) {
  if (!best) return null
  const value = Number.isInteger(best.value) ? String(best.value) : best.value.toFixed(4)
  return <span className="pc-metric"
    title={`best ${best.direction === 'min' ? 'lowest' : 'highest'} robust metric across `
      + `${best.runs} run(s) of task ${best.taskId}`}>
    {best.direction === 'min' ? '↓' : '↑'} {value}
  </span>
}

function ConceptRow({ row, selected, expanded, onToggle, onSelect, setRef }) {
  const node = row.node
  return <li className="pc-row" style={{ paddingLeft: 4 + row.depth * 17 }}>
    {row.hasChildren
      ? <button type="button" className="pc-twist" aria-expanded={expanded}
          aria-label={`${expanded ? 'Collapse' : 'Expand'} ${row.id}`}
          onClick={() => onToggle(row.id)}>{expanded ? '▾' : '▸'}</button>
      : <span className="pc-twist pc-twist-leaf" aria-hidden="true">·</span>}
    <button ref={setRef} type="button" data-concept-id={row.id}
      className={'pc-name' + (selected ? ' on' : '') + (node.tagged ? '' : ' pc-grouping')}
      aria-pressed={selected} title={row.id} onClick={() => onSelect(row.id)}>
      <span className="pc-label">{node.label}</span>
      {/* A materialized ancestor is OUR grouping, not something a run said. Saying so is the
          difference between showing a hierarchy and claiming one: nobody tagged `optimization`,
          they tagged `optimization/lr`, and the parent row exists because the id spells it. */}
      {!node.tagged && <span className="pc-tag-note">grouping</span>}
      <span className="pc-counts">
        <span className="pc-runs">{node.runs} run{node.runs === 1 ? '' : 's'}</span>
        {node.tagged && <span className="pc-exp">{node.directExperiments} exp</span>}
        <BestMetric best={node.best} />
      </span>
    </button>
  </li>
}

function ConceptDetail({ forest, id, runsById, onOpenRun, onClose }) {
  const node = id && forest.nodes[id]
  if (!node) return null
  const shown = node.runIds.slice(0, MAX_DETAIL_RUNS)
  return <aside className="pc-detail" aria-label={`Concept ${id}`}>
    <div className="pc-detail-h">
      <code className="pc-detail-id">{id}</code>
      <button type="button" className="btn xs" onClick={onClose}>Close</button>
    </div>
    <dl className="pc-facts">
      <div><dt>Runs</dt><dd>{node.runs}</dd>
        <p className="muted">Distinct runs tagged with this concept or anything below it.</p></div>
      <div><dt>Experiments</dt>
        <dd>{node.tagged ? node.directExperiments : '—'}</dd>
        {/* No subtree experiment total, on purpose: one experiment carries several tags, so summing
            a subtree counts it once per tag (measured on this corpus: 8 experiments, 21 tag pairs).
            There is nothing in the run-list payload that could de-duplicate it. */}
        <p className="muted">{node.tagged
          ? 'Experiments tagged with exactly this id. A subtree total is not shown — one experiment '
            + 'carries several tags, so summing a subtree would count it more than once.'
          : 'No run tagged this id itself; it exists because a deeper id spells it as an ancestor.'}</p>
      </div>
      <div><dt>Best metric</dt>
        <dd>{node.best
          ? `${node.best.direction === 'min' ? '↓' : '↑'} ${node.best.value}`
          : 'not shown'}</dd>
        <p className="muted">{node.best
          ? `Best robust metric below this concept, over ${node.best.runs} run(s) of `
            + `${node.best.taskId} (${node.best.direction}).`
          : 'The runs under this concept do not share one task and one objective direction, or none '
            + 'of them scored. A single number here would compare two different objectives.'}</p>
      </div>
    </dl>
    <h4>Evidence</h4>
    <ul className="pc-detail-runs">
      {shown.map(runId => {
        const run = runsById.get(runId)
        return <li key={runId}>
          <button type="button" className="pc-run-link" onClick={() => onOpenRun(runId)}>
            {runLabel(run) || runId}
          </button>
          <span className="muted">{run?.task_id || 'task unknown'}</span>
        </li>
      })}
    </ul>
    {node.runIds.length > shown.length
      && <p className="muted">+{node.runIds.length - shown.length} more runs not listed.</p>}
  </aside>
}

export default function PortfolioConcepts({
  runs = [], scopeLabel = 'All runs', selectedRuns = [], onOpenRun = () => {}, headingRef = null,
}) {
  const [restrictToSelection, setRestrictToSelection] = useState(false)
  const [expanded, setExpanded] = useState(() => new Set())
  const [selected, setSelected] = useState('')
  const [query, setQuery] = useState('')
  const rowRefs = useRef(new Map())

  const selectedIds = useMemo(() => new Set(selectedRuns.map(run => run.run_id)), [selectedRuns])
  // The selection restriction can only ever NARROW what the list is already showing. A checked run
  // that has since left the scope must not reappear here, or the two surfaces disagree about the
  // population while claiming the same scope.
  const active = useMemo(() => restrictToSelection && selectedIds.size
    ? runs.filter(run => selectedIds.has(run.run_id)) : runs, [restrictToSelection, selectedIds, runs])
  useEffect(() => { if (!selectedIds.size) setRestrictToSelection(false) }, [selectedIds])

  const runsById = useMemo(() => new Map(active.map(run => [run.run_id, run])), [active])
  const forest = useMemo(() => buildConceptForest(active, { runsById }), [active, runsById])
  const coverage = forestCoverage(forest)

  const search = query.trim().toLowerCase()
  // Search OPENS the matching branches rather than filtering the tree down to them. A filtered tree
  // silently drops every ancestor and sibling, which is how "one result" starts reading as "this is
  // all there is". The matches are highlighted where they actually sit.
  const matches = useMemo(() => {
    if (!search) return null
    return new Set(Object.keys(forest.nodes).filter(id => id.includes(search)))
  }, [search, forest])
  const openSet = useMemo(() => {
    if (!matches) return expanded
    const out = new Set(expanded)
    for (const id of matches) for (const step of forestPathTo(id)) out.add(step)
    return out
  }, [matches, expanded])

  const rows = useMemo(() => visibleForestRows(forest, openSet), [forest, openSet])
  const toggle = id => setExpanded(current => {
    const next = new Set(current)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  const expandAll = () => setExpanded(new Set(Object.keys(forest.nodes)))
  const collapseAll = () => { setExpanded(new Set()); setQuery('') }
  useEffect(() => {
    // A concept can leave the scope while its detail panel is open (a filter changed, a run finished
    // and was re-tagged). Drop the selection rather than render a panel about nothing.
    if (selected && !forest.nodes[selected]) setSelected('')
  }, [forest, selected])

  const scopeName = restrictToSelection && selectedIds.size
    ? `${selectedIds.size} selected run${selectedIds.size === 1 ? '' : 's'}` : scopeLabel

  return <div className="pc-stage">
    <div className="pc-head">
      <h2 ref={headingRef} tabIndex={-1}>Concepts · {scopeName}</h2>
      <p className="muted pc-lede">
        Every concept the runs in this scope were tagged with, as the tree their ids spell out.
        This is folded from the runs already on screen — changing a filter or a project changes it.
      </p>
      <div className="pc-controls">
        <div className="seg pc-scope" role="group" aria-label="Concept scope">
          <button type="button" aria-pressed={!restrictToSelection}
            className={restrictToSelection ? '' : 'on'}
            onClick={() => setRestrictToSelection(false)}>
            List scope · {runs.length}
          </button>
          <button type="button" aria-pressed={restrictToSelection}
            className={restrictToSelection ? 'on' : ''} disabled={!selectedIds.size}
            title={selectedIds.size
              ? 'Restrict to the runs checked in List'
              : 'Check runs in List to build a subset'}
            onClick={() => setRestrictToSelection(true)}>
            Selected · {selectedIds.size}
          </button>
        </div>
        <label className="pc-search">
          <span className="sr-only">Find a concept</span>
          <input type="search" value={query} placeholder="find a concept…"
            onChange={event => setQuery(event.target.value.slice(0, 120))} />
        </label>
        <button type="button" className="btn sm" onClick={expandAll}>Expand all</button>
        <button type="button" className="btn sm" onClick={collapseAll}>Collapse all</button>
      </div>
    </div>

    {/* The coverage sentence goes ABOVE the tree, not under it. A tree drawn from 15 of 46 runs is a
        true picture of 15 runs and says nothing about the other 31 — and without this line it reads
        as "the lab has studied 71 things", which is the exact misreading conceptShelf.js was written
        to prevent for memory rows. */}
    {coverage && <div className={'pc-coverage' + (coverage.complete ? ' pc-complete' : '')}
      role="status">
      <b>{coverage.tagged} of {coverage.runs} runs</b> carry concept tags
      {coverage.untagged > 0 && <> · <b>{coverage.untagged} untagged</b></>}
      {' · '}{coverage.concepts} concept{coverage.concepts === 1 ? '' : 's'} across {coverage.roots} root{coverage.roots === 1 ? '' : 's'}
      {coverage.complete && <> · every run in scope is tagged</>}
      {coverage.malformedRuns > 0 && <> · <b>{coverage.droppedIds} unreadable tag(s)</b> in {coverage.malformedRuns} run(s)</>}
    </div>}

    {forest.truncated && <div className="notice resource-warning" role="status">
      This scope carries more concepts than the tree renders. Narrow the scope to see the rest.
    </div>}

    {/* Reported, never merged. Two runs spelling one word differently is evidence the taggers drifted,
        not licence for LoopLab to pick a winner — ConceptView already states that LoopLab does not
        infer a taxonomy, and a global tree that quietly joined these roots would be doing exactly
        that. Renaming them for real is a governed cross-run action, not a render-time guess. */}
    {forest.variants.length > 0 && <details className="pc-variants">
      <summary>{forest.variants.length} concept{forest.variants.length === 1 ? '' : 's'} spelled
        more than one way — shown separately, not merged</summary>
      <ul>
        {forest.variants.map(group => <li key={group.key}>
          {group.ids.map(id => <code key={id}>{id}</code>)}
        </li>)}
      </ul>
      <p className="muted">These differ only in <code>-</code> versus <code>_</code>. LoopLab does not
        infer a taxonomy, so it keeps them apart rather than choosing one for you. Merging them is a
        cross-run concept action in the Atlas, where it is recorded.</p>
    </details>}

    <div className="pc-body">
      <div className="pc-tree-shell">
        {coverage?.empty
          ? <div className="notice resource-empty">
              No run in this scope carries a concept tag.
              {coverage.runs > 0 && <> All {coverage.runs} of them ran; none was tagged, which is a
                fact about tagging and not about what was learned.</>}
            </div>
          : <ul className="pc-tree" aria-label="Concept tree">
              {rows.map(row => <ConceptRow key={row.id} row={row}
                selected={selected === row.id}
                expanded={openSet.has(row.id)}
                onToggle={toggle} onSelect={setSelected}
                setRef={node => node
                  ? rowRefs.current.set(row.id, node) : rowRefs.current.delete(row.id)} />)}
            </ul>}
        {search && matches?.size === 0
          && <div className="notice" role="status">No concept id contains “{search}”.</div>}

        {/* Always rendered, even at zero. Its absence would read as "everything is tagged", and the
            count being zero is exactly the signal that says this tree covers the whole scope —
            conceptShelf.js's rule for memory rows, which is the same rule for runs. */}
        <div className="pc-untagged">
          <b>Untagged</b>
          <span>{forest.untagged.runs} run{forest.untagged.runs === 1 ? '' : 's'} in this scope carry
            no concept at all.</span>
          {forest.untagged.runs > 0 && <span className="muted">They are not in the tree above. That is
            a gap in tagging, not evidence that nothing was learned in them.</span>}
        </div>
      </div>

      {selected
        ? <ConceptDetail forest={forest} id={selected} runsById={runsById}
            onOpenRun={onOpenRun} onClose={() => setSelected('')} />
        : <aside className="pc-detail pc-detail-idle">
            <p className="muted">Pick a concept to see which runs are evidence for it.</p>
          </aside>}
    </div>
  </div>
}
