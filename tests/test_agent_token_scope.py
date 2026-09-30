"""doc 70 70.8: the agent token (`LOOPLAB_HARNESS_TOKEN`) is the `owner` principal (`serve/server.py`),
and the refusal of the intents LoopLab's own model fulfils held only on an EXTERNALLY driven run — so
on an INTERNAL run its holder queued a `fork` the live engine then built with the owner's paid
Developer, though the harness manifest promises "scoped agent requests cannot … invoke LoopLab's owner
model workflows" (critic 2026-09-29, driven). Driven here through `POST /commands`, the one intake:
on an internal run the agent token is refused every such intent and every start of the run's own loop
(`serve/control_validation.py::agent_token_refusal`); the owner keeps all of them; a ready-made
inject, a remeasure and a pause stay open to the agent; on an external run the harness keeps its
resume and meets the external rule as before.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.serve.control_validation import AGENT_TOKEN_REFUSED_STARTS, agent_token_refusal
from looplab.serve.server import make_app
from tests.factories import post_command

AGENT = {"X-LoopLab-Token": "agent-secret"}
OWNER = {"X-LoopLab-Token": "operator-secret"}


def _client(tmp_path, monkeypatch, *, external: bool, snapshot: bool = True) -> TestClient:
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "operator-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    rd = tmp_path / "demo"
    rd.mkdir()
    if snapshot:
        settings = Settings(backend="toy", external_harness=external)
        (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "one", "task_id": "task",
                                 "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(1)"})
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0})
    store.append("pause", {})          # a stopped run: an admitted intent waits for the resume
    return TestClient(make_app(tmp_path))


def _refused(response) -> bool:
    assert response.status_code == 200, response.text
    record = response.json()
    return (record.get("status") == "rejected"
            and (record.get("error") or {}).get("code") == "agent_token_refused")


_MODEL_FULFILLED = (
    ("fork", {"from_node_id": 0}),
    ("force_ablate", {}),
    ("deep_research", {}),
    ("node_reset", {"node_id": 0, "from_stage": "implement"}),
    ("inject_node", {"idea": {"operator": "improve", "rationale": "the agent's idea"},
                     "parent_id": 0}),
)


def test_the_agent_token_is_refused_what_the_owner_s_model_fulfils_on_an_internal_run(
        tmp_path, monkeypatch):
    """MUTATIONS, each red here: drop the agent-token clause in `_external_mode_restriction`; stamp
    no agent marker in the auth middleware; the route passes no `agent_token`."""
    client = _client(tmp_path, monkeypatch, external=False)
    for i, (kind, data) in enumerate(_MODEL_FULFILLED):
        response = post_command(client, kind, data, key=f"agent-{i}", headers=AGENT)
        assert _refused(response), (kind, response.json())
    for i, kind in enumerate(sorted(AGENT_TOKEN_REFUSED_STARTS)):
        response = post_command(client, kind, {}, key=f"agent-start-{i}", headers=AGENT)
        assert _refused(response), (kind, response.json())
    # What stays open to the agent: a ready-made candidate, a hint.
    ready = post_command(client, "inject_node", {"idea": {"operator": "improve"}, "parent_id": 0,
                                                 "code": "print(2)"},
                         key="agent-ready", headers=AGENT)
    assert not _refused(ready) and ready.json().get("status") != "rejected", ready.json()
    hint = post_command(client, "hint", {"text": "try a smaller learning rate"},
                        key="agent-hint", headers=AGENT)
    assert not _refused(hint), hint.json()
    # The OWNER keeps every one of them.
    owner = post_command(client, "fork", {"from_node_id": 0}, key="owner-fork", headers=OWNER)
    assert owner.status_code == 200 and not _refused(owner), owner.json()
    assert owner.json().get("status") != "rejected", owner.json()


def test_on_an_external_run_the_harness_keeps_its_resume_and_meets_the_external_rule(
        tmp_path, monkeypatch):
    """The external rule is unchanged (its own 409-coded refusal, for every credential), and a resume
    is the harness's to drive there. MUTATION: refuse the agent token's starts on an external run."""
    client = _client(tmp_path, monkeypatch, external=True)
    fork = post_command(client, "fork", {"from_node_id": 0}, key="agent-fork", headers=AGENT)
    record = fork.json()
    assert record.get("status") == "rejected" and not _refused(fork), record
    resume = post_command(client, "resume", {}, key="agent-resume", headers=AGENT)
    assert not _refused(resume), resume.json()


def test_a_run_with_no_snapshot_fails_closed_for_the_agent_token_only(tmp_path, monkeypatch):
    """The run's mode cannot be read: the agent token is refused (fail closed), the owner's historical
    admission is untouched."""
    client = _client(tmp_path, monkeypatch, external=False, snapshot=False)
    assert _refused(post_command(client, "fork", {"from_node_id": 0}, key="a", headers=AGENT))
    owner = post_command(client, "fork", {"from_node_id": 0}, key="o", headers=OWNER)
    assert not _refused(owner), owner.json()


@pytest.mark.parametrize("kind,data,refused", [
    ("fork", {}, True), ("force_ablate", {}, True), ("deep_research", {}, True),
    ("resume", {}, True), ("restart", {}, True), ("run_reopened", {}, True),
    ("node_reset", {"from_stage": "implement"}, True), ("node_reset", {"from_stage": "propose"}, True),
    ("node_reset", {"from_stage": "eval"}, False), ("node_reset", {}, False),
    ("inject_node", {}, True), ("inject_node", {"code": "x"}, False),
    ("inject_node", {"files": {"a.py": "x"}}, False),
    ("pause", {}, False), ("hint", {"text": "t"}, False), ("budget_extend", {}, False),
])
def test_the_agent_token_rule_s_truth_table(kind, data, refused):
    got = agent_token_refusal(kind, data)
    assert (got is not None) is refused, (kind, data)
    if got is not None:
        assert got.status_code == 403 and got.detail["code"] == "agent_token_refused"
