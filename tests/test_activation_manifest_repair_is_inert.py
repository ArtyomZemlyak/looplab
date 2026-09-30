"""A repair that changed ONLY the activation manifest is `inert` — unless the activation check is what
failed (doc 69 §4.1, 69.10a).

`minionerec-backbones-v10` node 12, seq 4363: `changed: ["looplab_activation.json"]`,
`edit_calls: 0`. The repair session wrote nothing but its markers again, the verdict said a file
moved, and a full canary was bought for code that had just failed. The manifest is read by the engine
AFTER an evaluation that otherwise succeeded (`engine/activation.py`) and by nothing the pipeline runs,
so for every failure but `inert_path` it moves nothing the next evaluation executes. For `inert_path`
the same edit is the directive's own ask ("If the change is meant to be conditional, declare no
marker for it"), so there it stays a change.

Drives the REAL inline-repair loop over a real sandbox, as `tests/test_repair_verification.py` does.
"""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Idea, NodeStatus
from looplab.engine.activation import ACTIVATION_MANIFEST_NAME, manifest_text
from looplab.engine.crash_repair import _format_repair_log
from looplab.engine.orchestrator import Engine
from looplab.engine.repair_verify import (INERT_REPAIR_LIMIT, REPAIR_INERT, REPAIR_UNSTATED,
                                          verify_repair)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"

_BAD = "import definitely_not_a_real_module_zzz\nprint('x')\n"
_GOOD = "import json; print(json.dumps({'metric': 0.1}))\n"


# ------------------------------------------------------------------------------ the rule
def test_a_manifest_only_change_is_inert_for_every_failure_but_the_activation_check():
    for reason in ("crash", "timeout", "canary_timeout", "no_metric", "expect_failed"):
        v = verify_repair("fix it", changed={ACTIVATION_MANIFEST_NAME}, engine_reason=reason)
        assert v.verdict == REPAIR_INERT, reason
        v = verify_repair("fix it", changed=(), deleted=[ACTIVATION_MANIFEST_NAME],
                          engine_reason=reason)
        assert v.verdict == REPAIR_INERT, ("a deleted manifest runs nothing either", reason)
    # The one failure the manifest decides: re-declaring the markers is the fix it asked for.
    v = verify_repair("fix it", changed={ACTIVATION_MANIFEST_NAME}, engine_reason="inert_path")
    assert v.verdict == REPAIR_UNSTATED


def test_anything_else_that_moved_is_still_a_change():
    both = {ACTIVATION_MANIFEST_NAME, "train.py"}
    assert verify_repair("fix it", changed=both, engine_reason="crash").verdict != REPAIR_INERT
    assert verify_repair("fix it", changed={ACTIVATION_MANIFEST_NAME}, code_changed=True,
                         engine_reason="crash").verdict != REPAIR_INERT
    # Named by path, not by suffix: a stage's own JSON config is an input the pipeline reads.
    assert verify_repair("fix it", changed={"sub/looplab_activation.json"},
                         engine_reason="crash").verdict != REPAIR_INERT


def test_an_unknown_reason_keeps_the_old_verdict():
    """The loop ACTS on `inert` (two in a row end the node), so a caller that cannot say what failed
    gets the historical reading, in which every moved file is a change. MUTATION: exempt on an
    unknown reason -> this row turns inert."""
    assert verify_repair("fix it", changed={ACTIVATION_MANIFEST_NAME}).verdict == REPAIR_UNSTATED


def test_the_judge_is_told_what_moved_and_why_it_does_not_count():
    """Prompt text is a contract: a pure no-op row keeps its sentence byte for byte; the new row — a
    shape no log carried before — gets its own, which does not claim "no file at all"."""
    row = dict(attempt=1, error="boom", fix="f", verified=REPAIR_INERT)
    manifest_only = _format_repair_log([{**row, "changed": [ACTIVATION_MANIFEST_NAME]}])
    assert "changed only the activation manifest" in manifest_only
    assert "no file at all" not in manifest_only
    nothing = _format_repair_log([{**row, "changed": []}])
    assert ("THE ENGINE COMPARED THE BYTES: this attempt changed no file at all, so the evaluation "
            "after it re-ran inputs identical to the one before it.") in nothing


# ------------------------------------------------------------------------------ driven
class _Judge:
    def __init__(self):
        self.histories: list[str] = []

    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    def triage_crash(self, node, error, attempt, *, state=None, brief="", history="",
                     stages_passed=None, attempts_left=None):
        self.histories.append(history or "")
        return {"action": "repair", "rationale": "fix it"}


class _ManifestOnlyDev:
    """Every repair hands back the code it was given and a NEW activation manifest — node 12's
    second repair, which declared its markers again and edited nothing."""

    def __init__(self, markers=None):
        self.repair_calls = 0
        self.markers = markers
        self.last_files: dict = {}
        self.last_deleted: list = []

    def implement(self, idea):
        return _BAD

    def repair(self, idea, code, error):
        self.repair_calls += 1
        marks = self.markers or [f"new path on, attempt {self.repair_calls}"]
        self.last_files = {ACTIVATION_MANIFEST_NAME: manifest_text(marks)}
        return code


def _drive(run_dir, dev, judge, *, code, files=None, **kw):
    kw.setdefault("auto_install_deps", False)
    kw.setdefault("inline_repair", True)
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=judge, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": code, **({"files": dict(files)} if files else {})})

    async def _bounded() -> bool:
        with anyio.move_on_after(180) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the inline-repair loop did not terminate"
    return list(EventStore(Path(run_dir) / "events.jsonl").read_all())


def _repairs(evs):
    return [e for e in evs if e.type == "node_repaired" and e.data.get("node_id") == 0]


def _terminals(evs):
    return [e for e in evs if e.type in ("node_evaluated", "node_failed")
            and e.data.get("node_id") == 0]


def test_a_chain_of_manifest_only_repairs_ends_at_the_inert_limit(tmp_path):
    """Before: each row read `changed: ["looplab_activation.json"]` and an unmoved verdict, so the
    chain ran to the operator's cap — every link a full evaluation of the code that just crashed."""
    dev, judge = _ManifestOnlyDev(), _Judge()
    evs = _drive(tmp_path / "chain", dev, judge, code=_BAD, inline_repair_attempts=8)
    rows = _repairs(evs)
    assert len(rows) == INERT_REPAIR_LIMIT, [r.data.get("verified") for r in rows]
    assert all(r.data["verified"] == REPAIR_INERT for r in rows)
    assert all(r.data["changed"] == [ACTIVATION_MANIFEST_NAME] for r in rows), \
        "the column still says what moved; the verdict says it runs nothing"
    (terminal,) = _terminals(evs)
    assert terminal.type == "node_failed"
    rationale = terminal.data["triage_rationale"]
    assert "activation manifest" in rationale and "byte-identical" not in rationale
    assert fold(evs).nodes[0].status is NodeStatus.failed
    # The judge read the engine's sentence for the first inert row before the second repair.
    assert "changed only the activation manifest" in judge.histories[-1]


def test_re_declaring_the_markers_is_the_fix_when_the_markers_are_what_failed(tmp_path):
    """The control: the node's code runs and scores, but its declared marker never printed
    (`inert_path`). A repair that re-declares a marker the code does print moved exactly what
    failed — it is not inert, and the node ends EVALUATED."""
    dev, judge = _ManifestOnlyDev(markers=["metric"]), _Judge()
    evs = _drive(tmp_path / "inert_path", dev, judge, code=_GOOD,
                 files={ACTIVATION_MANIFEST_NAME: manifest_text(["a marker nothing prints"])},
                 inline_repair_attempts=8)
    rows = _repairs(evs)
    assert len(rows) == 1 and rows[0].data["verified"] != REPAIR_INERT
    (terminal,) = _terminals(evs)
    assert terminal.type == "node_evaluated"


class _ManifestThenNothingDev(_ManifestOnlyDev):
    """Repair 1 re-declares the markers; repair 2 hands back exactly what the node already carries."""

    def repair(self, idea, code, error):
        self.repair_calls += 1
        self.last_files = {ACTIVATION_MANIFEST_NAME: manifest_text(["new path on"])}
        return code


def test_a_mixed_streak_is_described_by_every_row_in_it(tmp_path):
    """The abandon sentence reads the WHOLE inert streak, not its last row: here the last repair
    moved nothing at all, and "byte-identical" would still be false of the streak, because the
    first one rewrote the manifest. MUTATION: read only the last row -> the historical sentence."""
    dev, judge = _ManifestThenNothingDev(), _Judge()
    evs = _drive(tmp_path / "mixed", dev, judge, code=_BAD, inline_repair_attempts=8)
    rows = _repairs(evs)
    assert [r.data["changed"] for r in rows] == [[ACTIVATION_MANIFEST_NAME], []]
    assert all(r.data["verified"] == REPAIR_INERT for r in rows)
    (terminal,) = _terminals(evs)
    rationale = terminal.data["triage_rationale"]
    assert "activation manifest" in rationale and "byte-identical" not in rationale
