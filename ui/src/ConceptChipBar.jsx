import React, { useEffect, useMemo, useRef, useState } from 'react'
import { chipsAtPath, breadcrumb, matchingNodeIds, orderChipsByDelta } from './conceptChips.js'
import { searchConcepts } from './conceptSearch.js'
import { Marked } from './Highlight.jsx'
import { canonicalId } from './conceptId.js'
import { get, runApiPath } from './util.js'

const SEARCH_RESULTS = 8   // dropdown cap; the pure model ranks globally, this trims the visible list

const SearchIcon = ({ small }) => (
  <svg width={small ? 13 : 15} height={small ? 13 : 15} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="2.2" aria-hidden="true">
    <circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>)

// View 2 — the concept chip bar riding OVER the lineage graph. Breadcrumb-navigable (drill into a
// concept to reveal its next level) and multi-selectable (OR): selecting concepts highlights every
// graph node that touches any selected concept OR a descendant of it, dimming the rest. Reads the
// folded `node_concepts` + consolidation rename straight off the run state (same source as View 1) for
// the chips themselves, plus ONE best-effort /concepts fetch for the advisory Δbest sort order (never
// blocking). Pure presentation; the selection drives the graph via `onHighlight(set | null)`.
// The pure model (chip counts, breadcrumb, matching set) lives in conceptChips.js and is unit-tested;
// this component is only wiring + markup.
export default function ConceptChipBar({ state, onHighlight, runId = null, generation = null }) {
  const nodeConcepts = state?.node_concepts || {}
  const rename = state?.concept_consolidation || {}
  const [path, setPath] = useState('')                 // breadcrumb drill position ('' = roots)
  const [selected, setSelected] = useState([])         // ordered selected concept ids (OR)
  const [searchOpen, setSearchOpen] = useState(false)  // the search box is revealed (icon toggled)
  const [query, setQuery] = useState('')               // live free-text concept query
  const [cursor, setCursor] = useState(-1)             // keyboard-focused result index
  const [rollup, setRollup] = useState(null)           // per-concept subtree Δ (best-effort sort hint)
  const inputRef = useRef(null)

  // F2: order chips by which concepts led to the best outcomes — descending Δbest-from-baseline, the same
  // signal the old Directions bar carried, now on concepts. The authoritative per-concept subtree metric
  // lives in the /concepts frame (concept_metrics.rollup); the UI state carries `metric` but not the
  // backend's `robust_metric`, so re-deriving Δ here would diverge — fetch the frame's rollup instead.
  // Best-effort and ADVISORY: a failure/loading just leaves the count order, so the bar never blocks.
  useEffect(() => {
    if (!runId) return undefined
    let alive = true
    const ctrl = new AbortController()
    get(`${runApiPath(runId, '/concepts')}?lens=is_a`, { signal: ctrl.signal, cache: 'no-store' })
      .then(frame => { if (alive && frame && frame.metrics && frame.metrics.rollup) setRollup(frame.metrics.rollup) })
      .catch(() => {})     // advisory sort only
    return () => { alive = false; ctrl.abort() }
  }, [runId, generation])

  const hasConcepts = useMemo(
    () => Object.values(nodeConcepts).some(v => v && v.length), [nodeConcepts])
  const rawChips = useMemo(() => chipsAtPath(nodeConcepts, rename, path), [nodeConcepts, rename, path])
  // Overlay the Δbest order when the advisory rollup is available; else keep chipsAtPath's count order.
  const chips = useMemo(() => orderChipsByDelta(rawChips, rollup), [rawChips, rollup])
  const crumbs = useMemo(() => breadcrumb(path), [path])

  // Free-text search is GLOBAL (across the whole concept tree, not just the drilled level): the ranked
  // results drive a live graph-highlight preview and, on commit, become an ordinary subtree selection.
  const trimmedQuery = query.trim()
  const searching = hasConcepts && trimmedQuery.length > 0
  // Rank ALL matches once (bounded to the model default), then slice for the dropdown and reuse the
  // same ranked set for the graph preview — one conceptUniverse build per keystroke instead of two.
  const allResults = useMemo(
    () => searching ? searchConcepts(nodeConcepts, rename, query) : [],
    [searching, nodeConcepts, rename, query])
  const results = useMemo(() => allResults.slice(0, SEARCH_RESULTS), [allResults])
  const matchedIds = useMemo(() => new Set(allResults.map(r => r.id)), [allResults])

  // The highlighted node set is a pure function of (nodeConcepts, selected, rename). Push it up whenever
  // its VALUE changes — keyed on a stable signature so a live SSE tick that only re-refs node_concepts
  // (same matching ids) doesn't churn the parent, while a genuinely new/vanished match still updates.
  // The signature must distinguish "no selection" (highlight === null -> no dim) from "selection matches
  // zero nodes" (empty Set -> everything dims): else, if the matches vanish while selected and the user
  // then clicks Clear, both map to the same key, the effect never re-fires, and the graph stays fully
  // dimmed. Prefix null with `none` and a Set with `s:`.
  // If a live projection temporarily carries no concepts, remove graph dimming in the same
  // render that hides this control. Keeping an empty Set here would dim every DAG node while also
  // removing the only visible way to clear the filter.
  const committed = useMemo(
    () => hasConcepts ? matchingNodeIds(nodeConcepts, selected, rename) : null,
    [hasConcepts, nodeConcepts, selected, rename])
  // While a query is live, the graph previews the SEARCH match instead of the pinned selection (null on
  // no match -> no dimming, never a stuck empty Set); clearing the query reverts to the committed set.
  const preview = useMemo(
    () => (searching && allResults.length)
      ? matchingNodeIds(nodeConcepts, allResults.map(result => result.id), rename) : null,
    [searching, allResults, nodeConcepts, rename])
  const highlight = searching ? preview : committed
  const sig = highlight ? 's:' + [...highlight].sort((a, b) => a - b).join(',') : 'none'
  useEffect(() => { onHighlight && onHighlight(highlight) }, [sig])       // onHighlight is a stable setter
  useEffect(() => () => { onHighlight && onHighlight(null) }, [])         // clear the dim when unmounted
  useEffect(() => {
    if (hasConcepts) return
    setSelected(value => value.length ? [] : value)
    setPath(value => value ? '' : value)
    setQuery(value => value ? '' : value)
    setSearchOpen(value => value ? false : value)
  }, [hasConcepts])
  // A new query starts at the first result; live SSE/consolidation changes under the same query
  // re-clamp the existing cursor so Enter and aria-selected never point outside the visible list.
  useEffect(() => { setCursor(results.length ? 0 : -1) }, [query])
  useEffect(() => {
    setCursor(current => results.length
      ? Math.min(Math.max(current, 0), results.length - 1)
      : -1)
  }, [results.length])

  // A chip's SELECTION KEY: a normal chip selects its subtree (plain id, prefix match); the "· here"
  // chip selects EXACTLY the nodes tagged at `path` (an `=`-prefixed key, exact match) so its count and
  // its highlight agree. `selected` holds these keys; conceptMatches interprets the `=` marker.
  const keyOf = (chip) => (chip.atLevel ? '=' : '') + chip.id
  const toggleSelect = (key) =>
    setSelected(s => s.includes(key) ? s.filter(x => x !== key) : [...s, key])
  const removeSelected = (key) => setSelected(s => s.filter(x => x !== key))
  const clearSelection = () => setSelected([])

  // Commit a searched concept as a plain (subtree) selection, then clear the query but keep the box open
  // and focused so several concepts can be pinned in a row. Deduped against the existing selection.
  const commitConcept = (id) => {
    setSelected(s => s.includes(id) ? s : [...s, id])
    setQuery('')
    setCursor(-1)
    inputRef.current?.focus()
  }
  const openSearch = () => { setSearchOpen(true); setTimeout(() => inputRef.current?.focus(), 0) }
  const closeSearch = () => { setSearchOpen(false); setQuery(''); setCursor(-1) }
  const onSearchKey = (event) => {
    if (event.key === 'ArrowDown') { event.preventDefault(); setCursor(c => Math.min(c + 1, results.length - 1)) }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setCursor(c => Math.max(c - 1, 0)) }
    else if (event.key === 'Enter') { event.preventDefault(); const r = results[cursor]; if (r) commitConcept(r.id) }
    else if (event.key === 'Escape') { event.preventDefault(); query ? setQuery('') : closeSearch() }
  }

  if (!hasConcepts) return null
  // Display a selection key: strip the `=` exact marker for the label, keep a hint that it's level-exact.
  const keyValue = (key) => key[0] === '=' ? key.slice(1) : key
  const keyLabel = (key) => canonicalId(keyValue(key), rename) || keyValue(key)
  const keyExact = (key) => key[0] === '='

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
        <div className="cs">
          {!searchOpen
            ? <button type="button" className="cs-icon" aria-label="Search concepts" title="Search concepts"
                onClick={openSearch}><SearchIcon /></button>
            : <div className={'cs-box' + (searching ? ' focus' : '')}>
                <SearchIcon small />
                <input ref={inputRef} className="cs-input" style={{ width: 150 }} value={query}
                  placeholder="find a concept…" aria-label="Search concepts" autoComplete="off"
                  role="combobox" aria-expanded={searching}
                  aria-controls={searching ? 'cb-search-results' : undefined}
                  onChange={e => setQuery(e.target.value)} onKeyDown={onSearchKey}
                  onBlur={() => { if (!query) closeSearch() }} />
                {query &&
                  <button type="button" className="cs-clear" aria-label="Clear search"
                    onClick={() => { setQuery(''); inputRef.current?.focus() }}>×</button>}
              </div>}
          {searching &&
            <div className="cs-pop" id="cb-search-results" role="listbox" aria-label="Concept search results">
              {results.length === 0
                ? <div className="cs-empty">No concept matches “{trimmedQuery}”.</div>
                : <>
                  <div className="cs-pop-h">Concepts · Enter to pin</div>
                  {results.map((r, i) => {
                    const parent = r.id.includes('/') ? r.id.slice(0, r.id.lastIndexOf('/') + 1) : ''
                    return (
                      <button key={r.id} type="button" role="option" aria-selected={i === cursor}
                        className={'cs-res' + (i === cursor ? ' cursor' : '')}
                        onMouseEnter={() => setCursor(i)} onClick={() => commitConcept(r.id)}
                        title={`${r.id} · ${r.count} experiment(s)`}>
                        <span><span className="cs-path">{parent}</span><Marked text={r.label} query={query} /></span>
                        <span className="cs-cnt">{r.count}</span>
                      </button>
                    )
                  })}
                </>}
            </div>}
        </div>
        {selected.length > 0 &&
          <button type="button" className="btn sm ghost" onClick={clearSelection}>
            clear ({selected.length})</button>}
      </div>

      {selected.length > 0 &&
        <div className="cb-selected" aria-label="Selected concepts">
          {selected.map(key => {
            const label = keyLabel(key)
            return <button key={key} type="button" className="cb-pill"
              onClick={() => removeSelected(key)}
              title={`${label}${keyExact(key) ? ' (exactly)' : ''} — click to remove`}>
              {keyExact(key) && <span className="cb-here" aria-hidden="true">·</span>}
              <span className="cb-pill-label">{label}</span>
              <span className="cb-x" aria-hidden="true">×</span>
              <span className="sr-only"> remove filter {label}</span>
            </button>
          })}
        </div>}

      <div className="cb-chips" aria-label={path ? `Concepts under ${path}` : 'Top-level concepts'}>
        {chips.length === 0
          ? <span className="muted cb-empty">No concepts at this level.</span>
          : chips.map(chip => {
            const key = keyOf(chip)
            const on = selected.includes(key)
            return (
              <span key={chip.id + (chip.atLevel ? '#here' : '')}
                className={'cb-chip' + (on ? ' on' : '') + (chip.atLevel ? ' here' : '')
                  + (!on && searching && matchedIds.has(chip.id) ? ' match' : '')}>
                <button type="button" className="cb-chip-main" aria-pressed={on}
                  onClick={() => toggleSelect(key)}
                  title={`${chip.id} · ${chip.count} experiment(s)${chip.atLevel ? ' tagged here (not deeper) — highlights only these' : ''}`}>
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
