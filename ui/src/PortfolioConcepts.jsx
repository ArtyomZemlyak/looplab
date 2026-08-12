import React, { useEffect, useMemo, useRef, useState } from 'react'
import { OpIcon } from './icons.jsx'
import {
  applyConceptPolicy, buildConceptCooccurrence, buildConceptForest, conceptScopeClaim,
  forestCoverage, forestPathTo,
  partnersOf, visibleForestRows,
} from './conceptForest.js'
import { conceptMemory, conceptMemoryNotice } from './conceptMemoryModel.js'
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
const MAX_DETAIL_PARTNERS = 12
const MAX_PAIR_ROWS = 12
// The co-occurrence floor. 2 is the default everywhere (`search/concept_lens.py::project_concept_map`
// ships the same number) because one run tagging `a` and `b` is a fact about that run's tagger.
// Dropping to 1 is offered because the honest answer on a young corpus is often "nothing repeats yet",
// and an operator has to be able to see the single-run pairings that lie behind that sentence.
const PAIR_FLOORS = [
  { value: 2, label: '2+ runs', hint: 'Pairs two or more runs studied together' },
  { value: 1, label: 'any run', hint: 'Include pairs only one run named — a coincidence, not a pattern' },
]

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

// The concepts this one was studied ALONGSIDE — the map's one relation that the id itself cannot
// spell. `is_a` is the tree; a run naming two ids together is the only evidence of anything else, and
// the floor is what keeps a single run's two labels from reading as a pattern.
function Partners({ cooccurrence, node }) {
  const partners = node.tagged ? partnersOf(cooccurrence, node.id) : []
  return <>
    <h4>Studied alongside</h4>
    {!node.tagged
      ? <p className="muted">This row is a grouping — no run named it, so nothing was named beside it.
          Open a concept below it.</p>
      : partners.length === 0
        ? <p className="muted">No concept appeared with this one in {cooccurrence.minRuns}+ runs of this
            scope. That is a statement about repetition, not about whether the pairing is interesting.</p>
        : <ul className="pc-partners">
            {partners.slice(0, MAX_DETAIL_PARTNERS).map(partner => <li key={partner.id}>
              <code>{partner.id}</code>
              <span className="muted">{partner.runs} run{partner.runs === 1 ? '' : 's'}</span>
            </li>)}
          </ul>}
    {/* A pruned node's pairs were never counted. Saying "no partners" for it would turn a bound into
        a finding, which is the same rule the tree's truncation notice follows. */}
    {node.tagged && cooccurrence.pairsOutsideProjectionUnknown
      && <p className="muted">Pairs touching {cooccurrence.pairSourceNodesPruned} less-used concept(s)
        were not computed, so this list may be incomplete.</p>}
  </>
}

// What the lab LEARNED about this concept — the lessons, cases and notes that carry it, straight
// here rather than one screen away. Subtree matching is `conceptShelf`'s, so `loss` answers with
// everything under `loss/contrastive/…`; two definitions of "about this concept" would drift.
function _ConceptMemory({ memory, id }) {
  const result = useMemo(() => conceptMemory(memory, id), [memory, id])
  if (memory === null) return <><h4>What the lab learned</h4>
    <p className="muted" role="status">loading cross-run memory…</p></>
  const notice = conceptMemoryNotice(result)
  return <>
    <h4>What the lab learned</h4>
    {result.groups.map(group => <div key={group.key} className="pc-memory-group">
      <div className="section-h">{group.label} · {group.rows.length}</div>
      <ul className="pc-detail-runs">
        {group.rows.slice(0, MAX_DETAIL_RUNS).map((row, index) => <li key={`${group.key}-${index}`}>
          <span>{String(row._text || '').slice(0, 220) || '(no text)'}</span>
          <span className="muted">{row.task_id || 'task unknown'}</span>
        </li>)}
      </ul>
      {group.rows.length > MAX_DETAIL_RUNS
        && <p className="muted">+{group.rows.length - MAX_DETAIL_RUNS} more not listed.</p>}
    </div>)}
    {notice && <p className="muted" role="status">{notice}</p>}
  </>
}

function ConceptDetail({ forest, cooccurrence, id, runsById, onOpenRun, onClose, memory = null }) {
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
    <_ConceptMemory memory={memory} id={id} />
    <Partners cooccurrence={cooccurrence} node={node} />
  </aside>
}

export default function PortfolioConcepts({
  runs = [], scopeLabel = 'All runs', selectedRuns = [], onOpenRun = () => {}, headingRef = null,
}) {
  const [restrictToSelection, setRestrictToSelection] = useState(false)
  const [expanded, setExpanded] = useState(() => new Set())
  const [selected, setSelected] = useState('')
  const [query, setQuery] = useState('')
  const [pairFloor, setPairFloor] = useState(PAIR_FLOORS[0].value)
  const [policyState, setPolicyState] = useState({ status: 'loading', policy: null })
  // WHAT THE LAB LEARNED, not just which runs touched it. The Memory panel already indexes lessons,
  // cases and notes by these very concept ids; this view could only ever answer "which runs", and the
  // path between the two did not exist. One bounded read, shared by every concept the operator clicks.
  const [memory, setMemory] = useState(null)
  const rowRefs = useRef(new Map())

  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/cross-run/concept-policy', { signal: controller.signal })
      .then(response => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then(policy => setPolicyState({ status: 'ready', policy }))
      .catch(error => {
        if (error?.name !== 'AbortError') setPolicyState({ status: 'unavailable', policy: null })
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/memory', { signal: controller.signal })
      .then(response => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`))))
      .then(payload => setMemory(payload))
      .catch(error => { if (error?.name !== 'AbortError') setMemory({}) })
    return () => controller.abort()
  }, [])

  const selectedIds = useMemo(() => new Set(selectedRuns.map(run => run.run_id)), [selectedRuns])
  // The selection restriction can only ever NARROW what the list is already showing. A checked run
  // that has since left the scope must not reappear here, or the two surfaces disagree about the
  // population while claiming the same scope.
  const activeRaw = useMemo(() => restrictToSelection && selectedIds.size
    ? runs.filter(run => selectedIds.has(run.run_id)) : runs, [restrictToSelection, selectedIds, runs])
  const governance = useMemo(
    () => applyConceptPolicy(activeRaw, policyState.policy), [activeRaw, policyState.policy])
  const active = governance.runs
  useEffect(() => { if (!selectedIds.size) setRestrictToSelection(false) }, [selectedIds])

  const runsById = useMemo(() => new Map(active.map(run => [run.run_id, run])), [active])
  const forest = useMemo(() => buildConceptForest(active, { runsById }), [active, runsById])
  const coverage = forestCoverage(forest)
  // `active`, not `runs` — the SAME array the tree above is folded from, so the pairs and the tree
  // can never describe different populations. This is also why co-occurrence is not fetched: the
  // server's cross-run corpus is the capsule ledger, whose membership is not this list's.
  const cooccurrence = useMemo(
    () => buildConceptCooccurrence(active, { minRuns: pairFloor, forest }),
    [active, pairFloor, forest])

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

  // The heading names the population the TREE is folded from, which is `active` — not the number of
  // boxes ticked in List. The rule is in the model (`conceptScopeClaim`) because this state is behind
  // a click and a rule only a click can reach is a rule no test drives.
  const { name: scopeName, outOfScope: selectedOutOfScope } = conceptScopeClaim({
    scopeLabel, restrictToSelection, selectedCount: selectedIds.size, activeCount: active.length })

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

    {/* Checked in List, then filtered out of it. The tree already excludes them — the disagreement
        was only ever in the words, and silently dropping them is how "the runs I picked" and "the
        runs this tree describes" become two different sets nobody mentions. */}
    {selectedOutOfScope > 0 && <p className="muted pc-scope-note" role="status">
      {selectedOutOfScope} checked run(s) are outside the current list scope and are not in this
      tree. Clear the list filters to include them.</p>}

    {policyState.status !== 'ready' && <div className="notice resource-warning" role="status">
      Concept governance is {policyState.status === 'loading' ? 'loading' : 'unavailable'}; this tree
      is temporarily showing raw run-authored ids, so governed merges and purges may be unapplied.
    </div>}
    {policyState.status === 'ready' && !governance.complete
      && <div className="notice resource-warning" role="status">
        Concept governance is partially applied.
        {governance.unappliedSplits > 0 && <> {governance.unappliedSplits} split-dependent tag(s) remain raw.</>}
        {governance.invalidTargets > 0 && <> {governance.invalidTargets} invalid policy/tag target(s) were omitted.</>}
      </div>}
    {policyState.status === 'ready' && governance.capsuleCoverageKnown
      && (governance.unrepresentedRuns > 0 || governance.capsuleIdsOmitted > 0)
      && <div className="notice resource-warning" role="status">
        Durable cross-run concept memory does not cover this whole list: {governance.unrepresentedRuns}
        visible run(s) have no retained capsule
        {governance.capsuleIdsOmitted > 0 && <>; {governance.capsuleIdsOmitted} capsule id(s) were omitted from the policy receipt</>}.
        The tree still shows their run-authored tags, but Atlas and agent priors may use a smaller population.
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
      {/* Remaining variants are the residue after the governed alias/purge lookup. Split-dependent
          ids stay raw and are disclosed above because resolving them needs a server-side sibling-aware
          projection, not a browser guess. */}
      <p className="muted">These differ only in <code>-</code> versus <code>_</code>. LoopLab does not
        infer a taxonomy, so it keeps ungoverned variants apart. A governed merge
        (<code>looplab concept-merge</code>) is applied here once the revisioned policy is available.</p>
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

        {/* The tree is the `is_a` relation — a concept id spells its own ancestry, so nesting is
            RECOVERED and never inferred. Co-occurrence is the one relation no id can state about
            itself, and it is folded from the same rows: a pair is two concepts one run was tagged
            with, counted over DISTINCT runs. This section replaces what the removed
            `portfolio_concept_graph` used to compute over the capsule ledger — same rule, but over
            the population this view is actually showing. */}
        {!coverage?.empty && <section className="pc-pairs" aria-label="Concept co-occurrence">
          <div className="pc-pairs-h">
            <h3>Studied together</h3>
            <div className="seg pc-floor" role="group" aria-label="Co-occurrence threshold">
              {PAIR_FLOORS.map(floor => <button key={floor.value} type="button"
                aria-pressed={pairFloor === floor.value}
                className={pairFloor === floor.value ? 'on' : ''} title={floor.hint}
                onClick={() => setPairFloor(floor.value)}>{floor.label}</button>)}
            </div>
          </div>
          {cooccurrence.pairs.length === 0
            ? <p className="muted">
                {cooccurrence.pairCandidates === 0
                  ? 'No run in this scope was tagged with two concepts, so there is nothing to pair.'
                  : `No pair of concepts appeared together in ${cooccurrence.minRuns} or more runs. `
                    + `${cooccurrence.pairCandidates} pairing(s) were seen in a single run — a tagger `
                    + 'emitting two labels once, which is not yet evidence of a pattern.'}
              </p>
            : <>
                <ol className="pc-pair-list">
                  {cooccurrence.pairs.slice(0, MAX_PAIR_ROWS).map(pair => <li key={`${pair.a} ${pair.b}`}>
                    <button type="button" className="pc-pair" onClick={() => setSelected(pair.a)}>
                      <code>{pair.a}</code>
                    </button>
                    <span aria-hidden="true">+</span>
                    <button type="button" className="pc-pair" onClick={() => setSelected(pair.b)}>
                      <code>{pair.b}</code>
                    </button>
                    <span className="pc-pair-n">{pair.runs} run{pair.runs === 1 ? '' : 's'}</span>
                  </li>)}
                </ol>
                {cooccurrence.pairs.length > MAX_PAIR_ROWS
                  && <p className="muted">+{cooccurrence.pairs.length - MAX_PAIR_ROWS} more pair(s)
                    above the threshold, not listed.</p>}
              </>}
          {/* The RANKING cap, which is a different absence from both of the two below and was the
              only one with no words. `pairsOmitted` is pairs that reached the floor, were counted,
              and then fell off the end of the ranked list — so unlike a pruned node's pairs their
              count is EXACT, and unlike the row cap above it is measured against the whole ranked
              set rather than the twelve rows shown. Reachable without a large corpus: the server
              caps ONE run at MAX_ROLLUP_CONCEPTS = 64 ids, C(64,2) = 2,016, and the `any run` floor
              beside this heading is right there — so a single wide-tagged run already spills, and
              `partnersOf` then returns a SHORT list for the concepts that fell off while the detail
              pane says "no concept appeared with this one". */}
          {cooccurrence.pairsOmitted > 0 && <p className="muted">
            {cooccurrence.pairsOmitted} further pair(s) reached this threshold and were counted, but
            fall outside the ranked set this view keeps. They are missing from the list above and
            from the partners of any concept they touch.</p>}
          {/* Two different absences, never merged. The cap above is exact — we counted them and showed
              fewer. A pruned node's pairs were never materialized, so their count is UNKNOWN, and
              printing it as zero would present a bound as a finding. */}
          {cooccurrence.pairsOutsideProjectionUnknown && <p className="muted">
            Only the {cooccurrence.pairSourceNodesIncluded} most-used concepts were paired;
            pairs touching the other {cooccurrence.pairSourceNodesPruned} are unknown, not absent.</p>}
        </section>}
      </div>

      {selected
        ? <ConceptDetail forest={forest} cooccurrence={cooccurrence} id={selected} runsById={runsById}
            onOpenRun={onOpenRun} onClose={() => setSelected('')} memory={memory} />
        : <aside className="pc-detail pc-detail-idle">
            <p className="muted">Pick a concept to see which runs are evidence for it, and what the lab has learned about it.</p>
          </aside>}
    </div>
  </div>
}
