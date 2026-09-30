"""doc 70 70.8: the agent token (`LOOPLAB_HARNESS_TOKEN`) is the `owner` principal (`serve/server.py`),
and the refusal of the intents LoopLab's own model fulfils held only on an EXTERNALLY driven run — so
on an INTERNAL run its holder queued a `fork` the live engine then built with the owner's paid
Developer, though the harness manifest promises "scoped agent requests cannot … invoke LoopLab's owner
model workflows" (critic 2026-09-29, driven). Refusing that hand list was not enough: every intent
whose engine policy is not `NO_SPAWN` starts the run's own loop when no engine is alive — an agent
`node_reset`, a ready-made inject and a `budget_extend` each spawned a plain `looplab resume` (critic
crit_v60 F1, driven). Driven here through `POST /commands`, the one intake, with a spawn recorder: on
an internal run the agent token keeps exactly the `NO_SPAWN` intents
(`serve/control_validation.py::agent_token_refusal`), may not retry a record it could not have
submitted, edit the run's configuration or start the paid concept lens; the owner keeps all of them;
on an external run the harness keeps its resume and meets the external rule as before.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.serve.control_validation import (
    AGENT_TOKEN_INTERNAL_INTENTS, AGENT_TOKEN_REFUSED_STARTS, CONTROL_SPECS, agent_token_refusal)
from looplab.serve.protocol import COMMAND_TERMINAL_STATUSES, EnginePolicy
from looplab.serve.run_commands import RunCommandService
from looplab.serve.server import make_app
from tests.factories import command_terminal, http_run_generation, post_command

AGENT = {"X-LoopLab-Token": "agent-secret"}
OWNER = {"X-LoopLab-Token": "operator-secret"}
# The external run's own obligations (a memo, a report, a hypothesis, concepts), off: the candidate
# admission they gate is not what is under test here (`tests/test_external_progress.py`'s set).
_NO_OBLIGATIONS = {"deep_research_every": -1, "report_every": 0, "track_hypotheses": False,
                   "concept_pivot": False, "concept_run_base": False, "cross_run_concepts": False}


def _client(tmp_path, monkeypatch, *, external: bool, snapshot: bool = True,
            shape: str = "paused", task: bool = False):
    """A toy run in one of three stopped shapes — `paused` (the operator's pause), `dead` (its engine
    died: no pause, no finish) or `finished` — and a client whose engine spawns are RECORDED, never
    started (`commands.spawn_engine`)."""
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "operator-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    if snapshot:
        # An external run's own obligations (a research memo, a report) stay out of the way.
        settings = Settings(backend="toy", external_harness=external,
                            **(_NO_OBLIGATIONS if external else {}))
        (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    if task:
        (rd / "task.snapshot.json").write_text(json.dumps({"kind": "quadratic"}))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "one", "task_id": "task",
                                 "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(1)"})
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0})
    if shape == "paused":
        store.append("pause", {})      # an admitted intent waits for the resume
    elif shape == "finished":
        store.append("run_finished", {"reason": "budget"})
    app = make_app(tmp_path)
    srv = app.state.looplab
    spawns: list = []
    # SHORT deadlines: a command the recorder "spawned" waits for an engine that never comes, and at
    # the service's default 120 s its worker outlived the test, polling with `time.sleep` into every
    # later test that patches the clock (full suite, shard 1: the retry ladder's recorded sleeps and
    # the finalize monitor's tick meter). `_no_worker_outlives_the_test` waits them out on top.
    srv.commands = RunCommandService(
        srv, spawn_engine=lambda args, env=None, run_dir=None: spawns.append(list(args)) or None,
        startup_timeout=0.2, command_timeout=0.4, poll_interval=0.01, max_observation_timeout=0.8)
    client = TestClient(app)
    client.spawns = spawns
    _OPEN.append((client, rd))
    return client


_OPEN: list = []


@pytest.fixture(autouse=True)
def _no_worker_outlives_the_test():
    """Every command a test admitted, read to a terminal status before the next test starts."""
    yield
    try:
        for client, rd in _OPEN:
            deadline = time.time() + 15.0
            for record in sorted((rd / ".commands").glob("cmd_*.json")):
                while time.time() < deadline:
                    status = client.get(f"/api/runs/demo/commands/{record.stem}",
                                        headers=OWNER).json().get("status")
                    if status in COMMAND_TERMINAL_STATUSES:
                        break
                    time.sleep(0.02)
                else:
                    raise AssertionError(f"command {record.stem} never settled")
    finally:
        _OPEN.clear()


def _refused(response) -> bool:
    assert response.status_code == 200, response.text
    record = response.json()
    return (record.get("status") == "rejected"
            and (record.get("error") or {}).get("code") == "agent_token_refused")


def _no_spawn_after(client, seconds: float = 0.5) -> list:
    """The spawns recorded after the worker had `seconds` to act on anything admitted."""
    time.sleep(seconds)
    return list(client.spawns)


_SPAWNING = (
    ("fork", {"from_node_id": 0}),
    ("force_ablate", {}),
    ("deep_research", {}),
    ("node_reset", {"node_id": 0, "from_stage": "implement"}),
    ("node_reset", {"node_id": 0}),                       # a remeasure starts the loop too
    ("inject_node", {"idea": {"operator": "improve", "rationale": "the agent's idea"},
                     "parent_id": 0}),
    ("inject_node", {"idea": {"operator": "improve"}, "parent_id": 0, "code": "print(2)"}),
    ("budget_extend", {"add_nodes": 3}),
    ("set_strategy", {"strategy": {"policy": "greedy"}}),
    ("force_confirm", {"node_id": 0}),
    ("approval_granted", {"node_id": 0}),
    ("run_abort", {}),
)


def test_the_agent_token_keeps_only_the_no_spawn_intents_on_an_internal_run(tmp_path, monkeypatch):
    """MUTATIONS, each red here: admit a `NO_SPAWN`-policy check's complement (the hand list of
    27b0228c); drop the agent-token clause in `_external_mode_restriction`; stamp no agent marker in
    the auth middleware; the route passes no `agent_token`."""
    client = _client(tmp_path, monkeypatch, external=False)
    for i, (kind, data) in enumerate(_SPAWNING):
        response = post_command(client, kind, data, key=f"agent-{i}", headers=AGENT)
        assert _refused(response), (kind, data, response.json())
        assert response.json()["error"]["retryable"] is False
    for i, kind in enumerate(sorted(AGENT_TOKEN_REFUSED_STARTS)):
        response = post_command(client, kind, {}, key=f"agent-start-{i}", headers=AGENT)
        assert _refused(response), (kind, response.json())
    # What stays open to the agent: the allow-list — and its records say who asked.
    note = post_command(client, "annotation", {"node_id": 0, "text": "looks promising"},
                        key="agent-note", headers=AGENT)
    assert not _refused(note) and note.json().get("status") != "rejected", note.json()
    assert note.json().get("submitted_by") == "agent_token", note.json()
    # …but not a hint, even one that only ADDS: it reads to every role as the operator's directive,
    # the newest first (critic crit_v63 L1, driven — six agent hints pushed the owner's out of every
    # prompt); a `replace` erased the owner's standing ones outright (critic crit_v62 F4).
    for i, data in enumerate(({"text": "try a smaller learning rate"},
                              {"text": "only mine now", "replace": True})):
        hint = post_command(client, "hint", data, key=f"agent-hint-{i}", headers=AGENT)
        assert _refused(hint), hint.json()
        assert "hint" in hint.json()["error"]["message"], hint.json()
    assert _no_spawn_after(client) == []
    # The OWNER keeps every one of them.
    owner = post_command(client, "fork", {"from_node_id": 0}, key="owner-fork", headers=OWNER)
    assert owner.status_code == 200 and not _refused(owner), owner.json()
    assert owner.json().get("status") != "rejected", owner.json()
    assert "submitted_by" not in owner.json(), "the owner's record is the owner's"


_DRIVING = (
    ("node_abort", {"node_id": 0}),
    ("metric_retarget", {"key": "acc"}),
    ("promote", {"node_id": 0}),
    ("research_completed", {"memo": {"summary": "the agent's memo"}}),
    ("report_generated", {"content": "the agent's report"}),
    ("hypothesis_added", {"id": "h1", "statement": "the agent's hypothesis"}),
    ("card_dropped", {"id": "card-0"}),
)


def test_the_agent_token_may_not_drive_an_internal_run_that_it_cannot_start(
        tmp_path, monkeypatch):
    """Critic crit_v61 M1 (driven): `NO_SPAWN` means an intent never STARTS the engine, not that it
    never DRIVES the owner's run — an agent `metric_retarget` replaced the run's goal and dropped paid
    confirmation evals, a `promote` moved the exported champion, a dropped Card cancelled an
    in-flight evaluation, a memo, a report and a hypothesis spoke as the owner's. Each is refused to
    the agent token now (after the payload's field allow-list, which every credential meets first).
    MUTATION: admit every `NO_SPAWN` intent (the rule this replaced)."""
    client = _client(tmp_path, monkeypatch, external=False)
    for i, (kind, data) in enumerate(_DRIVING):
        assert CONTROL_SPECS[kind].engine_policy is EnginePolicy.NO_SPAWN, kind
        response = post_command(client, kind, data, key=f"drive-{i}", headers=AGENT)
        assert _refused(response), (kind, response.json())


@pytest.mark.parametrize("shape,kind,data", [
    ("paused", "node_reset", {"node_id": 0}),
    ("dead", "inject_node", {"idea": {"operator": "improve"}, "parent_id": 0,
                             "code": "print(2)"}),
    ("finished", "budget_extend", {"add_nodes": 3}),
])
def test_no_agent_intent_spawns_an_internal_run_s_loop(tmp_path, monkeypatch, shape, kind, data):
    """Critic crit_v60 F1's three driven shapes, each of which spawned a plain `looplab resume` for
    an agent-token intent 27b0228c admitted: refused now, and nothing spawned — while the OWNER's
    same intent on the same run spawns the loop (the recorder sees the spawn it would have hidden)."""
    client = _client(tmp_path, monkeypatch, external=False, shape=shape, task=True)
    agent = post_command(client, kind, data, key="agent", headers=AGENT)
    assert _refused(agent), agent.json()
    assert _no_spawn_after(client) == []
    owner = post_command(client, kind, data, key="owner", headers=OWNER).json()
    assert owner.get("status") != "rejected", owner
    deadline = time.time() + 20
    while not client.spawns and time.time() < deadline:
        time.sleep(0.05)
    assert [args[0] for args in client.spawns] == ["resume"], client.spawns


def test_on_an_external_run_the_harness_keeps_its_resume_and_meets_the_external_rule(
        tmp_path, monkeypatch):
    """The external rule is unchanged (its own 409-coded refusal, for every credential), and a resume
    or a ready-made inject is the harness's own to drive there. MUTATION: refuse the agent token's
    starts on an external run."""
    client = _client(tmp_path, monkeypatch, external=True, task=True)
    fork = post_command(client, "fork", {"from_node_id": 0}, key="agent-fork", headers=AGENT)
    record = fork.json()
    assert record.get("status") == "rejected" and not _refused(fork), record
    # (a ready-made inject meets the external run's own obligations, as it would for any credential)
    ready = post_command(client, "inject_node", {"idea": {"operator": "improve"}, "parent_id": 0,
                                                 "code": "print(2)"},
                         key="agent-ready", headers=AGENT)
    assert not _refused(ready), ready.json()
    resume = post_command(client, "resume", {}, key="agent-resume", headers=AGENT)
    assert not _refused(resume) and resume.json().get("status") != "rejected", resume.json()


def test_a_run_with_no_snapshot_fails_closed_for_the_agent_token_only(tmp_path, monkeypatch):
    """The run's mode cannot be read: the agent token is refused (fail closed), the owner's historical
    admission is untouched."""
    client = _client(tmp_path, monkeypatch, external=False, snapshot=False)
    assert _refused(post_command(client, "fork", {"from_node_id": 0}, key="a", headers=AGENT))
    owner = post_command(client, "fork", {"from_node_id": 0}, key="o", headers=OWNER)
    assert not _refused(owner), owner.json()


def test_an_import_is_judged_by_the_node_it_carries(tmp_path):
    """Critic crit_v60 F3 (driven): `{source_run, source_node}` read as ready-made BEFORE the import,
    which then resolved a source node reset from `implement` into an inject with no code — built by
    the run's Developer. The rule runs again over the imported payload: an external run refuses a
    code-less import for every credential and admits one that carries code; on an internal run the
    owner keeps the import (its Developer, its choice). MUTATION: drop the post-normalization
    `_external_mode_restriction`."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from looplab.core.models import Idea, Node, NodeStatus, RunState
    from looplab.events.types import EV_INJECT_NODE
    from looplab.serve.control_validation import normalize_control

    source = RunState(task_id="task", run_id="src", goal="g", direction="min")
    for node_id, code in ((0, ""), (1, "print(3)")):      # node 0 was reset from `implement`
        source.nodes[node_id] = Node(id=node_id, operator="draft", idea=Idea(operator="draft"),
                                     code=code, metric=1.0, status=NodeStatus.evaluated)

    def _run(name, external):
        rd = tmp_path / name
        rd.mkdir()
        settings = Settings(backend="toy", external_harness=external,
                            **(_NO_OBLIGATIONS if external else {}))
        (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
        (rd / "task.snapshot.json").write_text(json.dumps({"kind": "quadratic"}))
        return rd

    target = RunState(task_id="task", run_id="demo", goal="g", direction="min")
    srv = SimpleNamespace(run_dir=lambda run_id: tmp_path / run_id,
                          state=lambda path: source if Path(path).name == "src" else target)
    external, internal = _run("external", True), _run("internal", False)
    carried = normalize_control(srv, external, EV_INJECT_NODE, {"source_run": "src",
                                                                "source_node": 1})
    assert carried["code"] == "print(3)"
    for agent_token in (False, True):
        with pytest.raises(HTTPException) as caught:
            normalize_control(srv, external, EV_INJECT_NODE,
                              {"source_run": "src", "source_node": 0}, agent_token=agent_token)
        assert "requires code or files" in str(caught.value.detail), caught.value.detail
    owner = normalize_control(srv, internal, EV_INJECT_NODE, {"source_run": "src", "source_node": 0})
    assert not owner.get("code") and not owner.get("files")
    with pytest.raises(HTTPException) as caught:
        normalize_control(srv, internal, EV_INJECT_NODE, {"source_run": "src", "source_node": 1},
                          agent_token=True)
    assert caught.value.detail["code"] == "agent_token_refused"


def test_the_agent_token_may_not_retry_the_owner_s_spawning_record(tmp_path, monkeypatch):
    """Critic crit_v60 F5 (driven): the retry route carried no credential, so the agent token
    re-drove the owner's failed `resume` — which spawned the run's loop. MUTATION: the route passes no
    `agent_token` to `RunCommandService.retry`."""
    client = _client(tmp_path, monkeypatch, external=False)        # no task snapshot: spawn fails
    owner = command_terminal(client, post_command(client, "resume", {}, key="owner-resume",
                                                  headers=OWNER).json(), headers=OWNER)
    assert owner["status"] == "failed", owner
    (tmp_path / "demo" / "task.snapshot.json").write_text(json.dumps({"kind": "quadratic"}))
    retried = client.post(f"/api/runs/demo/commands/{owner['id']}/retry", headers=AGENT)
    assert retried.status_code == 403, retried.text
    assert retried.json()["detail"]["code"] == "agent_token_refused"
    assert _no_spawn_after(client) == []
    again = client.post(f"/api/runs/demo/commands/{owner['id']}/retry", headers=OWNER)
    assert "agent_token_refused" not in again.text, again.text     # the owner's retry is its own


def test_the_agent_token_may_not_edit_an_internal_run_s_configuration(tmp_path, monkeypatch):
    """The snapshot is the next resume's settings (critic crit_v60 F4): refused to the agent token on
    an internal run, kept on an external one, the owner's either way."""
    for external in (False, True):
        client = _client(tmp_path / str(external), monkeypatch, external=external)
        current = client.get("/api/runs/demo/config", headers=OWNER).json()
        body = {"expected_generation": http_run_generation(client, headers=OWNER),
                "expected_revision": current["_looplab_config_meta"]["config_revision"],
                "settings": {"timeout": 190}}
        agent = client.put("/api/runs/demo/config", json=body, headers=AGENT)
        if external:
            assert agent.status_code == 200, agent.text
        else:
            assert agent.status_code == 403, agent.text
            assert agent.json()["detail"]["code"] == "agent_token_refused"
            owner = client.put("/api/runs/demo/config", json=body, headers=OWNER)
            assert owner.status_code == 200, owner.text


def test_the_agent_token_may_not_start_the_paid_concept_lens(tmp_path, monkeypatch):
    """Critic crit_v60 F2 (driven): `POST /concepts/lens` reached `metered_run_client` and a paid
    `derive_lens` call for the agent token. Refused at the middleware now."""
    client = _client(tmp_path, monkeypatch, external=False)
    response = client.post("/api/runs/demo/concepts/lens", json={"lens": "group by mechanism"},
                           headers=AGENT)
    assert response.status_code == 403, response.text


_MIDDLEWARE_DENIAL = "harness token cannot change operator defaults"


@pytest.mark.parametrize("path", [
    "/api/runs/demo/chat-log", "/api/runs/demo/memory-purge",
    "/api/runs/demo/resolve-activity-claims", "/api/runs/demo/nodes/0/clear_trace",
    "/api/runs/demo/concepts/lens/abandon", "/api/runs/demo/concepts/lens/recovery/abandon",
    "/api/scope-report-actions/act-1/abandon"])
def test_the_agent_token_may_not_write_the_chat_log_or_clean_up_the_owner_s_work(
        tmp_path, monkeypatch, path):
    """Critic crit_v62 F1 (driven): the chat log holds the pending actions the owner's TUI replays
    with the OWNER's token — a forged row became a fork, a resume and a paid report refresh. F5
    (driven): `memory-purge` emptied the lesson store of a live internal run, and the other routes
    abandon or clear the owner's work. Each is refused at the middleware; the owner's is not.
    MUTATION: drop either clause from the harness deny list."""
    client = _client(tmp_path, monkeypatch, external=False)
    refused = client.post(path, json={}, headers=AGENT)
    assert refused.status_code == 403 and _MIDDLEWARE_DENIAL in refused.text, (path, refused.text)
    owner = client.post(path, json={}, headers=OWNER)
    assert _MIDDLEWARE_DENIAL not in owner.text, (path, owner.text)


_SAMPLE = {"{run_id}": "demo", "{kind}": "skills", "{name}": "agent.md",
           "{operation_id}": "op-1"}


def test_the_phase_index_s_write_access_is_what_the_middleware_enforces(tmp_path, monkeypatch):
    """Critic crit_v62 F2 (driven): the harness phase index advertised the authoring write as the
    agent's after the middleware had closed it. Every HTTP write of the index, sent with the agent
    token: an `operator` one meets the middleware's denial, an `external_agent` one does not (the
    route may still refuse its body — that is the route's own answer, not the credential's)."""
    from looplab.harness.phases import PHASES, OPERATOR_WRITES
    client = _client(tmp_path, monkeypatch, external=True)
    seen = set()
    for phase in PHASES:
        access = phase.public()["write_access"]
        for ref, who in access.items():
            if ref.startswith("command:") or ref in seen:
                continue
            seen.add(ref)
            method, template = ref.split(" ", 1)
            path = template
            for placeholder, value in _SAMPLE.items():
                path = path.replace(placeholder, value)
            response = client.request(method, path, json={}, headers=AGENT)
            denied = response.status_code == 403 and _MIDDLEWARE_DENIAL in response.text
            assert denied is (who == "operator") is (ref in OPERATOR_WRITES), (ref, response.text)
    assert "PUT /api/{kind}/{name}/operations/{operation_id}" in seen
    # The authoring phase names what is left to the agent beside the write it may not make (critic
    # crit_v63 N4: dropping the skill-candidate entry went unnoticed, another phase lists the route).
    knowledge = next(p for p in PHASES if p.id == "agent_knowledge").public()["write_access"]
    assert knowledge == {"PUT /api/{kind}/{name}/operations/{operation_id}": "operator",
                         "POST /api/runs/{run_id}/skill-candidates": "external_agent"}, knowledge


@pytest.mark.parametrize("path", ["/api/runs/demo/reviews/link-1", "/api/projects/p-1",
                                  "/api/supertasks/s-1"])
def test_the_agent_token_may_not_revoke_a_share_link_or_delete_a_project(
        tmp_path, monkeypatch, path):
    """Critic crit_v63 (incidental): revoking the owner's review link and deleting a project or a
    super-task were open to the harness token. Refused at the middleware; the owner's is not.
    MUTATION: drop any of the three from the harness deny list's DELETE clause."""
    client = _client(tmp_path, monkeypatch, external=False)
    refused = client.delete(path, headers=AGENT)
    assert refused.status_code == 403 and _MIDDLEWARE_DENIAL in refused.text, (path, refused.text)
    owner = client.delete(path, headers=OWNER)
    assert _MIDDLEWARE_DENIAL not in owner.text, (path, owner.text)


@pytest.mark.parametrize("kind", ["prompts", "skills", "knowledge"])
def test_the_agent_token_may_not_rewrite_the_owner_s_prompts_skills_or_knowledge(
        tmp_path, monkeypatch, kind):
    """Critic crit_v61 M2 (driven): `PUT /api/prompts/<key>.md` and `PUT /api/skills/<name>.md`
    answered the harness token 200, and every live internal run re-reads them — the owner's
    defaults. Refused at the middleware, on both authoring write routes; the owner's write is not.
    MUTATION: drop the authoring clause from the harness deny list."""
    client = _client(tmp_path, monkeypatch, external=False)
    for path in (f"/api/{kind}/agent.md", f"/api/{kind}/agent.md/operations/op-1"):
        refused = client.put(path, json={"content": "x"}, headers=AGENT)
        assert refused.status_code == 403, (path, refused.text)
        assert "operator defaults" in refused.text
        owner = client.put(path, json={"content": "x"}, headers=OWNER)
        assert owner.status_code != 403, (path, owner.text)


@pytest.mark.parametrize("kind,data,refused", [
    ("fork", {}, True), ("force_ablate", {}, True), ("deep_research", {}, True),
    ("resume", {}, True), ("restart", {}, True), ("run_reopened", {}, True),
    ("node_reset", {"from_stage": "implement"}, True), ("node_reset", {"from_stage": "propose"}, True),
    ("node_reset", {"from_stage": "eval"}, True), ("node_reset", {}, True),
    ("inject_node", {}, True), ("inject_node", {"code": "x"}, True),
    ("inject_node", {"files": {"a.py": "x"}}, True), ("budget_extend", {}, True),
    ("set_strategy", {}, True), ("approval_granted", {}, True), ("spec_approved", {}, True),
    ("run_abort", {}, True), ("force_confirm", {}, True), ("not_an_intent", {}, True),
    ("metric_retarget", {}, True), ("promote", {}, True), ("research_completed", {}, True),
    ("report_generated", {}, True), ("hypothesis_added", {}, True), ("card_dropped", {}, True),
    ("card_reopened", {}, True), ("run_concepts", {}, True),
    ("node_abort", {}, True), ("comment_edited", {}, True), ("comment_resolution_changed", {}, True),
    ("hint", {"text": "t", "replace": True}, True), ("hint", {"text": "t", "replace": "yes"}, True),
    ("hint", {"text": "t"}, True), ("hint", {"text": "t", "replace": False}, True),
    ("pause", {}, False), ("annotation", {}, False), ("comment_created", {}, False),
])
def test_the_agent_token_rule_s_truth_table(kind, data, refused):
    got = agent_token_refusal(kind, data)
    assert (got is not None) is refused, (kind, data)
    if got is not None:
        assert got.status_code == 403 and got.detail["code"] == "agent_token_refused"
        assert got.detail["retryable"] is False


def test_the_allow_list_starts_nothing_and_is_all_the_token_keeps():
    """Every control intent the agent token keeps on an internal run is one no admission can spawn an
    engine for (`run_commands.py::admission_spawns_driver`), and it keeps exactly the allow-list — so
    an intent added later is refused to the token until someone adds it there."""
    from looplab.serve.run_commands import admission_spawns_driver
    for kind, spec in CONTROL_SPECS.items():
        admitted = agent_token_refusal(kind, {}) is None
        assert admitted is (kind in AGENT_TOKEN_INTERNAL_INTENTS), kind
        if admitted:
            assert not (admission_spawns_driver(spec.engine_policy, alive=False)
                        or admission_spawns_driver(spec.engine_policy, alive=True)), kind


# The words the manifest and the guide use for each intent the agent token keeps (crit_v61 M1: the
# docs named five while the rule kept twenty).
_KEPT_WORDS = {"pause": "pause", "annotation": "annotate", "comment_created": "add a comment"}


def test_the_manifest_and_the_guide_name_what_the_token_keeps():
    """The harness manifest's credential promise and the harness guide name the allow-list — the
    same set, both ways. MUTATION: widen the allow-list without the docs -> red."""
    from pathlib import Path

    from looplab.harness.manifest import harness_manifest
    assert set(_KEPT_WORDS) == set(AGENT_TOKEN_INTERNAL_INTENTS)
    credential = harness_manifest()["external_run_mode"]["credential"]
    guide = (Path(__file__).resolve().parents[1] / "docs" / "guide"
             / "external-harness.md").read_text(encoding="utf-8")
    scope = " ".join(guide.split("## Scope and provenance", 1)[1].split("\n## ", 1)[0].split())
    for word in set(_KEPT_WORDS.values()):
        assert word in credential, (word, credential)
        assert word in scope, (word, scope)
    assert "agent_token_refused" in credential and "agent_token_refused" in scope
