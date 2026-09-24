"""`budget_extend{eval_timeout}`: an operator raises (or lowers) the per-eval wall clock of a LIVE
eval-spec run, durably (2026-09-24).

The incident: `minionerec-backbones` (a repo task — one `eval.command` plus a Developer-declared
`train` stage) needed a 12 h ceiling instead of 4 h. `budget_extend{timeout}` existed, but it moves
`Settings.timeout`, which an ACTIVE eval spec never consults (`engine/shared.py::
effective_eval_time_budget`), and the spec's own timeout is the task's recorded one — so the only way
to raise it was to restart the run and lose hours.

What is DRIVEN here, not pinned:
  * the rule (`command_eval.leashed_timeout`) as a truth table, and the spec/chain rewrites over it;
  * the fold: absolute, last-write-wins, total over junk;
  * the server: a bounded, finite, positive number or a 400 — through `normalize_control` and
    through `POST /api/runs/<id>/commands`;
  * the ENGINE: a run whose eval prints its own `LOOPLAB_EVAL_TIMEOUT_S` as its metric is launched at
    X, the operator's event lands, and the NEXT evaluation — on a fresh engine process, i.e. a resume
    — runs at Y; the same for a Developer-declared stage sized at the old budget; replay keeps Y;
  * the Developer: the budget it is told and held to follows the state it is bound to.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.engine.orchestrator import Engine
from looplab.engine.shared import (effective_eval_spec, effective_eval_time_budget,
                                   effective_max_eval_timeout,
                                   effective_researcher_eval_timeout)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime import command_eval
from looplab.runtime.command_eval import (eval_spec_time_budget, eval_timeout_override,
                                          leashed_stages, leashed_timeout, with_eval_timeout)
from looplab.runtime.sandbox import MAX_TIMEOUT_S, SubprocessSandbox
from looplab.search.policy import GreedyTree

_M = {"kind": "stdout_json", "key": "metric"}


# ------------------------------------------------------------------------------------ the rule

@pytest.mark.parametrize("declared, budget, override, expected", [
    (600.0, 14400.0, None, 600.0),          # no override: untouched
    (14400.0, 14400.0, 43200.0, 43200.0),   # AT the old ceiling: moves up with it
    (20000.0, 14400.0, 43200.0, 43200.0),   # ABOVE it (a carried pre-gate manifest): moves too
    (600.0, 14400.0, 43200.0, 600.0),       # a shorter leash is its author's estimate: kept
    (14400.0, 14400.0, 3600.0, 3600.0),     # lowered: the leash at the ceiling moves down
    (7200.0, 14400.0, 3600.0, 3600.0),      # lowered below a shorter leash: cut
    (600.0, 14400.0, 3600.0, 600.0),        # lowered, still above it: kept
])
def test_the_leash_rule_truth_table(declared, budget, override, expected):
    assert leashed_timeout(declared, budget, override) == expected
    # idempotent — the chain-level leash re-applies over the spec-level one without drift
    once = leashed_timeout(declared, budget, override)
    assert leashed_timeout(once, budget, override) == once


def test_a_non_number_declaration_is_left_to_the_launch_backstop():
    for junk in ("x", None, True, float("nan")):
        assert leashed_timeout(junk, 100.0, 50.0) is junk or (
            isinstance(junk, float) and math.isnan(leashed_timeout(junk, 100.0, 50.0)))


def test_the_spec_rewrite_makes_the_override_the_budget_and_leaves_the_input_alone():
    es = {"command": ["x"], "timeout": 14400.0,
          "profiles": {"smoke": {"timeout": 60}, "full": {"timeout": 14400}},
          "stages": [{"name": "prep", "command": ["a"], "timeout": 600},
                     {"name": "train", "command": ["b"], "timeout": 14400},
                     {"name": "score", "command": ["c"]}]}
    before = json.dumps(es, sort_keys=True)
    out = with_eval_timeout(es, 43200.0)
    assert json.dumps(es, sort_keys=True) == before, "the recorded spec must never be mutated"
    assert eval_spec_time_budget(out) == 43200.0
    assert command_eval.build_command(out, {}, "full")[1] == 43200.0
    assert command_eval.build_command(out, {}, None)[1] == 60.0      # smoke keeps its own leash
    assert [s.get("timeout") for s in out["stages"]] == [600, 43200.0, None]
    # no override: the SAME object, so the no-override path is byte-identical
    assert with_eval_timeout(es, None) is es
    # a spec declaring no timeout at all: build_command's 600 s default WAS the budget
    bare = {"command": ["x"]}
    assert eval_spec_time_budget(with_eval_timeout(bare, 5000.0)) == 5000.0
    assert command_eval.build_command(with_eval_timeout(bare, 5000.0))[1] == 5000.0
    # no spec: nothing to override
    assert with_eval_timeout({}, 5000.0) == {}


def test_the_chain_leash_copies_and_skips_undeclared_stages():
    chain = [{"name": "t", "timeout": 14400}, {"name": "u"}, "junk"]
    out = leashed_stages(chain, 14400.0, 43200.0)
    assert out == [{"name": "t", "timeout": 43200.0}, {"name": "u"}, "junk"]
    assert chain[0]["timeout"] == 14400, "entries are copied, never mutated"
    assert leashed_stages(chain, 14400.0, None) is chain


@pytest.mark.parametrize("raw, expected", [
    (43200, 43200.0), ("600", 600.0), (MAX_TIMEOUT_S * 10, MAX_TIMEOUT_S),
    (0, None), (-5, None), (float("nan"), None), (float("inf"), None), (True, None),
    ("abc", None), (None, None),
])
def test_the_override_reader_is_total(raw, expected):
    assert eval_timeout_override({"eval_timeout": raw}) == expected
    assert eval_timeout_override(None) is None and eval_timeout_override("x") is None


# ------------------------------------------------------------------------------------- the fold

def test_the_fold_keeps_the_last_good_value_and_skips_junk(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("budget_extend", {"eval_timeout": 14400.0})
    s.append("budget_extend", {"eval_timeout": "43200"})       # a string from a form: coerced
    for junk in (float("nan"), float("inf"), -1, 0, "abc", True):
        s.append("budget_extend", {"eval_timeout": junk})      # a poison value never lands
    st = fold(s.read_all())
    assert st.budget_overrides["eval_timeout"] == 43200.0
    # deterministic: a second fold of the same log is the same value
    assert fold(s.read_all()).budget_overrides == st.budget_overrides


# ------------------------------------------------------------------------------------ the server

def _normalize(tmp_path, payload):
    pytest.importorskip("fastapi")
    from tests.test_control_registry import _seed, _Srv
    from looplab.serve.control_validation import normalize_control
    rd = _seed(tmp_path)
    return normalize_control(_Srv(tmp_path), rd, "budget_extend", payload)


def test_the_server_accepts_a_bounded_positive_number(tmp_path):
    assert _normalize(tmp_path / "a", {"eval_timeout": 43200})["eval_timeout"] == 43200.0
    assert _normalize(tmp_path / "b", {"eval_timeout": "600"})["eval_timeout"] == 600.0
    assert _normalize(tmp_path / "c", {"eval_timeout": MAX_TIMEOUT_S})["eval_timeout"] == MAX_TIMEOUT_S


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), MAX_TIMEOUT_S + 1, True,
                                 "abc", [1], {"s": 1}])
def test_the_server_refuses_a_value_that_is_not_a_budget(tmp_path, bad):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as refused:
        _normalize(tmp_path, {"eval_timeout": bad})
    assert refused.value.status_code == 400
    assert "eval_timeout" in str(refused.value.detail)


def test_the_commands_route_refuses_and_accepts_through_http(tmp_path):
    """The HTTP contract, end to end: a bad value is a rejected `invalid_command` record that appends
    nothing; a good one lands in the log as the normalized float the fold reads."""
    pytest.importorskip("fastapi")
    from tests.test_run_command_service import _Driver, _client, _seed
    from tests.factories import command_terminal, post_command

    rd = _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver())
    refused = post_command(client, "budget_extend", {"eval_timeout": -3}, key="bad").json()
    # The durable command protocol answers a refused payload as a REJECTED record (the
    # normalizer's 400, carried as `invalid_command`), never as an append.
    assert refused["status"] == "rejected"
    assert refused["error"]["code"] == "invalid_command"
    assert "eval_timeout" in refused["error"]["message"]
    assert not [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "budget_extend"]

    accepted = post_command(client, "budget_extend", {"eval_timeout": "43200"}, key="good")
    assert accepted.status_code in (200, 201, 202), accepted.text
    command_terminal(client, accepted.json())
    rows = [e.data for e in EventStore(rd / "events.jsonl").read_all() if e.type == "budget_extend"]
    assert [row["eval_timeout"] for row in rows] == [43200.0]
    assert fold(EventStore(rd / "events.jsonl").read_all()).budget_overrides["eval_timeout"] == 43200.0


# ------------------------------------------------------------------------------------ the engine

# The eval prints its OWN leash as its metric, so the number the process was really given is the
# number the node records — the dispatch path, `_run_stages`/`run_argv` and `eval_deadline_env`
# all between the event and the assertion.
_PRINT_OWN_TIMEOUT = ("import json, os\n"
                      "print(json.dumps({'metric': float(os.environ['LOOPLAB_EVAL_TIMEOUT_S'])}))\n")


def _task(repo: Path, **eval_kw) -> RepoTask:
    return RepoTask(id="lt", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                    eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M, **eval_kw))


def _engine(rd: Path, task: RepoTask, max_nodes: int) -> Engine:
    r, d = task.build_roles()
    return Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=1, max_nodes=max_nodes), max_nodes=max_nodes)


def _operator_extends(tmp_path, rd: Path, eval_timeout) -> None:
    """What `POST /commands` does for the operator: normalize, then append — plus the reopen that
    lets the finished test run take one more node."""
    data = _normalize(tmp_path / "norm", {"eval_timeout": eval_timeout})
    store = EventStore(rd / "events.jsonl")
    store.append("budget_extend", data)
    store.append("budget_extend", {"add_nodes": 1})
    store.append("run_reopened", {})


def _metrics(state) -> list:
    return [state.nodes[i].metric for i in sorted(state.nodes)]


def test_the_next_evaluation_runs_under_the_operators_new_budget(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run.py").write_text(_PRINT_OWN_TIMEOUT, encoding="utf-8")
    task = _task(repo, timeout=30.0)
    rd = tmp_path / "run"

    st0 = anyio.run(_engine(rd, task, 1).run)
    assert _metrics(st0) == [30.0], "launched at X"

    _operator_extends(tmp_path, rd, 77)
    st1 = anyio.run(_engine(rd, task, 1).run)          # a fresh engine process: a resume
    assert _metrics(st1) == [30.0, 77.0], "the evaluation after the event runs at Y"

    # replay reproduces it, and a SECOND resume still sees it (the value is the fold's, not memory)
    assert fold(EventStore(rd / "events.jsonl").read_all()).budget_overrides["eval_timeout"] == 77.0
    store = EventStore(rd / "events.jsonl")
    store.append("budget_extend", {"add_nodes": 1})
    store.append("run_reopened", {})
    st2 = anyio.run(_engine(rd, task, 1).run)
    assert _metrics(st2) == [30.0, 77.0, 77.0]


def test_a_developer_stage_sized_at_the_old_budget_moves_with_it(tmp_path):
    """The `minionerec` shape: the operator's `command` is the protected `score` stage and the
    stage that runs out of time is a Developer-declared `train`, declared AT the budget it was told.
    Its leash moves with the budget; a shorter `prep` leash does not."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "stage.py").write_text(
        "import os, sys\n"
        "open(sys.argv[1] + '.leash', 'w').write(os.environ['LOOPLAB_EVAL_TIMEOUT_S'])\n",
        encoding="utf-8")
    (repo / "run.py").write_text(
        "import json\n"
        "t = float(open('train.leash').read()); p = float(open('prep.leash').read())\n"
        "print(json.dumps({'metric': t * 1000 + p}))\n", encoding="utf-8")
    (repo / "looplab_stages.json").write_text(json.dumps({"stages": [
        {"name": "prep", "command": [sys.executable, "stage.py", "prep"], "timeout": 5},
        {"name": "train", "command": [sys.executable, "stage.py", "train"], "timeout": 30},
    ]}), encoding="utf-8")
    task = _task(repo, timeout=30.0)
    rd = tmp_path / "run"

    st0 = anyio.run(_engine(rd, task, 1).run)
    assert _metrics(st0) == [30.0 * 1000 + 5.0]

    _operator_extends(tmp_path, rd, 90)
    st1 = anyio.run(_engine(rd, task, 1).run)
    assert _metrics(st1)[-1] == 90.0 * 1000 + 5.0, "train lifted to Y, prep keeps its own leash"


def test_every_engine_reader_sees_the_override_and_the_recorded_spec_is_untouched():
    es = {"command": ["x"], "timeout": 14400.0, "metric": _M}
    eng = SimpleNamespace(_eval_spec=es, _eval_timeout_override=None, max_eval_timeout=3600.0,
                          timeout=30.0, _agent_may=lambda role, setting: True)
    assert effective_eval_time_budget(eng) == 14400.0
    assert effective_eval_spec(eng) is es
    eng._eval_timeout_override = 43200.0
    assert effective_eval_time_budget(eng) == 43200.0          # the Researcher's TIME BUDGET cue
    assert effective_eval_spec(eng)["timeout"] == 43200.0
    assert es["timeout"] == 14400.0
    # the agent clamp is LIFTED to a larger override, never lowered by a smaller one
    assert effective_max_eval_timeout(eng) == 43200.0
    eng._eval_timeout_override = 60.0
    assert effective_max_eval_timeout(eng) == 3600.0
    # a script task (no spec): the override lifts the clamp a governed request meets
    script = SimpleNamespace(_eval_spec={}, _eval_timeout_override=7200.0, max_eval_timeout=3600.0,
                             timeout=30.0, _agent_may=lambda role, setting: True)
    assert effective_eval_time_budget(script) == 30.0           # `timeout` still owns that branch
    assert effective_researcher_eval_timeout(script, SimpleNamespace(eval_timeout=7000.0)) == 7000.0


def test_the_override_is_applied_on_every_turn_from_the_fold():
    from looplab.core.models import RunState
    from looplab.engine.width_settling import WidthSettlingMixin

    eng = SimpleNamespace(_speculation_gate_calibration=False, max_seconds=None,
                          max_eval_seconds=None, timeout=30.0, _eval_timeout_override=None)
    st = RunState()
    st.budget_overrides = {"eval_timeout": 43200.0}
    WidthSettlingMixin._apply_control_overrides(eng, st)
    assert eng._eval_timeout_override == 43200.0
    assert eng.timeout == 30.0, "`eval_timeout` is not `timeout`: the script-path default is untouched"


# --------------------------------------------------------------------------------- the Developer

def test_the_developer_is_told_and_held_to_the_bound_states_budget(tmp_path):
    from looplab.adapters.repo_developer import LLMRepoDeveloper

    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    dev.task = SimpleNamespace(eval_spec=lambda: {"command": ["x"], "timeout": 14400.0})
    dev._memory_state = None
    assert dev._eval_time_budget() == 14400.0
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("budget_extend", {"eval_timeout": 43200.0})
    dev.bind_state(fold(s.read_all()))
    assert dev._eval_time_budget() == 43200.0
    assert "43200s" in dev._time_budget_note()


# ------------------------------------------------------------------ the reuse predicate keeps working

def test_a_raised_budget_is_not_read_as_a_manifest_edit_by_the_reuse_predicate():
    """`_eval_pipeline` hands the planners the LEASHED chain while the previous manifest is raw text.
    Without leashing the previous side too, every raised budget would read as an edited prefix and a
    repaired node would re-train from scratch instead of reusing its completed stages."""
    from looplab.engine.eval_stages import EvalStagesMixin, manifest_prefix_unchanged

    manifest = json.dumps({"stages": [
        {"name": "prep", "command": ["python", "prep.py"], "timeout": 14400},
        {"name": "train", "command": ["python", "train.py"], "timeout": 14400}]})
    eng = SimpleNamespace(_eval_spec={"command": ["python", "score.py"], "timeout": 14400.0},
                          _eval_timeout_override=43200.0)
    leash = lambda stages: EvalStagesMixin._leash_stages(eng, stages)  # noqa: E731
    chain = leash(command_eval.materialized_stages(json.loads(manifest)))
    assert chain[0]["timeout"] == 43200.0                    # the chain really is leashed
    assert manifest_prefix_unchanged(manifest, chain, "train", leash=leash) is True
    assert manifest_prefix_unchanged(manifest, chain, "train") is False   # what the leash prevents
    # and a real prefix edit is still an edit under the leash
    edited = json.dumps({"stages": [
        {"name": "prep", "command": ["python", "prep.py", "--x"], "timeout": 14400},
        {"name": "train", "command": ["python", "train.py"], "timeout": 14400}]})
    assert manifest_prefix_unchanged(edited, chain, "train", leash=leash) is False
