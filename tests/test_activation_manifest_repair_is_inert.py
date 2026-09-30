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
    # Said of what the ENGINE reads — a candidate reading its own manifest is the stated cost.
    assert "unless the candidate's own code reads that file" in manifest_only
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


# ------------------------------------------------------------------------------ what it costs
# The critic's cost half (2026-09-30): the verdict said `inert`, but the node still paid for a
# manifest-only repair more than for a true no-op — a second canary over code that had already
# passed one (the canary's digest hashed the manifest), and every completed stage re-run (the reuse
# predicate forfeits on any non-`.py` change) with a full re-train charged for code nobody touched.
def _ledger_script(ledger: Path) -> str:
    return ("import os\n"
            "c = os.environ.get('LOOPLAB_CANARY') == '1'\n"
            f"open({str(ledger)!r}, 'a').write(('canary' if c else 'full') + '\\n')\n"
            "if c:\n    print('METRIC: 0.1')\n"
            "else:\n    raise KeyError('history_item_sid')\n")


class _LedgerDev(_ManifestOnlyDev):
    """Hands back the node's own script each repair; with `manifest`, a new activation manifest too
    (else a true no-op)."""

    def __init__(self, body: str, *, manifest: bool):
        super().__init__()
        self.body, self.manifest = body, manifest

    def repair(self, idea, code, error):
        self.repair_calls += 1
        self.last_files = {"run.py": self.body}
        if self.manifest:
            self.last_files[ACTIVATION_MANIFEST_NAME] = manifest_text([f"on {self.repair_calls}"])
        return code


def _drive_canary(run_dir: Path, *, manifest: bool, manifest_at_start: bool = False) -> list:
    import sys
    run_dir.mkdir(parents=True)
    ledger = run_dir.parent / f"{run_dir.name}.ledger"
    body = _ledger_script(ledger)
    files = {"run.py": body}
    if manifest_at_start:
        files[ACTIVATION_MANIFEST_NAME] = manifest_text(["on 0"])
    eng = Engine(run_dir / "run", task=ToyTask.load(TASK), researcher=_Judge(),
                 developer=_LedgerDev(body, manifest=manifest), sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=1), auto_install_deps=False,
                 inline_repair=True, inline_repair_attempts=6, eval_canary=True)
    eng._eval_spec = {"command": [sys.executable, "run.py"], "cwd": ".",
                      "metric": {"kind": "stdout_regex", "pattern": "METRIC: ([0-9.]+)"},
                      "timeout": 120.0, "canary": {"env": {}, "timeout": 60.0}}
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g",
                                     "direction": "max"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": "print('unused')\n", "files": files})

    async def _bounded() -> bool:
        with anyio.move_on_after(180) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the inline-repair loop did not terminate"
    return ledger.read_text().split()


def test_a_manifest_only_repair_does_not_buy_a_passed_canary_again(tmp_path):
    """The canary never runs the activation check, so its "already passed" key leaves the manifest
    out (`evaluate.py::_canary_code_digest`). MUTATION: key it on `_workdir_manifest_digest` -> the
    manifest-only chain reads canary, full, canary, full."""
    runs = _drive_canary(tmp_path / "manifest", manifest=True)
    assert runs == _drive_canary(tmp_path / "noop", manifest=False) == ["canary", "full", "full"]


def test_the_canary_records_the_digest_its_due_check_reads(tmp_path):
    """A node that carries a manifest FROM ITS FIRST ATTEMPT: the passed canary's row must be keyed
    on the digest the due check asks for, or the first manifest-only repair buys it again (the test
    above starts manifest-less, where the two digests agree). MUTATION: record
    `_workdir_manifest_digest` at the canary run -> canary, full, canary, full."""
    runs = _drive_canary(tmp_path / "manifest", manifest=True, manifest_at_start=True)
    assert runs == ["canary", "full", "full"], runs


def test_the_canary_digest_is_unchanged_for_a_node_with_no_manifest():
    """Every canary row a manifest-less node already wrote keeps matching."""
    from types import SimpleNamespace

    from looplab.engine.evaluate import _canary_code_digest, _workdir_manifest_digest
    plain = SimpleNamespace(attempt=1, code="c", files={"a.py": "x"}, deleted=["b.py"])
    assert _canary_code_digest(plain) == _workdir_manifest_digest(plain)
    marked = SimpleNamespace(attempt=1, code="c", files={"a.py": "x", ACTIVATION_MANIFEST_NAME: "m"},
                             deleted=["b.py"])
    assert _canary_code_digest(marked) == _canary_code_digest(plain)
    assert _workdir_manifest_digest(marked) != _workdir_manifest_digest(plain), \
        "the workdir stamp still sees the manifest: the new file must reach the disk"
    gone = SimpleNamespace(attempt=1, code="c", files={"a.py": "x"},
                           deleted=["b.py", ACTIVATION_MANIFEST_NAME])
    assert _canary_code_digest(gone) == _canary_code_digest(plain)


class _StageDev:
    """Repair 1 edits `first` (a real change); every later repair rewrites ONLY the activation
    manifest beside the same bytes."""

    def __init__(self, first: str):
        self.first, self.repair_calls = first, 0
        self.last_files: dict = {}
        self.last_deleted: list = []

    def implement(self, idea):
        return ""

    def repair(self, idea, code, error):
        self.repair_calls += 1
        body = ({"train.py": "import loss\nprint('train v1')\n"} if self.first == "train.py"
                else {"mine.py": "print('mine v1')\n"})
        self.last_files = (dict(body) if self.repair_calls == 1 else
                           {**body, ACTIVATION_MANIFEST_NAME: manifest_text([f"on {self.repair_calls}"])})
        return ""


def _drive_stages(tmp_path: Path, monkeypatch, *, first: str, retrain_cap: int, dev_cls=None,
                  seed_manifest: bool = False):
    import json
    import sys

    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.runtime import command_eval
    from looplab.runtime.sandbox import RunResult

    src = tmp_path / "src"
    src.mkdir()
    (src / "mine.py").write_text("print('mine')\n")
    (src / "train.py").write_text("import loss\nprint('train')\n")
    (src / "loss.py").write_text("x = 1\n")
    (src / "looplab_eval.py").write_text("print('score v0')\n")
    if seed_manifest:
        (src / ACTIVATION_MANIFEST_NAME).write_text(manifest_text(["on 0"]))
    (src / "looplab_stages.json").write_text(json.dumps({"stages": [
        {"name": "mine", "command": ["python", "mine.py"], "timeout": 900},
        {"name": "train", "command": ["python", "train.py"], "timeout": 900}]}))
    starts: list = []

    def _fail_train(cmd, _cwd, timeout, metric, env=None, **kw):
        starts.append(kw.get("start_stage"))
        return RunResult(exit_code=1, stdout="", stderr="RuntimeError: boom in train\n",
                         metric=None, timed_out=False, failed_stage="train",
                         stages=[{"name": "mine", "status": "ok", "exit_code": 0, "seconds": 1.0},
                                 {"name": "train", "status": "fail", "exit_code": 1,
                                  "seconds": 1.0}])

    monkeypatch.setattr(command_eval, "run_command_eval", _fail_train)
    task = RepoTask(id="r", direction="max", editable_path=str(src),
                    edit_surface=["*.py", "*.yaml", "*.json"],
                    eval=EvalSpec(command=[sys.executable, "looplab_eval.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}, cwd="."))
    researcher, _ = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher,
                 developer=(dev_cls or _StageDev)(first),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True, inline_repair_attempts=6,
                 inline_repair_retrain_cap=retrain_cap)
    anyio.run(eng.run)
    return starts, list(EventStore(tmp_path / "run" / "events.jsonl").read_all())


def test_a_manifest_only_repair_reuses_what_a_no_op_reuses_and_is_charged_nothing(
        tmp_path, monkeypatch):
    """Repair 1 edits `train.py` (the failed stage: `mine` stays reusable); repair 2 rewrites only
    the manifest. MUTATION: ask the reuse question about the whole change set -> the third attempt
    starts at stage 0 and a full re-train is charged for it."""
    starts, evs = _drive_stages(tmp_path, monkeypatch, first="train.py", retrain_cap=0)
    assert starts[:3] == [None, "train", "train"], starts
    assert not [e for e in evs if e.type == "full_retrain_charged"]


class _DeletingStageDev(_StageDev):
    """Repair 1 edits `train.py`; every later repair only DELETES the repo's own manifest."""

    def repair(self, idea, code, error):
        self.repair_calls += 1
        self.last_files = {"train.py": "import loss\nprint('train v1')\n"}
        self.last_deleted = [] if self.repair_calls == 1 else [ACTIVATION_MANIFEST_NAME]
        return ""


def test_a_manifest_deletion_reuses_what_a_no_op_reuses(tmp_path, monkeypatch):
    """The deletion half of the reuse question: a repair that only DELETES the manifest ran nothing
    new. MUTATION: ask the reuse question about every deletion -> the third attempt starts at stage 0
    and a full re-train is charged for it."""
    starts, evs = _drive_stages(tmp_path, monkeypatch, first="train.py", retrain_cap=0,
                                dev_cls=_DeletingStageDev, seed_manifest=True)
    assert starts[:3] == [None, "train", "train"], starts
    assert not [e for e in evs if e.type == "full_retrain_charged"]


def test_a_spent_retrain_cap_is_not_blamed_on_a_manifest_only_repair(tmp_path, monkeypatch):
    """Repair 1 edits `mine.py` (a real first-stage change: charged, the cap of 1 spent); the
    manifest-only repairs after it end the node on the inert streak, never on "repair keeps changing
    earlier-stage (training) code"."""
    starts, evs = _drive_stages(tmp_path, monkeypatch, first="mine.py", retrain_cap=1)
    charges = [e.data["attempt"] for e in evs if e.type == "full_retrain_charged"]
    assert charges == [1], charges
    assert starts[2:] and all(s == "train" for s in starts[2:]), starts
    (terminal,) = _terminals(evs)
    rationale = terminal.data["triage_rationale"]
    assert "activation manifest" in rationale and "earlier-stage" not in rationale
    assert rationale.rstrip().endswith("fresh work"), "the sentence fits the 300-char cut whole"


def test_the_repair_critic_is_told_a_manifest_only_row_moved_nothing_the_evaluation_runs():
    """`it changed: looplab_activation.json` alone reads as a fix that moved something; the row is
    graded inert. Every other row keeps its bytes. MUTATION: drop the note -> red."""
    from looplab.engine.repair_judgment import format_repair_trajectory
    base = dict(attempt=1, error="boom", fix="f", reason="crash", stages_passed=0)
    shown = format_repair_trajectory([{**base, "verified": REPAIR_INERT,
                                       "changed": [ACTIVATION_MANIFEST_NAME]}])
    assert (f"it changed: {ACTIVATION_MANIFEST_NAME} (graded inert: nothing the evaluation runs "
            "moved)") in shown
    moved = format_repair_trajectory([{**base, "verified": REPAIR_UNSTATED, "changed": ["a.py"]}])
    assert "it changed: a.py\n" in moved and "graded inert" not in moved
    nothing = format_repair_trajectory([{**base, "verified": REPAIR_INERT, "changed": []}])
    assert "it changed: nothing\n" in nothing and "graded inert" not in nothing


def test_the_exempt_set_is_exactly_the_manifest_and_only_for_an_engine_named_failure():
    """MUTATION: widen the set (a stage's own config, the idea report) -> red."""
    from looplab.engine.repair_verify import inert_exempt_paths
    assert inert_exempt_paths("crash") == frozenset({ACTIVATION_MANIFEST_NAME})
    assert inert_exempt_paths("inert_path") == frozenset()
    assert inert_exempt_paths(None) == frozenset()


def test_a_deletion_is_a_change_whatever_the_reason():
    """MUTATION: build the change set without `deleted` -> a repair that deleted a stage's script
    reads inert."""
    v = verify_repair("fix it", changed=(), deleted=["train.py"], engine_reason="crash")
    assert v.verdict != REPAIR_INERT


def test_the_judge_sentence_names_why_the_manifest_did_not_matter():
    """MUTATION: drop the clause that rules out the one failure the manifest decides -> red."""
    row = dict(attempt=1, error="boom", fix="f", verified=REPAIR_INERT,
               changed=[ACTIVATION_MANIFEST_NAME])
    assert "and the failure was not a missing activation marker" in _format_repair_log([row])


class _RealThenNothingDev(_ManifestOnlyDev):
    """Repair 1 adds a helper file (a real change); every later repair hands back the node unchanged."""

    def repair(self, idea, code, error):
        self.repair_calls += 1
        self.last_files = {"helper.py": "X = 1\n"} if self.repair_calls == 1 else {}
        return code


def test_the_abandon_sentence_reads_the_streak_not_the_whole_chain(tmp_path):
    """An earlier, non-inert repair that moved a file is not part of the inert streak: two true
    no-ops after it end on the historical "byte-identical" sentence. MUTATION: read every row of
    `repair_log` instead of the streak's -> the manifest sentence, false of both streak rows."""
    dev, judge = _RealThenNothingDev(), _Judge()
    evs = _drive(tmp_path / "real_then_nothing", dev, judge, code=_BAD, inline_repair_attempts=8)
    rows = _repairs(evs)
    assert [r.data["changed"] for r in rows] == [["helper.py"], [], []], rows
    (terminal,) = _terminals(evs)
    assert "byte-identical" in terminal.data["triage_rationale"]
    assert "activation manifest" not in terminal.data["triage_rationale"]
