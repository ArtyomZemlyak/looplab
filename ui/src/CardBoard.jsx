// The Card board — the authoritative kanban plus the legacy hypothesis fallback it degrades to —
// lifted out of panels.jsx (doc 25 UI-04). It carries a whole optimistic-control mini-framework
// (cardControlReflected / _cardWithOptimisticControls / the sentEditRef pruning) and the hypothesis
// delete-recovery journal, which is what makes it a module rather than one more function in the hub.
// panels.jsx re-exports HypothesisBoard, so RunView still funnels every panel through one lazy chunk.
import React, { useEffect, useMemo, useRef, useState } from 'react'
import { fmt, fmtInt, CONTROL, commandFeedback, createIdempotencyKey, deadlineGet, getRunCommand,
  isTransientCommandReadError, retryRunCommand, runApiPath, runCommand,
  submitCommand, traceDeadlineGet, traceGenerationMatches, nodeActivityStatus, nodeActivityView,
  NODE_ACTIVITY } from './util.js'
import { OpIcon } from './icons.jsx'
import Panel, { PanelPresentationContext } from './PanelShell.jsx'
import { cardControlRecovery, cardControlSubmission, cardEditReflected }
  from './cardControlModel.js'
// The lane vocabulary and the card-shape readers moved down into the pure model beside this file
// (the `ui/` house pattern) so the board and `node --test` read ONE table. They are aliased back to
// this file's existing private spellings rather than renamed at ~77 call sites: the point of the
// move is that the DECISIONS became testable, and a mechanical rename through the optimistic-control
// code would have put real regression risk on a change that adds none.
import {
  CARD_COLUMNS as _CARD_COLUMNS, CARD_FROZEN_STATUSES as _CARD_FROZEN_STATUSES,
  CARD_OPTIONAL_STATUSES as _CARD_OPTIONAL_STATUSES, CARD_RENDER_LIMIT as _CARD_RENDER_LIMIT,
  cardAttemptSummary, cardInt as _cardInt, cardLanes as _cardLanes,
  cardNodes as _cardNodes, cardNumber as _cardNumber, cardOrder as _cardOrder,
  cardRows as _cardRows, cardStatus as _cardStatus, cardStatusLabel as _cardStatusLabel,
  cardReopenable as _cardReopenable,
  cardText as _cardText, cardLessons as _cardLessons, cardOrigin as _cardOrigin,
  cardSelectionBlock,
  resolveSelectedCard,
} from './cardBoardModel.js'
import { cardAttemptCoverage, cardAttemptIndex } from './cardBoardViewModel.js'
import { CARD_KIND_DIRECTION, cardIsDirection, cardLineageViews,
  cardProposalDrift, rollupChips, splitBoardByKind } from './cardLineageModel.js'
import ResearchView from './ResearchView.jsx'
import { cardTraceNotice, cardTraceSections } from './cardTraceModel.js'
import { nodeTraceSubject } from './traceSurfaceModel.js'
import { isRecord, PANEL_REQUEST_TIMEOUT_MS, RUN_GENERATION_RE } from './panelPrimitives.js'
import { traceReadDeadlineMs } from './traceScrollModel.js'
import { DIALOG_PRIORITY, useDialogFocus } from './useDialogFocus.js'

// Legacy direction board retained as a graceful fallback for pre-Card logs. Current runs use the
// bounded public Card DTO and four generation-fenced, server-stamped operator controls below.
const _HYP_COLUMNS = [
  ['open', 'Open', 'question posed, not yet tested'],
  ['testing', 'Testing', 'experiments running'],
  ['supported', 'Supported', 'an experiment improved'],
  ['tested', 'Tested', 'evaluated, no improvement'],
  ['abandoned', 'Abandoned', 'dropped'],
]
// Monochrome source glyphs (no emoji): who posed the hypothesis. Reuses the shared icon set.
const _HYP_ICON = { researcher: 'search', deep_research: 'bulb', human: 'user', strategist: 'compass' }

const _CARD_ICON = {
  researcher: 'search', deep_research: 'bulb', human: 'user', strategist: 'compass',
  operator: 'user', engine: 'bot', novelty: 'bulb',
}
const _cardRefs = value => Array.isArray(value)
  ? value.filter(item => typeof item === 'string' && item).slice(0, 32) : []

const _CARD_CONTROL_KINDS = ['edit', 'priority', 'resources', 'drop', 'reopen', 'abandon']

function _cardResourceValues(value) {
  if (!isRecord(value)) return null
  const gpus = _cardInt(value.gpus)
  const gpuMem = _cardInt(value.gpu_mem_mib)
  return gpus == null && gpuMem == null ? null : {
    ...(gpus == null ? {} : { gpus }),
    ...(gpuMem == null ? {} : { gpu_mem_mib: gpuMem }),
  }
}

function _sameCardResourceValues(left, right) {
  const a = _cardResourceValues(left)
  const b = _cardResourceValues(right)
  if (a == null || b == null) return a === b
  return ['gpus', 'gpu_mem_mib'].every(key => (
    Object.hasOwn(a, key) === Object.hasOwn(b, key) && a[key] === b[key]
  ))
}

function cardControlReflected(card, kind, patch, baseline, expectedEventSeq) {
  if (!card || !isRecord(patch)) return false
  if (kind === 'edit') {
    // Modern folds publish the exact durable event that owns the display overlay. This remains
    // reliable when public-state secret redaction transforms the text into a non-prefix value.
    return cardEditReflected(card, patch, baseline, expectedEventSeq)
  }
  if (kind === 'priority') return card.priority === patch.priority && card.pinned === true
  if (kind === 'resources') return _sameCardResourceValues(card.resource_pin, patch.resource_pin)
  if (kind === 'drop') return card.status === 'dropped'
    && (!patch.dropped_reason || card.dropped_reason === patch.dropped_reason)
  // The drop's mirror. Reflection is "the card left the dropped lane" and deliberately NOT "the
  // reason matches": the server's reopen receipt carries its OWN reason, and the fold clears
  // `dropped_reason` when the drop stops applying, so keying on it would leave every reopen
  // optimistically pending until a poll timed it out.
  if (kind === 'reopen') return card.status !== 'dropped'
  if (kind === 'abandon') return card.verdict === 'abandoned'
  return false
}

function _cardWithOptimisticControls(card, controlState) {
  if (!isRecord(controlState?.updates)) return card
  const visible = { ...card }
  for (const kind of _CARD_CONTROL_KINDS) {
    if (isRecord(controlState.updates[kind])) Object.assign(visible, controlState.updates[kind])
  }
  return visible
}

function _cardResourceSummary(value, { unavailable = 'unspecified' } = {}) {
  const footprint = _cardResourceValues(value)
  if (!footprint) return unavailable
  const gpus = footprint.gpus
  const memory = footprint.gpu_mem_mib
  return [
    gpus == null ? 'GPU count unspecified'
      : gpus === 0 ? 'CPU only' : `${gpus} GPU${gpus === 1 ? '' : 's'}`,
    memory == null ? null : `${fmtInt(memory)} MiB/GPU`,
  ].filter(Boolean).join(' · ')
}

function _CardProjectionNotice({ projection, cards }) {
  if (!isRecord(projection)) return <div className="card-projection-note" role="status">
    Card coverage receipt unavailable; this older payload may be incomplete.
  </div>
  if (projection.complete === true) return null
  const total = _cardInt(projection.total)
  const returned = _cardInt(projection.returned) ?? cards.length
  const sourceInvalid = projection.source_valid === false
  return <div className="card-projection-note" role="status">
    <OpIcon name="alert" size={12} />
    <span>{sourceInvalid
      ? 'Card source was invalid; no complete board can be claimed.'
      : `Showing ${returned}${total == null ? '' : ` of ${total}`} Cards; clipped or redacted public fields are marked partial.`}</span>
  </div>
}

// `presentation` picks between the board's two card shapes and nothing else:
//   'full'  — every fact plus the operator controls. What the modal panel has always rendered, and
//             what the workspace view's DETAIL PANE renders for the one selected Card.
//   'lane'  — a selectable summary for a lane in the split view, where the facts have a pane to live
//             in and repeating them under every lane card is what made the board need 1560px.
// The two shapes share one component (rather than splitting into two) because the drafts, the four
// re-seed effects and the whole optimistic-control closure below belong to the card IDENTITY, not to
// a presentation; duplicating them is how a control silently stops reflecting its own fold.
function _CardKanbanCard({
  card, receipt, onSelect, onClose, onControl, controlState, controlsLocked,
  presentation = 'full', selected = false, onOpen = null, attempts = null,
  attemptCoverage = null, onRecover = null, state = null, lineage = null,
}) {
  // Item 6's two derivations. `state` is optional on purpose: a lane card is a summary, and the
  // legacy pre-Card fallback board has no run state to pass — both then simply render nothing extra
  // rather than the component having to branch on which host built it.
  const origin = _cardOrigin(card)
  const lessons = _cardLessons(state, card)
  const beliefId = _cardText(card.belief_id)
  const retryOf = _cardText(card.retry_of)
  const claimRefs = [...new Set((Array.isArray(card.claim_refs) ? card.claim_refs : [])
    .map(_cardText).filter(Boolean))].slice(0, 64)
  const statement = _cardText(card.statement) || `Card ${card.id}`
  const source = _cardText(card.source)
  const operator = _cardText(card.operator)
  const evalProfile = _cardText(card.eval_profile)
  const params = isRecord(card.params) ? Object.entries(card.params)
    .filter(([, value]) => _cardNumber(value) != null).slice(0, 6) : []
  const spaceCount = isRecord(card.space) ? Object.keys(card.space).length : 0
  // The coordinates that RAN, keyed by the knob that moved, so the Action row above can lead with
  // the real value and keep the proposal in brackets — the same shape `param_carriers.
  // node_params_brief` renders for the agents. `drift` is null when the two agree or when nothing
  // was comparable, which is what keeps an unchanged card byte-identical to what it drew before.
  const isDirection = cardIsDirection(card)
  // The server's own exact count — see `card_child_rollup`; it stays true where `child_card_ids`
  // clipped and where the 256-card wire cap kept a child off this page.
  const childCount = Number.isSafeInteger(card.child_rollup?.children) ? card.child_rollup.children : 0
  const drift = cardProposalDrift(card)
  const moved = new Map((drift?.params || [])
    .filter(name => isRecord(card.applied_params) && card.applied_params[name] != null)
    .map(name => [name, card.applied_params[name]]))
  const footprintKnown = Object.hasOwn(card, 'footprint')
  const baseFootprint = isRecord(card.footprint) ? card.footprint : null
  const resourcePin = isRecord(card.resource_pin) ? card.resource_pin : null
  // This is the configured Card footprint after applying the operator override. Runtime scheduling may
  // still allocate less, so never label the client-side projection as an effective allocation.
  const configuredFootprint = baseFootprint || resourcePin
    ? { ...(_cardResourceValues(baseFootprint) || {}), ...(_cardResourceValues(resourcePin) || {}) }
    : null
  if (configuredFootprint?.gpus === 0) delete configuredFootprint.gpu_mem_mib
  const configuredGpus = configuredFootprint ? _cardInt(configuredFootprint.gpus) : null
  const pinValues = _cardResourceValues(resourcePin)
  const formGpus = pinValues?.gpus ?? configuredGpus
  const formGpuMem = pinValues && Object.hasOwn(pinValues, 'gpu_mem_mib')
    ? pinValues.gpu_mem_mib : null
  const evalTimeout = _cardNumber(card.eval_timeout)
  const identity = isRecord(card.identity) ? card.identity : null
  const selection = isRecord(card.selection_provenance) ? card.selection_provenance : null
  const blockersKnown = Object.hasOwn(card, 'selection_blockers')
  const blockers = _cardRefs(card.selection_blockers)
  const evidenceKnown = Object.hasOwn(card, 'evidence')
  const evidence = _cardNodes(card.evidence).slice(0, 8)
  const concepts = _cardRefs(card.concept_tags).slice(0, 5)
  const parents = _cardNodes(card.parent_ids)
  const parent = _cardInt(card.parent_id)
  if (parent != null && !parents.includes(parent)) parents.unshift(parent)
  const parentGenerations = isRecord(card.parent_generations) ? card.parent_generations : null
  const scoredAgainst = _cardInt(card.scored_against)
  const scoredAgainstGeneration = _cardInt(card.scored_against_generation)
  const parentLineage = parents.map(id => {
    const attempt = parentGenerations ? _cardInt(parentGenerations[String(id)]) : null
    return `#${id} · attempt ${attempt == null ? 'unknown' : attempt}`
  }).join(', ')
  const scoredLineage = scoredAgainst == null ? ''
    : `#${scoredAgainst} · attempt ${scoredAgainstGeneration == null ? 'unknown' : scoredAgainstGeneration}`
  const bestDelta = _cardNumber(card.best_delta)
  const priority = _cardNumber(card.priority)
  const novelty = isRecord(card.novelty_verdict) ? _cardText(card.novelty_verdict.grade) : null
  const omissionCount = isRecord(receipt?.omissions) ? Object.keys(receipt.omissions).length : 0
  const declaredResources = footprintKnown
    ? _cardResourceSummary(baseFootprint) : 'resource projection unavailable'
  const configuredResources = _cardResourceSummary(configuredFootprint)
  const pinResources = _cardResourceSummary(resourcePin)
  const provenanceBits = [
    source && `source ${source}`,
    identity && _cardText(identity.kind) && `identity ${identity.kind}`,
    _cardText(card.provenance_tier) && `tier ${card.provenance_tier}`,
    selection && _cardText(selection.action_source) && `action ${selection.action_source}`,
    baseFootprint && _cardText(baseFootprint.proposed_by) && `proposed ${baseFootprint.proposed_by}`,
    baseFootprint && _cardText(baseFootprint.finalized_by) && `finalized ${baseFootprint.finalized_by}`,
    resourcePin && _cardText(resourcePin.pinned_by) && `resource pin ${resourcePin.pinned_by}`,
    _cardText(card.research_origin) && `research ${card.research_origin}`,
  ].filter(Boolean)
  const [statementDraft, setStatementDraft] = useState(statement)
  const [priorityDraft, setPriorityDraft] = useState(
    _cardInt(card.priority) == null ? '' : String(card.priority + 1))
  const [gpuDraft, setGpuDraft] = useState(formGpus == null ? '' : String(formGpus))
  const [memoryDraft, setMemoryDraft] = useState(formGpuMem == null ? '' : String(formGpuMem))
  const [dropReason, setDropReason] = useState('operator dropped')
  const [reopenReason, setReopenReason] = useState('operator reopened')
  const [controlError, setControlError] = useState('')
  const ownPending = isRecord(controlState?.pending) ? controlState.pending : null
  const busy = !!ownPending || controlsLocked === true
  // The research VERDICT (open/supported/testing/tested/abandoned — the only values `_evidence_verdict`
  // produces) is distinct from the work-lifecycle STATUS (peer review): replay can publish
  // status=proposed/evaluated with verdict=abandoned, so read the verdict separately — render it as its
  // own chip (a supported/tested outcome was otherwise invisible) and treat an abandoned belief as
  // terminal so the board stops offering edit/priority/drop controls.
  const verdict = _cardText(card.verdict)
  const terminal = _cardStatus(card) === 'dropped' || !!_cardText(card.merged_into)
    || verdict === 'abandoned'
  // Re-seed each draft ONLY when its own folded source (or the card identity) changes. A single effect
  // over every dep re-ran on ANY change, so an unrelated live fold (e.g. a card_ranked priority bump
  // arriving while the operator is typing a new statement) reset ALL four drafts and silently discarded
  // the in-progress edits in the other fields. Per-field effects keep each edit until its own source moves.
  useEffect(() => { setStatementDraft(statement) }, [card.id, statement])
  useEffect(() => {
    setPriorityDraft(_cardInt(card.priority) == null ? '' : String(card.priority + 1))
  }, [card.id, card.priority])
  useEffect(() => { setGpuDraft(formGpus == null ? '' : String(formGpus)) }, [card.id, formGpus])
  useEffect(() => {
    setMemoryDraft(formGpuMem == null ? '' : String(formGpuMem))
  }, [card.id, formGpuMem])

  const control = async (kind, data, patch) => {
    if (!onControl || busy) return
    setControlError('')
    try { await onControl(card, kind, data, patch) }
    catch (error) { setControlError(error?.message || String(error)) }
  }
  const saveStatement = () => {
    const next = statementDraft.trim()
    if (!next || next.length > 4000) { setControlError('Display statement must be 1–4000 characters.'); return }
    if (next !== statement) control('edit', { statement: next }, { statement: next })
  }
  const savePriority = () => {
    const visible = Number(priorityDraft)
    if (!Number.isSafeInteger(visible) || visible < 1 || visible > 256) {
      setControlError('Priority must be between 1 and 256.'); return
    }
    control('priority', { priority: visible - 1 }, { priority: visible - 1 })
  }
  const saveResources = () => {
    const nextGpus = Number(gpuDraft)
    const nextMemory = memoryDraft.trim() === '' ? null : Number(memoryDraft)
    if (!Number.isSafeInteger(nextGpus) || nextGpus < 0
      || (nextMemory != null && (!Number.isSafeInteger(nextMemory) || nextMemory < 0))) {
      setControlError('GPU count and memory must be non-negative integers.'); return
    }
    if (nextGpus === 0 && nextMemory != null) {
      setControlError('CPU-only Cards cannot request GPU memory.'); return
    }
    // This local patch contains quantitative display values only. Authority/provenance is stamped by
    // the server event and is never supplied by the browser, even optimistically.
    const pin = { gpus: nextGpus, ...(nextMemory == null ? {} : { gpu_mem_mib: nextMemory }) }
    control('resources', { gpus: nextGpus, gpu_mem_mib: nextMemory }, { resource_pin: pin })
  }
  const drop = () => {
    const reason = dropReason.trim() || 'operator dropped'
    control('drop', { reason }, { status: 'dropped', dropped_reason: reason })
  }
  // The drop's counterpart. The optimistic patch clears `dropped_reason` alongside the lane
  // because the fold does: once a later reopen supersedes the drop, the card is not carrying a
  // drop reason any more, and showing one on a reopened row would state a stop that no longer
  // applies. The drop RECEIPT survives in the log — this is the board, not the history.
  const reopen = () => {
    const reason = reopenReason.trim() || 'operator reopened'
    control('reopen', { reason }, { status: 'proposed', dropped_reason: null })
  }
  // This is deliberately Card-scoped: the backend receives one Card id, so siblings that happen to
  // share a seed remain unchanged. The control changes the Card's research verdict, not its work lane.
  const abandonCard = () => control('abandon', {}, { verdict: 'abandoned' })
  if (presentation === 'lane') {
    // The attempt COUNT is the one card-level fact the lane must not drop, because it is the fact
    // the board has never shown and the one an operator gets wrong: a lane card that looked exactly
    // like an experiment row is why "a Card == a Node" reads as true. `n experiments` on the face of
    // every card contradicts that before the pane is even opened. `attempts` is passed in (not
    // derived here) so the join is computed once per board render, not once per card.
    const roll = attempts ? cardAttemptSummary(attempts) : null
    return <article className={'card-kanban-card card-lane-card' + (selected ? ' on' : '')}
      data-card-id={card.id} aria-busy={ownPending ? 'true' : undefined}>
      <button type="button" className="card-lane-open" aria-pressed={selected}
        aria-label={`Open Card ${card.id}: ${statement}`}
        onClick={event => onOpen?.(card.id, event.currentTarget)}>
        <span className="card-kanban-stmt">
          <span className="hyp-src" title={source ? `source: ${source}` : 'source unavailable'}>
            <OpIcon name={_CARD_ICON[source] || 'dot'} size={12} />
          </span>
          <span>{statement}</span>
        </span>
        <span className="card-kanban-meta">
          <span className="chip xs" title="durable Card identity">{card.id}</span>
          {verdict && verdict !== 'open' && <span
            className={'chip xs ' + (verdict === 'supported' ? 'ok' : verdict === 'abandoned' ? 'warn' : '')}
            title={`research verdict: ${verdict} (distinct from the work status)`}>{verdict}</span>}
          {priority != null && <span className="chip xs" title="derived priority; 1 is highest">#{priority + 1}</span>}
          {card.pinned === true && <span className="chip xs warn"><OpIcon name="flag" size={10} /> pinned</span>}
          {roll && <span className={'chip xs' + (roll.total === 0 ? ' warn' : '')}
            title={roll.total === 0
              ? 'no experiment has run for this work item yet'
              : `${roll.total} experiment${roll.total === 1 ? '' : 's'} tested this work item`
                + (roll.missing ? ` · ${roll.missing} not in this snapshot` : '')}>
            {(attemptCoverage?.label ?? roll.total)} exp</span>}
          {/* NOT a status, and no longer painted like one. `selection_ready === false` says the Card
              queue will not pick this card up next, which for `work_terminal` / `work_in_flight` is
              simply what a card looks like once its experiment has run or while it is running. The
              board painted all of those the same amber as a genuinely broken ownership receipt, over
              the single word "blocked", with the reason buried in a title — so the operator read a
              lifecycle fact as an alarm. `cardSelectionBlock` splits the two and says which. */}
          {(() => {
            const block = cardSelectionBlock(card)
            return block && <span className={`chip xs${block.tone === 'fault' ? ' warn' : ' quiet'}`}
              title={block.title}>{block.label}</span>
          })()}
          {receipt && receipt.complete !== true && <span className="chip xs warn"
            title={`${omissionCount} public field omission${omissionCount === 1 ? '' : 's'}`}>partial</span>}
        </span>
        {concepts.length > 0 && <span className="card-kanban-tags">
          {concepts.slice(0, 3).map(concept => <span key={concept} className="chip xs">{concept}</span>)}
        </span>}
      </button>
      {isRecord(controlState?.notice) && <div
        className={'card-control-feedback ' + (controlState.notice.tone || '')}
        role={controlState.notice.tone === 'error' ? 'alert' : 'status'} aria-live="polite">
        {controlState.notice.text}</div>}
    </article>
  }
  return <article className="card-kanban-card" data-card-id={card.id} aria-label={statement}
    aria-busy={ownPending ? 'true' : undefined}>
    <div className="card-kanban-stmt">
      <span className="hyp-src" title={source ? `source: ${source}` : 'source unavailable'}>
        <OpIcon name={_CARD_ICON[source] || 'dot'} size={12} />
      </span>
      <span>{statement}</span>
    </div>
    <div className="card-kanban-meta">
      <span className="chip xs" title="durable Card identity">{card.id}</span>
      {_cardText(card.belief_id) && <span className="chip xs" title="research belief identity">
        belief {card.belief_id}</span>}
      {_cardText(card.retry_of) && <span className="chip xs" title="retry of work item">
        retry of {card.retry_of}</span>}
      {verdict && verdict !== 'open' && <span
        className={'chip xs ' + (verdict === 'supported' ? 'ok' : verdict === 'abandoned' ? 'warn' : '')}
        title={`research verdict: ${verdict} (distinct from the work status)`}>{verdict}</span>}
      {priority != null && <span className="chip xs" title="derived priority; 1 is highest">#{priority + 1}</span>}
      {card.pinned === true && <span className="chip xs warn"><OpIcon name="flag" size={10} /> pinned</span>}
      {/* THE SAME CHIP AS THE LANE CARD, and it has to be: this is the DETAIL PANE (and the whole
          `HypothesisBoard`), i.e. what opens when the operator clicks the card whose lane chip they
          just read. It kept the retired binary chip after the lifecycle/fault split landed one
          branch over, so on the live board every one of the 15 cards showed a quiet, explained
          lifecycle chip in the lane and an amber unexplained one here — the exact "I thought
          `blocked` was some kind of status" confusion, still on screen, one click away. The retired
          wording is a NEGATIVE pin in cardSelectionBlockers.test.js, so it may not be respelled
          here either: a commented-out copy is the same drift risk as a live one. */}
      {card.selection_ready === true
        ? <span className="chip xs ok" title="eligible for Card-driven selection">selection ready</span>
        : card.selection_ready === false
          ? (() => {
            const block = cardSelectionBlock(card)
            return block && <span className={`chip xs${block.tone === 'fault' ? ' warn' : ' quiet'}`}
              title={block.title}>{block.label}</span>
          })()
          : <span className="chip xs" title="selection readiness was not present in the public projection">readiness unknown</span>}
      {receipt && receipt.complete !== true && <span className="chip xs warn"
        title={`${omissionCount} public field omission${omissionCount === 1 ? '' : 's'}`}>
        partial details{omissionCount ? ` · ${omissionCount}` : ''}</span>}
    </div>
    {/* The two counts are compared, not truth-tested. `a || b || n` YIELDS `n` when a and b are
        falsy, so with no operator, no profile, no params and no space this guard evaluated to the
        NUMBER 0 — and React renders a bare `0` for `0 && <div/>` instead of rendering nothing.
        Measured: a stray `0` between the Card's chips and its "Declared" row on every Card that
        declares no action, across most runs in `runs/`. `_cardText` returning null for an absent
        string is what keeps the first two operands safe; a count has no such spelling. */}
    {(operator || evalProfile || params.length > 0 || spaceCount > 0) && <div className="card-kanban-fact">
      <span className="card-kanban-k">Action</span>
      <span>{operator || 'operator unspecified'}</span>
      {evalProfile && <span>profile {evalProfile}</span>}
      {params.map(([key, value]) => <span key={key} className={'card-param' + (moved.has(key) ? ' card-param-moved' : '')}>
        {key}={fmt(moved.has(key) ? moved.get(key) : value)}
        {moved.has(key) && <span className="muted"> (proposed {fmt(value)})</span>}
      </span>)}
      {spaceCount > 0 && <span>{spaceCount} search variable{spaceCount === 1 ? '' : 's'}</span>}
    </div>}
    {/* WHAT ACTUALLY RAN, and until 2026-08-25 this pane showed the PROPOSAL alone. `card.params`
        is receipt-bound and cannot be corrected; `applied_params` rides beside it on the wire and
        was rendered nowhere — so the fix that taught the agent's prompt, the digest and the tools to
        lead with the coordinates that ran left the one surface the OPERATOR looks at still saying
        the old numbers. Measured on `runs/e5small-dr-unified-v4`: six of the nine cards with an
        applied record disagree with their own proposal, the run's champion among them.
        Silent when the two agree, so a card that ran as proposed renders exactly as it always did. */}
    {drift && <div className="card-kanban-fact card-drift">
      <span className="card-kanban-k">Ran at</span>
      <span>
        <span className="chip xs warn">{drift.moved} of {drift.compared} knobs moved</span>
        {typeof card.applied_params_node === 'number'
          ? <span className="muted">on experiment #{card.applied_params_node}</span> : null}
      </span>
    </div>}
    <div className="card-kanban-fact">
      <span className="card-kanban-k">Declared</span>
      <span>{declaredResources}{evalTimeout == null ? '' : ` · ${fmt(evalTimeout)}s timeout`}</span>
    </div>
    {resourcePin && <div className="card-kanban-fact card-resource-pin">
      <span className="card-kanban-k">Configured pin</span>
      <span><span className="chip xs warn">{_cardText(resourcePin.pinned_by) === 'operator'
        ? 'operator override' : 'pending operator override'}</span> {configuredResources}
        <span className="card-resource-request">requested {pinResources}</span></span>
    </div>}
    <div className="card-kanban-fact">
      <span className="card-kanban-k">Provenance</span>
      <span>{provenanceBits.length ? provenanceBits.join(' · ') : 'unavailable'}</span>
    </div>
    {/* THE GATE AND ITS BLOCKERS ANSWER "why will the Card queue not pick this up next" — a question
        about a WORK ITEM. A direction is not one: it owns no executable action BY DESIGN, so
        `identity_not_native` / `action_owner_missing` / `freshness_unknown` are not defects on it,
        they are its definition restated as three alarms. Every direction on
        `runs/e5small-dr-unified-v5` wore all three, which reads as breakage on a row that is
        working exactly as intended. What a direction needs is an experiment filed under it, and
        that is what this says instead. */}
    {!isDirection && <div className="card-kanban-fact">
      <span className="card-kanban-k">Gate</span>
      <span>{selection && _cardText(selection.freshness) ? `freshness ${selection.freshness}` : 'freshness unknown'}
        {selection && _cardText(selection.owner_state) ? ` · owner ${selection.owner_state}` : ''}
        {selection && typeof selection.action_complete === 'boolean'
          ? ` · action ${selection.action_complete ? 'complete' : 'incomplete'}` : ''}</span>
    </div>}
    {!isDirection && blockers.length > 0 && <div className="card-kanban-blockers" aria-label="Selection blockers">
      {blockers.slice(0, 5).map(blocker => <span key={blocker} className="chip xs warn">
        {blocker.replaceAll('_', ' ')}</span>)}
      {blockers.length > 5 && <span className="muted">+{blockers.length - 5}</span>}
    </div>}
    {isDirection && <div className="card-kanban-fact">
      <span className="card-kanban-k">Direction</span>
      <span>
        <span className="chip xs chip-direction">not runnable by design</span>
        <span className="muted">{childCount > 0
          ? `${childCount} experiment${childCount === 1 ? '' : 's'} filed under it`
          : 'no experiment filed under it yet'}</span>
      </span>
    </div>}
    {!blockersKnown && <div className="muted card-kanban-unknown">Selection blockers unavailable</div>}
    {/* WHICH RESEARCH QUESTION THIS ROW BELONGS TO — above the node Lineage below, because the two
        are different relations and were easy to confuse while only one of them was rendered. This
        one is card->card: the DIRECTION this experiment answers, or the experiments answering this
        direction. The row below is node->node: which experiment this one was bred from.
        A parent id we hold but cannot resolve says so; it must NOT read as "unfiled", which is a
        different and false statement (see `cardLineageModel.js::cardLineageView`). */}
    {lineage && (lineage.parentId || lineage.children.length > 0
      || lineage.kind === CARD_KIND_DIRECTION) && <div className="card-kanban-fact">
      <span className="card-kanban-k">Research</span>
      <span>
        {lineage.kind === CARD_KIND_DIRECTION
          ? <span className="chip xs chip-direction">direction</span> : null}
        {lineage.parentId ? <>
          {' answers '}
          {lineage.parent && onOpen
            ? <button type="button" className="btn xs ghost"
                title={_cardText(lineage.parent.statement) || lineage.parentId}
                onClick={e => onOpen(lineage.parentId, e.currentTarget)}>
                {_cardText(lineage.parent.statement) || lineage.parentId}
              </button>
            : <span>{lineage.parentId}{lineage.parent ? '' : ' (not on this page)'}</span>}
        </> : null}
        {lineage.children.length > 0 ? <>
          {lineage.parentId ? ' · ' : ' '}
          {rollupChips(lineage.rollup).map(chip => (
            <span key={chip.key} className="chip xs">{chip.label}</span>
          ))}
          {rollupChips(lineage.rollup).length === 0
            ? `${lineage.children.length} experiment${lineage.children.length === 1 ? '' : 's'}`
            : null}
        </> : null}
        {/* An unanswered direction says so rather than rendering an empty row. */}
        {lineage.kind === CARD_KIND_DIRECTION && lineage.children.length === 0
          ? ' — no experiment proposed against this yet' : null}
      </span>
    </div>}
    {(parents.length > 0 || scoredAgainst != null) && <div className="card-kanban-fact">
      <span className="card-kanban-k">Lineage</span>
      <span>{parents.length ? `parent ${parentLineage}` : ''}
        {scoredAgainst != null ? `${parents.length ? ' · ' : ''}scored vs ${scoredLineage}` : ''}</span>
    </div>}
    {(concepts.length > 0 || novelty) && <div className="card-kanban-tags">
      {novelty && <span className="chip xs">novelty {novelty}</span>}
      {concepts.map(concept => <span key={concept} className="chip xs">{concept}</span>)}
    </div>}
    {(_cardText(card.merged_into) || _cardText(card.dropped_reason)) && <div className="card-kanban-terminal">
      {_cardText(card.merged_into) ? `Merged into ${card.merged_into}` : card.dropped_reason}
      {_cardText(card.dropped_by) ? ` · by ${card.dropped_by}` : ''}
    </div>}
    {/* Item 6. Everything here was already on the wire and rendered NOWHERE, which is why
        "why is this card here, and what came of it?" had no answer in the UI even though the
        answer shipped. `cardOrigin`/`cardLessons` own the derivations. */}
    {origin.paraphrased && <div className="card-kanban-fact">
      <span className="card-kanban-k">Seed</span>
      <span title="The immutable statement captured at card_added — the key the whole Card ledger joins on. The text above is an operator display edit over it.">{origin.seed}</span>
    </div>}
    {origin.cues.length > 0 && <div className="card-kanban-fact">
      <span className="card-kanban-k">Proposed under</span>
      <span>{origin.cues.map(cue => <span key={cue.kind} className="chip xs"
        title={`steering cue ${cue.kind}${cue.detail.length ? ` · ${cue.detail.join(' · ')}` : ''}`}>
        {cue.label}{cue.detail.length ? ` · ${cue.detail.join(' · ')}` : ''}</span>)}</span>
    </div>}
    {origin.rationale && <div className="card-kanban-fact">
      <span className="card-kanban-k">Rationale</span><span>{origin.rationale}</span>
    </div>}
    {(origin.aliases.length > 0 || origin.createdAtNode != null) && <div className="card-kanban-fact">
      <span className="card-kanban-k">Formed</span>
      <span>{origin.createdAtNode != null ? `at node ${origin.createdAtNode}` : ''}
        {/* A card that absorbed three sibling proposals looked identical to one minted alone. */}
        {origin.aliases.length > 0
          ? `${origin.createdAtNode != null ? ' · ' : ''}absorbed ${origin.aliases.join(', ')}` : ''}</span>
    </div>}
    {(beliefId || retryOf) && <div className="card-kanban-fact">
      <span className="card-kanban-k">Belief lineage</span>
      <span>{beliefId ? `belief ${beliefId}` : 'belief id unavailable'}
        {retryOf ? ` · retry of ${retryOf}` : ''}</span>
    </div>}
    {(lessons.lessons.length > 0 || lessons.unresolved.length > 0) && <div className="card-kanban-fact">
      {/* `.card-kanban-fact` is a 2-column grid whose third and later children span column 2, so
          each lesson is a DIRECT child rather than being nested in one wrapping span. */}
      <span className="card-kanban-k">Taught</span>
      {lessons.lessons.map(lesson => <span key={lesson.lessonId}>
        {lesson.statement}{lesson.outcome ? ` (${lesson.outcome})` : ''}
        {lesson.evidence.length ? ` — from ${lesson.evidence.map(nid => `#${nid}`).join(', ')}` : ''}
      </span>)}
      {/* Referenced but not distilled in THIS run's log — an earlier run's lesson carried in as a
          prior. Reported, because a card claiming fewer lessons than it cites is quietly wrong. */}
      {lessons.unresolved.length > 0 && <span className="muted">
        {lessons.unresolved.length} referenced {lessons.unresolved.length === 1 ? 'lesson was' : 'lessons were'} distilled
        outside this run and cannot be resolved to their text here.</span>}
    </div>}
    {claimRefs.length > 0 && <div className="card-kanban-fact">
      <span className="card-kanban-k">Research claims</span>
      <span>{claimRefs.slice(0, 4).map(ref => <code key={ref}>{ref}</code>)}
        {claimRefs.length > 4 ? ` · ${claimRefs.length - 4} more` : ''}
        {' · '}<a href="#/claims">Open Claims &amp; Curation</a></span>
    </div>}
    <div className="card-kanban-evidence">
      {evidence.map(nid => <button key={nid} type="button" className="btn xs ghost"
        aria-label={`Open evidence node #${nid}`} title={`evidence node #${nid}`}
        onClick={() => { onSelect?.(nid); onClose?.() }}>#{nid}</button>)}
      {bestDelta != null && <span className={'chip xs ' + (bestDelta > 0 ? 'ok' : '')}
        title="best improvement over parent among the evidence">Δ{fmt(bestDelta)}</span>}
      {evidence.length === 0 && <span className="muted">
        {evidenceKnown ? 'No evidence nodes' : 'Evidence unavailable'}</span>}
    </div>
    {onControl && !terminal && <details className="card-kanban-controls">
      <summary aria-label={`Operator controls for ${card.id}`}>Operator controls</summary>
      <form className="card-control-form" onSubmit={event => { event.preventDefault(); saveStatement() }}>
        <label><span>Display statement</span><textarea className="text card-control-statement"
          aria-label={`Display statement for ${card.id}`} rows="3" value={statementDraft}
          maxLength={4000} disabled={busy} onChange={event => setStatementDraft(event.target.value)} /></label>
        <button type="submit" className="btn xs" disabled={busy || !statementDraft.trim()
          || statementDraft.trim() === statement}>Save text</button>
      </form>
      <form className="card-control-form" onSubmit={event => { event.preventDefault(); savePriority() }}>
        <label><span>Priority (1 is highest)</span><input className="text" type="number" min="1" max="256"
          aria-label={`Priority for ${card.id}`} value={priorityDraft} disabled={busy}
          onChange={event => setPriorityDraft(event.target.value)} /></label>
        <button type="submit" className="btn xs" disabled={busy || !priorityDraft}>Pin priority</button>
      </form>
      <form className="card-control-resource" onSubmit={event => { event.preventDefault(); saveResources() }}>
        <fieldset disabled={busy}>
          <legend>Configured resource override</legend>
          <div className="card-control-resource-fields">
            <label><span>GPUs</span><input className="text" type="number" min="0" step="1"
              aria-label={`GPU count for ${card.id}`} value={gpuDraft}
              onChange={event => {
                setGpuDraft(event.target.value)
                if (event.target.value === '0') setMemoryDraft('')
              }} /></label>
            <label><span>MiB / GPU</span><input className="text" type="number" min="0" step="1"
              aria-label={`GPU memory in MiB for ${card.id}`} placeholder="inherit declared"
              value={memoryDraft} disabled={busy || gpuDraft === '0'}
              onChange={event => setMemoryDraft(event.target.value)} /></label>
          </div>
          <div className="card-control-help">Validated against the current server GPU envelope;
            blank memory inherits the declared value. Execution may still wait for local GPU admission.</div>
          <button type="submit" className="btn xs" disabled={busy || gpuDraft === ''}>Pin resources</button>
        </fieldset>
      </form>
      <div className="card-control-form">
        <button type="button" className="btn xs" disabled={busy} onClick={abandonCard}
          title="Mark only this Card’s research verdict abandoned; sibling Cards stay unchanged and this Card remains visible">
          Abandon this Card
        </button>
      </div>
      <details className="card-control-danger">
        <summary>Drop Card…</summary>
        <form className="card-control-form" onSubmit={event => { event.preventDefault(); drop() }}>
          <label><span>Reason (optional)</span><input className="text" value={dropReason} maxLength={400}
            aria-label={`Drop reason for ${card.id}`} disabled={busy}
            onChange={event => setDropReason(event.target.value)} /></label>
          <button type="submit" className="btn xs danger" disabled={busy}>Confirm drop</button>
        </form>
      </details>
      {controlsLocked && !ownPending && <div className="card-control-feedback" role="status">
        Another Card command is still being submitted for this run.</div>}
    </details>}
    {/* A SIBLING OF THE CONTROLS DISCLOSURE, NOT A CHILD, and that placement is the whole fix.
        This form lived inside `{onControl && !terminal && <details>}` while requiring
        `status === 'dropped'` — and `terminal` is `status === 'dropped' || merged_into`, so the two
        gates were mutually exclusive and the button could render for NO card. That made the entire
        reopen stack — the event type, five control-validation rows, the fold handler,
        `CONTROL.reopenCard` and the `reopenable` authority gate — unreachable from the browser, the
        THIRD unreachability in this one feature. The first two were caught and fixed while this one
        survived because every guard tests the MODEL and the dispatch text, and nothing rendered a
        dropped card and looked for the button.

        The other controls deliberately STAY hidden on a terminal card: edit / priority / resources /
        drop are all about work in flight. Reopening is the one action a stopped card still has, so
        it is the one control that must outlive `!terminal`.

        SHOWN ON A STOPPED CARD AND NOT BEHIND A DANGER DISCLOSURE: dropping ends a line of work and
        reopening resumes one, so presenting them with the same weight would be wrong in both
        directions. Until `dccad06f` a drop was TERMINAL — the card sat visible in the `dropped`
        lane, unactionable, with no event in the vocabulary that could return it. */}
    {onControl && _cardStatus(card) === 'dropped' && _cardReopenable(card)
      && <form className="card-control-form card-control-reopen"
        onSubmit={event => { event.preventDefault(); reopen() }}>
        <label><span>Reopen reason (optional)</span><input className="text" value={reopenReason}
          maxLength={400} aria-label={`Reopen reason for ${card.id}`} disabled={busy}
          onChange={event => setReopenReason(event.target.value)} /></label>
        <button type="submit" className="btn xs" disabled={busy}
          title="Put this stopped Card back on the board; the drop receipt stays in the log">
          Reopen this Card
        </button>
      </form>}
    {isRecord(controlState?.notice) && <div
      className={'card-control-feedback ' + (controlState.notice.tone || '')}
      role={controlState.notice.tone === 'error' ? 'alert' : 'status'} aria-live="polite">
      {controlState.notice.text}
      {ownPending && onRecover && <span className="card-control-recovery">
        {ownPending.commandId && <button type="button" className="btn xs ghost"
          disabled={ownPending.phase === 'checking' || ownPending.phase === 'retrying'}
          onClick={() => onRecover(card.id, 'check')}>Check</button>}
        {ownPending.retryable && <button type="button" className="btn xs ghost"
          disabled={ownPending.phase === 'checking' || ownPending.phase === 'retrying'}
          onClick={() => onRecover(card.id, 'retry')}>Retry exact command</button>}
        <button type="button" className="btn xs ghost"
          onClick={() => onRecover(card.id, 'dismiss')}>Dismiss locally</button>
      </span>}
      </div>}
    {controlError && <div className="card-control-feedback error" role="alert">{controlError}</div>}
  </article>
}

// The section that exists because the whole board was ambiguous without it. A Card is the
// research-direction aggregate — `core/cards.py`'s Card docstring is explicit that "The Card IS the
// research-direction aggregate now (1 card = 1 hypothesis)" — and `cards.py:809` declares
// `evidence: list[int]  # node ids that tested it (== node_ids)`. A LIST. So one Card owns zero or
// more experiments, and until now the board rendered a Card with exactly the vocabulary of an
// experiment (its operator, its params, its footprint, its delta) and offered its evidence node ids
// as a row of bare `#7` buttons. That reads as "this card IS node 7". It is not: node 7 is one
// attempt at the question the card asks, and a retry, a debug child or a repeat is another.
function _CardAttempts({ attempts, selectedNodeId, onOpenNode, coverage = null }) {
  const roll = cardAttemptSummary(attempts)
  return <section className="card-attempts" aria-label="Experiments for this work item">
    <h3 className="card-attempts-h">
      Experiments <span className="muted">{coverage?.label ?? roll.total}</span>
      {roll.missing > 0 && <span className="chip xs warn"
        title="these attempts are not present in the snapshot being displayed (a historical fold, or trimmed live state)">
        {roll.missing} unavailable</span>}
    </h3>
    <p className="muted card-attempts-note">
      {roll.total === 0
        // A card with no node at all is a real, reachable state, not an empty-list placeholder:
        // `engine/card_reservation.py::_record_node_less_card` mints and immediately closes a
        // rejected proposal that never gets a Node owner. Say so, or the pane reads as "still loading".
        ? 'No experiment has run for this work item yet. A Card can also close with none — a proposal the engine minted and rejected before building anything.'
        : `This work item is not itself an experiment: it is the question ${roll.total === 1
          ? 'one experiment tested' : `these ${roll.total} experiments tested`}.`}
    </p>
    {attempts.length > 0 && <ul className="card-attempt-list">
      {attempts.map(entry => {
        const node = entry.node
        // The LIFECYCLE word is what this chip has always shown, and for a settled attempt it is
        // exactly right. For an unsettled one it was the bare word `pending` — so a node three hours
        // into training read "pending" here while the Inspector, one click away, read "Training /
        // evaluating" about the same node at the same instant. Two surfaces, one node, contradictory
        // sentences. The activity projection is the same one every other surface uses, and it falls
        // back to the lifecycle word whenever it cannot place the node.
        const status = _cardText(node?.status) || 'unknown'
        const lifecycleUnsettled = status !== 'evaluated' && status !== 'failed'
        // No run state here, exactly as `workingOn` below already accepts: this pane is handed
        // attempts, not the fold. The projection then reads the server's generation-scoped
        // `node.activity` and simply cannot see a build MARKER, which is the one case it degrades
        // on — and it degrades to the lifecycle word this chip printed anyway.
        const activityLabel = node && lifecycleUnsettled
          ? nodeActivityView(node).shortLabel : null
        const statusText = activityLabel || status
        const metric = _cardNumber(node?.metric)
        const attempt = _cardInt(node?.attempt)
        // `evidence` and `owned` are kept apart on purpose (see `cardAttempts`): "this node produced
        // the verdict" and "this node was reserved for the card" are different claims, and a node
        // that is still building has only the second. Flattening them would report an in-flight
        // build as settled evidence.
        const lane = entry.evidence ? 'evidence' : 'reserved'
        return <li key={entry.nodeId}>
          <button type="button"
            className={'card-attempt' + (selectedNodeId === entry.nodeId ? ' on' : '')
              + (entry.present ? '' : ' missing')}
            aria-pressed={selectedNodeId === entry.nodeId} disabled={!entry.present}
            onClick={() => entry.present && onOpenNode?.(entry.nodeId)}
            title={entry.present
              ? `open experiment #${entry.nodeId} in the inspector`
              : `experiment #${entry.nodeId} is not present in this snapshot`}>
            <span className="card-attempt-id">#{entry.nodeId}</span>
            <span className="card-attempt-op">{_cardText(node?.idea?.operator)
              || _cardText(node?.operator) || (entry.present ? 'operator unknown' : 'unavailable')}</span>
            <span className={'chip xs' + (status === 'evaluated' ? ' ok' : status === 'failed' ? ' warn' : '')}
              title={activityLabel ? `experiment #${entry.nodeId} — ${nodeActivityView(node).label}` : undefined}>
              {entry.present ? statusText : 'not in snapshot'}</span>
            {attempt != null && <span className="muted">attempt {attempt}</span>}
            {metric != null && <span className="card-attempt-metric">{fmt(metric)}</span>}
            <span className={'chip xs' + (lane === 'reserved' ? ' warn' : '')}
              title={lane === 'evidence'
                ? 'in the Card’s evidence list — this attempt reached a terminal and fed the verdict'
                : 'reserved for this Card by its mint stamp, but not yet in the evidence list'}>{lane}</span>
          </button>
        </li>
      })}
    </ul>}
  </section>
}

// The right-hand pane. It has exactly two modes and a breadcrumb between them, rather than nesting a
// node Inspector inside a scrolling card sheet: the Card and the Node are different objects, and the
// pane says which one you are looking at instead of blurring them into one column.
// A CARD's whole story in one place: the proposal(s) that produced it, then a section per
// experiment it produced. Sections COUNT their traces and open them on demand, so a card with a
// dozen attempts stays readable — and what opens is the same surface the node Inspector's Trace tab
// is, not a lesser rendering of a different trace. The decisions (ordering, matching labels, what is
// openable at all, the omission receipt) live in `cardTraceModel.js`.
// THE trace surface, not a second one. It lives in `Inspector.jsx` beside the span tree, the
// conversation and the reach affordance it is made of, and is loaded LAZILY here: a static edge
// would pull the whole node Inspector (charts, code viewer, markdown) into the board's chunk, and
// the board is also a review-route surface. Same shape as the `OpTrace` lazy import this replaces.
const LazyTraceSurface = React.lazy(
  () => import('./Inspector.jsx').then(module => ({ default: module.TraceSurface })))
const LazyResearchTraces = React.lazy(
  () => import('./Inspector.jsx').then(module => ({ default: module.ResearchTraces })))

function _CardTrace({ card, runId, expectedGeneration, onOpenNode, attempts = [] }) {
  const [payload, setPayload] = useState(null)
  const [open, setOpen] = useState(null)
  const cardId = card?.id
  useEffect(() => {
    setPayload(null); setOpen(null)
    if (!cardId || !runId) return undefined
    let alive = true
    // `deadlineGet` returns a HANDLE — `{controller, promise, timedOut}` — not a promise. Calling
    // `.then` on it throws at runtime, which no test catches because none mounts this component and
    // the build compiles it happily.
    // FENCED like every other trace read: `traceDeadlineGet` sends `expected_generation` and the
    // response is checked with `traceGenerationMatches`. Without both, a read issued just before a
    // reset resolves after it and commits the ARCHIVED generation's payload, while every sibling
    // surface refuses the same bytes as superseded.
    //
    // The shared TRACE rule, not the generic panel timeout: this route pays the same fixed
    // per-request fence cost every other trace read does. Measured 2026-08-12 on the live run —
    // 2.2-10.1 s — against a 15 s panel budget that was marginal and a `deadlineGet` default of
    // 8 s that was not. The cost is an absent-marker `lstat` on this FUSE mount (105-950 ms, five
    // per request), not the spans.
    const request = traceDeadlineGet(
      runApiPath(runId, `/cards/${encodeURIComponent(cardId)}/trace`),
      expectedGeneration, null, 0, traceReadDeadlineMs(0))
    request.promise
      .then(d => {
        if (!alive) return
        if (!traceGenerationMatches(d, expectedGeneration)) {
          // A superseded read is not an unreadable one, and must not be shown as this card's trace.
          setPayload({ projection: { unavailable: true, superseded: true } })
          return
        }
        setPayload(d || {})
      })
      .catch(() => { if (alive) setPayload({ projection: { unavailable: true } }) })
    return () => { alive = false; request.controller.abort() }
  }, [cardId, runId, expectedGeneration])
  const sections = useMemo(() => cardTraceSections(payload), [payload])
  const notice = cardTraceNotice(payload)

  if (payload === null) return <div className="muted" role="status">loading this work item’s trace…</div>
  // The node's own generation, when this snapshot knows it. `null` is not zero: it means "whichever
  // attempt is current", which is what the routes settle an absent one to — asserting 0 for a
  // repaired node would 409 every read.
  const entryOf = nodeId => attempts.find(item => String(item.nodeId) === String(nodeId))
  const attemptOf = nodeId => {
    const attempt = entryOf(nodeId)?.node?.attempt
    return Number.isSafeInteger(attempt) && attempt >= 0 ? attempt : null
  }
  // Live-refresh only while generation-scoped activity says the node owns build/evaluation work.
  // Raw `pending` also includes the queue and cannot be used as an ownership test.
  const workingOn = nodeId => [NODE_ACTIVITY.BUILDING, NODE_ACTIVITY.EVALUATING]
    .includes(nodeActivityStatus(entryOf(nodeId)?.node))
  const fallback = <div className="muted" role="status">loading trace…</div>
  return <div className="card-trace">
    {notice && <div className="muted" role="status">{notice}</div>}
    {sections.map(section => section.kind === 'research'
      ? <div key={section.key} className="card-trace-section">
          <div className="section-h">{section.title}</div>
          {/* The same research surface the node's Trace tab shows — one implementation, so the
              proposal reads the same way whichever screen the operator arrived from. */}
          <React.Suspense fallback={fallback}>
            <LazyResearchTraces rows={section.rows} runId={runId}
              expectedGeneration={expectedGeneration} />
          </React.Suspense>
        </div>
      : <div key={section.key} className="card-trace-section">
          <div className="section-h card-trace-divider">{section.title}</div>
          <div className="card-trace-row">
            {/* Keyed by NODE, never by `node_created.trace_id`. That trace is where the node was
                authored — two spans, `Author node` → `materialize_node` — while the Developer's
                build, repairs and evaluation run in other traces entirely. Opening it here is what
                made the Developer trace look like it had disappeared: the rollup beside this button
                counts the NODE's spans (measured on rubertlite-dr-unified-v3 node 0: 61) and the
                tree below it showed 2. */}
            <button type="button" className="btn xs ghost" disabled={!section.openable}
              aria-expanded={open === section.key}
              onClick={() => setOpen(cur => (cur === section.key ? null : section.key))}>
              {open === section.key ? '▾' : '▸'} Developer · build and evaluation
            </button>
            <button type="button" className="btn xs ghost"
              onClick={() => onOpenNode?.(Number(section.node.node_id))}>open experiment ›</button>
            <span className="muted">{fmtInt(section.node.spans)} spans
              · {fmtInt(section.node.generations)} gen
              · {fmtInt(section.node.tools)} tools · {fmtInt(section.node.tokens?.total)} tok
              {section.node.errors ? ` · ${fmtInt(section.node.errors)} error` : ''}</span>
          </div>
          {open === section.key && <div className="card-trace-body">
            <React.Suspense fallback={fallback}>
              <LazyTraceSurface
                subject={nodeTraceSubject(section.node.node_id, attemptOf(section.node.node_id))}
                runId={runId} expectedGeneration={expectedGeneration} chrome="inline"
                working={workingOn(section.node.node_id)} />
            </React.Suspense>
          </div>}
        </div>)}
  </div>
}

function _CardDetailPane({
  card, receipt, attempts, selectedNodeId, onOpenNode, onSelect, onControl, controlState,
  controlsLocked, renderInspector, state, onRecover, runId = null, expectedGeneration = null,
  // The lineage view for THIS card. It defaults to `null` on `_CardKanbanCard` and the Research
  // block is gated on it, so omitting it here silently withheld "answers DIRECTION card-7" from the
  // one surface built for reading a single card in full — while the lane tiles, which are summaries,
  // showed it. That is the reverse of where a reader expects the detail.
  lineage = null,
}) {
  if (!card) {
    return <div className="card-detail card-detail-empty">
      <p className="muted">Pick a work item to see its full record — its verdict, the experiments
        that tested it, its selection gate and its operator controls.</p>
    </div>
  }
  const inspectingNode = selectedNodeId != null
    && attempts.some(entry => entry.nodeId === selectedNodeId && entry.present)
  if (inspectingNode && renderInspector) {
    return <div className="card-detail card-detail-node">
      <div className="card-detail-crumb">
        <button type="button" className="btn xs ghost" onClick={() => onOpenNode?.(null)}
          aria-label={`Back to Card ${card.id}`}>‹ {card.id}</button>
        <span className="muted">experiment #{selectedNodeId} — one attempt at this work item</span>
      </div>
      {renderInspector(selectedNodeId)}
    </div>
  }
  return <div className="card-detail">
    <_CardAttempts attempts={attempts} selectedNodeId={selectedNodeId} onOpenNode={onOpenNode}
      coverage={cardAttemptCoverage(attempts, receipt)} />
    <_CardKanbanCard card={card} receipt={receipt} presentation="full" state={state}
      controlState={controlState} controlsLocked={controlsLocked} onControl={onControl}
      onRecover={onRecover} onSelect={onSelect} onClose={null} lineage={lineage} />
    {runId && <_CardTrace card={card} runId={runId} expectedGeneration={expectedGeneration}
      onOpenNode={onOpenNode} attempts={attempts} />}
  </div>
}

function _CardKanban({
  state, cards, runId, runGeneration = null, onSelect, onClose, onToast,
  // The workspace-view extras. They stay on THIS component rather than moving the split layout into
  // a sibling because the optimistic-control state (`optim`, `cardControl`, `sentEditRef` and the
  // reconcile effect) belongs to the board, and the detail pane needs all four. Passing that bundle
  // across a module boundary is exactly the unguarded props hazard `ui/`'s guidance warns about: a
  // missing prop arrives as `undefined`, and for `disabled={controlsLocked}` that silently ENABLES a
  // mutation gate instead of failing.
  layout = 'panel', selectedCardId = null, onSelectCard = null,
  selectedNodeId = null, onSelectNode = null, renderInspector = null, pane = null,
  readOnly = false,
}) {
  const [optim, setOptim] = useState({})
  const [addDraft, setAddDraft] = useState('')
  // HOW THE BOARD IS GROUPED, and why there is a choice at all rather than a replacement. The lanes
  // answer "what is the machine doing right now"; they are the right default and nothing here
  // changes them. They cannot answer "what are we trying to find out", because that question is one
  // level up: a research DIRECTION owns no runnable action, so the fold gives it no meaningful lane
  // and the Kanban drew it among the work items as a card that would never move. Measured on
  // `runs/e5small-dr-unified-v5`, 5 of the 5 rows an operator saw were exactly that.
  // Not persisted deliberately: this is a way of LOOKING at the current board, not a preference —
  // an operator who opened the run to see what is running should find the lanes, every time.
  const [grouping, setGrouping] = useState('lanes')
  const inFlight = useRef(new Set())
  const activeRef = useRef(true)
  useEffect(() => {
    activeRef.current = true
    return () => { activeRef.current = false }
  }, [])
  // Last edit statement SUBMITTED per card id. It outlives the optimistic override (which clears on a
  // success ack before the SSE fold arrives), so a chained extend edit can baseline against the prior
  // in-flight submission instead of a stale fold — see the editBaseline capture in cardControl.
  const sentEditRef = useRef({})
  const detailCloseRef = useRef(null)
  const detailDrawerRef = useRef(null)
  const detailReturnFocusRef = useRef(null)
  const cardsById = new Map(cards.map(card => [card.id, card]))
  const cardsByIdRef = useRef(cardsById)
  cardsByIdRef.current = cardsById
  useEffect(() => {
    // Prune sentEditRef for cards no longer on the board: the ref outlives the optimistic override
    // (needed so a chained extend edit can baseline against the prior in-flight submission), so without
    // this it accumulates one entry per distinct id ever edited AND a recreated same id would inherit a
    // vanished card's last submission as a stale edit baseline. Bound it to live cards.
    for (const id of Object.keys(sentEditRef.current)) {
      if (!cardsByIdRef.current.has(id)) delete sentEditRef.current[id]
    }
    setOptim(current => {
      let changed = false
      const next = { ...current }
      for (const [id, entry] of Object.entries(current)) {
        const card = cardsByIdRef.current.get(id)
        if (!card) { delete next[id]; changed = true; continue }
        const updates = { ...(entry.updates || {}) }
        for (const kind of _CARD_CONTROL_KINDS) {
          if (updates[kind]
              && cardControlReflected(
                card, kind, updates[kind], entry.editBaseline, entry.editEventSeq)) {
            delete updates[kind]
            changed = true
          }
        }
        const pending = entry.pending && updates[entry.pending.kind] ? entry.pending : null
        if (pending !== entry.pending) changed = true
        if (Object.keys(updates).length === 0 && !pending) {
          delete next[id]
          changed = true
        } else if (changed || pending !== entry.pending) {
          next[id] = { ...entry, updates, pending }
        }
      }
      return changed ? next : current
    })
  }, [state.cards])
  // MEMOIZED, and it is not a micro-optimisation. A fresh array every render changes the identity
  // of the `cards` prop `ResearchView` receives, and that prop is the root dependency of its whole
  // memo chain — `all` -> `questions` -> `rows` (`latticeRows`, whose own REVIEW note measures up to
  // 109,600 placements on a legal 255-card payload) -> `rollups` (`latticeRollups`, a pairwise
  // comparability split per row). Every one of those recomputed on every render: each 2.5 s run
  // poll, each keystroke in the add-card draft, each optimistic-control change. The memos were
  // written and were dead weight.
  const visibleCards = useMemo(
    () => cards.map(card => _cardWithOptimisticControls(card, optim[card.id])),
    [cards, optim])
  // A 'confirmation-unknown' pending (a lost/uncertain submission) MAY never self-clear: if the intent
  // never actually landed, the fold never reflects it and the reconcile effect above never drops it (if
  // it DID land, that effect clears it normally). Because it can hang indefinitely, it must not count
  // toward the board-wide lock, or one uncertain command would freeze the controls on EVERY Card until
  // reload. The real concurrency guard is `inFlight` (released in the finally), and the stuck Card still
  // shows its own 'waiting for the live fold' notice via its own pending. Only an active transport
  // operation gates the rest of the board; an offline/delayed fold must not freeze unrelated Cards.
  const globalPending = Object.values(optim).some(
    entry => isRecord(entry?.pending)
      && ['submitting', 'checking', 'retrying'].includes(entry.pending.phase))
  const cardControl = async (card, kind, data, patch) => {
    const labels = {
      edit: { saving: 'Saving Card display text…', success: 'Card display text updated', failure: 'Could not edit Card' },
      priority: { saving: 'Pinning Card priority…', success: 'Card priority pinned', failure: 'Could not pin Card priority' },
      resources: { saving: 'Pinning Card resources…', success: 'Card resources pinned', failure: 'Could not pin Card resources' },
      drop: { saving: 'Dropping Card…', success: 'Card dropped', failure: 'Could not drop Card' },
      abandon: { saving: 'Abandoning this Card…', success: 'Card abandoned', failure: 'Could not abandon Card' },
      reopen: { saving: 'Reopening Card…', success: 'Card reopened', failure: 'Could not reopen Card' },
    }[kind]
    // A kind with no row here is REFUSED by the guard below, and it refuses with the concurrency
    // message — so a control that reaches the dispatch ladder but not this table reads to the operator
    // as "another command is in flight" forever. That is how `reopen` shipped unreachable: the event
    // type, the five control-validation rows, the fold handler and the form all landed, and one absent
    // row here meant `CONTROL.reopenCard` was never called. `_CARD_CONTROL_KINDS` is the vocabulary
    // both sides must cover; `ui/test/cardBoardGrouping.test.js` now derives this table from it.
    if (!labels || inFlight.current.size > 0) {
      const message = 'Another Card command is still being submitted for this run.'
      onToast?.(message)
      return { kind: 'pending', message }
    }
    inFlight.current.add(card.id)
    // Baseline for edit-reflection = the value the card shows JUST BEFORE this edit. Normally that is the
    // current fold, but for a CHAINED edit the prior edit may not have folded yet, so the visible fold is
    // stale (one step behind). Use the prior SUBMITTED statement when the current card is a proper prefix
    // of it (we are still catching up to that earlier edit); otherwise the card has already moved on, so
    // the current statement is right. This self-cleans: once the fold reaches the prior submission the
    // prefix test fails and we fall back to the fold. (See `cardControlReflected`.)
    let editBaseline
    if (kind === 'edit' && typeof card.statement === 'string') {
      const prior = sentEditRef.current[card.id]
      editBaseline = (typeof prior === 'string' && prior !== card.statement
        && prior.startsWith(card.statement)) ? prior : card.statement
      if (typeof patch.statement === 'string') sentEditRef.current[card.id] = patch.statement
    }
    // Only this submission's receipt may satisfy the edit fence; a chained edit resets the previous seq.
    setOptim(current => cardControlSubmission(
      current, card.id, kind, patch, editBaseline, labels.saving))
    try {
      const record = kind === 'edit'
        ? await CONTROL.editCard(runId, card.id, data.statement)
        : kind === 'priority'
          ? await CONTROL.reprioritizeCard(runId, card.id, data.priority)
          : kind === 'resources'
            ? await CONTROL.pinCardResources(runId, card.id, data.gpus, data.gpu_mem_mib)
            : kind === 'abandon'
              ? await CONTROL.abandonHypothesis(runId, card.id)
              : kind === 'reopen'
                ? await CONTROL.reopenCard(runId, card.id, data.reason)
                : await CONTROL.dropCard(runId, card.id, data.reason)
      if (!activeRef.current) return { kind: 'stale', message: 'Card board scope changed' }
      const feedback = commandFeedback(record, {
        success: labels.success, noop: `${labels.success} (already current)`,
        executing: `${labels.success} — waiting for the live fold`, failure: labels.failure,
      })
      const recordEditSeq = kind === 'edit' ? _cardInt(record?.event_seq) : null
      onToast?.(feedback.message)
      setOptim(current => {
        const entry = current[card.id]
        if (!entry) return current
        const updates = { ...(entry.updates || {}) }
        const rawCard = cardsByIdRef.current.get(card.id)
        const editEventSeq = recordEditSeq ?? entry.editEventSeq
        const reflected = cardControlReflected(
          rawCard, kind, patch, entry.editBaseline, editEventSeq)
        if (reflected) {
          delete updates[kind]
        }
        if (feedback.kind === 'error') delete updates[kind]
        const commandId = typeof record?.id === 'string' ? record.id : null
        const pending = updates[kind] && feedback.kind !== 'error'
          ? { kind, phase: 'waiting-for-fold', commandId } : null
        const notice = feedback.kind === 'error'
          ? { tone: 'error', text: feedback.message }
          : { tone: feedback.kind === 'pending' ? 'pending' : 'success', text: feedback.message }
        return { ...current, [card.id]: {
          ...entry, updates, pending, notice,
          ...(editEventSeq == null ? {} : { editEventSeq }),
        } }
      })
      return feedback
    } catch (error) {
      if (!activeRef.current) return { kind: 'stale', message: 'Card board scope changed' }
      const uncertain = error?.submissionMayHaveSucceeded === true || error?.commandUnknown === true
        || ['accepted', 'executing'].includes(error?.commandRecord?.status)
      const commandEditSeq = kind === 'edit' ? _cardInt(error?.commandRecord?.event_seq) : null
      const message = uncertain
        ? `${labels.success} may still complete — waiting for the live fold`
        : `${labels.failure}: ${error?.message || error}`
      onToast?.(message)
      setOptim(current => {
        const entry = current[card.id]
        if (!entry) return current
        const updates = { ...(entry.updates || {}) }
        if (!uncertain) delete updates[kind]
        return { ...current, [card.id]: {
          ...entry, updates,
          ...(commandEditSeq == null ? {} : { editEventSeq: commandEditSeq }),
          pending: uncertain ? {
            kind, phase: 'confirmation-unknown',
            commandId: typeof error?.commandRecord?.id === 'string'
              ? error.commandRecord.id : (typeof error?.commandId === 'string' ? error.commandId : null),
            retryable: ['failed', 'timed_out'].includes(error?.commandRecord?.status),
          } : null,
          notice: { tone: uncertain ? 'pending' : 'error', text: message },
        } }
      })
      return { kind: uncertain ? 'pending' : 'error', message }
    } finally {
      inFlight.current.delete(card.id)
    }
  }
  // The CHOREOGRAPHY (which request, which optimistic entry, the commandId re-check that drops a
  // late answer about a superseded command) stays here; the DECISION — which statuses are failed,
  // which are retryable, and what phase and tone follow — is `cardControlModel.cardControlRecovery`,
  // where `node --test` can drive its truth table. The two status lists are not the same list, and
  // inline they were a one-token edit away from offering a retry on a terminal `rejected`.
  const recoverCardControl = async (cardId, action) => {
    const entry = optim[cardId]
    const pending = entry?.pending
    if (!pending) return
    if (action === 'dismiss') {
      setOptim(current => { const next = { ...current }; delete next[cardId]; return next })
      return
    }
    if (!pending.commandId) return
    setOptim(current => ({ ...current, [cardId]: {
      ...current[cardId], pending: { ...pending, phase: action === 'retry' ? 'retrying' : 'checking' },
    } }))
    try {
      const record = action === 'retry'
        ? await retryRunCommand(runId, pending.commandId, { requestTimeoutMs: PANEL_REQUEST_TIMEOUT_MS })
        : await getRunCommand(runId, pending.commandId, { requestTimeoutMs: PANEL_REQUEST_TIMEOUT_MS })
      const feedback = commandFeedback(record, {
        success: 'Command succeeded — waiting for the live fold',
        noop: 'Command is already current — waiting for the live fold',
        executing: 'Command is still executing', failure: 'Command failed',
      })
      setOptim(current => {
        const latest = current[cardId]
        if (!latest || latest.pending?.commandId !== pending.commandId) return current
        // The verdict lives in `cardControlModel.js`: the failed and retryable status lists are
        // NOT the same list (`rejected` is terminal and must never offer a retry), and inline they
        // were a one-token edit away from an infinite retry button, shipping green.
        const verdict = cardControlRecovery(record)
        return { ...current, [cardId]: {
          ...latest,
          pending: {
            ...latest.pending, phase: verdict.phase, retryable: verdict.retryable,
          },
          notice: { tone: verdict.tone, text: feedback.message },
        } }
      })
    } catch (error) {
      setOptim(current => {
        const latest = current[cardId]
        if (!latest || latest.pending?.commandId !== pending.commandId) return current
        return { ...current, [cardId]: {
          ...latest, pending: { ...latest.pending, phase: 'confirmation-unknown' },
          notice: { tone: 'pending', text: `Exact command could not be verified: ${error?.message || error}` },
        } }
      })
    }
  }
  const projection = isRecord(state.cards_projection) ? state.cards_projection : null
  const receipts = isRecord(projection?.items) ? projection.items : {}
  const lanes = _cardLanes(visibleCards)
  const total = _cardInt(projection?.total)
  const sub = total != null && total !== cards.length
    ? `${visibleCards.length} of ${total} public work items` : `${visibleCards.length} work item${visibleCards.length === 1 ? '' : 's'}`
  // A card is born as a hypothesis (peer review): keep the "+ Add" belief affordance on the
  // authoritative Card board, not only the empty-Card fallback — otherwise the operator loses the
  // documented control the moment the first card exists. Wired to the same addHypothesis control.
  const canAdd = typeof runId === 'string' && !!runId
  const addCard = async () => {
    const s = addDraft.trim()
    if (!s) return
    const feedback = await submitCommand(CONTROL.addHypothesis(runId, s), {
      success: 'Card added', noop: 'That hypothesis was already tracked',
      executing: 'Card requested — waiting for the run', failure: 'Could not add Card',
    }, onToast)
    if (feedback.kind === 'success') setAddDraft('')
  }
  const view = layout === 'view'
  const control = typeof runId === 'string' && runId ? cardControl : null
  // Compute the Card -> Node join ONCE per render for the whole board rather than per lane card:
  // it walks `state.nodes` for the mint stamp, so doing it inside each card would be O(cards x nodes)
  // on a board the wire already lets reach 256 cards.
  const attemptsByCard = view ? cardAttemptIndex(state, visibleCards) : null
  const selectedCard = view ? resolveSelectedCard(visibleCards, selectedCardId) : null
  const closeDetails = () => {
    onSelectCard?.(null)
    window.requestAnimationFrame(() => detailReturnFocusRef.current?.focus?.())
  }
  const openDetails = (cardId, trigger) => {
    detailReturnFocusRef.current = trigger || detailReturnFocusRef.current
    onSelectCard?.(cardId)
  }
  const detailOpen = view && (!pane?.compact || !!selectedCard)
  // ESCAPE GOES THROUGH THE PRIORITY SYSTEM, like every other dialog. A raw window keydown that
  // unconditionally `preventDefault()`s and closes sat outside `DIALOG_PRIORITY` arbitration, so
  // with a nested prioritized dialog open inside `renderInspector` — the destructive trace-clear
  // confirm — Escape fired BOTH: the confirm cancelled AND the drawer unmounted it mid-interaction.
  // `useDialogFocus` also declines Escape that `defaultPrevented` already claimed, which is what
  // lets a text input inside the drawer cancel its own edit instead of dismissing the whole drawer.
  //
  // NONMODAL, matching the structurally identical drawer in `RunView.jsx` ("dialog navigation …
  // without claiming or enforcing modal containment"). `modal: true` would make
  // `isolateBackgroundFor` walk to <body> setting `inert` on every sibling — including the
  // `.workspace-scrim` close button rendered in this same parent — so click-outside-to-close would
  // be dead and the board behind it non-interactive, leaving Escape and the ⟩ button as the only
  // ways out. Nothing in the suite mounts `_CardKanban` in compact mode, so no test would say so.
  useDialogFocus(detailDrawerRef, closeDetails, !!(pane?.compact && selectedCard),
    { modal: false, priority: DIALOG_PRIORITY.NONMODAL })
  const addBar = canAdd && <div className="toolbar" style={{ marginBottom: 10, gap: 6 }}>
    <input className="text" style={{ flex: 1 }} aria-label="New hypothesis" disabled={readOnly}
      placeholder="Pose a hypothesis to test (e.g. “target is right-skewed; a log transform helps”)"
      value={addDraft} onChange={e => setAddDraft(e.target.value)}
      onKeyDown={e => { if (e.key === 'Enter') addCard() }} />
    <button className="btn sm primary" onClick={addCard}
      disabled={readOnly || !addDraft.trim()}>+ Add</button>
  </div>
  // The DIRECTIONS view. One section per research direction, its experiments nested under it, and
  // the experiments nobody filed in a bucket of their own that is never merged away — "unfiled" is
  // a fact an operator acts on, and folding it into a total would claim a coverage the run does not
  // have. The direction header wears COUNTS and never a lifecycle lane: giving a parent its
  // children's worst status parks a months-long direction in "Running" because one of two hundred
  // experiments under it is training, which is the failure the operator named before this existed.
  // ONE walk of the edges for the whole board, mirroring why `attemptsByCard` is hoisted:
  // the per-card `cardLineageView` rebuilds the index from the card list every call, so using it
  // here would be O(cards^2) on a board the wire already lets reach 256 rows.
  const lineageByCard = useMemo(() => cardLineageViews(visibleCards), [visibleCards])
  const renderCard = card => <_CardKanbanCard key={card.id} card={card}
    lineage={lineageByCard.get(card.id) || null}
    receipt={isRecord(receipts[card.id]) ? receipts[card.id] : null}
    controlState={optim[card.id]}
    controlsLocked={readOnly || (globalPending && !optim[card.id]?.pending)}
    onSelect={onSelect} onClose={onClose} onControl={control}
    presentation={view ? 'lane' : 'full'} state={view ? null : state}
    selected={view && selectedCardId === card.id} onOpen={openDetails}
    attempts={attemptsByCard?.get(card.id) || null}
    attemptCoverage={cardAttemptCoverage(
      attemptsByCard?.get(card.id) || [], receipts[card.id])} />
  // The question ladder. `visibleCards` and `renderCard` are the SAME inputs the other two views
  // draw from, so a filter or a control applied on one board reaches this one too rather than the
  // view growing its own quietly-different population.
  const researchBoard = <ResearchView cards={visibleCards} state={state} renderCard={renderCard} />
  // The lanes are a LIFECYCLE view and a question has no lifecycle of its own — see
  // `splitBoardByKind`. The questions are not dropped: the count and the way to them ride above the
  // lanes, because "five questions await an experiment" and "the board is empty" are different runs.
  const { work: laneCards, questions: laneQuestions } = splitBoardByKind(visibleCards)
  // Said where the lanes are, not where the questions went: an operator who sees fewer rows than the
  // board's own total needs the reconciliation on the surface that shrank.
  const questionNotice = laneQuestions.length > 0 && grouping === 'lanes'
    ? <div className="muted card-question-notice" role="status">
        {laneQuestions.length} research question{laneQuestions.length === 1 ? '' : 's'} not shown
        here — a question owns no experiment, so it has no lane.{' '}
        <button type="button" className="btn sm ghost" onClick={() => setGrouping('research')}>
          open the Research ladder
        </button>
      </div>
    : null
  const groupingBar = <div className="toolbar card-grouping" role="group"
    aria-label="Group the board by">
    {[['lanes', 'Lanes', 'lifecycle status — what the machine is doing now'],
      ['research', 'Research', 'the ladder of questions, each one narrowing the one above it'],
    ].map(([key, label, hint]) => <button key={key} type="button" title={hint}
      className={'btn sm' + (grouping === key ? ' primary' : '')}
      aria-pressed={grouping === key} onClick={() => setGrouping(key)}>{label}</button>)}
  </div>
  const board = <div className="card-board" role="region" aria-label="Card lifecycle kanban">
    {lanes.map(([key, label, hint]) => {
      const rows = laneCards.filter(card => _cardStatus(card) === key).sort(_cardOrder)
      const tone = _CARD_FROZEN_STATUSES.has(key) ? ` card-${key}` : ''
      const laneId = `card-lane-${encodeURIComponent(key)}`
      return <section key={key} className={'card-col' + tone} aria-labelledby={laneId}>
        <h3 id={laneId} className="card-col-h" title={hint}>
          {label} <span className="muted">{rows.length}</span>
        </h3>
        {rows.map(renderCard)}
        {rows.length === 0 && <div className="muted card-empty">—</div>}
      </section>
    })}
  </div>
  if (view) {
    // The workspace shape the modal could never have: lanes keep the whole left column (and their own
    // horizontal scroll, so six lanes at the 225px floor no longer have to fit the window), and the
    // Card's full record moves into a resizable pane on the right. `pane` carries the pane chrome
    // RunView already owns for the graph inspector — same width, same persisted `ll.sideW`, same
    // splitter, same compact drawer — so the board inherits the workspace's behaviour instead of
    // growing a second, subtly different one.
    return <div className={'main run-workspace card-workspace' + (pane?.compact ? ' compact' : '')}>
      <div className="card-lanes-wrap">
        <div className="card-lanes-head">
          <span className="muted">{sub}</span>
          <_CardProjectionNotice projection={projection} cards={visibleCards} />
          {groupingBar}
          {questionNotice}
        </div>
        {addBar}
        {grouping === 'research' ? researchBoard : board}
      </div>
      {detailOpen && pane?.compact && <button type="button" className="workspace-scrim"
        tabIndex={-1} onClick={closeDetails} aria-label="Close work item details" />}
      {detailOpen && !pane?.compact && pane?.splitter}
      {detailOpen && <aside ref={detailDrawerRef}
        className={'side card-detail-side' + (pane?.compact ? ' compact-drawer' : '')}
        style={pane?.width ? { width: pane.width } : undefined}
        tabIndex={pane?.compact ? -1 : undefined}
        data-route-focus-guard={pane?.compact ? 'true' : undefined}
        role={pane?.compact ? 'dialog' : 'complementary'} aria-label="Work item details">
        <div className="pane-grip">
          <span className="muted">{selectedCard ? selectedCard.id : 'work item'}</span>
          <span className="spacer" style={{ flex: 1 }} />
          {selectedCard && <button ref={detailCloseRef} className="btn sm ghost" title="close details"
            data-dialog-initial-focus={pane?.compact ? true : undefined}
            aria-label={`Close details for ${selectedCard.id}`}
            onClick={closeDetails}>⟩</button>}
        </div>
        {/* `runGeneration`, NOT `state?.generation`. The folded run state has no run-level
            `generation` field at all — the generation is an envelope SIBLING of `state` in the
            /api/state payload and lives in useRunState's separate generationState — so
            `state?.generation` was always undefined and every trace surface reached from this
            board ran with a dead fence: superseded reads accepted after a reset, and effects keyed
            on expectedGeneration never re-firing on a generation change. */}
        <_CardDetailPane card={selectedCard}
          receipt={selectedCard && isRecord(receipts[selectedCard.id]) ? receipts[selectedCard.id] : null}
          attempts={selectedCard ? attemptsByCard.get(selectedCard.id) || [] : []}
          selectedNodeId={selectedNodeId} onOpenNode={onSelectNode} onSelect={onSelect}
          onControl={control} renderInspector={renderInspector} state={state}
          controlState={selectedCard ? optim[selectedCard.id] : null}
          controlsLocked={readOnly || (globalPending && !(selectedCard && optim[selectedCard.id]?.pending))}
          onRecover={recoverCardControl}
          lineage={selectedCard ? lineageByCard.get(selectedCard.id) || null : null}
          runId={runId} expectedGeneration={runGeneration || null} />
      </aside>}
    </div>
  }
  // `size="board"`, not `wide`: `wide` is a READING width (~1100px) and the kanban's intrinsic
  // minimum GROWS with the data — `grid-auto-flow: column` at a 225px floor needs ~1390px for six
  // lanes, so the board overflowed its own panel at every viewport (measured 1623px of lanes
  // inside a 1070px content box). Still a percentage-capped `min()`, so the JupyterHub proxy's
  // narrower window gets a panel that fits rather than one clipped by the browser edge.
  return <Panel title="Cards" sub={sub} onClose={onClose} size="board">
    <_CardProjectionNotice projection={projection} cards={visibleCards} />
    {groupingBar}
    {questionNotice}
    {addBar}
    {grouping === 'research' ? researchBoard : board}
  </Panel>
}

const HYPOTHESIS_DELETE_STORAGE_PREFIX = 'll.hypothesis-delete.'
const HYPOTHESIS_DELETE_COMMAND_RE = /^cmd_[0-9a-f]{32}$/
const HYPOTHESIS_DELETE_PENDING = new Set(['submitting', 'accepted', 'executing'])
const HYPOTHESIS_DELETE_TERMINAL = new Set(['succeeded', 'noop', 'failed', 'timed_out', 'rejected'])
const HYPOTHESIS_DELETE_STORED = new Set([
  ...HYPOTHESIS_DELETE_PENDING, 'failed', 'timed_out', 'rejected',
])
const HYPOTHESIS_DELETE_KEYS = new Set([
  'runId', 'expectedGeneration', 'hypothesisId', 'idempotencyKey', 'commandId', 'status', 'updatedAt',
])
const canonicalHypothesisId = value => typeof value === 'string' && value === value.trim()
  && value.length > 0 && [...value].length <= 256 && !/\p{C}/u.test(value)
const ownHypothesisEntry = (collection, id) => collection
  && Object.hasOwn(collection, String(id)) ? collection[String(id)] : null
const exactHypothesisDeleteRecord = (intent, record) => !!intent && isRecord(record)
  && typeof record.id === 'string' && HYPOTHESIS_DELETE_COMMAND_RE.test(record.id)
  && (!intent.commandId || record.id === intent.commandId)
  && record.event_type === 'hypothesis_updated'
  && record.run_generation === intent.expectedGeneration
  && isRecord(record.subject) && record.subject.kind === 'hypothesis'
  && record.subject.id === intent.hypothesisId && record.subject.status === 'deleted'

const hypothesisDeleteStorage = () => {
  try { return typeof sessionStorage === 'undefined' ? null : sessionStorage } catch { return null }
}
const hypothesisDeleteStorageKey = (runId, generation) => HYPOTHESIS_DELETE_STORAGE_PREFIX
  + encodeURIComponent(`${String(runId || '')}\u0000${String(generation || '')}`)
const validHypothesisDeleteIntent = (value, runId, generation) => !!value && isRecord(value)
  && Object.keys(value).every(key => HYPOTHESIS_DELETE_KEYS.has(key))
  && value.runId === String(runId) && value.expectedGeneration === String(generation)
  && RUN_GENERATION_RE.test(value.expectedGeneration)
  && canonicalHypothesisId(value.hypothesisId)
  && typeof value.idempotencyKey === 'string' && value.idempotencyKey.length > 0
  && value.idempotencyKey.length <= 200 && !/[\u0000-\u001f\u007f]/.test(value.idempotencyKey)
  && typeof value.commandId === 'string'
  && (!value.commandId || HYPOTHESIS_DELETE_COMMAND_RE.test(value.commandId))
  && HYPOTHESIS_DELETE_STORED.has(value.status) && Number.isFinite(value.updatedAt)

function loadHypothesisDeleteIntent(runId, generation) {
  const storage = hypothesisDeleteStorage()
  if (!storage || !runId || !RUN_GENERATION_RE.test(String(generation || ''))) return null
  try {
    const parsed = JSON.parse(storage.getItem(hypothesisDeleteStorageKey(runId, generation)) || 'null')
    return validHypothesisDeleteIntent(parsed, runId, generation) ? parsed : null
  } catch { return null }
}

function inspectHypothesisDeleteRecovery(runId, generation) {
  const storage = hypothesisDeleteStorage()
  if (!storage || !runId || !RUN_GENERATION_RE.test(String(generation || ''))) {
    return { state: 'unavailable', raw: null, key: null, intent: null }
  }
  const key = hypothesisDeleteStorageKey(runId, generation)
  try {
    const raw = storage.getItem(key)
    if (raw == null) return { state: 'empty', raw: null, key, intent: null }
    const intent = loadHypothesisDeleteIntent(runId, generation)
    return intent ? { state: 'valid', raw, key, intent }
      : { state: 'damaged', raw, key, intent: null }
  } catch { return { state: 'unavailable', raw: null, key, intent: null } }
}

function clearDamagedHypothesisDeleteRecovery(recovery) {
  const storage = hypothesisDeleteStorage()
  if (!storage || recovery?.state !== 'damaged' || !recovery.key || typeof recovery.raw !== 'string') return false
  try {
    // Compare-and-clear only the unreadable envelope the operator inspected. A different tab or a
    // late command receipt wins the race and remains protected.
    if (storage.getItem(recovery.key) !== recovery.raw) return false
    storage.removeItem(recovery.key)
    return storage.getItem(recovery.key) == null
  } catch { return false }
}

function saveHypothesisDeleteIntent(intent, expectedRaw) {
  const storage = hypothesisDeleteStorage()
  if (!storage || !validHypothesisDeleteIntent(intent, intent?.runId, intent?.expectedGeneration)) return null
  try {
    const key = hypothesisDeleteStorageKey(intent.runId, intent.expectedGeneration)
    const raw = storage.getItem(key)
    if (expectedRaw !== undefined && raw !== expectedRaw) return null
    const existing = loadHypothesisDeleteIntent(intent.runId, intent.expectedGeneration)
    // A corrupt/unknown recovery envelope may still describe an accepted destructive command. Never
    // overwrite it with a fresh identity; keep the surface fail-closed until storage is repaired.
    if (raw != null && !existing) return null
    if (existing && (existing.idempotencyKey !== intent.idempotencyKey
        || existing.hypothesisId !== intent.hypothesisId
        || (existing.commandId && existing.commandId !== intent.commandId))) return null
    const serialized = JSON.stringify(intent)
    storage.setItem(key, serialized)
    const stored = loadHypothesisDeleteIntent(intent.runId, intent.expectedGeneration)
    return stored && stored.idempotencyKey === intent.idempotencyKey
      && stored.hypothesisId === intent.hypothesisId && stored.commandId === intent.commandId
      && storage.getItem(key) === serialized
      ? { storageKey: key, storageRaw: serialized } : null
  } catch { return null }
}

function clearHypothesisDeleteIntent(intent) {
  const storage = hypothesisDeleteStorage()
  if (!storage || !intent?.storageKey || typeof intent.storageRaw !== 'string') return false
  try {
    const key = hypothesisDeleteStorageKey(intent.runId, intent.expectedGeneration)
    if (intent.storageKey !== key || storage.getItem(key) !== intent.storageRaw) return false
    storage.removeItem(key)
    return storage.getItem(key) == null
  } catch { return false }
}

function _HypothesisFallback({ state, runId, runGeneration, onSelect, onClose, onToast,
  onRecoveryReleased }) {
  const [draft, setDraft] = useState('')
  // Optimistic status overrides {id: 'abandoned'|'deleted'}: the run-state round-trip that reflects a
  // control event can lag (its SSE is buffered by a proxy), so apply the click to the board AT ONCE
  // instead of leaving it looking dead for up to a minute. The real fold catches up idempotently.
  const [optim, setOptim] = useState({})
  const [deleteIntents, setDeleteIntents] = useState(() => {
    const recovery = inspectHypothesisDeleteRecovery(runId, runGeneration)
    const restored = recovery.state === 'valid' ? recovery.intent : null
    return restored ? { [restored.hypothesisId]: {
      ...restored, storageKey: recovery.key, storageRaw: recovery.raw,
      phase: 'unknown',
      releaseAllowed: false, releaseInspected: false,
      message: restored.commandId
        ? 'A saved permanent deletion needs recovery. Check this exact command before another action.'
        : 'A prior permanent deletion has an unknown outcome. Resume the exact saved request to recover it safely.',
    } } : {}
  })
  const [deleteNotices, setDeleteNotices] = useState({})
  const [damagedRecovery, setDamagedRecovery] = useState(() => {
    const inspected = inspectHypothesisDeleteRecovery(runId, runGeneration)
    return inspected.state === 'damaged' ? inspected : null
  })
  const [damagedInspected, setDamagedInspected] = useState(false)
  const deleteIntentsRef = useRef(deleteIntents)
  deleteIntentsRef.current = deleteIntents
  const deleteFlights = useRef(new Set())
  const activeRef = useRef(true)
  useEffect(() => {
    activeRef.current = true
    return () => { activeRef.current = false }
  }, [])
  const refreshDeleteRecovery = fallback => {
    const inspected = inspectHypothesisDeleteRecovery(runId, runGeneration)
    if (inspected.state === 'valid') {
      const restored = {
        ...inspected.intent, storageKey: inspected.key, storageRaw: inspected.raw,
        phase: 'unknown', releaseAllowed: false, releaseInspected: false,
        message: inspected.intent.commandId
          ? 'The saved recovery changed. Check its exact command before another action.'
          : 'The saved recovery changed. Resume its exact retained request before another action.',
      }
      const collection = { [restored.hypothesisId]: restored }
      deleteIntentsRef.current = collection
      if (activeRef.current) setDeleteIntents(collection)
      setDamagedRecovery(null)
      setDamagedInspected(false)
      return
    }
    if (inspected.state === 'damaged') {
      deleteIntentsRef.current = {}
      if (activeRef.current) setDeleteIntents({})
      setDamagedRecovery(inspected)
      setDamagedInspected(false)
      return
    }
    const current = ownHypothesisEntry(deleteIntentsRef.current, fallback.hypothesisId)
    if (!current || current.idempotencyKey !== fallback.idempotencyKey) return
    const retained = { ...current, phase: 'unknown', releaseAllowed: false,
      releaseInspected: false,
      message: 'Recovery storage changed or became unavailable. No command was sent; keep this tab open and inspect recovery again.' }
    const collection = { ...deleteIntentsRef.current, [fallback.hypothesisId]: retained }
    deleteIntentsRef.current = collection
    if (activeRef.current) setDeleteIntents(collection)
  }
  const updateDeleteIntent = (intent, patch, persist = true) => {
    const current = ownHypothesisEntry(deleteIntentsRef.current, intent.hypothesisId)
    if (!current || current.idempotencyKey !== intent.idempotencyKey
        || current.expectedGeneration !== intent.expectedGeneration) return null
    const next = { ...current, ...patch, updatedAt: Date.now() }
    const storedSnapshot = persist ? saveHypothesisDeleteIntent({
      runId: next.runId, expectedGeneration: next.expectedGeneration,
      hypothesisId: next.hypothesisId, idempotencyKey: next.idempotencyKey,
      commandId: next.commandId || '', status: HYPOTHESIS_DELETE_STORED.has(next.status)
        ? next.status : 'submitting', updatedAt: next.updatedAt,
    }, current.storageRaw) : null
    const durable = !persist || !!storedSnapshot
    const presented = durable ? {
      ...next,
      ...(storedSnapshot || { storageKey: current.storageKey, storageRaw: current.storageRaw }),
    } : {
      ...next,
      phase: 'unknown',
      message: 'The command was observed, but its updated recovery receipt could not be saved. Keep this tab open; recovery will reuse the original exact request identity.',
    }
    if (!durable) {
      refreshDeleteRecovery(current)
      return null
    }
    const collection = { ...deleteIntentsRef.current, [intent.hypothesisId]: presented }
    deleteIntentsRef.current = collection
    if (activeRef.current) setDeleteIntents(collection)
    return presented
  }
  const dropDeleteIntent = intent => {
    const current = ownHypothesisEntry(deleteIntentsRef.current, intent.hypothesisId)
    if (!current || current.idempotencyKey !== intent.idempotencyKey) return false
    if (!clearHypothesisDeleteIntent({ ...current, commandId: current.commandId || '' })) {
      updateDeleteIntent(current, {
        phase: 'unknown',
        message: 'The command settled, but its saved recovery identity could not be released. No new deletion will be sent.',
      }, false)
      return false
    }
    const collection = { ...deleteIntentsRef.current }
    delete collection[intent.hypothesisId]
    deleteIntentsRef.current = collection
    if (activeRef.current) setDeleteIntents(collection)
    onRecoveryReleased?.()
    return true
  }
  // Drop an optimistic override once the real fold REFLECTS it (deleted card gone from state; abandoned
  // card now status='abandoned'), so a stale override can't keep masking a LATER server-side reopen of
  // the same hypothesis while the board stays mounted.
  useEffect(() => {
    setOptim(o => {
      const next = Object.create(null)
      for (const [id, v] of Object.entries(o)) {
        const h = ownHypothesisEntry(state.hypotheses, id)
        if (v === 'deleted' && h) next[id] = v                          // not yet dropped by the fold
        else if (v === 'abandoned' && h && h.status !== 'abandoned') next[id] = v   // not yet reflected
      }
      return next
    })
  }, [state.hypotheses])
  const hyps = Object.values(state.hypotheses || {})
    .filter(h => ownHypothesisEntry(optim, h.id) !== 'deleted')
    .map(h => {
      const status = ownHypothesisEntry(optim, h.id)
      return status ? { ...h, status } : h
    })
  // FOREAGENT board prioritization: order cards by predicted payoff (`priority`, 0 = best;
  // unranked cards last), so the kanban shows the sort the world model chose. `ranking` carries the
  // analysis trace (reason + confidence) surfaced as a header note and per-card tooltip.
  const ranking = state.hypothesis_ranking || null
  const rankConf = ranking && typeof ranking.confidence === 'number' ? Math.round(ranking.confidence * 100) : null
  const byStatus = (s) => hyps.filter(h => (h.status || 'open') === s)
    .sort((a, b) => (a.priority ?? Infinity) - (b.priority ?? Infinity))
  const pendingDelete = Object.values(deleteIntents)[0] || null
  const deleteLocked = !!pendingDelete || !!damagedRecovery
  const add = async () => {
    const s = draft.trim()
    if (!s || deleteLocked) return
    const feedback = await submitCommand(CONTROL.addHypothesis(runId, s), {
      success: 'Hypothesis added', noop: 'That hypothesis was already tracked',
      executing: 'Hypothesis requested — waiting for the run', failure: 'Could not add hypothesis',
    }, onToast)
    if (feedback.kind === 'success') setDraft('')
  }
  const _revert = (id) => setOptim(o => { const n = { ...o }; delete n[id]; return n })
  const abandon = async (h) => {
    if (deleteLocked) {
      onToast?.('Finish checking the pending permanent deletion before another hypothesis command.')
      return
    }
    setOptim(o => ({ ...o, [h.id]: 'abandoned' }))          // reflect immediately (SSE lag)
    const feedback = await submitCommand(CONTROL.abandonHypothesis(runId, h.id), {
      success: 'Hypothesis abandoned', noop: 'Hypothesis was already abandoned',
      executing: 'Abandon requested — waiting for the run', failure: 'Could not update hypothesis',
    }, onToast)
    // NOT `kind === 'error'`: a still-`executing` command has not abandoned anything yet, so the
    // optimistic strike-through would be showing an outcome the run may still refuse.
    if (feedback.kind !== 'success') _revert(h.id)
  }
  const deleteFailure = (intent, message) => {
    dropDeleteIntent(intent)
    if (!activeRef.current) return
    setDeleteNotices(current => ({ ...current, [intent.hypothesisId]: message }))
    onToast?.(message)
  }
  const retainDeleteFailure = (intent, record, message) => {
    const retryable = ['failed', 'timed_out'].includes(record?.status)
      && record?.error?.retryable === true
    updateDeleteIntent(intent, {
      commandId: record.id, status: record.status,
      phase: retryable ? 'retryable' : 'terminal', message,
      releaseAllowed: !retryable, releaseInspected: false,
    })
    if (activeRef.current) onToast?.(message)
  }
  const deleteSuccess = (intent, message) => {
    dropDeleteIntent(intent)
    if (!activeRef.current) return
    setOptim(current => ({ ...current, [intent.hypothesisId]: 'deleted' }))
    setDeleteNotices(current => {
      const next = { ...current }; delete next[intent.hypothesisId]; return next
    })
    onToast?.(message)
  }
  const observeDeleteRecord = (intent, record) => {
    if (!record || typeof record.id !== 'string' || !HYPOTHESIS_DELETE_COMMAND_RE.test(record.id)
        || !exactHypothesisDeleteRecord(intent, record)) return null
    if (!HYPOTHESIS_DELETE_PENDING.has(record.status)) return record
    const message = record.status === 'executing'
      ? 'Permanent deletion is executing — waiting for the run.'
      : 'Permanent deletion was accepted — waiting for the run.'
    return updateDeleteIntent(intent, {
      commandId: record.id, status: record.status, phase: 'pending', message,
      releaseAllowed: false, releaseInspected: false,
    }) ? record : null
  }
  const submitDelete = async intent => {
    const hypothesisId = intent.hypothesisId
    const current = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
    if (deleteFlights.current.has(hypothesisId) || !current
        || current.idempotencyKey !== intent.idempotencyKey
        || current.expectedGeneration !== intent.expectedGeneration) return
    const durableSubmission = updateDeleteIntent(current, {
      commandId: current.commandId || '', status: current.status || 'submitting', phase: 'submitting',
      message: 'Submitting the exact saved permanent deletion…',
      releaseAllowed: false, releaseInspected: false,
    })
    if (!durableSubmission) return
    deleteFlights.current.add(hypothesisId)
    try {
      const record = await runCommand(intent.runId, 'hypothesis_updated', {
        id: intent.hypothesisId, status: 'deleted',
      }, {
        expectedGeneration: intent.expectedGeneration,
        idempotencyKey: intent.idempotencyKey,
        onRecord: next => {
          const latest = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
          if (!latest || latest.idempotencyKey !== intent.idempotencyKey) return
          observeDeleteRecord(intent, next)
        },
      })
      const currentIntent = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
      if (!currentIntent || currentIntent.idempotencyKey !== intent.idempotencyKey) return
      if (!record || !exactHypothesisDeleteRecord(currentIntent, record)) {
        const current = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
        const observedCommandId = typeof record?.id === 'string'
          && HYPOTHESIS_DELETE_COMMAND_RE.test(record.id) ? record.id : ''
        const terminalMismatch = !!record && HYPOTHESIS_DELETE_TERMINAL.has(record.status)
          && !!observedCommandId && (!current?.commandId || current.commandId === observedCommandId)
        updateDeleteIntent(intent, {
          commandId: current?.commandId || observedCommandId,
          phase: 'unknown', releaseAllowed: terminalMismatch, releaseInspected: false,
          message: 'The delete command receipt did not prove this exact run generation and hypothesis. It remains quarantined.',
        })
        if (activeRef.current) onToast?.('Permanent deletion outcome is unknown. The receipt did not prove the exact target.')
        return
      }
      const feedback = commandFeedback(record, {
        success: 'Hypothesis deleted', noop: 'Hypothesis was already deleted',
        executing: 'Delete requested — waiting for the run', failure: 'Could not delete hypothesis',
      })
      if (feedback.kind === 'success') deleteSuccess(intent, feedback.message)
      else if (feedback.kind === 'pending') {
        observeDeleteRecord(intent, record)
        if (activeRef.current) onToast?.(feedback.message)
      } else if (record.status === 'rejected') deleteFailure(intent, feedback.message)
      else retainDeleteFailure(intent, record, feedback.message)
    } catch (error) {
      const latestIntent = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
      if (!latestIntent || latestIntent.idempotencyKey !== intent.idempotencyKey) return
      const record = error?.commandRecord
      const recoveryIntent = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
      if (record && !exactHypothesisDeleteRecord(recoveryIntent, record)) {
        const savedCommandId = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)?.commandId || ''
        const commandId = savedCommandId || (typeof record.id === 'string'
          && HYPOTHESIS_DELETE_COMMAND_RE.test(record.id) ? record.id : '')
        const transportUnknown = error?.submissionMayHaveSucceeded === true
          || error?.commandUnknown === true || isTransientCommandReadError(error)
        const terminalMismatch = HYPOTHESIS_DELETE_TERMINAL.has(record.status)
          && (!savedCommandId || savedCommandId === record.id)
        const message = transportUnknown
          ? 'The exact command is temporarily unavailable. Its identity remains quarantined; check it again.'
          : 'A command receipt was returned, but it did not prove this exact run generation and hypothesis. The deletion remains quarantined.'
        updateDeleteIntent(intent, {
          commandId, phase: 'unknown', message,
          releaseAllowed: !transportUnknown && terminalMismatch, releaseInspected: false,
        })
        if (activeRef.current) onToast?.(message)
        return
      }
      if (record && ['failed', 'timed_out'].includes(record.status)) {
        const feedback = commandFeedback(record, { failure: 'Could not delete hypothesis' })
        retainDeleteFailure(intent, record, feedback.message)
        return
      }
      if (record?.status === 'rejected') {
        const feedback = commandFeedback(record, { failure: 'Could not delete hypothesis' })
        deleteFailure(intent, feedback.message)
        return
      }
      const pendingRecord = HYPOTHESIS_DELETE_PENDING.has(record?.status)
        && typeof record?.id === 'string' && HYPOTHESIS_DELETE_COMMAND_RE.test(record.id)
        && exactHypothesisDeleteRecord(recoveryIntent, record)
      const errorCommandId = [error?.commandId]
        .map(value => String(value || '')).find(value => HYPOTHESIS_DELETE_COMMAND_RE.test(value)) || ''
      const ambiguous = pendingRecord || error?.submissionMayHaveSucceeded === true
        || error?.commandUnknown === true || isTransientCommandReadError(error)
      if (ambiguous) {
        const commandId = pendingRecord ? record.id
          : (ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)?.commandId || errorCommandId)
        const status = pendingRecord ? record.status
          : (ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)?.status || 'submitting')
        const message = commandId
          ? 'Permanent deletion may still complete. Check this exact saved command; it was not replayed.'
          : 'Permanent deletion outcome is unknown. Resume this exact saved request; it reuses the same identity and cannot create a second logical deletion.'
        updateDeleteIntent(intent, {
          commandId, status, phase: 'unknown', message,
          releaseAllowed: false, releaseInspected: false,
        })
        if (activeRef.current) onToast?.(message)
      } else if (errorCommandId) {
        const message = 'The exact command identity was returned, but its target outcome could not be proved. Inspect it before releasing recovery.'
        updateDeleteIntent(intent, {
          commandId: ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)?.commandId || errorCommandId,
          phase: 'unknown', message, releaseAllowed: false, releaseInspected: false,
        })
        if (activeRef.current) onToast?.(message)
      } else {
        const feedback = record ? commandFeedback(record, {
          failure: 'Could not delete hypothesis',
        }) : null
        deleteFailure(intent, feedback?.message || `Could not delete hypothesis: ${error?.message || error}`)
      }
    } finally {
      deleteFlights.current.delete(hypothesisId)
    }
  }
  const del = async h => {
    const hypothesisId = String(h.id)
    if (!canonicalHypothesisId(hypothesisId)) {
      const message = 'This hypothesis has a non-canonical id and cannot be targeted safely. Refresh or repair the run record before deleting it.'
      setDeleteNotices(current => ({ ...current, [hypothesisId]: message }))
      onToast?.(message)
      return
    }
    if (deleteFlights.current.has(hypothesisId)
        || ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
        || Object.keys(deleteIntentsRef.current).length > 0) {
      onToast?.('A permanent hypothesis deletion is already pending. Recover that exact command first.')
      return
    }
    if (!runId || !RUN_GENERATION_RE.test(String(runGeneration || ''))) {
      const message = 'The displayed run generation is unavailable. Refresh the run before deleting a hypothesis.'
      setDeleteNotices(current => ({ ...current, [hypothesisId]: message }))
      onToast?.(message)
      return
    }
    const statement = String(h.statement || '').trim()
    if (!window.confirm(`Delete this hypothesis permanently?\n\n${statement.slice(0, 500)}\n\nThis removes it from the board and cannot be undone.`)) return
    const intent = {
      runId: String(runId), expectedGeneration: String(runGeneration), hypothesisId,
      idempotencyKey: createIdempotencyKey(), commandId: '', status: 'submitting',
      updatedAt: Date.now(), phase: 'submitting',
      message: 'Submitting one permanent deletion…',
    }
    const stored = {
      runId: intent.runId, expectedGeneration: intent.expectedGeneration,
      hypothesisId: intent.hypothesisId, idempotencyKey: intent.idempotencyKey,
      commandId: '', status: 'submitting', updatedAt: intent.updatedAt,
    }
    // Commit the exact operation identity before POST. If tab-scoped storage is unavailable, fail
    // closed: an unremembered destructive request could be replayed after close/reload.
    const storedSnapshot = saveHypothesisDeleteIntent(stored, null)
    if (!storedSnapshot) {
      const message = 'Permanent deletion was not sent because this browser could not retain its recovery identity.'
      setDeleteNotices(current => ({ ...current, [hypothesisId]: message }))
      onToast?.(message)
      return
    }
    const retainedIntent = { ...intent, ...storedSnapshot }
    deleteIntentsRef.current = { [hypothesisId]: retainedIntent }
    setDeleteIntents(deleteIntentsRef.current)
    setDeleteNotices(current => { const next = { ...current }; delete next[hypothesisId]; return next })
    await submitDelete(retainedIntent)
  }
  const resumeDelete = async intent => {
    const current = ownHypothesisEntry(deleteIntentsRef.current, intent?.hypothesisId)
    if (!current || current.commandId || deleteFlights.current.has(current.hypothesisId)
        || current.idempotencyKey !== intent.idempotencyKey) return
    if (!window.confirm(
      'Resume the exact saved permanent deletion?\n\nThis reuses the original idempotency identity and payload. It cannot create a second logical deletion.',
    )) return
    await submitDelete(current)
  }
  const checkDelete = async intent => {
    if (!intent?.commandId || deleteFlights.current.has(intent.hypothesisId)) return
    const durableCheck = updateDeleteIntent(intent, {
      phase: 'checking', message: 'Checking the exact delete command…',
      releaseAllowed: false, releaseInspected: false,
    })
    if (!durableCheck) return
    deleteFlights.current.add(intent.hypothesisId)
    try {
      const record = await getRunCommand(intent.runId, intent.commandId, {
        requestTimeoutMs: PANEL_REQUEST_TIMEOUT_MS,
      })
      const current = ownHypothesisEntry(deleteIntentsRef.current, intent.hypothesisId)
      if (!activeRef.current || !current || current.idempotencyKey !== intent.idempotencyKey
          || current.commandId !== intent.commandId) return
      if (!exactHypothesisDeleteRecord(intent, record)) {
        const terminalMismatch = HYPOTHESIS_DELETE_TERMINAL.has(record?.status)
          && record?.id === intent.commandId
        updateDeleteIntent(intent, {
          phase: 'unknown', releaseAllowed: terminalMismatch, releaseInspected: false,
          message: 'The saved command did not prove this exact run generation and hypothesis. It remains quarantined.',
        })
        return
      }
      const feedback = commandFeedback(record, {
        success: 'Hypothesis deleted', noop: 'Hypothesis was already deleted',
        executing: 'Delete requested — waiting for the run', failure: 'Could not delete hypothesis',
      })
      if (feedback.kind === 'success') deleteSuccess(intent, feedback.message)
      else if (feedback.kind === 'pending') {
        observeDeleteRecord(intent, record)
        onToast?.(feedback.message)
      } else if (record.status === 'rejected') deleteFailure(intent, feedback.message)
      else retainDeleteFailure(intent, record, feedback.message)
    } catch (error) {
      const current = ownHypothesisEntry(deleteIntentsRef.current, intent.hypothesisId)
      if (!activeRef.current || !current || current.idempotencyKey !== intent.idempotencyKey
          || current.commandId !== intent.commandId) return
      const message = isTransientCommandReadError(error)
        ? 'The exact delete command is temporarily unavailable. Its identity is retained; try checking again.'
        : 'The exact delete command could not be verified. Its identity remains quarantined; no delete was replayed.'
      updateDeleteIntent(intent, {
        phase: 'unknown', message,
        releaseAllowed: !isTransientCommandReadError(error)
          && ([403, 404].includes(Number(error?.status))
            || error?.code === 'COMMAND_PROTOCOL_ERROR'),
        releaseInspected: false,
      })
      onToast?.(message)
    } finally {
      deleteFlights.current.delete(intent.hypothesisId)
    }
  }
  const retryDelete = async intent => {
    const current = ownHypothesisEntry(deleteIntentsRef.current, intent?.hypothesisId)
    if (!current || current.phase !== 'retryable' || !current.commandId
        || deleteFlights.current.has(current.hypothesisId)
        || current.idempotencyKey !== intent.idempotencyKey) return
    if (!window.confirm(
      'Retry this exact failed permanent-deletion command?\n\nThis reuses the same durable command id; it does not submit a new delete intent.',
    )) return
    const durableRetry = updateDeleteIntent(current, {
      phase: 'retrying', releaseAllowed: false, releaseInspected: false,
      message: 'Retrying the exact durable delete command…',
    })
    if (!durableRetry) return
    deleteFlights.current.add(current.hypothesisId)
    try {
      const record = await retryRunCommand(current.runId, current.commandId, {
        requestTimeoutMs: PANEL_REQUEST_TIMEOUT_MS,
        onRecord: next => {
          const latest = ownHypothesisEntry(deleteIntentsRef.current, current.hypothesisId)
          if (!latest || latest.idempotencyKey !== current.idempotencyKey) return
          observeDeleteRecord(latest, next)
        },
      })
      const latest = ownHypothesisEntry(deleteIntentsRef.current, current.hypothesisId)
      if (!activeRef.current || !latest || latest.idempotencyKey !== current.idempotencyKey) return
      if (!exactHypothesisDeleteRecord(latest, record)) {
        const message = 'The retry receipt did not prove this exact run generation and hypothesis. Recovery remains quarantined.'
        updateDeleteIntent(latest, {
          phase: 'unknown', message,
          releaseAllowed: HYPOTHESIS_DELETE_TERMINAL.has(record?.status)
            && record?.id === latest.commandId,
          releaseInspected: false,
        })
        onToast?.(message)
        return
      }
      const feedback = commandFeedback(record, {
        success: 'Hypothesis deleted', noop: 'Hypothesis was already deleted',
        executing: 'Delete retry is still executing', failure: 'Could not delete hypothesis',
      })
      if (feedback.kind === 'success') deleteSuccess(latest, feedback.message)
      else if (feedback.kind === 'pending') {
        observeDeleteRecord(latest, record)
        onToast?.(feedback.message)
      } else if (record.status === 'rejected') deleteFailure(latest, feedback.message)
      else retainDeleteFailure(latest, record, feedback.message)
    } catch (error) {
      const latest = ownHypothesisEntry(deleteIntentsRef.current, current.hypothesisId)
      if (!activeRef.current || !latest || latest.idempotencyKey !== current.idempotencyKey) return
      const message = isTransientCommandReadError(error) || error?.submissionMayHaveSucceeded === true
        ? 'The exact retry outcome is temporarily unavailable. Its command identity remains retained.'
        : 'The exact retry could not be verified. Inspect the saved command before releasing recovery.'
      updateDeleteIntent(latest, {
        phase: 'unknown', message,
        releaseAllowed: false,
        releaseInspected: false,
      })
      onToast?.(message)
    } finally {
      deleteFlights.current.delete(current.hypothesisId)
    }
  }
  const releaseValidRecovery = intent => {
    const current = ownHypothesisEntry(deleteIntentsRef.current, intent?.hypothesisId)
    if (!current || !current.releaseAllowed || !current.releaseInspected
        || current.idempotencyKey !== intent.idempotencyKey) return
    if (!window.confirm(
      'Release this exact permanent-deletion recovery identity?\n\nThis sends no command. Only continue after inspecting the run and accepting that this old outcome cannot be proved.',
    )) return
    if (!clearHypothesisDeleteIntent({ ...current, commandId: current.commandId || '' })) {
      updateDeleteIntent(current, {
        phase: 'unknown', releaseAllowed: false, releaseInspected: false,
        message: 'The saved recovery identity changed or could not be released. It remains protected.',
      }, false)
      onToast?.('The exact recovery identity changed or could not be released.')
      return
    }
    const collection = { ...deleteIntentsRef.current }
    delete collection[current.hypothesisId]
    deleteIntentsRef.current = collection
    setDeleteIntents(collection)
    onRecoveryReleased?.()
    onToast?.('The exact recovery identity was released. No deletion was sent.')
  }
  const releaseDamagedRecovery = () => {
    if (!damagedRecovery || !damagedInspected) return
    if (!window.confirm(
      'Release this exact unreadable recovery record?\n\nOnly continue after inspecting the current run state and confirming that no permanent deletion still needs recovery.',
    )) return
    if (!clearDamagedHypothesisDeleteRecovery(damagedRecovery)) {
      onToast?.('The recovery record changed or could not be released. It remains protected; inspect it again.')
      const refreshed = inspectHypothesisDeleteRecovery(runId, runGeneration)
      if (refreshed.state === 'valid') {
        const restored = {
          ...refreshed.intent, phase: 'unknown',
          storageKey: refreshed.key, storageRaw: refreshed.raw,
          releaseAllowed: false, releaseInspected: false,
          message: refreshed.intent.commandId
            ? 'The recovery record changed to a valid permanent deletion. Check its exact command.'
            : 'The recovery record changed to a valid id-less deletion. Resume its exact saved request.',
        }
        deleteIntentsRef.current = { [restored.hypothesisId]: restored }
        setDeleteIntents(deleteIntentsRef.current)
        setDamagedRecovery(null)
      } else if (refreshed.state === 'damaged') setDamagedRecovery(refreshed)
      setDamagedInspected(false)
      return
    }
    setDamagedRecovery(null)
    setDamagedInspected(false)
    onToast?.('The exact damaged recovery record was released. No deletion was sent.')
    onRecoveryReleased?.()
  }
  return (
    <Panel title="Hypotheses" sub={`${hyps.length} tracked — what the run is trying to learn`} onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 10, gap: 6 }}>
        <input className="text" style={{ flex: 1 }} aria-label="New hypothesis"
          placeholder="Pose a hypothesis to test (e.g. “target is right-skewed; a log transform helps”)"
          value={draft} disabled={deleteLocked} onChange={e => setDraft(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') add() }} />
        <button className="btn sm primary" onClick={add} disabled={!draft.trim() || deleteLocked}>+ Add</button>
      </div>
      {pendingDelete && <div className="report-inline-state" role="status" style={{ marginBottom: 10 }}>
        <OpIcon name="alert" size={14} /><span>{pendingDelete.message}</span>
        {pendingDelete.commandId && <button className="btn sm" onClick={() => checkDelete(pendingDelete)}
          disabled={['checking', 'retrying', 'submitting'].includes(pendingDelete.phase)}>
          {pendingDelete.phase === 'checking' ? 'Checking…' : 'Check exact command'}</button>}
        {pendingDelete.commandId && pendingDelete.phase === 'retryable'
          && <button className="btn sm" onClick={() => retryDelete(pendingDelete)}>Retry exact command</button>}
        {!pendingDelete.commandId && <button className="btn sm" onClick={() => resumeDelete(pendingDelete)}
          disabled={pendingDelete.phase === 'submitting'}>
          {pendingDelete.phase === 'submitting' ? 'Submitting…' : 'Resume exact request'}</button>}
        {pendingDelete.releaseAllowed && !pendingDelete.releaseInspected
          && <button className="btn sm" onClick={() => updateDeleteIntent(pendingDelete, {
            releaseInspected: true,
          }, false)}>Inspect recovery</button>}
        {pendingDelete.releaseAllowed && pendingDelete.releaseInspected && <>
          <span className="muted">Run {pendingDelete.runId}; generation {pendingDelete.expectedGeneration.slice(0, 12)}…;
            hypothesis {pendingDelete.hypothesisId}; command {pendingDelete.commandId || 'not recorded'}.</span>
          <button className="btn sm danger" onClick={() => releaseValidRecovery(pendingDelete)}>
            Release exact recovery</button>
        </>}
      </div>}
      {damagedRecovery && <div className="report-inline-state error" role="alert" style={{ marginBottom: 10 }}>
        <OpIcon name="alert" size={14} />
        <span>An unreadable permanent-deletion recovery record exists for this exact run generation.
          Destructive controls stay locked until it is inspected and explicitly released.</span>
        {!damagedInspected
          ? <button className="btn sm" onClick={() => setDamagedInspected(true)}>Inspect recovery</button>
          : <>
            <span className="muted">Run {runId}; generation {String(runGeneration).slice(0, 12)}…;
              stored record {damagedRecovery.raw.length} bytes. Its command identity cannot be verified.</span>
            <button className="btn sm danger" onClick={releaseDamagedRecovery}>Release exact record</button>
          </>}
      </div>}
      {ranking && <div className="muted" style={{ marginBottom: 8, fontSize: 12, display: 'flex', gap: 6, alignItems: 'baseline' }}
        title={ranking.reason || 'predicted before execution'}>
        <OpIcon name="bulb" size={11} />
        <span>Predicted priority order (FOREAGENT{rankConf != null ? `, ${rankConf}% confidence` : ''})
          {ranking.reason ? `: ${ranking.reason}` : ''}</span>
      </div>}
      {hyps.length === 0
        ? <div className="muted">No hypotheses yet. The Researcher states one per experiment (its
          <code> hypothesis</code> field); deep-research directions and your “+ Add” questions land here too,
          then get tracked to a verdict as experiments run.</div>
        : <div className="hyp-board">
          {_HYP_COLUMNS.map(([key, label, hint]) => {
            const col = byStatus(key)
            return <div key={key} className={'hyp-col hyp-' + key}>
              <div className="hyp-col-h" title={hint}>{label} <span className="muted">{col.length}</span></div>
              {col.map(h => {
                const deletion = ownHypothesisEntry(deleteIntents, h.id)
                const deleteNotice = ownHypothesisEntry(deleteNotices, h.id) || ''
                return <div key={h.id} className="hyp-card">
                <div className="hyp-stmt">
                  <span className="hyp-src" title={`source: ${h.source}`}>
                    <OpIcon name={_HYP_ICON[h.source] || 'dot'} size={12} /></span> {h.statement}
                </div>
                <div className="hyp-meta">
                  {h.priority != null && <span className="chip xs" title={'predicted priority '
                    + (h.priority + 1) + (rankConf != null ? ` · ${rankConf}% confidence` : '')
                    + (ranking && ranking.reason ? ` · ${ranking.reason}` : '')}>#{h.priority + 1}</span>}
                  {(h.evidence || []).slice(0, 8).map(nid => <button key={nid} className="btn xs ghost"
                    title={`experiment #${nid}`} onClick={() => { onSelect && onSelect(nid); onClose() }}>#{nid}</button>)}
                  {h.best_delta != null && <span className={'chip xs ' + (h.best_delta > 0 ? 'ok' : '')}
                    title="best improvement over parent among the evidence">Δ{fmt(h.best_delta)}</span>}
                  {key !== 'abandoned' && <button className="btn xs ghost" title="abandon — move to the Abandoned column (keeps the record)"
                    disabled={deleteLocked} onClick={() => abandon(h)}><OpIcon name="cross" size={11} /></button>}
                  <button className="btn xs ghost danger" title="delete this hypothesis permanently (remove from the board)"
                    disabled={deleteLocked} aria-label={`Delete hypothesis ${h.id} permanently`}
                    onClick={() => del(h)}>{deletion ? 'Deleting…' : 'Delete'}</button>
                </div>
                {deleteNotice && <div className="report-inline-state error" role="alert">
                  <OpIcon name="alert" size={14} /><span>{deleteNotice}</span>
                </div>}
              </div>})}
              {col.length === 0 && <div className="muted hyp-empty">—</div>}
            </div>
          })}
        </div>}
    </Panel>
  )
}

// The Card board as a top-level WORKSPACE VIEW, beside the graph / concept tree / report.
//
// Same dispatch as `HypothesisBoard` — authoritative Cards, or the legacy hypothesis fallback for a
// pre-Card log — but the fallback is rendered through `PanelPresentationContext` = 'page'. That is
// the existing seam for "this component is the whole screen, not a detour over one": it drops the
// `aria-modal` dialog wrapper, which would otherwise make the view-toggle in the header inert while
// a *view* was showing.
export function CardWorkspace({
  state, runId, runGeneration, onSelect, onToast,
  selectedCardId, onSelectCard, selectedNodeId, onSelectNode, renderInspector, pane,
  readOnly = false,
}) {
  const [, setRecoveryEpoch] = useState(0)
  // MEMOIZED, because `cardRows` returns a fresh array of freshly-spread objects every call.
  // The `visibleCards` memo below (and, through it, the whole `all -> questions -> rows ->
  // rollups` chain in ResearchView) deps on this array's IDENTITY, so an unmemoized call here
  // busted it on every parent re-render — a card click, a pane change, a `readOnly` toggle —
  // and not only when `state` actually moved. That is the entire lattice cost, per click.
  const cards = useMemo(() => _cardRows(state), [state])
  const projection = isRecord(state?.cards_projection) ? state.cards_projection : null
  const hasAuthoritativeCards = cards.length > 0 || (_cardInt(projection?.total) ?? 0) > 0
    || projection?.source_valid === false
  const recovery = inspectHypothesisDeleteRecovery(runId, runGeneration)
  const recoveryVisible = recovery.state === 'valid' || recovery.state === 'damaged'
  const scopeKey = `${runId || ''}:${runGeneration || ''}`
  if (hasAuthoritativeCards && !recoveryVisible) {
    return <_CardKanban key={`cards:${scopeKey}`} state={state} cards={cards} runId={runId}
      runGeneration={runGeneration}
      layout="view" onSelect={onSelect} onClose={null} onToast={onToast}
      selectedCardId={selectedCardId} onSelectCard={onSelectCard}
      selectedNodeId={selectedNodeId} onSelectNode={onSelectNode}
      renderInspector={renderInspector} pane={pane} readOnly={readOnly} />
  }
  return <div className="main card-workspace-legacy">
    <PanelPresentationContext.Provider value="page">
      <_HypothesisFallback key={`hypotheses:${scopeKey}`} state={state} runId={runId}
        runGeneration={runGeneration} onSelect={onSelect} onClose={() => {}} onToast={onToast}
        onRecoveryReleased={() => setRecoveryEpoch(value => value + 1)} />
    </PanelPresentationContext.Provider>
  </div>
}

export function HypothesisBoard({ state, runId, runGeneration, onSelect, onClose, onToast }) {
  const [, setRecoveryEpoch] = useState(0)
  // MEMOIZED, because `cardRows` returns a fresh array of freshly-spread objects every call.
  // The `visibleCards` memo below (and, through it, the whole `all -> questions -> rows ->
  // rollups` chain in ResearchView) deps on this array's IDENTITY, so an unmemoized call here
  // busted it on every parent re-render — a card click, a pane change, a `readOnly` toggle —
  // and not only when `state` actually moved. That is the entire lattice cost, per click.
  const cards = useMemo(() => _cardRows(state), [state])
  const projection = isRecord(state?.cards_projection) ? state.cards_projection : null
  // A non-empty/omitted/invalid Card projection is authoritative. With no Cards at all, preserve the
  // hypothesis add/abandon workflow for older logs and for a run before its first Card is minted.
  // Both operator affordances now live on the authoritative Card board too: `+ Add` (below) and
  // `Abandon this Card` (the per-card `abandon` control emitting hypothesis_updated(status=abandoned)),
  // so an ordinary cards-only run — where this fallback is unmounted after the first Card — still
  // exposes them; this fallback remains only for the pre-first-Card / legacy-hypotheses shape.
  const hasAuthoritativeCards = cards.length > 0 || (_cardInt(projection?.total) ?? 0) > 0
    || projection?.source_valid === false
  // Card ids can repeat across runs and after an in-place reset. Remount every optimistic/ref tracker
  // at that exact scope boundary; the child also ignores completions after unmount.
  const scopeKey = `${runId || ''}:${runGeneration || ''}`
  const recovery = inspectHypothesisDeleteRecovery(runId, runGeneration)
  const recoveryVisible = recovery.state === 'valid' || recovery.state === 'damaged'
  return hasAuthoritativeCards && !recoveryVisible
    ? <_CardKanban key={`cards:${scopeKey}`} state={state} cards={cards} runId={runId}
      runGeneration={runGeneration} onSelect={onSelect}
      onClose={onClose} onToast={onToast} />
    : <_HypothesisFallback key={`hypotheses:${scopeKey}`} state={state} runId={runId}
      runGeneration={runGeneration}
      onSelect={onSelect} onClose={onClose} onToast={onToast}
      onRecoveryReleased={() => setRecoveryEpoch(value => value + 1)} />
}
