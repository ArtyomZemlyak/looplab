"""F8: the repair bound is a JUDGMENT, not a counter — and the judgment never reaches the record.

WHAT THIS FILE HOLDS, and why each half exists.

The stop used to be two things: the triage judge's `abandon`, and `attempt < inline_repair_attempts`.
The counter half is what both recorded disasters walked straight through:

  * `rubert-dr-0804` — 2,345 repairs on one node, 369 DISTINCT error signatures, because the broken
    `transformers`/`torch` registry renamed the symbol it failed on every attempt. Nothing counting
    repetitions ever saw one.
  * `rubertlite-dr-unified-v6` node 5 — three rounds of halving a batch size (8192 -> 256) against an
    OOM that never happened. Three attempts, nowhere near any cap; one idea tried three times.

So two signals that already existed and were unused now bound the loop, and this file drives both
through the REAL `_evaluate`:

  1. the DEVELOPER's own "I do not know how to fix this" (`core/models.py::DEVELOPER_STUCK_PREFIX`),
     a first-class outcome of the repair CALL rather than another failed attempt;
  2. a CRITIC that reads the trajectory and answers only "are these attempts addressing different
     causes, or circling one?".

And the half that matters more than either, doc 36's line: **the judgment decides whether to keep
going, never what the result was.** Every stop here terminalizes the node carrying the eval's OWN
authenticated `reason`, so no LLM verdict can move a metric, a champion, selectability or whether a
violation stands. The critic's evidence is authenticated for the same reason `c862045c` took the
stderr sentinel away from the failure classifier: the sentinel is mixed into the candidate's own
output and is forgeable, so the per-attempt CAUSE the critic compares is the engine's `reason` column
and the stderr tail is labelled as candidate-controlled text.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.core.models import (DEVELOPER_STUCK_PREFIX, developer_stuck_reason,
                                 is_developer_error, is_developer_stuck)
from looplab.engine.evaluate import (_UNLIMITED_REPAIR_CEILING, _effective_repair_cap,
                                     _repair_attempts_left)
from looplab.engine.repair_judgment import (AGENT_CRITIC_ACTIONS, CRITIC_CONTINUE, CRITIC_STOP,
                                            CRITIC_ACTIONS, CRITIC_SILENT_SOURCES,
                                            CRITIC_SOURCE_MODEL, CRITIC_SOURCE_NO_TRAJECTORY,
                                            CRITIC_SOURCE_NO_VERDICT, CRITIC_SOURCE_OUT_OF_ENUM,
                                            CRITIC_SOURCE_UNREACHABLE, CRITIC_SOURCE_UNWIRED,
                                            CRITIC_SOURCES, DEFAULT_CRITIC_ACTION,
                                            coerce_critic_action, critic_due, critic_evidence,
                                            developer_stuck_contract, format_repair_trajectory,
                                            repair_floor_stop)
from looplab.events.types import DIAGNOSTIC_EVENTS, EV_REPAIR_CRITIC_VERDICT
from tests.test_repair_stop_decision import (_GOOD, _Judge, _ScriptedDev, _drive, _emits,
                                             _lazy_import_src, _repairs, _terminals)


# --------------------------------------------------------------- the critic's verdict contract
def test_critic_vocabulary_has_one_spelling():
    """Registry (CLAUDE.md: duck-typed seams are registry-guarded). The agent's emit schema and the
    engine's coercion must both read `AGENT_CRITIC_ACTIONS` — a typo'd literal here turns a stop
    into "keep repairing", which is invisible in a passing run and is the whole incident."""
    from looplab.agents import unified_agent

    src = inspect.getsource(unified_agent.UnifiedAgent.repair_critic)
    assert '"enum": list(AGENT_CRITIC_ACTIONS)' in src, (
        "the emit schema must read the enum from the registry, never re-spell it")
    assert set(CRITIC_ACTIONS) == {"continue", "stop"}
    # Unlike triage there is no engine-minted verdict to keep off the wire: a critic has no
    # transport-failure claim of its own to make, because the triage call one line above it reached
    # the same endpoint moments earlier and already owns that diagnosis.
    assert set(AGENT_CRITIC_ACTIONS) == set(CRITIC_ACTIONS)


def test_an_unreadable_critic_verdict_never_stops_a_node():
    """The fail-closed direction, and it is the OPPOSITE of `coerce_triage_action`'s — deliberately.

    The triage judge is the only per-attempt stop, so a verdict nobody can read must not mean "keep
    spending". The critic is an ADDITIONAL veto layered over a triage judge that is still running and
    floors that are still enforced, so defaulting ITS non-answer to `stop` would let one flapped
    socket kill a node every other participant considers healthy — the same shape as the collapsed
    `unanswerable`/`unreadable` verdicts, which cost a whole run per bad emit until 2026-08-06."""
    for bad in (None, "", "STOP IT", "abandon", "halt", 7, {"a": 1}):
        assert coerce_critic_action(bad) == DEFAULT_CRITIC_ACTION == CRITIC_CONTINUE
    for good in AGENT_CRITIC_ACTIONS:
        assert coerce_critic_action(good) == good
        assert coerce_critic_action(f"  {good.upper()} ") == good
    # And the two defaults really do point opposite ways, which is the property a future reader is
    # most likely to "fix" into consistency.
    from looplab.engine.triage import DEFAULT_TRIAGE_ACTION, coerce_triage_action
    assert coerce_triage_action("nonsense") == DEFAULT_TRIAGE_ACTION != "repair"
    assert coerce_critic_action("nonsense") == CRITIC_CONTINUE


def test_critic_cadence_truth_table():
    """`critic_due` — the one place "how many attempts before a second model gets a veto" is stated.

    `attempt` is the count of DURABLE repairs already made, so 0 is the first failure, where the
    critic's question ("are these circling?") is not yet askable at all."""
    assert critic_due(0, 3) is False
    assert critic_due(2, 3) is False
    assert critic_due(3, 3) is True
    assert critic_due(99, 3) is True
    # Off, and off in every spelling an interval knob can arrive in.
    for off in (0, -1, None, True, False, "3", 2.0):
        assert critic_due(5, off) is False, off
    for bad_attempt in (None, True, "5", 2.0):
        assert critic_due(bad_attempt, 3) is False, bad_attempt


# --------------------------------------------------------------- the floors
def test_the_floors_are_the_floor_and_the_message_names_the_right_knob():
    """`repair_floor_stop` — what no judgment may cross, with a truth table instead of three inline
    comparisons that used to disagree about what 0 meant."""
    # No operator cap (the default since F8): the engine's absolute ceiling is what binds.
    assert repair_floor_stop(attempt=0, operator_cap=0, ceiling=_UNLIMITED_REPAIR_CEILING) is None
    assert repair_floor_stop(attempt=49, operator_cap=0, ceiling=50) is None
    stopped = repair_floor_stop(attempt=50, operator_cap=0, ceiling=50)
    assert stopped and "50" in stopped and "no operator cap" in stopped
    # An operator who asked for a count still gets exactly that count, and is told which knob it was:
    # somebody whose snapshot says 12 must not read a terminal implying they chose 50.
    assert repair_floor_stop(attempt=11, operator_cap=12, ceiling=50) is None
    capped = repair_floor_stop(attempt=12, operator_cap=12, ceiling=50)
    assert capped and "inline_repair_attempts" in capped and "12" in capped
    # A CAP ABOVE THE CEILING IS LEGAL AND IS THE MIRROR OF THE RULE ABOVE. `inline_repair_attempts`
    # is `ge=0` with NO upper bound, and `settings_ui_schema.json` has no `max` and explicitly
    # invites "set a number here to get the old fixed cap back" — so an operator may spell 60, the
    # node stops at the ceiling's 50, and the terminal must not tell them their setting is 0.
    assert repair_floor_stop(attempt=49, operator_cap=60, ceiling=50) is None
    over = repair_floor_stop(attempt=50, operator_cap=60, ceiling=50)
    assert over and "50" in over
    assert "is 0" not in over, over
    assert "60" in over, over
    # …and the number the JUDGE is told has to agree with the bound that will actually stop it.
    # `_effective_repair_cap` stays UNCLAMPED on purpose (an explicit cap is the operator's number),
    # so the remaining count is measured against `min(cap, ceiling)` instead — otherwise a run
    # spelling 60 tells the judge it has 11 attempts left on the turn that is about to be its last.
    assert _repair_attempts_left(0, _effective_repair_cap(60)) == _UNLIMITED_REPAIR_CEILING
    assert _repair_attempts_left(49, _effective_repair_cap(60)) == 1
    assert _repair_attempts_left(50, _effective_repair_cap(60)) == 0
    # A cap BELOW the ceiling is the bound, and is counted down verbatim.
    assert _repair_attempts_left(0, _effective_repair_cap(12)) == 12
    assert _repair_attempts_left(11, _effective_repair_cap(12)) == 1
    # No operator cap: the ceiling is the bound, which is what it always was.
    assert _repair_attempts_left(0, _effective_repair_cap(0)) == _UNLIMITED_REPAIR_CEILING
    # THE OTHER TWO FLOORS ARE NOT HERE, on purpose, and this pins that they are not re-derived:
    # the eval-time budget is compared against a FRESH fold in `_evaluate` (a stale one undercounts
    # whatever a sibling burned under `eval_parallel > 1`), and the money ceiling raises
    # `BudgetExceeded` at the LLM client. Both are enforced where the resource actually leaves the
    # account, which is what makes them untalkable-past; a parameter here that no caller passes
    # would be a rule nobody reviews.
    import inspect
    params = set(inspect.signature(repair_floor_stop).parameters)
    assert params == {"attempt", "operator_cap", "ceiling"}, params


def test_the_shipped_default_is_no_operator_count_cap():
    """F8's actual behavioural change: a fixed count is no longer the transition.

    Both defaults move together — `Settings` and `EngineOptions` disagreeing about what stops a
    repair loop is exactly the kind of drift the library/product split has produced before."""
    from looplab.core.config import Settings
    from looplab.engine.options import EngineOptions
    assert Settings().inline_repair_attempts == 0
    assert EngineOptions().inline_repair_attempts == 0
    # …but the engine still has a floor underneath, so "no operator cap" is not "unbounded".
    assert _UNLIMITED_REPAIR_CEILING > 0
    assert Settings().repair_critic_after == EngineOptions().repair_critic_after == 3


# --------------------------------------------------------------- the Developer's own verdict
def test_the_stuck_sentinel_is_not_the_developer_error_sentinel():
    """Two different facts that must never share a spelling.

    `(developer error: …)` says the Developer's SESSION failed — it routes to the provider circuit
    breaker and pauses the RUN. `(developer stuck: …)` says the session worked and the model has no
    fix left, which must stop one node and nothing else. Pausing a run because a healthy model
    admitted defeat would be the 2026-08-06 `unanswerable` defect with a new door."""
    stuck = f"{DEVELOPER_STUCK_PREFIX} the import fails inside a compiled extension)"
    assert is_developer_stuck(stuck) and not is_developer_error(stuck)
    err = "(developer error: 402 out of credits)"
    assert is_developer_error(err) and not is_developer_stuck(err)
    # Total on anything, and never a "sounds hopeless?" heuristic over a repair's prose — that is the
    # deleted error-signature normalizer's mistake with a new name.
    for other in (None, "", 7, _GOOD, "I give up", "# (developer stuck: nope)"):
        assert is_developer_stuck(other) is False, other
        assert developer_stuck_reason(other) == ""
    assert developer_stuck_reason(stuck) == "the import fails inside a compiled extension"
    assert developer_stuck_reason(f"  {DEVELOPER_STUCK_PREFIX} no idea)  ") == "no idea"


def test_the_developer_is_actually_told_it_may_decline():
    """A sentinel nobody is told about is a sentinel nobody emits — which is precisely the state F8
    found: the Developer always knew when it was beaten and had no way to say so.

    The contract text is spelled FROM the constant, so a drifted sentinel cannot leave the engine
    listening for one string while the prompt asks for another (that drift reads as a syntax error
    and charges the provider-failure counter instead of stopping the node)."""
    contract = developer_stuck_contract(DEVELOPER_STUCK_PREFIX)
    assert DEVELOPER_STUCK_PREFIX in contract
    # And it is reachable from the engine's own repair ask, not just defined. Scoped to `_evaluate`
    # rather than the whole module so a definition sitting unused elsewhere cannot satisfy it — the
    # behavioural half is `test_the_declaration_reaches_the_developer_in_the_repair_ask` above, which
    # reads the string the Developer was actually handed.
    from looplab.engine.evaluate import EvaluateMixin
    assert "developer_stuck_contract(DEVELOPER_STUCK_PREFIX)" in inspect.getsource(
        EvaluateMixin._evaluate)


def test_a_developer_that_declares_itself_stuck_ends_the_node_and_spends_nothing(tmp_path):
    """THE PROPERTY, driven end to end through the real `_evaluate`.

    A Developer that answers the repair ask with the declaration instead of code must: stop the node,
    write NO `node_repaired` (nothing was repaired), and terminalize carrying the EVAL's own reason —
    not `developer_crash`, which is the provider-outage terminal and would pause the run.

    The ordering this pins is load-bearing and easy to lose: the declaration is not valid Python, so
    if it reached `_repair_provider_failure` first it would be classified `unparseable`, charge the
    provider counter, and three declarations in would terminalize the node as `developer_crash` AND
    pause the whole RUN naming a provider that is answering perfectly."""

    class _StuckDev(_ScriptedDev):
        def repair(self, idea, code, error):
            self.repair_calls += 1
            self.errors.append(error)
            return f"{DEVELOPER_STUCK_PREFIX} every fix I can think of has already failed here)"

    dev = _StuckDev([], first=_emits("ValueError: boom\n"))
    evs, _eng = _drive(tmp_path, dev, _Judge())          # judge always says "repair"
    assert dev.repair_calls == 1, "the loop must stop on the declaration, not keep asking"
    assert _repairs(evs) == [], "nothing was repaired, so no repair may be recorded"
    term = _terminals(evs)
    assert len(term) == 1 and term[0].type == "node_failed"
    # THE LINE: the stop does not get to say what the failure WAS.
    assert term[0].data.get("reason") == "crash"
    assert "does not know how to fix" in term[0].data.get("error", "") + str(
        term[0].data.get("triage_rationale", ""))
    # …and no run-level pause: a healthy model giving up is not a provider outage.
    assert [e for e in evs if e.type == "pause"] == []


def test_the_declaration_reaches_the_developer_in_the_repair_ask(tmp_path):
    """The contract has to be in the text the Developer actually receives, not merely defined
    somewhere — the failure mode is a paragraph that exists in the repo and in no prompt."""
    dev = _ScriptedDev([_GOOD], first=_emits("ValueError: boom\n"))
    _drive(tmp_path, dev, _Judge())
    assert dev.errors, "the repair was never asked"
    assert DEVELOPER_STUCK_PREFIX in dev.errors[0]


# --------------------------------------------------------------- the critic
class _CriticJudge(_Judge):
    """A judge that never stops (always `repair`) plus a scripted critic — so whatever ends the loop
    below, it is the critic and nothing else."""

    def __init__(self, verdict=None, *, raises=False, **kw):
        super().__init__(**kw)
        self.verdict = verdict
        self.raises = raises
        self.critic_calls: list[dict] = []

    def repair_critic(self, node, *, state=None, brief="", trajectory="", attempt=None):
        self.critic_calls.append({"trajectory": trajectory, "attempt": attempt})
        if self.raises:
            raise ConnectionError("critic endpoint is gone")
        return dict(self.verdict) if self.verdict else None


def test_the_critic_stops_a_circling_chain_without_deciding_what_failed(tmp_path):
    """The critic's `stop` ends the loop — and the terminal still carries the EVAL's authenticated
    `reason`, because doc 36's line is that this judgment decides whether to keep going and never
    what the result was. If a critic verdict could set `reason`, it would reach salvage, selection
    and violations, which is the thing that must not happen."""
    judge = _CriticJudge({"action": CRITIC_STOP, "rationale": "the same cause after three fixes"})
    # Enough scripted failures that only the critic can end this.
    # Each attempt must CHANGE something, or the chain is inert rather than circling — and inert has
    # its own, earlier and cheaper rung (`repair_verify.inert_streak`, 2 in a row) that would stop
    # this node at attempt 2 on a bound with nothing to do with the critic. A circling chain is one
    # that keeps editing and keeps failing the same way, so the marker varies while the crash does
    # not; that is exactly the condition the critic exists to name.
    dev = _ScriptedDev([_emits("ValueError: boom\n") + f"# attempt {i}\n" for i in range(40)],
                       first=_emits("ValueError: boom\n"), cycle=True)
    evs, _eng = _drive(tmp_path, dev, judge, repair_critic_after=2)
    assert judge.critic_calls, "the critic was never consulted"
    # Consulted only once there is a trajectory to judge: 2 durable repairs, then stop.
    assert len(_repairs(evs)) == 2
    term = _terminals(evs)
    assert len(term) == 1 and term[0].type == "node_failed"
    assert term[0].data.get("reason") == "crash", (
        "the critic must not be able to re-classify what failed")
    assert "repair critic" in str(term[0].data.get("error", "")) + str(
        term[0].data.get("triage_rationale", ""))


def test_the_critic_is_silent_before_its_cadence_and_never_asked_on_an_empty_chain(tmp_path):
    """Asking "is this chain circling?" of a chain with nothing in it buys a paid call whose only
    honest answer is `continue`."""
    judge = _CriticJudge({"action": CRITIC_CONTINUE, "rationale": "moving"})
    dev = _ScriptedDev([_emits("ValueError: boom\n"), _GOOD], first=_emits("ValueError: boom\n"))
    _drive(tmp_path, dev, judge, repair_critic_after=5)
    assert judge.critic_calls == [], "the critic must not be consulted below its cadence"


def test_a_critic_that_cannot_be_reached_changes_nothing(tmp_path):
    """FAILS OPEN, and this is the property most likely to be "fixed" into consistency with triage.

    A critic is an extra veto. Its absence must restore the previous behaviour exactly — the node
    finishes on the triage judge and the floors, with no terminal and no pause attributable to the
    critic. Compare `_triage_crash`, where the same exception IS the dead-provider signal, because
    there it is the only stop."""
    judge = _CriticJudge(raises=True)
    # The two failing repairs must differ from each other and from `first`, or the second one is an
    # EMPTY change set and `repair_verify`'s inert rung terminalizes the node before the critic is
    # reached — a different bound testing a different thing. Same reason as the circling case above.
    dev = _ScriptedDev([_emits("ValueError: boom\n") + "# a\n",
                        _emits("ValueError: boom\n") + "# b\n", _GOOD],
                       first=_emits("ValueError: boom\n"))
    evs, _eng = _drive(tmp_path, dev, judge, repair_critic_after=1)
    assert judge.critic_calls, "the critic was never consulted"
    term = _terminals(evs)
    assert len(term) == 1 and term[0].type == "node_evaluated", (
        "an unreachable critic must not cost the node")
    assert [e for e in evs if e.type == "pause"] == []


def test_the_critic_reads_the_authenticated_cause_and_is_told_stderr_is_not(tmp_path):
    """`c862045c` MUST NOT BE UNDONE. It took the stderr sentinel away from the failure classifier
    because the sentinel is mixed into the candidate's own output and is forgeable — a candidate that
    can write "what kind of failure this was" can make three different causes look like one, or one
    look like three, and drive the stop decision either way.

    So the per-attempt `cause` the critic compares is the engine's `reason` (from `_failure_reason`
    over the sandbox's out-of-band signals), it is carried durably on `node_repaired`, and the stderr
    tail is labelled in the prompt as candidate-controlled."""
    judge = _CriticJudge({"action": CRITIC_CONTINUE, "rationale": "moving"})
    dev = _ScriptedDev([_emits("ValueError: boom\n"), _emits("ValueError: boom\n"), _GOOD],
                       first=_emits("ValueError: boom\n"))
    evs, _eng = _drive(tmp_path, dev, judge, repair_critic_after=1)
    rows = _repairs(evs)
    assert rows and all(r.data.get("reason") == "crash" for r in rows), (
        "the authenticated cause must be durable, or a resumed critic reads a blank column")
    seen = judge.critic_calls[0]["trajectory"]
    assert "cause = crash" in seen
    assert "candidate-controlled" in seen and "authoritative" in seen


def test_a_row_from_before_the_cause_column_says_so_rather_than_guessing():
    """"We do not know what that attempt's cause was" and "it was the same as last time" are exactly
    the two answers the critic is asked to tell apart, so a legacy row must not default to one.

    Same rule as `_format_repair_log`'s missing `changed` key, and it is additive-only per invariant
    #5: a log written before this column existed simply omits it."""
    rendered = format_repair_trajectory([
        {"attempt": 1, "error": "boom", "fix": "widen the import", "stages_passed": 0},
        {"attempt": 2, "error": "boom", "fix": "widen it more", "stages_passed": 0,
         "reason": "crash", "changed": ["train.py"]},
    ])
    assert "(not recorded" in rendered            # the legacy row
    assert "cause = crash" in rendered            # the current one
    assert format_repair_trajectory([]) == "" and format_repair_trajectory(None) == ""


def test_the_critic_never_gets_a_verdict_that_extends_the_loop():
    """The action space is deliberately narrow: `stop` and `continue`, where `continue` is only ever
    "no opinion". There is no verdict here that makes the loop repair MORE than the triage judge and
    the floors already allow — doc 36's second corollary, a wider action space must not widen the
    trusted set."""
    from looplab.engine import evaluate
    src = inspect.getsource(evaluate)
    # The only consumption of a critic verdict in the engine is the stop (the second occurrence of
    # the name is the import that binds it).
    assert src.count('== CRITIC_STOP') == 1
    assert "CRITIC_CONTINUE" not in src, (
        "the engine must not branch on `continue` — a critic that says nothing must be "
        "indistinguishable from a critic that is not wired")


# ------------------------------------------------- the verdict RECORD (2026-08-15, backlog §0.2)
# THE GAP THIS CLOSES. The critic was consulted and left no trace of what it ANSWERED: its
# `repair_critic` span carried `{attempt, node_id, generation}` and nothing else, a `continue`
# appended nothing at all, and a STOP was visible only indirectly as the `abandon` triage_action it
# produced. Measured on the live `rubertlite-dr-unified-v8`: several consultations, `events: []` on
# every span, and ZERO occurrences of the word "critic" anywhere in `events.jsonl` — while the
# verdicts were RIGHT, because those chains were genuinely progressing. That is exactly the case
# where a missing record goes unnoticed.
#
# WHY IT IS NOT BOOKKEEPING: F8's premise is that a JUDGEMENT replaces a counter as the stop rule,
# and a judgement that leaves no trace cannot be reviewed, tuned or trusted. `repair_critic_after`
# cannot be calibrated by anyone who cannot see what the critic has been saying.
def _critic_rows(evs):
    return [e for e in evs if e.type == EV_REPAIR_CRITIC_VERDICT and e.data.get("node_id") == 0]


def test_the_verdict_row_is_diagnostic_and_its_vocabularies_collide_with_nothing():
    """Registry (CLAUDE.md), in every direction — FOUR vocabularies now ride on one repair chain's
    durable record and none of them may be readable as another.

    `node_repaired` already carries a `triage_action` (a model's judgement) beside a `verified` (the
    engine's byte comparison); `repair_critic_verdict` adds a `verdict` (a second model's judgement)
    beside a `source` (the engine's own account of which branch produced it). Merging any pair breaks
    an emit schema's enum in one direction and makes a reader treat it as exhaustive in the other —
    `metric_salvage.SALVAGE_CAUSE_TRIAGE_ACTION`'s rule, inherited twice over."""
    from looplab.engine.metric_salvage import SALVAGE_CAUSE_TRIAGE_ACTION
    from looplab.engine.repair_verify import REPAIR_VERDICTS
    from looplab.engine.triage import (AGENT_TRIAGE_ACTIONS, TRIAGE_ACTIONS, coerce_triage_action)

    for vocab in (set(CRITIC_ACTIONS), set(CRITIC_SOURCES)):
        assert not (vocab & set(TRIAGE_ACTIONS)), vocab & set(TRIAGE_ACTIONS)
        assert not (vocab & set(AGENT_TRIAGE_ACTIONS))
        assert not (vocab & set(REPAIR_VERDICTS)), vocab & set(REPAIR_VERDICTS)
        assert SALVAGE_CAUSE_TRIAGE_ACTION not in vocab
    # A PROVENANCE IS NOT A VERDICT, and this is the enforcement rather than the documentation: no
    # member of `CRITIC_SOURCES` may arrive off the wire as an action, in EITHER judge's coercion.
    # `model` coercing to a stop would be a route from "who answered" into "what was decided".
    assert not (set(CRITIC_SOURCES) & set(CRITIC_ACTIONS))
    for source in CRITIC_SOURCES:
        assert coerce_critic_action(source) == CRITIC_CONTINUE
        assert coerce_triage_action(source) not in CRITIC_SOURCES
    # ...and the agent's emit schema offers the ACTION enum and never the source: the model is not
    # invited to say where its own answer came from.
    from looplab.agents import unified_agent
    schema_src = inspect.getsource(unified_agent.UnifiedAgent.repair_critic)
    assert "CRITIC_SOURCES" not in schema_src
    for source in CRITIC_SOURCES:
        assert f'"{source}"' not in schema_src, source
    # The row itself: FOLD-IGNORED, which is what makes a `continue` incapable of moving anything.
    assert EV_REPAIR_CRITIC_VERDICT in DIAGNOSTIC_EVENTS
    # And the partition test in tests/test_event_types.py covers the other half (registered + not
    # folded); this pins the membership the append site asserts.
    from looplab.engine import evaluate as ev_mod
    # OPEN[review-guard-substring-pin] this file's own tier-3 pin is evadable by a comment.
    # proof:present:ev_mod.EvaluateMixin._evaluate)@tests/test_repair_judgment.py
    # REVIEW 2026-08-18 (guard-test): a POSITIVE SUBSTRING pin — the exact residue class CLAUDE.md's
    # guard-test ladder sends to tier 3, because it is satisfied by deleting the in-code assert in
    # `evaluate.py::_evaluate` and leaving a comment carrying the literal. The next test down already
    # uses tests/_source_scan.py; use its AST helpers here too (e.g. `names_read` on `_evaluate`, or
    # walk `function_tree` for a real ast.Assert reading both names) so a commented-out copy of the
    # membership assert cannot keep this green.
    assert "assert EV_REPAIR_CRITIC_VERDICT in DIAGNOSTIC_EVENTS" in inspect.getsource(
        ev_mod.EvaluateMixin._evaluate)


def test_the_source_registry_is_two_way():
    """Registry guard, both directions (CLAUDE.md): a member nobody mints is a dead literal the next
    reader will mis-type, and a site that minted a source outside the registry would be a provenance
    no reader can interpret — the exact failure `REPAIR_VERDICTS` and `TRIAGE_ACTIONS` are guarded
    against, on a vocabulary that now rides the same repair chain's record.

    AST, not substrings (tier 3): the constants are named in prose in both modules' docstrings, so a
    text scan would read the documentation and call it coverage."""
    from tests._source_scan import names_read

    from looplab.engine import crash_repair, repair_judgment

    declared = {n for n in vars(repair_judgment) if n.startswith("CRITIC_SOURCE_")}
    assert {getattr(repair_judgment, n) for n in declared} == set(CRITIC_SOURCES), (
        "every declared source constant must be in the registry and vice versa")
    minted = (names_read(crash_repair.CrashRepairMixin._repair_critic)
              | names_read(crash_repair._coerced_critic_verdict))
    assert declared <= minted, declared - minted        # nothing dead
    assert (minted & declared) == declared              # ...and nothing minted from elsewhere
    # The other direction as a NEGATIVE pin — what must not come back is the TEXT of a hand-spelled
    # source at either minting site (CLAUDE.md: negative pins stay substrings on purpose).
    for site in (inspect.getsource(crash_repair.CrashRepairMixin._repair_critic),
                 inspect.getsource(crash_repair._coerced_critic_verdict)):
        for value in CRITIC_SOURCES:
            assert f'"{value}"' not in site, value
    # `no_trajectory` is DEFENSIVE and unreachable through `_evaluate` today: `critic_due` requires
    # at least one durable repair, so the trajectory is never empty at the call site. It stays in the
    # registry rather than being deleted because `_repair_critic` is duck-typed on `repair_log` and a
    # caller handing it an empty list must produce a named row, not an unnamed one.
    assert critic_due(1, 1) and not critic_due(0, 1)


def test_the_source_truth_table_separates_five_kinds_of_continue():
    """`continue` is five different facts and the operator could not tell them apart.

    "Six consultations, zero stops" reads as "the critic looked and was satisfied"; it reads
    IDENTICALLY when the endpoint was down six times, when no critic was wired, and when the model
    answered a word the coercion could not read. Those have opposite meanings — one says the
    threshold is well placed, the others say the mechanism is not running — and `repair_critic_after`
    cannot be tuned without the difference."""
    from looplab.engine.crash_repair import _coerced_critic_verdict

    ok = _coerced_critic_verdict({"action": "stop", "rationale": "same cause three times"})
    assert ok == {"action": CRITIC_STOP, "rationale": "same cause three times",
                  "source": CRITIC_SOURCE_MODEL}
    # A REAL `continue` is `model`, and this is the pair the whole registry exists for.
    assert _coerced_critic_verdict({"action": "CONTINUE ", "rationale": "moving"})["source"] == (
        CRITIC_SOURCE_MODEL)
    for bad in ("halt", "abandon", "", None, 7, {"a": 1}):
        v = _coerced_critic_verdict({"action": bad, "rationale": "x"})
        assert v["action"] == CRITIC_CONTINUE and v["source"] == CRITIC_SOURCE_OUT_OF_ENUM, bad
    for nothing in (None, "", 7, [], object()):
        v = _coerced_critic_verdict(nothing)
        assert v["action"] == CRITIC_CONTINUE and v["source"] == CRITIC_SOURCE_NO_VERDICT, nothing
    # The rationale is bounded exactly as it was before the source column existed.
    assert len(_coerced_critic_verdict({"action": "stop", "rationale": "y" * 900})["rationale"]) == 300
    # The silent set is a PARTITION of the vocabulary against `model`, so a reader asking "did the
    # critic actually have an opinion?" has one name for it instead of re-deriving the tuple.
    assert set(CRITIC_SILENT_SOURCES) | {CRITIC_SOURCE_MODEL} == set(CRITIC_SOURCES)
    assert CRITIC_SOURCE_MODEL not in CRITIC_SILENT_SOURCES


def test_the_evidence_digest_keeps_a_missing_cause_missing():
    """The evidence beside the verdict is what makes the verdict checkable — "stop, the same cause
    after three fixes" is reviewable only against the causes the critic saw.

    It carries the ENGINE's columns, not the rendered prompt: `format_repair_trajectory` is many KB
    of candidate-controlled stderr per attempt, and a `None` cause must stay `None`, because "we do
    not know what that attempt's cause was" and "it was the same as last time" are precisely the two
    answers the critic is asked to tell apart (invariant #5's additive-only rule, reader side)."""
    rows = critic_evidence([
        {"attempt": 1, "error": "boom", "fix": "widen the import", "stages_passed": 0},
        {"attempt": 2, "reason": "crash", "changed": ["train.py", "cfg.py"], "verified": "unmet"},
        {"attempt": 3, "reason": "oom", "changed": [], "verified": "inert"},
        "not a row",
    ])
    assert rows == [
        {"attempt": 1, "cause": None, "changed": None, "verified": None},
        {"attempt": 2, "cause": "crash", "changed": 2, "verified": "unmet"},
        {"attempt": 3, "cause": "oom", "changed": 0, "verified": "inert"},
    ]
    assert critic_evidence([]) == [] and critic_evidence(None) == []
    # Bounded, so one pathological chain cannot write an unbounded row.
    assert len(critic_evidence([{"attempt": i} for i in range(50)])) == 12
    # It is NOT a re-parse of the prompt: a reviewer must read the same columns the loop recorded,
    # never a re-derivation from the prose handed to the model. AST (CLAUDE.md tier 3), because the
    # renderer is named in this function's own docstring and a substring pin would read that.
    from tests._source_scan import called_names
    assert "format_repair_trajectory" not in called_names(critic_evidence)


def test_a_continue_verdict_is_durable_with_the_evidence_it_judged(tmp_path):
    """THE PROPERTY, driven: the row that used to be nothing at all.

    A `continue` changes nothing about the run — which is exactly why it was invisible, and exactly
    why it has to be recorded: it is the verdict an operator has to see to know the critic is running
    and to judge whether `repair_critic_after` is placed where it earns its call."""
    judge = _CriticJudge({"action": CRITIC_CONTINUE, "rationale": "the cause moved each time"})
    dev = _ScriptedDev([_emits("ValueError: boom\n") + "# a\n",
                        _emits("ValueError: boom\n") + "# b\n", _GOOD],
                       first=_emits("ValueError: boom\n"))
    evs, _eng = _drive(tmp_path, dev, judge, repair_critic_after=1)
    rows = _critic_rows(evs)
    assert rows, "a consultation that answered `continue` must still be recorded"
    assert len(rows) == len(judge.critic_calls)
    first = rows[0].data
    assert first["verdict"] == CRITIC_CONTINUE
    assert first["source"] == CRITIC_SOURCE_MODEL, "a real answer must not read as a fail-open"
    assert first["rationale"] == "the cause moved each time"
    # The cadence that fired, beside the chain it fired on — the two numbers the calibration
    # question ("was this consultation worth paying for?") cannot be answered without.
    assert first["after"] == 1
    assert first["attempt"] == first["durable_repairs"] + 1
    # ...and the AUTHENTICATED evidence, joinable to this node's `node_repaired` rows.
    assert first["judged"] and all(r["cause"] == "crash" for r in first["judged"])
    assert [r["attempt"] for r in first["judged"]] == [
        r.data["attempt"] for r in _repairs(evs)[:len(first["judged"])]]
    # The node finished normally: a recorded `continue` is not a stop.
    term = _terminals(evs)
    assert len(term) == 1 and term[0].type == "node_evaluated"


def test_the_span_learns_the_answer_too_but_is_not_the_record(tmp_path):
    """The cheaper HALF of the record: the verdict beside the model's own prompt/completion I/O,
    where somebody debugging a bad verdict is already looking.

    It is not the record, and the reason is the whole design argument: `spans.jsonl` is OPTIONAL
    (tracing may be off) and DESTROYABLE (`serve/trace_clear.py` is an operator-facing button,
    `serve/reset_transaction.py` removes it), while F8's premise is that a judgement replaces a
    counter as the stop rule. The pin below is that the attributes are set INSIDE the span — a span
    is written on CLOSE, so an attribute set after the `with` reaches an already-flushed record."""
    import json

    judge = _CriticJudge({"action": CRITIC_CONTINUE, "rationale": "moving on"})
    dev = _ScriptedDev([_emits("ValueError: boom\n") + "# a\n",
                        _emits("ValueError: boom\n") + "# b\n", _GOOD],
                       first=_emits("ValueError: boom\n"))
    _evs, eng = _drive(tmp_path, dev, judge, repair_critic_after=1)
    eng.tracer.shutdown()
    rows = [json.loads(line) for line in
            (tmp_path / "run" / "spans.jsonl").read_text(encoding="utf-8").splitlines()]
    spans = [r for r in rows if r.get("name") == "repair_critic"]
    assert spans, "the critic span must still be opened"
    for span in spans:
        attrs = span["attributes"]
        assert attrs["verdict"] == CRITIC_CONTINUE
        assert attrs["critic_source"] == CRITIC_SOURCE_MODEL
        assert attrs["critic_rationale"] == "moving on"
    # The span carried ONLY `{attempt, node_id, generation}` before 2026-08-15 — the fact of a
    # consultation with nothing about its answer. That is the state this closes.
    assert set(spans[0]["attributes"]) > {"attempt", "node_id", "generation"}


def test_a_stop_records_its_reason_before_the_chain_ends(tmp_path):
    """The other direction, and the ordering is load-bearing: the row is appended BEFORE the loop
    acts on the verdict, so a chain that ends here still has its reason durable rather than only as
    prose inside the terminal's triage text."""
    judge = _CriticJudge({"action": CRITIC_STOP, "rationale": "the same cause after three fixes"})
    dev = _ScriptedDev([_emits("ValueError: boom\n") + f"# attempt {i}\n" for i in range(40)],
                       first=_emits("ValueError: boom\n"), cycle=True)
    evs, _eng = _drive(tmp_path, dev, judge, repair_critic_after=2)
    rows = _critic_rows(evs)
    assert [r.data["verdict"] for r in rows] == [CRITIC_STOP]
    assert rows[0].data["source"] == CRITIC_SOURCE_MODEL
    assert rows[0].data["rationale"] == "the same cause after three fixes"
    assert rows[0].data["durable_repairs"] == 2 == rows[0].data["after"]
    term = _terminals(evs)
    assert len(term) == 1 and term[0].type == "node_failed"
    # The verdict row precedes the terminal it explains — order in the log, not just in the code.
    assert rows[0].seq < term[0].seq
    # THE LINE IS UNMOVED: the verdict is the REASON beside the terminal, never a second authority
    # for it. The node still carries the eval's own authenticated classification.
    assert term[0].data.get("reason") == "crash"
    assert rows[0].data["verdict"] not in (term[0].data.get("reason"),)


def test_a_chain_too_short_to_consult_the_critic_records_nothing(tmp_path):
    """The negative control. A row per consultation means NO row when there was no consultation —
    otherwise the log would suggest a judgement was made on every short chain, and the count an
    operator calibrates `repair_critic_after` against would be the count of repairs."""
    judge = _CriticJudge({"action": CRITIC_CONTINUE, "rationale": "moving"})
    dev = _ScriptedDev([_emits("ValueError: boom\n"), _GOOD], first=_emits("ValueError: boom\n"))
    evs, _eng = _drive(tmp_path, dev, judge, repair_critic_after=5)
    assert judge.critic_calls == [], "the critic must not be consulted below its cadence"
    assert _critic_rows(evs) == [], "no consultation, no row"
    assert _repairs(evs), "the chain really did repair — the absence is the critic's, not the loop's"


def test_an_unreachable_critic_is_recorded_as_SILENT_rather_than_as_approval(tmp_path):
    """The row that makes the whole record worth having, and the one a stop-shaped log could never
    hold: the critic did not answer, the loop correctly carried on, and the operator can SEE that
    "no stops" meant "no opinion" rather than "six approvals".

    Behaviour is unchanged — an unreachable critic still costs the node nothing (see
    `test_a_critic_that_cannot_be_reached_changes_nothing`); only the record is new."""
    judge = _CriticJudge(raises=True)
    dev = _ScriptedDev([_emits("ValueError: boom\n") + "# a\n",
                        _emits("ValueError: boom\n") + "# b\n", _GOOD],
                       first=_emits("ValueError: boom\n"))
    evs, _eng = _drive(tmp_path, dev, judge, repair_critic_after=1)
    rows = _critic_rows(evs)
    assert rows and all(r.data["verdict"] == CRITIC_CONTINUE for r in rows)
    assert all(r.data["source"] == CRITIC_SOURCE_UNREACHABLE for r in rows)
    assert all(r.data["source"] in CRITIC_SILENT_SOURCES for r in rows)
    # ...and the node still finished on the triage judge and the floors, with no pause.
    term = _terminals(evs)
    assert len(term) == 1 and term[0].type == "node_evaluated"
    assert [e for e in evs if e.type == "pause"] == []


def test_a_critic_that_is_not_wired_at_all_is_recorded_and_named(tmp_path):
    """`not_wired` — the branch that opens no span at all, which is half of why the span could never
    be the record. `_Judge` has no `repair_critic` attribute."""
    dev = _ScriptedDev([_emits("ValueError: boom\n") + "# a\n",
                        _emits("ValueError: boom\n") + "# b\n", _GOOD],
                       first=_emits("ValueError: boom\n"))
    evs, _eng = _drive(tmp_path, dev, _Judge(), repair_critic_after=1)
    rows = _critic_rows(evs)
    assert rows and {r.data["source"] for r in rows} == {CRITIC_SOURCE_UNWIRED}
    assert all(r.data["verdict"] == CRITIC_CONTINUE for r in rows)
    assert all(r.data["judged"] for r in rows), (
        "the row still says what chain the un-wired critic would have judged")
    # The two sources that never reach a span at all are exactly the two early returns — half the
    # reason `spans.jsonl` cannot be the record. (`unreachable` is the third: its span closes as an
    # error carrying no verdict.)
    from looplab.engine import crash_repair as _cr
    critic_src = inspect.getsource(_cr.CrashRepairMixin._repair_critic)
    for early in ("CRITIC_SOURCE_UNWIRED", "CRITIC_SOURCE_NO_TRAJECTORY"):
        assert critic_src.rindex(early) < critic_src.index("self.tracer.span"), early


def test_a_verdict_moves_nothing_selection_side(tmp_path):
    """THE PROPERTY THAT MUST STAY TRUE AND CHECKABLE: the critic can only stop.

    Three ways, because "the fold ignores it" is only one of them and invariant #1 says the question
    is never "does the fold read it?" but "does any reader key on its position?"."""
    from looplab.core.models import Event
    from looplab.events.replay import fold

    judge = _CriticJudge({"action": CRITIC_CONTINUE, "rationale": "moving"})
    dev = _ScriptedDev([_emits("ValueError: boom\n") + "# a\n",
                        _emits("ValueError: boom\n") + "# b\n", _GOOD],
                       first=_emits("ValueError: boom\n"))
    evs, _eng = _drive(tmp_path, dev, judge, repair_critic_after=1)
    rows = _critic_rows(evs)
    assert rows, "nothing to prove neutrality about"

    # 1. THE FOLD. A real run's state is byte-identical with the verdict rows removed — so no
    #    metric, champion, selectability or violation can be standing on one.
    without = [e for e in evs if e.type != EV_REPAIR_CRITIC_VERDICT]
    assert fold(evs) == fold(without)

    # 2. POSITION. Spliced anywhere in the log, the fold is the same object — invariant #5's
    #    order-tolerance, driven rather than asserted.
    probe = Event(seq=9999, ts=9999.0, type=EV_REPAIR_CRITIC_VERDICT,
                  data={"node_id": 0, "generation": 0, "attempt": 99, "durable_repairs": 98,
                        "after": 1, "verdict": CRITIC_STOP, "source": CRITIC_SOURCE_MODEL,
                        "rationale": "spliced", "judged": []})
    ref = fold(without)
    for pos in range(len(without) + 1):
        assert fold(without[:pos] + [probe] + without[pos:]) == ref, pos

    # 3. THE PAID-PROPOSAL CAS FENCE, the reader that a diagnostic moved once already (invariant #1:
    #    a `train_monitor_alert` landing in `_prepare_node_idea`'s window discarded a Developer call
    #    the run had paid for). The fence excludes `DIAGNOSTIC_EVENTS` WHOLESALE and drives it from
    #    the registry, so this type inherits the exclusion — pinned here because the append happens
    #    from an eval child, i.e. exactly the concurrent writer that lands in that window.
    from looplab.engine.speculation import SpeculationMixin
    assert SpeculationMixin._proposal_authority_seq(evs) == (
        SpeculationMixin._proposal_authority_seq(without))

    # 4. WHAT THE ENGINE READS OFF THE VERDICT, exhaustively: three keys, and the only one that
    #    reaches control flow is `action`, compared once against `CRITIC_STOP`. `source` and
    #    `rationale` are written to the diagnostic row and never branched on. Derived from the real
    #    subscripts rather than counted, so adding a fourth read is a red test naming the key.
    import re

    from looplab.engine import evaluate as ev_mod
    body = inspect.getsource(ev_mod.EvaluateMixin._evaluate)
    assert set(re.findall(r'critic\.get\(\s*"(\w+)"', body)) == {"action", "source", "rationale"}
    assert body.count("== CRITIC_STOP") == 1
    # ...and the verdict object itself is never handed to anything: a call taking `critic` is how a
    # stop-only signal becomes an input to something that is not a stop.
    assert not re.search(r"\w+\(\s*critic\s*[,)]", body), "the verdict must not be passed anywhere"


def test_a_cap_above_the_engine_ceiling_is_never_reached_and_the_terminal_says_so(tmp_path):
    """The mirror of `test_an_always_repair_judge_under_zero_now_terminates`, driven.

    `inline_repair_attempts` is `Field(ge=0)` with no upper bound and the settings UI schema offers
    no `max` — it invites "set a number here to get the old fixed cap back" — so 60 is a legal
    setting. The node stops at `_UNLIMITED_REPAIR_CEILING`, and the terminal used to read *"this run
    sets no operator cap (inline_repair_attempts is 0 …)"* to an operator whose snapshot plainly
    says 60, which is `repair_floor_stop`'s own stated invariant read backwards.
    """
    cap = _UNLIMITED_REPAIR_CEILING + 10
    dev = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
    evs, _ = _drive(tmp_path, dev, _Judge(), inline_repair_attempts=cap, wall=300)

    # The CEILING bound it, not the operator's number.
    assert len(_repairs(evs)) == _UNLIMITED_REPAIR_CEILING
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed"
    why = terminal[0].data["triage_rationale"]
    assert "absolute ceiling" in why
    assert "inline_repair_attempts is 0" not in why, why
    assert str(cap) in why, why
    # …and the judge was never told it had attempts the ceiling would not give it.
    told = [row.data.get("attempts_left") for row in evs
            if row.type == "node_repaired" and isinstance(row.data, dict)
            and row.data.get("attempts_left") is not None]
    assert all(value <= _UNLIMITED_REPAIR_CEILING for value in told), told


def test_the_license_is_priced_at_the_chains_largest_declaration_not_its_latest():
    """The spend and the license have to be priced under the same declarations.

    `chain_seconds` accumulates wall-clock earned under every manifest the chain has run, while the
    pipeline cost is re-resolved from the CURRENT one — so a repair that legitimately shrinks its
    declared stage timeouts (right-sizing after an epoch cut) retroactively re-priced seconds that
    were inside the license when they were spent. Driven as the pure decision the engine makes: the
    same spend, against the same cap, answered by the two prices.
    """
    from looplab.engine.repair_judgment import repair_redone_work_stop

    big, small, cap = 3600.0, 600.0, 2       # licenses 10,800s vs 1,800s
    spent = 4000.0                           # earned while the pipeline still declared 3600s

    assert repair_redone_work_stop(chain_seconds=spent, pipeline_seconds=big,
                                   retrain_cap=cap) is None
    stop = repair_redone_work_stop(chain_seconds=spent, pipeline_seconds=small, retrain_cap=cap)
    assert stop is not None                  # the pre-fix answer: abandoned on a cheaper manifest
    assert "1800s" in stop and "600s" in stop, stop

    # ...and the engine prices against a RUNNING MAXIMUM over the chain's declarations, so the
    # license survives a later, smaller one. Driven by simulating the accumulator over a real
    # sequence rather than by restating the first assertion: `max(big, small)` IS `big`, so the line
    # this replaces asserted `f(big) is None` for the second time and added nothing. Two orders,
    # because a running maximum is order-insensitive and a "keep the latest" bug is not.
    for declarations in ([big, small], [small, big], [big, small, small]):
        high = 0.0
        for declared in declarations:
            high = max(high, declared)
        assert repair_redone_work_stop(chain_seconds=spent, pipeline_seconds=high,
                                       retrain_cap=cap) is None, declarations

    # The negative control the assertions above cannot supply: a chain that only ever declared the
    # SMALL pipeline really is stopped. Without this, every line here would pass on a rule that
    # never stops anything.
    only_small = repair_redone_work_stop(chain_seconds=spent, pipeline_seconds=small,
                                         retrain_cap=cap)
    assert only_small is not None, "the stop must still fire when the license was never earned"


def test_the_engine_feeds_that_floor_a_running_maximum_and_not_the_latest_pipeline():
    """Pinned on the call itself: the value handed to `repair_redone_work_stop` must be the
    chain-scoped high-water mark, not the attempt's freshly-resolved number. Driving it would mean
    standing up a repo eval and two manifests for one `max()`; the AST is rung 3 of the ladder and
    the arithmetic either side of it is driven above."""
    import ast

    from tests._source_scan import function_tree
    from looplab.engine.evaluate import EvaluateMixin

    tree = function_tree(EvaluateMixin._evaluate)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "repair_redone_work_stop"]
    assert len(calls) == 1, "the floor is decided in one place; re-derive this test"
    priced = {kw.arg: ast.unparse(kw.value) for kw in calls[0].keywords}
    assert priced.get("pipeline_seconds") == "chain_pipeline_s", priced
    # and that name is a running maximum, not a rebinding of the per-attempt resolution
    maxima = [n for n in ast.walk(tree)
              if isinstance(n, ast.Assign)
              and any(getattr(t, "id", None) == "chain_pipeline_s" for t in n.targets)
              and isinstance(n.value, ast.Call) and getattr(n.value.func, "id", "") == "max"]
    assert len(maxima) == 1, ast.unparse(tree)[:200]

    # ...AND ITS OPERANDS, which this test did not constrain until 2026-08-20. "is a call to `max`"
    # is satisfied by `max(chain_pipeline_s, chain_pipeline_s)` (the accumulator never rises) and by
    # `max(_pipeline_s, _pipeline_s)` (the pre-fix "latest manifest" pricing this whole rung exists
    # to replace). Both restore the defect while leaving every assertion above green, which is the
    # same shape as a positive substring pin: the guard names the mechanism and not the property.
    #
    # The property is that the new value is folded INTO the accumulator: one operand must be
    # `chain_pipeline_s` itself, and at least one other must be a DIFFERENT expression.
    operands = [ast.unparse(a) for a in maxima[0].value.args]
    assert "chain_pipeline_s" in operands, (
        f"the accumulator is not an operand of its own max — {operands} is a rebinding, not a "
        "running maximum")
    assert len(set(operands)) > 1, (
        f"`max` is applied to one repeated expression {operands}; a running maximum has to fold in "
        "the freshly resolved pipeline cost")
