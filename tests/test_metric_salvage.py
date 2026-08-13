"""METRIC SALVAGE — a node that failed for something other than "the metric is absent", and whose
metric the operator's own declared reader can still find, comes out EVALUATED with provenance.

THE RUN. `runs/rubertlite-dr-unified-v5` node 0 trained for 76 minutes on two H200s, exited 0, ran
the scorer at the end of its own `train` stage and printed

    RECALL@100: 0.743250

which is EXACTLY what the run's declared metric reader matches
(`{"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)"}`). The node was failed with
`reason: no_metric` because the stage manifest declared the checkpoint at
`.../unified-baseline_rubert-tiny-lite/final/model.safetensors` while the testbed composes its
output dir as `<run_name>_<model>`. Two independent gates in `command_eval._run_stages` had to hold
for that: the `expect_failed` early return hard-codes `metric=None`, and the crash branch's salvage
read is gated on `_i == len(stages) - 1` while `train` is not last (the operator's protected `score`
stage is appended after it).

The end-to-end cases below drive the REAL `Engine._evaluate` over a REAL subprocess pipeline shaped
exactly like that one — a `train` stage that prints the metric and then fails its declared artifact
contract — and observe the node's ONE terminal event. The truth-table cases beside them drive the
pure rules directly, because most of their branches are otherwise reachable only by running a whole
sandboxed evaluation that failed in precisely the right way.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Idea, NodeStatus
from looplab.engine import metric_salvage as ms
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime.sandbox import RunResult, SubprocessSandbox
from looplab.search.policy import GreedyTree

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"

# The v5 spec, verbatim from `runs/rubertlite-dr-unified-v5/task.snapshot.json`.
V5_METRIC = {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)"}
V5_VALUE = 0.743250
DECLARED = "vectorsearch/experiments/unified-baseline_rubert-tiny-lite/final/model.safetensors"
ACTUAL = ("vectorsearch/experiments/unified-baseline_rubert-tiny-lite_rubert-tiny-lite/final/"
          "model.safetensors")


# --------------------------------------------------------------------------------------------
# the pure rules
# --------------------------------------------------------------------------------------------

def _res(**kw):
    base = dict(exit_code=0, stdout="", stderr="", metric=None, timed_out=False)
    base.update(kw)
    return RunResult(**base)


def _stages(*rows):
    return [{"name": n, "status": s, "exit_code": 0, "seconds": 0.0} for n, s in rows]


@pytest.mark.parametrize("reason", sorted(ms.NEVER_SALVAGED_REASONS))
def test_the_three_refused_reasons_are_refused_before_anything_is_read(reason):
    """Each for its own argument (see `NEVER_SALVAGED_REASONS`): drift was ALREADY read and then
    discarded as uncorroborated, setup short-circuits before the eval's own work, and a hard deadline
    means the process was still working when it was killed."""
    res = _res(stages=_stages(("train", "expect_failed")), failed_stage="train")
    assert ms.salvage_condition(res, reason) is None


def test_the_v5_shape_is_salvageable_and_names_its_condition():
    res = _res(stages=_stages(("train", "expect_failed")), failed_stage="train")
    assert ms.salvage_condition(res, "no_metric") == "artifact_contract"


def test_a_failed_declared_assertion_is_never_salvaged_past():
    """`expect.files` is a claim about WHERE the stage wrote — it fails on a path typo, which is the
    v5 defect. `expect.assert` / `check` is a claim about WHAT it did: the half that caught
    "hard negatives for 9,364 of 764,676 queries". Salvaging past the second re-creates exactly the
    defect the contract exists to end, one field over."""
    res = _res(stages=_stages(("mine", "check_failed")), failed_stage="mine")
    assert ms.salvage_condition(res, "no_metric") is None


def test_a_crashed_stage_is_not_salvageable_unless_the_stall_watchdog_authenticated_it():
    """A non-zero exit means the process did not finish, so a metric line in its output is one it
    printed on the way somewhere — a per-epoch value reads identically to a final one. The single
    carve-out is the AUTHENTICATED stall verdict, which is evidence the process had stopped talking
    before it was killed."""
    crashed = _res(exit_code=2, stages=_stages(("train", "fail")), failed_stage="train")
    assert ms.salvage_condition(crashed, "crash") is None
    stalled = _res(exit_code=-9, stalled=True, stages=_stages(("train", "fail")),
                   failed_stage="train")
    assert ms.salvage_condition(stalled, "crash") == "stage_failed"


def test_an_unknown_future_stage_status_refuses_rather_than_inheriting_a_neighbours_answer():
    res = _res(stages=_stages(("train", "quarantined")), failed_stage="train")
    assert ms.salvage_condition(res, "no_metric") is None


def test_an_unrecognised_mode_settles_to_the_conservative_rung_never_the_permissive_one():
    """This is read off an engine attribute an operator, a Strategist or a resumed snapshot can set.
    A typo must not silently mean `select`."""
    assert ms.settle_mode("select") == "select"
    assert ms.settle_mode("off") == "off"
    for junk in ("SELECT", "on", "", None, 1, ["audit"]):
        assert ms.settle_mode(junk) == ms.DEFAULT_METRIC_SALVAGE == "audit"


def test_the_adapter_reader_is_refused_because_it_execs_agent_authored_code(tmp_path):
    """The same rule `run_command_eval` applies to the metrics/constraints gates and
    `validate_cross_check` to the drift reader. A salvage reader decides whether a FAILED node counts
    as successful, so it is the last place to relax it — and refusing it is also what lets salvage
    run with no docker `wrap` at all."""
    (tmp_path / "LOOPLAB_adapter.py").write_text(
        "def read_metric(wd):\n    return 0.99\n", encoding="utf-8")
    assert ms.read_salvageable_metric({"kind": "adapter"}, "", str(tmp_path), None) is None


def test_a_stale_file_from_an_earlier_attempt_is_not_this_attempts_result(tmp_path):
    """The workdir persists across repair attempts on purpose, which is exactly what makes a stale
    artifact plausible here."""
    (tmp_path / "metrics.json").write_text(json.dumps({"metric": 0.9}), encoding="utf-8")
    old = time.time() - 3600
    os.utime(tmp_path / "metrics.json", (old, old))
    spec = {"kind": "file_json", "path": "metrics.json", "key": "metric"}
    assert ms.read_salvageable_metric(spec, "", str(tmp_path), time.time() - 5) is None
    # …and with no freshness floor at all (a caller that has no start time) it reads.
    assert ms.read_salvageable_metric(spec, "", str(tmp_path), None)[0] == 0.9


def test_a_declared_file_reader_follows_the_near_miss_the_stage_actually_wrote(tmp_path):
    """RUNG 2. A run whose metric reader names a FILE has the same exposure to the v5 defect as one
    whose `expect.files` does: one wrong directory name. Still the operator's spec and the operator's
    basename, still inside the workdir, still fresh-only."""
    real = tmp_path / "vectorsearch/experiments/unified-baseline_rubert-tiny-lite_rubert-tiny-lite/eval"
    real.mkdir(parents=True)
    (real / "metrics.json").write_text(json.dumps({"Recall_at_100": V5_VALUE}), encoding="utf-8")
    spec = {"kind": "file_json",
            "path": "vectorsearch/experiments/unified-baseline_rubert-tiny-lite/eval/metrics.json",
            "key": "Recall_at_100"}
    value, source, reader, detail = ms.read_salvageable_metric(spec, "", str(tmp_path),
                                                               time.time() - 60)
    assert (value, source, reader) == (V5_VALUE, "relocated_file", "file_json")
    assert "metrics.json" in detail and "declared" in detail


def test_a_nonfinite_value_is_never_salvaged(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps({"metric": "NaN"}), encoding="utf-8")
    spec = {"kind": "file_json", "path": "m.json", "key": "metric"}
    assert ms.read_salvageable_metric(spec, "", str(tmp_path), None) is None


def test_the_provenance_record_and_the_enforcement_row_are_two_different_things():
    """The record says HOW; the violation row is what any of it costs. Under `select` the operator
    has accepted salvaged metrics as selectable, so only the record remains."""
    s = ms.SalvagedMetric(metric=V5_VALUE, condition="artifact_contract", source="declared_reader",
                          reader="stdout_regex", stage="score", producer=ms.OPERATOR_PRODUCED)
    assert s.as_event() == {"salvaged": True, "condition": "artifact_contract",
                            "source": "declared_reader", "reader": "stdout_regex", "stage": "score",
                            "producer": "operator_stage"}
    rows = s.violation_rows("audit")
    assert [r["name"] for r in rows] == ["metric_salvaged"]
    assert rows[0]["value"] == V5_VALUE and rows[0]["salvage"]["salvaged"] is True
    assert s.violation_rows("select") == []
    assert s.violation_rows("off") == []


# --------------------------------------------------------------------------------------------
# WHOSE BYTES the operator's reader was asked about — the trust question the record must answer
# --------------------------------------------------------------------------------------------

def test_the_record_names_who_wrote_the_output_the_reader_read():
    """The SPEC is the operator's; the OUTPUT is whatever the failing stage printed, and in
    Developer-manifest mode that is a script the AGENT wrote. `run_command_eval`'s primary read runs
    over the FINAL command's stdout (the protected `score` stage); the `expect_failed` early return
    sets stdout to the failing stage's."""
    operator = [{"name": "prep"}, {"name": "train"}]
    assert ms.stage_producer(operator, "train") == ms.OPERATOR_PRODUCED
    assert ms.stage_producer(operator, "score") == ms.OPERATOR_PRODUCED   # the protected stage
    assert ms.stage_producer(operator, "") == ms.OPERATOR_PRODUCED        # single-command eval
    # The Developer's own manifest stage, which is the v5 shape.
    assert ms.stage_producer(operator, "mine") == ms.AGENT_PRODUCED
    assert ms.stage_producer([], "train") == ms.AGENT_PRODUCED
    # Total over junk, and conservative in the direction that matters.
    assert ms.stage_producer(None, "train") == ms.AGENT_PRODUCED
    assert ms.stage_producer([{"nope": 1}, "prep"], "prep") == ms.OPERATOR_PRODUCED


def test_select_does_not_admit_a_number_the_agents_own_script_printed():
    """`select` means "a metric MY declared reader recovered may compete". It never meant "a number
    the Developer's training script printed may compete", which is what it silently bought whenever
    the failing stage came out of the agent's manifest — a Developer whose script prints
    `RECALL@100: 0.999` would have it admitted every time one of its own stages failed a contract.
    The exclusion is the same `metric_salvaged` row; only its trigger is wider."""
    agent = ms.SalvagedMetric(metric=0.999, condition="artifact_contract", source="declared_reader",
                              reader="stdout_regex", stage="train", producer=ms.AGENT_PRODUCED)
    assert [r["name"] for r in agent.violation_rows("select")] == ["metric_salvaged"]
    assert [r["name"] for r in agent.violation_rows("audit")] == ["metric_salvaged"]
    assert agent.violation_rows("off") == []
    # …and the row explains itself: the provenance rides along, producer included.
    assert agent.violation_rows("select")[0]["salvage"]["producer"] == "agent_stage"
    # An operator-declared stage under the same rung competes, which is the setting's whole point.
    operator = ms.SalvagedMetric(metric=0.999, condition="artifact_contract",
                                 source="declared_reader", reader="stdout_regex", stage="score",
                                 producer=ms.OPERATOR_PRODUCED)
    assert operator.violation_rows("select") == []
    # An unspecified producer is treated as the agent's, never as the operator's.
    assert ms.SalvagedMetric(metric=1.0, condition="eval_failed", source="declared_reader",
                             reader="stdout_regex").producer == ms.AGENT_PRODUCED


def test_off_salvages_nothing_at_all(tmp_path):
    res = _res(stdout="RECALL@100: 0.743250\n", stages=_stages(("train", "expect_failed")),
               failed_stage="train")
    assert ms.salvage(res, "no_metric", V5_METRIC, str(tmp_path), None, mode="off") is None
    assert ms.salvage(res, "no_metric", V5_METRIC, str(tmp_path), None).metric == V5_VALUE


# --------------------------------------------------------------------------------------------
# the property, driven through the real engine
# --------------------------------------------------------------------------------------------

class _Dev:
    """A Developer whose `repair` fixes the DECLARATION (a node file), like the real repo one does —
    a multi-file repair returns "" and carries its work in `last_files`."""

    def __init__(self, fixed_manifest=None):
        self.fixed_manifest = fixed_manifest
        self.last_files: dict = {}
        self.last_deleted: list = []
        self.errors: list[str] = []

    def implement(self, idea):
        return "print('unused')\n"

    def repair(self, idea, code, error):
        self.errors.append(error)
        if self.fixed_manifest is None:
            return code
        self.last_files = {"looplab_stages.json": self.fixed_manifest}
        return ""


class _Researcher:
    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    def triage_crash(self, node, error, attempt, **kw):
        return {"action": "abandon", "rationale": "no"}


def _manifest(declared):
    """A one-stage manifest shaped like node 0's: a `train` stage that runs the scorer itself and
    prints the metric, then declares an artifact at `declared`."""
    return json.dumps({"stages": [{
        "name": "train",
        "command": ["python", "-c", "print('RECALL@100: 0.743250')"],
        "timeout": 120.0,
        "expect": {"files": [declared]},
    }]})


def _drive(tmp_path, *, manifest, dev=None, name="run", salvage=None, cause_repair=True,
           eval_spec=None, **kw):
    """Seed one node with a stage manifest and run the REAL `_evaluate` over it.

    `salvage`/`cause_repair` go in as `Engine(...)` KEYWORDS, which is the whole
    `Settings -> EngineOptions -> settle_mode -> Engine` chain this feature's policy actually
    travels. They used to be assigned as attributes AFTER construction, and that made every case in
    this file — 29 of them — pass with the wiring commented out: `Engine(metric_salvage="off")`
    silently yielded `audit` and nothing went red, because no test ever asked the constructor for a
    mode. `tests/test_engine_options.py::test_the_salvage_policy_reaches_the_engine_*` drives the
    same chain from `Settings`; this keeps the end-to-end cases honest about where the value came
    from.
    """
    run_dir = tmp_path / name
    dev = dev if dev is not None else _Dev()
    kw.setdefault("auto_install_deps", False)
    kw.setdefault("inline_repair", True)
    if salvage is not None:
        kw["metric_salvage"] = salvage
    kw["metric_salvage_repair"] = cause_repair
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=_Researcher(), developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)
    # A command-eval run: the operator's `cmd` is the protected final `score` stage and the
    # Developer's `looplab_stages.json` supplies the preceding ones — exactly v5's shape.
    eng._eval_spec = eval_spec or {"command": ["python", "-c", "print('score stage never runs')"],
                                   "cwd": ".", "metric": dict(V5_METRIC), "timeout": 120.0}
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": "print('unused')\n",
        "files": {"looplab_stages.json": manifest}})

    async def _bounded() -> bool:
        with anyio.move_on_after(300) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the eval did not terminate"
    return list(EventStore(run_dir / "events.jsonl").read_all()), eng, dev


def _terminals(evs):
    return [e for e in evs if e.type in ("node_evaluated", "node_failed")
            and e.data.get("node_id") == 0]


def test_the_v5_node_comes_out_evaluated_with_the_metric_it_had_already_computed(tmp_path):
    """THE STANDARD. A real node whose `train` stage exits 0, prints `RECALL@100: 0.743250` and then
    fails its declared artifact contract: today that is one `node_failed` with `reason: no_metric`
    and 76 GPU-minutes on the floor. It must now be ONE `node_evaluated` carrying that number."""
    evs, eng, _dev = _drive(tmp_path, manifest=_manifest(DECLARED), cause_repair=False)
    terminals = _terminals(evs)
    assert len(terminals) == 1, "invariant #2: exactly one terminal per node"
    assert terminals[0].type == "node_evaluated"
    assert terminals[0].data["metric"] == pytest.approx(V5_VALUE)
    # The stage record still says the contract failed — salvage recovers the metric, it does not
    # rewrite what happened.
    rows = [e for e in evs if e.type == "stage_finished" and e.data.get("node_id") == 0]
    assert [r.data["status"] for r in rows] == ["expect_failed"]
    # The failure the salvage overrode, kept verbatim — INSIDE `metric_provenance`, which is the
    # folded field. As a top-level `salvaged_error` key the fold ignored it, so the one reader it
    # was written for (a replayed RunState: the UI, the report, `looplab replay`) never saw it.
    assert "did NOT produce its declared artifact" in (
        terminals[0].data["metric_provenance"]["salvaged_error"])
    assert "salvaged_error" not in terminals[0].data, "a key the fold ignores is a key nobody reads"
    assert "did NOT produce its declared artifact" in (
        fold(evs).nodes[0].metric_provenance["salvaged_error"])


def test_the_salvaged_metric_is_marked_as_salvaged_and_says_which_reader_found_it(tmp_path):
    evs, _eng, _dev = _drive(tmp_path, manifest=_manifest(DECLARED), cause_repair=False)
    prov = _terminals(evs)[0].data["metric_provenance"]
    assert prov["salvaged"] is True
    assert prov["source"] == "declared_reader"      # not a model, not a widened spec
    assert prov["reader"] == "stdout_regex"         # the operator's own kind
    assert prov["condition"] == "artifact_contract"
    assert prov["stage"] == "train"


def test_the_selection_path_can_tell_a_salvaged_metric_from_a_measured_one(tmp_path):
    """`metric_provenance` alone would satisfy "CAN tell" and not "does": nothing on the selection
    path reads an unknown event key. The `metric_salvaged` violation row is the enforcement — the
    fold's rule is `feasible = not violations`, and `feasible_nodes()` is what champion selection and
    breeding read."""
    evs, _eng, _dev = _drive(tmp_path, manifest=_manifest(DECLARED), cause_repair=False)
    st = fold(evs)
    node = st.nodes[0]
    assert node.status is NodeStatus.evaluated       # it COUNTS: budget, UI, digest, lineage
    assert node.metric == pytest.approx(V5_VALUE)
    assert [v["name"] for v in node.violations] == ["metric_salvaged"]
    assert node.feasible is False
    assert node in st.evaluated_nodes() and node not in st.feasible_nodes()
    assert st.breedable_nodes() == []


def test_select_mode_lets_the_operator_accept_a_salvaged_metric_as_first_class(tmp_path):
    """Same run, one setting different: the provenance survives, the exclusion does not.

    The stage is one the OPERATOR declared, which is what `select` can speak for — see
    `test_select_never_admits_the_agents_own_stage_end_to_end` for the same run with the same
    setting over a Developer-manifest stage."""
    # The operator's OWN pipeline (`cmd.stages`), which wins over the Developer's manifest — the
    # same `train` stage, declared by the person who owns the scoring path.
    spec = {"command": ["python", "-c", "print('score stage never runs')"], "cwd": ".",
            "metric": dict(V5_METRIC), "timeout": 120.0,
            "stages": json.loads(_manifest(DECLARED))["stages"]}
    evs, _eng, _dev = _drive(tmp_path, manifest=_manifest(DECLARED), name="sel",
                             salvage="select", cause_repair=False, eval_spec=spec)
    st = fold(evs)
    assert st.nodes[0].metric == pytest.approx(V5_VALUE)
    assert st.nodes[0].violations == [] and st.nodes[0].feasible is True
    assert st.nodes[0] in st.feasible_nodes()
    assert _terminals(evs)[0].data["metric_provenance"]["salvaged"] is True
    assert _terminals(evs)[0].data["metric_provenance"]["producer"] == "operator_stage"


def test_select_never_admits_the_agents_own_stage_end_to_end(tmp_path):
    """The v5 shape under `select`: the number was printed by the Developer's `train` script, so the
    operator's opt-in does not reach it and the node stays out of champion selection."""
    evs, _eng, _dev = _drive(tmp_path, manifest=_manifest(DECLARED), name="sel-agent",
                             salvage="select", cause_repair=False)
    st = fold(evs)
    assert st.nodes[0].metric == pytest.approx(V5_VALUE)     # still recovered, still counted
    assert [v["name"] for v in st.nodes[0].violations] == ["metric_salvaged"]
    assert st.nodes[0].feasible is False and st.nodes[0] not in st.feasible_nodes()
    assert _terminals(evs)[0].data["metric_provenance"]["producer"] == "agent_stage"


def test_off_restores_the_old_behaviour_exactly(tmp_path):
    evs, _eng, _dev = _drive(tmp_path, manifest=_manifest(DECLARED), name="off",
                             salvage="off", cause_repair=False, inline_repair=False)
    terminals = _terminals(evs)
    assert len(terminals) == 1 and terminals[0].type == "node_failed"
    # The v5 outcome, verbatim — except for the reason, which is now its own name. A contract
    # failure is no longer reported as "the command printed no metric" about a stage that
    # printed one (`core/models.py::FAILURE_REASONS`).
    assert terminals[0].data["reason"] == "expect_failed"


def test_a_node_that_really_produced_nothing_still_fails(tmp_path):
    """The deterministic reader IS the discriminator between "the declaration names the wrong path"
    and "the stage never did the work": a train that writes no checkpoint never ran the scorer, so
    there is no fresh value to find and salvage abstains on its own."""
    manifest = json.dumps({"stages": [{
        "name": "train", "command": ["python", "-c", "print('starting')"],
        "timeout": 120.0, "expect": {"files": [DECLARED]}}]})
    evs, _eng, _dev = _drive(tmp_path, manifest=manifest, cause_repair=False)
    terminals = _terminals(evs)
    assert len(terminals) == 1 and terminals[0].type == "node_failed"


def test_a_reader_that_raises_costs_the_salvage_and_never_the_run(tmp_path):
    """`command_eval._read_host_score` RAISES `ValueError` when the operator's held-out labels sit
    inside the workspace, and that raise escapes `read_metric`, `run_command_eval` and `_run_eval` —
    measured. `engine/evaluate.py`'s only handler around the eval worker is
    `except GpuPinUnenforceable`, so on the PRIMARY read path that misconfiguration gives the node no
    terminal event and the run re-dies on every resume (an audit finding of this change, fixed in the
    write-up diff for `command_eval.py`). The salvage read walks the candidate's own workdir, so it
    must never be able to add a second instance of that failure class."""
    lbl = tmp_path / "labels.json"
    lbl.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    class _Eng:
        _eval_spec = {"metric": {"kind": "host_score", "predictions": "p.json",
                                 "labels": str(lbl), "scorer": "rmse"}}
        metric_salvage = "audit"

    from looplab.engine.evaluate import EvaluateMixin
    res = _res(stages=_stages(("train", "expect_failed")), failed_stage="train")
    # The reader answers "nothing readable" over this spec rather than raising — the guard that used
    # to raise out of `read_metric`, `run_command_eval` AND `_run_eval` (leaving the node with no
    # terminal and the run re-dying on every resume) now logs and returns None. Kept as an assertion
    # because the containment below must hold WHICHEVER way that guard goes.
    assert ms.read_salvageable_metric(_Eng._eval_spec["metric"], "", str(tmp_path), None) is None
    # …and the engine seam agrees: "nothing to salvage", not a dead run.
    assert EvaluateMixin._salvage_eval_metric(_Eng(), res, "no_metric", tmp_path, None) is None


def test_the_cause_is_repaired_in_the_same_breath_and_without_a_second_evaluation(tmp_path):
    """The operator asked for both. A salvaged node that kept its broken manifest would walk into
    the identical failure on its next attempt, having learnt nothing — so the Developer is asked to
    fix the DECLARATION, the fix is committed through the ordinary `node_repaired` event, and the
    node terminalizes on the metric it already has rather than paying for another evaluation."""
    fixed = _manifest(ACTUAL)
    evs, eng, dev = _drive(tmp_path, manifest=_manifest(DECLARED), dev=_Dev(fixed_manifest=fixed))
    terminals = _terminals(evs)
    assert len(terminals) == 1 and terminals[0].type == "node_evaluated", "still ONE terminal"
    assert terminals[0].data["metric"] == pytest.approx(V5_VALUE)
    assert terminals[0].data["metric_provenance"]["cause_repaired"] is True
    repairs = [e for e in evs if e.type == "node_repaired" and e.data.get("node_id") == 0]
    assert len(repairs) == 1, "exactly one cause fix, and no repair-and-re-evaluate loop"
    assert repairs[0].data["triage_action"] == "salvage_cause_fix"
    assert repairs[0].data["changed"] == ["looplab_stages.json"]
    # The repair prompt must say the experiment SUCCEEDED — the ordinary repair prompt would send
    # the model at the training code, which is the one thing it must not touch after measurement.
    assert "do NOT re-run" in dev.errors[0]
    assert "0.74325" in dev.errors[0]
    # …and only ONE evaluation was ever run: the stage ran once.
    rows = [e for e in evs if e.type == "stage_finished" and e.data.get("node_id") == 0]
    assert len(rows) == 1
    # The fix is DURABLE — the folded node and the workdir both carry the corrected manifest, so the
    # node's next attempt does not re-hit the same wall.
    node = fold(evs).nodes[0]
    assert node.files["looplab_stages.json"] == fixed
    assert (eng.run_dir / "nodes" / "node_0" / "looplab_stages.json").read_text() == fixed
    # …and it did NOT touch the experiment the metric describes. A multi-file repair returns "" for
    # the whole-file artifact, and the attempt loop's unconditional `"code": new_code` would blank
    # the node's code through `replay._on_node_repaired`'s `d.get("code", n.code)`.
    assert node.code == "print('unused')\n"


def test_a_dead_provider_during_the_cause_fix_never_costs_the_salvaged_metric(tmp_path):
    """The cause fix is best-effort by construction: it runs after the metric is in hand and before
    the terminal, so a repair call that fails must lose the fix, never the number."""

    class _DeadDev(_Dev):
        def repair(self, idea, code, error):
            raise RuntimeError("402 out of credits")

    evs, _eng, _dev = _drive(tmp_path, manifest=_manifest(DECLARED), dev=_DeadDev())
    terminals = _terminals(evs)
    assert len(terminals) == 1 and terminals[0].type == "node_evaluated"
    assert terminals[0].data["metric"] == pytest.approx(V5_VALUE)
    assert terminals[0].data["metric_provenance"]["cause_repaired"] is False
    assert [e for e in evs if e.type == "node_repaired"] == []
    assert [e for e in evs if e.type == "pause"] == [], "a cause fix must not pause the run"


# --------------------------------------------------------------------------------------------
# THE CAUSE FIX'S OWN BOUNDS — it claimed the inline-repair loop's and honoured none of them
# --------------------------------------------------------------------------------------------

class _CountingDev(_Dev):
    """Counts the Developer calls, which is the unit the bounds below are denominated in."""

    def __init__(self, fixed_manifest=None):
        super().__init__(fixed_manifest)
        self.calls = 0

    def repair(self, idea, code, error):
        self.calls += 1
        return super().repair(idea, code, error)


def test_a_narrowed_inline_repair_reasons_is_honoured_by_the_cause_fix(tmp_path):
    """`inline_repair_reasons` is the operator's answer to "may this failure class buy a Developer
    call at all". A run narrowed to `("crash",)` still bought one for an `expect_failed` node,
    because the cause fix checked the feature flag and nothing else — while its docstring claimed
    "every bound is the inline-repair loop's own"."""
    dev = _CountingDev(fixed_manifest=_manifest(ACTUAL))
    evs, _eng, _dev = _drive(tmp_path, manifest=_manifest(DECLARED), dev=dev,
                             inline_repair_reasons=("crash",))
    terminals = _terminals(evs)
    assert len(terminals) == 1 and terminals[0].type == "node_evaluated"
    assert terminals[0].data["metric"] == pytest.approx(V5_VALUE)     # the metric is still salvaged
    assert dev.calls == 0, "an excluded reason must not buy a Developer call"
    assert [e for e in evs if e.type == "node_repaired"] == []
    assert terminals[0].data["metric_provenance"]["cause_repaired"] is False


def test_a_budget_stop_during_the_cause_fix_still_leaves_the_node_a_terminal(tmp_path):
    """THE RUN-KILLER in the optional half. `_evaluate`'s callers wrap it in try/FINALLY, not except
    (`orchestrator.py`'s dispatchers), so a `BudgetExceeded` raised out of this best-effort fix left
    the node with NO terminal event at all — discarding the metric the salvage had just recovered
    and re-dying on every resume. A budget that has run out means stop spending; returning here
    spends nothing."""
    from looplab.core.errors import BudgetExceeded

    class _BrokeDev(_CountingDev):
        def repair(self, idea, code, error):
            self.calls += 1
            raise BudgetExceeded("run budget exhausted")

    dev = _BrokeDev()
    evs, _eng, _dev = _drive(tmp_path, manifest=_manifest(DECLARED), dev=dev)
    terminals = _terminals(evs)
    assert dev.calls == 1, "the fix was attempted (else this proves nothing)"
    assert len(terminals) == 1 and terminals[0].type == "node_evaluated"
    assert terminals[0].data["metric"] == pytest.approx(V5_VALUE)
    assert terminals[0].data["metric_provenance"]["cause_repaired"] is False


def test_a_resume_after_the_fix_landed_does_not_pay_for_it_twice(tmp_path):
    """INVARIANT #3, and the window is completely ordinary: the `node_repaired` row is appended,
    the loop breaks, and the terminal is written under a later lock acquisition. A process that dies
    in between left the fix committed and no terminal — and the resume re-evaluated, re-salvaged and
    bought a SECOND identical Developer call. Nothing read the row back."""
    dev = _CountingDev(fixed_manifest=_manifest(ACTUAL))
    run_dir = tmp_path / "resumed"
    run_dir.mkdir(parents=True)
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": "print('unused')\n",
        "files": {"looplab_stages.json": _manifest(DECLARED)}})
    # …the durable row a crashed process left behind.
    store.append("node_repaired", {
        "node_id": 0, "generation": 0, "attempt": 0, "files": {}, "deleted": [],
        "error_in": "stage 'train' failed its declared artifact contract",
        "triage_action": ms.SALVAGE_CAUSE_TRIAGE_ACTION, "rationale": "metric salvaged",
        "changed": ["looplab_stages.json"], "stages_passed": None, "salvaged_metric": V5_VALUE})

    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=_Researcher(), developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True)
    eng._eval_spec = {"command": ["python", "-c", "print('score stage never runs')"],
                      "cwd": ".", "metric": dict(V5_METRIC), "timeout": 120.0}

    async def _bounded() -> bool:
        with anyio.move_on_after(300) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the eval did not terminate"
    evs = list(EventStore(run_dir / "events.jsonl").read_all())
    assert dev.calls == 0, "the fix had already landed; a resume must not re-buy it"
    assert len([e for e in evs if e.type == "node_repaired"]) == 1
    terminals = _terminals(evs)
    assert len(terminals) == 1 and terminals[0].type == "node_evaluated"
    # …and the terminal still reports the fix as landed, because it did.
    assert terminals[0].data["metric_provenance"]["cause_repaired"] is True


def test_the_cause_fix_does_not_spend_one_of_the_nodes_repair_attempts(tmp_path):
    """The ledger charges what a resume must not refund, and a cause fix is not in that class: it
    re-evaluated nothing, so charging it takes a repair away from a resumed node that the same node
    in-process never lost. The ROW still reaches the judge history — "the declaration was corrected
    here" is real evidence about the trajectory."""
    from looplab.engine.evaluate import _durable_repair_ledger

    class _E:
        def __init__(self, type_, data):
            self.type, self.data = type_, data

    events = [
        _E("node_repaired", {"node_id": 0, "generation": 0, "attempt": 1, "error_in": "boom",
                             "rationale": "fix the import", "triage_action": "repair"}),
        _E("node_repaired", {"node_id": 0, "generation": 0, "attempt": 2, "error_in": "contract",
                             "rationale": "metric salvaged; fixing the declaration",
                             "triage_action": ms.SALVAGE_CAUSE_TRIAGE_ACTION}),
    ]
    attempts, rows, _unparseable = _durable_repair_ledger(events, 0, 0)
    assert attempts == 1, "only the repair that bought a re-evaluation is charged"
    assert len(rows) == 2, "the judge still sees the correction"


# --------------------------------------------------------------------------------------------
# THE GATES THAT QUALIFY A METRIC — skipped entirely for a salvaged node
# --------------------------------------------------------------------------------------------

def _operator_spec(**extra):
    spec = {"command": ["python", "-c", "print('score stage never runs')"], "cwd": ".",
            "metric": dict(V5_METRIC), "timeout": 120.0,
            "stages": json.loads(_manifest(DECLARED))["stages"]}
    spec.update(extra)
    return spec


def test_the_operators_constraints_are_applied_to_a_salvaged_metric(tmp_path):
    """`run_command_eval` reads the constraints in its TAIL, which every early return that produces
    a salvageable failure skips — so `res.violations` was EMPTY for every salvaged node. Under
    `select` (advertised as "competes on equal terms") that read as "no bound was breached" when the
    truth was "no bound was ever evaluated", and such a node could become champion."""
    spec = _operator_spec(constraints=[{"name": "latency_ms", "kind": "stdout_regex",
                                        "pattern": "LATENCY: ([0-9.]+)", "max": 100.0}])
    evs, _eng, _dev = _drive(tmp_path, manifest=_manifest(DECLARED), name="cons",
                             salvage="select", cause_repair=False, eval_spec=spec)
    st = fold(evs)
    node = st.nodes[0]
    assert node.metric == pytest.approx(V5_VALUE)
    # The stage never printed a latency, so the bound is unverifiable — which `_violations` counts
    # as a violation ("never a silent pass"), exactly as it would for a measured metric.
    assert [v["name"] for v in node.violations] == ["latency_ms"]
    assert node.feasible is False and node not in st.feasible_nodes()


def test_a_salvaged_metric_the_drift_reader_cannot_corroborate_is_refused(tmp_path):
    """`drift` is in NEVER_SALVAGED_REASONS because re-admitting a metric the trust gate discarded is
    worse than losing it. The cross-check never ran on the salvage path at all, so the same value
    the gate would have thrown away was admitted one step later."""
    spec = _operator_spec(cross_check={"kind": "stdout_regex", "pattern": "CROSS: ([0-9.]+)"})
    evs, _eng, _dev = _drive(tmp_path, manifest=_manifest(DECLARED), name="drift",
                             salvage="select", cause_repair=False, eval_spec=spec,
                             eval_trust_mode="ratify_freeze_drift", inline_repair=False)
    terminals = _terminals(evs)
    assert len(terminals) == 1 and terminals[0].type == "node_failed"
    assert "could not corroborate" in terminals[0].data["error"]
    assert [e.data["primary"] for e in evs if e.type == "spec_drift"] == [pytest.approx(V5_VALUE)]
    assert fold(evs).nodes[0].metric is None


def test_the_extra_readers_still_report_on_a_salvaged_node(tmp_path):
    """The same tail carries the audit-only `metrics` readers. They are not a gate, so a missing one
    is simply absent — but a present one must not vanish because the eval failed."""
    spec = _operator_spec(metrics={"recall": {"kind": "stdout_regex",
                                              "pattern": "RECALL@100: ([0-9.]+)"}})
    evs, _eng, _dev = _drive(tmp_path, manifest=_manifest(DECLARED), name="extra",
                             salvage="select", cause_repair=False, eval_spec=spec)
    assert fold(evs).nodes[0].extra_metrics == {"recall": pytest.approx(V5_VALUE)}


def test_a_constraint_reader_that_execs_agent_code_is_never_run(tmp_path):
    """`run_command_eval` REFUSES an `adapter` in metrics/constraints — in its tail, which a salvaged
    eval never reaches. Skipping it here would be a silent pass on an operator's hard bound;
    executing it would be the trust boundary. It counts as unverifiable."""
    (tmp_path / "LOOPLAB_adapter.py").write_text("raise SystemExit('EXECUTED')\n", encoding="utf-8")
    gates = ms.salvage_gates(
        {"constraints": [{"name": "budget", "kind": "adapter", "path": "LOOPLAB_adapter.py",
                          "max": 1.0}]},
        0.5, "", str(tmp_path), None)
    assert [v["name"] for v in gates["violations"]] == ["budget"]
    assert "adapter" in gates["violations"][0]["unverifiable"]


# --------------------------------------------------------------------------------------------
# the relocation scan, when the workdir holds more than one candidate
# --------------------------------------------------------------------------------------------

def test_two_fresh_same_basename_files_refuse_the_relocation_rather_than_pick_one(tmp_path):
    """`os.walk` yields directories in FILESYSTEM order, so "the first match" is not a rule — the
    same workdir can answer differently on two reads. The diagnostic half can show a human the
    nearest miss; a salvage promotes the NUMBER inside the file it names, so it abstains."""
    for sub, value in (("a", 0.1), ("b", 0.9)):
        d = tmp_path / "experiments" / sub
        d.mkdir(parents=True)
        (d / "metrics.json").write_text(json.dumps({"metric": value}), encoding="utf-8")
    spec = {"kind": "file_json", "path": "experiments/declared/metrics.json", "key": "metric"}
    assert ms.read_salvageable_metric(spec, "", str(tmp_path), time.time() - 60) is None
    # One candidate is unambiguous and still salvages — the rule is about ambiguity, not about
    # having refused the rung.
    import shutil
    shutil.rmtree(tmp_path / "experiments" / "b")
    value, source, _reader, _detail = ms.read_salvageable_metric(spec, "", str(tmp_path),
                                                                time.time() - 60)
    assert (value, source) == (0.1, "relocated_file")


class _NoOpDev(_Dev):
    """Answers the repair call and proposes NOTHING — returning the node's own manifest verbatim.

    Deliberately not an empty reply: `_repair_provider_failure` reads empty-everything as the
    Developer's SESSION having failed (dead endpoint, 401, 402), which must not be receipted because
    a resume should retry it. This is the other shape — the provider answered, billed, and proposed
    no change — which is the one that owed a receipt and had none.
    """

    def __init__(self, unchanged: str):
        super().__init__()
        self.unchanged = unchanged

    def repair(self, idea, code, error):
        self.errors.append(error)
        self.last_files = {"looplab_stages.json": self.unchanged}
        self.last_deleted = []
        return ""


def test_a_cause_fix_that_changed_nothing_is_still_receipted_so_a_resume_cannot_re_buy_it(tmp_path):
    """The paid call is the side effect, not the edit it happened to produce.

    `_repair_salvaged_cause` is documented as ONCE PER LIFECYCLE, gated on its own durable
    `node_repaired` row (invariant #3). But the row was appended only when the repair produced
    something committable, so the branch where the model ANSWERS and proposes no change spent a
    provider call and left nothing durable — and a process dying in the ordinary window before the
    terminal made the resume re-evaluate, re-salvage and buy the identical call again, with nothing
    in folded state to bound the repeat across successive crashes.

    Driven end to end over the real `_evaluate`: a Developer that answers with no change, then the
    gate the resume actually consults, read off the real event log.
    """
    from looplab.engine.evaluate import _durable_salvage_cause_fix
    from looplab.engine.metric_salvage import SALVAGE_CAUSE_TRIAGE_ACTION

    dev = _NoOpDev(_manifest(DECLARED))
    evs, _eng, dev = _drive(
        tmp_path, manifest=_manifest(DECLARED), dev=dev, name="noop")

    assert len(dev.errors) == 1, "the cause fix was asked for exactly once"
    rows = [e for e in evs if e.type == "node_repaired"
            and (e.data or {}).get("triage_action") == SALVAGE_CAUSE_TRIAGE_ACTION]
    assert len(rows) == 1, "a billed call that produced no change still owes a durable receipt"
    row = rows[0].data
    assert row["changed"] == [] and row["files"] == {} and row["deleted"] == []
    # Fold-neutral: no `code` key, so `replay._on_node_repaired`'s `d.get("code", n.code)` leaves the
    # node's artifact alone. A receipt for the spend must not assert an edit.
    assert "code" not in row

    # What the RESUME sees. The gate returns the row, and it must not read back as a correction —
    # `metric_provenance.cause_repaired` would otherwise claim a fix nobody made.
    prior = _durable_salvage_cause_fix(evs, 0, row["generation"])
    assert prior is not None, "the resume gate has nothing to read, so it would re-buy the call"
    assert not (prior.get("changed") or prior.get("code") or prior.get("files")
                or prior.get("deleted"))
