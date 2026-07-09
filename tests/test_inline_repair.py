"""Hybrid in-node crash repair: agent/rule triage + inline repair within one node.

Covers the replay-safety of the new `node_repaired` event, the inline-repair attempt loop in
`_evaluate`, the deterministic rule-based triage fallback, and the `idea_rejected` lineage
suppression in `debug_action`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

from looplab.events.eventstore import EventStore
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.orchestrator import Engine, _rule_triage
from looplab.search.policy import GreedyTree, debug_action
from looplab.events.replay import fold
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}

# A solution that raises a MECHANICAL crash (ModuleNotFoundError) — the rule-based triage treats
# this as repairable in place.
_BAD = "import definitely_not_a_real_module_zzz\n"
_GOOD = "import json; print(json.dumps({'metric': 0.1}))\n"


class _Stub:
    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})


class _MechCrashThenFixed:
    """Crashes mechanically on first run, then repair() returns a working script."""
    def __init__(self):
        self.repair_calls = 0

    def implement(self, idea):
        return _BAD

    def repair(self, idea, code, error):
        self.repair_calls += 1
        return _GOOD


class _AlwaysMechCrash:
    """Every attempt (implement and repair) crashes mechanically — exercises the attempt bound."""
    def __init__(self):
        self.repair_calls = 0

    def implement(self, idea):
        return _BAD

    def repair(self, idea, code, error):
        self.repair_calls += 1
        return _BAD


def _engine(run_dir, dev, **kw):
    # auto_install_deps off: `_BAD` imports a deliberately fake module — env-prep must not try to
    # pip-install it (it isn't on the install allowlist anyway, but keep these tests fully offline).
    kw.setdefault("auto_install_deps", False)
    return Engine(run_dir, task=ToyTask.load(TASK), researcher=_Stub(), developer=dev,
                  sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=1, max_nodes=4, debug_depth=1), **kw)


def _events(run_dir):
    return list(EventStore(Path(run_dir) / "events.jsonl").read_all())


# --------------------------------------------------------------------------- replay safety
def test_node_repaired_folds_final_code_once(tmp_path):
    """node_created(BAD) -> node_repaired(GOOD) -> node_evaluated folds to GOOD/evaluated, and the
    eval cost is counted exactly once. Re-folding is identical (determinism)."""
    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""},
                              "code": _BAD})
    s.append("node_repaired", {"node_id": 0, "attempt": 1, "code": _GOOD})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.1, "eval_seconds": 3.0})

    a = fold(EventStore(p).read_all())
    b = fold(EventStore(p).read_all())
    assert a.model_dump() == b.model_dump()
    assert a.nodes[0].code == _GOOD
    assert a.nodes[0].status is NodeStatus.evaluated
    assert a.total_eval_seconds == 3.0


def test_node_repaired_after_terminal_is_noop(tmp_path):
    """A stray/corrupt node_repaired AFTER the terminal event must not mutate the (now non-pending)
    node — mirrors the first_terminal idempotency guard."""
    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""},
                              "code": _GOOD})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.1, "eval_seconds": 1.0})
    s.append("node_repaired", {"node_id": 0, "attempt": 9, "code": "print('hijacked')"})

    st = fold(EventStore(p).read_all())
    assert st.nodes[0].code == _GOOD          # unchanged: node already terminal
    assert st.nodes[0].status is NodeStatus.evaluated


# --------------------------------------------------------------------------- engine: happy path
def test_inline_repair_fixes_in_place_without_new_node(tmp_path):
    dev = _MechCrashThenFixed()
    eng = _engine(tmp_path / "on", dev, inline_repair=True, inline_repair_attempts=1)
    anyio.run(eng.run)

    evs = _events(tmp_path / "on")
    repaired = [e for e in evs if e.type == "node_repaired"]
    assert repaired, "expected an inline node_repaired event"
    assert dev.repair_calls >= 1

    st = fold(evs)
    n0 = st.nodes[0]
    assert n0.status is NodeStatus.evaluated      # repaired in place -> evaluated
    assert n0.code == _GOOD
    # The repair did NOT add a debug node for node 0 (inline repair never creates a tree node).
    assert not any(n.operator == "debug" and 0 in n.parent_ids for n in st.nodes.values())


def test_inline_repair_off_restores_debug_node(tmp_path):
    """With inline_repair=False the crash fails normally and the inter-node debug operator repairs
    it via a NEW node (the prior behavior)."""
    dev = _MechCrashThenFixed()
    eng = _engine(tmp_path / "off", dev, inline_repair=False)
    anyio.run(eng.run)

    evs = _events(tmp_path / "off")
    assert not any(e.type == "node_repaired" for e in evs)
    assert any(e.type == "node_failed" and e.data.get("reason") == "crash" for e in evs)
    st = fold(evs)
    assert any(n.operator == "debug" for n in st.nodes.values())   # a debug node was created


def test_inline_repair_attempt_bound(tmp_path):
    """A node that keeps crashing emits exactly `inline_repair_attempts` node_repaired events, then
    fails normally and stays eligible for the budgeted inter-node debug operator."""
    dev = _AlwaysMechCrash()
    eng = _engine(tmp_path / "bound", dev, inline_repair=True, inline_repair_attempts=2)
    anyio.run(eng.run)

    evs = _events(tmp_path / "bound")
    repaired_n0 = [e for e in evs if e.type == "node_repaired" and e.data.get("node_id") == 0]
    assert len(repaired_n0) == 2                 # bounded by inline_repair_attempts
    failed_n0 = [e for e in evs if e.type == "node_failed" and e.data.get("node_id") == 0]
    assert failed_n0 and failed_n0[0].data.get("reason") == "crash"


# --------------------------------------------------------------------------- triage trace band
def test_agent_triage_runs_under_its_own_span_not_evaluate(tmp_path):
    """The crash-triage LLM decision must band as `triage`, NOT `evaluate`: it runs INSIDE the engine's
    `evaluate` span, so without its own span its (often many, agentic) turns inherit phase=evaluate and
    inflate the 'evaluate' band with failure-debugging that never scored anything (the 'why is there a
    big eval when it never scored?' confusion)."""
    from looplab.core import tracing
    seen = {}

    class _TriageR:
        def propose(self, state, parent):
            return Idea(operator="x", params={"x": 1.0, "y": 1.0})

        def triage_crash(self, node, error, attempt, *, state=None, brief=""):
            ph = tracing._phase_ctx.get()        # (name, span_id) of the innermost open operation
            seen["phase"] = ph[0] if ph else None
            return {"action": "repair", "rationale": "fix it"}

    eng = Engine(tmp_path / "run", task=ToyTask.load(TASK), researcher=_TriageR(),
                 developer=_MechCrashThenFixed(), sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=2, debug_depth=1),
                 auto_install_deps=False, inline_repair=True)
    anyio.run(eng.run)
    assert seen.get("phase") == "triage"         # triage_crash saw its OWN phase, not 'evaluate'
    names = [json.loads(l).get("name") for l in
             (tmp_path / "run" / "spans.jsonl").read_text().splitlines() if l.strip()]
    assert "triage" in names                     # the span is on disk for the trace view


# --------------------------------------------------------------------------- safe stage reuse (P2)
def _mk_repo(tmp_path):
    """A workdir with a train stage script that imports loss.py, and a separate score script."""
    (tmp_path / "train.py").write_text("import loss\nimport torch\nprint('train')\n", encoding="utf-8")
    (tmp_path / "loss.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "looplab_eval.py").write_text("import pickle\nprint('score')\n", encoding="utf-8")
    return tmp_path


_STAGES = [{"name": "train", "command": ["python", "train.py", "--bs", "8192"]},
           {"name": "score", "command": ["python", "looplab_eval.py"]}]


def test_stage_reachable_files_scripts_plus_one_hop_imports(tmp_path):
    _mk_repo(tmp_path)
    reach = Engine._stage_reachable_files([_STAGES[0]], tmp_path)   # the train stage only
    assert "train.py" in reach          # its own script
    assert "loss.py" in reach           # one-hop import of train.py
    assert "looplab_eval.py" not in reach   # belongs to a later stage, not reachable from train


def test_safe_reuse_reuses_when_repair_touched_only_the_failed_stage_script(tmp_path):
    _mk_repo(tmp_path)
    e = Engine.__new__(Engine)                       # bare instance — the method uses only static helpers
    # the observed case: score crashed, repair fixed ONLY looplab_eval.py → reuse train, restart at score
    assert e._safe_reuse_start(_STAGES, "score", {"looplab_eval.py"}, tmp_path) == "score"


def test_safe_reuse_retrains_when_repair_touched_train_or_its_imports(tmp_path):
    _mk_repo(tmp_path)
    e = Engine.__new__(Engine)
    # editing the train script → must re-train (fresh model), never reuse a stale checkpoint
    assert e._safe_reuse_start(_STAGES, "score", {"train.py"}, tmp_path) is None
    # editing a file IMPORTED by the train script (loss.py) → also re-train (one-hop closure)
    assert e._safe_reuse_start(_STAGES, "score", {"loss.py"}, tmp_path) is None
    # editing BOTH the eval and a train input → the train input forces a re-train
    assert e._safe_reuse_start(_STAGES, "score", {"looplab_eval.py", "loss.py"}, tmp_path) is None


def test_safe_reuse_none_when_first_stage_failed_or_no_stages(tmp_path):
    _mk_repo(tmp_path)
    e = Engine.__new__(Engine)
    assert e._safe_reuse_start(_STAGES, "train", {"train.py"}, tmp_path) is None   # nothing before it
    assert e._safe_reuse_start([], "score", {"x.py"}, tmp_path) is None            # single-command eval
    assert e._safe_reuse_start(_STAGES, None, {"x.py"}, tmp_path) is None          # no failed stage


def test_stage_reachable_files_opaque_stage_forbids_reuse(tmp_path):
    # A stage that runs SOMETHING with no local .py script (`python -m pkg`, a shell wrapper) is OPAQUE:
    # we can't bound which files it reads, so its checkpoint must NEVER be reused — even if the repair's
    # changed set looks disjoint. Fail-closed: _stage_reachable_files returns None and reuse is refused.
    _mk_repo(tmp_path)
    (tmp_path / "trainer").mkdir()
    (tmp_path / "trainer" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "trainer" / "__main__.py").write_text("import loss\n", encoding="utf-8")
    m_stage = [{"name": "train", "command": ["python", "-m", "trainer"]},
               {"name": "score", "command": ["python", "looplab_eval.py"]}]
    assert Engine._stage_reachable_files([m_stage[0]], tmp_path) is None        # opaque -> sentinel
    e = Engine.__new__(Engine)
    # even a disjoint-looking change (only the score script) must NOT reuse across an opaque train stage
    assert e._safe_reuse_start(m_stage, "score", {"looplab_eval.py"}, tmp_path) is None
    sh_stage = [{"name": "train", "command": ["bash", "train.sh"]},
                {"name": "score", "command": ["python", "looplab_eval.py"]}]
    assert Engine._stage_reachable_files([sh_stage[0]], tmp_path) is None       # shell wrapper -> opaque


def test_stage_reachable_files_transitive_and_subdir_imports(tmp_path):
    # The reachable set must follow TRANSITIVE + dotted + subdir-sibling imports, else a repair that
    # edits a training dependency two hops down (or in a package submodule) escapes and a stale
    # checkpoint is reused. train.py -> from pkg import a ; pkg/a.py -> import deep ; src/train2.py -> import sib
    (tmp_path / "train.py").write_text("from pkg import a\nimport model, extra\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("import deep\n", encoding="utf-8")
    (tmp_path / "deep.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "model.py").write_text("m = 1\n", encoding="utf-8")
    (tmp_path / "extra.py").write_text("e = 1\n", encoding="utf-8")
    reach = Engine._stage_reachable_files([{"name": "train", "command": ["python", "train.py"]}], tmp_path)
    assert {"train.py", "pkg/a.py", "deep.py", "model.py", "extra.py"} <= reach   # transitive + 2nd of `import a, b`
    assert "pkg/__init__.py" in reach                                             # package init on the path
    # subdir script whose sibling import resolves via the script's own dir (sys.path[0])
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "train.py").write_text("import sib\n", encoding="utf-8")
    (tmp_path / "src" / "sib.py").write_text("s = 1\n", encoding="utf-8")
    reach2 = Engine._stage_reachable_files([{"name": "t", "command": ["python", "src/train.py"]}], tmp_path)
    assert "src/sib.py" in reach2


def test_safe_reuse_refused_when_the_stage_manifest_changed(tmp_path):
    # A repair that rewrites looplab_stages.json alters the pipeline's argv (e.g. train hyperparams), so
    # the completed checkpoint no longer matches the declared command — reuse must be refused even though
    # the manifest isn't a stage SCRIPT and so wouldn't appear in any reachable set.
    _mk_repo(tmp_path)
    e = Engine.__new__(Engine)
    assert e._safe_reuse_start(_STAGES, "score", {"looplab_eval.py", "looplab_stages.json"}, tmp_path) is None


def test_safe_reuse_fail_closed_on_deletion(tmp_path):
    # D1: a DELETED file is unlinked before the reuse predicate runs, so the reachability closure
    # (walked over files still on disk) can never rediscover an earlier stage's import of it —
    # changed ∩ reachable would be trivially empty even when train imported the vanished module.
    # ANY deletion must therefore force a full re-run, even one that looks disjoint from train.
    _mk_repo(tmp_path)
    e = Engine.__new__(Engine)
    assert e._safe_reuse_start(_STAGES, "score", {"helper.py", "looplab_eval.py"}, tmp_path,
                               deleted=["helper.py"]) is None
    # without the deletion the same change set reuses fine (the guard is the deletion, not the name)
    assert e._safe_reuse_start(_STAGES, "score", {"looplab_eval.py"}, tmp_path, deleted=[]) == "score"


def test_safe_reuse_fail_closed_on_non_py_change(tmp_path):
    # D2: reachability only bounds PYTHON imports — a changed config/params/data file read by the
    # train stage is invisible to the closure, so its effect can't be proven absent → full re-run.
    _mk_repo(tmp_path)
    e = Engine.__new__(Engine)
    assert e._safe_reuse_start(_STAGES, "score", {"config.yaml"}, tmp_path) is None
    assert e._safe_reuse_start(_STAGES, "score", {"looplab_eval.py", "params.json"}, tmp_path) is None


def test_safe_reuse_fail_closed_on_non_default_cwd(tmp_path):
    # D3: with a non-default eval cwd the changed-file keys and the stage-script paths resolve
    # against DIFFERENT bases (sub/train.py vs train.py), so the disjointness test proves nothing.
    _mk_repo(tmp_path)
    e = Engine.__new__(Engine)
    assert e._safe_reuse_start(_STAGES, "score", {"looplab_eval.py"}, tmp_path, cwd="sub") is None
    assert e._safe_reuse_start(_STAGES, "score", {"looplab_eval.py"}, tmp_path, cwd="/abs/dir") is None
    for c in (None, "", "."):        # the default-cwd spellings still allow reuse
        assert e._safe_reuse_start(_STAGES, "score", {"looplab_eval.py"}, tmp_path, cwd=c) == "score"


def test_stage_reachable_files_parenthesized_multiline_import(tmp_path):
    # D4: `from pkg import (\n  vit,\n  mlp,\n)` spans lines — the line-anchored import scan only
    # saw `from pkg import (` and credited pkg/__init__.py while MISSING pkg/vit.py / pkg/mlp.py,
    # so a repair to a submodule escaped the closure and a stale checkpoint was reused.
    # The ')' inside vit's trailing comment is deliberate: the paren pattern's `[^)]*` group stops
    # at the FIRST ')', so before comments were stripped from the source it truncated the captured
    # name list right there and DROPPED mlp — a repair to pkg/mlp.py escaped the closure.
    (tmp_path / "train.py").write_text(
        "from pkg import (\n    vit,  # backbone (legacy)\n    mlp,\n)\nprint('train')\n",
        encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "vit.py").write_text("v = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "mlp.py").write_text("m = 1\n", encoding="utf-8")
    reach = Engine._stage_reachable_files([{"name": "train", "command": ["python", "train.py"]}], tmp_path)
    assert {"pkg/__init__.py", "pkg/vit.py", "pkg/mlp.py"} <= reach
    e = Engine.__new__(Engine)
    stages = [{"name": "train", "command": ["python", "train.py"]},
              {"name": "score", "command": ["python", "score.py"]}]
    (tmp_path / "score.py").write_text("print('s')\n", encoding="utf-8")
    assert e._safe_reuse_start(stages, "score", {"pkg/vit.py"}, tmp_path) is None   # submodule edit -> re-train


# --------------------------------------------------------------------------- rule-based triage
def test_rule_triage_repairs_mechanical_only():
    assert _rule_triage("crash", "ModuleNotFoundError: no module", 1, 1)["action"] == "repair"
    assert _rule_triage("crash", "TypeError: unexpected keyword argument 'multi_class'",
                        1, 2)["action"] == "repair"
    # Non-mechanical crash -> abandon (never reject_idea from the rule).
    assert _rule_triage("crash", "AssertionError: metric too low", 1, 2)["action"] == "abandon"
    # Attempts exhausted -> abandon even if mechanical.
    assert _rule_triage("crash", "ImportError: x", 2, 1)["action"] == "abandon"


# --------------------------------------------------------------------------- idea_rejected gating
def test_idea_rejected_lineage_skipped_by_debug_action():
    st = RunState(run_id="r", task_id="t", direction="min")
    st.nodes[0] = Node(id=0, parent_ids=[], operator="draft",
                       idea=Idea(operator="draft", params={}),
                       status=NodeStatus.failed, error="boom", error_reason="idea_rejected")
    assert debug_action(st, debug_depth=1) is None     # rejected idea is not debugged
    # A plain crash leaf IS debugged.
    st.nodes[0].error_reason = "crash"
    act = debug_action(st, debug_depth=1)
    assert act and act["parent_id"] == 0


# ------------------------------------------------- loop wiring: reuse start_stage / retrain cap (D13)
# The static predicate above is only half the feature — these drive the REAL repair loop in
# `_evaluate` (with `run_command_eval` stubbed) and assert what actually reaches the eval call:
# the safe-reuse `start_stage`, the fail-closed guards forcing a full re-run, and the retrain-cap
# abandon. The stub replaces the subprocess layer only; stage resolution (the workdir manifest +
# the appended protected score stage), triage, and the changed-set computation all run for real.

def _staged_src(tmp_path):
    """An editable repo whose Developer manifest declares a `train` stage before the operator's
    score command — train.py imports loss.py, so edits to either must invalidate reuse."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "train.py").write_text("import loss\nprint('train')\n", encoding="utf-8")
    (src / "loss.py").write_text("x = 1\n", encoding="utf-8")
    (src / "helper.py").write_text("h = 1\n", encoding="utf-8")
    (src / "looplab_eval.py").write_text("print('score v0')\n", encoding="utf-8")
    (src / "looplab_stages.json").write_text(
        json.dumps({"stages": [{"name": "train", "command": ["python", "train.py"]}]}),
        encoding="utf-8")
    return src


def _fail_score(stderr):
    """A staged eval where train completed but the protected score stage crashed."""
    from looplab.runtime.sandbox import RunResult
    return RunResult(exit_code=1, stdout="", stderr=stderr, metric=None, timed_out=False,
                     failed_stage="score",
                     stages=[{"name": "train", "status": "ok", "exit_code": 0, "seconds": 0.1},
                             {"name": "score", "status": "fail", "exit_code": 1, "seconds": 0.1}])


def _ok_eval():
    from looplab.runtime.sandbox import RunResult
    return RunResult(exit_code=0, stdout='{"metric": 1.0}', stderr="", metric=1.0, timed_out=False,
                     stages=[{"name": "train", "status": "ok", "exit_code": 0, "seconds": 0.1},
                             {"name": "score", "status": "ok", "exit_code": 0, "seconds": 0.1}])


class _StagedDev:
    """Repo-style developer: implement() makes no edits (the seeded tree IS the solution);
    each repair() ships the next entry of `fixes` as its cumulative last_files (+ deletions).
    `implement_deleted` are deletions made at IMPLEMENT time — like the real repo developer's
    `last_deleted`, they stay in the CUMULATIVE deletion set every later repair reports too."""
    def __init__(self, fixes, deleted=None, implement_deleted=None):
        self.fixes = list(fixes)
        self.deleted = list(deleted or [])
        self.implement_deleted = list(implement_deleted or [])
        self.last_files: dict = {}
        self.last_deleted: list = []
        self.repair_calls = 0

    def implement(self, idea):
        self.last_files, self.last_deleted = {}, list(self.implement_deleted)
        return ""

    def repair(self, idea, code, error):
        self.repair_calls += 1
        if self.fixes:
            self.last_files = dict(self.fixes.pop(0))
        self.last_deleted = list(self.implement_deleted) + list(self.deleted)
        return ""


def _staged_engine(run_dir, src, dev, monkeypatch, captured, results, cwd=".", **kw):
    """Engine over the staged repo with run_command_eval stubbed: records each eval call's
    `start_stage` into `captured` and returns the next queued RunResult."""
    from looplab.runtime import command_eval

    def fake(cmd, _cwd, timeout, metric, env=None, **kwargs):
        captured.append(kwargs.get("start_stage"))
        return results.pop(0)

    monkeypatch.setattr(command_eval, "run_command_eval", fake)
    t = RepoTask(id="r", direction="max", editable_path=str(src),
                 edit_surface=["*.py", "*.yaml", "*.json"],
                 eval=EvalSpec(command=[sys.executable, "looplab_eval.py"], metric=_M, cwd=cwd))
    r, _ = t.build_roles()
    kw.setdefault("auto_install_deps", False)
    kw.setdefault("inline_repair", True)
    kw.setdefault("inline_repair_attempts", 2)
    return Engine(run_dir, task=t, researcher=r, developer=dev, sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)


def test_loop_safe_repair_passes_start_stage_into_eval(tmp_path, monkeypatch):
    """A repair that touched ONLY the failed score stage's script must re-enter the eval with
    start_stage='score' (train reused) — the wiring from _safe_reuse_start into run_command_eval."""
    captured, results = [], [_fail_score("ModuleNotFoundError: No module named 'alpha'"), _ok_eval()]
    dev = _StagedDev(fixes=[{"looplab_eval.py": "print('score v1')\n"}])
    eng = _staged_engine(tmp_path / "run", _staged_src(tmp_path), dev, monkeypatch, captured, results)
    anyio.run(eng.run)
    assert dev.repair_calls == 1
    assert captured == [None, "score"]           # full first run, then reuse-into-score after the fix
    st = fold(_events(tmp_path / "run"))
    assert st.nodes[0].status is NodeStatus.evaluated and st.nodes[0].metric == 1.0


def test_loop_deletion_forces_full_rerun(tmp_path, monkeypatch):
    """D1 at loop level: the same score-only fix, but the repair ALSO deleted a file — the next
    eval must be a FULL re-run (start_stage None), never a reuse of the train checkpoint."""
    captured, results = [], [_fail_score("ModuleNotFoundError: No module named 'alpha'"), _ok_eval()]
    dev = _StagedDev(fixes=[{"looplab_eval.py": "print('score v1')\n"}], deleted=["helper.py"])
    eng = _staged_engine(tmp_path / "run", _staged_src(tmp_path), dev, monkeypatch, captured, results)
    anyio.run(eng.run)
    assert captured == [None, None]              # deletion -> fail closed -> full pipeline re-run


def test_loop_prior_deletion_does_not_block_reuse(tmp_path, monkeypatch):
    """F2: `last_deleted` is CUMULATIVE (seeded from node.deleted at repair_from), so a file the
    IMPLEMENT step deleted — before the train stage ever completed — must not veto reuse forever:
    a deletion that predates the completed train stage cannot invalidate its checkpoint. Only THIS
    repair's deletion DELTA may fail closed; a score-only repair still reuses train."""
    captured, results = [], [_fail_score("ModuleNotFoundError: No module named 'alpha'"), _ok_eval()]
    dev = _StagedDev(fixes=[{"looplab_eval.py": "print('score v1')\n"}],
                     implement_deleted=["helper.py"])
    eng = _staged_engine(tmp_path / "run", _staged_src(tmp_path), dev, monkeypatch, captured, results)
    anyio.run(eng.run)
    assert dev.repair_calls == 1
    assert captured == [None, "score"]           # the pre-existing deletion did not force a re-train
    st = fold(_events(tmp_path / "run"))
    assert st.nodes[0].status is NodeStatus.evaluated and st.nodes[0].metric == 1.0


def _poke_sibling_state_before_repair(eng, dev, sibling_deleted):
    """Simulate the max_parallel>1 interleave: a SIBLING node's build overwrites the SHARED
    developer's `last_deleted` between this node's implement and its repair. Triage runs
    immediately before the engine snapshots the pre-repair deletion baseline, so poking the
    developer there plants the stale sibling state exactly where the pre-fix code read it."""
    orig = eng._triage_crash

    def wrapped(*a, **kw):
        dev.last_deleted = list(sibling_deleted)
        return orig(*a, **kw)

    eng._triage_crash = wrapped


def test_loop_stale_sibling_deletion_state_does_not_leak_into_baseline(tmp_path, monkeypatch):
    """G1: the pre-repair deletion BASELINE must be read off the NODE (`node.deleted`), never the
    shared developer — at snapshot time `developer.last_deleted` belongs to whatever node it built
    LAST. Here the node's OWN implement deleted helper.py (so it is in node.deleted and in the
    cumulative post-repair set) while the shared dev carries a sibling's unrelated deletion: the
    node's own OLD deletion must not read as a fresh delta, so a score-only repair still reuses
    train. Pre-fix, the sibling baseline made helper.py look freshly deleted -> spurious re-train."""
    captured, results = [], [_fail_score("ModuleNotFoundError: No module named 'alpha'"), _ok_eval()]
    dev = _StagedDev(fixes=[{"looplab_eval.py": "print('score v1')\n"}],
                     implement_deleted=["helper.py"])
    eng = _staged_engine(tmp_path / "run", _staged_src(tmp_path), dev, monkeypatch, captured, results)
    _poke_sibling_state_before_repair(eng, dev, ["other_nodes_file.py"])
    anyio.run(eng.run)
    assert dev.repair_calls == 1
    assert captured == [None, "score"]      # own implement deletion is baseline, not a fresh delta
    st = fold(_events(tmp_path / "run"))
    assert st.nodes[0].status is NodeStatus.evaluated and st.nodes[0].metric == 1.0


def test_loop_repair_deletion_matching_stale_sibling_state_still_fails_closed(tmp_path, monkeypatch):
    """G1 fail-open corner: THIS repair genuinely deletes helper.py, and the stale sibling state
    ALSO happens to name helper.py. Pre-fix the sibling entry masked the real deletion from the
    delta (prev baseline was read off the shared dev), so train's checkpoint was reused across a
    vanished module; with the node-side baseline (node.deleted is empty — this node never deleted
    anything before) it stays a fresh delta and forces the full re-run."""
    captured, results = [], [_fail_score("ModuleNotFoundError: No module named 'alpha'"), _ok_eval()]
    dev = _StagedDev(fixes=[{"looplab_eval.py": "print('score v1')\n"}], deleted=["helper.py"])
    eng = _staged_engine(tmp_path / "run", _staged_src(tmp_path), dev, monkeypatch, captured, results)
    _poke_sibling_state_before_repair(eng, dev, ["helper.py", "other_nodes_file.py"])
    anyio.run(eng.run)
    assert captured == [None, None]         # the real deletion still forces a full pipeline re-run


def test_loop_non_py_change_forces_full_rerun(tmp_path, monkeypatch):
    """D2 at loop level: a repair that edits a non-.py input (config.yaml) alongside the score fix
    is invisible to import reachability — the next eval must be a full re-run."""
    captured, results = [], [_fail_score("ModuleNotFoundError: No module named 'alpha'"), _ok_eval()]
    dev = _StagedDev(fixes=[{"looplab_eval.py": "print('score v1')\n", "config.yaml": "lr: 0.2\n"}])
    eng = _staged_engine(tmp_path / "run", _staged_src(tmp_path), dev, monkeypatch, captured, results)
    anyio.run(eng.run)
    assert captured == [None, None]


def test_loop_non_default_cwd_forces_full_rerun(tmp_path, monkeypatch):
    """D3 at loop level: with the eval spec's cwd set to a subdir, even a score-only fix must NOT
    reuse — changed-file keys and stage-script paths resolve against different bases."""
    captured, results = [], [_fail_score("ModuleNotFoundError: No module named 'alpha'"), _ok_eval()]
    dev = _StagedDev(fixes=[{"looplab_eval.py": "print('score v1')\n"}])
    src = _staged_src(tmp_path)
    (src / "sub").mkdir()
    eng = _staged_engine(tmp_path / "run", src, dev, monkeypatch, captured, results, cwd="sub")
    anyio.run(eng.run)
    assert captured == [None, None]


def test_loop_retrain_cap_abandons_after_cap(tmp_path, monkeypatch):
    """A repair that keeps rewriting EARLIER-stage (train) code burns exactly
    `inline_repair_retrain_cap` full re-trains, then the loop abandons the node (terminal
    node_failed with the cap rationale) instead of paying for another full train."""
    captured = []
    # Two DIFFERENT mechanical errors so the anti-stuck signature guard never fires first.
    results = [_fail_score("ModuleNotFoundError: No module named 'alpha'"),
               _fail_score("TypeError: unexpected keyword argument 'beta'")]
    dev = _StagedDev(fixes=[{"train.py": "import loss\nprint('train v2')\n"},
                            {"train.py": "import loss\nprint('train v3')\n"}])
    eng = _staged_engine(tmp_path / "run", _staged_src(tmp_path), dev, monkeypatch, captured, results,
                         inline_repair_attempts=5, inline_repair_retrain_cap=1)
    anyio.run(eng.run)
    # eval1 (full) + exactly ONE capped full re-train; the second train-touching repair abandons
    # BEFORE another eval, so the results queue of 2 is fully consumed and no 3rd call happens.
    assert captured == [None, None]
    assert dev.repair_calls == 2
    evs = _events(tmp_path / "run")
    failed = [e for e in evs if e.type == "node_failed" and e.data.get("node_id") == 0]
    assert failed and failed[0].data.get("triage_action") == "abandon"
    assert "full re-train" in failed[0].data.get("triage_rationale", "")


def _fail_stage(name, stderr, earlier=()):
    """A staged eval that failed at `name`; `earlier` stages completed before it. Mirrors the real
    RunResult contract the cap check relies on: one record per PRE-failure stage in order, the
    failed stage always the LAST record."""
    from looplab.runtime.sandbox import RunResult
    return RunResult(exit_code=1, stdout="", stderr=stderr, metric=None, timed_out=False,
                     failed_stage=name,
                     stages=[{"name": e, "status": "ok", "exit_code": 0, "seconds": 0.1}
                             for e in earlier]
                     + [{"name": name, "status": "fail", "exit_code": 1, "seconds": 0.1}])


def test_loop_renamed_first_stage_repair_does_not_consume_retrain_cap(tmp_path, monkeypatch):
    """F3: a FIRST-stage (train) failure whose repair renames the stage in the manifest loses the
    failed stage's index in the POST-repair pipeline (-1) — but there was no completed earlier-
    stage work to discard, so it must NOT consume the retrain cap (judged from the pre-repair
    res.stages, where the failed stage is the ONLY record). Pre-fix this was over-counted and
    abandoned at the cap; now the node keeps repairing and evaluates."""
    captured = []
    # Different mechanical errors so the anti-stuck signature guard never fires first; each failure
    # names the CURRENT (renamed) first stage, as the real pipeline would report it.
    results = [_fail_stage("train", "ModuleNotFoundError: No module named 'alpha'"),
               _fail_stage("fit", "TypeError: unexpected keyword argument 'beta'"),
               _ok_eval()]
    _m1 = json.dumps({"stages": [{"name": "fit", "command": ["python", "train.py"]}]})
    _m2 = json.dumps({"stages": [{"name": "fit2", "command": ["python", "train.py"]}]})
    dev = _StagedDev(fixes=[{"looplab_stages.json": _m1}, {"looplab_stages.json": _m2}])
    eng = _staged_engine(tmp_path / "run", _staged_src(tmp_path), dev, monkeypatch, captured, results,
                         inline_repair_attempts=5, inline_repair_retrain_cap=1)
    anyio.run(eng.run)
    assert dev.repair_calls == 2                 # both renaming repairs ran; neither hit the cap
    assert captured == [None, None, None]        # full re-runs (nothing earlier to reuse), no abandon
    st = fold(_events(tmp_path / "run"))
    assert st.nodes[0].status is NodeStatus.evaluated and st.nodes[0].metric == 1.0


def test_loop_renamed_later_stage_repair_still_consumes_retrain_cap(tmp_path, monkeypatch):
    """F3 counterpart: a LATER-stage failure whose repair renames/drops the failed stage still
    discards the completed earlier stage's work on the forced full re-run, so it MUST keep
    consuming the retrain cap — the reason the renamed/-1 case was widened into the count."""
    captured = []
    results = [_fail_stage("train", "ModuleNotFoundError: No module named 'alpha'", earlier=("prep",)),
               _fail_stage("fit", "TypeError: unexpected keyword argument 'beta'", earlier=("prep",))]

    def _manifest(train_name):
        return json.dumps({"stages": [{"name": "prep", "command": ["python", "helper.py"]},
                                      {"name": train_name, "command": ["python", "train.py"]}]})

    src = _staged_src(tmp_path)
    (src / "looplab_stages.json").write_text(_manifest("train"), encoding="utf-8")
    dev = _StagedDev(fixes=[{"looplab_stages.json": _manifest("fit")},
                            {"looplab_stages.json": _manifest("fit2")}])
    eng = _staged_engine(tmp_path / "run", src, dev, monkeypatch, captured, results,
                         inline_repair_attempts=5, inline_repair_retrain_cap=1)
    anyio.run(eng.run)
    # eval1 (full) + exactly ONE capped full re-run; the second renaming repair abandons BEFORE
    # another eval — completed `prep` work was being discarded both times.
    assert captured == [None, None]
    assert dev.repair_calls == 2
    evs = _events(tmp_path / "run")
    failed = [e for e in evs if e.type == "node_failed" and e.data.get("node_id") == 0]
    assert failed and failed[0].data.get("triage_action") == "abandon"
    assert "full re-train" in failed[0].data.get("triage_rationale", "")


# #5 — the error-feedback repair loop fires for a repo task even when the failing node had
#      empty files (e.g. after a baseline fallback)
def test_repair_fires_for_repo_with_empty_files(tmp_path):
    class _Dev:
        def __init__(self):
            self.last_files: dict = {}
            self.repaired = False

        def implement(self, idea: Idea) -> str:
            self.last_files = {}                        # no edits -> baseline eval fails
            return ""

        def repair(self, idea: Idea, code: str, error: str) -> str:
            self.repaired = True
            self.last_files = {"config.json": json.dumps({"needed_x": 3.0})}
            return ""

    t = RepoTask(id="r", direction="max", editable_path=str(FIXTURE), edit_surface=["*.json"],
                 protect=["ttrain_strict.py"],
                 eval=EvalSpec(command=[sys.executable, "ttrain_strict.py"], metric=_M))
    r, _ = t.build_roles()
    dev = _Dev()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=4))
    anyio.run(eng.run)
    assert dev.repaired                                 # repair fired despite empty parent.files
