"""Genesis (CLI kind-inference): the LLM authors an inline task from a plain goal, so the user never
names a `kind`. These exercise the authoring logic with a scripted client (no network)."""
from __future__ import annotations

from looplab.genesis import GENERATIVE_KINDS, author_task, _missing_local_paths

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
    res = author_task("predict", client=client, kinds=KINDS, check_paths=False)
    assert res.kind == "dataset" and res.path_error == ""
