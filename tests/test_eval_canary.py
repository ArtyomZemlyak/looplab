"""The EVAL CANARY: a node's own stage chain on the task's tiny slice before its full evaluation.

Motivated by multi-hour repo evals (MiniOneRec SFT: 30 min prep + 8 h training + 15 min scoring)
whose trivial scoring-tail defect surfaced only after the whole run. `engine/eval_canary.py` owns the
rules; everything below the unit tests drives the REAL `Engine._evaluate` over a real command eval
whose script reads `LOOPLAB_CANARY` itself and appends what it ran to a ledger OUTSIDE the run, so the
tests count canary and full executions from the side the engine cannot write.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from looplab.adapters.repo_task import CanarySpec, EvalSpec
from looplab.adapters.toytask import ToyTask
from looplab.core.config import Settings
from looplab.core.models import Idea
from looplab.engine.eval_canary import (canary_already_passed, canary_failure_result,
                                        canary_passed, canary_spec, capped_pipeline)
from looplab.engine.evaluate import _workdir_manifest_digest
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import (DIAGNOSTIC_EVENTS, EV_EVAL_CANARY_FINISHED,
                                  EV_EVAL_CANARY_STARTED)
from looplab.runtime.sandbox import RunResult, SubprocessSandbox
from looplab.search.policy import GreedyTree

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"
_CANARY = {"env": {"CANARY_USERS": "2000"}, "timeout": 60.0}


# ------------------------------------------------------------------ the declaration + pure rules
def test_the_task_declaration_is_validated_at_submit():
    assert CanarySpec().timeout == 900.0 and CanarySpec().env == {}
    spec = EvalSpec(command=["python", "run.py"], canary={"env": {"CANARY_USERS": "2000"},
                                                          "timeout": 300})
    assert spec.model_dump()["canary"] == {"env": {"CANARY_USERS": "2000"}, "timeout": 300.0}
    assert EvalSpec(command=["python", "run.py"]).model_dump()["canary"] is None
    with pytest.raises(ValidationError):                       # the marker is the ENGINE's
        CanarySpec(env={"LOOPLAB_CANARY": "0"})
    with pytest.raises(ValidationError):
        CanarySpec(timeout=0)
    with pytest.raises(ValidationError):
        CanarySpec(timeout=float("inf"))
    with pytest.raises(ValidationError):                       # a typo is refused, not dropped
        CanarySpec(tiemout=5)
    with pytest.raises(ValidationError):                       # secrets are refused, as everywhere
        CanarySpec(env={"OPENAI_API_KEY": "sk-abc"})


def test_the_snapshot_reader_is_total_and_always_stamps_the_marker():
    assert canary_spec({}) is None and canary_spec(None) is None
    assert canary_spec({"canary": None}) is None
    assert canary_spec({"canary": {"env": "x"}}) is None
    assert canary_spec({"canary": {"timeout": -1}}) is None
    assert canary_spec({"canary": {"timeout": "x"}}) is None
    assert canary_spec({"canary": {}}) == {"env": {"LOOPLAB_CANARY": "1"}, "timeout": 900.0}
    assert canary_spec({"canary": {"env": {"A": 1, "LOOPLAB_CANARY": "0"}}})["env"] == {
        "A": "1", "LOOPLAB_CANARY": "1"}


def test_the_cap_is_applied_to_a_copy_of_every_stage():
    stages = [{"name": "train", "timeout": 28800.0}, {"name": "score"}, {"name": "x", "timeout": 5}]
    t, capped = capped_pipeline(3600.0, stages, 900.0)
    assert t == 900.0
    assert [s["timeout"] for s in capped] == [900.0, 900.0, 5.0]
    assert stages[0]["timeout"] == 28800.0 and "timeout" not in stages[1], "the input is untouched"
    assert capped_pipeline(60.0, [], 900.0) == (60.0, [])


def test_the_pass_rule_is_the_full_eval_s_success_rule():
    ok = RunResult(exit_code=0, stdout="", stderr="", metric=0.1, timed_out=False)
    assert canary_passed(ok)
    assert not canary_passed(ok, expired=True)
    assert not canary_passed(RunResult(exit_code=0, stdout="", stderr="", metric=None, timed_out=False))
    assert not canary_passed(RunResult(exit_code=1, stdout="", stderr="", metric=0.1, timed_out=False))
    assert not canary_passed(RunResult(exit_code=0, stdout="", stderr="", metric=0.1, timed_out=True))


def test_a_failure_result_carries_no_measurement_and_no_stage_to_reuse():
    raw = RunResult(exit_code=0, stdout="METRIC: 0.7", stderr="Traceback\nKeyError: 'x'",
                    metric=0.7, timed_out=True, stages=[{"name": "train", "status": "ok"}],
                    failed_stage="score")
    res = canary_failure_result(raw, detail="d", log_dir="/tmp/c", env_names=["LOOPLAB_CANARY"])
    assert res.metric is None and res.exit_code != 0 and not res.timed_out
    assert not res.stages and res.failed_stage is None
    assert res.stderr.startswith("[eval canary]") and "KeyError: 'x'" in res.stderr


def test_the_rows_are_diagnostic_and_the_gate_keys_on_node_generation_and_code():
    assert {EV_EVAL_CANARY_STARTED, EV_EVAL_CANARY_FINISHED} <= DIAGNOSTIC_EVENTS

    class _E:
        def __init__(self, data):
            self.type, self.data = EV_EVAL_CANARY_FINISHED, data

    rows = [_E({"node_id": 0, "generation": 0, "code_digest": "abc", "passed": True}),
            _E({"node_id": 1, "generation": 0, "code_digest": "def", "passed": False})]
    assert canary_already_passed(rows, 0, 0, "abc")
    assert not canary_already_passed(rows, 0, 0, "other")      # new code -> a new canary
    assert not canary_already_passed(rows, 0, 1, "abc")        # new lifecycle -> a new canary
    assert not canary_already_passed(rows, 1, 0, "def")        # only a PASS is remembered
    assert not canary_already_passed(rows, 0, 0, "")


def test_the_setting_is_off_by_default():
    assert Settings().eval_canary is False


# ------------------------------------------------------------------ driven through the real engine
def _script(ledger: Path, *, canary: str, full: str) -> str:
    """A node program that appends what it ran to `ledger` (outside the run) and then does
    `canary` / `full` — each one of: a metric `METRIC: <n>`, `raise`, `nometric`, `sleep`."""
    def _body(kind: str) -> str:
        if kind == "raise":
            return "    raise KeyError('history_item_sid')\n"
        if kind == "nometric":
            return "    print('done, forgot the metric')\n"
        if kind == "sleep":
            return "    import time; time.sleep(30)\n"
        # Computed, so the number itself never appears in the node's source (the tests look for it
        # in the log and would otherwise find the code that prints it).
        return f"    print('METRIC: ' + str({round(float(kind) * 10**6)} / 10**6))\n"
    return ("import os\n"
            "c = os.environ.get('LOOPLAB_CANARY') == '1'\n"
            f"open({str(ledger)!r}, 'a').write(('canary' if c else 'full') + '\\n')\n"
            "open('users.txt', 'w').write(os.environ.get('CANARY_USERS', '-'))\n"
            "open('artifact.txt', 'w').write('canary' if c else 'full')\n"
            "if c:\n" + _body(canary) + "else:\n" + _body(full))


class _Dev:
    """The node's program lives in `run.py` (a repo task's shape: the eval command runs a FILE).
    Its repairs write `fixes` in order into that file; records every error it was handed."""

    def __init__(self, first: str, fixes=()):
        self.first, self.fixes, self.errors = first, list(fixes), []
        self.last_files: dict = {}
        self.last_deleted: list = []
        self.n = 0

    def implement(self, idea):
        return "print('unused')\n"

    def repair(self, idea, code, error):
        self.errors.append(error)
        self.n += 1
        body = self.fixes.pop(0) if self.fixes else self.first + f"# unchanged idea {self.n}\n"
        self.last_files = {"run.py": body}
        self.last_deleted = []
        return code


class _Researcher:
    def __init__(self, repairs: int = 3):
        self.repairs = repairs

    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    def triage_crash(self, node, error, attempt, **kw):
        if attempt <= self.repairs:
            return {"action": "repair", "rationale": "fix the defect the canary found"}
        return {"action": "abandon", "rationale": "stop"}


def _engine(run_dir: Path, dev, *, canary=_CANARY, repairs: int = 3, **kw) -> Engine:
    kw.setdefault("eval_canary", True)
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=_Researcher(repairs), developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True, **kw)
    eng._eval_spec = {"command": [sys.executable, "run.py"], "cwd": ".",
                      "metric": {"kind": "stdout_regex", "pattern": "METRIC: ([0-9.]+)"},
                      "timeout": 120.0, "canary": canary}
    return eng


def _seed(eng: Engine, code: str) -> None:
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": "print('unused')\n", "files": {"run.py": code}})


def _evaluate(eng: Engine) -> list:
    async def _bounded() -> bool:
        with anyio.move_on_after(300) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the eval did not terminate"
    return list(EventStore(eng.run_dir / "events.jsonl").read_all())


def _of(evs, kind):
    return [e for e in evs if e.type == kind and e.data.get("node_id") == 0]


def _terminals(evs):
    return [e for e in evs if e.type in ("node_evaluated", "node_failed")
            and e.data.get("node_id") == 0]


def test_a_passing_canary_lets_the_full_eval_run_and_its_number_is_dropped(tmp_path):
    ledger = tmp_path / "ledger.txt"
    dev = _Dev(_script(ledger, canary="0.271828", full="0.9"))
    eng = _engine(tmp_path / "run", dev)
    _seed(eng, dev.first)
    evs = _evaluate(eng)
    assert ledger.read_text().split() == ["canary", "full"]
    (term,) = _terminals(evs)
    assert term.type == "node_evaluated" and term.data["metric"] == 0.9
    (started,), (finished,) = _of(evs, EV_EVAL_CANARY_STARTED), _of(evs, EV_EVAL_CANARY_FINISHED)
    assert finished.data["passed"] is True and started.data["code_digest"] == finished.data["code_digest"]
    assert "0.271828" not in json.dumps([e.data for e in evs]), "the canary's number is written nowhere"
    assert fold(evs).nodes[0].metric == 0.9
    # The canary ran in its own scratch tree, never the node's workdir, and a passed one is removed.
    assert (eng.run_dir / "nodes" / "node_0" / "artifact.txt").read_text() == "full"
    assert (eng.run_dir / "nodes" / "node_0" / "users.txt").read_text() == "-", \
        "the canary's env never reaches the full eval"
    assert not (eng.run_dir / "canary" / "node_0").exists()
    assert dev.errors == []


def test_a_failing_canary_is_the_attempt_s_crash_and_the_full_eval_never_starts(tmp_path):
    ledger = tmp_path / "ledger.txt"
    broken = _script(ledger, canary="raise", full="raise")
    fixed = _script(ledger, canary="0.1", full="0.8")
    dev = _Dev(broken, fixes=[fixed])
    eng = _engine(tmp_path / "run", dev)
    _seed(eng, broken)
    evs = _evaluate(eng)
    # canary (fails) -> repair -> canary on the REPAIRED code (passes) -> the one full eval.
    assert ledger.read_text().split() == ["canary", "canary", "full"]
    assert len(dev.errors) == 1
    assert "[eval canary]" in dev.errors[0] and "history_item_sid" in dev.errors[0]
    finished = _of(evs, EV_EVAL_CANARY_FINISHED)
    assert [f.data["passed"] for f in finished] == [False, True]
    assert finished[0].data["code_digest"] != finished[1].data["code_digest"]
    assert finished[0].data["exit_code"] != 0
    (repaired,) = _of(evs, "node_repaired")
    assert repaired.data.get("engine_reason", repaired.data.get("reason")) == "crash"
    # No evaluator invocation was claimed for the canary-failed attempt: only the full eval's.
    claims = _of(evs, "eval_invocation_claimed")
    assert [c.data["attempt"] for c in claims] == [1]
    (term,) = _terminals(evs)
    assert term.type == "node_evaluated" and term.data["metric"] == 0.8
    # The canary's seconds are charged to the node like the eval's own.
    assert term.data["eval_seconds"] >= sum(f.data["eval_seconds"] for f in finished) - 0.01


def test_a_canary_that_keeps_failing_ends_the_node_without_one_full_eval(tmp_path):
    ledger = tmp_path / "ledger.txt"
    broken = _script(ledger, canary="raise", full="0.9")
    dev = _Dev(broken)                       # every "repair" leaves the bug in
    eng = _engine(tmp_path / "run", dev, repairs=1)
    _seed(eng, broken)
    evs = _evaluate(eng)
    assert set(ledger.read_text().split()) == {"canary"}, "the multi-hour eval must never start"
    (term,) = _terminals(evs)
    assert term.type == "node_failed"
    assert term.data.get("engine_reason", term.data.get("reason")) == "crash"
    assert "[eval canary]" in term.data.get("error", "")
    assert _of(evs, "eval_invocation_claimed") == []
    assert fold(evs).nodes[0].metric is None
    # The failed canary's logs are kept for the operator, outside the node's workdir.
    assert (eng.run_dir / "canary" / "node_0").is_dir()
    assert (eng.run_dir / "canary" / "node_0" / "users.txt").read_text() == "2000", \
        "the task's declared canary env reached the canary"
    assert not (eng.run_dir / "nodes" / "node_0" / "artifact.txt").exists()


@pytest.mark.parametrize("metric_salvage", ["audit", "select"])
def test_a_canary_metric_never_becomes_the_node_metric(tmp_path, metric_salvage):
    """The canary PASSES with a number and the full eval then produces none: the node has no
    metric — no salvage rung, no terminal, no fold field carries the canary's 0.271828."""
    ledger = tmp_path / "ledger.txt"
    code = _script(ledger, canary="0.271828", full="nometric")
    dev = _Dev(code)
    eng = _engine(tmp_path / "run", dev, repairs=0, metric_salvage=metric_salvage)
    _seed(eng, code)
    evs = _evaluate(eng)
    (term,) = _terminals(evs)
    assert term.type == "node_failed"
    st = fold(evs)
    assert st.nodes[0].metric is None
    assert "0.271828" not in json.dumps([e.data for e in evs])


def test_a_canary_that_outruns_its_cap_fails_as_a_crash(tmp_path):
    ledger = tmp_path / "ledger.txt"
    code = _script(ledger, canary="sleep", full="0.9")
    dev = _Dev(code)
    eng = _engine(tmp_path / "run", dev, repairs=0,
                  canary={"env": {"LOOPLAB_CANARY": "1"}, "timeout": 1.5})
    _seed(eng, code)
    evs = _evaluate(eng)
    (finished,) = _of(evs, EV_EVAL_CANARY_FINISHED)
    assert finished.data["passed"] is False and finished.data["timed_out"] is True
    assert "did not finish within" in finished.data["error"]
    (term,) = _terminals(evs)
    assert term.type == "node_failed"
    assert term.data.get("engine_reason", term.data.get("reason")) == "crash"
    assert ledger.read_text().split() == ["canary"]


class _Kill(BaseException):
    """A process death: a BaseException, so `_evaluate`'s containment cannot absorb it."""


def test_a_resume_does_not_rerun_a_canary_that_already_passed_for_the_same_code(tmp_path):
    ledger = tmp_path / "ledger.txt"
    code = _script(ledger, canary="0.1", full="0.9")
    run_dir = tmp_path / "run"
    dev = _Dev(code)
    first = _engine(run_dir, dev)
    _seed(first, code)
    real = first._run_eval

    def _die_on_the_full_eval(*a, canary=None, **kw):
        if canary is None:
            raise _Kill()
        return real(*a, canary=canary, **kw)

    first._run_eval = _die_on_the_full_eval
    with pytest.raises(BaseException) as info:
        anyio.run(lambda: first._evaluate(0, anyio.CapacityLimiter(1), None))
    assert any(isinstance(x, _Kill) for x in _leaves(info.value))
    assert ledger.read_text().split() == ["canary"]

    second = _engine(run_dir, _Dev(code))            # a fresh process over the same log
    evs = _evaluate(second)
    assert ledger.read_text().split() == ["canary", "full"], "the paid canary is not re-run"
    assert [f.data["passed"] for f in _of(evs, EV_EVAL_CANARY_FINISHED)] == [True]
    (term,) = _terminals(evs)
    assert term.type == "node_evaluated" and term.data["metric"] == 0.9


def _leaves(exc):
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _leaves(sub)
    else:
        yield exc


def test_a_passed_row_for_other_code_does_not_waive_the_canary(tmp_path):
    ledger = tmp_path / "ledger.txt"
    code = _script(ledger, canary="0.1", full="0.9")
    dev = _Dev(code)
    eng = _engine(tmp_path / "run", dev)
    _seed(eng, code)
    node = fold(eng.store.read_all()).nodes[0]
    eng.store.append(EV_EVAL_CANARY_FINISHED, {
        "node_id": 0, "generation": 0, "attempt": 0, "code_digest": "not-this-code",
        "passed": True, "eval_seconds": 1.0})
    assert _workdir_manifest_digest(node) != "not-this-code"
    _evaluate(eng)
    assert ledger.read_text().split() == ["canary", "full"]


@pytest.mark.parametrize("enabled, declared", [(False, True), (True, False)])
def test_off_or_undeclared_is_the_eval_exactly_as_before(tmp_path, enabled, declared):
    ledger = tmp_path / "ledger.txt"
    code = _script(ledger, canary="raise", full="0.9")
    dev = _Dev(code)
    eng = _engine(tmp_path / "run", dev, canary=(_CANARY if declared else None),
                  eval_canary=enabled)
    _seed(eng, code)
    evs = _evaluate(eng)
    assert ledger.read_text().split() == ["full"]
    assert not [e for e in evs if e.type in (EV_EVAL_CANARY_STARTED, EV_EVAL_CANARY_FINISHED)]
    (term,) = _terminals(evs)
    assert term.type == "node_evaluated" and term.data["metric"] == 0.9
    assert not (eng.run_dir / "canary").exists()


def test_the_bare_engine_ships_it_off():
    from looplab.engine.options import EngineOptions
    assert EngineOptions().eval_canary is False


def test_the_canary_runs_on_the_attempt_s_own_env_and_lease(tmp_path):
    """Same `env` object (the lifecycle's GPU pin, when it holds one) for the canary and the full
    eval; only the canary carries the canary declaration, in a directory that is not the node's."""
    ledger = tmp_path / "ledger.txt"
    code = _script(ledger, canary="0.1", full="0.9")
    eng = _engine(tmp_path / "run", _Dev(code))
    _seed(eng, code)
    pinned = {"LOOPLAB_TEST_PIN": "3"}             # stands in for a lease's CUDA_VISIBLE_DEVICES
    calls, real_run, real_admit = [], eng._run_eval, eng._eval_admit

    def _spy(node, workdir, env=None, profile=None, cancel=None, start_stage=None, canary=None):
        calls.append((Path(workdir), env, canary))
        return real_run(node, workdir, env, profile, cancel, start_stage, canary=canary)

    async def _admit(a):
        signal = await real_admit(a)
        a.eval_env = pinned                        # what a lease-holding lifecycle carries
        return signal

    eng._run_eval, eng._eval_admit = _spy, _admit
    _evaluate(eng)
    (cdir, cenv, cspec), (fdir, fenv, fspec) = calls
    assert cdir == eng.run_dir / "canary" / "node_0" and fdir == eng.run_dir / "nodes" / "node_0"
    assert cenv is pinned and fenv is pinned
    assert cspec is not None and cspec["env"]["LOOPLAB_CANARY"] == "1" and fspec is None
