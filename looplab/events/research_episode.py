"""DR-01's missing half: the EPISODE — what a run's research has settled, across memos.

WHAT WAS ALREADY DURABLE, so this builds no second copy of it. Doc 52 row 16 landed the record:
`ResearchMemo.plan` carries the stage's own last `update_plan` (the ResearchPlan and its
ProgressLedger, `{plan, todos:[{item, status}], updates}`), `RunState.research_plan` folds the
LATEST memo's, `research_evidence` indexes every exact-span evidence item any memo cited,
`literature` every paper an `arxiv_search` returned, and `research_attempts` holds one paid-attempt
receipt per attempt. What none of that answers is the EPISODE question, because every one of those
is either per-memo or latest-only:

    which questions has this run's research settled, and which are still open?

Doc 28 DR-01's acceptance gate is "replay yields identical episode state across every event splice;
resume never repeats a settled question". THIS IS A PROJECTION, NOT AN EVENT TYPE, and that is how
the first half of the gate is met by construction: `episode()` is a pure function of a folded
`RunState`, so two logs that fold alike project alike whatever order their rows arrived in. Adding a
`research_episode_*` event instead would have put replay-affecting rows in front of the same facts
the fold already holds — and invariant #5 asks new readers to default old logs, not new writers to
re-record them.

THE SETTLED/OPEN SPLIT IS THE BOARD'S, NOT A NEW JUDGEMENT. A question is SETTLED when the card it
became carries evidence — i.e. an experiment answered it — which is exactly the population
`open_research_beliefs()` excludes and exactly the edge `card-ladder` measures. Re-derived over the
eleven run dirs on this box: 31 parent/child edges, and 17 of 17 parents have an evidenced child, so
the relation is real and populated. The alternative — asking a model whether a question is answered
— would put a judgement where a fold already has a fact.

WHAT IT IS FOR, immediately: `trust/verifier_routing.py` needs `rounds_used`, the open gaps, the
evidence ids and a draft digest to route a revision round, and every one of those is an episode
property rather than a memo property. A router handed one memo's numbers would re-open a question
the run settled two memos ago.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

# How many question rows the episode carries. A bound rather than a page size: this projection is
# read into prompts and receipts, and an unbounded list is how a board of twenty becomes a wall of
# text nobody reads. The overflow is COUNTED, never silently dropped — `omitted` below.
MAX_QUESTIONS = 64


class EpisodeQuestion(NamedTuple):
    """One research question, and what the run did about it.

    `statement` is the seed text as registered. `card_id` is the board card it became, when the
    fold made one. `evidence` is the node ids that answered it (a substituted build does not) —
    non-empty means SETTLED. `memos`
    is how many memos raised it, which is the re-proposal signal the duplicate rules leave visible:
    a question raised three times and never carded is a question the run kept asking and never ran.
    """
    statement: str
    card_id: Optional[str]
    evidence: tuple
    memos: int
    at_node: Optional[int]

    @property
    def settled(self) -> bool:
        return bool(self.evidence)


class ResearchEpisode(NamedTuple):
    """The run's research, as one record.

    `memos` / `attempts` are deliberately separate: an attempt receipt is appended BEFORE the
    provider call and the memo after it, so `attempts > memos` is a hard kill between the two —
    the durability gap doc 28 DR-05 is about — and a single count would hide it.

    `unfulfilled` names that difference rather than making the reader subtract.
    """
    memos: int
    attempts: int
    unfulfilled: int
    questions: tuple
    omitted: int
    settled: tuple
    open: tuple
    evidence_ids: tuple
    literature_ids: tuple
    plan: Optional[dict]
    todos_open: tuple

    @property
    def rounds_used(self) -> int:
        """What `verifier_routing.route_after_verification` means by `rounds_used`: how many memos
        this episode has already paid for. Named here rather than at the call site so the router
        and the record cannot come to disagree about what a round is."""
        return self.memos


def _memo_rows(state) -> list:
    rows = getattr(state, "research", None)
    return [row for row in (rows or ()) if isinstance(row, dict)]


def _question_statements(row: dict) -> list:
    """The questions a memo REGISTERED, in the field the engine actually reads.

    `research_cadence._record_research_steering` registers `open_questions` when the memo drew the
    split and falls back to `recommended_directions` when it did not — so reading the union here
    would credit the episode with `next_experiments` entries that never became board rows. That
    exact over-claim was measured on `runs/e5small-dr-unified-v11`: its three non-empty memos split
    `open_questions` 4/3/2 against `next_experiments` 6/8/5, so the union is 10/11/7 and the
    registered half is less than half of it.
    """
    questions = row.get("open_questions")
    if not isinstance(questions, (list, tuple)) or not questions:
        questions = row.get("recommended_directions")
    out = []
    for item in (questions or ()):
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _normalized(statement: str) -> str:
    """The same collapse `research_cadence.normalized_belief_key` uses — case and whitespace are not
    semantics — so the episode groups a re-worded twin the way the append site already refused it.
    Imported by VALUE would bind a copy; spelled here because `events` may not import `engine`."""
    return " ".join(str(statement or "").split()).casefold()


def episode(state) -> ResearchEpisode:
    """Project the run's research episode off a folded `RunState`. Pure: no I/O, no model, no clock.

    Tolerant of every older log shape by construction (invariant #5): a run with no `research` rows
    projects an empty episode, one written before the plan/evidence/literature fields existed
    projects `plan=None` and empty ledgers rather than raising — measured against
    `runs/e5small-dr-unified-v13`, whose seven memo rows carry none of the three.
    """
    rows = _memo_rows(state)
    cards = getattr(state, "cards", None) or {}

    # statement -> [the statement as first written, how many memos raised it, the EARLIEST node]
    #
    # `at_node` is the MINIMUM across every memo that raised the question, not the value from the
    # first row encountered — and that is DR-01's acceptance gate rather than a nicety. "Earliest
    # node this was raised at" is a property of the SET; reading it off arrival order makes the same
    # log project two different episodes depending on splice, which
    # `test_the_SAME_ROWS_IN_ANY_ORDER_project_the_same_episode` caught on the first run of this
    # module. The statement TEXT keeps the first spelling seen, which is order-dependent by nature
    # and immaterial: the key is normalized, so the variants are the same question, and the sort
    # falls through to the text only after two order-free keys.
    raised: dict = {}
    for row in rows:
        at_node = row.get("at_node")
        at_node = at_node if isinstance(at_node, int) and not isinstance(at_node, bool) else None
        for statement in _question_statements(row):
            key = _normalized(statement)
            if key not in raised:
                raised[key] = [statement, 0, at_node]
            raised[key][1] += 1
            known = raised[key][2]
            if at_node is not None and (known is None or at_node < known):
                raised[key][2] = at_node

    # The card a question became, matched on the SEED statement the fold wrote it from. Keyed on the
    # same normalized form, because a card's seed is the statement verbatim and a memo may re-raise
    # it with different spacing.
    card_by_key: dict = {}
    for card_id, card in cards.items():
        seed = getattr(card, "seed_statement", None)
        key = _normalized(seed) if seed else ""
        if key and key not in card_by_key:
            card_by_key[key] = (card_id, card)

    questions: list = []
    for key, (statement, memos, at_node) in raised.items():
        card_id, card = card_by_key.get(key, (None, None))
        # A substituted build (`Card.substituted_nodes`) did not answer the question — its Developer
        # built something else — so a card retired on two of them is still an OPEN question here.
        substituted = set(getattr(card, "substituted_nodes", None) or ()) if card is not None else set()
        evidence = tuple(i for i in (getattr(card, "evidence", None) or ()) if i not in substituted
                         ) if card is not None else ()
        questions.append(EpisodeQuestion(statement=statement, card_id=card_id,
                                         evidence=evidence, memos=memos, at_node=at_node))

    # Most-raised first, then by first appearance, then by text: a deterministic order, because this
    # list reaches a prompt and two replays of one log must render the same rows.
    questions.sort(key=lambda q: (-q.memos, q.at_node if q.at_node is not None else 1 << 30,
                                  q.statement))
    kept = tuple(questions[:MAX_QUESTIONS])
    omitted = max(0, len(questions) - len(kept))

    attempts = len(getattr(state, "research_attempts", None) or ())
    evidence_index = getattr(state, "research_evidence", None) or {}
    literature = getattr(state, "literature", None) or ()
    plan = getattr(state, "research_plan", None)
    todos = ()
    if isinstance(plan, dict):
        rows_todo = plan.get("todos")
        todos = tuple(
            str(item.get("item") or "").strip()
            for item in (rows_todo if isinstance(rows_todo, (list, tuple)) else ())
            if isinstance(item, dict) and str(item.get("status") or "").strip().casefold()
            not in ("done", "complete", "completed", "closed")
            and str(item.get("item") or "").strip())

    return ResearchEpisode(
        memos=len(rows),
        attempts=attempts,
        # NEVER negative: a memo appended by a path that wrote no attempt receipt (an older log)
        # would otherwise report a negative kill count, which reads as a corrupt record rather than
        # as the missing receipt it is.
        unfulfilled=max(0, attempts - len(rows)),
        questions=kept,
        omitted=omitted,
        settled=tuple(q.statement for q in kept if q.settled),
        open=tuple(q.statement for q in kept if not q.settled),
        evidence_ids=tuple(sorted(str(k) for k in evidence_index)),
        literature_ids=tuple(sorted(
            str(item.get("id") or "") for item in literature
            if isinstance(item, dict) and str(item.get("id") or ""))),
        plan=plan if isinstance(plan, dict) else None,
        todos_open=todos,
    )


def episode_digest_inputs(ep: ResearchEpisode) -> tuple:
    """The episode's half of the roadmap's no-progress key `(open_gaps, evidence_ids, draft_digest)`.

    Returns `(open_questions, evidence_ids)` and deliberately does NOT hash them: the hash lives in
    `trust/verifier_routing.py::progress_digest`, and `events` may import only `core` (CLAUDE.md
    layering) — so a convenience wrapper here would have been a layering violation for one function
    call. The first version of this module had exactly that, as a deferred import, which the rule
    still forbids. The CALLER composes the two.

    SETTLED QUESTIONS ARE EXCLUDED on purpose: a round that settles nothing new but re-states what
    is already answered has made no progress, and including the settled set would make that round
    look like movement.
    """
    return (ep.open, ep.evidence_ids)
