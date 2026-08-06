"""Orchestrator internals: workspace fingerprinting + resume-drift detection, the run failure
taxonomy (error_reason), and gap-safe node-id allocation.

Regressions consolidated from the code-review rounds (#4 workspace fingerprint + resume drift,
#6 failure taxonomy) and the hourly review loop (iter 6: gap-safe node-id allocation)."""
from __future__ import annotations

import sys
from pathlib import Path

import anyio

from looplab.engine.orchestrator import Engine, _dir_fingerprint
from looplab.search.policy import GreedyTree
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.events.replay import fold

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"
_M = {"kind": "stdout_json", "key": "metric"}


def _repo(tmp_path, body):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "run.py").write_text(body, encoding="utf-8")
    return repo


# large data/ref mounts use a cheap shallow fingerprint (no recursive walk), still
# catching top-level add/remove; missing path -> "absent".
def test_shallow_fingerprint(tmp_path):
    from looplab.engine.orchestrator import _shallow_fingerprint
    d = tmp_path / "data"; d.mkdir()
    (d / "a.bin").write_text("x", encoding="utf-8")
    fp1 = _shallow_fingerprint(str(d))
    assert fp1.startswith("dir:")
    (d / "b.bin").write_text("y", encoding="utf-8")          # top-level add -> changes
    assert _shallow_fingerprint(str(d)) != fp1
    assert _shallow_fingerprint(str(tmp_path / "nope")) == "absent"


# --------------------------------- #6b failure taxonomy ------------------------------
def test_failure_reason_no_metric(tmp_path):
    repo = _repo(tmp_path, "print('hello, no metric here')\n")
    t = RepoTask(id="f", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    state = anyio.run(Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                             sandbox=SubprocessSandbox(),
                             policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    assert state.nodes[0].error_reason == "no_metric"


def test_failure_reason_crash(tmp_path):
    repo = _repo(tmp_path, "raise SystemExit('boom')\n")
    t = RepoTask(id="f", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    state = anyio.run(Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                             sandbox=SubprocessSandbox(),
                             policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    assert state.nodes[0].error_reason == "crash"


# --------------------------------- #4 workspace fingerprint --------------------------
def test_dir_fingerprint_changes_with_content(tmp_path):
    d = tmp_path / "r"; d.mkdir()
    (d / "a.py").write_text("x=1\n", encoding="utf-8")
    fp1 = _dir_fingerprint(str(d))
    (d / "b.py").write_text("y=2\n", encoding="utf-8")     # add a file -> different signature
    assert _dir_fingerprint(str(d)) != fp1
    assert _dir_fingerprint(str(tmp_path / "missing")) == "absent"


def test_workspace_fingerprints_survive_a_wedged_git(tmp_path, monkeypatch):
    """Both fingerprinters shell out to `git rev-parse`, and both run at setup AND on every resume.
    On a wedged FUSE/network mount an unbounded call hangs the run with no diagnostic — so the call
    must be bounded, and a timeout must fall through to the stat/scandir signature, not propagate."""
    import subprocess

    from looplab.engine import triage

    d = tmp_path / "r"; d.mkdir()
    (d / "a.py").write_text("x=1\n", encoding="utf-8")
    seen: list[object] = []

    def wedged_run(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)

    monkeypatch.setattr(subprocess, "run", wedged_run)   # both fingerprinters import it locally

    assert _dir_fingerprint(str(d)).startswith("hash:")          # fell back, did not raise
    assert triage._shallow_fingerprint(str(d)).startswith("dir:")
    # A deadline was actually passed — without one the hang is what the test could never observe.
    assert seen and all(t == triage._GIT_TIMEOUT_S for t in seen), seen


def test_run_records_workspace_and_resume_detects_change(tmp_path):
    repo = _repo(tmp_path, 'import json; print(json.dumps({"metric": 1.0}))\n')
    t = RepoTask(id="w", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    run_dir = tmp_path / "run"
    s1 = anyio.run(Engine(run_dir, task=t, researcher=r, developer=d,
                          sandbox=SubprocessSandbox(),
                          policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    assert s1.workspace and "editable:." in s1.workspace
    assert s1.workspace_changed is False
    # The operator's source changes after the run started; a resume must NOT pretend it's the
    # same workspace.
    (repo / "run.py").write_text(
        'import json; print(json.dumps({"metric": 2.0}))\n', encoding="utf-8")
    r2, d2 = t.build_roles()
    s2 = anyio.run(Engine(run_dir, task=t, researcher=r2, developer=d2,
                          sandbox=SubprocessSandbox(),
                          policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    assert s2.workspace_changed is True


# --------------------------------------------------------------------------- gap-safe node-id alloc
def test_create_node_id_is_gap_safe(tmp_path):
    # A dropped/malformed node_created leaves a GAP in node ids (fold skips the bad event). The next
    # created node must take max(id)+1, NOT len(nodes) — len would collide with an existing higher id
    # and silently overwrite it (corrupting lineage/best-selection). Regression for that bug.
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.adapters.toytask import ToyTask

    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "gap", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=8))
    eng.store.append("run_started", {"run_id": "gap", "task_id": "t", "direction": "min"})
    eng.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""}})
    # node id 2 with NO id 1 -> a gap, as if node 1's event was dropped by fold's malformed-event guard
    eng.store.append("node_created", {"node_id": 2, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": 9.0}, "rationale": ""}})
    assert set(fold(eng.store.read_all()).nodes) == {0, 2}   # gap at 1; len(nodes)==2 would hit node 2

    eng._create_node({"kind": "draft"})

    after = fold(eng.store.read_all())
    assert 2 in after.nodes and after.nodes[2].idea.params["x"] == 9.0   # node 2 NOT overwritten
    assert 3 in after.nodes                                              # new node took max+1, not len(=2)


# ------------------------------------------- the run spine's phase helpers (doc 25 XP-06)
# `_run_with_llm_broker` shed three blocks: the re-entry prologue (`_enter_run`), the eval-spec
# onboarding gates (`_run_spec_gates`) and the empty-action ladder (`_handle_no_actions`). Two
# things must hold for that to be a refactor rather than a regression: the signal vocabulary the
# loop dispatches on stays closed, and the helpers that FOLD stay in the module whose `fold` global
# is the monkeypatch seam.


def _toy_engine(tmp_path, **kwargs):
    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine

    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    return Engine(tmp_path / "spine", task=task, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                  auto_install_deps=False, **kwargs)


def test_the_run_loop_dispatches_on_a_closed_signal_vocabulary():
    """A phase helper reports an outer-loop `break`/`continue` it cannot execute itself. A typo'd
    literal is the `TRIAGE_ACTIONS` failure class: `"brake"` is not `"break"`, so a stop silently
    becomes another turn. Resolved as real `ast.Return` constants — a comment is not an AST node."""
    import ast

    from _source_scan import function_tree
    from looplab.engine.orchestrator import Engine

    expected = {
        "_run_spec_gates": {"continue", "break", None},   # this one may also fall through
        "_handle_no_actions": {"continue", "break"},
        "_handle_create_actions": {"continue", "break"},
    }
    for name, vocabulary in expected.items():
        tree = function_tree(getattr(Engine, name))
        returned = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return):
                continue
            value = node.value
            if isinstance(value, ast.Tuple):          # (signal, state, no_mint_turns)
                value = value.elts[0]
            assert value is None or isinstance(value, ast.Constant), (
                f"{name} returns a computed signal: {ast.unparse(node)}")
            returned.add(None if value is None else value.value)
        assert returned == vocabulary, f"{name} returns {returned}, expected {vocabulary}"


class _GateProbe:
    """Stands in for the Engine so the terminal-gate ladder can be driven as a rule.

    Inline in the run loop it was reachable only by staging a real leakage/abort/budget trip with a
    real in-flight Card build, which is why its ORDER — settle first, finish second — was never
    tested in the one direction that matters: that finalization is not even ATTEMPTED while a
    durable request head is open."""

    def __init__(self, *, head=False, forced_head=False, finished=False):
        self.calls: list[tuple] = []
        self._head, self._forced_head, self._finished = head, forced_head, finished

    def _close_card_build_before_terminal_gate(self, state, max_eval_seconds=None):
        self.calls.append(("card_build", max_eval_seconds))
        return self._head

    def _close_node_creating_forced_request_before_terminal_gate(self, state, *, reason):
        self.calls.append(("forced_request", reason))
        return self._forced_head

    def _finish_with_report_if_quiescent(self, state, data, *, after_seq):
        self.calls.append(("finish", data["reason"], after_seq))
        return self._finished


def _drive_gate(probe, reason="aborted", **kwargs):
    from looplab.engine.orchestrator import Engine

    return Engine._settle_terminal_gate(probe, object(), reason, decision_seq=7, **kwargs)


def test_a_terminal_gate_never_attempts_finalization_while_a_build_head_is_open():
    probe = _GateProbe(head=True, finished=True)
    assert _drive_gate(probe, drain_forced_request=True) == "continue"
    assert probe.calls == [("card_build", None)], (
        "an open Card build must stop the ladder before anything else is tried")


def test_a_terminal_gate_never_attempts_finalization_while_a_forced_creator_is_open():
    probe = _GateProbe(forced_head=True, finished=True)
    assert _drive_gate(probe, "time_budget", max_es=1.5, drain_forced_request=True) == "continue"
    assert probe.calls == [("card_build", 1.5), ("forced_request", "time_budget")], (
        "the eval-seconds ceiling is forwarded, and the drain runs before the finish attempt")


def test_a_terminal_gate_finishes_only_when_the_log_is_quiescent():
    quiet = _GateProbe(finished=True)
    assert _drive_gate(quiet, "leakage") == "break"
    assert quiet.calls == [("card_build", None), ("finish", "leakage", 7)]

    noisy = _GateProbe(finished=False)
    assert _drive_gate(noisy, "leakage") == "continue", (
        "a tail that moved under the decision prefix re-enters every gate, it does not finish")


def test_only_a_budget_gate_drains_a_forced_node_creator():
    """`leakage`/`aborted` outrank a forced creator outright; the budget gates have to skip it
    durably first, so the flag is what tells the two ladders apart."""
    probe = _GateProbe(forced_head=True, finished=True)
    assert _drive_gate(probe, "aborted") == "break"
    assert [call[0] for call in probe.calls] == ["card_build", "finish"]


def test_the_reentry_prologue_still_folds_through_the_orchestrator_module_global(tmp_path,
                                                                                 monkeypatch):
    """`monkeypatch.setattr(orch, "fold", ...)` is what puts the run spine under test in four test
    files. `_enter_run` folds twice and therefore stays in THIS module: a `from looplab.events.replay
    import fold` in some other engine file binds a different object, so the patch would still apply
    and simply stop reaching the prologue — a silent narrowing, not a red test."""
    import looplab.engine.orchestrator as orch

    real = orch.fold
    seen: list[int] = []

    def counting(events):
        seen.append(len(events))
        return real(events)

    eng = _toy_engine(tmp_path)
    monkeypatch.setattr(orch, "fold", counting)
    # `_reentry_repin` folds too and has always lived here, so leaving it in place would satisfy
    # this assertion no matter what `_enter_run` itself binds — measured: the guard was vacuous
    # until this stub. Only the prologue's own folds can register now.
    monkeypatch.setattr(eng, "_reentry_repin", lambda: False)
    eng._enter_run()
    assert seen, "_enter_run no longer folds through the orchestrator module global"


def test_the_empty_action_ladder_still_folds_through_the_orchestrator_module_global(tmp_path,
                                                                                    monkeypatch):
    """Same seam, driven through the branch that actually folds: the HITL gate re-reads the log
    after appending its request, because an abort can win the race against the stale snapshot."""
    import looplab.engine.orchestrator as orch

    eng = _toy_engine(tmp_path, require_approval=True, confirm_top_k=0, confirm_seeds=0)
    eng.store.append("run_started", {"run_id": "spine", "task_id": "t", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""}})
    eng.store.append("node_evaluated", {"node_id": 0, "metric": 1.0, "metrics": {}})
    events = eng.store.read_all()
    state = fold(events)
    assert state.best() is not None and not state.awaiting_approval

    real = orch.fold
    seen: list[int] = []

    def counting(evs):
        seen.append(len(evs))
        return real(evs)

    monkeypatch.setattr(orch, "fold", counting)
    # The request carries the seq it believes it follows; the fold honours it only when it lands
    # exactly there, so this has to be the log's real tail rather than a placeholder.
    signal = anyio.run(lambda: eng._handle_no_actions(state, decision_seq=events[-1].seq))

    assert seen, "_handle_no_actions no longer folds through the orchestrator module global"
    assert signal == "break", "an accepted approval request stops the loop without finishing"
    assert fold(eng.store.read_all()).awaiting_approval


def test_a_binary_task_asset_materializes_instead_of_crashing_every_node(tmp_path):
    """The setup-provenance hash already handles bytes assets (`c.encode(...) if isinstance(c, str)
    else bytes(c)`), so a task exposing a BINARY asset — a pickled encoder, a parquet shard — passes
    setup cleanly. `write_assets` then called `write_text` on it, which raises TypeError on bytes:
    every node materialization crashed, after setup had already declared the asset fine."""
    from types import SimpleNamespace

    from looplab.engine.workspace import WorkspaceSeeder

    ws = WorkspaceSeeder(SimpleNamespace(_assets={
        "notes.txt": "plain text\n",
        "encoder.pkl": b"\x80\x04\x95binary",
        "shard.bin": bytearray(b"\x00\x01\x02"),
    }))
    wd = tmp_path / "node"
    ws.write_assets(wd)

    assert (wd / "notes.txt").read_text(encoding="utf-8") == "plain text\n"
    assert (wd / "encoder.pkl").read_bytes() == b"\x80\x04\x95binary"
    assert (wd / "shard.bin").read_bytes() == b"\x00\x01\x02"
