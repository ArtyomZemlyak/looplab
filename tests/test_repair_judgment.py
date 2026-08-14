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
                                            CRITIC_ACTIONS, DEFAULT_CRITIC_ACTION,
                                            coerce_critic_action, critic_due,
                                            developer_stuck_contract, format_repair_trajectory,
                                            repair_floor_stop)
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
