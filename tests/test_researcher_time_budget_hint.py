"""docs/29 F1h — the roles are told the per-eval WALL-CLOCK budget, in both prompts and to both roles.

F1b's twin, one axis over, and the same sentence: *a role asked to size a request against a budget it
cannot see will get it wrong at some rate, and there is no reason for that rate to be non-zero*.

Measured on `rubertlite-dr-unified-v7`, 2026-08-14. Node 0's second attempt paced at
`50/35300 [00:42<7:50:00, 1.25it/s]` — a 7 h 50 m schedule against a 21600 s (6 h) per-eval budget —
and the Developer had declared `timeout: 172800` on the `train` stage, 48 hours, after being asked
for "a GENEROUS timeout" against a number it was never given.

The honest scope, because half of this was already shipped and the other half was not:

* the RESEARCHER already heard a number on repo tasks (`_cue_experiment_time_budget`, present 54
  times verbatim in v7's own spans.jsonl). What it never heard is that number on the SCRIPT path, or
  anything at all about what its own `eval_timeout` may do — governed by `agent_control.timeout`,
  clamped by `max_eval_timeout`. That is `_time_budget_hint`.
* the DEVELOPER heard nothing anywhere, and it is the role that picks the batch size, writes the loop
  and declares the stage `timeout`. That is `repo_developer._time_budget_note`.

Both quote ONE derivation (`command_eval.eval_spec_time_budget`), because two roles sizing one
schedule between them must be given one number.

Everything here is offline: fake clients, the toy task, no provider.
"""
from __future__ import annotations

import re
import pytest

import looplab.engine.orchestrator as _orch
from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import (RESEARCHER_HINT_ATTRS, RESEARCHER_PROMPT_CUES, LLMResearcher,
                                  ToyObjectiveDeveloper, ToyResearcher)
from looplab.core.models import Idea, RunState
from looplab.engine.orchestrator import Engine
from looplab.engine.shared import effective_eval_time_budget
from looplab.runtime.command_eval import eval_spec_time_budget
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.foresight import ForesightPanelResearcher
from looplab.search.policy import GreedyTree

from tests._source_scan import called_names


# --------------------------------------------------------------------------- fixtures / fakes
class _ToolEmitClient:
    """`parse_structured(tool_call)` fake: records the first message list, emits a valid Idea."""

    def __init__(self):
        self.messages = None

    def complete_tool(self, messages, json_schema):
        if self.messages is None:
            self.messages = [dict(m) for m in messages]
        return {"operator": "draft", "params": {}, "rationale": "r"}


def _engine(run_dir, **kw) -> Engine:
    task = ToyTask()
    researcher = ToyResearcher(task.bounds, seed=task.seed, step=task.step)
    researcher.client = object()          # mark it LLM-backed, as `_build_calls_an_llm` reads it
    return Engine(run_dir, task=task, researcher=researcher,
                  developer=ToyObjectiveDeveloper(), sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=2), n_seeds=2, max_nodes=2, **kw)


def _stamp(engine, researcher) -> str:
    """Drive the engine's real per-proposal stamp and return what the Researcher will read."""
    engine._set_complexity_hint(RunState(goal="g", direction="min"), None, researcher=researcher)
    return getattr(researcher, "_time_budget_hint", "<UNSET>")


# --------------------------------------------------------------------------- the rule itself
@pytest.mark.parametrize("spec,expected", [
    ({"timeout": 21600.0}, 21600.0),                                   # v7's own shape
    ({"timeout": 600.0, "profiles": {"full": {"timeout": 18000.0}}}, 18000.0),   # a node picks a
    ({"timeout": 18000.0, "profiles": {"smoke": {"timeout": 60.0}}}, 18000.0),   # profile: take the
    ({"profiles": {"a": {"timeout": 300.0}, "b": {"timeout": 900.0}}}, 900.0),   # LARGEST as the real
    ({"command": ["python", "x.py"]}, 600.0),      # a spec with no timeout -> build_command's default
])
def test_the_eval_spec_budget_is_the_largest_the_task_could_apply(spec, expected):
    """A node runs under whichever profile it selects, so the largest declared timeout is the real
    ceiling an experiment has to fit — quoting the smoke profile's 60 s would tell a role to size a
    training against a budget the full eval never uses."""
    assert eval_spec_time_budget(spec) == expected


@pytest.mark.parametrize("spec", [None, {}, {"timeout": 0.0}, {"timeout": float("nan")},
                                  {"timeout": float("inf")}, {"timeout": -5.0}])
def test_an_unusable_spec_yields_no_number_at_all(spec):
    """None means "say nothing". A plausible wrong ceiling is worse than no ceiling: the whole point
    is that the role stops guessing. (`{}` is "no eval spec", i.e. a sandbox task, whose budget is
    `Settings.timeout` and is resolved one layer up.)"""
    assert eval_spec_time_budget(spec) is None


def test_an_active_eval_spec_outranks_settings_timeout(tmp_path):
    """The dispatcher never consults `self.timeout` on the eval-spec branch, so neither may the
    announcement. A cue that read it printed "~30s (~0.0h)" for a multi-hour repo training."""
    engine = _engine(tmp_path / "spec")
    engine.timeout = 30.0
    assert effective_eval_time_budget(engine) == 30.0        # no spec: the run default genuinely is it
    engine._eval_spec = {"timeout": 21600.0}
    assert effective_eval_time_budget(engine) == 21600.0


@pytest.mark.parametrize("bad", [None, "1800", True, float("nan"), 0.0, -1.0])
def test_an_unknowable_run_default_yields_no_number(tmp_path, bad):
    engine = _engine(tmp_path / f"bad-{str(bad)[:4]}")
    engine._eval_spec = {}
    engine.timeout = bad
    assert effective_eval_time_budget(engine) is None


def test_the_repo_cue_and_the_hint_read_the_SAME_derivation(tmp_path):
    """`_cue_experiment_time_budget` has announced this number on repo tasks since before the hint
    existed. It now delegates rather than re-deriving: two statements of one budget that drift are
    worse than one, and the drift would be invisible — both are prose in the same prompt."""
    engine = _engine(tmp_path / "one-rule")
    engine._eval_spec = {"timeout": 600.0, "profiles": {"full": {"timeout": 21600.0}}}
    assert engine._experiment_time_budget() == effective_eval_time_budget(engine) == 21600.0
    assert "eval_spec_time_budget" not in "".join(called_names(type(engine)._experiment_time_budget)), (
        "the cue must reach the rule through effective_eval_time_budget, not re-derive the spec half")


# --------------------------------------------------------------------------- a real engine stamps it
def test_a_real_engine_states_the_ceiling_and_the_framing(tmp_path):
    engine = _engine(tmp_path / "repo")
    engine._eval_spec = {"timeout": 21600.0}
    hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))
    assert "TIME BUDGET" in hint and "21600s (~6.0h)" in hint
    # The framing, both halves. A bare number invites the mistake in the other direction, which is
    # exactly why `_FOOTPRINT_GUIDANCE` had to be reworded for F1b.
    assert "REPORTS A NUMBER beats a longer one that reports nothing" in hint
    assert "too small to answer the question" in hint
    # The lever that produced v7's 48-hour train stage, named on the pipeline path.
    assert "A longer `timeout` on a stage is not more budget" in hint


def test_a_sub_ten_minute_budget_is_not_announced_as_zero_hours(tmp_path):
    """The shipped sandbox default is 30 s. "~0.0h" is the exact shape of the pre-fix repo cue's own
    bug, and printing it here would make the hint read as broken on every toy/offline run."""
    engine = _engine(tmp_path / "small")
    hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))
    assert "30s of wall clock" in hint and "h)" not in hint.split("wall clock")[0]


# --------------------------------------------------------------------------- the governed headroom
def test_a_granted_researcher_is_told_how_far_it_may_raise_the_budget(tmp_path):
    """`_EVAL_TIMEOUT_GUIDANCE` asks for an `eval_timeout` "e.g. 300-1800" and never says what may be
    done with it. Granted, it is hard-clamped at `max_eval_timeout` after the governance gate
    (`effective_researcher_eval_timeout`) — so asking for more than the ceiling buys nothing."""
    engine = _engine(tmp_path / "granted", max_eval_timeout=7200.0)
    assert engine._agent_may("researcher", "timeout") is True       # the shipped default
    hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))
    assert "up to 7200s" in hint and "clamped to that ceiling" in hint


def test_an_ungranted_researcher_is_told_the_field_is_dead(tmp_path):
    """The opposite instruction, and the role can derive neither. With `timeout` revoked, an
    `eval_timeout` the model sets is ignored by execution and the run default silently stands —
    while the prompt goes on asking for the number."""
    engine = _engine(tmp_path / "locked", agent_control={"timeout": []})
    assert engine._agent_may("researcher", "timeout") is False
    hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))
    # The property is "no eval_timeout HEADROOM is quoted", and the headroom has a shape:
    # `up to <N>s` plus the clamp sentence (see the granted case above). A bare `up to` is the
    # MECHANISM, not the property, and it stopped separating the two on 2026-08-23 when the
    # deadline-grace clause — which quotes a RULE, not a number, and says to plan as if it does not
    # exist — became part of the default prompt.
    assert "is NOT honoured here" in hint
    assert re.search(r"up to \d+s", hint) is None and "clamped to that ceiling" not in hint


def test_a_repo_task_is_told_nothing_about_eval_timeout_at_all(tmp_path):
    """On the eval-spec branch `effective_researcher_eval_timeout` returns None unconditionally — the
    stage manifest owns the timeout — so quoting a headroom there would name a lever that does not
    exist. `_EVAL_TIMEOUT_GUIDANCE` already says to leave the field null on repo tasks."""
    engine = _engine(tmp_path / "repo-no-lever")
    engine._eval_spec = {"timeout": 3600.0}
    hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))
    assert "eval_timeout" not in hint


# --------------------------------------------------------------------------- the grace interaction
def test_the_default_grace_is_auto_and_names_the_rule_not_a_number(tmp_path):
    """Since 2026-08-23 the feature ships ON as AUTO (`-1`), so the shipped prompt DOES carry the
    clause — and it must quote the RULE, because there is no number to quote. The ceiling is a
    fraction of whichever stage reaches its wall, and this hint is written once per proposal, before
    any stage exists. A seconds figure here would be invented."""
    engine = _engine(tmp_path / "auto-grace")
    assert engine.eval_deadline_grace_s == -1.0
    hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))
    assert "judge" in hint
    assert "10% of that stage's own time limit" in hint and "at most 30 minutes" in hint
    assert "a rescue, not budget" in hint and "plan as if it does not exist" in hint
    # AUTO must never be spelled as seconds, and least of all as its own sentinel.
    assert re.search(r"up to -?\d+s more", hint) is None


def test_an_explicit_zero_is_still_silent(tmp_path):
    """`0.0` stopped being the default and became the OFF switch. It must keep meaning exactly what
    it always did — the unconditional tree-kill, byte for byte — so a prompt on a run that turned the
    feature off gains nothing from this clause."""
    engine = _engine(tmp_path / "no-grace", eval_deadline_grace_s=0.0)
    assert engine.eval_deadline_grace_s == 0.0
    assert "judge" not in _stamp(engine, LLMResearcher(_ToolEmitClient()))


def test_a_configured_grace_is_announced_as_a_rescue_not_as_budget(tmp_path):
    """A judge-granted extension does not change the number to plan against. Announcing a ceiling a
    grace might extend must not read as "you have more than this" — so the clause says so, and the
    ceiling itself is unchanged by it."""
    engine = _engine(tmp_path / "grace", eval_deadline_grace_s=900.0)
    engine._eval_spec = {"timeout": 21600.0}
    hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))
    assert "21600s (~6.0h)" in hint                      # the ceiling is NOT 21600 + 900
    assert "900s more" in hint and "a rescue, not budget" in hint
    assert "plan as if it does not exist" in hint


# --------------------------------------------------------------------------- both prompts carry it
def test_both_researcher_prompts_carry_the_engine_stamped_budget(tmp_path, monkeypatch):
    """The whole point of the registry: the agentic path and the plain path ask the model the SAME
    question. Both prompts are built for real — no source pin — from a hint a real Engine stamped."""
    from looplab.agents import agent as agent_mod
    from looplab.agents.agent import ToolUsingResearcher

    engine = _engine(tmp_path / "both")
    engine._eval_spec = {"timeout": 21600.0}
    state = RunState(goal="g", direction="min")

    plain_client = _ToolEmitClient()
    plain = LLMResearcher(plain_client)
    stamped = _stamp(engine, plain)
    plain.propose(state, None)
    plain_turn = "".join(m["content"] for m in plain_client.messages)

    seen: dict = {}

    def _fake_run_phase(client, tools, messages, emit_spec, **kw):
        seen["messages"] = [dict(m) for m in messages]
        return Idea(operator="draft", params={}, rationale="ok")

    monkeypatch.setattr(agent_mod, "run_phase", _fake_run_phase)
    agentic = ToolUsingResearcher(client=object(), tools=None)
    _stamp(engine, agentic)
    agentic.propose(state, None)
    agentic_turn = "".join(m["content"] for m in seen["messages"])

    for turn in (plain_turn, agentic_turn):
        assert "TIME BUDGET" in turn and "21600s (~6.0h)" in turn
        # Spliced VERBATIM — the two prompts carry the same bytes, not two paraphrases.
        assert stamped in turn


def test_the_budget_survives_a_real_wrapper_chain(tmp_path):
    """The DELIVERY half. The engine stamps hints on the OUTERMOST active researcher, which in the
    shipped config is a wrapper; only `RESEARCHER_HINT_ATTRS` members are mirrored onto the delegate.
    A budget missing from that registry reaches the wrapper and dies there, and the inner prompt —
    the one that actually goes to the provider — is byte-identical to the pre-fix prompt."""
    engine = _engine(tmp_path / "wrapped")
    engine._eval_spec = {"timeout": 21600.0}

    inner_client = _ToolEmitClient()
    panel = ForesightPanelResearcher(LLMResearcher(inner_client), k=1)
    _stamp(engine, panel)                                   # engine-style: on the OUTERMOST object
    panel.propose(RunState(goal="g", direction="min"), None)

    assert "21600s (~6.0h)" in "".join(m["content"] for m in inner_client.messages)
    assert "_time_budget_hint" in RESEARCHER_HINT_ATTRS and "_time_budget_hint" in RESEARCHER_PROMPT_CUES


# --------------------------------------------------------------------------- read per proposal
def test_a_budget_extend_retune_moves_the_number_with_it(tmp_path):
    """`self.timeout` is mutable mid-run — an operator's `budget_extend{timeout}` re-applies through
    `_apply_control_overrides` every turn, and a granted Strategist retune writes the same attribute.
    The budget is read per proposal for exactly that reason: a stamp taken at construction would keep
    quoting a ceiling nobody is running under, and role instances are POOLED across builds."""
    engine = _engine(tmp_path / "retuned")
    researcher = LLMResearcher(_ToolEmitClient())
    assert "30s of wall clock" in _stamp(engine, researcher)
    engine.timeout = 7200.0                     # what a `budget_extend{timeout}` control settles to
    assert "7200s (~2.0h)" in _stamp(engine, researcher)


def test_the_stamp_is_unconditional_so_a_stale_budget_cannot_outlive_it(tmp_path):
    """Empty included. A pooled role carrying last turn's ceiling is the failure this prevents."""
    engine = _engine(tmp_path / "cleared")
    researcher = LLMResearcher(_ToolEmitClient())
    assert _stamp(engine, researcher)
    engine.timeout = None                                   # now unknowable -> say nothing
    assert _stamp(engine, researcher) == ""


# --------------------------------------------------------------------------- the Developer half
class _EvalSpecTask:
    """The minimum a repo Developer needs to answer "what is my budget?" — `_cmd_context` reads
    `task.eval_spec()` and falls back to `task.onboard_command`."""

    onboard_command = None

    def __init__(self, spec):
        self._spec = spec

    def eval_spec(self):
        return self._spec


def _repo_dev(spec):
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    dev = object.__new__(LLMRepoDeveloper)          # no client, no repo — the note reads only the task
    dev.task = _EvalSpecTask(spec)
    return dev


def test_the_developer_is_told_the_budget_it_actually_spends(tmp_path):
    """v7's `train` stage declared `timeout: 172800` — 48 h against a 6 h budget — because the stages
    prompt asks for "a GENEROUS timeout" and named no ceiling. The note is spliced AFTER that ask,
    never instead of it (prompt strings are contracts), and it states the lever honestly: nothing
    clamps a declared stage timeout, so a longer one really does run — it just spends GPU-hours the
    run was never planned around."""
    dev = _repo_dev({"command": ["python", "-m", "vectorsearch.test"], "timeout": 21600.0})
    user = dev._stages_user(Idea(operator="draft", params={}, rationale="r"),
                            dev.task.eval_spec(), True)
    assert "Give training a generous timeout." in user          # the existing ask, untouched
    assert "WALL-CLOCK BUDGET" in user and "21600s (~6.0h)" in user
    assert "A stage `timeout` longer than the budget is not more budget" in user
    assert "batch size YOU choose" in user


def test_a_developer_with_no_declared_budget_is_told_nothing(tmp_path):
    """An onboarding/toy Developer has no eval spec, so there is no number to state — and a bare dev
    with no task at all must not raise inside a prompt builder."""
    assert _repo_dev({})._time_budget_note() == ""
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    assert object.__new__(LLMRepoDeveloper)._time_budget_note() == ""


def test_the_implement_phase_carries_it_too():
    """The stages phase declares the leash; the CODE written in the implement phase is where the
    schedule and the batch size are actually chosen — and a REPAIR session skips the stages phase
    entirely and reaches only this path. AST, not a substring: a comment naming the method is not a
    call. (Behaviour is out of reach here — the implement message is built inside a session that
    needs a live client, a repo and tools.)"""
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    assert "self._time_budget_note" in called_names(LLMRepoDeveloper._run)      # implement + repair
    assert "self._time_budget_note" in called_names(LLMRepoDeveloper._stages_user)


def test_both_roles_are_quoted_the_same_number(tmp_path):
    """The finding in one assertion: the Researcher plans the epochs and the Developer writes the
    loop, so a run where the two are told different budgets is a run that cannot fit either."""
    spec = {"command": ["python", "score.py"], "timeout": 21600.0,
            "profiles": {"smoke": {"timeout": 600.0}}}
    engine = _engine(tmp_path / "one-number")
    engine._eval_spec = spec
    researcher_hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))
    developer_note = _repo_dev(spec)._time_budget_note()
    assert "21600s (~6.0h)" in researcher_hint and "21600s (~6.0h)" in developer_note
