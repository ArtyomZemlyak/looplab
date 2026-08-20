"""What a repair chain that re-runs work WITHOUT discarding a completed stage may spend.

THE COMPOSITION, found by the 2026-08-15 merge-day review and reproduced here before it was
changed. Three same-day changes, each individually documented and defensible:

  (a) `Settings.inline_repair_attempts` 12 -> 0 (F8: the repair bound became a judgment);
  (b) `evaluate.py::_effective_repair_cap(0)` -> `_UNLIMITED_REPAIR_CEILING` (50), a TIGHTENING of
      the `or 10**9` that preceded it;
  (c) `triage.py::_rule_triage` turned the no-judge path's `abandon` for a NON-MECHANICAL crash into
      `repair` (F5: the Debug node that made `abandon` conservative was deleted, so the cautious
      spelling had become the destructive one).

Their product, driven below on the configuration where no triage model is wired — a supported
configuration, `crash_repair.py::_triage_crash`'s own "a configuration, not a failure" branch — is
**51 full pipeline evaluations, 50 repairs, and `inline_repair_retrain_cap` charging ZERO of them**,
because `_repair_forces_full_retrain` answers False for a FIRST-stage failure: nothing completed, so
nothing is re-trained. That rule is not wrong. What moved underneath it is the thing it delegates to
— its comment says such a repair is "bounded by the attempt budget like any other", and when that
was written the attempt budget was 12 and was the transition.

AND THE CRITIC IS NOT THERE. `repair_critic_after=3` is supposed to bound the 50 by a judgement, and
on `runs/rubertlite-dr-unified-v8` (which HAS `unified_agent: true`) it fired 6 times. But
`crash_repair.py::_repair_critic` reads `researcher.repair_critic` — the same object that would have
carried `triage_crash` — so in the configuration that produces the 50 there is no critic either.
`test_the_critic_is_absent_from_exactly_the_configuration_that_reaches_fifty` drives that.

WHAT THE FIX IS NOT. Charging the retrain cap per first-stage repair was measured against the live
record and rejected: v8 node 3 failed `mine` (its FIRST stage) three times and PASSED it on the
fourth attempt, so at the default `inline_repair_retrain_cap=2` a per-repair charge abandons that
node one repair before the stage it was fixing worked. `test_the_v8_node_3_chain_is_not_abandoned`
is that negative control, driven at v8's own declared stage timeouts.
"""
from __future__ import annotations

import json
import sys

import anyio
import pytest

from looplab.events.eventstore import EventStore
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.runtime.sandbox import RunResult, SubprocessSandbox

_M = {"kind": "stdout_json", "key": "metric"}

# The bound that actually stops a no-judge chain on a crash the rule path cannot classify. Imported
# rather than typed, because a test that hard-codes 12 pins the number instead of the rule.
from looplab.engine.triage import _RULE_BLIND_CRASH_ATTEMPTS as _BLIND

# v8's own manifest (`runs/rubertlite-dr-unified-v8/nodes/node_3/looplab_stages.json`): a `mine`
# stage the operator declared at 9000 s in front of a `train` at 22000 s. The numbers are the point
# — the whole defect is that re-running the FIRST of those is charged nothing while re-running the
# second is charged one full re-train.
_MINE_TIMEOUT = 9000.0
_TRAIN_TIMEOUT = 22000.0
# What node 3's first `mine` attempt actually took, from that run's event timestamps
# (node_eval_started 1786749686 -> node_repaired#1 1786754597).
_V8_FIRST_MINE_SECONDS = 4911.0


def _staged_src(tmp_path, *, mine_timeout=_MINE_TIMEOUT, train_timeout=_TRAIN_TIMEOUT):
    """v8 node 3's pipeline shape: mine -> train, with the operator's score command appended by the
    engine as the protected stage."""
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "mine.py").write_text("print('mine')\n", encoding="utf-8")
    (src / "train.py").write_text("import loss\nprint('train')\n", encoding="utf-8")
    (src / "loss.py").write_text("x = 1\n", encoding="utf-8")
    (src / "looplab_eval.py").write_text("print('score v0')\n", encoding="utf-8")
    (src / "looplab_stages.json").write_text(json.dumps({"stages": [
        {"name": "mine", "command": ["python", "mine.py"], "timeout": mine_timeout},
        {"name": "train", "command": ["python", "train.py"], "timeout": train_timeout}]}),
        encoding="utf-8")
    return src


def _fail_mine(stderr, seconds=1.0):
    """The FIRST stage crashed and nothing else ever ran — one stage record, so
    `_repair_forces_full_retrain`'s `was_first` is True and the retrain cap is never consulted."""
    return RunResult(exit_code=1, stdout="", stderr=stderr, metric=None, timed_out=False,
                     failed_stage="mine",
                     stages=[{"name": "mine", "status": "fail", "exit_code": 1,
                              "seconds": seconds}])


def _fail_train(stderr):
    """A LATER stage failed over a completed `mine` — the case the retrain cap already charges."""
    return RunResult(exit_code=1, stdout="", stderr=stderr, metric=None, timed_out=False,
                     failed_stage="train",
                     stages=[{"name": "mine", "status": "ok", "exit_code": 0, "seconds": 1.0},
                             {"name": "train", "status": "fail", "exit_code": 1, "seconds": 1.0}])


class _MineDev:
    """A Developer that always edits the FIRST stage's own script — a real byte change every attempt
    (so `repair_verify`'s inert-streak rung never fires) that never touches a later stage."""

    def __init__(self, target="mine.py"):
        self.target = target
        self.repair_calls = 0
        self.last_files: dict = {}
        self.last_deleted: list = []

    def implement(self, idea):
        self.last_files, self.last_deleted = {}, []
        return ""

    def repair(self, idea, code, error):
        self.repair_calls += 1
        self.last_files = {self.target: f"print('v{self.repair_calls}')\n"}
        self.last_deleted = []
        return ""


def _drive(tmp_path, monkeypatch, results, dev=None, *, src=None, eval_seconds=None, **kw):
    """Run the REAL repair loop over a staged repo with `run_command_eval` stubbed.

    `eval_seconds` makes each stubbed eval CLAIM wall-clock the loop then accounts for, which is what
    a cost floor has to be driven against — a stub that returns instantly spends nothing.
    """
    from looplab.runtime import command_eval

    src = src or _staged_src(tmp_path)
    dev = dev or _MineDev()
    calls: list = []
    clock = {"now": 1_000_000.0}
    if eval_seconds is not None:
        monkeypatch.setattr("looplab.engine.evaluate.time.time", lambda: clock["now"])

    def fake(cmd, _cwd, timeout, metric, env=None, **kwargs):
        calls.append(kwargs.get("start_stage"))
        if eval_seconds is not None:
            clock["now"] += float(eval_seconds)
        return results.pop(0) if results else _fail_mine("RuntimeError: still empty\n")

    monkeypatch.setattr(command_eval, "run_command_eval", fake)
    task = RepoTask(id="r", direction="max", editable_path=str(src),
                    edit_surface=["*.py", "*.yaml", "*.json"],
                    eval=EvalSpec(command=[sys.executable, "looplab_eval.py"], metric=_M, cwd="."))
    researcher, _ = task.build_roles()
    kw.setdefault("auto_install_deps", False)
    kw.setdefault("inline_repair", True)
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)
    anyio.run(eng.run)
    return list(EventStore(tmp_path / "run" / "events.jsonl").read_all()), calls, dev, researcher


def _of(evs, type_, node_id=0):
    return [e for e in evs if e.type == type_ and e.data.get("node_id") == node_id]


def _terminal(evs, node_id=0):
    rows = [e for e in evs if e.type in ("node_evaluated", "node_failed")
            and e.data.get("node_id") == node_id]
    assert len(rows) == 1, f"expected exactly one terminal, got {len(rows)}"
    return rows[0]


# --------------------------------------------------------------- the composition, reproduced
def test_a_first_stage_crash_with_no_judge_is_charged_nothing_by_the_retrain_cap(tmp_path,
                                                                                 monkeypatch):
    """THE DEFECT ITSELF, and the half of it that survives the fix.

    `_repair_forces_full_retrain` answers False for a first-stage failure, so a chain that re-runs
    the whole pipeline every attempt spends ZERO of `inline_repair_retrain_cap`. That is still true
    after the fix and is deliberately so — the cap counts DISCARDED COMPLETED WORK and a first-stage
    failure discards none. What the fix adds is a second floor; this test pins the charging rule it
    is layered over, so a later change that starts charging the cap here goes red and has to argue
    with `test_the_v8_node_3_chain_is_not_abandoned` below.
    """
    # `eval_seconds` is left unset, so every stubbed eval costs no measurable wall clock and the
    # SECONDS floor added below has nothing to charge. That isolates the COUNT rule, which is the
    # thing this test pins: every re-run a FULL one and not one of them charged.
    #
    # The COUNT itself moved 50 -> 12 on 2026-08-15 and the two numbers name two different bounds.
    # 50 is `_UNLIMITED_REPAIR_CEILING`, the backstop under a JUDGE; what actually stops this chain
    # now is `triage._RULE_BLIND_CRASH_ATTEMPTS`, because a `RuntimeError` is not in
    # `_MECHANICAL_MARKERS` and the rule path can say nothing about it — repairing blind is worth a
    # bounded number of tries and is not worth fifty when, as the test below drives, there is no
    # judge and no critic under it either. The charging rule this test pins is unchanged.
    evs, calls, dev, researcher = _drive(
        tmp_path, monkeypatch, [],
        inline_repair_attempts=0, inline_repair_retrain_cap=2, repair_critic_after=3)

    assert not callable(getattr(researcher, "triage_crash", None)), "a judge was wired"
    assert len(_of(evs, "node_repaired")) == _BLIND, "the blind bound is what stopped it"
    assert dev.repair_calls == _BLIND
    assert len(calls) == _BLIND + 1, "one original eval plus the blind re-runs"
    assert set(calls) == {None}, "every re-run was a FULL pipeline run, never a stage reuse"
    assert _of(evs, "full_retrain_charged") == [], (
        "a first-stage repair discards no completed stage, so the retrain cap charges nothing — "
        "the composition's headline")
    rationale = _terminal(evs).data.get("triage_rationale", "")
    assert "cannot classify" in rationale and f"{_BLIND} blind" in rationale, rationale
    assert "ceiling of 50" not in rationale, (
        "the ceiling is a DIFFERENT bound with a different remedy; quoting it here sends the "
        "operator to raise a cap that was never reached")


def test_the_critic_is_absent_from_exactly_the_configuration_the_rule_path_serves(tmp_path,
                                                                                  monkeypatch):
    """THE CRUX. "The critic bounds this chain in practice" is true where a judge is wired and FALSE
    here, because `_repair_critic` and `_triage_crash` read the same object.

    So the configuration this chain runs in is the one with no judgement of any kind under it: the
    count is not a floor beneath a judgement, it is the entire policy — which is why
    `triage._RULE_BLIND_CRASH_ATTEMPTS` had to be the calibrated count and not the backstop.
    `repair_critic_after=3` is set below and buys nothing.
    """
    from looplab.engine.crash_repair import CrashRepairMixin

    consulted: list = []
    original = CrashRepairMixin._repair_critic

    def _record(self, state, node, repair_log, attempt):
        out = original(self, state, node, repair_log, attempt)
        consulted.append(out)
        return out

    monkeypatch.setattr(CrashRepairMixin, "_repair_critic", _record)
    evs, _calls, _dev, _r = _drive(tmp_path, monkeypatch, [], inline_repair_attempts=0,
                                   inline_repair_retrain_cap=2, repair_critic_after=3)

    assert len(_of(evs, "node_repaired")) == _BLIND
    assert consulted, "the loop never even asked — the gate would be a different finding"
    assert all(c.get("rationale") == "no repair critic wired" for c in consulted), (
        "the critic that is supposed to bound this chain does not exist in this configuration")
    assert all(c.get("action") == "continue" for c in consulted)


# --------------------------------------------------------------- the floor that was added
def test_the_cost_floor_bounds_a_first_stage_chain_at_what_the_cap_licenses(tmp_path, monkeypatch):
    """The fix, driven: the SAME `inline_repair_retrain_cap` the operator set, spent in seconds
    against what the task declares one full pipeline costs.

    `cap=2` licenses three pipelines of 9000 + 22000 + the appended score stage, i.e. 94800 s.

    `eval_seconds` is 12000 rather than the 6200 this test was first written at, and the reason is
    worth stating because it is the ONE place the two 2026-08-15 bounds meet. At 6200 s the seconds
    floor binds at 15 repairs, which is now BEHIND `triage._RULE_BLIND_CRASH_ATTEMPTS` (12) — the
    blind bound would stop the chain first and this test would be pinning that instead, silently.
    They pull the same way (both stop a chain the other would let run) but they measure different
    things, and a test for the seconds floor has to be driven where seconds are what runs out: at
    12000 s an eval, the allowance is gone after eight of them. The assertion below is bounded ABOVE
    by the blind count for exactly that reason — if a later change makes the blind bound bind first,
    this goes red rather than quietly re-pointing.
    """
    evs, calls, dev, _r = _drive(
        tmp_path, monkeypatch, [], eval_seconds=12000.0,
        inline_repair_attempts=0, inline_repair_retrain_cap=2, repair_critic_after=3)

    repairs = _of(evs, "node_repaired")
    assert 5 <= len(repairs) < _BLIND, f"the cost floor did not bind: {len(repairs)} repairs"
    assert len(calls) == len(repairs) + 1
    assert _of(evs, "full_retrain_charged") == [], "the charging rule is unchanged"
    error = _terminal(evs).data.get("triage_rationale", "")
    assert "inline_repair_retrain_cap=2" in error and "licenses" in error, error
    assert "charged in seconds" in error, error   # not truncated by the 300-char cut
    # The durable half: every row carries the eval it answers, which is what survives a resume.
    assert all(float(e.data.get("eval_seconds") or 0) == pytest.approx(12000.0) for e in repairs)


def test_the_v8_node_3_chain_is_not_abandoned(tmp_path, monkeypatch):
    """THE NEGATIVE CONTROL THAT DECIDED THE DESIGN, replayed at v8's own numbers.

    `runs/rubertlite-dr-unified-v8` node 3 failed its FIRST stage three times and passed it on the
    fourth attempt (`node_repaired` attempts 1-3 carry `stages_passed: 0`, attempt 4 carries 1). Its
    first `mine` took 4911 s and the whole chain 9567 s. Charging `inline_repair_retrain_cap=2` once
    per first-stage repair — the obvious fix, "make the charge about work redone" — abandons that
    node at attempt 3, one repair before the stage it was fixing worked.

    The seconds floor does not, and this is why the ledger stayed a COUNT of discarded re-trains: at
    v8's declared 9000 + 22000 s pipeline the licensed allowance is far above what a chain that
    keeps failing EARLY can spend, while it still binds on one that keeps failing early FOREVER.
    """
    results = [_fail_mine("RuntimeError: mined < 2 negatives\n", seconds=_V8_FIRST_MINE_SECONDS),
               _fail_mine("RuntimeError: mined < 2 negatives\n", seconds=1168.0),
               _fail_mine("TypeError: mine_negatives() got an unexpected keyword\n", seconds=504.0),
               # attempt 4: `mine` PASSED and the failure moved to `train`.
               _fail_train("torch.OutOfMemoryError: CUDA out of memory\n"),
               RunResult(exit_code=0, stdout='{"metric": 0.74}', stderr="", metric=0.74,
                         timed_out=False,
                         stages=[{"name": "mine", "status": "reused", "exit_code": 0,
                                  "seconds": 0.0},
                                 {"name": "train", "status": "ok", "exit_code": 0, "seconds": 1.0},
                                 {"name": "score", "status": "ok", "exit_code": 0, "seconds": 1.0}])]
    # The real chain's own per-attempt wall clock, from v8's event timestamps.
    seconds = iter([4911.0, 1168.0, 504.0, 2984.0, 10.0])
    from looplab.runtime import command_eval
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr("looplab.engine.evaluate.time.time", lambda: clock["now"])
    dev = _MineDev()

    def fake(cmd, _cwd, timeout, metric, env=None, **kwargs):
        clock["now"] += next(seconds, 10.0)
        # After `mine` passed, the Developer moves to the training code like v8's attempt 4 did.
        dev.target = "train.py" if len(results) <= 2 else "mine.py"
        return results.pop(0)

    monkeypatch.setattr(command_eval, "run_command_eval", fake)
    src = _staged_src(tmp_path)
    task = RepoTask(id="r", direction="max", editable_path=str(src),
                    edit_surface=["*.py", "*.yaml", "*.json"],
                    eval=EvalSpec(command=[sys.executable, "looplab_eval.py"], metric=_M, cwd="."))
    researcher, _ = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True, inline_repair_attempts=0,
                 inline_repair_retrain_cap=2, repair_critic_after=3)
    anyio.run(eng.run)
    evs = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())

    assert len(_of(evs, "node_repaired")) == 4, (
        "v8 node 3's four repairs must all still be bought — the chain reached its research "
        "question and this is the failure direction that costs a node")
    assert _terminal(evs).type == "node_evaluated"


def test_a_mechanical_first_stage_failure_still_gets_its_repairs(tmp_path, monkeypatch):
    """The other negative control the review asked for: the fix must not touch the rule path's
    treatment of a MECHANICAL crash, which `_rule_triage` has repaired since long before F5."""
    results = [_fail_mine("ModuleNotFoundError: No module named 'faiss'\n"),
               _fail_mine("ImportError: cannot import name 'mine_negatives'\n"),
               RunResult(exit_code=0, stdout='{"metric": 0.5}', stderr="", metric=0.5,
                         timed_out=False,
                         stages=[{"name": "mine", "status": "ok", "exit_code": 0, "seconds": 1.0},
                                 {"name": "train", "status": "ok", "exit_code": 0, "seconds": 1.0},
                                 {"name": "score", "status": "ok", "exit_code": 0, "seconds": 1.0}])]
    evs, _calls, dev, _r = _drive(tmp_path, monkeypatch, results, eval_seconds=5.0,
                                  inline_repair_attempts=0, inline_repair_retrain_cap=2)
    assert dev.repair_calls == 2
    assert _terminal(evs).type == "node_evaluated"


def test_a_later_stage_repair_is_still_charged_the_retrain_cap(tmp_path, monkeypatch):
    """And the rule the fix is layered OVER is untouched: a repair that discards a completed `mine`
    still charges `inline_repair_retrain_cap`, still abandons at it, and still says so in the
    operator-facing sentence two tests pin verbatim."""
    results = [_fail_train("RuntimeError: loss went nan\n"),
               _fail_train("RuntimeError: loss went nan again\n")]
    dev = _MineDev(target="mine.py")     # editing an EARLIER stage refuses reuse -> a full re-train
    evs, _calls, _dev, _r = _drive(tmp_path, monkeypatch, results, dev=dev,
                                   inline_repair_attempts=5, inline_repair_retrain_cap=1)
    assert len(_of(evs, "full_retrain_charged")) == 1
    assert "full re-train(s) already spent" in _terminal(evs).data.get("triage_rationale", "")


# --------------------------------------------------------------- the rules, as truth tables
def test_declared_pipeline_seconds_prefers_the_stages_and_fails_open():
    from looplab.engine.repair_judgment import declared_pipeline_seconds

    assert declared_pipeline_seconds(
        [{"name": "mine", "timeout": 9000.0}, {"name": "train", "timeout": 22000.0}]) == 31000.0
    # A stage with no declared timeout contributes nothing rather than a guess.
    assert declared_pipeline_seconds([{"name": "mine"}, {"name": "train", "timeout": 5.0}]) == 5.0
    # No stage declares anything -> the single-command timeout; nothing at all -> no bound.
    assert declared_pipeline_seconds([{"name": "mine"}], 600.0) == 600.0
    assert declared_pipeline_seconds([], 600.0) == 600.0
    assert declared_pipeline_seconds([], None) == 0.0
    assert declared_pipeline_seconds(None) == 0.0
    assert declared_pipeline_seconds(["not a stage", {"timeout": "nonsense"}], "nope") == 0.0


def test_the_cost_floor_licenses_the_original_run_plus_what_the_cap_pays_for():
    from looplab.engine.repair_judgment import repair_redone_work_stop

    def stop(spent, declared=100.0, cap=2):
        return repair_redone_work_stop(chain_seconds=spent, pipeline_seconds=declared,
                                       retrain_cap=cap)

    assert stop(299.0) is None            # under (cap + 1) * declared
    assert stop(300.0) is not None        # at it
    assert "inline_repair_retrain_cap=2" in stop(300.0) and "300s" in stop(300.0)
    # FAILS OPEN in both documented directions: the legacy unlimited cap, and an eval whose cost
    # nobody declared. A floor that guessed a pipeline's cost would abandon nodes on an invented
    # number, which is the expensive direction.
    assert stop(10 ** 9, cap=0) is None
    assert stop(10 ** 9, declared=0.0) is None
    assert stop(10 ** 9, declared=None) is None
    assert repair_redone_work_stop(chain_seconds=10 ** 9, pipeline_seconds=1.0,
                                   retrain_cap=True) is None    # a bool is not a cap


# --------------------------------------------------------- the bound on repairing BLIND (2026-08-15)
def test_there_is_ONE_crash_bound_and_the_text_no_longer_chooses_which():
    """`_rule_triage`'s truth table across the crash branch — ONE branch since 2026-08-20, not two.

    F5's change is kept — a `crash` the rule path cannot classify still buys a repair rather than an
    instant abandon, which is the whole point of the branch (a `RuntimeError` one edit would fix must
    not end a node just because nothing recognised it). What it gains is a bound, because in the
    configuration this function exists for there is nothing else: no triage judge, no critic, and
    `LEGACY_CONFIG_SNAPSHOT_DEFAULTS`' `inline_repair_attempts: 0` resolves to the engine's 50.

    **THE SECOND BRANCH IS DELETED AND THIS TEST NOW HOLDS ITS ABSENCE.** `_MECHANICAL_MARKERS`
    scanned stderr for `ImportError`/`NameError`/`TypeError`/"has no attribute"/… and, on a hit,
    granted the caller's full cap instead of the blind 12. That is a bound that depends on the TEXT
    QUALITY of a program's error output, which is exactly what `engine/triage.py`'s own header says
    is not a bound — the 2026-08-05 finding, kept alive in the one branch that chose an ACTION
    rather than a classification. A Cyrillic identifier, a blank stderr, a launcher that swallows the
    child exception and any non-English traceback all read as "not mechanical" and silently got the
    shorter budget; a `RuntimeError` whose message happened to contain the word `TypeError` got the
    longer one.

    **THE DELETION COSTS NOTHING MEASURABLE, and that is why it is a deletion rather than a better
    scanner.** Re-derived over every `node_repaired` row in `runs/` (2026-08-20): 50 nodes have a
    repair chain, the MEDIAN is 2, the MAXIMUM is 5, and ZERO chains are longer than 12 — so the
    split separated 12 from 50 while nothing on this box ever reached 6. The longest chain on record
    anywhere, `rubert-dr-0805` node 0's eight repairs (six of them the stale-dependency migrations
    this branch existed to protect), also clears 12. `_RULE_BLIND_CRASH_ATTEMPTS` was calibrated
    against exactly that chain plus room for one half again as long, which is what makes it the
    single bound rather than the shorter of two.
    """
    from looplab.engine.triage import _rule_triage

    blind = _BLIND
    # The improvement survives: the FIRST undiagnosable crash is repaired, not abandoned.
    assert _rule_triage("crash", "AssertionError: metric too low", 1, 50)["action"] == "repair"
    assert _rule_triage("crash", "RuntimeError: x", blind, 50)["action"] == "repair"
    # ...and it stops there, with a rationale that names THIS bound rather than the ceiling.
    stopped = _rule_triage("crash", "RuntimeError: x", blind + 1, 50)
    assert stopped["action"] == "abandon"
    assert "cannot classify" in stopped["rationale"] and f"{blind} blind" in stopped["rationale"]

    # THE TEXT NO LONGER CHOOSES THE BUDGET. A traceback that names a Python exception class gets
    # exactly the bound a `RuntimeError` gets — which is the whole change, driven rather than
    # asserted about source: every marker the deleted list held, at the same attempt, same answer.
    for marker in ("ImportError: no name", "ModuleNotFoundError: no module named 'x'",
                   "NameError: name 'x' is not defined", "AttributeError: has no attribute",
                   "SyntaxError: bad", "TypeError: unexpected keyword argument"):
        assert _rule_triage("crash", marker, blind, 50)["action"] == "repair"
        assert _rule_triage("crash", marker, blind + 1, 50)["action"] == "abandon", (
            f"{marker!r} still buys a wider budget than a crash nobody could name")
    # …and the same holds for the shapes the marker list could never see, which is the other half of
    # why it was not a bound: a Cyrillic identifier and an empty stderr.
    for unseeable in ("ImportError: не удалось импортировать модуль", ""):
        assert _rule_triage("crash", unseeable, blind, 50)["action"] == "repair"
        assert _rule_triage("crash", unseeable, blind + 1, 50)["action"] == "abandon"

    # Never a WIDENING: an operator who spelled a smaller cap keeps it, and the message says which.
    tight = _rule_triage("crash", "RuntimeError: x", 3, 2)
    assert tight["action"] == "abandon" and "2 blind" in tight["rationale"]
    assert _rule_triage("crash", "RuntimeError: x", 2, 2)["action"] == "repair"

    # `unclassified` — a WIRED diagnostician that could not answer — shares the blind branch by
    # design, because a failure nobody could name is the definition of repairing blind.
    from looplab.engine.failure_diagnosis import UNCLASSIFIED_REASON
    assert _rule_triage(UNCLASSIFIED_REASON, "", 1, 50)["action"] == "repair"
    assert _rule_triage(UNCLASSIFIED_REASON, "", blind + 1, 50)["action"] == "abandon"

    # And nothing else moved: a non-crash reason still ends the node here exactly as before.
    for reason in ("drift", "no_metric", "idea_rejected"):
        out = _rule_triage(reason, "", 1, 50)
        assert out["action"] == "abandon" and "non-repairable" in out["rationale"]


# ----------------------------------------------- what ONE pipeline is licensed against (2026-08-15)
def _unstaged_src(tmp_path):
    """A single-command eval: no `looplab_stages.json`, so `_resolved_stages` is [] and the cost
    floor's licensed number comes entirely from the eval spec's own timeout."""
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "looplab_eval.py").write_text("print('score v0')\n", encoding="utf-8")
    (src / "train.py").write_text("print('train')\n", encoding="utf-8")
    return src


def test_the_cost_floor_is_licensed_against_the_profile_the_eval_actually_runs(tmp_path,
                                                                               monkeypatch):
    """The floor must charge a chain against the budget the node RUNS under, not the base profile's.

    A repo task whose active profile raises the timeout — `timeout: 600` beside
    `profiles: {full: {timeout: 21600}}` — really does get 21600 s from `build_command`. Licensing
    the chain against the raw `timeout` key charged a six-hour attempt at a ten-minute pipeline's
    rate, so `(cap + 1) * 600 = 1800` was already spent by the END OF THE FIRST EVAL: the floor fired
    before any repair was attempted and the terminal told the operator their task "declares" a
    pipeline 36x cheaper than the one they wrote. `command_eval.eval_spec_time_budget` is the
    derivation the rest of the engine quotes to both roles, and it is now the one this floor reads.
    """
    from looplab.runtime import command_eval

    results: list = []
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr("looplab.engine.evaluate.time.time", lambda: clock["now"])

    def fake(cmd, _cwd, timeout, metric, env=None, **kwargs):
        clock["now"] += 21600.0            # what the `full` profile's own leash actually costs
        return RunResult(exit_code=1, stdout="", stderr="RuntimeError: diverged\n", metric=None,
                         timed_out=False)

    monkeypatch.setattr(command_eval, "run_command_eval", fake)
    src = _unstaged_src(tmp_path)
    task = RepoTask(id="r", direction="max", editable_path=str(src),
                    edit_surface=["*.py"],
                    eval=EvalSpec(command=[sys.executable, "looplab_eval.py"], metric=_M, cwd=".",
                                  timeout=600.0, profiles={"full": {"timeout": 21600.0}}))
    researcher, _ = task.build_roles()
    dev = _MineDev(target="train.py")
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True, inline_repair_attempts=0,
                 inline_repair_retrain_cap=2, repair_critic_after=0)
    anyio.run(eng.run)
    evs = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())

    repairs = _of(evs, "node_repaired")
    assert len(repairs) >= 1, (
        "the floor fired before a single repair was attempted — it licensed the chain against the "
        "base profile's 600 s while the attempt spent 21600")
    rationale = _terminal(evs).data.get("triage_rationale", "")
    if "licenses" in rationale:               # the floor is what stopped it, so it must say 21600
        assert "600s" not in rationale.replace("21600s", ""), rationale
        assert "21600s" in rationale, rationale
