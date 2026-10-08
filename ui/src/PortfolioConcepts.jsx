import { uiText, uiMessage, uiPlural, useUILanguage } from './uiLanguage.js'
import React, { useEffect, useMemo, useRef, useState } from 'react'
import { OpIcon } from './icons.jsx'
import {
  applyConceptPolicy, buildConceptCooccurrence, buildConceptForest, conceptScopeClaim,
  forestCoverage, forestPathTo,
  partnersOf, visibleForestRows,
} from './conceptForest.js'
import { conceptMemory, conceptMemoryNotice } from './conceptMemoryModel.js'
import { get } from './api.js'
import './portfolio-concepts.css'
import ConceptEffect from './ConceptEffect.jsx'

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
  useUILanguage()

  if (!best) return null
  const value = Number.isInteger(best.value) ? String(best.value) : best.value.toFixed(4)
  return <span className="pc-metric"
    title={(best.direction === 'min'
      ? uiPlural(best.runs, 'best lowest robust metric across {0} run(s) of task {1}', 'best lowest robust metric across {0} run(s) of task {1}', [best.runs, best.taskId])
      : uiPlural(best.runs, 'best highest robust metric across {0} run(s) of task {1}', 'best highest robust metric across {0} run(s) of task {1}', [best.runs, best.taskId]))
      + (best.objective ? uiMessage(" — ranked by {0}, an operator retarget, not the task's own metric", [best.objective]) : '')}>
    {best.direction === 'min' ? '↓' : '↑'} {value}{best.objective ? ` (${best.objective})` : ''}
  </span>
}

function ConceptRow({ row, selected, matched, expanded, forcedOpen, onToggle, onSelect, setRef }) {
  useUILanguage()

  const node = row.node
  return <li className={'pc-row' + (row.depth === 0 ? ' pc-root' : '')}
    style={{ paddingLeft: 4 + row.depth * 17 }}>
    {row.hasChildren
      ? <button type="button" className="pc-twist" aria-expanded={expanded}
          disabled={forcedOpen} title={((forcedOpen ? uiText('Clear search to collapse this path') : undefined))}
          aria-label={`${expanded ? 'Collapse' : 'Expand'} ${row.id}`}
          onClick={() => onToggle(row.id)}>{expanded ? '▾' : '▸'}</button>
      : <span className="pc-twist pc-twist-leaf" aria-hidden="true">·</span>}
    <button ref={setRef} type="button" data-concept-id={row.id}
      className={'pc-name' + (selected ? ' on' : '') + (matched ? ' pc-match' : '')
        + (node.tagged ? '' : ' pc-grouping')}
      aria-pressed={selected} title={row.id} onClick={() => onSelect(row.id)}>
      <span className="pc-label">{uiText(node.label)}</span>
      {/* A materialized ancestor is OUR grouping, not something a run said. Saying so is the
          difference between showing a hierarchy and claiming one: nobody tagged `optimization`,
          they tagged `optimization/lr`, and the parent row exists because the id spells it. */}
      {!node.tagged && <span className="pc-tag-note">{uiText("grouping")}</span>}
      <span className="pc-counts">
        <span className="pc-runs">{uiPlural(node.runs, '{0} run', '{0} runs')}</span>
        {node.tagged && <span className="pc-exp">{node.directExperiments}{uiText(" exp")}</span>}
        <BestMetric best={node.best} />
      </span>
    </button>
  </li>
}

// The concepts this one was studied ALONGSIDE — the map's one relation that the id itself cannot
// spell. `is_a` is the tree; a run naming two ids together is the only evidence of anything else, and
// the floor is what keeps a single run's two labels from reading as a pattern.
function Partners({ cooccurrence, node }) {
  useUILanguage()

  const partners = node.tagged ? partnersOf(cooccurrence, node.id) : []
  return <>
    <h4>{uiText("Studied alongside")}</h4>
    {!node.tagged
      ? <p className="muted">{uiText("This row is a grouping — no run named it, so nothing was named beside it. Open a concept below it.")}</p>
      : partners.length === 0
        ? <p className="muted">{uiText("No concept appeared with this one in ")}{cooccurrence.minRuns}{uiText("+ runs of this scope. That is a statement about repetition, not about whether the pairing is interesting.")}</p>
        : <ul className="pc-partners">
            {partners.slice(0, MAX_DETAIL_PARTNERS).map(partner => <li key={partner.id}>
              <code>{partner.id}</code>
              <span className="muted">{uiPlural(partner.runs, '{0} run', '{0} runs')}</span>
            </li>)}
          </ul>}
    {/* A pruned node's pairs were never counted. Saying "no partners" for it would turn a bound into
        a finding, which is the same rule the tree's truncation notice follows. */}
    {node.tagged && cooccurrence.pairsOutsideProjectionUnknown
      && <p className="muted">{uiPlural(cooccurrence.pairSourceNodesPruned, 'Pairs touching {0} less-used concept(s) were not computed, so this list may be incomplete.', 'Pairs touching {0} less-used concept(s) were not computed, so this list may be incomplete.')}</p>}
  </>
}

// What the lab LEARNED about this concept — the lessons, cases and notes that carry it, straight
// here rather than one screen away. Subtree matching is `conceptShelf`'s, so `loss` answers with
// everything under `loss/contrastive/…`; two definitions of "about this concept" would drift.
function _ConceptMemory({ memory, id, onRetry = null }) {
  const result = useMemo(() => conceptMemory(memory, id), [memory, id])
  if (memory === null) return <><h4>{uiText("What the lab learned")}</h4>
    <p className="muted" role="status">{uiText("loading cross-run memory…")}</p></>
  // Unreadable is its own answer, and must never be spelled as "empty" — see the fetch's `.catch`.
  if (memory?.unavailable) return <><h4>{uiText("What the lab learned")}</h4>
    <p className="muted" role="status">{uiText("Cross-run memory could not be read, so what the lab learned about this concept is unknown — this is not a statement that it learned nothing.")}</p>
    {onRetry && <button type="button" className="btn sm ghost" onClick={onRetry}>{uiText("Try again")}</button>}</>
  const notice = conceptMemoryNotice(result)
  return <>
    <h4>{uiText("What the lab learned")}</h4>
    {result.groups.map(group => <div key={group.key} className="pc-memory-group">
      <div className="section-h">{uiText(group.label)} · {group.rows.length}</div>
      <ul className="pc-detail-runs">
        {group.rows.slice(0, MAX_DETAIL_RUNS).map((row, index) => <li key={`${group.key}-${index}`}>
          <span>{((String(row._text || '').slice(0, 220) || uiText('(no text)')))}</span>
          <span className="muted">{((row.task_id || uiText('task unknown')))}</span>
        </li>)}
      </ul>
      {group.rows.length > MAX_DETAIL_RUNS
        && <p className="muted">+{group.rows.length - MAX_DETAIL_RUNS}{uiText(" more not listed.")}</p>}
    </div>)}
    {notice && <p className="muted" role="status">{uiText(notice)}</p>}
  </>
}

export function ConceptDetail({ forest, cooccurrence, id, runsById, onOpenRun, onClose, detailRef,
  memory = null, onMemoryRetry = null }) {
  useUILanguage()

  const node = id && forest.nodes[id]
  if (!node) return null
  const shown = node.runIds.slice(0, MAX_DETAIL_RUNS)
  return <aside ref={detailRef} tabIndex={-1} className="pc-detail" aria-label={uiMessage("Concept {0}", [id])}>
    <div className="pc-detail-h">
      <code className="pc-detail-id">{id}</code>
      <button type="button" className="btn xs" onClick={onClose}>{uiText("Back to map")}</button>
    </div>
    <dl className="pc-facts">
      <div><dt>{uiText("Runs")}</dt><dd>{node.runs}</dd><small>{uiText("this branch")}</small></div>
      <div><dt>{uiText("Experiments")}</dt>
        <dd>{node.tagged ? node.directExperiments : '—'}</dd>
        <small>{((node.tagged ? uiText('exact tag') : uiText('grouping only')))}</small>
      </div>
      <div><dt>{uiText("Best metric")}</dt>
        <dd>{((node.best ? `${node.best.direction === 'min' ? '↓' : '↑'} ${node.best.value}` : uiText('not shown')))}</dd>
        {node.best && <small>{`${node.best.taskId} · ${node.best.direction}`
          + (node.best.objective ? ` · ranked by ${node.best.objective}` : '')}</small>}
      </div>
    </dl>
    <section aria-label={uiText('Concept contribution')}>
      <h4>{uiText('Concept contribution · with / without')}</h4>
      <p className="muted">{uiText('Within each run, compare experiments with and without this concept while matching the remaining concepts and evaluation conditions. Effects from different objectives are not averaged.')}</p>
      {shown.map(runId => <div key={runId}><code>{runId}</code>{' · '}
        <ConceptEffect effect={node.effects?.get(runId)} /></div>)}
    </section>
    {!node.best && <p className="muted pc-fact-warning">{uiText("The runs do not share one task and objective direction, or none scored. A single metric would compare different objectives.")}</p>}
    <details className="pc-method">
      <summary>{uiText("How these counts are defined")}</summary>
      <p>{uiText("Runs count distinct runs tagged with this concept or anything below it.")}</p>
      <p>{((node.tagged ? uiText('Experiments count only this exact tag. A subtree total would count experiments with several tags more than once.') : uiText('No run tagged this id itself; a deeper id spells it as an ancestor.')))}</p>
      {node.best && <p>{uiPlural(node.best.runs, 'Best robust metric below this concept, over {0} run(s) of {1} ({2})', 'Best robust metric below this concept, over {0} run(s) of {1} ({2})', [node.best.runs, node.best.taskId, node.best.direction])
        + (node.best.objective ? uiMessage(' — ranked by {0}, an operator retarget, not the task’s own metric.', [node.best.objective]) : '.')}</p>}
    </details>
    <h4>{uiText("Evidence")}</h4>
    <ul className="pc-detail-runs">
      {shown.map(runId => {
        const run = runsById.get(runId)
        return <li key={runId}>
          <button type="button" className="pc-run-link" onClick={() => onOpenRun(runId)}>
            {runLabel(run) || runId}
          </button>
          <span className="muted">{((run?.task_id || uiText('task unknown')))}</span>
        </li>
      })}
    </ul>
    {node.runIds.length > shown.length
      && <p className="muted">{uiPlural(node.runIds.length - shown.length, '+{0} more runs not listed.', '+{0} more runs not listed.')}</p>}
    <_ConceptMemory memory={memory} id={id} onRetry={onMemoryRetry} />
    <Partners cooccurrence={cooccurrence} node={node} />
  </aside>
}

export default function PortfolioConcepts({
  runs = [], scopeLabel = 'All runs', selectedRuns = [], onOpenRun = () => {}, headingRef = null,
}) {
  useUILanguage()

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
  const [memoryNonce, setMemoryNonce] = useState(0)
  const rowRefs = useRef(new Map())
  const treeRef = useRef(null)
  const detailRef = useRef(null)

  useEffect(() => {
    const controller = new AbortController()
    // `get`, not a bare `fetch`: a root-relative `/api/...` misses the deployment's path prefix
    // (this UI is served under `/user/<name>/proxy/<port>/` on JupyterHub) and carries no owner
    // token, so both reads 404 or 401 on exactly the deployment the operator uses.
    get('/api/cross-run/concept-policy', { signal: controller.signal })
      .then(policy => setPolicyState({ status: 'ready', policy }))
      .catch(error => {
        if (error?.name !== 'AbortError') setPolicyState({ status: 'unavailable', policy: null })
      })
    return () => controller.abort()
  }, [])

  // LAZY, and only once a concept is actually selected. This downloads the whole bounded cross-run
  // memory window — multi-MiB worst case per tier — and it is read by ONE panel inside the detail
  // pane, so fetching it on mount made every List<->Concepts flip re-pay for it before the operator
  // clicked anything. `memoryNonce` is the retry: a failed read used to be permanent for the life of
  // the view, so one transient 503 showed "could not be read" until the operator left and came back.
  useEffect(() => {
    if (!selected) return undefined
    if (memory !== null && !memory?.unavailable) return undefined
    const controller = new AbortController()
    setMemory(null)
    get('/api/memory', { signal: controller.signal })
      .then(payload => setMemory(payload))
      // A FAILED read is not an empty store. `{}` folds to `total: 0`, which `conceptMemoryNotice`
      // reports as "Cross-run memory is empty, so nothing can be linked yet." — a positive claim
      // that no lesson, case or note in the whole shared memory dir carries this concept, made about
      // a read that never landed (revoked token, restarted server, slow store). The sibling policy
      // read above already keeps a distinct `unavailable` state for exactly this reason.
      .catch(error => { if (error?.name !== 'AbortError') setMemory({ unavailable: true }) })
    return () => controller.abort()
  }, [selected, memoryNonce])

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
  const forcedOpen = useMemo(() => {
    const out = new Set()
    if (matches) for (const id of matches) for (const step of forestPathTo(id)) out.add(step)
    return out
  }, [matches])
  const openSet = useMemo(() => matches ? new Set([...expanded, ...forcedOpen]) : expanded,
    [matches, expanded, forcedOpen])

  const rows = useMemo(() => visibleForestRows(forest, openSet), [forest, openSet])
  const firstMatch = search ? rows.find(row => matches?.has(row.id))?.id : null
  const toggle = id => setExpanded(current => {
    const next = new Set(current)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  const expandAll = () => { setExpanded(new Set(Object.keys(forest.nodes))); setSelected('') }
  const collapseAll = () => { setExpanded(new Set()); setQuery(''); setSelected('') }
  useEffect(() => {
    // A concept can leave the scope while its detail panel is open (a filter changed, a run finished
    // and was re-tagged). Drop the selection rather than render a panel about nothing.
    if (selected && !forest.nodes[selected]) setSelected('')
  }, [forest, selected])
  useEffect(() => {
    if (!selected) return undefined
    const frame = requestAnimationFrame(() => detailRef.current?.focus({ preventScroll: true }))
    return () => cancelAnimationFrame(frame)
  }, [selected])
  const closeDetail = () => {
    const previous = selected
    setSelected('')
    requestAnimationFrame(() => (rowRefs.current.get(previous) || treeRef.current)?.focus())
  }

  // The heading names the population the TREE is folded from, which is `active` — not the number of
  // boxes ticked in List. The rule is in the model (`conceptScopeClaim`) because this state is behind
  // a click and a rule only a click can reach is a rule no test drives.
  const { name: scopeName, outOfScope: selectedOutOfScope } = conceptScopeClaim({
    scopeLabel, restrictToSelection, selectedCount: selectedIds.size, activeCount: active.length })

  return <div className="pc-stage">
    <div className="pc-head">
      <h2 ref={headingRef} tabIndex={-1}>{uiText("Concepts · ")}{scopeName}</h2>
      <p className="muted pc-lede">{uiText("Every concept the runs in this scope were tagged with, as the tree their ids spell out. This is folded from the runs already on screen — changing a filter or a project changes it.")}</p>
      <div className="pc-controls">
        <div className="seg pc-scope" role="group" aria-label={uiText("Concept scope")}>
          <button type="button" aria-pressed={!restrictToSelection}
            className={restrictToSelection ? '' : 'on'}
            onClick={() => setRestrictToSelection(false)}>{uiText("List scope · ")}{runs.length}
          </button>
          <button type="button" aria-pressed={restrictToSelection}
            className={restrictToSelection ? 'on' : ''} disabled={!selectedIds.size}
            title={((selectedIds.size ? uiText('Restrict to the runs checked in List') : uiText('Check runs in List to build a subset')))}
            onClick={() => setRestrictToSelection(true)}>{uiText("Selected · ")}{selectedIds.size}
          </button>
        </div>
        <label className="pc-search">
          <span className="sr-only">{uiText("Find a concept")}</span>
          <input type="search" value={query} placeholder={uiText("find a concept…")}
            onChange={event => {
              setQuery(event.target.value.slice(0, 120))
              setSelected('')
            }}
            onKeyDown={event => {
              if (event.key === 'Escape') setQuery('')
              if (event.key === 'Enter' && firstMatch) {
                event.preventDefault()
                setSelected('')
                requestAnimationFrame(() => rowRefs.current.get(firstMatch)?.focus())
              }
            }} />
        </label>
        <button type="button" className="btn sm" onClick={expandAll}>{uiText("Expand all")}</button>
        <button type="button" className="btn sm" onClick={collapseAll}>{uiText("Collapse all")}</button>
      </div>
      {search && <p className="pc-search-result" role="status">
        {uiPlural(matches.size, '{0} matching concept highlighted in the tree.', '{0} matching concepts highlighted in the tree.')}{((matches.size > 0 && uiText(' Matching paths stay open until search is cleared. Press Enter to focus the first match.')))}
      </p>}
    </div>

    {/* The coverage sentence goes ABOVE the tree, not under it. A tree drawn from 15 of 46 runs is a
        true picture of 15 runs and says nothing about the other 31 — and without this line it reads
        as "the lab has studied 71 things", which is the exact misreading conceptShelf.js was written
        to prevent for memory rows. */}
    {coverage && <div className={'pc-coverage' + (coverage.complete ? ' pc-complete' : '')}
      role="status">
      <b>{uiPlural(coverage.runs, '{0} of {1} runs', '{0} of {1} runs', [coverage.tagged, coverage.runs])}</b>{uiText(" carry concept tags")}{coverage.untagged > 0 && <> · <b>{uiPlural(coverage.untagged, '{0} untagged', '{0} untagged')}</b></>}
      {' · '}{uiPlural(coverage.concepts, '{0} concept', '{0} concepts')} {uiPlural(coverage.roots, 'across {0} root', 'across {0} roots')}
      {coverage.complete && <>{uiText(" · every run in scope is tagged")}</>}
      {coverage.malformedRuns > 0 && <> · <b>{uiPlural(coverage.droppedIds, '{0} unreadable tag(s)', '{0} unreadable tag(s)')}</b>{uiPlural(coverage.malformedRuns, ' in {0} run(s)', ' in {0} run(s)')}</>}
    </div>}

    {/* Checked in List, then filtered out of it. The tree already excludes them — the disagreement
        was only ever in the words, and silently dropping them is how "the runs I picked" and "the
        runs this tree describes" become two different sets nobody mentions. */}
    {selectedOutOfScope > 0 && <p className="muted pc-scope-note" role="status">
      {uiPlural(selectedOutOfScope, '{0} checked run(s) are outside the current list scope and are not in this tree. Clear the list filters to include them.', '{0} checked run(s) are outside the current list scope and are not in this tree. Clear the list filters to include them.')}</p>}

    {policyState.status !== 'ready' && <div className="notice resource-warning" role="status">{uiText("Concept governance is ")}{((policyState.status === 'loading' ? uiText('loading') : uiText('unavailable')))}{uiText("; this tree is temporarily showing raw run-authored ids, so governed merges and purges may be unapplied.")}</div>}
    {policyState.status === 'ready' && !governance.complete
      && <div className="notice resource-warning" role="status">{uiText("Concept governance is partially applied.")}{governance.unappliedSplits > 0 && <> {uiPlural(governance.unappliedSplits, '{0} split-dependent tag(s) remain raw.', '{0} split-dependent tag(s) remain raw.')}</>}
        {governance.invalidTargets > 0 && <> {uiPlural(governance.invalidTargets, '{0} invalid policy/tag target(s) were omitted.', '{0} invalid policy/tag target(s) were omitted.')}</>}
      </div>}
    {policyState.status === 'ready' && governance.capsuleCoverageKnown
      && (governance.unrepresentedRuns > 0 || governance.capsuleIdsOmitted > 0)
      && <div className="notice resource-warning" role="status">{uiText("Durable cross-run concept memory does not cover this whole list: ")}{uiPlural(governance.unrepresentedRuns, '{0} visible run(s) have no retained capsule', '{0} visible run(s) have no retained capsule')}{governance.capsuleIdsOmitted > 0 && <>; {uiPlural(governance.capsuleIdsOmitted, '{0} capsule id(s) were omitted from the policy receipt', '{0} capsule id(s) were omitted from the policy receipt')}</>}{uiText(". The tree still shows their run-authored tags, but the agent priors may use a smaller population.")}</div>}

    {forest.truncated && <div className="notice resource-warning" role="status">{uiText("This scope carries more concepts than the tree renders. Narrow the scope to see the rest.")}</div>}

    {/* Reported, never merged. Two runs spelling one word differently is evidence the taggers drifted,
        not licence for LoopLab to pick a winner — ConceptView already states that LoopLab does not
        infer a taxonomy, and a global tree that quietly joined these roots would be doing exactly
        that. Renaming them for real is a governed cross-run action, not a render-time guess. */}
    {forest.variants.length > 0 && <details className="pc-variants">
      <summary>{uiPlural(forest.variants.length, '{0} concept spelled more than one way — shown separately, not merged', '{0} concepts spelled more than one way — shown separately, not merged')}</summary>
      <ul>
        {forest.variants.map(group => <li key={group.key}>
          {group.ids.map(id => <code key={id}>{id}</code>)}
        </li>)}
      </ul>
      {/* Remaining variants are the residue after the governed alias/purge lookup. Split-dependent
          ids stay raw and are disclosed above because resolving them needs a server-side sibling-aware
          projection, not a browser guess. */}
      <p className="muted">{uiText("These differ only in ")}<code>-</code>{uiText(" versus ")}<code>_</code>{uiText(". LoopLab does not infer a taxonomy, so it keeps ungoverned variants apart. A governed merge (")}<code>{"looplab concept-merge"}</code>{uiText(") is applied here once the revisioned policy is available.")}</p>
    </details>}

    <div className={'pc-body' + (selected ? ' pc-has-selection' : '')}>
      <div className="pc-tree-shell">
        {coverage?.empty
          ? <div className="notice resource-empty">{uiText("No run in this scope carries a concept tag.")}{coverage.runs > 0 && <>{uiPlural(coverage.runs, ' All {0} of them ran; none was tagged, which is a fact about tagging and not about what was learned.', ' All {0} of them ran; none was tagged, which is a fact about tagging and not about what was learned.')}</>}
            </div>
          : <ul ref={treeRef} tabIndex={-1} className="pc-tree" aria-label={uiText("Concept tree")}>
              {rows.map(row => <ConceptRow key={row.id} row={row}
                selected={selected === row.id}
                matched={!!matches?.has(row.id)}
                expanded={openSet.has(row.id)}
                forcedOpen={forcedOpen.has(row.id)}
                onToggle={toggle} onSelect={setSelected}
                setRef={node => node
                  ? rowRefs.current.set(row.id, node) : rowRefs.current.delete(row.id)} />)}
            </ul>}
        {search && matches?.size === 0
          && <div className="notice" role="status">{uiText("No concept id contains “")}{search}”.</div>}
      </div>

      {selected
        ? <ConceptDetail forest={forest} cooccurrence={cooccurrence} id={selected} runsById={runsById}
            onOpenRun={onOpenRun} onClose={closeDetail} detailRef={detailRef} memory={memory}
            onMemoryRetry={() => setMemoryNonce(n => n + 1)} />
        : <aside className="pc-detail pc-detail-idle">
            <p className="muted">{uiText("Pick a concept to see which runs are evidence for it, and what the lab has learned about it.")}</p>
          </aside>}

      <div className="pc-secondary">

        {/* Always rendered, even at zero. Its absence would read as "everything is tagged", and the
            count being zero is exactly the signal that says this tree covers the whole scope —
            conceptShelf.js's rule for memory rows, which is the same rule for runs. */}
        <div className="pc-untagged">
          <b>{uiText("Untagged")}</b>
          <span>{uiPlural(forest.untagged.runs, '{0} run in this scope carry no concept at all.', '{0} runs in this scope carry no concept at all.')}</span>
          {forest.untagged.runs > 0 && <span className="muted">{uiText("They are not in the tree above. That is a gap in tagging, not evidence that nothing was learned in them.")}</span>}
        </div>

        {/* The tree is the `is_a` relation — a concept id spells its own ancestry, so nesting is
            RECOVERED and never inferred. Co-occurrence is the one relation no id can state about
            itself, and it is folded from the same rows: a pair is two concepts one run was tagged
            with, counted over DISTINCT runs. This section replaces what the removed
            `portfolio_concept_graph` used to compute over the capsule ledger — same rule, but over
            the population this view is actually showing. */}
        {!coverage?.empty && <section className="pc-pairs" aria-label={uiText("Concept co-occurrence")}>
          <div className="pc-pairs-h">
            <h3>{uiText("Studied together")}</h3>
            <div className="seg pc-floor" role="group" aria-label={uiText("Co-occurrence threshold")}>
              {PAIR_FLOORS.map(floor => <button key={floor.value} type="button"
                aria-pressed={pairFloor === floor.value}
                className={pairFloor === floor.value ? 'on' : ''} title={uiText(floor.hint)}
                onClick={() => setPairFloor(floor.value)}>{uiText(floor.label)}</button>)}
            </div>
          </div>
          {cooccurrence.pairs.length === 0
            ? <p className="muted">
                {((cooccurrence.pairCandidates === 0 ? uiText('No run in this scope was tagged with two concepts, so there is nothing to pair.') : uiPlural(cooccurrence.minRuns, 'No pair of concepts appeared together in {0} or more runs. ', 'No pair of concepts appeared together in {0} or more runs. ')
                    + uiPlural(cooccurrence.pairCandidates, '{0} pairing(s) were seen in a single run — a tagger emitting two labels once, which is not yet evidence of a pattern.', '{0} pairing(s) were seen in a single run — a tagger emitting two labels once, which is not yet evidence of a pattern.')))}
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
                    <span className="pc-pair-n">{uiPlural(pair.runs, '{0} run', '{0} runs')}</span>
                  </li>)}
                </ol>
                {cooccurrence.pairs.length > MAX_PAIR_ROWS
                  && <p className="muted">{uiPlural(cooccurrence.pairs.length - MAX_PAIR_ROWS, '+{0} more pair(s) above the threshold, not listed.', '+{0} more pair(s) above the threshold, not listed.')}</p>}
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
            {uiPlural(cooccurrence.pairsOmitted, '{0} further pair(s) reached this threshold and were counted, but fall outside the ranked set this view keeps. They are missing from the list above and from the partners of any concept they touch.', '{0} further pair(s) reached this threshold and were counted, but fall outside the ranked set this view keeps. They are missing from the list above and from the partners of any concept they touch.')}</p>}
          {/* Two different absences, never merged. The cap above is exact — we counted them and showed
              fewer. A pruned node's pairs were never materialized, so their count is UNKNOWN, and
              printing it as zero would present a bound as a finding. */}
          {cooccurrence.pairsOutsideProjectionUnknown && <p className="muted">{uiPlural(cooccurrence.pairSourceNodesIncluded, 'Only the {0} most-used concepts were paired; pairs touching the other {1} are unknown, not absent.', 'Only the {0} most-used concepts were paired; pairs touching the other {1} are unknown, not absent.', [cooccurrence.pairSourceNodesIncluded, cooccurrence.pairSourceNodesPruned])}</p>}
        </section>}
      </div>
    </div>
  </div>
}
