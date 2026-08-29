"""Research cadence (P2) for the engine — the Deep-Research stage (serial + concurrent seams),
the agentic open-hypothesis-board merge, and the run-report cadence — extracted from
orchestrator.py as a MIXIN: `class Engine(…, ResearchCadenceMixin)` inherits these methods
unchanged, so there is ZERO call-site churn and `self` here IS the engine. The method bodies are
verbatim moves and read engine attributes freely (`store` / `tracer` / `deep_researcher` /
`report_writer` / `_op_span` / `_cadence_due` / `_reflect_client` / `_embedder` / `lessons` /
`deep_research_every` / `report_every` / the `_research_verify`, `_track_hypotheses` knobs),
exactly as they did inside the class.

`_op_span` / `_cadence_due` / `_reflect_client` stay on the Engine (generic helpers / lessons
delegators); the moved methods call them as `self.…`, resolved on the Engine instance. The heavy
deps (ResearchMemo, verify_memo, hybrid_merge.consolidate) stay method-local imports, so a test
monkeypatching `looplab.trust.memo_verify.verify_memo` etc. still intercepts them.

Layering: no runtime import of the orchestrator (TYPE_CHECKING only) and never serve — only core,
events and stdlib (the trust/search deps are lazy, method-local imports)."""
from __future__ import annotations

import logging
import uuid
from typing import Iterable, Optional

from looplab.agents.hints import DEEP_RESEARCH_HINT_PREFIX
from looplab.agents.roles import BOARD_PROMPT_CARDS
from looplab.core.llm import BudgetExceeded
from looplab.core.llm_broker import in_llm_lane
from looplab.core.jsonutil import canonical_json_digest
from looplab.core.models import RunState, idea_proposal_ref, normalize_researcher_footprint
from looplab.engine.cadence import at_creation_boundary, deep_research_window
from looplab.events.replay import fold
from looplab.events.types import (EV_HINT, EV_HYPOTHESIS_ADDED, EV_HYPOTHESIS_MERGED,
                                  EV_REPORT_GENERATED, EV_RESEARCH_ATTEMPTED,
                                  EV_RESEARCH_COMPLETED,
                                  BACKGROUND_APPENDABLE,
                                  NON_CARD_SELECTION_BACKGROUND_APPENDABLE)


_LOG = logging.getLogger(__name__)


# The open belief board is a QUEUE the search spends, not a scratchpad the research stage fills.
# Sized to the window every prompt that reads the board can actually SHOW — `next_board_prompt_cards`
# and `attempted_board_prompt_cards` both stop at `BOARD_PROMPT_CARDS` whole rows — because a belief
# the model cannot see is a belief it re-proposes in new words, which is precisely how the measured
# board reached eleven cards for five ideas. Untested beliefs leave this population as soon as they
# get evidence, so the cap throttles the WRITER, it does not close the board.
#
# DERIVED, not copied. This value's whole justification is the reader's row cap, and it used to be a
# bare `5` beside a comment that quoted the other file's literal — so raising the prompt window left
# this cap silently wrong, which is the exact drift the comment was written to prevent.
DEEP_RESEARCH_OPEN_BELIEF_CAP = BOARD_PROMPT_CARDS


def normalized_belief_key(statement) -> str:
    """The append-site duplicate key: case-folded, whitespace-collapsed statement text.

    The fold already collapses BYTE-IDENTICAL statements (one card per `hypothesis_id(statement)`),
    so an append-site copy of only that rule would buy nothing. This is the smallest widening that
    stays deterministic and cannot fuse two different ideas: case and spacing are not semantics.

    It is deliberately NOT a similarity threshold, and that is a measurement rather than a taste.
    Over the 18 `hypothesis_added` statements of `runs/rubertlite-dr-unified-v6`, token-set Jaccard
    does not separate the five real ideas at ANY threshold: the highest-scoring pair in the whole
    corpus (0.489) is distillation-from-a-teacher against hard-negative-mining — two different
    experiments — while true re-wordings of one idea run down to 0.118. A cheap lexical guard set
    anywhere in that range fuses distinct directions before it catches the duplicates. Near-duplicate
    BELIEF identity needs the model (`_maybe_merge_hypotheses`), so the deterministic half of this
    fix does what determinism can do — exact restatements and a hard bound — and says so.
    """
    #
    # SIBLING, and the differences are load-bearing: `engine/lesson_hygiene.py::normalize_statement`
    # is the same collapse for LESSON identity, with `.lower()` instead of `.casefold()` and a
    # 160-char cap. Neither may be folded into the other — the cap fuses two research DIRECTIONS
    # that share a prefix, which is worse here than a duplicate card, while widening lesson identity
    # re-buckets a shipped cross-run store. That docstring carries the same note.
    return " ".join(str(statement or "").split()).casefold()


def admit_research_beliefs(open_statements: Iterable[str], directions: Iterable[str], *,
                           cap: int = DEEP_RESEARCH_OPEN_BELIEF_CAP,
                           counted: "Iterable[str] | None" = None) -> list[str]:
    """Which of a memo's directions may become OPEN BELIEFS, given the board already open.

    PURE and stateable on purpose (CLAUDE.md tier 2): the rule used to be "all five, every memo,
    forever", buried in an append loop no caller could reach, and over one 90-minute evaluation that
    is 18 board rows for five ideas. Two rules, both deterministic:

      1. a direction whose `normalized_belief_key` already names an open belief is DROPPED — the
         fold's own exact-statement rule, applied one step earlier so the duplicate never becomes a
         card at all rather than becoming one the consolidator may or may not get to;
      2. the open board is capped at `cap` DISTINCT beliefs; a memo may fill the remaining room and
         no more.

    TWO POPULATIONS, because the two rules are about different things and sharing one list made the
    second silently break the first. `open_statements` is the DEDUP universe — every open direction,
    including ones already being worked on, because restating a question somebody answered is exactly
    the duplicate rule 1 exists to refuse. `counted` is the subset that OCCUPIES a cap slot, and it
    is narrower: a direction with children is no longer an unanswered question competing for room.
    Passing one list for both meant the caller's (correct) narrowing of the cap also removed those
    directions from `seen`, so a later memo restating one registered a second card for the same
    question — and, because a direction never accrues evidence, the open population then grew
    without bound and the five-row prompt window began rotating real questions out of view.
    `counted=None` means "the same list", i.e. byte-for-byte the historical behaviour.

    Order is preserved and the memo's own repeats collapse against each other, so `admit(open, ds)`
    is idempotent under re-running the same memo. Everything dropped is still recorded — the MEMO
    BODY carries every `recommended_direction`, and `read_research_memo` renders them in full — so
    nothing is LOST here; what is refused is the board row, which is the resource that was
    overflowing.

    THAT SENTENCE USED TO SAY "the memo body and the `hint` row carry the full list", AND THE HINT
    HALF WAS FALSE. The hint carries the first `DEEP_RESEARCH_HINT_DIRECTIONS` of them (see
    `deep_research_hint_text`), so on `runs/e5small-dr-unified-v7`'s third memo — 8 directions, the
    only one of that run's three with any content — directions 6-8 reached the hint not at all. It
    is a bounded PUSH, not the record, and reading it as the record is how "nothing is lost" gets
    believed by whoever next decides a drop is safe. The memo body genuinely is the record, which is
    why the corrected sentence names it alone.
    """
    # ONE spelling of "usable statements, keyed", used for both populations. Written out twice, the
    # dedup universe and the cap-occupancy set are keyed by two expressions that a later change to
    # what counts as a usable statement (a length bound inside `normalized_belief_key`, blank
    # detection moving off `str(s or "").strip()`) can update independently — which is the two
    # populations silently disagreeing again, the exact failure `counted` was added to fix.
    def _keys(statements) -> set:
        return {normalized_belief_key(s) for s in statements if str(s or "").strip()}

    seen = _keys(open_statements)
    occupied = set(seen) if counted is None else _keys(counted)
    admitted: list[str] = []
    for direction in directions:
        text = str(direction or "").strip()
        if not text:
            continue
        key = normalized_belief_key(text)
        if key in seen:
            continue
        if len(occupied) >= max(0, int(cap)):
            break
        seen.add(key)          # never registered twice, whether or not it holds a slot
        occupied.add(key)      # …and a newly admitted direction is unanswered, so it holds one
        admitted.append(text)
    return admitted


# HOW MANY DIRECTIONS THE PUSHED HINT CARRIES — a bound on a PROMPT, not on the record.
# CLAIM[deep-research-hint-carries-five] the `hint` row carries the FIRST FIVE recommended
# directions, never all of them; the memo body is what carries every one.
# decided:line:DEEP_RESEARCH_HINT_DIRECTIONS&&5@looplab/engine/research_cadence.py
#
# Measured on `runs/e5small-dr-unified-v7`: its third memo (the only one of three with content)
# returned 8 directions, so three of them reached the hint not at all. Deliberately NOT raised to
# cover that memo — the hint is spliced into a prompt, `agents/hints.py` filters on its prefix, and
# a push that grows with whatever the model happened to return is how a brief becomes a wall of
# text. The remedy for a reader that wants them all is `read_research_memo`, which renders the
# directions IN FULL, not a bigger push.
DEEP_RESEARCH_HINT_DIRECTIONS = 5


def deep_research_hint_text(directions: Iterable) -> str:
    """The pushed hint's exact text — hoisted so the bound above is a rule and not a slice.

    Byte-identical to the inline `DEEP_RESEARCH_HINT_PREFIX + "; ".join(directions[:5])` it replaces;
    the prefix is load-bearing (`agents/hints.py` FILTERS on it, which is how a row whose `source`
    stamp predates that field is still recognised), so it is never spelled separately.
    """
    return DEEP_RESEARCH_HINT_PREFIX + "; ".join(
        list(directions)[:DEEP_RESEARCH_HINT_DIRECTIONS])


def question_concept_rows(questions: Iterable, per_question: Iterable) -> dict[str, list]:
    """Join each question to ITS OWN concept row: `question_concepts[i]` describes `questions[i]`.

    PURE and shared on purpose (CLAUDE.md §0.8 measured the alternative: four implementations of one
    claim/verdict join, and every drift was between the copies). Two callers — the deep-research memo
    and, since #72, the Researcher's own registered questions — and a positional join that disagrees
    with itself files a question under a concept set belonging to a different question.

    THE ORDER IS THE WHOLE FUNCTION. Blanks are skipped AFTER the index is read, never before. The
    in-place version filtered them out first and then enumerated the SHORTENED list, so every
    question after a blank took its predecessor's row: driven with `["", "q2"]` and
    `[["loss/contrastive"], ["training/negative-mining"]]`, q2 was filed under `loss/contrastive`.
    That misplaces the row in the question lattice, which keys on the concept SET.

    LATENT WHEN FIXED, and the zero is worth keeping: over every event log on the box — 173 memos
    carrying an `open_questions` list — 0 contained a blank and 0 carried `question_concepts` at all,
    because the field could not reach the durable row until `_assemble` stopped raising on it
    (7d406cc2). Repairing that carrier is exactly what made this reachable.

    Checked, not trusted: a short, missing or non-list row simply yields no concepts for that
    question, and a question with none is registered exactly as it was before any of this shipped.
    """
    rows = list(per_question or [])
    joined: dict[str, list] = {}
    for index, question in enumerate(questions or []):
        statement = str(question or "").strip()
        if not statement:
            continue
        row = rows[index] if index < len(rows) else None
        if isinstance(row, list) and row:
            joined[statement] = row
    return joined


def is_pure_belief(card) -> bool:
    """A board row that owns no ACTION — the Card equivalent of the old open hypothesis.

    Identity, not readiness (peer review): `selection_ready` is transient (a native card is not-ready
    while stale/incomplete/in-flight/terminal), so a `not selection_ready` filter admits a native
    work item whenever it is blocked. A native card OWNS an action
    (`selection_provenance.action_source` != "none", i.e. action_owner_count > 0 — the model enforces
    the equivalence); a pure belief owns none. Shared by the consolidation cadence and the
    append-site bound so both mean the same board.
    """
    provenance = getattr(card, "selection_provenance", None)
    return getattr(provenance, "action_source", "none") == "none"


def research_memo_sig(memo) -> str:
    """Stable content signature of a research memo (its summary + recommended directions). PURE and
    deterministic. Used by the REPEATED concurrent-research loop to skip re-recording an identical
    memo: a long eval re-runs research on a timer, and when the analysis has converged the researcher
    returns the same conclusions — recording those again would bloat the log/hypothesis board without
    adding signal. Accepts a ResearchMemo (attr access) or a plain dict (the sanitized payload)."""
    import hashlib

    def _get(key):
        if isinstance(memo, dict):
            return memo.get(key)
        return getattr(memo, key, None)

    summary = str(_get("summary") or "").strip()
    directions = [str(d).strip() for d in (_get("recommended_directions") or []) if str(d).strip()]
    # OPEN[research-memo-signature-omits-split-output] the convergence key still covers only the
    # legacy compatibility projection, not the two new memo outputs.
    # proof:`line:blob = summary&&join(directions)@looplab/engine/research_cadence.py`
    # REVIEW 2026-08-27 (P1 delivery): two valid memos with the same summary and an empty legacy
    # union hash identically even when their open questions and concrete experiments differ. The
    # repeated-research loop then treats the later paid answer as converged and never records it.
    # Include both split lists in a canonical, field-delimited signature; the existing whitespace
    # normalization can otherwise stay unchanged.
    blob = summary + "\n" + "\n".join(directions)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


class ResearchCadenceMixin:
    """The engine's research-cadence cluster (deep research + hypothesis merge + report). See the
    module docstring for the mixin convention (`self` is the Engine)."""

    # ---------------------------------------------------- research cadence (P2)
    def _maybe_deep_research(self, state: RunState) -> RunState:
        """Run the Deep-Research stage when there's demand, then re-fold. Three triggers, each gated
        for replay safety: a MANUAL `deep_research` control event (counter gate), a CADENCE
        (`deep_research_every`, once per node-count), or a Strategist `request_research` decided at
        this node-count. No-op when the stage is off or already served. Records a
        `research_completed` memo that is neutral for direct node/champion ranking and feeds its
        directions back as standing hints that can steer later proposals."""
        n = len(state.nodes)
        # Manual: serve outstanding requests first, regardless of node-count (operator asked now).
        # Requests whose paid attempt is still unreconciled count as served here: their think was
        # already bought, and a resume must not charge for it a second time (see
        # `_outstanding_manual_research`).
        if (len(state.research_requests)
                > state.research_served + self._outstanding_manual_research(state)):
            return self._run_deep_research(state, trigger="manual", manual=True)
        # Auto triggers only at a creation decision point (no pending evals), never re-firing at a
        # node-count already researched (the at_node gate makes resume a no-op).
        # THIS IS THE ONE MEMBER OF THE F1i FAMILY THAT KEEPS THE OLD PREDICATE, deliberately. The
        # other four moved to `cadence.at_creation_boundary` because their phase stopped happening;
        # this one's did not — `_spawn_research` runs the SAME decision concurrently and never
        # carried the guard, so `research_completed (trigger=cadence)` is alive in all six runs in
        # `runs/`, including the three with zero quiescent prefixes. Opening this gate mid-eval buys
        # a double-spend (two thinks racing between the shared `_cadence_research_marks` read and
        # their receipts) to reach work already being done. The `concurrent_research=false` hole is
        # `docs/BACKLOG.md` F1i-b; `tests/test_cadence_while_evaluating.py` pins the refusal.
        # `n == 0` used to be part of THIS clause; it is now the run-opening branch below, because
        # "no nodes yet" is not "nothing to research" — see `_ground_run_start`. The at_node gate is
        # evaluated FIRST so a run-opening memo already in the log makes the branch a no-op on
        # resume, exactly as it does for every later node-count.
        if state.pending_nodes() or self._already_researched_at(state, n):
            return state
        if n == 0:
            return self._ground_run_start(state)
        # Since-last cadence (not `n % every == 0`): a rung-0/seed batch that jumps the node count by
        # k>1 must not step over the only multiple and skip the whole window. The last researched
        # at_node is the marker; `_already_researched_at` above already de-dups the same-n resume.
        # `default=0` (no prior research → baseline at the run start, node 0): the first deep-research
        # fires a full `every` nodes in (n >= every), so the opening window is the SAME width as every
        # later one. (`default=-1` would fire it one node early — a narrower first window.)
        # …and under the shipped default there IS no opening window: `deep_research_window` settles
        # `deep_research_every=0` to 1, which against `n - last >= every` is due at the first node.
        #
        # A CADENCE needs a stage to schedule. `_due_research_trigger` — the concurrent half of this
        # same decision — has always answered None with no `deep_researcher` wired, and only this
        # serial half did not: it fell through to `_run_deep_research`, which by contract records a
        # STUB memo ("deep research unavailable: no model configured") so a MANUAL request's gate can
        # advance. That is right for a request and wrong for a schedule, and at the old `every=3` it
        # was merely quiet noise on an offline run (see `tests/data/golden_run_events.jsonl`, stubs at
        # n=2/5/8). At the shipped `0` it would be two events per node on every toy/offline run,
        # each claiming a completed think nobody could have run. The manual and Strategist branches
        # keep the old treatment on purpose: they answer an outstanding REQUEST, whose requester is
        # waiting on the recorded answer.
        _last_research_n = max(self._cadence_research_marks(state), default=0)
        if (self.deep_researcher is not None
                and self._cadence_due(n, _last_research_n,
                                      deep_research_window(self.deep_research_every))):
            return self._run_deep_research(state, trigger="cadence", manual=False)
        hist = state.strategy_history
        if (hist and hist[-1].get("at_node") == n
                and (hist[-1].get("strategy") or {}).get("request_research")):
            return self._run_deep_research(state, trigger="strategist", manual=False)
        return state

    def _ground_run_start(self, state: RunState) -> RunState:
        """The RUN-OPENING think: one Deep-Research pass at `at_node=0`, BEFORE the first idea is
        proposed. Fires once per run, for any cadence that is not spelled OFF.

        WHY THIS IS NOT THE CADENCE. `cadence_due` cannot express it — `n > 0` is in its body and
        `engine/cadence.py` is shared by five other consumers for which "no nodes yet" really is
        "nothing to do". So the run-opening pass is a SEPARATE, once-per-run branch, in the spirit of
        `cadence.seed_boundary_due` (a first-ever firing the ordinary window cannot reach), and it
        deliberately does not touch the window: the marks this pass leaves are `at_node=0`, and
        `max(marks, default=0)` is 0 either way, so the first CADENCE think still lands a full
        `every` nodes into the run and every later one is unchanged. Grounding the run costs one
        think, not a re-phasing.

        WHY IT MUST EXIST AT ALL. Measured over the whole shipped run corpus on 2026-08-12: NOT ONE
        run of 22 with any research row ever recorded one at `at_node=0`. The earliest anywhere is
        `at_node=1` (`runs/lt-recovery-0811`, the only run on the shipped `deep_research_every=0`),
        i.e. the first memo has always landed AFTER the first idea was proposed and the first
        Developer build was committed. 2026-08-07 removed the node-counted WINDOW from the front of
        the run (`deep_research_window`), which bought the overlap with the first eval; it did not
        remove the `n == 0` guard, so the one proposal that has no results to learn from — the one
        that seeds the entire tree, and in Card-driven mode mints card-0's action identity — was
        still the only proposal made with no memo behind it. `runs/rubertlite-dr-unified-v5` spent
        its first 13.5 minutes on exactly that proposal.

        AND IT IS NOT CONDITIONED ON CROSS-RUN MEMORY. "Only when this task fingerprint has no prior
        lessons" is the tempting narrower rule and it is the wrong one, for a replay reason before a
        product one: the cross-run stores are explicit SIDECARS that `fold` does not rebuild (see the
        module header of `events/replay.py` and CLAUDE.md), so gating a PAID side effect on them
        would make whether the run paid depend on a mutable file outside the log — two replays of one
        event log could disagree about what the engine did. The gates that decide paid work read
        folded state only. (The product half: prior lessons for a fingerprint are an input to the
        memo, not a substitute for it — they say what happened last time, not what to try now.)

        OFF STILL MEANS OFF. `deep_research_every=-1` (`cadence.DEEP_RESEARCH_OFF`) settles to a
        window of 0 and this branch declines, so an operator who spelled the stage off does not
        acquire a paid think at run start; manual `deep_research` and the Strategist's
        `request_research` remain their only triggers, and the manual branch above already answers at
        `n == 0`. A wired researcher is required for the same reason the cadence branch requires one:
        with no model `_run_deep_research` records a STUB memo by contract, which here would spend
        the run-opening slot on a think nobody could have run.

        THE CONCURRENT SEAM IS DELIBERATELY NOT CHANGED. `_due_research_trigger` keeps its `n == 0`
        answer of None: it exists to overlap a think with a RUNNING eval, and at node-count 0 there
        is no eval to overlap with. This pass is serial on purpose — the whole point is that the
        first proposal waits for it.
        """
        if self.deep_researcher is None:
            return state
        if deep_research_window(self.deep_research_every) <= 0:
            return state
        return self._run_deep_research(state, trigger="run_start", manual=False)

    @classmethod
    def _cadence_research_marks(cls, state: RunState) -> set[int]:
        """Node-counts at which the serial (node-count) cadence has already been SPENT.

        Two sources, deliberately unioned. A recorded memo is the obvious one. A paid ATTEMPT is the
        other: `research_attempted` is appended before the provider call, so a kill between "the model
        answered" and "the memo is durable" leaves an attempt with no memo — and without counting it
        here the very next decision point would pay for the identical think again. Repeat passes are
        excluded for the same reason their memos are excluded from `_cadence_research_memos` (they
        ride a TIME cadence, not this one) — and they never record an attempt at all.

        MANUAL attempts count here exactly as manual MEMOS already do: `_cadence_research_memos`
        never filtered by trigger except for `repeat`, so treating an interrupted manual think
        differently from a completed one would make the node-count gate depend on whether the process
        happened to survive.
        """
        def _mark(value) -> Optional[int]:
            # Tolerant on purpose: this replaced a bare `int(m.get("at_node", -1))`, and a legacy or
            # hostile row whose at_node is unusable must be SKIPPED rather than raise — but a value
            # that coerces (an old float-serialized count) must still spend its window, because
            # dropping it would let the cadence fire again and pay for the same think twice.
            if value is None or isinstance(value, bool):
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        marks = {mark for mark in (_mark(m.get("at_node"))
                                   for m in cls._cadence_research_memos(state))
                 if mark is not None}
        marks |= {mark for mark in (_mark(a.get("at_node")) for a in state.research_attempts
                                    if isinstance(a, dict) and a.get("trigger") != "repeat")
                  if mark is not None}
        return marks

    @staticmethod
    def _outstanding_manual_research(state: RunState) -> int:
        """Manual `deep_research` requests whose paid attempt was made but whose memo never landed.

        `research_served` only advances on a recorded memo, so without this the manual queue would
        re-serve — and re-pay for — a request whose think was already bought by a dead process. The
        operator can always ask again; what they must not get is a silent second charge for the
        request they already made.
        """
        return sum(1 for a in state.research_attempts
                   if isinstance(a, dict) and a.get("manual")
                   and a.get("attempt_id") not in state.research_attempts_completed)

    @staticmethod
    def _cadence_research_memos(state: RunState) -> list:
        """Research memos that COUNT toward the serial (node-count) cadence — everything EXCEPT the
        repeated concurrent-overlap memos (`trigger="repeat"`). Those fire on a TIME cadence during a
        long eval (`_research_overlap_loop`), so letting them advance the node-count marker would
        re-phase and suppress the between-nodes research pass — the one that runs with the freshest
        results at a no-pending decision point. Excluding them keeps the two mechanisms independent."""
        return [m for m in state.research
                if isinstance(m, dict) and (m or {}).get("trigger") != "repeat"]

    @classmethod
    def _already_researched_at(cls, state: RunState, n: int) -> bool:
        return n in cls._cadence_research_marks(state)

    def _run_deep_research(self, state: RunState, *, trigger: str, manual: bool) -> RunState:
        """Execute one Deep-Research step (serial path) and record it, then re-fold. Always records a
        `research_completed` event (even with no model wired, so a manual request's gate advances and
        the loop doesn't spin)."""
        # One trace for the whole serial step: compute WITHOUT its own inner span (trace=False) so the
        # research LLM spans + the research_completed append both live in THIS op-trace → the event is
        # stamped with it (UI scopes the event's trace to just the research, not a node).
        with self._op_span("deep_research", trigger=trigger):
            self._research_attempt_step(state, trigger, manual=manual)
        return fold(self.store.read_all())

    def _research_attempt_step(self, state: RunState, trigger: str, *, manual: bool = False,
                               last_sig: Optional[str] = None) -> tuple[Optional[str], bool]:
        """ONE paid Deep-Research think — receipt, provider call, record — as a single INDIVISIBLE
        step. The one spelling shared by the serial cadence (`_run_deep_research`) and BOTH concurrent
        seams (`orchestrator._spawn_research`, `orchestrator._research_overlap_loop`).

        The receipt goes down BEFORE the provider call: the memo only becomes durable at
        `_record_deep_research`, and a kill in that window used to leave every trigger gate
        outstanding, so resume bought the same think again.

        INDIVISIBILITY IS THE POINT, and it is why this is one SYNC method rather than three calls at
        each caller. The concurrent seams run it through a single non-abandonable
        `anyio.to_thread.run_sync` hop, and a worker thread has no cancellation points — so a cancel
        that lands anywhere between "the gate was spent" and "the memo is durable" is delivered only
        AFTER the memo lands. Split across separate awaits (as it was), the eval-join cancel in
        `_dispatch_evals`/`_run_card_session` landed on `to_thread.run_sync`'s leading checkpoint and
        raised BaseException `CancelledError` — invisible to the callers' `except Exception` — so on
        any task whose evals finish faster than the research call the cadence gate was spent, the
        provider was paid AND waited for, and the answer was thrown away. Measured live: 4
        `research_attempted` / 0 `research_completed` over a 12-node run. `7a2a2ff4`'s "an interrupted
        think is simply spent rather than re-paid" is for a hard process kill, not for the normal path.

        Bounded, so an operator stop cannot hang on it: the un-interruptible window is exactly the one
        the concurrent seams already committed to (the provider call, bounded by the research
        endpoint's timeout) plus the record, whose optional verify pass is bounded by the same
        endpoint timeout and whose appends are bounded by the store lock. Nothing new is shielded —
        the fix REMOVES a checkpoint, it does not add a shield.

        Returns `(sig, recorded)`: the memo's content signature (None when the compute yielded
        nothing) and whether it was appended. `last_sig` is the repeated-overlap convergence gate —
        an identical re-run is not re-recorded (that pass rides a TIME cadence and is deliberately
        unreceipted, so nothing is spent by skipping it)."""
        attempt_id = self._record_research_attempt(state, trigger=trigger, manual=manual)
        memo = self._compute_deep_research(state, trigger, trace=False)
        if memo is None:
            # Contractually unreachable (`_compute_deep_research` degrades to a stub memo rather than
            # returning None), and kept only so a stubbed/foreign compute cannot crash the record.
            return None, False
        sig = research_memo_sig(memo)
        if last_sig is not None and sig == last_sig:
            return sig, False
        self._record_deep_research(memo, trigger=trigger, manual=manual, attempt_id=attempt_id)
        return sig, True

    def _record_research_attempt(self, state: RunState, *, trigger: str,
                                 manual: bool) -> Optional[str]:
        """Append the paid-attempt receipt for ONE Deep-Research step and return its identity.

        Called from BOTH the main-task cadence AND the concurrent research task, so — like every
        other write on this path — the type must stay in BACKGROUND_APPENDABLE (the assertion below
        makes a future non-neutral type fail fast). `repeat` passes are deliberately unreceipted:
        they ride an in-process TIME cadence with no durable gate to protect, so an attempt row would
        be pure log growth. Best-effort by design — a store that refuses the append leaves
        `attempt_id=None` and the caller behaves exactly as it did before this receipt existed.
        """
        if trigger == "repeat":
            return None
        attempt_id = uuid.uuid4().hex
        assert EV_RESEARCH_ATTEMPTED in BACKGROUND_APPENDABLE   # see the note on _record_deep_research
        try:
            self.store.append(EV_RESEARCH_ATTEMPTED, {
                "attempt_id": attempt_id, "trigger": trigger, "manual": bool(manual),
                # The gate that this receipt spends is keyed on the node-count the trigger fired at,
                # which is the same snapshot `_compute_deep_research` stamps into a stub memo.
                "at_node": len(state.nodes)})
        except Exception:  # noqa: BLE001 — research is advisory; refusing to think because the
            # bookkeeping append failed would stall the run for no safety gain. Degrade to the
            # pre-receipt behavior (the gate then advances only on the recorded memo).
            return None
        return attempt_id

    @in_llm_lane("deep_research")
    def _compute_deep_research(self, state: RunState, trigger: str, *, trace: bool = True):
        """PURE compute: run one Deep-Research step and RETURN the memo WITHOUT writing the event log,
        so it can run in a worker thread concurrently with an eval while the engine stays the sole
        writer. Best-effort for ordinary failures (a crash/None model yields a stub so the gate still
        advances); the global `BudgetExceeded` hard stop always propagates.
        `trace=False` skips the span: the tracer is not safe to write from the concurrent worker."""
        from looplab.core.models import ResearchMemo
        if self.deep_researcher is None:
            return ResearchMemo(at_node=len(state.nodes), trigger=trigger,
                                summary="(deep research unavailable: no model configured)")
        try:
            if trace:
                with self.tracer.span("deep_research", new_trace=True, trigger=trigger):
                    return self.deep_researcher.research(state, trigger=trigger)
            return self.deep_researcher.research(state, trigger=trigger)
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 — ordinary research failures degrade to a stub
            return ResearchMemo(at_node=len(state.nodes), trigger=trigger,
                                summary=f"(deep research failed: {exc})")

    # Every append below must stay in events.types.BACKGROUND_APPENDABLE: this method is invoked
    # from the CONCURRENT research task (`orchestrator._spawn_research`), the one enforced
    # exception to engine invariant #1 ("only the main task appends"). The assertions make a
    # future selection-affecting append here fail fast instead of racing the event order.
    @in_llm_lane("deep_research")
    def _record_deep_research(self, memo, *, trigger: str, manual: bool,
                              attempt_id: Optional[str] = None) -> None:
        """Append the memo to the event log. Called from BOTH the main-task cadence AND the
        concurrent research task — see the note above; every append here must stay in
        BACKGROUND_APPENDABLE.

        `attempt_id` closes this think's paid-attempt receipt (`_record_research_attempt`). Absent
        for `repeat` passes and for any caller that predates the receipt, in which case the trigger
        gates fall back to counting recorded memos alone — exactly the old behavior."""
        from looplab.core.advisory_payloads import (
            research_claim_ref,
            research_memo_ref,
            sanitize_research_memo_payload,
        )
        # Verify the same canonical, redacted payload that can be persisted. Otherwise a custom
        # researcher can expose secrets/prompt controls to the verifier and receive a verdict over
        # evidence that is later truncated into a materially different durable memo.
        memo_payload = memo.model_dump(mode="json")
        # ResearchMemo excludes the receipt from generic dumps for replay compatibility;
        # this durable writer must explicitly carry the original pre-cap denominator across sanitizers.
        if getattr(memo, "claims_receipt", None) is not None:
            memo_payload["claims_receipt"] = memo.claims_receipt
        memo_d = sanitize_research_memo_payload(memo_payload)
        # D8 · decoupled Verifier: check the memo's claims against their CITED evidence before the
        # memo is recorded — synthesis is the documented weak link (Kosmos: 57.9% accurate).
        # Deterministic layer always (refs exist? quoted numbers match?); LLM rubric pass when a
        # client is wired. Verdicts ride INSIDE the folded memo and cannot change this run's champion;
        # finalize later uses their aligned support as the gate for positive D8 evidence.
        if self._research_verify and memo_d.get("claims"):
            try:
                from looplab.trust.memo_verify import verify_memo
                state = fold(self.store.read_all())
                ver = verify_memo(memo_d, state,
                                  client=getattr(self.deep_researcher, "client", None),
                                  parser=getattr(self.deep_researcher, "parser", "tool_call"))
                if ver is not None:
                    memo_d["verification"] = ver
            except BudgetExceeded:
                raise
            except Exception:  # noqa: BLE001 — ordinary verifier failures do not block the memo
                pass
        # The model, tool ledger, and verifier are all untrusted text producers. This
        # writer-side pass is the invariant: custom researchers cannot bypass redaction, control
        # stripping, list caps, or the aggregate text budget before any durable derivative.
        memo_d = sanitize_research_memo_payload(memo_d)
        # Layer 1b Card provenance is reference-only.  Mint the memo id from the FINAL canonical payload
        # (including verification) and bind every retained claim to that exact memo + positional slot.
        # The full bodies stay exclusively on the research timeline; Cards will carry only these ids.
        memo_id = research_memo_ref(memo_d)
        if memo_id is not None:
            memo_d["memo_id"] = memo_id
            for index, claim in enumerate(memo_d.get("claims", [])):
                claim_id = research_claim_ref(memo_id, index, claim)
                if claim_id is not None:
                    claim["claim_id"] = claim_id
        assert EV_RESEARCH_COMPLETED in BACKGROUND_APPENDABLE   # see the method-level note
        self.store.append(EV_RESEARCH_COMPLETED, {
            "memo": memo_d,
            **({"memo_id": memo_id} if memo_id is not None else {}),
            "at_node": memo.at_node, "trigger": trigger, "served_manual": manual,
            **({"attempt_id": attempt_id} if attempt_id else {})})
        # Steer the next proposals: retain the legacy hint projection for replay compatibility.
        # It is explicitly model-generated advisory data, not operator authority; prompt rendering
        # filters this source while the research memo/open-hypothesis channels carry the signal.
        directions = [d for d in memo_d.get("recommended_directions", []) if str(d).strip()]
        # OPEN[next-experiments-never-reach-proposal] the concrete half of the new memo split has no
        # production reader, so an experiments-only memo steers no later work.
        # proof:absent:memo_d.get("next_experiments")@looplab/engine/research_cadence.py
        # REVIEW 2026-08-27 (P1 delivery): the durable model says these entries are left to be
        # proposed as real work, but this writer reads only the legacy union and open questions. A
        # schema-valid memo that omits the optional compatibility field therefore appends its paid
        # `research_completed` row yet emits no hint, card or executable proposal for its concrete
        # list.
        # AMENDED 2026-08-29 — THIS REVIEW'S OWN PARENTHETICAL REMEDY IS SPENT, and re-deriving it
        # is what the next reader must not repeat. It said "route that list through the real
        # proposal intake (or a dedicated bounded hint)"; the second half cannot work.
        # `agents/hints.py::render_hint_directives` is the ONLY renderer of `state.hints`, and it
        # FILTERS deep-research rows out on BOTH keys — `source == "deep_research"` and the
        # `DEEP_RESEARCH_HINT_PREFIX` text, the second catching rows folded from logs older than the
        # stamp — deliberately, because model output must not be relabelled as operator authority.
        # So a second hint appended here would be a second channel with no reader, which is the
        # defect one field over (`RunTools._research_memo` keyed on a `summary` no writer produced,
        # dead from the commit that added it) reintroduced by its own fix. That is also why the
        # EXISTING directions hint is not the delivery path it reads as: what actually reaches the
        # proposal prompt is the `EV_HYPOTHESIS_ADDED` board (`agents/roles.py::board_prompt_lines`)
        # plus the memo summary pushed by `_state_brief`, and the pull tool `read_research_memo`.
        # A BOARD ROW IS ALSO THE WRONG DESTINATION and that is the whole point of the split: a
        # concrete one-change experiment registered as an open belief is a row owning no action,
        # unbuildable by construction, which is the defect the paragraph below records for
        # `recommended_directions`. So the remaining route is the PROPOSAL INTAKE and nothing else,
        # and it cannot be taken from here: this is the background research task, which invariant #1
        # bounds to `BACKGROUND_APPENDABLE`, and minting a card is the main task's own append.
        # WHAT IS ALREADY DELIVERED, so the gap is narrower than "no reader": `tools/run_tools.py`
        # renders `open_questions + next_experiments` as the memo's directions when
        # `recommended_directions` is empty, so an AGENTIC Researcher that reads the memo sees them
        # (measured there: 71 of 78 concrete experiments, 91 %, already appear in the union). The
        # genuine hole is the NON-agentic proposal path, which pulls nothing.
        # MEASURED 2026-08-29, over every run directory on this box, and it decides the disposition.
        # All NINE runs record `unified_agent: true` in `config.snapshot.json` and every one PULLS
        # the memo — `read_research_memo` appears 138 / 139 / 216 / 229 / 252 / 342 / 362 / 698 /
        # 2,091 times in their `spans.jsonl`. So the non-agentic proposal path, the only route this
        # marker still describes, is UNREACHABLE here.
        # AND THE REACHABLE PATH IS ALREADY FIXED BUT HAS NEVER RUN. `git merge-base --is-ancestor`
        # says `899f6244` — the `run_tools.py` fallback that renders `open_questions +
        # next_experiments` as the memo's directions — is an ancestor of NEITHER `e6d7d680` (v9's
        # launch) NOR `1280de6d` (v10's), and IS on master. A running engine pins its own code, so
        # every memo on disk was written by a process that could not deliver these entries, and
        # every FUTURE run can. That is what the v9 evidence actually shows: 18 memos, 11 with a
        # filled `next_experiments`, and memo 4 is `(next 7, directions 0, questions 0)` — a
        # schema-valid memo whose ONLY content is the concrete list, which steered nothing in that
        # run because the reader did not exist yet, not because the design lacks one.
        # SO THIS IS NOT CODE WORK. It is unexercised delivery: the next run launched from master
        # is what turns 11-of-18 memos from unreadable into read, and the marker stays only until a
        # run carrying `899f6244` shows a `next_experiments` entry reaching a proposal.
        # WHAT BECOMES A BOARD ROW is now what the memo itself called a QUESTION, not everything it
        # would try next. `recommended_directions` was described to the model as "specific next
        # experiments to try", so it correctly returned experiments and every one of them landed as
        # a row owning no action — unbuildable by construction. Measured on
        # `runs/e5small-dr-unified-v5`: one of five was a genuine family; the rest were concrete
        # single-change experiments the engine could not run.
        #
        # `open_questions` when the memo filled it, the whole list otherwise. The fallback is what
        # keeps every log already on disk and every memo from a pre-split prompt folding exactly as
        # it did — absence means "this memo did not draw the distinction", never "it has no
        # questions".
        questions = [q for q in memo_d.get("open_questions", []) if str(q).strip()] or directions
        # TWO CHANNELS, TWO GATES, and they were one until 2026-08-27. The legacy hint projection
        # is keyed on `recommended_directions` and the board registration on `questions`, but both
        # sat under `if directions:` — so a schema-valid memo that filled `open_questions` and left
        # the redundant compatibility field empty (it is optional, defaults to `[]`, and the prompt
        # asks for it only as a union of the two new lists) suppressed EVERY `EV_HYPOTHESIS_ADDED`
        # append. The run paid for a think-hard deep-research pass and the board stayed empty.
        #
        # The hint stays on `directions` alone rather than falling back to `questions`: it is the
        # replay-compatibility projection, prompt rendering already filters its source, and the live
        # signal travels on the memo/open-hypothesis channels. Widening it would put new text into
        # old logs' channel for no reader. Where a memo drew no distinction `questions` falls back
        # to `directions`, so every log already on disk folds byte-identically.
        if directions:
            assert EV_HINT in BACKGROUND_APPENDABLE             # see the method-level note
            # The prefix comes from `agents/hints.py`, which FILTERS on it — a deep-research row
            # whose `source` stamp is missing (a log older than the field) is recognised by this
            # text alone, so the two must not be spelled separately.
            self.store.append(EV_HINT, {
                "text": deep_research_hint_text(directions),
                "source": "deep_research"})
        # P1: register each question as an OPEN hypothesis so a deep-research idea is tracked to a
        # verdict (was fire-and-forget) — it accrues evidence when a matching node runs, and shows
        # on the board as an open question the search should resolve.
        if questions and self._track_hypotheses:
            assert EV_HYPOTHESIS_ADDED in BACKGROUND_APPENDABLE   # see the method-level note
            # THE JOIN, carried to the durable row. `question_concepts[i]` describes
            # `open_questions[i]` — POSITIONAL alignment, which is why it is resolved against
            # the memo's own question list rather than against `questions` (which falls back to
            # `recommended_directions` for a memo that drew no distinction, where the positions
            # mean nothing). Checked, not trusted: a mis-aligned or short list simply yields no
            # concepts for that question, and a question with none is registered exactly as it
            # was before this shipped.
            # The order rule and its driven counter-example live in `question_concept_rows`,
            # which is shared with the Researcher's own registered questions — a positional join
            # spelled twice is a join that will disagree with itself.
            by_statement = question_concept_rows(
                memo_d.get("open_questions") or [], memo_d.get("question_concepts") or [])
            # NO INTAKE TRUNCATION, and the bare `5` that used to be here was wrong twice over.
            # `DEEP_RESEARCH_OPEN_BELIEF_CAP` is DERIVED from `BOARD_PROMPT_CARDS` precisely so
            # raising the prompt window cannot leave a stale literal behind (see its comment), and
            # this line reintroduced the literal it was written to abolish. Worse, truncating HERE
            # happens BEFORE `admit_research_beliefs` dedups: a memo whose first five questions are
            # all already open registered ZERO cards while question six was genuinely new and there
            # was room for it — the same "the run paid for a think-hard pass and the board stayed
            # empty" outcome this cadence's own cap fix was written for. `admit_research_beliefs`
            # owns both the dedup universe and the cap, and it bounds its OUTPUT, so handing it the
            # whole list is what lets the cap mean what it says.
            for direction in self._admissible_beliefs(questions):
                concepts = by_statement.get(str(direction).strip())
                self.store.append(EV_HYPOTHESIS_ADDED, {
                    "statement": direction, "source": "deep_research",
                    "at_node": memo.at_node,
                    **({"concepts": concepts} if concepts else {})})

    def _admissible_beliefs(self, directions: list) -> list[str]:
        """Read the open belief board and apply `admit_research_beliefs` to this memo's directions.

        THE WRITE-SIDE HALF of the duplicate fix, and deliberately a REFUSAL TO APPEND rather than a
        merge after the fact. That distinction is what makes it legal from here: this method runs on
        the CONCURRENT research task, and engine invariant #1 admits it only because
        `EV_HYPOTHESIS_ADDED` is in `BACKGROUND_APPENDABLE`. Not writing an event moves no reader's
        position — not the fold, not `speculation._proposal_authority_seq` — so a bound expressed as
        "append fewer rows" is safe from a background task in a way that `hypothesis_merged` is not.
        See `_maybe_merge_hypotheses` for why the consolidator itself still may not run here.

        Best-effort by construction: if the board cannot be read the memo falls back to the historical
        behaviour (at most five rows for THIS memo), because refusing to register a research direction
        because a fold hiccuped would silently drop the stage's only durable output.

        The count can be stale by a race — an operator `add_hypothesis` may land between this fold and
        the caller's appends. That is accepted and is why the cap is not a fence: it bounds a writer that
        would otherwise add five rows per memo forever, and one extra row from a concurrent human is
        exactly the case where the human's intent should win.
        """
        try:
            board = fold(self.store.read_all())
            # A DIRECTION THAT HAS BEEN TAKEN UP NO LONGER OCCUPIES A SLOT, and without this clause
            # the cap is permanent. `open_research_beliefs()` means "open and carrying no EVIDENCE",
            # and a direction never carries any — since the `parent_card_id` edge shipped, the
            # experiments answering it are CHILD cards with evidence of their own, so the direction
            # stays evidence-free for the whole run by design.
            #
            # Measured live on `runs/e5small-dr-unified-v5`: FOUR research memos completed and only
            # the FIRST one's directions were ever registered — five of them, seq 35-39. Memos 2, 3
            # and 4 produced concrete directions (a `dcl_threshold` sweep among them, visible in
            # their `hint` rows) and contributed ZERO to the board, because five childless beliefs
            # met a cap of five and nothing ever frees it. The run paid for three think-hard reviews
            # and could not act on any of them.
            #
            # Counting only CHILDLESS directions is the whole fix: the cap still bounds "unanswered
            # questions on the board", which is the resource it was written to protect, and a
            # question somebody is already working on stops competing for that room. The proposal
            # FEED is untouched — a direction with one child and twelve experiments left to run must
            # still be visible — so this narrows what the cap counts, never what the model sees.
            # TWO POPULATIONS OUT OF ONE FOLD, and the narrowing belongs to exactly one of them.
            # `open_statements` is every open direction — the DEDUP universe, because restating a
            # question somebody is already answering is precisely the duplicate to refuse. `counted`
            # drops the taken-up ones, because those no longer compete for board room. Handing one
            # narrowed list to both (which this did for a day) let a later memo register a SECOND
            # card for a question already under way, and since a direction never accrues evidence the
            # open population then grew unbounded past the five-row prompt window.
            taken_up = {c.parent_card_id for c in board.cards.values() if c.parent_card_id}
            beliefs = [c for c in board.open_research_beliefs() if is_pure_belief(c)]
            open_statements = [c.seed_statement for c in beliefs]
            unanswered = [c.seed_statement for c in beliefs if c.id not in taken_up]
        except Exception:  # noqa: BLE001 — see the docstring: degrade to the pre-bound behaviour
            open_statements = unanswered = []
        admitted = admit_research_beliefs(open_statements, directions, counted=unanswered)
        dropped = len(directions) - len(admitted)
        if dropped:
            # Not silent: the operator reading the log sees a memo whose directions did not all
            # become cards, and the memo body + `hint` row still carry every one of them.
            _LOG.info("deep research: %d of %d recommended direction(s) not registered as beliefs "
                      "(%d already open, cap %d) — the memo and its hint still carry them",
                      dropped, len(directions), len(unanswered),
                      DEEP_RESEARCH_OPEN_BELIEF_CAP)
        return admitted

    @staticmethod
    def _card_enrichment_subject(state: RunState, node_id: int):
        """Resolve one live node to its exact canonical native Card + proposal fence."""
        if type(node_id) is not int or node_id < 0:
            return None
        node = state.nodes.get(node_id)
        if (node is None or node.tombstoned or node_id in state.aborted_nodes
                or node.idea is None or not isinstance(node.idea.card_id, str)):
            return None
        raw_card_id = node.idea.card_id
        card_id = raw_card_id if raw_card_id in state.cards else None
        if card_id is None:
            matches = [cid for cid, card in state.cards.items()
                       if raw_card_id in (card.aliases or [])]
            if len(matches) == 1:
                card_id = matches[0]
        proposal_ref = idea_proposal_ref(node.idea)
        if card_id is None or proposal_ref is None:
            return None
        return card_id, node, proposal_ref

    def _sync_card_enrichments(self, state: RunState) -> RunState:
        """Write ref-only Card links for memos, research claims and distilled lessons.

        Research may finish on a background worker, but every Layer-1 Card event is deliberately a
        main-task write.  The run cadence calls this collector after folding all producers; it emits one
        exact card/node/generation/proposal-fenced snapshot per changed Card and then re-folds once.
        """
        from looplab.core.advisory_payloads import valid_advisory_ref
        from looplab.events.types import EV_CARD_ENRICHED

        desired_lessons: dict[str, set[str]] = {}
        desired_claims: dict[str, set[str]] = {}
        desired_origins: dict[str, str] = {}
        desired_footprints: dict[str, tuple] = {}
        subjects: dict[str, tuple] = {}

        # Establish a deterministic live subject per native Card.  It owns the enrichment envelope;
        # replay independently verifies this exact lifecycle and proposal digest before applying it.
        for node_id in sorted(state.nodes):
            subject = self._card_enrichment_subject(state, node_id)
            if subject is None:
                continue
            card_id, node, proposal_ref = subject
            subjects.setdefault(card_id, (node, proposal_ref))
            if node.footprint_finalized:
                footprint = normalize_researcher_footprint(node.idea.footprint)
                if footprint is not None:
                    # Sorted traversal deliberately makes the newest materialized node the Card's
                    # Developer-finalization receipt.  Operator pins are overlaid later by replay.
                    desired_footprints[card_id] = (
                        node, proposal_ref,
                        {**footprint, "proposed_by": "researcher", "finalized_by": "developer"})
            origin = node.research_origin if isinstance(node.research_origin, dict) else {}
            memo_id = origin.get("memo_id")
            if valid_advisory_ref(memo_id, "memo"):
                desired_origins.setdefault(card_id, memo_id)

        # Lesson audit rows now carry an opaque id plus the exact cited node generations.  Legacy rows
        # without both remain visible on their timeline but cannot be guessed onto a Card.
        for batch in list(state.lessons_distilled or [])[-512:]:
            if not isinstance(batch, dict):
                continue
            raw_lessons = batch.get("lessons")
            if not isinstance(raw_lessons, (list, tuple)):
                continue
            for lesson in raw_lessons[:64]:
                if not isinstance(lesson, dict):
                    continue
                lesson_id = lesson.get("lesson_id")
                refs = lesson.get("evidence_refs")
                if (not valid_advisory_ref(lesson_id, "lesson")
                        or not isinstance(refs, (list, tuple)) or len(refs) > 64):
                    continue
                for ref in refs:
                    if (not isinstance(ref, dict) or set(ref) != {"node_id", "generation"}
                            or type(ref.get("node_id")) is not int
                            or type(ref.get("generation")) is not int):
                        continue
                    subject = self._card_enrichment_subject(state, ref["node_id"])
                    if subject is None or subject[1].attempt != ref["generation"]:
                        continue
                    desired_lessons.setdefault(subject[0], set()).add(lesson_id)

        # Claims are aligned positionally with verifier verdicts.  Only exact verifier node_refs carry
        # a generation, so a legacy/bare numeric citation is intentionally not promoted onto a Card.
        for memo in list(state.research or [])[-256:]:
            if not isinstance(memo, dict):
                continue
            memo_id = memo.get("memo_id")
            if not valid_advisory_ref(memo_id, "memo"):
                continue
            claims = memo.get("claims")
            verification = memo.get("verification")
            verdicts = verification.get("verdicts") if isinstance(verification, dict) else None
            if not isinstance(claims, (list, tuple)) or not isinstance(verdicts, (list, tuple)):
                continue
            for index, claim in enumerate(claims[:64]):
                if not isinstance(claim, dict) or index >= len(verdicts):
                    continue
                claim_id = claim.get("claim_id")
                verdict = verdicts[index]
                if (not valid_advisory_ref(claim_id, "claim") or not isinstance(verdict, dict)
                        or verdict.get("statement") != claim.get("statement")):
                    continue
                evidence = verdict.get("evidence")
                refs = evidence.get("node_refs") if isinstance(evidence, dict) else None
                if not isinstance(refs, (list, tuple)) or len(refs) > 64:
                    continue
                for ref in refs:
                    if (not isinstance(ref, dict) or set(ref) != {"node_id", "generation"}
                            or type(ref.get("node_id")) is not int
                            or type(ref.get("generation")) is not int):
                        continue
                    subject = self._card_enrichment_subject(state, ref["node_id"])
                    if subject is None or subject[1].attempt != ref["generation"]:
                        continue
                    desired_claims.setdefault(subject[0], set()).add(claim_id)

        def _footprint_receipt_exists(card_id, node, proposal_ref, target) -> bool:
            """Compare against the durable enrichment rows, before operator-wins replay overlays."""
            for row in reversed(list(state.cards_enriched or [])):
                if (not isinstance(row, dict) or row.get("id") != card_id
                        or row.get("node_id") != node.id
                        or row.get("generation") != node.attempt
                        or row.get("proposal_ref") != proposal_ref
                        or not isinstance(row.get("footprint"), dict)):
                    continue
                raw = row["footprint"]
                quantitative = normalize_researcher_footprint(raw)
                persisted = ({**quantitative,
                              **({"proposed_by": "researcher"}
                                 if raw.get("proposed_by") == "researcher" else {}),
                              **({"finalized_by": "developer"}
                                 if raw.get("finalized_by") == "developer" else {})}
                             if quantitative is not None else None)
                return persisted == target
            return False

        appended = False
        wanted_cards = (set(desired_lessons) | set(desired_claims) | set(desired_origins)
                        | set(desired_footprints))
        for card_id in sorted(wanted_cards):
            footprint_subject = desired_footprints.get(card_id)
            subject = ((footprint_subject[0], footprint_subject[1])
                       if footprint_subject is not None else subjects.get(card_id))
            card = state.cards.get(card_id)
            if subject is None or card is None:
                continue
            lesson_refs = sorted(desired_lessons.get(card_id, ()))[:64]
            claim_refs = sorted(desired_claims.get(card_id, ()))[:64]
            origin = desired_origins.get(card_id)
            delta = {}
            if lesson_refs and lesson_refs != list(card.lesson_refs or []):
                delta["lesson_refs"] = lesson_refs
            if claim_refs and claim_refs != list(card.claim_refs or []):
                delta["claim_refs"] = claim_refs
            if origin is not None and origin != card.research_origin:
                delta["research_origin"] = origin
            if footprint_subject is not None:
                fp_node, fp_proposal_ref, footprint = footprint_subject
                if not _footprint_receipt_exists(
                        card_id, fp_node, fp_proposal_ref, footprint):
                    delta["footprint"] = footprint
            # DO NOT RE-APPEND A DELTA THAT ALREADY DID NOT TAKE. `replay._on_card_enriched`
            # bounds the enrichment journal at 4,096 keys, and a NEW key refused at that cap is
            # refused on every subsequent fold — the fold replays the same log prefix, so the same
            # 4,096 keys win forever. The delta below is computed from the FOLDED card, so a refused
            # key makes it non-empty again on the very next pass and the reconciliation re-appended
            # a byte-identical `card_enriched` for the rest of the run (reproduced: appends-per-pass
            # 0,1,2,3,4… against a control converging at one — unbounded growth of a log every fold
            # re-reads).
            #
            # Keyed on the EXACT delta, not on the card. Gating on `_card_enrichment_complete`
            # instead was too broad in a way that costs the operator real information: that flag is
            # set for ANY omission and never clears, so a card that once had one key refused could
            # never receive a lesson-ref or origin correction the fold would have accepted, and
            # `public_cards` reported it enrichment-incomplete forever with no path back. A memo of
            # what THIS process already tried and watched fail stops the loop without freezing the
            # card. Runtime-only and bounded by the card/delta space; a restart retries once more,
            # which is the correct amount of forgetting for a non-durable memo.
            if delta:
                attempted = getattr(self, "_card_enrichment_attempted", None)
                if attempted is None:
                    attempted = self._card_enrichment_attempted = set()
                # THE KEY CARRIES THE SUBJECT EXACTLY WHEN THE DELTA DOES, and the two halves of
                # this delta are scoped differently.
                #
                # `footprint` is fenced by card/node/generation/proposal_ref —
                # `_footprint_receipt_exists` checks all four — so two DIFFERENT nodes under one
                # card legitimately produce byte-identical footprint deltas as the newest
                # materialized node advances across passes, and a (card_id, delta) key suppressed
                # the second one permanently, losing that node's Developer-finalization receipt.
                #
                # `lesson_refs`/`claim_refs`/`research_origin` are fenced by the CARD alone: the
                # delta is computed by comparing against `card.<field>`, never against a node. So
                # keying those on the subject fixes nothing and reintroduces the defect the memo
                # exists to stop. The subject for a card-scoped delta is `subjects.setdefault`'s —
                # the card's LOWEST-numbered node, which adding nodes does not move — but its
                # `proposal_ref` does move: an inline repair that finalizes a different held
                # envelope rewrites the idea and the digest with it, and a card carrying a footprint
                # takes the newest finalized node as subject instead. Each such move bought a
                # refused card-scoped delta another byte-identical row: bounded rather than
                # unbounded, which is the same re-append with a constant in front. So the subject
                # (and its `proposal_ref`) enters the key only for the node-scoped half.
                # `proposal_ref` is a dict, so it goes THROUGH the digest rather than into the
                # tuple — the fence is its value, not its identity.
                node_scoped = "footprint" in delta
                fingerprint = (
                    card_id,
                    subject[0].id if node_scoped else None,
                    subject[0].attempt if node_scoped else None,
                    canonical_json_digest({"ref": subject[1], "delta": delta} if node_scoped
                                          else {"delta": delta}))
                if fingerprint in attempted:
                    continue
            if not delta:
                continue
            node, proposal_ref = subject
            self.store.append(EV_CARD_ENRICHED, {
                "id": card_id,
                "node_id": node.id,
                "generation": node.attempt,
                "proposal_ref": proposal_ref,
                **delta,
            })
            # AFTER the append, never before: the memo's premise is "a delta that was written and
            # the fold then refused". Recording it first meant an append that RAISED (ENOSPC, EIO)
            # permanently suppressed a delta no row ever carried.
            attempted.add(fingerprint)
            appended = True
        return fold(self.store.read_all()) if appended else state

    def _due_research_trigger(self, state: RunState) -> str | None:
        """Is an AUTO deep-research trigger (cadence/strategist) due at the current node-count? Used by
        the concurrent-research seam to overlap the "think" with an in-flight eval. Mirrors the auto
        triggers in _maybe_deep_research but WITHOUT the no-pending gate (we overlap with pending evals
        on purpose). Manual requests stay on the serial path; the at_node gate (a memo recorded at this
        node-count) keeps the serial path from re-firing after the concurrent memo lands."""
        if self.deep_researcher is None:
            return None
        n = len(state.nodes)
        if n == 0 or self._already_researched_at(state, n):
            return None
        _last_research_n = max(self._cadence_research_marks(state), default=0)
        # since-last, gap-safe; `deep_research_window` is where `0` becomes "no window at all" (the
        # shipped default) and a negative becomes off. THIS is the gate that decides whether the
        # concurrent think overlaps the very first eval, which on a multi-hour node is the whole
        # feature — see `deep_research_window`'s comment for what a node-counted window measured.
        if self._cadence_due(n, _last_research_n,
                             deep_research_window(self.deep_research_every)):
            return "cadence"
        hist = state.strategy_history
        if (hist and hist[-1].get("at_node") == n
                and (hist[-1].get("strategy") or {}).get("request_research")):
            return "strategist"
        return None

    @in_llm_lane("novelty_dedup")
    def _maybe_merge_hypotheses(self, state: RunState) -> RunState:
        """Agentic consolidation of pure OPEN belief cards (P1+).
        The fold merges cards only by EXACT statement hash, so paraphrases of one belief pile up as
        separate open cards. Here —
        LIVE only, gated on `track_hypotheses` + a reflect client — hybrid retrieval clusters near-dups
        and the agent decides the true merges, appended as `hypothesis_merged` events that the fold
        applies deterministically (alias evidence -> canonical). Best-effort: never raises, never
        blocks the loop. Cadence: only when the open board has grown to >=4 and by >=2 since the last
        pass, so it doesn't re-run every node or thrash. Replay-safe — the engine only WRITES the
        decision here; on replay the fold reapplies the recorded merges with no model call, and a
        re-run finds already-merged aliases gone (converges).

        Phase 2: ALSO invoked from the concurrent eval-window background loop
        (`orchestrator._research_overlap_loop`, gated on `concurrent_consolidate`) so the board the
        repeated research keeps filling is deduped DURING a long eval, not only between nodes. That is
        safe only while Card-driven selection is disabled: `EV_HYPOTHESIS_MERGED` is in the explicit
        non-Card conditional background registry. Card mode invokes this method only from the joined
        main-task cadence, where ownership/readiness changes are serialized before selection. The
        background loop is cancelled before that serial pass, so the two never race on
        `_last_hyp_merge_n`.

        THAT ASYMMETRY IS THE STRUCTURAL HALF of the duplicate-board defect and it was re-examined
        rather than lifted (`runs/rubertlite-dr-unified-v6`: research ran concurrently four times, the
        gate below was satisfied many times over, and `hypothesis_merged` fired zero times while the
        main task sat in a 90-minute evaluation). Invariant #1's real question is not "does the fold
        read it?" but "does any reader key on its POSITION?", and for this event the answer is yes,
        twice:

          * the FOLD itself does. `replay._on_hypothesis_merged` stamps `_event_index` onto the
            receipt and `card_ledger._CardAliases.canon_at` resolves a control through only the merge
            edges durable AT that index — so a merge spliced before rather than after a `card_dropped`
            row decides whether the operator's drop lands on the alias or on the surviving canonical
            card. `tests/test_hypothesis_merge.py` drives exactly that splice.
          * `speculation._proposal_authority_seq` does. It excludes `DIAGNOSTIC_EVENTS` wholesale and
            the two LLM-accounting rows; `hypothesis_merged` is FOLDED, so it is none of those. A
            background merge landing in the window where `_prepare_node_idea` makes the Developer call
            moves the fence and discards a proposal the run has already paid for.

        So the answer to a board that fills faster than it drains is NOT to let this run in the
        background. It is to stop the background writer overfilling it: `_admissible_beliefs` bounds
        what one memo may register, which is a refusal to append and therefore moves no reader's
        position at all."""
        if not self._track_hypotheses:
            return state
        client = self._reflect_client()
        if client is None:
            return state
        # Consolidate the pure open BELIEF rows — the Card equivalent of the old open hypotheses
        # (verdict 'open' == the old status 'open'). Native work-item cards may share one belief, but are
        # excluded below because this cadence merges near-duplicate
        # research BELIEFS; collapsing a receipt-backed WORK ITEM's action identity is not its job.
        # Exclude native work-item cards by IDENTITY, not readiness — the rule, and what a readiness
        # filter cost, are now stated once at module level (`is_pure_belief`), because the append-site
        # bound `_admissible_beliefs` has to mean the SAME board this cadence merges. Merges emit
        # `hypothesis_merged` with card ids, which `_derive_cards` applies unchanged.
        # The canonical this picks is a belief, but it need not STAY one: the Researcher may later
        # mint a native work item for that exact statement, and `_card_identity_map` bridges the
        # belief hash onto the native id — one claim, one row, which is what we want. The consequence
        # (every paraphrase becomes an alias of a work item nobody has touched) is handled where it
        # is decidable, at fold time: `core/cards.py::surviving_work_item_aliases` blocks only aliases
        # that could own work. Nothing here needs to predict a card that does not exist yet.
        _pure_belief = is_pure_belief
        open_hyps = [c for c in state.open_research_cards()
                     if not c.selection_ready and _pure_belief(c)]
        n = len(open_hyps)
        # KNOWN GAP, bounded and stated (the same class as the cadence note in `strategy.py`):
        # `_last_hyp_merge_n` is IN-MEMORY only, never folded. On a fresh process after resume the
        # baseline is -1, so this gate fires unconditionally whenever the board holds >=4 pure
        # beliefs — re-running the PAID `consolidate()` (hybrid retrieval plus one merge-decision LLM
        # call) even if a prior process already consolidated exactly this board. The MERGES themselves
        # are idempotent through EV_HYPOTHESIS_MERGED, so nothing is double-applied; what is
        # re-purchased is the DECISION, and only for a cluster the prior agent decided NOT to merge,
        # since a declined merge leaves no durable record to compare against. The cost is bounded to
        # one extra pass per resume. Closing it means what the deep-research path did — a durable
        # attempt receipt claimed BEFORE the provider call (`_record_research_attempt`,
        # `curation_protocol._paid_curation_attempt`) — because an eventual `hypothesis_merged` is not a
        # payment fence: it only exists when the answer was yes.
        if n < 4 or (n - getattr(self, "_last_hyp_merge_n", -1)) < 2:
            return state
        try:
            from looplab.search.hybrid_merge import consolidate
            texts = [h.statement for h in open_hyps]
            wrote = False
            # Own trace so each hypothesis_merged event (appended INSIDE) is stamped with THIS merge's
            # trace_id — the UI can then show only the merge's own retrieval+decision trace under it.
            with self._op_span("hypothesis_merge"):   # no node_id — see strategist_consult (avoids leaking into a node's trace)
                # merge_system.md override + configured structured-output parser live on the ROLES
                # (tasks.py wires them), not the engine — resolve both via the lessons helper that
                # already walks the researcher→inner→fallback→developer chain (one lookup path,
                # not a shallow re-derivation that misses wrapped roles). getattr guard: some
                # tests build Engine via __new__ (no `lessons`); (None, "tool_call") are exactly
                # the defaults `agent_merge` assumes when nothing is wired.
                _lm = getattr(self, "lessons", None)
                _prompts, _parser = (_lm._merge_prompt_opts() if _lm is not None
                                     else (None, "tool_call"))
                for g in consolidate(texts, client, kind="research hypotheses",
                                     embed=self._embedder, goal=state.goal,
                                     prompts=_prompts, parser=_parser):
                    if len(g["members"]) < 2:
                        continue
                    ids = [open_hyps[i].id for i in g["members"]]
                    assert EV_HYPOTHESIS_MERGED in NON_CARD_SELECTION_BACKGROUND_APPENDABLE
                    self.store.append(EV_HYPOTHESIS_MERGED, {
                        "canonical": ids[0], "aliases": ids[1:], "statement": g["merged"],
                        "at_node": len(state.nodes)})
                    wrote = True
        except Exception:  # noqa: BLE001 — advisory hygiene; a merge hiccup must not disturb the loop
            # The cadence baseline is NOT consumed here. It used to be assigned before the call, so a
            # transient LLM/transport failure silently skipped the whole window and duplicates piled
            # up until the board grew another 2 past a merge that never happened.
            return state
        merged_state = fold(self.store.read_all()) if wrote else state
        # Baseline = the POST-merge open board, which is what "grown by >=2 SINCE THE LAST PASS"
        # means. Recording the pre-merge count made a successful consolidation raise its own bar:
        # merging 8 cards down to 4 left the baseline at 8, so the board had to re-grow to 10 before
        # the next pass instead of 6, and duplicates re-accumulated for far longer than the
        # documented cadence — the more effective the merge, the longer the blackout it caused.
        self._last_hyp_merge_n = len([c for c in merged_state.open_research_cards()
                                      if not c.selection_ready and _pure_belief(c)])
        return merged_state

    def _maybe_refresh_report(self, state: RunState) -> RunState:
        """Regenerate the agent-authored run report on a node-count cadence, then re-fold. No-op when
        the writer is off, when there's nothing evaluated yet, or when the report is already current
        for this node-count (the `at_node` gate makes resume a no-op). Best-effort sidecar.

        THE CREATION DECISION POINT IS `cadence.at_creation_boundary` (F1i). This gate opened with a
        bare `state.pending_nodes()` and stated no reason for it at all — `engine/cadence.py` names
        it as the one copier of the 2026-06-24 predicate that never even wrote down what it thought
        it was protecting. Since backlog F1f the observable is false for the whole life of every
        evaluation, so the phase whose entire purpose is to let the report GROW WITH THE SEARCH
        could only ever run in a drain: measured over `runs/` on 2026-08-18, `report_generated`
        with `trigger=cadence` is 26/1/0/1/0/0 across dense-retrieval / v6 / v7 / v8 / v9 / the live
        `e5small-dr-unified-v2` — zero in all three runs with no quiescent prefix, whose operators
        therefore had no mid-run narrative at all while a 47-hour search ran.

        THE MONEY RULE. Unchanged pace, and no in-process memo is needed: `serve/report.py` sets
        `content["at_node"]` OUTSIDE its try, so even a provider failure that degrades to the
        minimal report still closes the durable window — one paid report per node count however
        many times the outer loop turns at it. The report is selection-neutral narrative, so a
        mid-eval one names nodes that are still training; that is what `trigger` is for, and it is
        strictly more information than the nothing this recorded before.

        NOTE the OTHER clause is not this one and stays: `not state.evaluated_nodes()` is a refusal
        to spend a window on a run with no results yet, which is true at a creation decision point
        or not."""
        if self.report_writer is None or self.report_every <= 0:
            return state
        if not at_creation_boundary(len(state.pending_nodes()),
                                    while_evaluating=getattr(
                                        self, "_cadence_while_evaluating", False)):
            return state
        if not state.evaluated_nodes():
            return state
        n = len(state.nodes)
        last = int((state.report or {}).get("at_node") or 0)
        if not self._cadence_due(n, last, self.report_every):   # resume-safe since-last gate
            return state
        return self._write_report(state, trigger="cadence")

    def _write_report(self, state: RunState, *, trigger: str,
                      finalize_scope: str | None = None) -> RunState:
        """Generate one run report and record it as a `report_generated` event, then re-fold. Never
        raises — the writer itself degrades to a minimal report on any failure."""
        return self._write_report_with_seq(
            state, trigger=trigger, finalize_scope=finalize_scope)[0]

    @in_llm_lane("enrichment")
    def _write_report_with_seq(self, state: RunState, *, trigger: str,
                               finalize_scope: str | None = None) -> tuple[RunState, int | None]:
        """Write a report and return its event sequence for the natural-finish CAS."""
        if self.report_writer is None:
            return state, None
        with self.tracer.span("report", new_trace=True, trigger=trigger):
            content = self.report_writer.generate(state, trigger=trigger)
            # append INSIDE the span so report_generated is stamped with the report op-trace (UI scopes it).
            payload = {
                "content": content, "at_node": content.get("at_node"), "trigger": trigger,
            }
            if finalize_scope is not None:
                payload["finalize_scope"] = finalize_scope
            event = self.store.append(EV_REPORT_GENERATED, payload)
        return fold(self.store.read_all()), event.seq
