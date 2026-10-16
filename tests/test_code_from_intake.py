"""The `code_from` intake, the `/code/*` routes and the composite over them (review 2026-10-16),
driven through the REAL app, the durable command route and the MCP client's HTTP half, with real
git throughout (`serve/code_from.py` is the one conversion both the intake and the preview run)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from tests.test_code_identity import _git
from tests.test_code_identity_agent import _NO_OBLIGATIONS, _external_run, _foreign_branch, \
    _harness, _settle_commands, _tool  # noqa: F401 — the autouse settle fixture rides along

OWNER = {"X-LoopLab-Token": "owner-secret"}


def _lane_run(tmp_path, monkeypatch):
    from tests.test_upstream_lane import fixture
    lane, store, generation, proposal = fixture(tmp_path)
    rd = tmp_path / "run"
    (rd / "config.snapshot.json").write_text(Settings(backend="toy", external_harness=True,
                                                      **_NO_OBLIGATIONS).model_dump_json())
    server, api = _harness(tmp_path, rd, monkeypatch)
    return lane, store, proposal, rd, server, api


def _inject(server, rd, key, data, headers=OWNER):
    generation = run_generation_token(EventStore(rd / "events.jsonl").read_all())
    return server.post(f"/api/runs/{rd.name}/commands", headers={**headers, "Idempotency-Key": key},
                       json={"type": "inject_node", "expected_generation": generation,
                             "data": {"idea": {"operator": "draft"}, **data}})


def test_code_from_refusals_are_named_and_a_transient_one_is_not_recorded(tmp_path, monkeypatch):
    """Items 1/2/4/6/7: an EMPTY change is refused (it read as "no code" and bought a Developer
    build); `code` beside code_from conflicts; `start` beside a worktree is refused and a patch's
    None start means base on BOTH the preview and the intake; a missing worktree directory is a 409
    by name, not git's 503; the generation fence takes an uppercase token; and a git timeout is a
    503 with NO durable record, so the same key retries — while the MCP preview of it stays an
    unavailable read, never an unknown write outcome."""
    from looplab.engine.code_repo import CodeRepoError
    from looplab.harness.mcp_server import build_server
    lane, store, _, rd, server, api = _lane_run(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    made = server.post("/api/runs/run/code/worktrees", headers=OWNER,
                       json={"name": "w", "expected_generation": generation.upper()})
    assert made.status_code == 200, made.text
    wt = Path(made.json()["path"])
    listed = server.get("/api/runs/run/code/worktrees", headers=OWNER,
                        params={"expected_generation": generation.upper()})
    assert listed.status_code == 200 and listed.json()["generation"] == generation

    empty = server.post("/api/runs/run/code/overlay", headers=OWNER, json={"worktree": "w"})
    assert empty.status_code == 400 and empty.json()["detail"]["code"] == "code_from_empty"
    record = _inject(server, rd, "e1", {"code_from": {"worktree": "w"}}).json()
    assert record["status"] == "rejected" and record["error"]["code"] == "code_from_empty"

    (wt / "recipe.env").write_text("MOMENTUM=0.4\n")
    both = _inject(server, rd, "c1", {"code_from": {"worktree": "w"}, "code": "print(1)"}).json()
    assert both["status"] == "rejected" and both["error"]["code"] == "code_from_conflict"
    started = _inject(server, rd, "s1", {"code_from": {"worktree": "w", "start": "node:0"}}).json()
    assert started["status"] == "rejected" and started["error"]["code"] == "code_from_invalid"
    assert server.post("/api/runs/run/code/overlay", headers=OWNER,
                       json={"worktree": "w", "start": "base"}).status_code == 400
    patch = "--- a/recipe.env\n+++ b/recipe.env\n@@ -1 +1 @@\n-MOMENTUM=0.0\n+MOMENTUM=0.7\n"
    preview = server.post("/api/runs/run/code/overlay", headers=OWNER,
                          json={"patch": patch, "start": None})
    assert preview.status_code == 200 and preview.json()["files"] == {"recipe.env": "MOMENTUM=0.7\n"}

    # A transient failure: 503, no record, and the SAME key then succeeds.
    import looplab.serve.code_from as code_from_module
    real = code_from_module.compute_overlay

    def timed_out(*args, **kwargs):
        raise code_from_module.code_http_error(CodeRepoError("git_timeout", "git fetch exceeded"))
    monkeypatch.setattr(code_from_module, "compute_overlay", timed_out)
    transient = _inject(server, rd, "p1", {"code_from": {"patch": patch, "start": None}})
    assert transient.status_code == 503 and transient.json()["detail"]["code"] == "git_timeout"
    mcp = build_server(api)
    unavailable = _tool(mcp, "code_overlay", {"run_id": "run", "expected_generation": generation,
                                              "worktree": "w"})
    assert unavailable["status"] == 503 and unavailable.get("code") != "request_outcome_unknown"
    monkeypatch.setattr(code_from_module, "compute_overlay", real)
    accepted = _inject(server, rd, "p1", {"code_from": {"patch": patch, "start": None}}).json()
    assert accepted["status"] != "rejected", accepted

    # A worktree whose directory is gone: named, 409, through the route and the MCP tool.
    server.post("/api/runs/run/code/worktrees", headers=OWNER, json={"name": "gone"})
    shutil.rmtree(rd / "code" / "worktrees" / "gone")
    missing = server.post("/api/runs/run/code/overlay", headers=OWNER, json={"worktree": "gone"})
    assert missing.status_code == 409 and missing.json()["detail"]["code"] == "code_worktree_missing"
    via_mcp = _tool(mcp, "code_overlay", {"run_id": "run", "expected_generation": generation,
                                          "worktree": "gone"})
    assert via_mcp["status"] == 409 and via_mcp["body"]["detail"]["code"] == "code_worktree_missing"
    assert [e.type for e in EventStore(rd / "events.jsonl").read_all()].count("inject_node") <= 1


def test_a_change_made_on_an_advanced_base_is_refused_with_the_paths(tmp_path, monkeypatch):
    """Item 5: a worktree made on base B1, then an upstream advance to B2 that changed README.md.
    Whole files from B1 would silently revert B2 there, so a change touching README.md is refused
    (preview and intake alike, the rejected record keeping the paths); a change elsewhere is not;
    after `git merge base` the same worktree is accepted."""
    lane, store, proposal, rd, server, api = _lane_run(tmp_path, monkeypatch)
    wt = Path(server.post("/api/runs/run/code/worktrees", headers=OWNER,
                          json={"name": "old"}).json()["path"])
    other = Path(server.post("/api/runs/run/code/worktrees", headers=OWNER,
                             json={"name": "elsewhere"}).json()["path"])
    generation = run_generation_token(store.read_all())
    made = lane.propose(proposal)
    checked = lane.check({"expected_generation": generation, "action_id": "c1",
                          "proposal_id": made["proposal_id"]})
    assert checked["status"] == "succeeded", checked
    lane.advance({"expected_generation": generation, "action_id": "a1",
                  "proposal_id": made["proposal_id"],
                  "expected_base_revision": proposal["expected_base_revision"],
                  "evidence_token": checked["evidence_token"]})
    (wt / "README.md").write_text("Runner, my way\n")
    _git(wt, "commit", "-qam", "agent: readme")
    stale = server.post("/api/runs/run/code/overlay", headers=OWNER, json={"worktree": "old"})
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "code_from_stale_base"
    assert stale.json()["detail"]["paths"] == ["README.md"]
    record = _inject(server, rd, "st1", {"code_from": {"worktree": "old"}}).json()
    assert record["status"] == "rejected" and record["error"]["code"] == "code_from_stale_base"
    assert record["error"]["paths"] == ["README.md"]          # kept on the durable rejection
    (other / "recipe.env").write_text("MOMENTUM=0.5\n")
    fine = server.post("/api/runs/run/code/overlay", headers=OWNER, json={"worktree": "elsewhere"})
    assert fine.status_code == 200 and fine.json()["files"] == {"recipe.env": "MOMENTUM=0.5\n"}
    _git(wt, "merge", "-q", "--no-edit", "-X", "theirs", "base")
    merged = server.post("/api/runs/run/code/overlay", headers=OWNER, json={"worktree": "old"})
    assert merged.status_code == 200, merged.text


def test_the_agent_token_may_merge_only_the_runs_own_source_repository(tmp_path, monkeypatch):
    """SEC: `code_from.git.repo` was any absolute path the server could read, fetched into the run
    and checkable out from a run worktree. The agent token may name the run's source repository or
    a worktree of it; another repository — even a clone with the same history — is 403, at the
    preview, through MCP and at the intake (a recorded rejection). The owner may name it."""
    from looplab.harness.mcp_server import build_server
    repo, _, rd = _external_run(tmp_path, monkeypatch)
    wt, _ = _foreign_branch(tmp_path, repo, lambda d: (d / "recipe.env").write_text("SEED=5\n"))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    _git(clone, "checkout", "-q", "-b", "elsewhere")
    (clone / "recipe.env").write_text("SEED=6\n")
    _git(clone, "commit", "-qam", "clone work")
    server, api = _harness(tmp_path, rd, monkeypatch)
    generation = run_generation_token(EventStore(rd / "events.jsonl").read_all())
    mcp = build_server(api)
    own = _tool(mcp, "code_overlay", {"run_id": "run", "expected_generation": generation,
                                      "git": {"repo": str(wt), "ref": "worktree-agent-x"}})
    assert own["status"] == 200, own
    foreign = {"repo": str(clone), "ref": "elsewhere"}
    refused = _tool(mcp, "code_overlay", {"run_id": "run", "expected_generation": generation,
                                          "git": foreign})
    assert refused["status"] == 403
    assert refused["body"]["detail"]["code"] == "code_from_git_not_permitted"
    record = _inject(server, rd, "g1", {"code_from": {"git": foreign}},
                     headers={"X-LoopLab-Token": "agent-secret"}).json()
    assert record["status"] == "rejected"
    assert record["error"]["code"] == "code_from_git_not_permitted"
    owner = server.post("/api/runs/run/code/overlay", headers=OWNER,
                        json={"git": foreign, "include_files": False})
    assert owner.status_code == 200 and owner.json()["files"] == {"recipe.env": 7}, owner.text


def test_a_rejected_inject_is_not_replayed_forever_by_a_same_key_resume(tmp_path, monkeypatch):
    """Item 3: the first submit's preview passes and its inject is REJECTED (a research question
    the board does not hold). Repeating with the same key used to skip the preview, replay the
    rejection and say "repeat with the SAME key" again — forever. It now stops at once, naming the
    rejection and telling the agent to use a NEW key; the new key then lands."""
    from looplab.harness import composite
    lane, store, _, rd, server, api = _lane_run(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    wt = Path(server.post("/api/runs/run/code/worktrees", headers=OWNER,
                          json={"name": "w"}).json()["path"])
    (wt / "recipe.env").write_text("MOMENTUM=0.4\n")
    idea = {"operator": "draft", "parent_card_id": "no-such-question"}
    first = composite.submit_experiment(api, "run", generation, key="K1", idea=idea,
                                        code_from={"worktree": "w"})
    assert not first["ok"] and first["stopped_at"] == "inject", first
    assert "NEW key" in first["message"]
    again = composite.submit_experiment(api, "run", generation, key="K1", idea=idea,
                                        code_from={"worktree": "w"})
    assert not again["ok"] and again["stopped_at"] == "inject"
    assert "preview_code" not in [s["step"] for s in again["steps"]]
    assert again["steps"][-1]["status"] == "rejected" and "NEW key" in again["message"]
    landed = composite.submit_experiment(api, "run", generation, key="K2",
                                         idea={"operator": "draft"}, code_from={"worktree": "w"})
    assert landed["ok"], landed


def test_phase_info_does_not_offer_the_fields_the_intake_refuses_as_forged(tmp_path):
    """Item 7: `origin` and `code_commit` are STAMPED by the server and refused when submitted
    (`origin_forged`, `code_commit_forged`); phase_info listed them as request fields."""
    pytest.importorskip("mcp")
    from fastapi import HTTPException
    from looplab.harness.mcp_server import HarnessAPI, build_server
    from looplab.serve.control_validation import normalize_control
    from looplab.serve.protocol import CONTROL_SERVER_STAMPED_FIELDS
    mcp = build_server(HarnessAPI("http://127.0.0.1:1/", "agent-secret"))
    info = _tool(mcp, "phase_info", {"phase_id": "proposal"})["commands"]["inject_node"]
    stamped = CONTROL_SERVER_STAMPED_FIELDS["inject_node"]
    assert stamped == {"origin", "code_commit"}
    assert not stamped & set(info["request_fields"]) and stamped <= set(info["server_derived"])
    for field in stamped:
        with pytest.raises(HTTPException) as caught:
            normalize_control(None, tmp_path, "inject_node",
                              {"idea": {"operator": "draft"}, field: "x"})
        assert caught.value.detail["code"] == f"{field}_forged"
