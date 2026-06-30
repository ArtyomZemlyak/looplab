"""Genesis (CLI kind-inference): the LLM authors an inline task from a plain goal, so the user never
names a `kind`. These exercise the authoring logic with a scripted client (no network)."""
from __future__ import annotations

import json

from looplab.genesis import (GENERATIVE_KINDS, author_task, _missing_local_paths, _scout_roots,
                             GENESIS_E2E_RULE)

KINDS = ("quadratic", "dataset", "repo", "mlebench_real", "classification")


class _ScriptedClient:
    """Returns a fixed structured object and records the prompt it was sent."""
    def __init__(self, ret):
        self.ret = ret
        self.seen = None

    def complete_tool(self, messages, schema):
        self.seen = messages
        return self.ret


def test_author_infers_kind_and_grounds_prompt():
    client = _ScriptedClient({
        "task": {"kind": "dataset", "goal": "predict target", "direction": "max",
                 "data_path": "d.csv"},
        "rationale": "a data file plus a prediction goal"})
    res = author_task("predict target from my data", client=client, kinds=KINDS, data="d.csv",
                      check_paths=False)
    assert res.kind == "dataset"
    assert res.task["data_path"] == "d.csv"
    # The kind guide and the goal both reached the model.
    blob = " ".join(m["content"] for m in client.seen)
    assert "dataset" in blob and "predict target from my data" in blob and "d.csv" in blob


def test_author_fills_missing_goal_and_direction():
    # The model omitted goal/direction; author_task backfills from what it was told.
    client = _ScriptedClient({"task": {"kind": "quadratic", "bounds": {"x": [-1, 1]}}})
    res = author_task("minimize x^2", client=client, kinds=KINDS, direction="min")
    assert res.task["goal"] == "minimize x^2"
    assert res.task["direction"] == "min"


def test_author_vague_goal_returns_empty_with_reply():
    client = _ScriptedClient({"task": {}, "reply": "What data do you have?"})
    res = author_task("make it good", client=client, kinds=KINDS)
    assert res.kind is None
    assert res.reply == "What data do you have?"


def test_author_reports_endpoint_error_distinct_from_vague_goal():
    # A transport/model failure sets `error` (and leaves task empty) so the CLI can say "reach the
    # model" instead of mis-blaming the user's goal.
    class _Boom:
        def complete_tool(self, messages, schema):
            raise RuntimeError("connection refused")
    res = author_task("predict churn from data.csv", client=_Boom(), kinds=KINDS)
    assert res.kind is None and res.task == {}
    assert res.error and "connection refused" in res.error
    assert res.reply == ""          # not a vague-goal clarifying reply


def test_author_backfills_explicit_data_path_when_model_omits_it():
    client = _ScriptedClient({"task": {"kind": "dataset", "goal": "predict", "direction": "max"}})
    res = author_task("predict from my file", client=client, kinds=KINDS, data="/d/train.csv",
                      check_paths=False)
    assert res.task["data_path"] == "/d/train.csv"     # the named --data path is never lost


def test_author_backfills_editable_path_for_repo():
    client = _ScriptedClient({"task": {"kind": "repo", "goal": "improve", "direction": "max"}})
    res = author_task("improve my repo", client=client, kinds=KINDS, data="/my/repo", kind="repo",
                      check_paths=False)
    assert res.task["editable_path"] == "/my/repo"


def test_author_refines_a_draft_in_place():
    client = _ScriptedClient({"task": {"kind": "repo", "goal": "g", "direction": "max"}})
    author_task("make it faster", client=client, kinds=KINDS,
                draft={"kind": "repo", "eval": {"command": ["python", "score.py"]}})
    blob = " ".join(m["content"] for m in client.seen)
    assert "draft" in blob and "score.py" in blob      # the file's task block reached the model


def test_generative_kinds_membership():
    # The kinds that imply an LLM-driven run (so genesis defaults backend=llm for them).
    assert {"dataset", "repo", "mlebench_real"} <= GENERATIVE_KINDS
    assert "quadratic" not in GENERATIVE_KINDS


def test_author_pins_kind_even_if_model_drifts():
    # The user pinned kind=repo; even if the model emits a different kind, the pin wins, and the
    # prompt instructs it to stay within the pinned kind.
    client = _ScriptedClient({"task": {"kind": "dataset", "goal": "x"}, "rationale": "drifted"})
    res = author_task("optimize my project", client=client, kinds=KINDS, kind="repo")
    assert res.kind == "repo"
    blob = " ".join(m["content"] for m in client.seen)
    assert "PINNED" in blob and "repo" in blob


def test_author_describes_multiple_data_locations_in_prompt():
    # No --data passed: the model is told it may author one or many data locations from the words.
    client = _ScriptedClient({"task": {
        "kind": "dataset", "goal": "merge and predict", "direction": "max",
        "data": {"train": "/d/train.csv", "extra": "/other/feats"}}})
    res = author_task("data is in /d/train.csv and the folder /other/feats; predict the label",
                      client=client, kinds=KINDS, check_paths=False)
    assert res.task["data"] == {"train": "/d/train.csv", "extra": "/other/feats"}
    blob = " ".join(m["content"] for m in client.seen)
    assert "SEVERAL" in blob or "several" in blob          # the data guide reached the model
    assert "folder" in blob


def test_author_refuses_a_missing_data_path():
    # The model authored a dataset task pointing at a path that doesn't exist -> Genesis REFUSES with
    # a path_error + clarifying reply, and hands back NO task (so the CLI never spawns a doomed run).
    client = _ScriptedClient({"task": {"kind": "dataset", "goal": "predict", "direction": "max",
                                       "data_path": "/no/such/data.csv"}})
    res = author_task("predict from my file", client=client, kinds=KINDS)
    assert res.kind is None and res.task == {}
    assert res.path_error and "/no/such/data.csv" in res.path_error
    assert "/no/such/data.csv" in res.reply
    assert res.error == ""                                  # distinct from an endpoint failure


def test_author_accepts_an_existing_data_path(tmp_path):
    # A real on-disk path passes the gate and the task is returned normally.
    f = tmp_path / "train.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    client = _ScriptedClient({"task": {"kind": "dataset", "goal": "predict", "direction": "max",
                                       "data_path": str(f)}})
    res = author_task("predict from my file", client=client, kinds=KINDS)
    assert res.kind == "dataset" and res.path_error == ""
    assert res.task["data_path"] == str(f)


def test_author_refuses_missing_repo_editable_path(tmp_path):
    # Repo task: a non-existent editable_path is refused too (it's the repo the agent would edit).
    client = _ScriptedClient({"task": {"kind": "repo", "goal": "improve", "direction": "max",
                                       "editable_path": str(tmp_path / "ghost")}})
    res = author_task("improve my repo", client=client, kinds=KINDS, kind="repo")
    assert res.task == {} and res.path_error
    assert str(tmp_path / "ghost") in res.path_error


def test_missing_local_paths_covers_repo_mounts(tmp_path):
    # The collector reaches data{}, editables[].path and references[].path — not eval targets.
    real = tmp_path / "repo"
    real.mkdir()
    task = {"kind": "repo", "editable_path": str(real),
            "editables": [{"name": "model", "path": str(tmp_path / "missing_repo")}],
            "references": [{"name": "ref", "path": str(real)}],
            "data": {"d": str(tmp_path / "missing_data")},
            "eval": {"command": ["python", "not_written_yet.py"]}}
    missing = _missing_local_paths(task)
    assert str(tmp_path / "missing_repo") in missing
    assert str(tmp_path / "missing_data") in missing
    assert str(real) not in missing                        # the existing repo/reference are fine
    assert "not_written_yet.py" not in " ".join(missing)   # eval targets are NOT existence-checked


def test_author_check_paths_false_skips_the_gate():
    # Opt-out for programmatic callers that only want the authoring logic (no disk).
    client = _ScriptedClient({"task": {"kind": "dataset", "goal": "g", "direction": "max",
                                       "data_path": "/no/such/path"}})
    res = author_task("predict", client=client, kinds=KINDS, check_paths=False, agentic=False)
    assert res.kind == "dataset" and res.path_error == ""


def test_scout_roots_widens_to_named_paths(tmp_path):
    # The scout's allowed roots include home/CWD plus every location named in the goal/--data, so the
    # agent can actually reach (and verify) a dataset/repo that lives outside home.
    f = tmp_path / "sub" / "train.csv"
    f.parent.mkdir(parents=True)
    f.write_text("x\n", encoding="utf-8")
    roots = _scout_roots(f"predict from {f}", data=None, repo=None)
    assert f in roots and f.parent in roots          # the named path AND its parent are reachable


def test_scout_roots_never_widens_to_filesystem_root():
    from pathlib import Path
    # An incidental slash in prose (a ratio/units, NOT a path) must NOT add `/` as a root — that would
    # let the scout read any allowlisted file anywhere on the machine (root is an ancestor of all).
    for goal in ("maximize throughput in req/s", "fit f(x)=x^2/2", "optimize cost per km/h"):
        roots = _scout_roots(goal, data=None, repo=None)
        assert Path("/") not in roots, (goal, roots)


def test_scout_roots_drops_nonexistent_goal_paths_but_keeps_explicit():
    from pathlib import Path
    # A made-up absolute path in the GOAL (doesn't exist) is dropped; an explicit --data/--repo path
    # keeps typo-recovery (its parent is added so the agent can find the real file nearby).
    g_roots = _scout_roots("predict from /no/such/ghost.csv", data=None, repo=None)
    assert Path("/no/such/ghost.csv") not in g_roots and Path("/no/such") not in g_roots
    d_roots = _scout_roots("improve my repo", data="/no/such/repo", repo=None)
    assert Path("/no/such/repo") in d_roots and Path("/no/such") in d_roots


class _ToolClient:
    """A fake tool-driving client: first .chat() inspects the disk, second emits the task. Records the
    system prompt so a test can assert the headless/e2e contract reached the model."""
    def __init__(self, list_path: str, emit_task: dict):
        self._calls = 0
        self._list_path = list_path
        self._emit_task = emit_task
        self.system = ""

    def chat(self, messages, tools, tool_choice="auto"):
        if not self.system:
            self.system = next((m["content"] for m in messages if m["role"] == "system"), "")
        self._calls += 1
        if self._calls == 1:          # turn 1: verify the data location on disk before authoring
            return {"content": "let me check the path",
                    "tool_calls": [{"id": "a", "type": "function",
                                    "function": {"name": "list_dir",
                                                 "arguments": json.dumps({"path": self._list_path})}}]}
        return {"content": "", "tool_calls": [{"id": "b", "type": "function",   # turn 2: emit
                "function": {"name": "emit",
                             "arguments": json.dumps({"task": self._emit_task,
                                                      "rationale": "verified on disk"})}}]}


def test_author_agentic_scouts_then_emits(tmp_path):
    # When the client can drive tools, genesis runs as an AGENT: it calls a filesystem tool, then
    # emits — and the headless/e2e contract is in the system prompt.
    f = tmp_path / "train.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    client = _ToolClient(str(tmp_path), {"kind": "dataset", "goal": "predict", "direction": "max",
                                         "data_path": str(f)})
    res = author_task(f"predict from {f}", client=client, kinds=KINDS, data=str(f))
    assert res.kind == "dataset" and res.task["data_path"] == str(f)
    assert res.path_error == ""
    assert "HEADLESS" in client.system and GENESIS_E2E_RULE.strip()[:20] in client.system


def test_author_agentic_emit_still_path_gated(tmp_path):
    # Even via the agent, a bad emitted path is caught by the deterministic backstop (the agent is
    # supposed to verify, but the gate guarantees a doomed task never escapes).
    client = _ToolClient(str(tmp_path), {"kind": "dataset", "goal": "p", "direction": "max",
                                         "data_path": str(tmp_path / "ghost.csv")})
    res = author_task("predict", client=client, kinds=KINDS)
    assert res.task == {} and res.path_error
    assert "ghost.csv" in res.path_error


def test_author_agentic_falls_back_when_tools_unsupported():
    # A client whose .chat raises (can't tool-call) must fall back to the single structured call.
    class _BadChat:
        def chat(self, messages, tools, tool_choice="auto"):
            raise RuntimeError("no tool support")
        def complete_tool(self, messages, schema):
            return {"task": {"kind": "quadratic", "goal": "g", "direction": "min",
                             "bounds": {"x": [-1, 1]}}, "rationale": "fallback"}
    res = author_task("minimize x^2", client=_BadChat(), kinds=KINDS)
    assert res.kind == "quadratic" and res.rationale == "fallback"
