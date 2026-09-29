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
from looplab.core.models import FAILURE_REASONS, NON_REPAIRABLE_REASONS, REPAIRABLE_REASONS
from looplab.engine.eval_canary import (CANARY_NEAR_CAP_FRACTION, CANARY_RETRY_CAP_FACTOR,
                                        canary_already_passed, canary_failure_result,
                                        canary_near_cap, canary_passed, canary_spec,
                                        capped_pipeline)
from looplab.engine.failure_diagnosis import ENGINE_FINAL_REASONS
from looplab.engine.metric_salvage import NEVER_SALVAGED_REASONS
from looplab.engine.triage import _failure_reason
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
def _script(ledger: Path, *, canary: str, full: str, warm: Path | None = None) -> str:
    """A node program that appends what it ran to `ledger` (outside the run) and then does
    `canary` / `full` — each one of: a metric `METRIC: <n>`, `raise`, `nometric`, `sleep`, and the
    two COLD-CACHE kinds over the marker file `warm` (outside the run too): the first run creates it
    and sleeps; a later one prints `METRIC: 0.1` (`cold`) or raises (`cold_raise`)."""
    def _body(kind: str) -> str:
        if kind in ("cold", "cold_raise"):
            after = ("    raise KeyError('history_item_sid')\n" if kind == "cold_raise"
                     else "    print('METRIC: ' + str(1 / 10))\n")
            return (f"    if not os.path.exists({str(warm)!r}):\n"
                    f"        open({str(warm)!r}, 'w').write('1')\n"
                    "        import time; time.sleep(30)\n" + after)
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
        self.triaged: list = []                  # every error a triage judge was asked about

    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    def triage_crash(self, node, error, attempt, **kw):
        self.triaged.append(error)
        if attempt <= self.repairs:
            return {"action": "repair", "rationale": "fix the defect the canary found"}
        return {"action": "abandon", "rationale": "stop"}


def _engine(run_dir: Path, dev, *, canary=_CANARY, repairs: int = 3, researcher=None,
            **kw) -> Engine:
    kw.setdefault("eval_canary", True)
    researcher = researcher if researcher is not None else _Researcher(repairs)
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=researcher, developer=dev,
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


# ------------------------------------------------------------------ the clock (doc 69 69.10)
def test_the_canary_timeout_reason_is_registered_as_the_engine_s_and_never_repaired():
    """`canary_timeout` is the ENGINE's clock (final, never diagnosed), never salvaged, and out of
    the default repair gate; `_failure_reason` names it off the flag only a canary result carries."""
    assert "canary_timeout" in FAILURE_REASONS and "canary_timeout" in NON_REPAIRABLE_REASONS
    assert "canary_timeout" not in REPAIRABLE_REASONS
    assert "canary_timeout" not in Settings().inline_repair_reasons
    assert "canary_timeout" in ENGINE_FINAL_REASONS and "canary_timeout" in NEVER_SALVAGED_REASONS
    expired = canary_failure_result(RunResult(exit_code=-9, stdout="", stderr="", metric=None,
                                              timed_out=True),
                                    detail="d", log_dir="L", env_names=["A"], expired=True)
    assert expired.timed_out is False and _failure_reason(expired) == "canary_timeout"
    crashed = canary_failure_result(RunResult(exit_code=1, stdout="", stderr="", metric=None,
                                              timed_out=False),
                                    detail="d", log_dir="L", env_names=["A"])
    assert _failure_reason(crashed) == "crash"
    assert "TIMED OUT" in expired.stderr and "Fix the defect below" not in expired.stderr
    assert "FAILED" in crashed.stderr and "Fix the defect below" in crashed.stderr


def test_the_near_cap_rule_is_a_share_of_the_cap():
    assert CANARY_NEAR_CAP_FRACTION == 0.75 and CANARY_RETRY_CAP_FACTOR == 2.0
    assert canary_near_cap(75.0, 100.0) and canary_near_cap(1102, 1200)
    assert not canary_near_cap(74.99, 100.0) and not canary_near_cap(0, 100)
    for seconds, cap in ((1, 0), (1, -5), (-1, 10), (float("nan"), 10), (5, float("inf")),
                         (None, 10), ("x", 10), (5, None)):
        assert not canary_near_cap(seconds, cap), (seconds, cap)


def test_a_canary_the_clock_stops_twice_ends_the_node_with_no_model_asked(tmp_path):
    """MiniOneRec v10 node 12 before 69.10: three canary expiries each bought a ~20-min triage and a
    ~20-min repair (2 h 54 min, no metric). Now: the one mechanical retry at twice the cap, then
    `node_failed{canary_timeout}` — no triage judge, no Developer repair, no full eval."""
    ledger = tmp_path / "ledger.txt"
    code = _script(ledger, canary="sleep", full="0.9")
    dev, researcher = _Dev(code), _Researcher(repairs=3)
    eng = _engine(tmp_path / "run", dev, researcher=researcher,
                  canary={"env": {"LOOPLAB_CANARY": "1"}, "timeout": 1.5})
    _seed(eng, code)
    evs = _evaluate(eng)
    started, finished = _of(evs, EV_EVAL_CANARY_STARTED), _of(evs, EV_EVAL_CANARY_FINISHED)
    assert [s.data["timeout"] for s in started] == [1.5, 3.0]
    assert [s.data.get("retry") for s in started] == [None, 1]
    assert [(f.data["passed"], f.data["timed_out"], f.data.get("retry")) for f in finished] == [
        (False, True, None), (False, True, 1)]
    assert "within its 1.5s cap" in finished[0].data["error"]
    assert "within its 3s cap" in finished[1].data["error"]
    assert not any(f.data.get("near_cap") for f in finished), "near_cap is a PASS's record only"
    assert len({f.data["code_digest"] for f in started + finished}) == 1
    (term,) = _terminals(evs)
    assert term.type == "node_failed" and term.data["reason"] == "canary_timeout"
    assert term.data.get("reason_source", "engine") == "engine"
    # The terminal keeps the TAIL of the failure text, so it is the closing sentence it carries;
    # the header's own wording is `canary_failure_result`'s, pinned in the unit test above.
    assert ("(the canary did not finish within its 1.5s cap, nor within 3s on its one retry; the "
            "full evaluation was not started)") in term.data["error"]
    assert researcher.triaged == [], "a clock kill is not a failure a model is asked to read"
    assert dev.errors == [] and _of(evs, "node_repaired") == []
    assert _of(evs, "eval_invocation_claimed") == []
    assert "full" not in ledger.read_text().split(), "the multi-hour eval must never start"
    assert fold(evs).nodes[0].error_reason == "canary_timeout"


def test_a_canary_slow_only_on_its_first_run_passes_on_the_one_retry(tmp_path):
    """The cause a retry heals — a cold JIT/compile/download cache (MiniOneRec v10 node 13 passed at
    1020 s once the cap was raised): the retry passes, the full eval runs, no model is asked, and
    the retry's passed row is the one a resume keys on."""
    ledger, warm = tmp_path / "ledger.txt", tmp_path / "warm"
    code = _script(ledger, canary="cold", full="0.9", warm=warm)
    dev, researcher = _Dev(code), _Researcher()
    eng = _engine(tmp_path / "run", dev, researcher=researcher,
                  canary={"env": {"CANARY_USERS": "2000"}, "timeout": 2.0})
    _seed(eng, code)
    evs = _evaluate(eng)
    started, finished = _of(evs, EV_EVAL_CANARY_STARTED), _of(evs, EV_EVAL_CANARY_FINISHED)
    assert [s.data["timeout"] for s in started] == [2.0, 4.0]
    assert [(f.data["passed"], f.data.get("retry")) for f in finished] == [(False, None), (True, 1)]
    assert ledger.read_text().split() == ["canary", "canary", "full"]
    (term,) = _terminals(evs)
    assert term.type == "node_evaluated" and term.data["metric"] == 0.9
    assert researcher.triaged == [] and dev.errors == []
    assert canary_already_passed(evs, 0, 0, finished[1].data["code_digest"])
    # Both rounds are this attempt's eval seconds, like the full eval's own.
    assert term.data["eval_seconds"] >= sum(f.data["eval_seconds"] for f in finished) - 0.01


def test_a_retry_that_fails_otherwise_takes_the_ordinary_crash_path(tmp_path):
    """The doubled cap uncovered a DEFECT: that is a crash in its own words, and the triage judge is
    asked about it exactly as before — only the clock's verdict skips the model."""
    ledger, warm = tmp_path / "ledger.txt", tmp_path / "warm"
    code = _script(ledger, canary="cold_raise", full="0.9", warm=warm)
    dev, researcher = _Dev(code), _Researcher(repairs=0)
    eng = _engine(tmp_path / "run", dev, researcher=researcher,
                  canary={"env": {"CANARY_USERS": "2000"}, "timeout": 2.0})
    _seed(eng, code)
    evs = _evaluate(eng)
    finished = _of(evs, EV_EVAL_CANARY_FINISHED)
    assert [(f.data["passed"], f.data["timed_out"], f.data.get("retry")) for f in finished] == [
        (False, True, None), (False, False, 1)]
    (term,) = _terminals(evs)
    # The ENGINE's word is `crash`; the judge was asked and named no kind, so the row reads
    # `unclassified` beside it — the ordinary path, word for word.
    assert term.type == "node_failed" and term.data["engine_reason"] == "crash"
    assert term.data["reason"] in ("crash", "unclassified")
    assert "history_item_sid" in term.data["error"]
    assert "(the canary exited 1; the full evaluation was not started)" in term.data["error"]
    assert len(researcher.triaged) == 1 and "history_item_sid" in researcher.triaged[0]
    assert "full" not in ledger.read_text().split()


@pytest.mark.parametrize("control", ["run_abort", "pause"])
def test_no_retry_once_the_run_stopped_taking_work(tmp_path, control):
    """A retry is NEW work: a stop or a pause recorded while the first round ran means no second
    round. A stopping run settles the node on the one expiry it had; a paused one keeps it pending
    (no terminal), so the canary is owed again after the pause lifts."""
    ledger = tmp_path / "ledger.txt"
    code = _script(ledger, canary="sleep", full="0.9")
    dev, researcher = _Dev(code), _Researcher()
    eng = _engine(tmp_path / "run", dev, researcher=researcher,
                  canary={"env": {"LOOPLAB_CANARY": "1"}, "timeout": 1.5})
    _seed(eng, code)
    real_round = eng._eval_canary_round

    async def _round(a, spec, digest, scratch, cancel, *, retry):
        out = await real_round(a, spec, digest, scratch, cancel, retry=retry)
        if retry == 0:                               # the operator's control lands mid-canary
            eng.store.append(control, {"reason": "operator"})
        return out

    eng._eval_canary_round = _round
    evs = _evaluate(eng)
    assert len(_of(evs, EV_EVAL_CANARY_STARTED)) == 1, "the retry ran on a run taking no work"
    assert researcher.triaged == [] and dev.errors == []
    if control == "pause":
        assert _terminals(evs) == [] and 0 in {n.id for n in fold(evs).pending_nodes()}
        return
    (term,) = _terminals(evs)
    assert term.type == "node_failed" and term.data["reason"] == "canary_timeout"
    assert "its one retry was not run" in term.data["error"]


def _expired(*, timed_out: bool):
    return RunResult(exit_code=-9, stdout="", stderr="killed\n", metric=None, timed_out=timed_out)


def _passed():
    return RunResult(exit_code=0, stdout="METRIC: 0.1\n", stderr="", metric=0.1, timed_out=False)


@pytest.mark.parametrize("shape", ["chain_clock", "stage_cap"])
def test_either_clock_buys_the_retry(tmp_path, shape):
    """Both of the canary's clocks are the CLOCK: the whole-chain `CanaryClock` (`expired`, the
    result itself not marked) and a stage's own cap (`res.timed_out`). Driven through a stub of the
    blocking half, so which of the two fired is decided here rather than by a race."""
    ledger = tmp_path / "ledger.txt"
    code = _script(ledger, canary="0.1", full="0.9")
    researcher = _Researcher()
    eng = _engine(tmp_path / "run", _Dev(code), researcher=researcher)
    _seed(eng, code)
    caps: list = []

    def _stub(a, spec, scratch, cancel):
        caps.append(spec["timeout"])
        if len(caps) == 1:
            return ((_expired(timed_out=False), True) if shape == "chain_clock"
                    else (_expired(timed_out=True), False))
        return _passed(), False

    eng._run_canary_in_scratch = _stub
    evs = _evaluate(eng)
    assert caps == [60.0, 120.0]
    (term,) = _terminals(evs)
    assert term.type == "node_evaluated" and term.data["metric"] == 0.9
    assert researcher.triaged == []


def test_a_kill_at_the_eval_s_OWN_timeout_is_not_the_canary_s_clock(tmp_path):
    """The chain runs at `min(own, cap)`: an eval whose own timeout (30 s) is under the canary cap
    (60 s) is killed at 30 s under the doubled cap too, so the retry was pure waste and "nor within
    120s on its one retry" was false (critic 2026-09-29). It takes the ordinary path, in its own
    words. MUTATION: drop `own is None` from `clocked` -> a second canary runs."""
    ledger = tmp_path / "ledger.txt"
    code = _script(ledger, canary="0.1", full="0.9")
    researcher = _Researcher(repairs=0)
    eng = _engine(tmp_path / "run", _Dev(code), researcher=researcher)
    eng._eval_spec["timeout"] = 30.0
    _seed(eng, code)
    caps: list = []

    def _stub(a, spec, scratch, cancel):
        caps.append(spec["timeout"])
        return _expired(timed_out=True), False

    eng._run_canary_in_scratch = _stub
    evs = _evaluate(eng)
    assert caps == [60.0], "a retry that cannot outlast the eval's own clock was run"
    (finished,) = _of(evs, EV_EVAL_CANARY_FINISHED)
    assert "own 30s timeout" in finished.data["error"]
    (term,) = _terminals(evs)
    assert term.data.get("reason") != "canary_timeout" and len(researcher.triaged) == 1


@pytest.mark.parametrize("when", ["during", "after_expiry"])
def test_an_operator_intervention_is_never_retried(tmp_path, when):
    """`during`: the operator's intervention stopped the canary before any clock — it is not the
    clock's verdict, so it keeps the ordinary path (a crash, the judge asked) and is not retried.
    `after_expiry`: the clock stopped it first and the operator intervened before the retry could
    start — no retry, and the node carries the one expiry it had."""
    ledger = tmp_path / "ledger.txt"
    code = _script(ledger, canary="0.1", full="0.9")
    researcher = _Researcher(repairs=0)
    eng = _engine(tmp_path / "run", _Dev(code), researcher=researcher)
    _seed(eng, code)
    caps: list = []

    def _stub(a, spec, scratch, cancel):
        caps.append(spec["timeout"])
        cancel.set()                                  # the intervention's own Event
        return _expired(timed_out=True), when == "after_expiry"

    eng._run_canary_in_scratch = _stub
    evs = _evaluate(eng)
    assert caps == [60.0], "an intervention bought a second canary"
    (finished,) = _of(evs, EV_EVAL_CANARY_FINISHED)
    (term,) = _terminals(evs)
    if when == "during":
        assert finished.data["error"] == "interrupted by an operator intervention"
        assert term.data["engine_reason"] == "crash" and len(researcher.triaged) == 1
    else:
        assert term.data["reason"] == "canary_timeout" and researcher.triaged == []
        assert "its one retry was not run" in term.data["error"]


@pytest.mark.parametrize("share, near", [(0.8, True), (0.0, False)])
def test_a_pass_close_to_its_cap_is_recorded_and_said(tmp_path, caplog, share, near):
    """MiniOneRec v10 node 10 passed at 92 % of its cap in silence. A pass at >= 75 % is marked
    `near_cap` on its row and logged at WARNING; it changes nothing else — the full eval runs."""
    ledger = tmp_path / "ledger.txt"
    code = _script(ledger, canary="0.1", full="0.9")
    eng = _engine(tmp_path / "run", _Dev(code),
                  canary={"env": {"CANARY_USERS": "2000"}, "timeout": 1.0})
    _seed(eng, code)

    def _timed_pass(a, spec, scratch, cancel):
        import time
        time.sleep(share * spec["timeout"])
        return RunResult(exit_code=0, stdout="METRIC: 0.1\n", stderr="", metric=0.1,
                         timed_out=False), False

    eng._run_canary_in_scratch = _timed_pass
    with caplog.at_level("WARNING", logger="looplab.engine.evaluate"):
        evs = _evaluate(eng)
    (finished,) = _of(evs, EV_EVAL_CANARY_FINISHED)
    assert finished.data["passed"] is True
    assert finished.data.get("near_cap", False) is near
    said = [r for r in caplog.records if "eval canary for node 0 passed" in r.getMessage()]
    assert bool(said) is near
    (term,) = _terminals(evs)
    assert term.type == "node_evaluated" and term.data["metric"] == 0.9


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
