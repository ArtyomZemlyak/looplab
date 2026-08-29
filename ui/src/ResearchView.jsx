// The RESEARCH view — the board read as a ladder of questions rather than as a lifecycle.
//
// The operator's objection is the reason it exists: a chain of sharpening claims ("distillation
// raises recall" / "…from an LLM, twice as much" / "…with an RL loss, three times") is a mess drawn
// as Kanban cards and a mess drawn as the Directions view's flat parent->child lines. It is a
// LADDER, and the whole chain has to stay visible at once.
//
// Every decision this file makes lives in `questionLattice.js`; what is here is choreography and
// DOM. The one rule this half owns is what a row may CLAIM, and it is the same rule the rest of the
// tree keeps: a number is shown with what qualifies it or not at all.
import React, { useMemo, useState } from 'react'

import { cardIsDirection, childrenByParent } from './cardLineageModel.js'
import { isRecord } from './panelPrimitives.js'
import { UNGROUPED_ID, latticeRollups, latticeRows, questionClosure,
  unfiledExperiments } from './questionLattice.js'

const _text = value => (typeof value === 'string' ? value.trim() : '')
const _delta = value => `${value > 0 ? '+' : ''}${Number(value.toFixed(4))}`

// Indent by DEPTH and not by set size, which are the same number for a chain and diverge for a row
// with two parents: `{distill, llm}` has two concepts under `{distill}` and under `{llm}` alike, so
// size would draw both copies at one depth and the ladder would stop reading as a ladder.
const _INDENT_PX = 22

// The concepts this row ADDS to its parent — what makes it the sharper question. Showing the whole
// set on every row repeats the parent's concepts down the entire chain, which is exactly the
// "мешанина" the operator predicted; showing the delta makes each rung say what it narrows.
function addedConcepts(row, byRowKey) {
  const parent = row.parentId && row.parentId !== UNGROUPED_ID
    ? byRowKey.get(row.rowKey.slice(0, row.rowKey.lastIndexOf('>')))
    : null
  if (!parent) return row.tags
  const inherited = new Set(parent.tags)
  return row.tags.filter(tag => !inherited.has(tag))
}

export default function ResearchView({ cards, state, renderCard }) {
  const [collapsed, setCollapsed] = useState(() => new Set())
  const [concept, setConcept] = useState('')

  const all = useMemo(() => (Array.isArray(cards) ? cards.filter(isRecord) : []), [cards])
  // QUESTIONS are the rows; experiments are drawn under the question that owns them. Putting every
  // card in the lattice is the "тупо карточками" the operator ruled out — and it would also give a
  // one-knob experiment a lattice position it has no business holding, since its concepts are a
  // fact about the work rather than a question anyone asked.
  const questions = useMemo(() => all.filter(cardIsDirection), [all])
  // CHILDREN ARE EXPERIMENTS, and the kind is now asked rather than assumed (2026-08-29).
  // `all` holds both kinds and the fold sets `parent_card_id` for ANY card without consulting its
  // kind (`events/card_ledger.py`), so a nested QUESTION landed here: counted, labelled and drawn
  // below its parent as an experiment, while the same card also stood in the lattice as a question.
  // One card, two contradictory readings on one screen — and the experiment reading is the false
  // one, since a direction owns no action and has no result to roll up.
  // A nested question keeps its lattice position and loses nothing: `latticeRows` is what draws
  // question-under-question, and it is the surface that can say what the nesting MEANS.
  // THE STATE IS UNREACHED ON THIS BOX AND THAT IS STATED RATHER THAN GLOSSED: across every
  // preserved log, 0 of 218 `card_added` rows carry a direction with a `parent_card_id`. The fold
  // permits it with no guard, so nothing but the Researcher's own habit keeps it at zero — this is
  // a cheap honesty guard on a reachable shape, not a recovery of anything that has happened.
  // The rule lives in `cardLineageModel.js` beside the kind predicate it asks — see the comment
  // there for why a nested question must not be grouped here, and why it was moved out of this
  // file at all (a replica of an inline loop cannot catch that loop being inverted).
  const childKids = useMemo(() => childrenByParent(all), [all])

  const rows = useMemo(() => latticeRows(questions), [questions])
  // The complement of the ladder. Without it a parentless experiment is drawn by NOTHING here, and
  // the Directions tab's "Not filed under any direction" group stays the only surface that has it —
  // which is exactly why that tab cannot be retired until this exists.
  const unfiled = useMemo(() => unfiledExperiments(all), [all])
  // `state?.nodes`, not `state`: `latticeRollups` reads nothing else off the run state, while
  // `useRunState` replaces the whole snapshot object on every 2.5 s poll. Depending on `state` paid
  // the full rollup — now real work, since `cardEvidenceNodes` started returning evidence — for
  // every poll that appended only a span, a comment or a cost row and moved no node at all.
  const rollups = useMemo(() => latticeRollups(state, all, rows), [state?.nodes, all, rows])
  const byRowKey = useMemo(() => new Map(rows.map(r => [r.rowKey, r])), [rows])

  // Every concept any question names, for the filter. Sorted, so the list does not reorder itself
  // as the run appends questions.
  const concepts = useMemo(() => {
    const seen = new Set()
    for (const row of rows) for (const tag of row.tags) seen.add(tag)
    return [...seen].sort()
  }, [rows])

  // A filtered row keeps its ANCESTRY: hiding the parents of a matching row would leave a sharpening
  // floating at depth 3 under nothing, which misstates what it sharpens. So a row is shown when it
  // matches or when a row below it in its own branch does.
  const visible = useMemo(() => {
    if (!concept) return rows
    const keep = new Set()
    for (const row of rows) {
      if (!row.tags.includes(concept)) continue
      const parts = row.rowKey.split('>')
      for (let i = 1; i <= parts.length; i += 1) keep.add(parts.slice(0, i).join('>'))
    }
    return rows.filter(row => keep.has(row.rowKey))
  }, [rows, concept])

  // A collapsed row hides its whole branch. Keyed by `rowKey`, so collapsing one copy of a
  // twice-placed question leaves the other copy open — they are two positions, not one row.
  const shown = visible.filter(row => ![...collapsed].some(
    key => key !== row.rowKey && row.rowKey.startsWith(`${key}>`)))
  const toggle = rowKey => setCollapsed((prev) => {
    const next = new Set(prev)
    if (next.has(rowKey)) next.delete(rowKey)
    else next.add(rowKey)
    return next
  })

  const bar = <div className="toolbar research-filter" role="group" aria-label="Filter by concept">
    <label>
      <span className="muted">Concept</span>{' '}
      <select value={concept} onChange={e => setConcept(e.target.value)}
        aria-label="Show only questions naming this concept">
        <option value="">all ({concepts.length})</option>
        {concepts.map(id => <option key={id} value={id}>{id}</option>)}
      </select>
    </label>
    {concept && <button type="button" className="btn sm ghost" onClick={() => setConcept('')}>
      clear
    </button>}
  </div>

  if (!questions.length) {
    return <div className="card-research" role="region" aria-label="Research questions">
      {/* NOT an error and NOT an empty board: at the start of a run the Researcher has not asked
          anything yet, and a view that said "no questions" as though something were missing would
          misreport a healthy run's first minutes. */}
      <div className="muted card-empty">
        no research question registered yet — the opening memo has not been written
      </div>
    </div>
  }

  return <div className="card-research" role="region" aria-label="Research questions">
    {concepts.length > 0 && bar}
    <ol className="research-lattice">
      {shown.map((row) => {
        const roll = rollups.get(row.rowKey) || {}
        const kids = childKids.get(row.id) || []
        const isCollapsed = collapsed.has(row.rowKey)
        const branch = visible.some(r => r.rowKey.startsWith(`${row.rowKey}>`))
        const added = addedConcepts(row, byRowKey)
        // DIMMED, never removed. A closed question is part of the chain that explains its
        // neighbours, and dropping it out of the ladder would leave a sharpening under nothing —
        // which is also why there is no "hide closed" control: the operator asked for the whole
        // chain to stay visible, and a row they can still read and expand is one they can reopen.
        const closure = questionClosure(row.card, roll)
        const headId = `research-row-${encodeURIComponent(row.rowKey)}`
        return <li key={row.rowKey}
          className={'research-row' + (closure ? ' research-closed' : '')
            + (closure && !closure.supported ? ' research-closed-unsupported' : '')}
          style={{ marginLeft: row.depth * _INDENT_PX }}
          aria-labelledby={headId}>
          <div className="research-row-h">
            {branch
              ? <button type="button" className="btn sm ghost research-twist"
                /* NO `aria-controls`: what this twist opens is the SIBLING rows below it in the
                   same list, not one container, and pointing it at this row's own heading — which
                   is what it did — announces the control as governing its own label. */
                aria-expanded={!isCollapsed}
                aria-label={`${isCollapsed ? 'Show' : 'Hide'} the sharper questions under `
                  + `${_text(row.card.statement) || row.id}`}
                title={isCollapsed ? 'show the sharper questions under this' : 'hide them'}
                onClick={() => toggle(row.rowKey)}>{isCollapsed ? '▸' : '▾'}</button>
              : <span className="research-twist" aria-hidden="true" />}
            <span id={headId} className="research-statement">
              {_text(row.card.statement) || row.id}
            </span>
            {added.map(tag => <span key={tag} className="chip chip-concept">{tag}</span>)}
            {/* A row that appears in two places says so where it appears. Without this the operator
                reads one question as two, and the duplication is deliberate. */}
            {closure && <span className={'chip' + (closure.supported ? ' muted' : ' warn')}
              title={closure.supported
                ? `closed (${closure.by}) with ${closure.sharper} sharper question(s) and `
                  + `${closure.measured} measured experiment(s) behind it`
                : 'closed with NOTHING narrower behind it — no sharper question was asked and no '
                  + 'experiment of its own produced evidence'}>
              {closure.by}{closure.supported ? '' : ' · nothing narrower'}
            </span>}
            {row.duplicated && <span className="chip muted"
              title="this question narrows more than one broader question, so it is listed under each">
              also listed above
            </span>}
          </div>
          <div className="research-row-facts">
            {typeof roll.best === 'number'
              // The tone follows the SIGN, not the presence of a number: a question answered with a
              // regression is answered, and drawing it green because it has a value would read as a
              // win. `warn` overrides both — a mixed field is the more important thing to say.
              ? <span className={'chip' + (roll.mixedComparability ? ' warn'
                : (roll.best > 0 ? ' ok' : ''))}
                title={roll.mixedComparability
                  ? 'the experiments behind these numbers recorded provably different comparability'
                    + ' keys, so this best won a mixed field'
                  : `best improvement measured under this question, by ${roll.bestCardId}`}>
                best {_delta(roll.best)}
                {roll.bestCardId && roll.bestCardId !== row.id ? ` by ${roll.bestCardId}` : ''}
                {roll.mixedComparability ? ' · mixed comparability' : ''}
              </span>
              // Absent is SAID and never drawn as a zero — an unanswered question and one answered
              // with no improvement are different findings.
              : <span className="chip muted">not measured yet</span>}
            {typeof roll.own === 'number' && roll.own !== roll.best && <span className="chip muted"
              title="what this question's OWN experiments reached, before its sharper children">
              own {_delta(roll.own)}
            </span>}
            {roll.descendants > 0 && <span className="chip muted">
              {roll.descendants} sharper question{roll.descendants === 1 ? '' : 's'}
            </span>}
            {kids.length > 0 && <span className="chip muted">
              {kids.length} experiment{kids.length === 1 ? '' : 's'}
            </span>}
          </div>
          {!isCollapsed && kids.length > 0 && <div className="research-experiments">
            {kids.map(child => renderCard(child))}
          </div>}
          {!isCollapsed && kids.length === 0 && !branch && <div className="muted card-empty">
            no experiment proposed against this yet
          </div>}
        </li>
      })}
      {shown.length === 0 && <li className="muted card-empty">
        no question names {concept}
      </li>}
    </ol>
    {/* Always LAST and never inside the ladder: these cards have no position in it. The section is
        rendered only when occupied — a permanently empty "unfiled" heading is a question the
        operator has to answer before they can ignore it — and it is suppressed while a concept
        filter is on, because an unfiled card names no concept and cannot match one. */}
    {!concept && unfiled.length > 0 && <section className="research-unfiled"
      aria-labelledby="research-unfiled-h">
      <h3 id="research-unfiled-h" className="research-unfiled-h">
        Not filed under any question <span className="muted">{unfiled.length}</span>
      </h3>
      <div className="muted card-empty">
        no question claims {unfiled.length === 1 ? 'this experiment' : 'these experiments'} — the
        Researcher proposed {unfiled.length === 1 ? 'it' : 'them'} without naming a direction
      </div>
      <div className="research-experiments">{unfiled.map(card => renderCard(card))}</div>
    </section>}
  </div>
}

export { addedConcepts }
