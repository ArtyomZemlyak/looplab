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
