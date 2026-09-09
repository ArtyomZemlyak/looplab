"""What a proposal role SEES: the state brief and the hypothesis-board prompt window (doc 25 AG-02).

One responsibility, split out of `roles.py` whole: the board window bounds, the two card builders
both prompts share, the `card_id`/`parent_card_id` binding that resolves what a proposal claimed,
and `_state_brief` — the PUSH channel every proposal, triage and repair-critic turn opens with.

It is the half of `roles.py` that reached ACROSS packages, and the imports that do it stay
function-local inside `_state_brief` for the reasons recorded there: `search` imports `agents` at
module level, so `agents -> search` may only ever be a deferred import, and `agents -> events` is a
declared deferred edge (`tests/test_package_layering.py::DEFERRED`, whose reason for that pair now
names this module).

`roles.py` re-exports every name here (`from looplab.agents.roles import _state_brief` is spelled
by `engine/crash_repair.py` and `engine/node_build.py`, and `BOARD_SEED_CHARS_MAX` /
`bind_idea_to_board_card` / `next_board_prompt_cards` by `search/foresight.py`), so both spellings
name the SAME objects.
"""
from __future__ import annotations

import json
from typing import Optional

from looplab.core.advisory_payloads import memo_snapshot_cue, memo_verdict_cue
from looplab.core.models import (Idea, Node, RunState, card_drift_brief, card_is_direction,
                                 hypothesis_statement_digest)


# THE BOARD PROMPT WINDOW, named once. These bounds were four sets of bare literals across three
# modules — both builders below, `search/foresight.py`'s prioritization window, and
# `engine/research_cadence.py::DEEP_RESEARCH_OPEN_BELIEF_CAP`, whose docstring justified its value by
# reading THIS file's `5`. So raising the window here silently invalidated the writer-side cap that
# bounds how many beliefs a memo may register, and nothing went red.
#
# Only the two genuinely shared bounds live here. The TOTAL char budgets stay local to each surface
# (20k for the claimable window, 8k for the context-only one, 21k for foresight's ranking call): they
# are different budgets for different prompts and collapsing them would assert a sameness that is not
# there.
BOARD_SEED_CHARS_MAX = 4_000    # a single seed statement larger than this is skipped, not truncated
BOARD_PROMPT_CARDS = 5          # whole rows either builder will show — the number the writer cap reads
# The prompt budget the window spends on seed statements. Named for the same reason the row
# count is: it bounds what the model SEES, and a bare literal here reads as incidental.
BOARD_PROMPT_SEED_BUDGET_CHARS = 20_000

def next_board_prompt_cards(
    state: RunState, hyp_order: Optional[list[str]] = None, *, attempt: int = 0,
) -> list:
    """Return a fair, whole-item Card prompt window (five Cards / 20k seed characters)."""
    cards = list(state.open_research_beliefs())
    if not cards:
        return []
    if hyp_order:
        position = {card_id: index for index, card_id in enumerate(hyp_order)}
        cards.sort(key=lambda card: (position.get(card.id, len(position)), card.id))
        # DERIVED from BOARD_PROMPT_CARDS, not written out. The window size is the constant three
        # lines up — which exists precisely because these bounds used to be bare literals — so a
        # hardcoded 5/4 here would leave the tail-rotation fairness matching a window size the
        # builder no longer uses the moment anyone raises it, silently and with no test to notice.
        if len(cards) > BOARD_PROMPT_CARDS:
            leaders, tail = cards[:BOARD_PROMPT_CARDS - 1], cards[BOARD_PROMPT_CARDS - 1:]
            offset = attempt % len(tail)
            cards = leaders + tail[offset:] + tail[:offset]
    elif len(cards) > 1:
        offset = attempt % len(cards)
        cards = cards[offset:] + cards[:offset]
    selected = []
    used = 0
    for card in cards:
        seed = card.seed_statement or ""
        if (not seed or len(seed) > BOARD_SEED_CHARS_MAX
                or used + len(seed) > BOARD_PROMPT_SEED_BUDGET_CHARS):
            continue
        selected.append(card)
        used += len(seed)
        if len(selected) == BOARD_PROMPT_CARDS:
            break
    return selected


def attempted_board_prompt_cards(state: RunState, shown=(), *,
                                 limit: int = BOARD_PROMPT_CARDS) -> list:
    """The board rows a proposer must CHECK AGAINST: research questions that already have work.

    `next_board_prompt_cards` above shows only `open_research_beliefs()` — open, **untested** cards,
    i.e. the ones with NO evidence (`core/models.py`). So the moment a card gets a node, the question
    it asks vanishes from the proposal prompt entirely, and the model is asked what to try next while
    unable to see what the board already asks. Measured in `runs/rubertlite-dr-unified-v5`: at node
    2's `propose` the board held card-0 and card-1, both with a node in flight, both therefore
    filtered out — the rendered user turn contains no board section at all, and its only trace of two
    hours of running work is the headline "Search so far — 2 experiment(s), 0 failed:" with nothing
    under it (`experiments_digest` lists winners and failures, and a PENDING node is neither).

    This is the other half of the same window and it is deliberately a SEPARATE list, not a widening
    of the untested one: that list carries the contract "return its CARD_ID in `card_id`", which
    binds the proposal to the card and restores its seed (`bind_idea_to_board_card`). These rows must
    never be claimable that way — their work item is already owned, and a claim would either be
    refused at the reservation fence or, worse, re-seed a fresh proposal with a statement someone
    else's node is already testing. They are here to be READ. `_state_brief` says so in exactly those
    terms; see the position it takes there for why it does not offer a repair instead.

    LIVE work only. The first version filtered on `research_cards()` alone, so a DROPPED or ABANDONED
    card kept appearing under "do NOT propose one of these again as if it were new" — which reads a
    deliberate operator drop as a claim on the direction and fences off the one question the
    operator most likely wants re-scoped. A closed work item is history, and history is
    `experiments_digest`'s job.

    Grouped by BELIEF, like its sibling. `open_research_beliefs()` collapses two cards that share a
    `belief_id` into one row; doing anything else here would let a repair's card and the card it
    attached to render as two separate "already attempted" questions, which is precisely the
    duplicate the attach exists to remove.

    Bounded like its sibling and by the same rule (whole items only, never a truncated statement), at
    half the character budget because this half is context rather than the actionable queue. Most
    RECENT first-shown-last: the tail is what the model reads closest to its instruction, and the
    newest work is the likeliest thing it is about to repeat.
    """
    already = {getattr(card, "id", None) for card in (shown or ())}
    shown_beliefs = {getattr(card, "belief_id", None) for card in (shown or ())} - {None}
    rows = []
    seen_beliefs: set = set()
    for c in state.research_cards():
        if c.id in already or not (c.seed_statement or "").strip():
            continue
        if not (c.evidence or c.status in {"building", "running", "evaluated"}):
            continue
        if (c.status in {"dropped", "gated"} or c.verdict == "abandoned"
                or c.dropped_reason is not None):
            continue
        belief = c.belief_id or hypothesis_statement_digest(c.seed_statement)
        if belief in shown_beliefs or belief in seen_beliefs:
            continue
        seen_beliefs.add(belief)
        rows.append(c)
    selected: list = []
    used = 0
    for card in reversed(rows):
        seed = card.seed_statement or ""
        if len(seed) > BOARD_SEED_CHARS_MAX or used + len(seed) > 8_000:
            continue
        selected.append(card)
        used += len(seed)
        if len(selected) == limit:
            break
    return list(reversed(selected))


def board_prompt_lines(state: RunState, hyp_order: Optional[list[str]] = None,
                       board_cards: Optional[list] = None, *,
                       for_proposal: bool = True) -> list[str]:
    """The board a prompt must read before it names a direction — BOTH halves, ONE vocabulary.

    Extracted from `_state_brief` so the deep-research memo prompt renders the SAME rows in the SAME
    spelling (`CARD_ID=`/`BELIEF_ID=`/`SEED_STATEMENT_JSON=`) rather than a second board vocabulary
    nobody could compare against the first. The proposal path grew this block when a `debug` retry
    was found minting a twin card; the RESEARCH path never got it, and that is the source measured in
    `runs/rubertlite-dr-unified-v6`: four deep-research memos, 18 `hypothesis_added` events, five
    distinct ideas, and a board of eleven cards — including three re-wordings of the very card that
    was running at the time. The memo prompt's user turn (recovered from that run's `spans.jsonl`)
    contained goal, node counts, a coverage receipt and an `experiments:` list, and NOTHING about the
    board those memos had themselves filled.

    `for_proposal` carries the claim contracts, which only a caller whose answer is an `Idea` can
    honour — see the two blocks below. Everything else (crash triage, the macro-action chooser, the
    deep-research memo) reads the same CONTENT with no contract attached.
    """
    lines: list[str] = []
    # P1: surface OPEN board hypotheses (human "+ Add" / deep-research directions) verbatim.
    # Without this the Researcher never sees them, and evidence only links when an experiment's
    # `hypothesis` matches the statement exactly — so board cards would stay "open" forever.
    # Read distinct open, untested BELIEFS from the Card work-item board. Card fields retain the old
    # Hypothesis-facing vocabulary, but multiple work items may share a `belief_id`; the helper below
    # collapses them before prompting. `seed_statement == statement` only until an operator edit or merge.
    # This feed DELIBERATELY shows the immutable `seed_statement` (not the display `statement`) and asks
    # the model to copy it EXACTLY: evidence links only when the built node's `idea.hypothesis` matches the
    # card's SEED — `_derive_cards` bridges `hypothesis_id(seed)` to the owning card id via
    # owner_by_statement (that hash EQUALS the card id only for a legacy hypothesis-shadow card, NOT for a
    # native `card-N`). Copying an edited/merged `statement` would hash elsewhere, so no card owns it and it
    # gains no evidence. Consequence (by design): an operator statement edit changes render/analysis/
    # selection (which read `statement`) but NOT the seed the proposal feed asks the model to test — a
    # display/analysis edit, not a re-seed of the linkable research direction.
    # Distinct untested BELIEFS, not raw work-item cards (peer review): two cards that reuse the exact
    # hypothesis wording are ONE belief, surfaced once so the model does not re-read a duplicate.
    open_hyps = board_cards if board_cards is not None else next_board_prompt_cards(
        state, hyp_order=hyp_order)
    if open_hyps:
        # FOREAGENT predict-before-execute (search/foresight.py): when the world model has ranked the
        # board by expected payoff (`hyp_order` = hypothesis ids best-first), surface the batch of
        # untested beliefs — which arrives from deep research / a human / the strategist — in that
        # predicted-value order, so the search tests the most promising one first and the [:5] cap now
        # drops the LOWEST-payoff cards, not arbitrary insertion-order ones. No ranking -> insertion
        # order (unchanged). Replay-safe: only the resulting node's `idea.hypothesis` is recorded.
        # TWO KINDS OF ROW, TWO DIFFERENT CONTRACTS, and until 2026-08-24 they were one list.
        # `open_research_beliefs()` returns every open untested card, and a card is either an
        # EXPERIMENT (it owns an executable action, so it can be claimed and built) or a
        # DIRECTION (it owns none — a deep-research `recommended_direction`, an operator's broad
        # question — so it can NEVER be built, no matter what the model returns). Rendering them
        # together offered a claim contract that is false for half the rows: measured on
        # `runs/e5small-dr-unified-v5`, 5 of 5 rows the proposer saw were directions, and a
        # `card_id` naming any of them resolves to a card that owns no action.
        #
        # The split gives the direction the contract it CAN honour: not "claim this", but "propose a
        # minimal-change experiment that ANSWERS this, and file it under the direction". That is
        # what turns a direction from a row that clogs the board into the source of the next
        # experiment — and it is the only way the `Card.parent_card_id` edge is ever authored,
        # since nothing can infer from a proposal's text which question it was written to answer.
        directions = [card for card in open_hyps if card_is_direction(card)]
        work_items = [card for card in open_hyps if not card_is_direction(card)]
        if directions:
            lines.append("OPEN RESEARCH DIRECTIONS (broad questions with no experiment yet"
                         + (", ordered by predicted payoff — best first" if hyp_order else "")
                         + " — these are NOT experiments and cannot be run as they stand):")
            for card in directions:
                lines.append(
                    f"- DIRECTION_ID={card.id} "
                    f"SEED_STATEMENT_JSON={json.dumps(card.seed_statement, ensure_ascii=False)}")
            if for_proposal:
                lines.append(
                    "To pursue one, propose ONE concrete minimal-change experiment that would move "
                    "it forward and return its DIRECTION_ID in `parent_card_id`. Do NOT put a "
                    "DIRECTION_ID in `card_id` — a direction owns no action and cannot be claimed. "
                    "Several experiments may be filed under the same direction over time; that is "
                    "what it is for.")
        if work_items:
            lines.append("Untested hypotheses on the board (registered by the operator or deep research"
                         + (", ordered by predicted payoff — best first" if hyp_order else "")
                         + " — none has evidence yet):")
            for card in work_items:
                lines.append(
                    f"- CARD_ID={card.id} BELIEF_ID={card.belief_id or ''} "
                    f"SEED_STATEMENT_JSON={json.dumps(card.seed_statement, ensure_ascii=False)}")
            if for_proposal:
                # A CLAIM CONTRACT, and only a caller whose answer is an `Idea` can honour it. This brief
                # also feeds the crash-triage judge (`engine/crash_repair.py`) and the macro-action
                # chooser (`engine/node_build.py`), whose replies are a verdict string and an index —
                # neither has a `card_id` field to return one in, so for them this sentence is an
                # instruction that cannot be followed, competing with the one that can.
                lines.append("If your next experiment tests one of these, return its CARD_ID in "
                             "`card_id`. The engine restores the complete immutable seed; do not use "
                             "display edits as semantic identity.")
    # …and the OTHER half of the board, which nothing showed until now: the questions that already
    # have an experiment against them. See `attempted_board_prompt_cards` for what it cost that a
    # card disappeared from this prompt the instant it got a node — including a node still RUNNING,
    # which no other part of this brief renders either. This block is advisory context, NOT a
    # claimable queue: it deliberately does not carry the "return its CARD_ID" contract above,
    # because these work items are owned. A NODES list with no verdict yet is work IN FLIGHT.
    attempted = attempted_board_prompt_cards(state, open_hyps)
    if attempted:
        lines.append("Research questions ALREADY on the board (each already has an experiment — "
                     "do NOT propose one of these again as if it were new):")
        for card in attempted:
            # …AND WHETHER THE EXPERIMENT THAT RAN IS STILL THE ONE THIS CARD PROPOSED. The arbiter
            # existed and nothing consumed it, which made this block quietly dangerous: a card's
            # `params` is the receipt-bound PROPOSAL, and under `params_style: "none"` the Developer
            # realises the idea by editing the repo, so a repair that fits a training into memory
            # moves the numbers while the card keeps the old ones. A proposer reading "already
            # tried" and sizing its next idea one knob off THAT is sizing it off a recipe nothing
            # ever ran. Measured on `runs/e5small-dr-unified-v4`: six of the nine cards with an
            # applied record disagree with their own proposal, the run's CHAMPION among them —
            # card-132 says batch 4096 / lr 0.001 / 3 epochs and node 13 ran 2048 / 0.0005 / ONE
            # epoch. Silent when the two agree, so the loud case stays loud.
            drift = card_drift_brief(card)
            lines.append(
                f"- CARD_ID={card.id} BELIEF_ID={card.belief_id or ''} "
                f"STATUS={card.status} VERDICT={card.verdict} "
                f"NODES={sorted(card.evidence)} "
                + (f"{drift} " if drift else "")
                + f"SEED_STATEMENT_JSON={json.dumps(card.seed_statement, ensure_ascii=False)}")
        if for_proposal:
            # THE PROMISE THAT WAS MADE HERE AND NEVER EXISTED, removed rather than implemented, and
            # the choice is deliberate. It read: "If one of these genuinely needs another attempt,
            # say so in `rationale` and name the CARD_ID — the engine decides whether that becomes a
            # repair under the same card." Nothing decides that. `bind_idea_to_board_card` is handed
            # ONLY `next_board_prompt_cards`, so a CARD_ID from this block resolves to no visible
            # card and is NULLED; no code path parses `rationale` for a card id; and the one thing
            # that does attach a re-attempt (`card_reservation.py::_retry_attach_card`) fired on the
            # POLICY's `debug` action against a failed node, never on a proposal — and since F5
            # deleted the Debug node (2026-08-13) it fires on nothing at all, which makes this block
            # MORE load-bearing, not less: it is now the only thing standing between a model asking
            # for "another attempt" and a second card. Driven: a
            # byte-identical seed offered back through this block still minted a second card — the
            # very card-0/card-3 shape this whole change removed, now reachable THROUGH the prompt
            # that describes the fix.
            #
            # Making it true would mean letting a proposal claim an owned work item, which is the
            # one thing the two-block split exists to prevent: an attach is only safe against a
            # FAILED LEAF whose belief digest matches, and a model asking for "another attempt"
            # cannot establish either — the engine can, and does, without being asked. So the block
            # says what is true: these are context, the retry decision is not the proposer's.
            lines.append("Propose a DIFFERENT question. These rows are NOT claimable — a CARD_ID "
                         "from this list is ignored. A failed experiment is re-attempted by the "
                         "engine itself, under the same card, without being asked.")
    return lines


def bind_idea_to_board_card(idea: Idea, cards: list) -> Idea:
    """Resolve a model claim to a visible Card and restore its immutable semantic seed.

    TWO independent edges, resolved against the SAME visible set. `card_id` is a CLAIM on a work
    item — the model says "this experiment IS that board row" — and it is nulled when it names
    nothing visible. `parent_card_id` is a FILING: "this experiment answers that research
    direction". They are resolved separately because they can be right or wrong independently, and
    because a direction is exactly the row a `card_id` claim must NOT resolve to (it owns no action,
    so claiming it would hand the engine an unbuildable work item — the failure the two prompt
    blocks were split to prevent).

    A direction named in `card_id` is NULLED, not re-routed into `parent_card_id`. Re-routing would
    read better on the case where the model plainly meant to file — but it infers intent from a
    field the prompt explicitly told it not to use, and it would mint a link no one authored while
    looking like one the model chose. Nulling is what this function already does with any `card_id`
    it cannot honour: the proposal keeps its OWN hypothesis (a claim overwrites it with the card's
    seed statement) and the experiment stands as a root.
    """
    by_id = {card.id: card for card in cards}
    # RESOLVE THE CLAIM FIRST — the self-edge test must compare against the card this proposal will
    # actually be bound to, not the id it happened to type. A proposal that names no `card_id` and
    # is matched to a card by its SEED STATEMENT can name that same card as its parent, and against
    # the raw `idea.card_id` (None) the guard sees no self edge and emits `card_id ==
    # parent_card_id` into the durable payload for the fold to drop silently.
    chosen = by_id.get(idea.card_id) if idea.card_id else None
    if chosen is None and idea.hypothesis:
        matches = [card for card in cards if card.seed_statement == idea.hypothesis]
        chosen = matches[0] if len(matches) == 1 else None
    # A DIRECTION IS NEVER A CLAIM — said in the docstring above, said in the prompt block that
    # renders directions, and until 2026-08-26 enforced NOWHERE: visibility was the only test, so
    # both resolution paths above could hand back a row that owns no action. Placed AFTER both of
    # them because they are two ways of reaching the same wrong row, and BEFORE the self-edge test
    # below because that ordering is the entire fix.
    #
    # THE SEED FALLBACK IS THE DANGEROUS PATH, and it fires on a COMPLIANT proposal. The direction
    # block instructs the model to "propose ONE concrete minimal-change experiment that would move
    # it forward and return its DIRECTION_ID in `parent_card_id`"; a model that does exactly that
    # and echoes the direction's wording as its `hypothesis` matched the direction HERE, and the
    # self-edge guard then saw `parent.id == chosen.id` and nulled the parent. Driven at 7d406cc2:
    # `parent_card_id="card-7"` in, `card_id='card-7' parent_card_id=None` out. The filing became a
    # claim on the question and the direction->experiment edge (#66, live on v7) was destroyed on
    # the one path the prompt actively invites. Nulling `chosen` first is what lets the parent live.
    if chosen is not None and card_is_direction(chosen):
        chosen = None
    parent = by_id.get(idea.parent_card_id) if idea.parent_card_id else None
    # A card names its parent, never itself. The fold refuses a self edge anyway, but nulling it
    # here keeps the durable payload from carrying a link the board will silently drop.
    if parent is not None and chosen is not None and parent.id == chosen.id:
        parent = None
    parent_update = ({} if (parent.id if parent else None) == idea.parent_card_id
                     else {"parent_card_id": parent.id if parent else None})
    if chosen is not None:
        return idea.model_copy(update={
            "card_id": chosen.id,
            "hypothesis": chosen.seed_statement,
            **parent_update,
        })
    if idea.card_id is not None or parent_update:
        return idea.model_copy(update={
            **({"card_id": None} if idea.card_id is not None else {}), **parent_update})
    return idea


def _state_brief(state: RunState, parent: Optional[Node], digest_cap: int = 0,
                 hyp_order: Optional[list[str]] = None, board_cards: Optional[list] = None,
                 *, for_proposal: bool = True, memo_verdicts: bool = False) -> str:
    # Function-local for the same reason as the `experiments_digest` import below: `agents` may not
    # take a module-level edge on `events`. `unscored_metric_clause` is the ONE spelling of "the
    # eval refused to produce this number" (doc 53 §4a) — the headline count and this line are two
    # renders of one fact and must not drift into two vocabularies.
    from looplab.events.digest import unscored_metric_clause
    best = state.best()
    lines = [f"Goal: {state.goal}", f"Optimize direction: {state.direction}."]
    # THE COORDINATES THAT RAN, not the ones that were asked for. `Idea.params` is a PROPOSAL, and
    # under `params_style: "none"` the Developer realises it by editing the repo — so a repair that
    # fits a run into memory moves the numbers while the proposal stays frozen. These two lines fed
    # the proposal to every proposal cycle: on `runs/e5small-dr-unified-v4` the Researcher was told
    # its champion ran `batch_size 8192 / accum 2 / n_epochs 15` for four days, when node 3 had
    # applied `4096 / 4 / 3`. Every idea sized "one knob off the champion" was sized off a recipe
    # that never existed. `node_params_brief` puts the applied value first and the proposal in
    # brackets beside the ones that moved.
    from looplab.core.param_carriers import node_params_brief
    if best is not None:
        lines.append(f"Best so far: node {best.id} metric={best.metric} "
                     f"params={node_params_brief(best)}"
                     + unscored_metric_clause(best))
    if parent is not None:
        lines.append(f"Refine from node {parent.id}: params={node_params_brief(parent)} "
                     f"metric={parent.metric}"
                     + unscored_metric_clause(parent))
    # PART V (B): a delta author cannot subtract from an invisible reference. Surface the run base and
    # effective primary-parent membership, bounded so a malformed taxonomy cannot consume the role context.
    # Replay uses the union of all actual parents for a merge; the proposal role sees the primary parent
    # before policy finalizes that edge set, so the prompt names this limitation instead of claiming exactness.
    # recorded taxonomy is data, never an instruction; the shared projector quotes/bounds it.
    # DEFERRED ON PURPOSE — this is the cycle-breaking import (doc 25 AG-07). `search` imports
    # `agents` at MODULE level in five places (forward_hints, WrapsDeveloper, the speculation
    # constants), so the only direction left for `agents -> search` is a function-local import.
    # Hoisting this to module scope closes the loop into an ImportError at startup.
    from looplab.search.concept_projection import (bounded_untrusted_concept_json,
                                                    concept_inheritance_context)
    concept_context = concept_inheritance_context(state, parent.id if parent is not None else None)
    lines.append("UNTRUSTED_RECORDED_CONCEPT_DATA="
                 + bounded_untrusted_concept_json(concept_context))
    if not concept_context["delta_safe"]:
        lines.append(
            "Concept authoring safety: inherited membership is UNAVAILABLE or PARTIAL. "
            "You MUST set `concept_mode=\"full\"`, provide the exact complete set in `concepts`, leave "
            "`concepts_added` and `concepts_removed` empty, and MUST NOT use delta mode for this proposal.")
    else:
        lines.append(
            "Concept membership context only: use delta mode only when a separate trusted run cue "
            "explicitly enables it; a root inherits the run base and a merge inherits all actual parents.")
    # Append the always-on "working set": a compact view of the whole search (top winners, weakest /
    # failures, theme map) so the Researcher proposes with awareness of what's already been tried,
    # not just `best` + `parent`. Depth (full experiments, code, data) lives behind the run tools.
    from looplab.events.digest import experiments_digest, lineage_lessons, sibling_digest
    lines.append(experiments_digest(state, char_cap=digest_cap))
    # M1/A0c operator-scoped memory: draft/improve additionally see their SIBLINGS (diversity
    # pressure — aira-dojo MEM_OPS `sibling`) and, when refining, the LESSONS distilled from the
    # lineage under the refined node (D6 insight backpropagation, Arbor's Backpropagate step).
    lines.append(sibling_digest(state, parent))
    lines.append(lineage_lessons(state, parent))
    # Signal-delivery (§1): the latest deep-research memo's takeaway. Its `recommended_directions`
    # already ride as standing hints, but the summary/findings/claims were recorded-but-unread — this
    # surfaces the one-line conclusion plus a pointer to the `read_research_memo` tool for the full
    # reasoning (available to the agentic Researcher). Best-effort; skipped when there's no memo.
    research = getattr(state, "research", None) or []
    if research and isinstance(research[-1], dict) and research[-1].get("summary"):
        # `memo_verdicts` (Settings.memo_verdict_cue) splices the memo's own verifier result at the
        # SAME position and changes nothing else, so OFF reproduces the historical line byte for
        # byte — the `developer_probe`/`train_monitor_tools` pattern, for the same reason: a prompt
        # is a contract and a resumed run must be able to keep the one it was written for.
        #
        # WHY THIS LINE NEEDED IT. `trust/memo_verify.py::verify_memo` verifies `memo["claims"]` and
        # has never, at any commit, looked at `memo["summary"]` — so the one field this line pushes
        # is the one field of the memo nothing checks, and until 2026-08-16 the verdicts could not be
        # PULLED either (`read_research_memo` keyed on a `verification["summary"]` no writer emits).
        # Measured over `rubertlite-dr-unified-v8`'s own `spans.jsonl`: this line reached 293 real
        # prompts in three phases (propose 269, triage 20, repair_critic 4) and NONE of those 293
        # whole prompts contains the word `Verifier` or the word `unsupported`, while its `at_node: 0`
        # memo — the one whose summary says "climb from the known ~0.88 plateau", a rounded
        # `rubert-dr-0807` number on a DIFFERENT `engine/eval_contract.py` contract — records
        # `total_verdicts: 8, unsupported: 8`. The cue is engine-derived (`trust/memo_verify.py`
        # wrote the verdicts before the memo was ever appended) and states a fact about the CHECK, so
        # it widens what the role SEES and nothing it trusts: no metric, champion, selectability or
        # violation can move, and no model's own text decides its own verdict (docs/36).
        #
        # `memo_snapshot_cue` is UNGATED, unlike the verdict cue beside it, and the reason is that
        # without it this prompt contradicts itself. A memo is computed from a state snapshot and
        # recorded when the provider returns; measured over the thirty AlgoTune run dirs, 78 of 119
        # memos were appended after a result their snapshot could not contain. On
        # `spectral_clustering` this line pushed "experiment #0 … is still pending, so there are no
        # measured results yet" into every later prompt — 256 s after node 0's 0.0 landed, and
        # directly beneath a working set that showed it. The clause is engine-derived, empty
        # whenever nothing was superseded (so a run with no overlap renders the historical bytes),
        # and states a fact about WHEN the memo was written, never about whether it is right.
        lines.append("Latest deep-research takeaway"
                     + (memo_verdict_cue(research[-1]) if memo_verdicts else "")
                     + memo_snapshot_cue(research[-1]) + ": "
                     + " ".join(str(research[-1]["summary"]).split())[:300]
                     # channel-neutral: a plain researcher has no tools, so state that the depth is
                     # recorded rather than commanding a `read_research_memo` call it can't make.
                     + " (full findings/claims are recorded; the read_research_memo tool returns them).")
    # The board itself — both halves, in the one spelling every prompt that must not re-propose
    # an existing question shares (`board_prompt_lines`). The deep-research memo prompt renders
    # the SAME rows; it had this exact defect and did not get this exact fix.
    lines.extend(board_prompt_lines(state, hyp_order, board_cards, for_proposal=for_proposal))
    return "\n".join(line for line in lines if line)
