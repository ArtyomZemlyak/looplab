"""Genesis (CLI kind-inference): the LLM authors an inline task from a plain goal, so the user never
names a `kind`. These exercise the authoring logic with a scripted client (no network)."""
from __future__ import annotations

from looplab.genesis import GENERATIVE_KINDS, author_task

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
    res = author_task("predict target from my data", client=client, kinds=KINDS, data="d.csv")
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


def test_author_survives_model_error():
    class _Boom:
        def complete_tool(self, messages, schema):
            raise RuntimeError("endpoint down")
    res = author_task("anything", client=_Boom(), kinds=KINDS)
    assert res.kind is None and res.task == {}


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
                      client=client, kinds=KINDS)
    assert res.task["data"] == {"train": "/d/train.csv", "extra": "/other/feats"}
    blob = " ".join(m["content"] for m in client.seen)
    assert "SEVERAL" in blob or "several" in blob          # the data guide reached the model
    assert "folder" in blob
