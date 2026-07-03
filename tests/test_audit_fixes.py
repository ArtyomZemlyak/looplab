"""Regression tests for the deep-audit findings (security/trust, resilience, resume, deletions)."""
from __future__ import annotations

import sys

import anyio
import pytest

from looplab.command_eval import read_metric
from looplab.config import Settings
from looplab.models import Idea, Node, NodeStatus, RunState
from looplab.orchestrator import Engine
from looplab.patch import gate
from looplab.policy import GreedyTree
from looplab.replay import fold
from looplab.repo_task import EditableSpec, EvalSpec, ReferenceSpec, RepoTask
from looplab.sandbox import SubprocessSandbox

_M = {"kind": "stdout_json", "key": "metric"}


def _eng(tmp_path, task, **kw):
    r, d = task.build_roles()
    return Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)


# A1 — an explicit (non-onboarded) adapter metric is protected from agent edits
def test_adapter_metric_protected_explicit_eval():
    t = RepoTask(id="a", editable_path="/x", edit_surface=["*.py"],
                 eval=EvalSpec(command=["python", "t.py"],
                               metric={"kind": "adapter", "path": "LOOPLAB_adapter.py"}))
    assert "LOOPLAB_adapter.py" in t.repo_spec()["protected_names"]


# A2 — protected check is case-insensitive (Windows/NTFS + fnmatch)
def test_write_node_files_case_insensitive_protect(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "ttrain.py").write_text("real", encoding="utf-8")
    t = RepoTask(id="p", editable_path=str(repo), edit_surface=["*.py"], protect=["ttrain.py"],
                 eval=EvalSpec(command=[sys.executable, "ttrain.py"], metric=_M))
    eng = _eng(tmp_path, t)
    wd = tmp_path / "wd"; wd.mkdir()
    (wd / "ttrain.py").write_text("real", encoding="utf-8")
    node = Node(id=0, operator="draft", idea=Idea(operator="draft"),
                files={"Ttrain.PY": "raise SystemExit('cheat')"})
    eng._write_node_files(node, wd)
    # case-variant name must NOT overwrite the protected file
    assert (wd / "ttrain.py").read_text(encoding="utf-8") == "real"


# A3 — the patch gate rejects a protected file even when it matches the surface
def test_patch_gate_rejects_protected():
    diff = ("diff --git a/ttrain.py b/ttrain.py\n--- a/ttrain.py\n+++ b/ttrain.py\n"
            "@@ -1 +1 @@\n-x\n+y\n")
    assert gate(diff, ["*.py"])["ok"] is True                      # in surface
    g = gate(diff, ["*.py"], ["ttrain.py"])                        # but protected
    assert g["ok"] is False and "ttrain.py" in g["rejected"]
    # case-variant is also rejected
    diff2 = diff.replace("ttrain.py", "Ttrain.PY")
    assert gate(diff2, ["*.py"], ["ttrain.py"])["ok"] is False


# A5 — reference/data mount names are validated (used as wd/name)
def test_reference_data_names_validated():
    with pytest.raises(ValueError, match="simple subdir"):
        RepoTask(id="r", editable_path="/x", references=[ReferenceSpec(name="../etc", path="/y")])
    with pytest.raises(ValueError, match="collision"):
        RepoTask(id="r", editable_path="/x", editables=[EditableSpec(name="dup", path="/m")],
                 data={"dup": "/d"})


# B3 — a bad regex metric pattern reads as no-metric, not a crash
def test_regex_metric_bad_pattern_is_none():
    assert read_metric("acc=0.9", ".", {"kind": "stdout_regex", "pattern": "(", "group": 1}) is None
    assert read_metric("acc=0.9", ".", {"kind": "stdout_regex", "pattern": r"acc=([0-9.]+)",
                                        "group": 5}) is None       # group out of range


# C1 — resume reconstructs run-only settings from the snapshot
def test_settings_roundtrip_through_snapshot():
    s = Settings()
    s.require_approval, s.trust_mode, s.confirm_seeds = True, "untrusted", 4
    snap = s.masked_snapshot()
    snap.pop("llm_api_key", None)
    s2 = Settings(**snap)
    assert s2.require_approval is True and s2.trust_mode == "untrusted" and s2.confirm_seeds == 4


# C2 — confirm_eval events populate the per-seed resume memo
def test_fold_confirm_seed_results():
    from looplab.models import Event
    evs = [Event(type="run_started", data={"run_id": "r", "task_id": "t"}),
           Event(type="confirm_eval", data={"node_id": 3, "seed": 0, "eval_seconds": 1.0, "metric": 0.5}),
           Event(type="confirm_eval", data={"node_id": 3, "seed": 1, "eval_seconds": 1.0, "metric": None})]
    st = fold(evs)
    assert st.confirm_seed_results == {3: {0: 0.5, 1: None}}


# E — an accepted in-surface deletion is applied to the eval workdir
def test_deletion_applied_in_write_node_files(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "keep.py").write_text("k", encoding="utf-8")
    t = RepoTask(id="d", editable_path=str(repo), edit_surface=["*.py"],
                 eval=EvalSpec(command=[sys.executable, "keep.py"], metric=_M))
    eng = _eng(tmp_path, t)
    wd = tmp_path / "wd"; wd.mkdir()
    (wd / "old.py").write_text("dead", encoding="utf-8")
    node = Node(id=0, operator="draft", idea=Idea(operator="draft"), deleted=["old.py"])
    eng._write_node_files(node, wd)
    assert not (wd / "old.py").exists()                            # accepted deletion took effect


# B1 — the agentic Researcher survives a malformed-JSON tool call (does not crash the run)
def test_tool_loop_survives_malformed_json_args():
    from looplab.agent import ToolUsingResearcher

    class _Tools:
        def specs(self): return []
        def execute(self, n, a): return ""

    class _Client:
        def chat(self, messages, tool_specs, tool_choice="auto"):
            return {"tool_calls": [{"id": "1", "function": {"name": "emit",
                                                            "arguments": '{"params": {'}}]}  # malformed

    r = ToolUsingResearcher(client=_Client(), tools=_Tools(), bounds=None)
    idea = r.propose(RunState(goal="g", direction="max"), None)
    assert isinstance(idea, Idea) and idea.operator                # fell back, no crash


# #32 — the patch gate scopes each named repo's surface to its own subdir
def test_gate_prefix_scopes_named_repo_surface():
    diff = ("diff --git a/model/evil.py b/model/evil.py\n--- a/model/evil.py\n"
            "+++ b/model/evil.py\n@@ -1 +1 @@\n-x\n+y\n")
    allow = ["**/*.py", "model/keep/*.py"]                          # model repo's narrow surface
    assert gate(diff, allow)["ok"] is True                          # without prefixes: root glob leaks
    assert gate(diff, allow, prefixes=["model"])["ok"] is False     # scoped: not in model/keep/*
    assert gate(diff.replace("model/evil.py", "model/keep/m.py"), allow, prefixes=["model"])["ok"]
    assert gate(diff.replace("model/evil.py", "top.py"), allow, prefixes=["model"])["ok"]


# #33 — protected names are normalized (./ , backslash) to match git-diff paths
def test_protected_names_normalized():
    t = RepoTask(id="n", editable_path="/x", edit_surface=["*.py"], protect=["./secret.py"],
                 eval=EvalSpec(command=["python", "t.py"],
                               metric={"kind": "file_json", "path": "./metrics.json"}))
    pn = t.repo_spec()["protected_names"]
    assert "secret.py" in pn and "metrics.json" in pn


# #17/#18 — event seq advances only after a durable write; a non-dict line stops the reader
def test_eventstore_seq_and_nondict_guard(tmp_path):
    from looplab.eventstore import EventStore, iter_jsonl
    s = EventStore(tmp_path / "e.jsonl")
    s.append("a", {}); s.append("b", {})
    assert [e.seq for e in s.read_all()] == [0, 1]
    with open(tmp_path / "e.jsonl", "ab") as f:
        f.write(b"5\n")                                             # valid JSON but not an object
    assert len(list(iter_jsonl(tmp_path / "e.jsonl"))) == 2         # stops cleanly, keeps the 2 records


# #21/#22 — grep rejects a bad/over-long (ReDoS) pattern and caps file size
def test_grep_guards_bad_and_long_pattern(tmp_path):
    from looplab.retrieval import grep
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    assert grep("(", str(tmp_path)) == []                          # invalid regex -> []
    assert grep("a" * 2000, str(tmp_path)) == []                   # over-long pattern -> []
    assert [h.line for h in grep("hello", str(tmp_path))] == ["hello"]


# #38 — confirm_top_k does not crown a node that produced zero usable seed scores
def test_confirm_top_k_skips_scoreless_node():
    from looplab.confirm import confirm_top_k
    nodes = [Node(id=0, operator="draft", idea=Idea(operator="draft"), metric=1.0,
                  status=NodeStatus.evaluated)]
    res = confirm_top_k(nodes, lambda n, s: 0.0, k=1, seeds=[], direction="min")
    assert res["best_node_id"] is None                             # no seeds -> no fabricated 0.0 winner


# #27/#31 — MCTS values a candidate by its FEASIBLE descendants only
def test_mcts_ignores_infeasible_descendant_metric():
    from looplab.policy import MCTSPolicy
    st = RunState(direction="max")
    p = Node(id=0, operator="draft", idea=Idea(operator="draft"), metric=1.0,
             status=NodeStatus.evaluated, feasible=True)
    child = Node(id=1, parent_ids=[0], operator="improve", idea=Idea(operator="improve"),
                 metric=99.0, status=NodeStatus.evaluated, feasible=False)   # great but infeasible
    other = Node(id=2, operator="draft", idea=Idea(operator="draft"), metric=2.0,
                 status=NodeStatus.evaluated, feasible=True)
    st.nodes = {0: p, 1: child, 2: other}
    act = MCTSPolicy(n_seeds=1, max_nodes=10).next_actions(st)
    # node 0's only high score is its infeasible child (99) — it must NOT be valued by it, so the
    # feasible node 2 (metric 2 > 1) is the better-valued expansion target.
    assert act and act[0]["kind"] == "improve" and act[0]["parent_id"] == 2


# #0/#36 — confirm resumes mid-node: seeds already recorded are NOT re-run
def test_confirm_phase_skips_already_run_seeds(tmp_path):
    from looplab.toytask import ToyTask
    from looplab.sandbox import RunResult
    task = ToyTask.load(__import__("pathlib").Path("examples/toy_task.json"))
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 confirm_top_k=1, confirm_seeds=3)
    ran: list[int] = []

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None):
        ran.append(int((env or {}).get("LOOPLAB_EVAL_SEED", -1)))
        return RunResult(exit_code=0, stdout="", stderr="", metric=1.0, timed_out=False)

    eng._run_eval = fake_run_eval
    st = RunState(direction="max")
    st.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft"), metric=1.0,
                        status=NodeStatus.evaluated, feasible=True)}
    # Seeds 1,2 already done in a prior attempt. (Confirm seeds are 1..3 by default now —
    # confirm_seed_base=1 keeps them disjoint from the search's implicit seed 0, D1.)
    st.confirm_seed_results = {0: {1: 1.0, 2: 1.0}}
    anyio.run(eng._confirm_phase, st)
    assert ran == [3]                                  # only the missing seed re-runs


# #55 — a metric file with a UTF-8 BOM still parses
def test_file_json_strips_bom(tmp_path):
    (tmp_path / "m.json").write_text('﻿{"metric": 0.7}', encoding="utf-8")
    assert read_metric("", str(tmp_path),
                       {"kind": "file_json", "path": "m.json", "key": "metric"}) == 0.7


# #80 — RepoTask direction is validated (a typo can't silently flip the objective)
def test_repo_task_direction_validated():
    with pytest.raises(ValueError, match="direction must be"):
        RepoTask(id="d", editable_path="/x", direction="maximize",
                 eval=EvalSpec(command=["python", "t.py"]))


# #54 — a constraint/metric reader may not be an agent-authored adapter
def test_constraints_adapter_reader_rejected(tmp_path):
    from looplab.command_eval import run_command_eval
    (tmp_path / "p.py").write_text('print("{\\"metric\\": 1.0}")', encoding="utf-8")
    with pytest.raises(ValueError, match="built-in, not 'adapter'"):
        run_command_eval([sys.executable, "p.py"], str(tmp_path), 60, _M,
                         constraints=[{"kind": "adapter", "path": "x.py", "max": 1}])


# #53 — opencode_config tolerates a trailing-slash model id
def test_opencode_config_trailing_slash():
    import json as _j
    from looplab.cli_agent import opencode_config
    cfg = _j.loads(opencode_config("http://h:1", "ollama/"))
    assert "ollama" in cfg["provider"]
    models = cfg["provider"]["ollama"]["models"]
    assert "ollama/" not in models                     # not the broken empty-name id
