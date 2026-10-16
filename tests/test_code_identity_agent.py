"""The agent surface of the run's code repository (doc 79 §4, 2026-10-16): worktree + merge as the
DEFAULT external-agent workflow, end to end through the real routes, the MCP tools and the composite
`submit_experiment`; a worktree on persistent disk outside a tmpfs run; and a foreign branch (a
Claude Code worktree branch of the OPERATOR's repository) merged onto the run's base — with the hard
invariant that LoopLab never writes into the operator's repository checked byte for byte. Real git
throughout."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import pytest

from looplab.core.config import Settings
from looplab.engine.code_identity import BASE_BRANCH, _digest, record_attempt, sync_run
from looplab.engine.code_repo import CodeRepo, CodeRepoError
from looplab.engine.code_worktree import (add_worktree, agent_backup, describe_worktrees,
                                          list_worktrees, overlay_from_worktree)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from tests.test_code_identity import _engine, _git, _repo_task

_NO_OBLIGATIONS = {"deep_research_every": -1, "report_every": 0, "track_hypotheses": False,
                   "concept_pivot": False, "concept_run_base": False, "cross_run_concepts": False,
                   "novelty_mode": "off", "foresight": False}
_OPEN: list = []


@pytest.fixture(autouse=True)
def _settle_commands():
    """A durable command's worker may still be finishing when a test returns (as in
    `tests/test_campaign_composite.py`): wait for terminal status before tmp_path goes."""
    yield
    from looplab.serve.protocol import COMMAND_TERMINAL_STATUSES
    try:
        for server, rd in _OPEN:
            deadline = time.time() + 15.0
            for record in sorted((rd / ".commands").glob("cmd_*.json")):
                while time.time() < deadline:
                    status = server.get(f"/api/runs/{rd.name}/commands/{record.stem}",
                                        headers={"X-LoopLab-Token": "owner-secret"}).json()
                    if status.get("status") in COMMAND_TERMINAL_STATUSES:
                        break
                    time.sleep(0.02)
    finally:
        _OPEN.clear()


def _harness(root: Path, rd: Path, monkeypatch):
    """The real app on `root` and a HarnessAPI (the MCP client's HTTP half) wired to it with the
    SCOPED agent token, so every call is authorized exactly as an external agent's would be."""
    from fastapi.testclient import TestClient
    from looplab.harness.mcp_server import HarnessAPI
    from looplab.serve.run_commands import RunCommandService
    from looplab.serve.server import make_app
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    app = make_app(root)
    srv = app.state.looplab
    srv.commands = RunCommandService(
        srv, spawn_engine=lambda args, env=None, run_dir=None: None,
        startup_timeout=0.2, command_timeout=0.4, poll_interval=0.01, max_observation_timeout=0.8)
    server = TestClient(app)
    _OPEN.append((server, rd))

    def forward(request):
        reply = server.request(request.method, request.url.raw_path.decode(), content=request.content,
                               headers={k: v for k, v in request.headers.items()
                                        if k.lower() in ("content-type", "x-looplab-token",
                                                         "idempotency-key")})
        return httpx.Response(reply.status_code, content=reply.content,
                              headers={"content-type": reply.headers.get("content-type", "")})
    return server, HarnessAPI("http://testserver/", "agent-secret",
                              transport=httpx.MockTransport(forward))


def _tool(server, name: str, args: dict) -> dict:
    """Call one MCP tool and return its JSON result (structured content when the SDK gives it)."""
    result = anyio.run(server.call_tool, name, args)
    if isinstance(result, tuple):
        result = result[1] if isinstance(result[1], dict) else result[0]
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    content = getattr(result, "content", result)
    return json.loads(content[0].text)


# ----------------------------------------------------------------- 1+2: MCP tools + submit_experiment

def test_worktree_merge_and_submit_experiment_land_both_nodes_changes(tmp_path, monkeypatch):
    """The default workflow, end to end: MCP `code_worktree` from node 0, `git merge nodes/n1` in
    it, MCP `code_overlay` previews, `submit_experiment(code_from=worktree, parent_ids=[0, 1])`
    injects ONE candidate carrying node 0's change, node 1's change and the agent's own edit."""
    pytest.importorskip("mcp")
    from looplab.harness import composite
    from looplab.harness.mcp_server import build_server
    from tests.test_upstream_lane import fixture
    _, store, _, _ = fixture(tmp_path)
    rd = tmp_path / "run"
    (rd / "config.snapshot.json").write_text(Settings(backend="toy", external_harness=True,
                                                      **_NO_OBLIGATIONS).model_dump_json())
    # A second measured experiment, node 1, whose change is its own README edit.
    store.append("node_created", {"node_id": 1, "operator": "draft", "parent_ids": [],
                                  "idea": {"operator": "draft"},
                                  "files": {"README.md": "Runner, documented\n"}})
    store.append("node_evaluated", {"node_id": 1, "metric": 1.0, "task_metric": 1.0,
                                    "eval_seconds": 0.1, "violations": []})
    events = store.read_all()
    seed = next(e for e in events if e.type == "workspace_seeded" and e.data.get("node_id") == 0)
    made = record_attempt(rd, events, node_id=1, generation=0, seed_event_seq=seed.seq,
                          base_digest=_digest(seed.data["base_revision"]), source="workspace",
                          changes={"README.md": (b"Runner, documented\n", False)})
    assert made["status"] == "recorded", made
    generation = run_generation_token(store.read_all())
    server, api = _harness(tmp_path, rd, monkeypatch)
    mcp = build_server(api)

    made = _tool(mcp, "code_worktree", {"run_id": "run", "expected_generation": generation,
                                        "name": "combo", "start": "node:0"})
    assert made["status"] == 200, made
    wt = Path(made["body"]["path"])
    assert made["body"]["branch"] == "agent/combo" and "git merge nodes/n" in made["body"]["guide"]["combine"]
    _git(wt, "merge", "-q", "--no-edit", "nodes/n1")                 # combine experiment 1
    (wt / "recipe.env").write_text("MOMENTUM=0.3\n")                  # and the agent's own edit
    _git(wt, "commit", "-qam", "agent: momentum 0.3")
    listed = _tool(mcp, "code_worktrees", {"run_id": "run", "expected_generation": generation})
    assert [w["name"] for w in listed["body"]["worktrees"]] == ["combo"], listed
    assert listed["body"]["worktrees"][0]["head"] == _git(wt, "rev-parse", "HEAD").strip()
    preview = _tool(mcp, "code_overlay", {"run_id": "run", "expected_generation": generation,
                                          "worktree": "combo"})
    assert preview["status"] == 200 and preview["body"]["files_omitted"], preview
    assert sorted(preview["body"]["files"]) == ["README.md", "recipe.env", "train.py"]
    # A replaced run's generation is refused, not answered for the replacement.
    stale = _tool(mcp, "code_overlay", {"run_id": "run", "expected_generation": "0" * 64,
                                        "worktree": "combo"})
    assert stale["status"] == 409 and stale["body"]["detail"]["code"] == "run_generation_changed"

    idea = {"operator": "merge", "rationale": "momentum runner + documented README"}
    dry = composite.submit_experiment(api, "run", generation, key="M1", idea=idea,
                                      code_from={"worktree": "combo"}, parent_ids=[0, 1],
                                      dry_run=True)
    assert dry["ok"], dry
    assert dry["steps"][1]["step"] == "preview_code"
    assert sorted(dry["steps"][1]["changed"]) == ["README.md", "recipe.env", "train.py"]
    assert "inject_node" not in [e.type for e in store.read_all()]
    both = composite.submit_experiment(api, "run", generation, key="M1", idea=idea,
                                       code_from={"worktree": "combo"}, files={"x.py": "1"})
    assert not both["ok"] and any("not both" in m for m in both["steps"][-1]["missing"])
    out = composite.submit_experiment(api, "run", generation, key="M1", idea=idea,
                                      code_from={"worktree": "combo"}, parent_ids=[0, 1])
    assert out["ok"], out
    assert [s["step"] for s in out["steps"]] == ["read_progress", "preview_code", "inject"]
    inject = next(e for e in store.read_all() if e.type == "inject_node")
    assert inject.data["parent_ids"] == [0, 1] and "code_from" not in inject.data
    files = inject.data["files"]
    assert files["README.md"] == "Runner, documented\n"               # node 1's change
    assert files["recipe.env"] == "MOMENTUM=0.3\n"                    # the agent's edit
    assert files["train.py"] == fold(store.read_all()).nodes[0].files["train.py"]   # node 0's
    # The same key resumes: the inject replays, the preview is not re-judged.
    again = composite.submit_experiment(api, "run", generation, key="M1", idea=idea,
                                        code_from={"worktree": "combo"}, parent_ids=[0, 1])
    assert again["ok"] and again["steps"][1]["status"].startswith("skipped"), again
    assert [e.type for e in store.read_all()].count("inject_node") == 1
    # A refused overlay stops BEFORE anything is written.
    (wt / "score.py").write_text("print('{\"metric\": 0}')\n")       # the protected scorer
    refused = composite.submit_experiment(api, "run", generation, key="M2", idea=idea,
                                          code_from={"worktree": "combo"}, parent_ids=[0, 1])
    assert not refused["ok"] and refused["stopped_at"] == "preview_code", refused
    assert refused["steps"][-1]["detail"]["code"] == "code_overlay_outside_surface"
    assert [e.type for e in store.read_all()].count("inject_node") == 1


def test_the_code_tools_carry_their_hints():
    pytest.importorskip("mcp")
    from looplab.harness.mcp_server import HarnessAPI, build_server
    api = HarnessAPI("http://127.0.0.1:8765", transport=httpx.MockTransport(
        lambda request: pytest.fail("listing local tools contacted the UI")))
    tools = {t.name: t for t in anyio.run(build_server(api).list_tools)}
    hints = {n: tools[n].annotations.model_dump(by_alias=True)
             for n in ("code_worktree", "code_worktrees", "code_overlay")}
    assert hints["code_worktrees"]["readOnlyHint"] and hints["code_worktrees"]["idempotentHint"]
    assert not hints["code_worktree"]["readOnlyHint"] and not hints["code_worktree"]["destructiveHint"]
    assert not hints["code_overlay"]["destructiveHint"] and hints["code_overlay"]["idempotentHint"]
    for name in hints:
        assert tools[name].model_dump(by_alias=True)["inputSchema"]["required"][:2] == ["run_id", "expected_generation"]


# ------------------------------------------------------------------ 3: a worktree on persistent disk

def test_a_worktree_outside_the_run_survives_a_tmpfs_wipe_and_is_refused_in_the_source(
        tmp_path):
    """`path=` puts the worktree on persistent disk: never inside the operator's source tree nor
    another subtree of the run. The mirror bundles its BRANCH but not its files (they are on the
    disk that survives), and after the wipe + restore + sync the very same directory is re-linked
    to the rebuilt repository — committed history, uncommitted edits and `git status` intact."""
    from looplab.cli import app
    from looplab.events.run_mirror import MirrorConfig, mirror_pass, restore_run
    from typer.testing import CliRunner
    repo, task = _repo_task(tmp_path)
    r, d = task.build_roles()
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree
    rd = tmp_path / "src" / "run"
    anyio.run(Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                     policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    # What `looplab run` writes beside the log: where the operator's source is.
    (rd / "task.snapshot.json").write_text(task.model_dump_json(), encoding="utf-8")
    operator_status = _git(repo, "status", "--porcelain")
    for bad, code in ((repo / "agentwt", "code_worktree_path_refused"),       # the source tree
                      (tmp_path, "code_worktree_path_refused"),               # contains it
                      (rd / "nodes" / "wt", "code_worktree_path_refused"),    # engine-owned
                      (Path("relative/wt"), "code_worktree_path_invalid")):
        with pytest.raises(CodeRepoError) as caught:
            add_worktree(rd, "bad", path=bad)
        assert caught.value.code == code, (bad, caught.value)
    assert not (repo / "agentwt").exists() and _git(repo, "status", "--porcelain") == operator_status
    snapshot = rd / "task.snapshot.json"
    snapshot.rename(rd / "task.moved")           # where the source is cannot be read: fail closed
    with pytest.raises(CodeRepoError) as caught:
        add_worktree(rd, "bad", path=tmp_path / "elsewhere")
    assert caught.value.code == "code_worktree_path_unverifiable"
    (rd / "task.moved").rename(snapshot)

    persist = tmp_path / "persist" / "wt"
    cli = CliRunner().invoke(app, ["code-worktree", str(rd), "keep", "--path", str(persist)])
    assert cli.exit_code == 0, cli.output
    meta = json.loads(cli.output)
    assert Path(meta["path"]) == persist.resolve() and meta["outside_run"] and meta["guide"]
    (persist / "train.py").write_text("print('LR: 0.04')\n")
    _git(persist, "commit", "-qam", "persistent proposal")
    head = _git(persist, "rev-parse", "HEAD").strip()
    (persist / "recipe.env").write_text("SEED=4\n")                   # uncommitted, on that disk
    status = _git(persist, "status", "--porcelain")
    before = overlay_from_worktree(rd, "keep")
    assert before["files"] == {"train.py": "print('LR: 0.04')\n", "recipe.env": "SEED=4\n"}
    assert describe_worktrees(rd)[0]["outside_run"] is True

    cfg = MirrorConfig(src_root=tmp_path / "src", dst_root=tmp_path / "dst", max_bytes_per_s=0,
                       max_files_per_s=0, run_extras=agent_backup)
    assert not mirror_pass(cfg).errors
    index = json.loads((cfg.dst_root / "run" / "code" / "agent_backup" / "agents.json").read_text())
    row = index["worktrees"][0]
    assert row["outside_run"] and row["dirty_commit"] is None and not row.get("files")
    assert index["refs"]["refs/heads/agent/keep"] == head             # the branch IS bundled

    shutil.rmtree(tmp_path / "src")                                   # the pod restart wipes tmpfs
    os.makedirs(tmp_path / "src")
    assert not restore_run(cfg.dst_root, "run", cfg.src_root)["failures"]
    report = sync_run(rd, restore_agents=True)["agent_worktrees"]
    assert report["restored"] == ["keep"] and not report["pending"], report
    assert [m["path"] for m in list_worktrees(rd)] == [str(persist.resolve())]
    assert _git(persist, "rev-parse", "HEAD").strip() == head
    assert _git(persist, "symbolic-ref", "HEAD").strip() == "refs/heads/agent/keep"
    assert (persist / "recipe.env").read_text() == "SEED=4\n"
    assert _git(persist, "status", "--porcelain") == status
    after = overlay_from_worktree(rd, "keep")
    assert (after["files"], after["deleted"]) == (before["files"], before["deleted"])
    assert CodeRepo(rd).rev("refs/heads/agent/keep") == head


# --------------------------------------------------------------------- 4: bring your own git branch

def _operator_fingerprint(repo: Path) -> dict:
    """Everything LoopLab must never write in the operator's repository: refs, objects, config,
    index, hooks, HEAD — and the worktree admin entries."""
    git_dir = repo / ".git"
    return {
        "refs": _git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "head": (git_dir / "HEAD").read_bytes(),
        "objects": sorted(str(p.relative_to(git_dir)) for p in (git_dir / "objects").rglob("*")),
        "config": (git_dir / "config").read_bytes(),
        "index": ((git_dir / "index").read_bytes(), (git_dir / "index").stat().st_mtime_ns),
        "hooks": sorted(p.name for p in (git_dir / "hooks").glob("*")),
        "worktrees": sorted((str(p.relative_to(git_dir)), p.read_bytes())
                            for p in (git_dir / "worktrees").rglob("*") if p.is_file()),
    }


def _foreign_branch(tmp_path, repo: Path, edit) -> tuple:
    """A Claude-Code-style worktree of the OPERATOR's repository on `worktree-agent-x`, cut from
    its HEAD (the commit the run recorded), with `edit(dir)` committed on it."""
    wt = tmp_path / "claude-wt"
    _git(repo, "worktree", "add", "-q", "-b", "worktree-agent-x", str(wt))
    edit(wt)
    _git(wt, "commit", "-qam", "agent work")
    return wt, _git(wt, "rev-parse", "HEAD").strip()


def _external_run(tmp_path, monkeypatch):
    repo, task = _repo_task(tmp_path)
    eng = _engine(tmp_path, task)
    anyio.run(eng.run)
    rd = tmp_path / "run"
    (rd / "task.snapshot.json").write_text(task.model_dump_json(), encoding="utf-8")
    (rd / "config.snapshot.json").write_text(Settings(backend="toy", external_harness=True,
                                                      **_NO_OBLIGATIONS).model_dump_json())
    return repo, task, rd


def test_a_foreign_branch_merges_onto_the_base_and_its_repository_is_untouched(tmp_path,
                                                                              monkeypatch):
    """Clean case: the agent's branch changed `recipe.env`; the run's base holds the operator's
    UNCOMMITTED `train.py` edit and the untracked scorer. The candidate is the merge — the agent's
    change only, the operator's edits kept (never reverted) — with the drift named."""
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from looplab.serve.control_validation import normalize_control
    from looplab.serve.server import make_app
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    repo, task, rd = _external_run(tmp_path, monkeypatch)
    wt, sha = _foreign_branch(tmp_path, repo, lambda d: (d / "recipe.env").write_text("SEED=5\n"))
    fingerprint = _operator_fingerprint(repo)
    foreign = {"repo": str(repo), "ref": "worktree-agent-x"}
    with TestClient(make_app(tmp_path)) as client:
        preview = client.post("/api/runs/run/code/overlay", json={"git": foreign})
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["files"] == {"recipe.env": "SEED=5\n"} and body["deleted"] == []
        assert body["foreign"]["commit"] == sha and body["foreign"]["fetched_ref"].endswith(sha)
        assert body["foreign"]["base_drift"]["paths"] == ["score.py", "train.py"]
        assert "your branch lacks 2 uncommitted file(s) of the source" in body["warnings"][0]
        # The worktree path itself (a gitfile directory) and a full commit id work alike.
        by_dir = client.post("/api/runs/run/code/overlay",
                             json={"git": {"repo": str(wt), "ref": sha}, "include_files": False})
        assert by_dir.status_code == 200 and by_dir.json()["files"] == {"recipe.env": 7}
        bad = client.post("/api/runs/run/code/overlay",
                          json={"git": {"repo": str(repo), "ref": "--upload-pack=x"}})
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "code_from_git_ref"
    srv = SimpleNamespace(run_dir=lambda run_id: tmp_path / run_id,
                          state=lambda path: fold(EventStore(Path(path) / "events.jsonl").read_all()))
    data = normalize_control(srv, rd, "inject_node", {"idea": {"operator": "draft"},
                                                      "code_from": {"git": foreign}})
    assert data["files"] == {"recipe.env": "SEED=5\n"} and "code_from" not in data
    with pytest.raises(HTTPException) as caught:
        normalize_control(srv, rd, "inject_node", {"idea": {"operator": "draft"},
                                                   "code_from": {"git": foreign, "patch": "x"}})
    assert caught.value.detail["code"] == "code_from_invalid"
    # Read-only on the operator's repository: refs, objects, config, index, hooks, worktrees.
    assert _operator_fingerprint(repo) == fingerprint
    assert " M train.py" in _git(repo, "status", "--porcelain")      # its uncommitted edit stays
    assert CodeRepo(rd).rev(f"refs/looplab/foreign/{sha}") == sha     # fetched into the run's repo


def test_an_overlap_with_the_operators_uncommitted_edit_is_a_named_conflict(tmp_path, monkeypatch):
    """The agent's branch (cut from HEAD) rewrote the LR line the operator had edited without
    committing — the base has `LR: 0.02`, the branch `LR: 0.05`. Taking the branch's tree would
    silently revert the operator's edit; the merge names the path instead (409)."""
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from looplab.serve.control_validation import normalize_control
    from looplab.serve.server import make_app
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    repo, task, rd = _external_run(tmp_path, monkeypatch)
    _foreign_branch(tmp_path, repo, lambda d: (d / "train.py").write_text(
        "print('LR: 0.05')\nprint('Batch: 32 (micro=8)')\n"))
    fingerprint = _operator_fingerprint(repo)
    foreign = {"repo": str(repo), "ref": "worktree-agent-x"}
    with TestClient(make_app(tmp_path)) as client:
        refused = client.post("/api/runs/run/code/overlay", json={"git": foreign})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "code_from_merge_conflict"
    assert refused.json()["detail"]["paths"] == ["train.py"]
    srv = SimpleNamespace(run_dir=lambda run_id: tmp_path / run_id,
                          state=lambda path: fold(EventStore(Path(path) / "events.jsonl").read_all()))
    with pytest.raises(HTTPException) as caught:
        normalize_control(srv, rd, "inject_node", {"idea": {"operator": "draft"},
                                                   "code_from": {"git": foreign}})
    assert caught.value.status_code == 409 and caught.value.detail["paths"] == ["train.py"]
    assert _operator_fingerprint(repo) == fingerprint
    assert CodeRepo(rd).blob(CodeRepo(rd).rev(BASE_BRANCH), "train.py").startswith(b"print('LR: 0.02')")


def test_a_git_older_than_merge_tree_write_tree_is_refused_by_name(tmp_path, monkeypatch):
    import looplab.engine.code_foreign as code_foreign
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    repo, task, rd = _external_run(tmp_path, monkeypatch)
    _foreign_branch(tmp_path, repo, lambda d: (d / "recipe.env").write_text("SEED=5\n"))
    fingerprint = _operator_fingerprint(repo)
    assert code_foreign.git_version() >= code_foreign.MIN_GIT          # the real probe reads
    monkeypatch.setattr(code_foreign, "git_version", lambda: (2, 37))
    with TestClient(make_app(tmp_path)) as client:
        old = client.post("/api/runs/run/code/overlay",
                          json={"git": {"repo": str(repo), "ref": "worktree-agent-x"}})
    assert old.status_code == 400 and old.json()["detail"]["code"] == "code_from_git_unsupported"
    assert "2.37" in old.json()["detail"]["message"]
    assert _operator_fingerprint(repo) == fingerprint
    assert not CodeRepo(rd).refs("refs/looplab/foreign/")              # nothing was fetched


def test_an_editable_in_a_subdirectory_merges_at_its_prefix(tmp_path):
    """The run's tree is the EDITABLE's (here `pkg/` of the operator's repository), the branch's
    is the whole repository's: the merge is taken at the editable's prefix, so the branch's
    `pkg/recipe.env` is the candidate's `recipe.env`, and a change outside `pkg/` is not in it."""
    import sys
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.engine.code_foreign import overlay_from_git
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "recipe.env").write_text("SEED=1\n")
    (repo / "pkg" / "train.py").write_text("print('LR: 0.01')\n")
    (repo / "README.md").write_text("top\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    (repo / "pkg" / "score.py").write_text('import json\nprint(json.dumps({"metric": 1.0}))\n')
    task = RepoTask(id="p", direction="max", editable_path=str(repo / "pkg"), protect=["score.py"],
                    edit_surface=["train.py", "recipe.env"],
                    eval=EvalSpec(command=[sys.executable, "score.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    anyio.run(_engine(tmp_path, task).run)
    rd = tmp_path / "run"
    (rd / "task.snapshot.json").write_text(task.model_dump_json(), encoding="utf-8")

    def edit(d):
        (d / "pkg" / "recipe.env").write_text("SEED=6\n")
        (d / "README.md").write_text("top, edited\n")
    _foreign_branch(tmp_path, repo, edit)
    fingerprint = _operator_fingerprint(repo)
    out = overlay_from_git(rd, {"repo": str(repo), "ref": "worktree-agent-x"})
    assert out["files"] == {"recipe.env": "SEED=6\n"} and out["foreign"]["prefix"] == "pkg"
    assert out["foreign"]["base_drift"]["paths"] == ["score.py"]
    assert _operator_fingerprint(repo) == fingerprint
