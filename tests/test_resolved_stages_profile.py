"""ONE derivation of "which stages will run", asked by the side that RUNS them and the sides that
PLAN against them.

THE DEFECT (BACKLOG survivor #9, closed 2026-08-14): `_resolved_stages` re-implemented
`eval_dispatch._run_eval`'s profile -> `build_command` -> `_resolve_stages` prologue as a parallel
copy, and the copy had already lost the dispatcher's `profile` argument — so a planner asked during
a `confirm`/full pass would answer with the SMOKE chain.

Latent, and the measurement is part of the record rather than a reason not to fix it. The only
production caller that passes an explicit profile is `confirm_phase.py` (`"full"`), and it does not
plan; `evaluate.py`'s three planning call sites all sit on the path that dispatches `profile=None`.
Nothing in the 66-run `runs/` corpus could have hit it — not one run contains a `confirm_eval` row.
And the two planners that read the chain most closely are structurally immune anyway: the watchdogs'
`eval_log_plan` reads stage NAMES, which no profile can move (pinned below, because that is the
"watchdogs judged the wrong log" defect 662a9be6 fixed once and this must not re-open by another
route). What the profile CAN move is the appended protected `score` stage's overrides and its
timeout — which is exactly what these tests observe.

Everything here is offline: a real Engine, a real staged subprocess eval, no provider.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

from looplab.adapters.repo_task import EvalSpec, NoOpRepoDeveloper, RepoParamResearcher, RepoTask
from looplab.core.models import Idea, Node
from looplab.engine import eval_dispatch
from looplab.engine.orchestrator import Engine
from looplab.engine.train_monitor import eval_log_plan
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}

# Two profiles that differ in BOTH things a profile can change: the overrides appended to the
# operator's scoring command, and the wall-clock timeout the protected `score` stage runs under.
_SMOKE = {"overrides": ["mode=smoke"], "timeout": 11}
_FULL = {"overrides": ["mode=full"], "timeout": 33}


def _task() -> RepoTask:
    return RepoTask(
        id="stage-profile", goal="agree about the chain", direction="max",
        editable_path=str(FIXTURE),
        eval=EvalSpec(command=[sys.executable, "score.py"], metric=_M, timeout=90,
                      profiles={"smoke": _SMOKE, "full": _FULL}),
    )


def _engine(tmp_path) -> Engine:
    task = _task()
    return Engine(tmp_path / "run", task=task,
                  researcher=RepoParamResearcher({}, seed=0), developer=NoOpRepoDeveloper(),
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                  auto_install_deps=False)


def _workdir(tmp_path):
    """A node workdir the DEVELOPER-MANIFEST branch resolves: a `looplab_stages.json` supplying the
    preceding `train` stage, with the operator's `cmd` appended as the protected `score` stage. That
    branch is the only one a profile can reach — when the OPERATOR declares `eval.stages` they are
    canonical and `score_cmd`/`score_timeout` are never consulted."""
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "train.py").write_text("print('trained')\n", encoding="utf-8")
    (wd / "score.py").write_text('import json; print(json.dumps({"metric": 0.5}))\n',
                                 encoding="utf-8")
    (wd / "looplab_stages.json").write_text(json.dumps({"stages": [
        {"name": "train", "command": [sys.executable, "train.py"], "timeout": 30}]}),
        encoding="utf-8")
    return wd


def _node() -> Node:
    return Node(id=0, operator="improve", idea=Idea(operator="improve", params={}, rationale="r"),
                code="")


def _dispatch(engine, node, wd, monkeypatch, profile):
    """Run a REAL eval through the dispatcher and hand back the chain it actually ran. The chain is
    read where the dispatcher hands it to the runtime — the only place the derivation's output is
    observable BEFORE the run consumes it (`RunResult.stages` is the post-run outcome record, which
    is why it cannot serve the planners and could not close this defect)."""
    from looplab.runtime import command_eval
    real, seen = command_eval.run_command_eval, {}

    def _capture(*a, **kw):
        seen["stages"] = kw.get("stages")
        return real(*a, **kw)

    monkeypatch.setattr(command_eval, "run_command_eval", _capture)
    res = engine._run_eval(node, str(wd), None, profile)
    monkeypatch.undo()
    return seen["stages"], res


# --------------------------------------------------------------- the property, driven end to end
def test_a_full_profile_eval_and_its_planner_agree_about_the_chain(tmp_path, monkeypatch):
    """THE DEFECT, DRIVEN. `confirm_phase.py` dispatches `_run_eval(..., "full")`; a planner asking
    what that eval runs must get the FULL chain — the same commands, the same timeouts, in the same
    order — and not the smoke one.

    The third assertion is what makes the first two non-vacuous: the smoke chain is genuinely a
    DIFFERENT object here, so a planner that ignored the profile (the shipped behaviour until this
    change, which could not even accept the argument) fails this test rather than passing it by
    coincidence."""
    engine, wd, node = _engine(tmp_path), _workdir(tmp_path), _node()
    ran, res = _dispatch(engine, node, wd, monkeypatch, "full")
    assert res.metric == 0.5                                   # a real pipeline really ran
    assert [s["name"] for s in ran] == ["train", "score"]
    assert ran[-1]["command"][-1] == "mode=full" and ran[-1]["timeout"] == 33

    assert engine._resolved_stages(node, str(wd), "full") == ran
    assert engine._resolved_stages(node, str(wd)) != ran       # the smoke chain is a different one
    assert engine._resolved_stages(node, str(wd))[-1]["timeout"] == 11


def test_the_default_path_agrees_too_which_is_what_evaluate_relies_on(tmp_path, monkeypatch):
    """`evaluate.py::_evaluate` dispatches `profile=None` and its three planning call sites — the
    log plan, the inline-repair reuse predicate, the salvage re-check — ask with the default. That
    agreement held before this change by coincidence (two copies that happened to match on that one
    argument); it now holds because there is one derivation, and this pins it either way."""
    engine, wd, node = _engine(tmp_path), _workdir(tmp_path), _node()
    ran, res = _dispatch(engine, node, wd, monkeypatch, None)
    assert res.metric == 0.5
    assert engine._resolved_stages(node, str(wd)) == ran
    assert ran[-1]["command"][-1] == "mode=smoke"              # None means the search default


def test_the_strategist_fidelity_override_is_inside_the_shared_derivation(tmp_path, monkeypatch):
    """The A7 override ("the active strategy pins smoke/full and the node asked for nothing") was
    the OTHER rule both copies had to carry. It lives in the shared function now, so a planner
    cannot be the copy that forgets it."""
    engine, wd, node = _engine(tmp_path), _workdir(tmp_path), _node()
    engine._strategy_fidelity = "full"
    ran, _ = _dispatch(engine, node, wd, monkeypatch, None)
    assert ran[-1]["command"][-1] == "mode=full" and ran[-1]["timeout"] == 33
    assert engine._resolved_stages(node, str(wd)) == ran


def test_an_explicit_profile_still_wins_over_the_node_and_the_strategy(tmp_path, monkeypatch):
    """Precedence, unchanged and now stated once: explicit arg > the Idea's own profile > the
    strategy's fidelity. Confirm's `"full"` must survive a node that asked for smoke."""
    engine, wd = _engine(tmp_path), _workdir(tmp_path)
    node = Node(id=0, operator="improve", code="",
                idea=Idea(operator="improve", params={}, rationale="r", eval_profile="smoke"))
    engine._strategy_fidelity = "smoke"
    ran, _ = _dispatch(engine, node, wd, monkeypatch, "full")
    assert ran[-1]["timeout"] == 33
    assert engine._resolved_stages(node, str(wd), "full") == ran
    assert engine._resolved_stages(node, str(wd))[-1]["timeout"] == 11      # the node's own smoke


# ------------------------------------------------------- the blast radius, pinned where it is nil
def test_the_watchdogs_log_plan_cannot_move_with_the_profile(tmp_path):
    """WHY THIS WAS NOT 662a9be6 COMING BACK. The watchdogs' plan is derived from the resolved
    pipeline's stage NAMES (that commit's whole fix — attribution by plan, not by mtime), and a
    profile changes only the appended `score` stage's overrides and timeout. So even had the
    log-watch planner been reachable at a non-default profile, it would have watched the same files
    with the same roles. The kill authority a wrong plan could have moved never moved."""
    engine, wd, node = _engine(tmp_path), _workdir(tmp_path), _node()
    smoke, full = engine._resolved_stages(node, str(wd)), engine._resolved_stages(node, str(wd), "full")
    assert smoke != full
    assert eval_log_plan(smoke) == eval_log_plan(full)


def test_the_planner_stays_total_when_the_derivation_raises(tmp_path):
    """`_resolved_stages` is called from the inline-repair loop and the salvage path, and neither may
    ever be the thing that turns a failed node into a crashed RUN. A malformed eval spec (an old or
    hand-edited snapshot — the shape `_resolve_stages` documents at its own fallback) degrades to
    "no chain, no reuse" and never propagates."""
    engine, wd, node = _engine(tmp_path), _workdir(tmp_path), _node()
    engine._eval_spec = {"metric": _M}                     # no `command` -> build_command raises
    assert engine._resolved_stages(node, str(wd)) == []
    assert engine._resolved_stages(node, str(wd), "full") == []
    with pytest.raises(KeyError):                          # the OWNER still raises; only the planner absorbs
        engine._eval_pipeline(node, str(wd))


def test_the_dispatcher_no_longer_carries_its_own_copy_of_the_derivation():
    """A negative pin, in the sense CLAUDE.md keeps them for: what must not come back is the TEXT of
    a second derivation. `_run_eval` may not resolve a profile or build a command itself again —
    that is precisely how the two came apart the first time."""
    src = inspect.getsource(eval_dispatch.EvalDispatchMixin._run_eval)
    assert "self._eval_pipeline(" in src
    assert "build_command(" not in src              # the CALL; the comment naming the shape stays
    assert "_strategy_fidelity" not in src
