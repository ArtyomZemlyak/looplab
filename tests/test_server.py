"""Live UI server (the [ui] extra). Skipped entirely when fastapi isn't installed, so the base
offline suite is unaffected. Builds a real finished run, then exercises the read API, time-travel,
node detail, the control append, and config masking through FastAPI's TestClient.
"""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import builtins  # noqa: E402

from looplab.engine.orchestrator import Engine  # noqa: E402
from looplab.search.policy import GreedyTree  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.events.eventstore import EventStore, iter_event_jsonl, iter_jsonl  # noqa: E402
from looplab.runtime.sandbox import SubprocessSandbox  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from looplab.adapters.toytask import ToyTask  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def _build_run(root: Path, name: str = "demo"):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(root / name, task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4))
    return anyio.run(eng.run)


def _make_resumable(rd: Path) -> Path:
    snap = rd / "task.snapshot.json"
    snap.write_text(TASK.read_text(encoding="utf-8"), encoding="utf-8")
    return snap


def test_state_exposes_deprecated_hypotheses_compat_projection(tmp_path):
    # Peer review: the derived hypothesis board was removed from RunState, but the /state CONTRACT
    # retirement was deferred to a deprecation window. /state must still expose a read-only `hypotheses`
    # projection (from the Card belief board, in the old shape) so a client can tell "known empty" from
    # "schema removed" and keep working until it migrates to `cards`.
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    EventStore(rd / "events.jsonl").append(
        "card_added", {"id": "card-belief", "statement": "log-transform the target",
                       "source": "researcher"})
    client = TestClient(make_app(tmp_path))
    state = client.get("/api/runs/demo/state").json()["state"]
    assert "hypotheses" in state                                 # the compat key is present...
    hyp = state["hypotheses"]["card-belief"]
    assert hyp["statement"] == "log-transform the target"        # ...in the old Hypothesis shape
    assert hyp["source"] == "researcher" and hyp["status"] == "open"   # no evidence yet -> open
    assert set(hyp) == {"id", "statement", "source", "status", "evidence",
                        "rationale", "created_at_node", "best_delta", "priority"}


def _delete_run(client, run_id: str, *, rd: Path, op: str = "1" * 8):
    """Delete a run through the operation-bound transaction that replaced bodyless DELETE.

    `DELETE /api/runs/{id}` is now a 409 stub ("deletion_identity_required"): a bodyless request
    could otherwise destroy a REPLACEMENT generation the caller never inspected. The real route is
    `POST /api/runs/{id}/deletions` carrying the exact generation + log tail the caller saw, plus a
    client-minted operation id so a retried request replays one receipt instead of deleting twice.

    A test that keeps calling the stub collects its 409 for free and stops exercising the
    destructive boundary at all — every guard downstream becomes unreachable, so the test passes
    while proving nothing. Read the identity from DISK so this works on runs no view has listed."""
    from looplab.serve.run_commands import run_generation_token
    from looplab.events.eventstore import EventStore

    events = EventStore(rd / "events.jsonl").read_all()
    return client.post(f"/api/runs/{run_id}/deletions", json={
        "operation_id": f"{op}-1111-4111-8111-{'1' * 12}",
        "expected_generation": run_generation_token(events),
        "expected_seq": events[-1].seq if events else -1,
    })


def _replacement_spawn(rd: Path, *, pid: int = 9101, task_id: str = "replacement"):
    """A `_spawn_engine` stand-in that behaves like a real Replay child: it writes the
    generation-defining first event.

    Replay reports success only once a REPLACEMENT generation is durably visible
    (`complete_reset_if_observed`), so a fake that merely returns a pid leaves the transaction at
    `phase="popen_returned"` and the route answers 425 forever. Writing that event needs the
    operation fence: while the reset marker exists, `EventStore` refuses every writer whose
    `RUN_RESET_OPERATION_ENV` does not match the marker's operation id
    (`core/run_reset.py::assert_run_reset_write_allowed`, read from the process environment). Adopt
    the value the route froze into the child env rather than inventing an id the marker rejects."""
    import os

    from looplab.core.run_reset import RUN_RESET_OPERATION_ENV
    from looplab.events.eventstore import EventStore

    def spawn(*_args, env=None, **_kwargs):
        previous = os.environ.get(RUN_RESET_OPERATION_ENV)
        os.environ[RUN_RESET_OPERATION_ENV] = (env or {}).get(RUN_RESET_OPERATION_ENV, "")
        try:
            EventStore(rd / "events.jsonl").append("run_started", {
                "run_id": rd.name, "task_id": task_id, "goal": "new", "direction": "min"})
        finally:
            if previous is None:
                os.environ.pop(RUN_RESET_OPERATION_ENV, None)
            else:
                os.environ[RUN_RESET_OPERATION_ENV] = previous
        return pid

    return spawn


def _run_config_put(client, run_id: str, body: dict, *, generation: str | None = None):
    """PUT a run's settings carrying the run-generation fence the route now REQUIRES.

    `expected_generation` is validated inside the config lock, because a reset can land between the
    request arriving and the write — without it a stale tab silently re-configures the REPLACEMENT
    run. It is mandatory on both body variants (`RunConfigUpdateRequest` and the legacy flat one),
    so a PUT that omits it is rejected before the route reaches the revision CAS at all: a test that
    omits it stops exercising the CAS, the pinned-field rules, and the lock behaviour it was written
    for, and only proves that the request was malformed."""
    payload = dict(body)
    payload.setdefault(
        "expected_generation",
        generation if generation is not None
        else client.get(f"/api/runs/{run_id}/state").json()["generation"])
    return client.put(f"/api/runs/{run_id}/config", json=payload)


def _artifact_generation(client, run_id: str = "demo") -> str:
    """The run generation the artifact views are fenced to.

    `/artifacts` and `/artifact` are attempt-scoped now and require `expected_generation`, so a
    caller cannot be handed files from a run that was reset out from under the view it is showing.
    Omitting it is a 422, which reads as "the endpoint broke" rather than "this read must name the
    attempt it belongs to"."""
    return client.get(f"/api/runs/{run_id}/state").json()["generation"]


def test_artifacts_list_and_view(tmp_path):
    """Visible files: distinguish run workspace from live task paths, serve content, and stay bounded."""
    _build_run(tmp_path)                                   # tmp_path/demo with events.jsonl, nodes/, …
    rd = tmp_path / "demo"
    (rd / "out.txt").write_bytes(b"hello artifact\n")      # bytes: no CRLF translation, exact-content check
    (rd / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    # a RepoTask-style snapshot pointing at a SEPARATE repo path on disk (not under runs/)
    repo = tmp_path / "myrepo"
    (repo / "outputs").mkdir(parents=True)
    (repo / "train.py").write_text("print('train')\n", encoding="utf-8")
    (repo / "outputs" / "submission.csv").write_text("id,pred\n1,0.5\n", encoding="utf-8")
    (rd / "task.snapshot.json").write_text(
        json.dumps({"kind": "repo", "editable_path": str(repo)}), encoding="utf-8")

    client = TestClient(make_app(tmp_path))
    inventory = client.get("/api/runs/demo/artifacts", params={"expected_generation": _artifact_generation(client)}).json()
    assert inventory["inventory_semantics"] == "live_workspace_snapshot"
    roots = {r["id"]: r for r in inventory["roots"]}
    assert "run" in roots and "editable:." in roots         # run dir + the separate repo path
    assert roots["run"]["visibility"] == "run_workspace"
    assert roots["editable:."]["visibility"] == "live_task_path"
    run_files = {f["path"] for f in roots["run"]["files"]}
    assert {"out.txt", "blob.bin"} <= run_files
    repo_files = {f["path"] for f in roots["editable:."]["files"]}
    assert {"train.py", "outputs/submission.csv"} <= repo_files

    # view a text file in the run dir
    v = client.get("/api/runs/demo/artifact", params={"expected_generation": _artifact_generation(client), "root": "run", "path": "out.txt"}).json()
    assert v["is_text"] is True and v["content"] == "hello artifact\n"
    # view a file under the SEPARATE repo root (incl. a nested subdir)
    v2 = client.get("/api/runs/demo/artifact",
                    params={"expected_generation": _artifact_generation(client), "root": "editable:.", "path": "outputs/submission.csv"}).json()
    assert "id,pred" in v2["content"]
    # binary file → flagged, no inline content
    vb = client.get("/api/runs/demo/artifact", params={"expected_generation": _artifact_generation(client), "root": "run", "path": "blob.bin"}).json()
    assert vb["is_text"] is False and vb["content"] is None
    # path-traversal and unknown root are both rejected
    assert client.get("/api/runs/demo/artifact",
                      params={"expected_generation": _artifact_generation(client), "root": "run", "path": "../../secret"}).status_code == 404
    assert client.get("/api/runs/demo/artifact",
                      params={"expected_generation": _artifact_generation(client), "root": "nope", "path": "x"}).status_code == 404


def test_trace_internals_cannot_escape_through_artifact_aliases(tmp_path, monkeypatch):
    """Raw/derived trace families stay behind projections across every declared artifact root."""
    import builtins

    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    protected = [
        "spans.jsonl", "spans.index.jsonl", "trace.json", "tree.html",
        "spans.jsonl.reset-1", "trace.json.backup", "SPANS.INDEX.JSONL.OLD",
        ".spans.jsonl.atomic.tmp", ".tree.html.atomic.tmp",
    ]
    for name in protected:
        (rd / name).write_text(f"private:{name}\n", encoding="utf-8")
    protected_dir = rd / "tree.html.archive"
    protected_dir.mkdir()
    (protected_dir / "secret.txt").write_text("nested private trace\n", encoding="utf-8")
    (rd / "out.txt").write_text("safe run output\n", encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir()
    # Same basenames are legitimate when they are independent files outside the canonical run dir.
    (repo / "spans.jsonl").write_bytes(b"external spans\n")
    (repo / "trace.json").write_bytes(b"external trace\n")
    (repo / ".env").write_text("SECRET=must-not-render\n", encoding="utf-8")
    secret_dir = repo / ".aws"
    secret_dir.mkdir()
    (secret_dir / "credentials").write_text("must-not-render\n", encoding="utf-8")
    allowed = repo / "allowed.txt"
    allowed.write_text("allowed\n", encoding="utf-8")

    hardlink = repo / "raw-hardlink.txt"
    hardlink_supported = True
    try:
        os.link(rd / "spans.jsonl", hardlink)
    except OSError:
        hardlink_supported = False
    symlink = repo / "raw-symlink.txt"
    symlink_supported = True
    try:
        symlink.symlink_to(rd / "spans.jsonl")
    except OSError:
        symlink_supported = False

    (rd / "task.snapshot.json").write_text(json.dumps({
        "kind": "repo",
        "editable_path": str(repo),
        "references": [{"name": "parent", "path": str(tmp_path)}],
    }), encoding="utf-8")
    client = TestClient(make_app(tmp_path))
    roots = {r["id"]: r for r in client.get("/api/runs/demo/artifacts", params={"expected_generation": _artifact_generation(client)}).json()["roots"]}
    assert {"run", "editable:."} <= roots.keys()
    assert "reference:parent" not in roots
    run_files = {item["path"] for item in roots["run"]["files"]}
    repo_files = {item["path"] for item in roots["editable:."]["files"]}

    assert "out.txt" in run_files
    assert "tree.html.archive/secret.txt" not in run_files
    assert client.get("/api/runs/demo/artifact", params={"expected_generation": _artifact_generation(client), 
        "root": "run", "path": "tree.html.archive/secret.txt"}).status_code == 404
    assert client.get("/api/runs/demo/artifact", params={"expected_generation": _artifact_generation(client), 
        "root": "reference:parent", "path": "demo/out.txt"}).status_code == 404
    for name in protected:
        assert name not in run_files
        assert client.get("/api/runs/demo/artifact",
                          params={"expected_generation": _artifact_generation(client), "root": "run", "path": name}).status_code == 404

    assert {"spans.jsonl", "trace.json", "allowed.txt"} <= repo_files
    assert ".env" not in repo_files and ".aws/credentials" not in repo_files
    for secret_path in (".env", ".aws/credentials"):
        assert client.get("/api/runs/demo/artifact", params={"expected_generation": _artifact_generation(client), 
            "root": "editable:.", "path": secret_path}).status_code == 404
    external = client.get("/api/runs/demo/artifact",
                          params={"expected_generation": _artifact_generation(client), "root": "editable:.", "path": "spans.jsonl"})
    assert external.status_code == 200 and external.json()["content"] == "external spans\n"

    for ambiguous in ("spans.jsonl::$DATA", "spans.jsonl ", "spans.jsonl.", "SpAnS.JsOnL"):
        assert client.get("/api/runs/demo/artifact",
                          params={"expected_generation": _artifact_generation(client), "root": "run", "path": ambiguous}).status_code == 404

    if hardlink_supported:
        assert "raw-hardlink.txt" not in repo_files
        assert client.get("/api/runs/demo/artifact", params={"expected_generation": _artifact_generation(client), 
            "root": "editable:.", "path": "raw-hardlink.txt"}).status_code == 404
    if symlink_supported:
        assert "raw-symlink.txt" not in repo_files
        assert client.get("/api/runs/demo/artifact", params={"expected_generation": _artifact_generation(client), 
            "root": "editable:.", "path": "raw-symlink.txt"}).status_code == 404

    # Simulate a writable-root swap after the pathname check: the opened descriptor points at the
    # protected source. The post-open fstat authorization must still reject it before reading bytes.
    real_open = builtins.open

    def swapped(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if isinstance(file, (str, os.PathLike)) and Path(file) == allowed and "rb" in str(mode):
            return real_open(rd / "spans.jsonl", *args, **kwargs)
        return real_open(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", swapped)
        assert client.get("/api/runs/demo/artifact", params={"expected_generation": _artifact_generation(client), 
            "root": "editable:.", "path": "allowed.txt"}).status_code == 404


def test_hidden_trace_files_do_not_consume_artifact_listing_cap(tmp_path, monkeypatch):
    from looplab.serve import artifacts as artifact_module

    rd = tmp_path / "run"
    rd.mkdir()
    for name in (".spans.jsonl.atomic.tmp", "spans.jsonl", "z1.txt", "z2.txt", "z3.txt"):
        (rd / name).write_text(name, encoding="utf-8")
    monkeypatch.setattr(artifact_module, "_ART_MAX_FILES", 2)
    files, truncated = artifact_module._list_artifact_files(
        rd, exposed=artifact_module._artifact_exposure_policy(rd))
    assert [item["path"] for item in files] == ["z1.txt", "z2.txt"]
    assert truncated is True


def test_unprovable_artifact_policy_is_unavailable_not_empty(tmp_path, monkeypatch):
    """Failure to inspect a reserved trace entry must not masquerade as an empty inventory."""
    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    protected = rd / "spans.jsonl"
    protected.write_text("private trace\n", encoding="utf-8")
    safe = rd / "out.txt"
    safe.write_text("safe\n", encoding="utf-8")
    client = TestClient(make_app(tmp_path))

    real_stat = Path.stat

    def denied(path, *args, **kwargs):
        if path == protected:
            raise PermissionError("simulated protected-entry ACL loss")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    listing = client.get("/api/runs/demo/artifacts", params={"expected_generation": _artifact_generation(client)})
    content = client.get("/api/runs/demo/artifact",
                         params={"expected_generation": _artifact_generation(client), "root": "run", "path": "out.txt"})
    assert listing.status_code == 503
    assert content.status_code == 503
    assert "safe" not in listing.text
    assert "safe" not in content.text


def test_artifact_opened_object_must_match_authorized_path(tmp_path, monkeypatch):
    """A path/open race must not substitute an arbitrary file outside every declared root."""
    import builtins

    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    repo = tmp_path / "repo"
    repo.mkdir()
    allowed = repo / "allowed.txt"
    allowed.write_bytes(b"allowed\n")
    outside = tmp_path / "outside-secret.txt"              # sibling: neither run nor repo root
    outside.write_bytes(b"must-not-leak\n")
    (rd / "task.snapshot.json").write_text(json.dumps({
        "kind": "repo", "editable_path": str(repo),
    }), encoding="utf-8")
    client = TestClient(make_app(tmp_path))
    params = {"root": "editable:.", "path": "allowed.txt",
              "expected_generation": _artifact_generation(client)}
    assert client.get("/api/runs/demo/artifact", params=params).status_code == 200

    real_open = builtins.open

    def swapped(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if isinstance(file, (str, os.PathLike)) and Path(file) == allowed and "rb" in str(mode):
            return real_open(outside, *args, **kwargs)
        return real_open(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", swapped)
        response = client.get("/api/runs/demo/artifact", params=params)
    assert response.status_code == 404
    assert "must-not-leak" not in response.text


def test_artifacts_token_gated(tmp_path, monkeypatch):
    """The artifact routes serve raw file CONTENT, so when LOOPLAB_UI_TOKEN is set they're gated — and
    under P1-3 deny-default so is every other /api/ read (only the zero-model /api/health stays open)."""
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "sekret")
    _build_run(tmp_path)
    (tmp_path / "demo" / "out.txt").write_bytes(b"hi\n")
    client = TestClient(make_app(tmp_path))
    # raw-file reads require the token
    assert client.get("/api/runs/demo/artifacts").status_code == 401
    assert client.get("/api/runs/demo/artifact",
                      params={"root": "run", "path": "out.txt"}).status_code == 401
    # P1-3 deny-default: even a folded-projection GET now requires the token
    assert client.get("/api/runs/demo/state").status_code == 401
    # with the token, content is served. The artifact views are fenced to a run ATTEMPT now, so they
    # require the generation the caller is looking at — the gate is asserted above without it,
    # because a missing fence must never be the thing that keeps an untokened caller out.
    h = {"X-LoopLab-Token": "sekret"}
    assert client.get("/api/runs/demo/state", headers=h).status_code == 200
    generation = client.get("/api/runs/demo/state", headers=h).json()["generation"]
    assert client.get("/api/runs/demo/artifacts", headers=h,
                      params={"expected_generation": generation}).status_code == 200
    assert client.get("/api/runs/demo/artifact", headers=h,
                      params={"root": "run", "path": "out.txt",
                              "expected_generation": generation}).json()["content"] == "hi\n"


def test_raw_content_read_routes_are_token_gated(tmp_path, monkeypatch):
    """M8 regression: routes that serve RAW content (not folded projections) must be gated when
    LOOPLAB_UI_TOKEN is set — the raw event log (solution code + captured stdout/stderr), AGENTS.md,
    operator-authored prompt/skill/knowledge files, cross-run memory, and the assistant permission
    preview. The old gate only covered /artifact(s) and falsely claimed everything else was projections."""
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "sekret")
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    # Raw content / captured output / model transcript — the FULL enumerated set, incl. the uncapped
    # span I/O (the complete LLM prompt) and the trace/conversation transcript the first pass missed.
    for path in ("/api/runs/demo/log", "/api/runs/demo/nodes/0/logs", "/api/runs/demo/agents_md",
                 "/api/runs/demo/chat-log", "/api/runs/demo/spans/abc", "/api/runs/demo/trace",
                 "/api/runs/demo/trace/tail", "/api/runs/demo/trace/by_trace/t1",
                 "/api/runs/demo/nodes/0/conversation", "/api/prompts", "/api/skills",
                 "/api/knowledge", "/api/memory", "/api/assistant/permissions",
                 # arch-review §4 P1-3: node DETAIL (full code/files/stdout_tail/parent code) was
                 # open; gate it. (The billable health check is asserted below — it is a POST now.)
                 "/api/runs/demo/nodes/0"):
        assert client.get(path).status_code == 401, f"{path} should be gated"
        assert client.get(path, headers={"X-LoopLab-Token": "sekret"}).status_code == 200, path
    # The billable model-completion check is a revision-fenced POST, so it cannot ride the GET loop
    # above. The property is unchanged and still worth pinning: an unauthenticated caller must be
    # refused BEFORE anything can reach a paid provider call. Deny-default answers 401 ahead of
    # routing, which is why even the retired GET spelling is still refused rather than 404'd.
    _health_body = {"expected_settings_revision": "x", "expected_secret_revision": "y",
                    "operation_id": "b2f6c1de-4a3e-4c1b-9f57-0d1e2a3b4c5d"}
    assert client.post("/api/llm/health", json=_health_body).status_code == 401
    assert client.get("/api/llm/health").status_code == 401
    # …and with the token it reaches the handler (409 here: the fabricated revisions do not match
    # the saved snapshot), rather than being turned away by the gate.
    _authed = client.post("/api/llm/health", json=_health_body,
                          headers={"X-LoopLab-Token": "sekret"})
    assert _authed.status_code != 401 and _authed.status_code != 404, _authed.status_code
    # assistant progress is gated too (session query required once past auth)
    assert client.get("/api/assistant/progress?session=x").status_code == 401
    # P1-3 deny-default: even LIGHT projection reads now require the token (a new sensitive route can no
    # longer leak by being omitted from an allow-list). The zero-model /api/health is the sole exception.
    for light in ("/api/runs/demo/state",
                  "/api/runs/demo/nodes/0/metrics", "/api/runs"):
        assert client.get(light).status_code == 401, light
        assert client.get(light, headers={"X-LoopLab-Token": "sekret"}).status_code == 200, light
    assert client.get("/api/health").status_code == 200          # zero-model liveness stays open
    # The redacted live projection still contains private portfolio state. The React client uses
    # authenticated fetch-SSE, so the stream follows the same deny-default rule as /state.
    assert client.get("/api/runs/demo/events").status_code == 401
    with client.stream("GET", "/api/runs/demo/events",
                       headers={"X-LoopLab-Token": "sekret"}) as resp:
        assert resp.status_code == 200


def test_node_metrics_are_receipt_bound_to_current_attempt(tmp_path, monkeypatch):
    from looplab.core.node_evidence import begin_metrics_attempt
    from looplab.events.eventstore import EventStore
    from looplab.serve import metrics_adapters

    rd = tmp_path / "demo"
    node_dir = rd / "nodes" / "node_0"
    node_dir.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    store.append("node_evaluated", {
        "node_id": 0, "generation": 0, "metric": 1.0, "eval_seconds": 1.0})
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    begin_metrics_attempt(node_dir, 1, started_at=123.0)
    calls = []

    def read_metrics(path, *, since_wall_time=None):
        calls.append((path, since_wall_time))
        return {"loss": [{"step": 1, "value": 0.25, "wall_time": 124.0}]}

    monkeypatch.setattr(metrics_adapters, "read_node_metrics", read_metrics)
    client = TestClient(make_app(tmp_path))

    stale = client.get("/api/runs/demo/nodes/0/metrics", params={"attempt": 0})
    assert stale.status_code == 409
    current = client.get("/api/runs/demo/nodes/0/metrics", params={"attempt": 1})
    assert current.status_code == 200
    assert current.json() == {
        "node_id": 0, "attempt": 1,
        "metrics": {"loss": [{"step": 1, "value": 0.25, "wall_time": 124.0}]},
    }
    assert calls == [(str(node_dir), 123.0)]


def test_terminal_lifecycle_probe_matches_state_without_shipping_folded_payload(tmp_path):
    from looplab.events.eventstore import EventStore

    rd = tmp_path / "demo"
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("run_finished", {"reason": "complete"})
    client = TestClient(make_app(tmp_path))

    state = client.get("/api/runs/demo/state").json()
    probe = client.get("/api/runs/demo/lifecycle")

    assert probe.status_code == 200
    assert probe.json() == {
        "schema": 1,
        "seq": state["seq"],
        "event_count": state["event_count"],
        # The probe's job is to let an idle client decide whether to reopen the stream, and
        # `event_count` is exactly the number a truncated log understates — so the integrity receipt
        # travels with it (tests/test_seq_gap_visibility.py). Taken from `state` rather than pinned
        # literally: this asserts the two envelopes cannot come to disagree about the same file.
        "source_integrity": state["source_integrity"],
        "generation": state["generation"],
        "engine_running": state["state"]["engine_running"],
    }
    assert probe.json()["source_integrity"]["complete"] is True   # this fixture's log is intact
    assert "state" not in probe.json()


def test_assistant_session_transcript_is_token_gated(tmp_path, monkeypatch):
    """An assistant session transcript returns `raw` (the full model-facing instruction incl. attached
    file contents), so it must be gated like a raw-file read when LOOPLAB_UI_TOKEN is set."""
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "sekret")
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    # the session-transcript GET requires the token (not a 200 open read)
    assert client.get("/api/assistant/sessions/whatever").status_code == 401
    # P1-3 deny-default: a folded-projection GET now also requires the token
    assert client.get("/api/runs/demo/state").status_code == 401


def test_reserved_run_id_is_case_insensitive(tmp_path):
    """arch-review §5 P2: a reserved run id (assistant/reports) must be refused case-INSENSITIVELY —
    on a case-insensitive FS `ASSISTANT` would otherwise alias the reserved service store."""
    client = TestClient(make_app(tmp_path))
    for rid in ("assistant", "ASSISTANT", "Assistant", "REPORTS"):
        r = client.post("/api/start", json={"run_id": rid, "task": {"kind": "quadratic",
                                                                     "goal": "g", "direction": "min"}})
        assert r.status_code == 400 and "reserved" in r.json()["detail"]["message"], rid


def test_start_cannot_claim_a_server_owned_file_in_the_run_root(tmp_path):
    """The run root also holds server-owned FILES — the settings/secrets/projects stores, their
    `.lock` siblings, and the digest-suffixed `.looplab-lifecycle-*.lock` fences. `safe_run_dir`'s
    conflict check only looks for an `events.jsonl` CHILD, which a file never has, so /api/start
    reserved a start record for `run_id: "secrets.json"` and then either failed late at mkdir or —
    when the file did not exist yet — occupied the path and wedged the later store_secret/os.replace
    and the lifecycle lock's `open("a+")`. A run is a directory; refuse cleanly here instead."""
    client = TestClient(make_app(tmp_path))
    task = {"kind": "quadratic", "goal": "g", "direction": "min"}

    # reserved by NAME — refused even before the file exists on disk
    for rid in ("secrets.json", "ui_settings.json", "projects.json", "SECRETS.JSON",
                "secrets.json.lock", ".looplab-lifecycle-deadbeef.lock"):
        response = client.post("/api/start", json={"run_id": rid, "task": task})
        assert response.status_code == 400, rid
        assert "reserved" in response.json()["detail"]["message"], rid

    # ...and any OTHER existing non-directory in the root is a clean 409, not a late mkdir wedge
    (tmp_path / "some-future-store.json").write_text("{}", encoding="utf-8")
    response = client.post("/api/start", json={"run_id": "some-future-store.json", "task": task})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_path_conflict"


def test_a_node_workspace_is_not_addressable_as_a_run(tmp_path):
    """`run_dir` accepted any DESCENDANT of the root, so `demo/nodes/n0_ws` — a SANDBOX-WRITABLE node
    workspace — resolved happily. Any events.jsonl the evaluated candidate wrote there became a fake
    "run" for every caller of this base helper: read routes, `inject_node`'s `source_run` (a request
    BODY field, not a single-segment path param), assistant tooling. A run is a DIRECT child.
    """
    from fastapi import HTTPException

    _build_run(tmp_path)
    fake = tmp_path / "demo" / "nodes" / "n0_ws"
    fake.mkdir(parents=True)
    (fake / "events.jsonl").write_text(
        json.dumps({"seq": 1, "ts": 0.0, "type": "run_started",
                    "data": {"run_id": "pwned", "task_id": "t", "goal": "g", "direction": "min"}})
        + "\n", encoding="utf-8")
    srv = make_app(tmp_path).state.looplab

    assert srv.run_dir("demo") == (tmp_path / "demo").resolve()      # a real run still resolves
    for escape in ("demo/nodes/n0_ws", "./demo/nodes/n0_ws", "demo/../demo/nodes/n0_ws"):
        with pytest.raises(HTTPException) as excinfo:
            srv.run_dir(escape)
        assert excinfo.value.status_code == 404, escape


def test_start_rejects_filesystem_ambiguous_run_names(tmp_path):
    client = TestClient(make_app(tmp_path))
    for rid in ("trailing.", " trailing", "trailing ", "bad:name", "NUL", "com1.txt"):
        response = client.post(
            "/api/start", json={"run_id": rid, "task_file": str(TASK)})
        assert response.status_code == 400, rid


def test_public_state_drops_all_nested_raw_payloads_and_redacts_secrets(tmp_path):
    """arch-review §4 P1-3: the public /state projection (served without the UI token) must not ship
    raw captured stdout, and must redact the short error snippet it shows — a secret the candidate
    printed could otherwise leak. The full tail stays behind the token-gated node-detail endpoint."""
    from looplab.events.eventstore import EventStore
    secret = "AKIAIOSFODNN7EXAMPLE1234"
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    s.append("node_evaluated", {"node_id": 0, "metric": 1.0,
                                "stdout_tail": f"token={secret} printed near the start"})
    s.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "improve",
                              "idea": {"operator": "improve", "params": {}, "rationale": ""}})
    s.append("node_failed", {"node_id": 1, "error": f"crashed with key={secret}", "reason": "crash",
                             "triage_rationale": f"copied credential {secret}"})
    s.append("inject_node", {"idea": {"operator": "manual", "rationale": "queued"},
                             "code": f"API_KEY='{secret}'",
                             "files": {"secret.py": f"TOKEN={secret}"},
                             "deleted": ["private/config.py"]})
    client = TestClient(make_app(tmp_path))
    state = client.get("/api/runs/demo/state").json()["state"]
    nodes = state["nodes"]
    assert "stdout_tail" not in nodes["0"]                       # raw captured stdout dropped from /state
    assert secret not in (nodes["1"].get("error") or "")         # error snippet redacted
    assert "triage_rationale" not in nodes["1"]
    queued = state["inject_requests"][0]
    assert not ({"code", "files", "deleted"} & queued.keys())
    assert secret not in str(state)
    # the FULL stdout tail is still available via the node-detail endpoint (token-gated in prod)
    assert secret in (client.get("/api/runs/demo/nodes/0").json().get("stdout_tail") or "")


def test_provenance_keeps_parent_generation_after_reset(tmp_path):
    """A child remains derived from the parent bytes it used, not a later in-place replacement."""
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append(
        "run_started",
        {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"},
    )
    idea = {"operator": "draft", "params": {}, "rationale": ""}
    store.append("node_created", {
        "node_id": 0, "generation": 0, "parent_ids": [], "operator": "draft", "idea": idea,
    })
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 2.0})
    store.append("node_created", {
        "node_id": 1,
        "generation": 0,
        "parent_ids": [0],
        "parent_generations": {"0": 0},
        "operator": "improve",
        "idea": {**idea, "operator": "improve"},
    })
    store.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": 1.0})
    store.append("node_reset", {
        "node_id": 0, "generation": 0, "from_stage": "eval",
    })
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.5})

    graph = TestClient(make_app(tmp_path)).get("/api/runs/demo/prov").json()

    assert {"sol:0:0", "sol:0:1", "sol:1:0"} <= set(graph["entity"])
    assert graph["entity"]["sol:0:0"]["ll:lifecycle"] == "historical"
    assert graph["used"]["used:1:0-0:0"] == {
        "prov:activity": "exp:1:0",
        "prov:entity": "sol:0:0",
    }
    assert all(
        edge["prov:usedEntity"] != "sol:0:1"
        for edge in graph["wasDerivedFrom"].values()
        if edge["prov:generatedEntity"] == "sol:1:0"
    )


def test_runs_list_state_and_node_detail(tmp_path):
    st = _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))

    runs = client.get("/api/runs").json()
    assert any(r["run_id"] == "demo" and r["finished"] for r in runs)

    payload = client.get("/api/runs/demo/state").json()
    assert payload["state"]["finished"] is True
    assert len(payload["state"]["nodes"]) == len(st.nodes)
    assert payload["seq"] >= 0
    # heavy fields trimmed out of the live state
    any_node = next(iter(payload["state"]["nodes"].values()))
    assert "code" not in any_node

    # node detail carries the full code + a trace block
    nid = st.best().id
    node = client.get(f"/api/runs/demo/nodes/{nid}").json()
    assert node["id"] == nid and "code" in node and "trace" in node


def test_add_and_abandon_hypothesis_via_control(tmp_path):
    """P1 (1 card = 1 hypothesis): a human posts a hypothesis to the board through /control (it's in
    CONTROL_EVENTS); it folds into the single Card board as a card with verdict `open`, and an abandon
    control event flips that verdict to `abandoned`. The separate `state.hypotheses` view is gone."""
    from looplab.core.models import hypothesis_id
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    stmt = "a log transform of the target helps"
    r = client.post("/api/runs/demo/control",
                    json={"type": "hypothesis_added", "data": {"statement": stmt, "source": "human"}})
    assert r.status_code == 200
    cards = client.get("/api/runs/demo/state").json()["state"]["cards"]
    hid = hypothesis_id(stmt)
    assert hid in cards and cards[hid]["verdict"] == "open" and cards[hid]["source"] == "human"

    client.post("/api/runs/demo/control",
                json={"type": "hypothesis_updated", "data": {"id": hid, "status": "abandoned"}})
    cards = client.get("/api/runs/demo/state").json()["state"]["cards"]
    assert cards[hid]["verdict"] == "abandoned"


def test_engine_liveness_lock_probe(tmp_path):
    """A run with no engine holding its singleton lock is reported engine_running=False (so the UI can
    tell a real "thinking" run from a ZOMBIE whose engine died without run_finished); while a process
    holds the lock the probe flips to True. Uses the real cli._engine_singleton so the lock semantics
    match production and the test stays cross-platform (msvcrt on Windows, fcntl elsewhere)."""
    from looplab.cli import _engine_singleton
    from looplab.serve.engine_proc import _engine_alive

    _build_run(tmp_path)                       # finished run; nothing holds the lock
    client = TestClient(make_app(tmp_path))

    assert _engine_alive(tmp_path / "demo") is False
    assert client.get("/api/runs/demo/state").json()["state"]["engine_running"] is False
    listed = next(r for r in client.get("/api/runs").json() if r["run_id"] == "demo")
    assert listed["engine_running"] is False
    # resume backstop: with no live engine the guard doesn't short-circuit (it proceeds to spawn).
    # (We don't assert a spawn here — just that the alive-probe the guard reads is False.)

    with _engine_singleton(tmp_path / "demo") as ok:   # simulate a live engine holding the lock
        assert ok is True
        assert _engine_alive(tmp_path / "demo") is True
        assert client.get("/api/runs/demo/state").json()["state"]["engine_running"] is True
    assert _engine_alive(tmp_path / "demo") is False   # released on context exit


def test_engine_liveness_probe_never_recreates_a_raced_away_lock(tmp_path, monkeypatch):
    """Observation must not become a filesystem write if cleanup wins the lstat→open race."""
    import looplab.serve.engine_proc as engine_proc

    rd = tmp_path / "demo"
    rd.mkdir()
    lock = rd / "engine.lock"
    lock.write_text("sentinel", encoding="utf-8")
    original_open = os.open

    def disappear_after_metadata(path, flags, *args, **kwargs):
        if Path(path) == lock:
            lock.unlink()
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", disappear_after_metadata)
    assert engine_proc._engine_liveness(rd) is None
    lock.write_text("sentinel", encoding="utf-8")
    assert engine_proc._engine_alive(rd) is True  # conservative bool callers also fail closed
    assert lock.exists() is False


def test_engine_liveness_dangling_lock_link_is_unknown(tmp_path, monkeypatch):
    """A dangling ownership link is an observed suspicious entry, not proof that no writer exists."""
    from looplab.serve.engine_proc import _engine_alive, _engine_liveness

    rd = tmp_path / "demo"
    rd.mkdir()
    lock = rd / "engine.lock"
    try:
        lock.symlink_to(rd / "missing-lock-target")
    except OSError:
        # Windows may deny symlink creation without Developer Mode. Simulate the exact lstat entry
        # so this safety regression still runs on the platform where it matters most.
        import stat as stat_module
        original_lstat = Path.lstat

        class _LinkStat:
            st_mode = stat_module.S_IFLNK | 0o777
            st_file_attributes = 0

        monkeypatch.setattr(
            Path, "lstat",
            lambda path, *args, **kwargs: (_LinkStat() if path == lock
                                           else original_lstat(path, *args, **kwargs)))
    assert lock.is_symlink() and not lock.exists()
    assert _engine_liveness(rd) is None
    assert _engine_alive(rd) is True


def test_engine_liveness_run_directory_link_is_unknown(tmp_path, monkeypatch):
    """A reconciler must not treat an aliased run directory with no lock as safe to spawn into."""
    import stat as stat_module
    from looplab.serve.engine_proc import _engine_alive, _engine_liveness

    rd = tmp_path / "aliased-run"
    original_lstat = Path.lstat

    class _RunLinkStat:
        st_mode = stat_module.S_IFLNK | 0o777
        st_file_attributes = 0

    monkeypatch.setattr(
        Path, "lstat",
        lambda path, *args, **kwargs: (_RunLinkStat() if path == rd
                                       else original_lstat(path, *args, **kwargs)))
    assert _engine_liveness(rd) is None
    assert _engine_alive(rd) is True


def test_engine_liveness_revalidates_lock_path_after_open(tmp_path, monkeypatch):
    """Locking an old fd is inconclusive if engine.lock was replaced with another inode."""
    from looplab.serve.engine_proc import _engine_liveness

    rd = tmp_path / "run"
    rd.mkdir()
    lock = rd / "engine.lock"
    lock.write_bytes(b"sentinel")
    original_lstat = Path.lstat
    first = original_lstat(lock)
    lock_lstats = 0

    class _ReplacementStat:
        st_mode = first.st_mode
        st_dev = first.st_dev
        st_ino = first.st_ino + 1
        st_file_attributes = getattr(first, "st_file_attributes", 0)

    def replaced_after_open(path, *args, **kwargs):
        nonlocal lock_lstats
        if path == lock:
            lock_lstats += 1
            if lock_lstats > 1:
                return _ReplacementStat()
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", replaced_after_open)
    assert _engine_liveness(rd) is None


def test_runs_summary_only_reconciles_cached_pending_resume(tmp_path, monkeypatch):
    """Liveness polling must not full-fold every ordinary finished run on every dashboard refresh."""
    from looplab.events.eventstore import EventStore
    from looplab.serve.routers import runs as runs_router

    _build_run(tmp_path)
    reconciled = []
    monkeypatch.setattr(
        runs_router, "reconcile_pending_resume",
        lambda rd, **kw: reconciled.append((rd, kw.get("cancel_event"))) or False)
    client = TestClient(make_app(tmp_path))
    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/runs").status_code == 200
    assert not reconciled

    EventStore(tmp_path / "demo" / "events.jsonl").append("resume_requested", {})
    assert client.get("/api/runs").status_code == 200
    assert len(reconciled) == 1 and reconciled[0][0] == tmp_path / "demo"
    assert reconciled[0][1] is not None


def test_reconcile_pending_resume(tmp_path, monkeypatch):
    """P1-1 recoverable-intent reconciler: re-spawn a run whose durable resume intent stayed unserved
    past the grace window (its detached spawn died before the engine ran) — but ONLY then, and never
    for a finished/alive/within-grace run. Idempotent via the singleton lock (not exercised here)."""
    from looplab.serve import engine_proc as ep
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    rd = tmp_path / "run1"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")     # resumable
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "run1", "task_id": "t", "direction": "min"})
    spawns = []
    monkeypatch.setattr(ep, "_spawn_engine", lambda *a, **k: spawns.append((a, k)))
    monkeypatch.setattr(ep, "_engine_alive", lambda _rd: False)

    assert ep.reconcile_pending_resume(rd) is False and not spawns    # no intent -> no re-spawn
    s.append("resume_requested", {})
    req_ts = fold(s.read_all()).last_resume_request_ts
    assert ep.reconcile_pending_resume(rd, now=req_ts + 1) is False and not spawns   # within grace
    assert ep.reconcile_pending_resume(rd, now=req_ts + 31) is True and len(spawns) == 1  # zombie -> spawn
    # backoff: the re-spawn re-recorded the intent, so a call within the NEW grace does NOT re-spawn
    new_ts = fold(EventStore(rd / "events.jsonl").read_all()).last_resume_request_ts
    assert ep.reconcile_pending_resume(rd, now=new_ts + 1) is False and len(spawns) == 1

    monkeypatch.setattr(ep, "_engine_alive", lambda _rd: True)         # an engine IS running now
    assert ep.reconcile_pending_resume(rd, now=req_ts + 31) is False and len(spawns) == 1
    monkeypatch.setattr(ep, "_engine_alive", lambda _rd: False)
    s.append("resume_served", {})                                     # engine served the intent
    assert ep.reconcile_pending_resume(rd, now=req_ts + 100) is False and len(spawns) == 1

    s.append("resume_requested", {})                                  # a new intent, then a bare/error finish
    s.append("run_finished", {"reason": "done"})
    fin_ts = fold(s.read_all()).last_resume_request_ts
    # Sequence order alone is not proof that the writer observed the intent. Only resume_served
    # acknowledges it; a guarded/error writer may append a bare finish while unwinding.
    assert fold(s.read_all()).resume_pending()
    assert ep.reconcile_pending_resume(rd, now=fin_ts + 100) is True and len(spawns) == 2
    s.append("resume_served", {})

    s.append("resume_requested", {})                                  # request AFTER finish must recover
    tail_ts = fold(s.read_all()).last_resume_request_ts
    assert ep.reconcile_pending_resume(rd, now=tail_ts + 31) is True and len(spawns) == 3


def test_resume_launch_claim_deduplicates_workers_and_new_requests(tmp_path, monkeypatch):
    """The event-log claim closes the pre-engine.lock window where two workers could both Popen."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    from looplab.serve import engine_proc as ep

    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "direction": "min"})
    store.append("resume_requested", {})
    spawns = []
    monkeypatch.setattr(ep, "_engine_alive", lambda _rd: False)
    monkeypatch.setattr(ep, "_spawn_engine", lambda *a, **k: spawns.append((a, k)))
    args = ["resume", str(rd), "--task-file", str(rd / "task.snapshot.json")]

    assert ep._claim_and_spawn_resume(rd, args) is True
    claimed = fold(store.read_all())
    assert claimed.resume_pending() and claimed.last_resume_launch_seq > 0
    assert ep._claim_and_spawn_resume(rd, args) is False
    # A second request arriving before the first detached CLI takes engine.lock is covered by the
    # same in-flight launch; starting another process would only create stderr/log churn.
    store.append("resume_requested", {})
    assert ep._claim_and_spawn_resume(rd, args) is False
    assert len(spawns) == 1


def test_resume_task_resolution_prefers_snapshot_and_tolerates_bad_legacy_meta(
        tmp_path):
    from types import SimpleNamespace
    from looplab.serve.engine_proc import _cli_args_for_resume_state, _resolve_task_file

    rd = tmp_path / "run"
    rd.mkdir()
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{}", encoding="utf-8")
    snap = rd / "task.snapshot.json"
    snap.write_text("{}", encoding="utf-8")
    (rd / "ui_meta.json").write_text(
        json.dumps({"task_file": str(legacy)}), encoding="utf-8")
    assert _resolve_task_file(rd) == str(snap)             # immutable run truth wins

    snap.unlink()
    assert _resolve_task_file(rd) == str(legacy)           # legacy existing-file fallback
    assert _cli_args_for_resume_state(
        rd, ["resume", str(rd), "--task-file", str(legacy)],
        SimpleNamespace(last_resume_request_mode="finalize"),
    ) == ["finalize", str(rd), "--task-file", str(legacy)]
    for malformed in ("{bad json", "[]", '{"task_file": 3}'):
        (rd / "ui_meta.json").write_text(malformed, encoding="utf-8")
        assert _resolve_task_file(rd) is None               # never crashes startup/control


def test_resume_grace_rejects_future_wall_clock_timestamps():
    from types import SimpleNamespace
    from looplab.serve import engine_proc as ep

    assert not ep._within_resume_grace(101.0, 100.0)
    future_claim = SimpleNamespace(
        last_resume_launch_seq=4, last_resume_served_seq=3,
        last_resume_launch_ts=101.0,
    )
    assert not ep._launch_claim_is_fresh(future_claim, 100.0)


def test_claim_live_flip_installs_tail_waiter(tmp_path, monkeypatch):
    """The engine can acquire its singleton between the dead probe and the durable launch claim."""
    from looplab.serve import engine_proc as ep

    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "direction": "min"})
    store.append("resume_requested", {"mode": "resume"})
    probes = iter((False, True))
    monkeypatch.setattr(ep, "_engine_alive", lambda _rd: next(probes, True))
    waiters = []
    monkeypatch.setattr(
        ep, "_spawn_engine_after_exit",
        lambda *a, **kw: waiters.append((a, kw)) or True)

    args = ["resume", str(rd), "--task-file", str(rd / "task.snapshot.json")]
    assert ep._claim_and_spawn_resume(rd, args, wait_on_alive=True) is False
    assert len(waiters) == 1 and waiters[0][1]["run_dir"] == rd


def test_resume_cancellation_after_claim_prevents_popen(tmp_path, monkeypatch):
    import threading
    from looplab.serve import engine_proc as ep

    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "direction": "min"})
    store.append("resume_requested", {"mode": "resume"})
    cancel = threading.Event()
    probes = 0

    def _cancel_after_claim(_rd):
        nonlocal probes
        probes += 1
        if probes == 2:                    # second probe is after durable launch_claim
            cancel.set()
        return False

    spawns = []
    monkeypatch.setattr(ep, "_engine_alive", _cancel_after_claim)
    monkeypatch.setattr(ep, "_spawn_engine", lambda *a, **kw: spawns.append((a, kw)))
    args = ["resume", str(rd), "--task-file", str(rd / "task.snapshot.json")]
    assert ep._claim_and_spawn_resume(rd, args, cancel_event=cancel) is False
    assert not spawns


def test_resume_route_passes_shutdown_cancellation_and_live_waiter(tmp_path, monkeypatch):
    from looplab.serve.routers import control as control_router

    _build_run(tmp_path)
    _make_resumable(tmp_path / "demo")
    captured = []
    monkeypatch.setattr(control_router, "_engine_alive", lambda _rd: False)
    monkeypatch.setattr(
        control_router, "_claim_and_spawn_resume",
        lambda *a, **kw: captured.append((a, kw)) or False)
    response = TestClient(make_app(tmp_path)).post("/api/runs/demo/resume")
    assert response.status_code == 200
    assert captured[0][1]["cancel_event"] is not None
    assert captured[0][1]["wait_on_alive"] is True


def test_corrupt_complete_log_does_not_crash_startup_recovery(tmp_path, monkeypatch):
    from looplab.serve import engine_proc as ep

    rd = tmp_path / "broken"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    (rd / "events.jsonl").write_bytes(
        b'{"seq":0,"ts":1,"type":"run_started","data":{}}\n{bad complete json}\n')
    spawns = []
    monkeypatch.setattr(ep, "_spawn_engine", lambda *a, **kw: spawns.append((a, kw)))

    with TestClient(make_app(tmp_path)) as client:
        assert client.get("/api/health").status_code == 200
    assert not spawns


@pytest.mark.parametrize("mutation", ["reset", "delete"])
def test_resume_claim_popen_gap_fences_reset_and_delete(
        tmp_path, monkeypatch, mutation):
    """A deterministic barrier pins the gap after launch-claim and before Popen returns."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time as _time
    from looplab.serve import engine_proc as ep

    _build_run(tmp_path)
    _make_resumable(tmp_path / "demo")
    entered = threading.Event()
    release = threading.Event()

    def _blocked_spawn(*_a, **_kw):
        entered.set()
        assert release.wait(2.0)

    monkeypatch.setattr(ep, "_spawn_engine", _blocked_spawn)
    client = TestClient(make_app(tmp_path))
    with ThreadPoolExecutor(max_workers=2) as pool:
        resume = pool.submit(client.post, "/api/runs/demo/resume")
        assert entered.wait(2.0), "resume did not reach the claim -> Popen barrier"
        # The delete arm goes through the deletion TRANSACTION: bodyless DELETE is a 409 stub that
        # returns instantly without taking the sequencer, so it would sail past the fence and the
        # arm would then "pass" on a 409 that means "wrong request shape", not "blocked".
        mutate = (pool.submit(client.post, "/api/runs/demo/reset") if mutation == "reset"
                  else pool.submit(_delete_run, client, "demo", rd=tmp_path / "demo"))
        _time.sleep(0.1)
        assert not mutate.done(), "lifecycle mutation crossed the in-flight launch fence"
        release.set()
        assert resume.result(timeout=2.0).status_code == 200
        assert mutate.result(timeout=2.0).status_code == 409


def test_reset_archive_failure_keeps_the_source_of_truth_and_never_spawns(tmp_path, monkeypatch):
    """A failed archive step leaves the event log where it was and starts no replacement engine.

    Inject at `_durable_archive_move`, the primitive the archive actually calls. This test used to
    patch `Path.rename`; the move became a native no-replace `renameat2`/`MoveFileExW` call, so that
    patch stopped intercepting anything and the test was asserting a failure code against a Replay
    that had quietly SUCCEEDED — including its spawn.

    The transaction no longer unwinds the artifacts it already archived: the receipt records
    `archiving`, so the retry continues from there rather than re-doing a move it cannot prove it
    made. What must still hold is that nothing is LOST (the event log is untouched and byte-exact,
    a pre-existing approved archive is not overwritten) and that no engine is launched against a
    half-archived run.
    """
    import looplab.serve.reset_route as reset_route
    from looplab.serve.routers import control as control_router

    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _make_resumable(rd)
    (rd / "spans.jsonl").write_text('{"span":1}\n', encoding="utf-8")
    approved_archive = rd / "spans.jsonl.reset-1"
    approved_archive.write_text('{"approved":true}\n', encoding="utf-8")
    before = (rd / "events.jsonl").read_bytes()
    real_move = reset_route._durable_archive_move

    def _fail_source_of_truth(source, destination):
        if source.name == "events.jsonl":
            raise OSError("injected event-log archive failure")
        return real_move(source, destination)

    spawns = []
    with monkeypatch.context() as patch:
        patch.setattr(reset_route, "_durable_archive_move", _fail_source_of_truth)
        patch.setattr(
            control_router, "_spawn_engine", lambda *a, **kw: spawns.append((a, kw)))
        with TestClient(make_app(tmp_path)) as client:
            response = client.post("/api/runs/demo/reset")

    assert response.status_code == 425
    detail = response.json()["detail"]
    assert detail["code"] == "reset_pending"
    assert "events.jsonl" in detail["message"], "the operator must be told WHICH artifact blocked"
    assert detail["remediation"] == "Retry this exact operation; do not submit a new Replay."
    assert (rd / "events.jsonl").read_bytes() == before        # source of truth never moved
    assert approved_archive.read_text(encoding="utf-8") == '{"approved":true}\n'
    assert not spawns, "no engine may be launched against a half-archived run"


def test_reset_spawn_failure_stays_resumable_without_losing_the_archived_run(tmp_path, monkeypatch):
    """A Popen that raised is UNCERTAIN, so Replay keeps the operation open instead of rolling back.

    This used to assert a 500 and a full restore. Rolling back is no longer safe: the process may
    have started before the exception surfaced, and un-archiving underneath a live child would hand
    it a log the server also considers current. The transaction instead reports
    `reset_launch_uncertain` and stays resumable — so what has to be proven is that nothing is lost
    (the archived log is byte-identical to the original) and that the retry the remediation demands
    REJOINS the same operation rather than opening a second Replay.
    """
    from looplab.serve.routers import control as control_router

    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _make_resumable(rd)
    (rd / "spans.jsonl").write_text('{"span":1}\n', encoding="utf-8")
    before = (rd / "events.jsonl").read_bytes()
    assert not list(rd.glob("*.reset-*"))
    with monkeypatch.context() as patch:
        patch.setattr(
            control_router, "_spawn_engine",
            lambda *_a, **_kw: (_ for _ in ()).throw(OSError("injected Popen failure")))
        with TestClient(make_app(tmp_path)) as client:
            response = client.post("/api/runs/demo/reset")

            assert response.status_code == 503
            failed = response.json()["detail"]
            assert failed["code"] == "reset_launch_uncertain"
            assert failed["remediation"] == "Retry this exact operation; never submit a new Replay."
            archived = next(rd.glob("events.jsonl.reset-*"))
            assert archived.read_bytes() == before      # recoverable, byte for byte

            # The retry carries no body, exactly as the failed request did. It must resolve to the
            # SAME operation: the live event log is gone at this point, so deriving the generation
            # from disk answers 404 "no such run" and the operation can never be finished by the
            # scripted callers the bodyless form exists for.
            patch.setattr(control_router, "_spawn_engine", _replacement_spawn(rd))
            rejoined = client.post("/api/runs/demo/reset")

    assert rejoined.status_code != 404, "the run must not read as deleted while Replay is in flight"
    rejoin = rejoined.json()["detail"]
    assert rejoin["operation_id"] == failed["operation_id"], (
        "a bodyless retry must rejoin the operation, not open a second Replay")
    assert rejoin["expected_generation"] == failed["expected_generation"]
    # Still uncertain, and deliberately so: the first child's launch claim is neither confirmed dead
    # nor cleared, so re-spawning could double-launch. The retry is a rejoin, not a relaunch.
    assert rejoined.status_code == 425 and rejoin["phase"] == "launch_uncertain"
    assert next(rd.glob("events.jsonl.reset-*")).read_bytes() == before


def test_reset_replays_legacy_snapshot_with_off_defaults_and_explicit_null_aliases(
        tmp_path, monkeypatch):
    from looplab.serve.routers import control as control_router

    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _make_resumable(rd)
    raw = {"timeout": 30.0, "max_parallel": 4, "parallel_build": 3}
    snapshot = rd / "config.snapshot.json"
    snapshot.write_text(json.dumps(raw), encoding="utf-8")
    before = snapshot.read_bytes()
    spawns = []
    # Capture the frozen launch AND write the replacement generation: without that first event the
    # transaction never leaves `popen_returned`, so the route answers 425 and the `--set` argv this
    # test exists to inspect is never reached through a successful Replay.
    replacement = _replacement_spawn(rd, pid=4242)

    def capture_spawn(args, **kwargs):
        spawns.append((args, kwargs))
        return replacement(args, **kwargs)

    monkeypatch.setattr(control_router, "_spawn_engine", capture_spawn)
    with TestClient(make_app(tmp_path)) as client:
        response = client.post("/api/runs/demo/reset")

    assert response.status_code == 200
    assert len(spawns) == 1
    args, kwargs = spawns[0]
    # The launch is frozen field by field: every optional setting the replacement must NOT re-inherit
    # from ambient config is passed as an explicit `--set <field>=null`. Pin the two legacy aliases
    # by name — a bare count would break on every field added to the freeze without saying anything
    # about the aliases this test exists for.
    assert "eval_parallel=null" in args and "llm_parallel=null" in args
    assert args.count("--set") == len([a for a in args if a.endswith("=null")])
    env = kwargs["env"]
    for key in (
            "LOOPLAB_TRAIN_MONITOR", "LOOPLAB_ASHA_LIVE",
            "LOOPLAB_WATCHDOG_REFLECTION", "LOOPLAB_CARD_DRIVEN_SELECTION",
            "LOOPLAB_CONCURRENT_RESEARCH_REPEAT", "LOOPLAB_CONCURRENT_CONSOLIDATE"):
        assert env[key] == "false"
    assert env["LOOPLAB_MAX_EVAL_TIMEOUT"] == str(24 * 3600.0)
    assert "LOOPLAB_EVAL_PARALLEL" not in env and "LOOPLAB_LLM_PARALLEL" not in env
    assert snapshot.read_bytes() == before


def test_resume_shutdown_hook_precedes_jupyter_reaper(tmp_path):
    app = make_app(tmp_path)
    names = [getattr(handler, "__name__", "") for handler in app.router.on_shutdown]
    assert names.index("_cancel_resume_timers") < names.index("_reap_on_shutdown")


def test_server_startup_recovers_pending_resume_without_runs_poll(tmp_path, monkeypatch):
    """A UI-server restart autonomously restores a durable intent; `/api/runs` is not required."""
    from looplab.events.eventstore import EventStore
    from looplab.serve import engine_proc as ep

    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "direction": "min"})
    store.append("resume_requested", {})
    spawns = []
    monkeypatch.setattr(ep, "_RESUME_RECONCILE_GRACE_S", 0.0)
    monkeypatch.setattr(ep, "_engine_alive", lambda _rd: False)
    monkeypatch.setattr(ep, "_spawn_engine", lambda *a, **k: spawns.append((a, k)))

    with TestClient(make_app(tmp_path)) as client:
        assert client.get("/api/health").status_code == 200
    assert len(spawns) == 1 and "resume" in spawns[0][0][0]


def test_server_startup_recovers_restart_after_command_worker_loss(tmp_path, monkeypatch):
    """The restart event itself is enough recovery truth; no browser or command thread must survive."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    from looplab.serve import engine_proc as ep

    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "direction": "min"})
    restart = store.append("restart", {"_command_id": "cmd_" + "a" * 32})
    state = fold(store.read_all())
    assert state.paused and state.resume_pending()
    assert state.last_resume_request_seq == restart.seq

    spawns = []
    monkeypatch.setattr(ep, "_RESUME_RECONCILE_GRACE_S", 0.0)
    monkeypatch.setattr(ep, "_engine_alive", lambda _rd: False)
    monkeypatch.setattr(ep, "_spawn_engine", lambda *args, **kwargs: spawns.append((args, kwargs)))

    with TestClient(make_app(tmp_path)) as client:
        assert client.get("/api/health").status_code == 200
    assert len(spawns) == 1
    assert spawns[0][0][0][0] == "resume"
    claimed = fold(store.read_all())
    assert claimed.resume_pending() and claimed.last_resume_launch_seq > restart.seq


def test_server_startup_recovers_restart_record_lost_before_intent_append(tmp_path, monkeypatch):
    """Recovery also closes the durable-record -> folded-intent worker crash window."""
    from looplab.events.eventstore import EventStore
    from looplab.serve.run_commands import RunCommandService

    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "run", "task_id": "t", "direction": "min"})

    first_app = make_app(tmp_path)
    first = first_app.state.looplab.commands
    first._start_worker = lambda *_args, **_kwargs: None
    record = first.submit(
        rd, "restart-worker-lost", "restart", {},
        expected_generation=first.run_generation(rd))
    assert record["status"] == "accepted"
    assert not any(event.type == "restart" for event in EventStore(rd / "events.jsonl").read_all())

    recovered = []
    monkeypatch.setattr(
        RunCommandService, "_start_worker",
        lambda self, run_dir, path, row: recovered.append((run_dir, path, row["id"])))
    with TestClient(make_app(tmp_path)) as client:
        assert client.get("/api/health").status_code == 200
    assert recovered == [(rd, rd / ".commands" / f"{record['id']}.json", record["id"])]


def test_server_startup_does_not_create_waiter_for_unknown_liveness(tmp_path, monkeypatch):
    """Unknown/reparse runs stay quarantined without one 20 Hz polling thread per directory."""
    from looplab.serve import engine_proc as ep

    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "direction": "min"})
    store.append("resume_requested", {})
    spawns = []
    waiters = []
    monkeypatch.setattr(ep, "_RESUME_RECONCILE_GRACE_S", 0.0)
    monkeypatch.setattr(ep, "_engine_liveness", lambda _rd: None)
    monkeypatch.setattr(ep, "_spawn_engine", lambda *args, **kwargs: spawns.append((args, kwargs)))
    monkeypatch.setattr(
        ep, "_spawn_engine_after_exit",
        lambda *args, **kwargs: waiters.append((args, kwargs)) or True)

    with TestClient(make_app(tmp_path)) as client:
        assert client.get("/api/health").status_code == 200
    assert spawns == [] and waiters == []


@pytest.mark.parametrize("intent", ["inject_node", "resume", "run_reopened"])
def test_resume_during_post_finish_tail_spawns_once_after_engine_exit(
        tmp_path, monkeypatch, intent):
    """An action after run_finished must not be stranded by the old engine's finalization lock tail."""
    import threading

    from looplab.cli import _engine_singleton

    run_id = f"demo-{intent}"
    _build_run(tmp_path, run_id)
    rd = tmp_path / run_id
    (rd / "task.snapshot.json").write_text(TASK.read_text(encoding="utf-8"), encoding="utf-8")
    spawned = []
    spawn_seen = threading.Event()

    def _fake_popen(cmd, **kwargs):
        spawned.append(cmd)
        spawn_seen.set()
        return type("P", (), {})()

    monkeypatch.setattr("looplab.serve.engine_proc.subprocess.Popen", _fake_popen)
    client = TestClient(make_app(tmp_path))

    with _engine_singleton(rd) as ok:
        assert ok
        # This is the exact SSE-visible window: state is already finished, but finalize_run still
        # owns engine.lock. The control intent is durable; two resume calls must install one waiter.
        data = ({"idea": {"operator": "manual", "params": {"x": 0.5}}}
                if intent == "inject_node" else {})
        action = client.post(
            f"/api/runs/{run_id}/control", json={"type": intent, "data": data})
        assert action.status_code == 200
        first = client.post(f"/api/runs/{run_id}/resume").json()
        second = client.post(f"/api/runs/{run_id}/resume").json()
        assert first["resume_after_exit"] is True and second["resume_after_exit"] is True
        assert not spawned

    assert spawn_seen.wait(2.0), "resume waiter did not hand off after engine.lock was released"
    assert len(spawned) == 1 and "resume" in spawned[0]
    state = fold(EventStore(rd / "events.jsonl").read_all())
    if intent == "inject_node":
        assert state.finished and len(state.inject_requests) > state.injects_done
    else:
        assert not state.finished


def test_live_owner_explicitly_serves_resume_before_finish(tmp_path, monkeypatch):
    """Only the live owner's explicit acknowledgement suppresses the post-exit replacement."""
    import threading
    import time

    from looplab.cli import _engine_singleton
    from looplab.serve import engine_proc as ep

    _build_run(tmp_path)
    rd = tmp_path / "demo"
    (rd / "task.snapshot.json").write_text(TASK.read_text(encoding="utf-8"), encoding="utf-8")
    spawned = []
    spawn_seen = threading.Event()

    def _fake_popen(cmd, **kwargs):
        spawned.append(cmd)
        spawn_seen.set()
        return type("P", (), {})()

    monkeypatch.setattr("looplab.serve.engine_proc.subprocess.Popen", _fake_popen)
    client = TestClient(make_app(tmp_path))
    with _engine_singleton(rd) as ok:
        assert ok
        response = client.post("/api/runs/demo/resume")
        assert response.status_code == 200 and response.json()["resume_after_exit"] is True
        EventStore(rd / "events.jsonl").append("resume_served", {})
        EventStore(rd / "events.jsonl").append("run_finished", {"reason": "post-wake done"})
        key = str(rd.resolve())
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            with ep._resume_after_exit_lock:
                if key not in ep._resume_after_exit:
                    break
            time.sleep(0.01)
        with ep._resume_after_exit_lock:
            assert key not in ep._resume_after_exit  # no 20 Hz lock polling for rest of live run
            assert key not in ep._resume_waiter_threads

    assert not spawn_seen.wait(0.3)
    assert not spawned
    assert not fold(EventStore(rd / "events.jsonl").read_all()).resume_pending()


def test_post_finish_tail_of_pending_abort_hands_off_to_finalize_not_resume(
        tmp_path, monkeypatch):
    """The accepted mode is classified before the live owner lands run_finished and survives its tail.
    Spawning ordinary resume afterward would reopen the just-finalized search."""
    import threading
    from looplab.cli import _engine_singleton

    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _make_resumable(rd)
    EventStore(rd / "events.jsonl").append("run_abort", {"reason": "operator"})
    spawned = []
    seen = threading.Event()

    def _fake_popen(cmd, **_kwargs):
        spawned.append(cmd)
        seen.set()
        return type("P", (), {})()

    monkeypatch.setattr("looplab.serve.engine_proc.subprocess.Popen", _fake_popen)
    client = TestClient(make_app(tmp_path))
    with _engine_singleton(rd) as ok:
        assert ok
        response = client.post("/api/runs/demo/resume")
        assert response.status_code == 200 and response.json()["resume_after_exit"] is True
        # Simulate the old owner accepting the abort after the handoff was durably classified.
        EventStore(rd / "events.jsonl").append("run_finished", {"reason": "operator"})

    assert seen.wait(2.0)
    assert len(spawned) == 1
    assert "finalize" in spawned[0] and "resume" not in spawned[0]
    requests = [e for e in EventStore(rd / "events.jsonl").read_all()
                if e.type == "resume_requested"]
    assert requests[-2].data.get("mode") == "finalize"
    assert requests[-1].data.get("launch_claim") is True
    assert requests[-1].data.get("mode") == "finalize"


def test_time_travel_seq(tmp_path):
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    raw = list(iter_event_jsonl(rd / "events.jsonl"))
    created = next(e for e in raw if e["type"] == "node_created")
    created_seq = created["seq"]
    nid = created["data"]["node_id"]
    EventStore(rd / "events.jsonl").append(
        "annotation", {"node_id": nid, "text": "added after the snapshot"})
    client = TestClient(make_app(tmp_path))
    # seq=0 is just run_started -> no nodes yet; the full state has nodes.
    early = client.get("/api/runs/demo/state", params={"seq": 0}).json()
    full = client.get("/api/runs/demo/state").json()
    assert len(early["state"]["nodes"]) == 0
    assert len(full["state"]["nodes"]) > 0
    assert early["seq"] == 0
    assert early["max_seq"] == full["seq"]
    assert early["state"]["engine_running"] is None  # current liveness is not stamped into history

    # Node detail uses the same prefix fold: future annotations and live spans must not leak backward.
    historical_node = client.get(f"/api/runs/demo/nodes/{nid}",
                                 params={"seq": created_seq,
                                         "expected_generation": early["generation"]}).json()
    live_node = client.get(f"/api/runs/demo/nodes/{nid}").json()
    fenced_live_node = client.get(f"/api/runs/demo/nodes/{nid}", params={
        "expected_generation": full["generation"]}).json()
    assert historical_node["annotations"] == []
    assert live_node["annotations"] == ["added after the snapshot"]
    assert fenced_live_node["run_generation"] == full["generation"]
    stale_live = client.get(f"/api/runs/demo/nodes/{nid}", params={
        "expected_generation": "0" * 64})
    assert stale_live.status_code == 409
    assert stale_live.json()["detail"]["code"] == "run_generation_changed"
    assert historical_node["trace"] == {"nodes": []}
    assert historical_node["historical_seq"] == created_seq

    # A future node is an explicit 404 at an earlier prefix rather than a live-detail fallback.
    later = next(e for e in raw if e["type"] == "node_created" and e["seq"] > created_seq)
    assert client.get(f"/api/runs/demo/nodes/{later['data']['node_id']}",
                      params={"seq": created_seq,
                              "expected_generation": early["generation"]}).status_code == 404


def test_historical_node_detail_rejects_replaced_run_generation(tmp_path):
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    log = rd / "events.jsonl"
    raw = list(iter_event_jsonl(log))
    created = next(event for event in raw if event["type"] == "node_created")
    seq = created["seq"]
    nid = created["data"]["node_id"]
    client = TestClient(make_app(tmp_path))
    generation_a = client.get("/api/runs/demo/state", params={"seq": seq}).json()["generation"]

    missing = client.get(f"/api/runs/demo/nodes/{nid}", params={"seq": seq})
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "historical_generation_required"

    log.rename(rd / "events.jsonl.generation-a")
    replacement = []
    for index, event in enumerate(raw):
        row = dict(event)
        row["data"] = dict(event.get("data") or {})
        if index == 0:
            row["ts"] = float(event["ts"]) + 1.0
        if row["type"] == "node_created" and row["data"].get("node_id") == nid:
            row["data"]["code"] = "GENERATION_B_MUST_NOT_APPEAR_UNDER_A"
        replacement.append(row)
    log.write_text("".join(json.dumps(row) + "\n" for row in replacement), encoding="utf-8")

    generation_b = client.get("/api/runs/demo/state", params={"seq": seq}).json()["generation"]
    assert generation_b != generation_a
    stale = client.get(f"/api/runs/demo/nodes/{nid}", params={
        "seq": seq, "expected_generation": generation_a})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "run_generation_changed"
    current = client.get(f"/api/runs/demo/nodes/{nid}", params={
        "seq": seq, "expected_generation": generation_b}).json()
    assert current["historical_generation"] == generation_b
    assert current["code"] == "GENERATION_B_MUST_NOT_APPEAR_UNDER_A"


def _clear_trace(client, run_id: str, nid: int, *, rd, op: str = "0" * 32):
    """POST clear_trace with the identities it now requires, computed straight from the run dir.

    `clear_node_trace` refuses without the exact run generation, trace revision, node generation and
    a client-minted operation id: a destructive whole-file rewrite must not be issuable from a stale
    view, and the idempotency receipt is keyed on that operation id. A test that POSTs an empty body
    gets 428 before any lifecycle check runs, so it says nothing about the behaviour it means to pin.

    Read from DISK, never through `GET /api/runs`: that handler runs the durable-resume reconciler,
    which would serve — and therefore destroy — the "unserved resume" precondition the lifecycle
    test below sets up."""
    from looplab.events.traceview import trace_file_revision
    from looplab.serve.run_commands import run_generation_token

    events = EventStore(rd / "events.jsonl").read_all()
    state = fold(events)
    node = state.nodes.get(nid)
    return client.post(f"/api/runs/{run_id}/nodes/{nid}/clear_trace", json={
        "expected_generation": run_generation_token(events),
        "expected_trace_revision": trace_file_revision(rd / "spans.jsonl"),
        "node_generation": getattr(node, "attempt", 0) if node is not None else 0,
        "operation_id": f"tc_{op}",
    })


def test_clear_node_trace_removes_only_that_nodes_spans(tmp_path):
    """The 'clear trace' button: erase ONE node's spans from spans.jsonl (append-only, so a reset+
    rebuild would otherwise stack fresh bands on the old attempt's) while leaving other nodes' spans
    AND the event log intact; refused with 409 while a live engine holds the lock."""
    from looplab.cli import _engine_singleton
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    # two nodes' worth of spans + one unscoped span (no node_id) that must survive
    spans = [
        {"span_id": "a", "kind": "operation", "name": "create_node", "attributes": {"node_id": 0}},
        {"span_id": "b", "kind": "generation", "name": "gen", "attributes": {"node_id": 0}},
        {"span_id": "c", "kind": "operation", "name": "create_node", "attributes": {"node_id": 1}},
        {"span_id": "d", "kind": "generation", "name": "gen", "attributes": {}},           # unscoped
    ]
    (rd / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    events_before = (rd / "events.jsonl").read_bytes()
    client = TestClient(make_app(tmp_path))

    # live engine -> refused, spans untouched
    with _engine_singleton(rd) as ok:
        assert ok
        r = _clear_trace(client, "demo", 0, rd=rd, op="a" * 32)
        assert r.status_code == 409
    assert len(list(iter_jsonl(rd / "spans.jsonl"))) == 4      # nothing removed while live

    # not live -> node 0's two spans gone, node 1 + unscoped kept, event log untouched
    r = _clear_trace(client, "demo", 0, rd=rd, op="b" * 32)
    assert r.status_code == 200 and r.json()["removed"] == 2 and r.json()["kept"] == 2
    left = list(iter_jsonl(rd / "spans.jsonl"))
    assert {s["span_id"] for s in left} == {"c", "d"}
    assert (rd / "events.jsonl").read_bytes() == events_before

    # idempotent: a NEW operation over the already-cleared trace removes nothing
    assert _clear_trace(client, "demo", 0, rd=rd, op="c" * 32).json()["removed"] == 0


def test_trace_tail_survives_a_huge_recent_span_line(tmp_path):
    """Mega-review 07-06: the live trace feed read a FIXED 256KB tail window of spans.jsonl. A single
    span line can be 100KB+ (a repo-Developer generation carries the whole prompt+output on it), so one
    giant most-recent line could fill the window and blank the feed exactly during the heavy generations
    a user most wants to watch. The backward reader must still surface it."""
    _build_run(tmp_path)                                   # a real run dir at tmp_path/demo
    rd = tmp_path / "demo"
    big = "Z" * 400_000                                    # one generation span > the 256KB window
    spans = [
        {"trace_id": "tail", "span_id": "s1", "kind": "generation", "start": 1.0,
         "duration_s": 0.5, "status": "ok",
         "attributes": {"model": "m", "output": "early small gen"}},
        {"trace_id": "tail", "span_id": "s2", "kind": "generation", "start": 2.0,
         "duration_s": 9.0, "status": "ok",
         "attributes": {"model": "m", "output": big}},
    ]
    (rd / "spans.jsonl").write_text("\n".join(json.dumps(s) for s in spans) + "\n", encoding="utf-8")
    client = TestClient(make_app(tmp_path))
    r = client.get("/api/runs/demo/trace/tail", params={"limit": 5})
    assert r.status_code == 200
    tail = r.json()["tail"]
    assert tail, "feed blanked on a >256KB span line"      # the bug: an empty list
    assert tail[-1]["span_id"] == "s2"                     # the huge, most-recent generation is present
    assert len(tail[-1]["text"]) <= 500                    # text still capped for the browser


def test_control_append_and_validation(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/control", json={"type": "pause", "data": {}})
    assert r.status_code == 200 and r.json()["type"] == "pause"
    st = fold(EventStore(tmp_path / "demo" / "events.jsonl").read_all())
    assert st.paused is True
    # unknown control event rejected
    bad = client.post("/api/runs/demo/control", json={"type": "danger", "data": {}})
    assert bad.status_code == 400
    # Internal durable handoff records (especially launch_claim) are written only by /resume;
    # exposing them through the generic control surface would let a caller suppress real launches.
    internal = client.post(
        "/api/runs/demo/control", json={"type": "resume_requested", "data": {"launch_claim": True}})
    assert internal.status_code == 400
    # P1-12 optimistic concurrency: a stale expected_seq -> 409 (the log advanced since); the matching
    # tail seq -> 200. A non-integer expected_seq -> 400.
    tail = client.post("/api/runs/demo/control", json={"type": "pause", "data": {}}).json()["seq"]
    stale = client.post("/api/runs/demo/control",
                        json={"type": "resume", "data": {}, "expected_seq": tail - 1})
    assert stale.status_code == 409
    fresh = client.post("/api/runs/demo/control",
                        json={"type": "resume", "data": {}, "expected_seq": tail})
    assert fresh.status_code == 200
    nonint = client.post("/api/runs/demo/control",
                         json={"type": "pause", "data": {}, "expected_seq": "nope"})
    assert nonint.status_code == 400
    # A lone surrogate is valid JSON (\ud800) that json.loads decodes but str.encode("utf-8") cannot
    # encode. The payload-size guard's encode must catch it as a clean 400, not surface a 500 (the
    # encode used to sit outside the try that wraps json.dumps). Sent as raw content because httpx's
    # own json= encoder would reject the surrogate before it ever reached the server.
    surrogate = client.post("/api/runs/demo/control", headers={"Content-Type": "application/json"},
                            content='{"type":"hint","data":{"text":"x\\ud800"}}')
    assert surrogate.status_code == 400 and "encodable" in surrogate.json()["detail"]


def test_node_controls_compare_and_set_lifecycle_generation(tmp_path):
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    store = EventStore(rd / "events.jsonl")
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    client = TestClient(make_app(tmp_path))
    endpoint = "/api/runs/demo/control"

    # A delayed generation-0 card/click must not mutate the generation-1 node.
    for etype, data in (
        ("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"}),
        ("node_abort", {"node_id": 0, "generation": 0}),
        ("approval_granted", {"node_id": 0, "generation": 0}),
        ("force_confirm", {"node_id": 0, "generation": 0}),
        ("force_ablate", {"node_id": 0, "generation": 0}),
        ("fork", {"from_node_id": 0, "generation": 0}),
        ("promote", {"node_id": 0, "generation": 0}),
    ):
        assert client.post(endpoint, json={"type": etype, "data": data}).status_code == 409
    assert client.post(endpoint, json={"type": "node_reset",
                                      "data": {"node_id": 0, "from_stage": "eval"}}).status_code == 409

    # The exact current generation succeeds and is persisted unchanged (not synthesized on receipt).
    ok = client.post(endpoint, json={"type": "node_reset",
                                    "data": {"node_id": 0, "generation": 1,
                                             "from_stage": "eval"}})
    assert ok.status_code == 200
    resets = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "node_reset"]
    assert [e.data["generation"] for e in resets[-2:]] == [0, 1]

    # Parent-derived inject/merge actions use the same CAS contract after reset.
    idea = {"operator": "improve", "params": {}, "rationale": ""}
    missing = client.post(endpoint, json={"type": "inject_node",
                                          "data": {"idea": idea, "parent_id": 0}})
    assert missing.status_code == 409
    current = client.post(endpoint, json={"type": "inject_node", "data": {
        "idea": idea, "parent_id": 0, "parent_generations": {"0": 2}}})
    assert current.status_code == 200

    assert client.post(endpoint, json=[]).status_code == 400
    assert client.post(endpoint, json={"type": "pause", "data": []}).status_code == 400


def test_config_masked_and_gpu_softfail(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    cfg = client.get("/api/runs/demo/config").json()
    # never leak a real secret value
    assert cfg.get("llm_api_key") in (None, "***")
    gpu = client.get("/api/gpu").json()
    assert "available" in gpu  # True or False, never an error


def test_delete_finished_and_stalled_runs(tmp_path):
    """A finished run deletes; so does a STALLED/zombie one (events but no run_finished, no live
    engine) — the old guard keyed on `finished` and wrongly 409'd a stalled run."""
    _build_run(tmp_path, "done")
    client = TestClient(make_app(tmp_path))
    assert _delete_run(client, "done", rd=tmp_path / "done").status_code == 200
    assert not (tmp_path / "done").exists()

    sr = tmp_path / "stalled"
    sr.mkdir()                       # engine died without run_finished
    (sr / "events.jsonl").write_text('{"seq":0,"type":"run_started","data":{}}\n', encoding="utf-8")
    r = _delete_run(client, "stalled", rd=sr, op="2" * 8)
    assert r.status_code == 200 and not sr.exists()             # was a spurious 409 before the fix


def test_delete_and_reset_fail_closed_when_engine_liveness_is_unknown(tmp_path, monkeypatch):
    """Neither destructive route may run while it cannot tell whether an engine still owns the run."""
    import looplab.serve.reset_route as reset_route
    from looplab.serve import deletion_service

    _build_run(tmp_path, "delete-unknown")
    _build_run(tmp_path, "reset-unknown")
    # Replay validates the task snapshot BEFORE it reads liveness, so without one the reset arm
    # 400s on "no reproducible task snapshot" and never reaches the gate under test.
    _make_resumable(tmp_path / "reset-unknown")
    client = TestClient(make_app(tmp_path))
    # Each route's liveness read moved to the module that owns its transaction. Patching the old
    # `routers.org` / `routers.control` names injected nothing (org no longer even defines it), so
    # both arms were running against REAL liveness and proving nothing about failing closed.
    monkeypatch.setattr(deletion_service, "_engine_liveness", lambda _rd: None)
    monkeypatch.setattr(reset_route, "_engine_liveness", lambda _rd: None)

    # Both fail closed; they classify the same unknown differently, and correctly: deletion is a
    # durable retryable transaction, so unverifiable ownership is a transient 503 the caller should
    # re-attempt, while Replay reports it as a 409 conflict on the run's current state.
    deleted = _delete_run(client, "delete-unknown", rd=tmp_path / "delete-unknown")
    assert deleted.status_code == 503
    assert deleted.json()["detail"]["code"] == "engine_liveness_unknown"
    assert deleted.json()["detail"]["retryable"] is True
    assert (tmp_path / "delete-unknown" / "events.jsonl").is_file()

    reset = client.post("/api/runs/reset-unknown/reset")
    assert reset.status_code == 409
    assert reset.json()["detail"]["code"] == "engine_liveness_unknown"
    assert (tmp_path / "reset-unknown" / "events.jsonl").is_file()


def test_engine_alive_unsupported_flock_is_unknown_and_bool_fails_closed(tmp_path, monkeypatch):
    """Unsupported flock is unknown: reads avoid a false stall and mutation bool callers block."""
    fcntl = pytest.importorskip("fcntl")        # POSIX-only; Windows uses the msvcrt branch
    from looplab.serve.engine_proc import _engine_alive, _engine_liveness
    rd = tmp_path / "r"
    rd.mkdir()
    (rd / "engine.lock").write_text("", encoding="utf-8")

    def _raise(exc):
        def _f(*a, **k):
            raise exc
        return _f
    monkeypatch.setattr(fcntl, "flock", _raise(OSError("flock not supported on this fs")))
    assert _engine_liveness(rd) is None          # unknown is not evidence for a derived stall
    assert _engine_alive(rd) is True            # conservative mutation compatibility blocks
    monkeypatch.setattr(fcntl, "flock", _raise(BlockingIOError("held")))
    assert _engine_liveness(rd) is True
    assert _engine_alive(rd) is True            # genuinely held by a live engine


def test_engine_singleton_fails_closed_on_unsupported_flock(tmp_path, monkeypatch):
    """The OTHER half of the lock: on a FUSE/S3 mount where flock raises a plain OSError, single-writer
    CANNOT be enforced, so the engine singleton now FAILS CLOSED by default — it refuses startup with an
    actionable RuntimeError. The old fail-open no-op let two engines (or the UI server + engine) corrupt
    events.jsonl / mint duplicate seq numbers (P1-12, doc 17 §6.3). The refusal is LOUD, not the older
    silent phantom-'already running' exit; LOOPLAB_ALLOW_UNLOCKED_WRITER=1 restores the degrade-and-run
    opt-in for a single operator who vouches for one engine per run dir. A genuine BlockingIOError is
    still just 'held' -> caller no-ops (not a refusal)."""
    fcntl = pytest.importorskip("fcntl")        # POSIX-only; Windows uses the msvcrt branch
    from looplab.cli import _engine_singleton
    rd = tmp_path / "r"

    def _raise(exc):
        def _f(*a, **k):
            raise exc
        return _f
    monkeypatch.delenv("LOOPLAB_ALLOW_UNLOCKED_WRITER", raising=False)
    monkeypatch.setattr(fcntl, "flock", _raise(OSError("flock not supported on this fs")))
    with pytest.raises(RuntimeError, match="single writer"):   # unsupported lock -> fail CLOSED
        with _engine_singleton(rd):
            pass
    monkeypatch.setenv("LOOPLAB_ALLOW_UNLOCKED_WRITER", "1")    # explicit opt-in -> degrade + run
    with _engine_singleton(rd) as ok:
        assert ok is True
    monkeypatch.delenv("LOOPLAB_ALLOW_UNLOCKED_WRITER", raising=False)
    monkeypatch.setattr(fcntl, "flock", _raise(BlockingIOError("held")))
    with _engine_singleton(rd) as ok:
        assert ok is False           # genuinely HELD by a live engine -> caller no-ops


def test_generic_job_unknown_id(tmp_path):
    """The generic background-job poll endpoint reports `unknown` for an expired/never-seen id (the UI
    re-issues the action) rather than 404ing."""
    client = TestClient(make_app(tmp_path))
    assert client.get("/api/jobs/deadbeef").json() == {"status": "unknown"}


def test_settings_get_put_roundtrip(tmp_path):
    client = TestClient(make_app(tmp_path))
    base = client.get("/api/settings").json()
    assert "settings" in base and "defaults" in base and base["overrides"] == {}
    assert isinstance(base["settings_revision"], str) and base["settings_revision"]
    assert isinstance(base["secret_revision"], str) and base["secret_revision"]
    assert base["settings_revision"] != base["secret_revision"]
    # saving a value EQUAL to the default keeps the override file empty (stores only diffs)
    default_nodes = base["defaults"]["max_nodes"]
    client.put("/api/settings", json={"settings": {"max_nodes": default_nodes}})
    assert client.get("/api/settings").json()["overrides"] == {}
    # a real change is persisted and reflected in the resolved settings
    r = client.put("/api/settings", json={"settings": {"max_nodes": 99, "policy": "mcts"}}).json()
    assert r["overrides"] == {"max_nodes": 99, "policy": "mcts"}
    got = client.get("/api/settings").json()
    assert got["settings"]["max_nodes"] == 99 and got["settings"]["policy"] == "mcts"
    # secrets are never accepted as an override
    client.put("/api/settings", json={"settings": {"llm_api_key": "leak"}})
    assert "llm_api_key" not in client.get("/api/settings").json()["overrides"]


def test_settings_cas_rejects_an_old_delayed_put(tmp_path):
    """A request prepared from an old GET cannot overwrite a newer same-resource save."""
    client = TestClient(make_app(tmp_path))
    loaded = client.get("/api/settings").json()
    old_revision = loaded["settings_revision"]

    newer = client.put("/api/settings", json={
        "settings": {"max_nodes": 93}, "expected_revision": old_revision,
    })
    assert newer.status_code == 200
    new_revision = newer.json()["settings_revision"]
    assert new_revision != old_revision

    # This body represents the older request arriving only after the newer one completed.
    delayed = client.put("/api/settings", json={
        "settings": {"max_nodes": 17}, "expected_revision": old_revision,
    })
    assert delayed.status_code == 409
    assert delayed.json()["detail"] == {
        "code": "settings_revision_conflict",
        "resource": "settings",
        "message": "Settings changed after this form was loaded; reload and retry.",
        "expected_revision": old_revision,
        "current_revision": new_revision,
    }
    current = client.get("/api/settings").json()
    assert current["settings"]["max_nodes"] == 93
    assert current["settings_revision"] == new_revision
    assert current["secret_revision"] == loaded["secret_revision"]


# `None` is NOT invalid: `_expected_revision` treats an explicit null the same as an absent field
# (no CAS check), matching the nullable request schema — its accept path is covered just below.
@pytest.mark.parametrize("bad_revision", ["", 1, True, [], "x" * 257])
def test_settings_put_rejects_invalid_expected_revision(tmp_path, bad_revision):
    client = TestClient(make_app(tmp_path))
    response = client.put("/api/settings", json={
        "settings": {"max_nodes": 22}, "expected_revision": bad_revision,
    })
    assert response.status_code == 400
    assert client.get("/api/settings").json()["overrides"] == {}


def test_settings_put_null_expected_revision_is_no_cas(tmp_path):
    # An explicit `expected_revision: null` means "no optimistic-concurrency check" (== omitting it),
    # so the write proceeds — the nullable-schema contract documented in routers/misc._expected_revision.
    client = TestClient(make_app(tmp_path))
    response = client.put("/api/settings", json={
        "settings": {"max_nodes": 22}, "expected_revision": None,
    })
    assert response.status_code == 200
    assert client.get("/api/settings").json()["overrides"]["max_nodes"] == 22


def test_settings_partial_put_preserves_hidden_overrides_and_null_clears(tmp_path):
    # novelty_gate stays default-False, so setting it True is a real (non-default) hidden override.
    (tmp_path / "ui_settings.json").write_text(
        json.dumps({"novelty_gate": True, "max_nodes": 17}), encoding="utf-8")
    client = TestClient(make_app(tmp_path))

    saved = client.put("/api/settings", json={"settings": {"policy": "mcts"}})
    assert saved.status_code == 200
    assert saved.json()["overrides"] == {
        "novelty_gate": True, "max_nodes": 17, "policy": "mcts"}

    cleared = client.put("/api/settings", json={"settings": {"novelty_gate": None}})
    assert cleared.status_code == 200
    assert cleared.json()["overrides"] == {"max_nodes": 17, "policy": "mcts"}


def test_concurrent_disjoint_settings_puts_do_not_lose_updates(tmp_path, monkeypatch):
    """Two deterministically overlapping PATCH-like PUTs retain both disjoint fields.

    Both request threads arrive immediately before lock acquisition at the first barrier. Neither
    response completes until both serialized transactions have left the lock at the second one.
    Atomic rename without the surrounding transaction could let both requests load ``{}``, after
    which the later rename silently discarded the other request's field.
    """
    from contextlib import contextmanager
    from threading import Barrier

    app = make_app(tmp_path)
    store = app.state.looplab.settings
    original_transaction = store.ui_settings_transaction
    # Rendezvous ceiling, not a latency budget: every party returns the instant the last one
    # arrives, so this bounds only the FAILURE case. At 5s a saturated full-suite host could not
    # always get both pool threads into the transaction in time, and the barrier broke
    # (BrokenBarrierError) while the test passed in isolation.
    _RENDEZVOUS_TIMEOUT_S = 60.0
    arrived = Barrier(3)
    completed = Barrier(3)

    @contextmanager
    def synchronized_transaction():
        arrived.wait(timeout=_RENDEZVOUS_TIMEOUT_S)
        try:
            with original_transaction():
                yield
        finally:
            completed.wait(timeout=_RENDEZVOUS_TIMEOUT_S)

    monkeypatch.setattr(store, "ui_settings_transaction", synchronized_transaction)
    clients = (TestClient(app), TestClient(app))
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            clients[0].put, "/api/settings", json={"settings": {"max_nodes": 91}})
        second = executor.submit(
            clients[1].put, "/api/settings", json={"settings": {"policy": "mcts"}})
        arrived.wait(timeout=_RENDEZVOUS_TIMEOUT_S)
        completed.wait(timeout=_RENDEZVOUS_TIMEOUT_S)
        responses = (first.result(timeout=10), second.result(timeout=10))

    assert all(response.status_code == 200 for response in responses)
    monkeypatch.setattr(store, "ui_settings_transaction", original_transaction)
    overrides = clients[0].get("/api/settings").json()["overrides"]
    assert overrides == {"max_nodes": 91, "policy": "mcts"}


def test_sparse_agent_control_patches_from_stale_tabs_merge_by_setting(tmp_path):
    client = TestClient(make_app(tmp_path))
    baseline = client.get("/api/settings").json()["settings"]["agent_control"]

    first = client.put(
        "/api/settings", json={"settings": {"agent_control": {"timeout": ["strategist"]}}})
    second = client.put(
        "/api/settings", json={"settings": {"agent_control": {"max_nodes": ["boss"]}}})

    assert first.status_code == second.status_code == 200
    merged = second.json()["settings"]["agent_control"]
    assert merged["timeout"] == ["strategist"]
    assert merged["max_nodes"] == ["boss"]
    assert merged["policy"] == baseline["policy"]


def test_boss_routes_never_run_their_whole_log_work_on_the_event_loop(tmp_path, monkeypatch):
    """Every BOSS route below is `async def` because it must `await` its request body, and each then
    ran whole-log blocking work INLINE on the ASGI loop: `chat-log` took the run command sequencer
    and fsync'd a sidecar that can be tens of MiB; `/chat`, `/suggest` and `/command` did a full
    event-log read + fold plus run-file prompt assembly; `report_refresh` took the sequencer AND read
    the entire events.jsonl for its idempotency ledger. Each froze every SSE stream and every other
    handler in the process for its duration, even though all of them carefully offload their LLM
    call. The probe is `asyncio.get_running_loop()`: it returns the loop on the ASGI thread and
    raises RuntimeError anywhere else."""
    import asyncio
    from contextlib import contextmanager

    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    app = make_app(tmp_path)
    srv = app.state.looplab
    on_loop: list[str] = []

    def _probe(name, original):
        def probing(*a, **kw):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass                                # a worker thread — the contract
            else:
                on_loop.append(name)
            return original(*a, **kw)
        return probing

    def _probe_into(sink, original):
        def probing(*a, **kw):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                sink.append(getattr(original, "__name__", "call"))
            return original(*a, **kw)
        return probing

    @contextmanager
    def probing_sequence(*a, **kw):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            on_loop.append("sequence")
        with original_sequence(*a, **kw):
            yield

    original_sequence = srv.commands.sequence
    monkeypatch.setattr(srv.commands, "sequence", probing_sequence)
    monkeypatch.setattr(srv.commands, "run_activity",
                        _probe("run_activity", srv.commands.run_activity))
    monkeypatch.setattr(srv, "state", _probe("state", srv.state))
    client = TestClient(app)

    assert client.post("/api/runs/demo/chat-log",
                       json={"role": "user", "content": "hi"}).status_code == 200
    # The LLM routes soft-fail offline, but their fold + prompt prologue still runs — which is the
    # part under test here.
    for route, payload in (("chat", {"messages": [{"role": "user", "content": "hi"}]}),
                           ("suggest", {"instruction": "try something"}),
                           ("command", {"instruction": "pause the run"})):
        assert client.post(f"/api/runs/demo/{route}", json=payload).status_code == 200
    # An expected_generation that cannot match still exercises each sequenced ledger claim.
    client.post("/api/runs/demo/report_refresh", headers={"Idempotency-Key": "k1"},
                json={"expected_generation": "0" * 64})
    generation = client.get("/api/runs/demo/state").json()["generation"]
    client.post("/api/runs/demo/concepts/lens",
                headers={"Idempotency-Key": "test-receipt::loop::0123456789abcdef"},
                json={"prompt": "group by usage", "expected_generation": generation})
    # /api/genesis is run-independent; its prologue reads the task catalogue, the settings store and
    # every prior report, so it is probed through those instead of the run-scoped hooks above.
    assert on_loop == [], on_loop

    catalogue_on_loop: list[str] = []
    monkeypatch.setattr(srv, "list_tasks_fn", _probe_into(catalogue_on_loop, srv.list_tasks_fn))
    monkeypatch.setattr(srv.settings, "resolved_settings",
                        _probe_into(catalogue_on_loop, srv.settings.resolved_settings))
    client.post("/api/genesis", json={"instruction": "plan a small run"})
    assert catalogue_on_loop == [], catalogue_on_loop


def test_settings_and_secret_puts_never_take_their_blocking_locks_on_the_event_loop(
        tmp_path, monkeypatch):
    """`ui_settings_transaction` / `secret_transaction` each end in `_interprocess_lock(required=
    True)` — a blocking `fcntl.flock(LOCK_EX)` with NO timeout — followed by load/validate/atomic-
    write disk I/O. Both PUTs must `await request.json()`, so they are `async def` and used to run
    that whole transaction INLINE on the ASGI loop: a lock another server process held froze every
    SSE stream and poll on this worker until it was released. The probe is `asyncio.
    get_running_loop()` — it returns the loop on the ASGI thread and raises RuntimeError elsewhere."""
    import asyncio
    from contextlib import contextmanager

    app = make_app(tmp_path)
    store = app.state.looplab.settings
    on_loop: list[str] = []

    def _probe(name, original):
        @contextmanager
        def probing():
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass                                # a worker thread — the contract
            else:
                on_loop.append(name)
            with original():
                yield
        return probing

    monkeypatch.setattr(store, "ui_settings_transaction",
                        _probe("settings", store.ui_settings_transaction))
    monkeypatch.setattr(store, "secret_transaction",
                        _probe("secret", store.secret_transaction))
    client = TestClient(app)

    saved = client.put("/api/settings", json={"settings": {"max_nodes": 11}})
    # Saving a credential now requires BOTH revisions from the latest snapshot: 428 without them,
    # 409 with stale ones. Re-read AFTER the settings save, which moved the settings revision.
    # A request turned away at either precondition never reaches the lock this test is about, so
    # `on_loop` would stay empty for a reason that has nothing to do with the offload.
    snapshot = client.get("/api/settings").json()
    stored = client.put("/api/settings/secret", json={
        "key": "llm_api_key", "value": "sk-x",
        "expected_settings_revision": snapshot["settings_revision"],
        "expected_secret_revision": snapshot["secret_revision"],
    })

    assert saved.status_code == 200 and stored.status_code == 200
    assert on_loop == [], on_loop
    # …and the offload did not break the writes it wraps.
    assert saved.json()["overrides"]["max_nodes"] == 11
    assert stored.json()["set"] is True


def test_settings_put_rejects_bad_shape_and_invalid_value_without_writing(tmp_path):
    client = TestClient(make_app(tmp_path))
    assert client.put("/api/settings", json={"settings": []}).status_code == 400
    invalid = client.put("/api/settings", json={"settings": {"max_nodes": 0}})
    assert invalid.status_code == 422
    assert client.get("/api/settings").json()["overrides"] == {}


def test_settings_store_recovers_from_valid_json_with_wrong_top_level_shape(tmp_path):
    (tmp_path / "ui_settings.json").write_text("[]", encoding="utf-8")
    client = TestClient(make_app(tmp_path))

    loaded = client.get("/api/settings")
    assert loaded.status_code == 200
    assert loaded.json()["overrides"] == {}
    saved = client.put("/api/settings", json={"settings": {"max_nodes": 19}})
    assert saved.status_code == 200
    assert saved.json()["overrides"] == {"max_nodes": 19}


def test_settings_profile_is_persisted_instead_of_diffed_away(tmp_path):
    client = TestClient(make_app(tmp_path))
    response = client.put("/api/settings", json={"settings": {"profile": "thorough"}})
    assert response.status_code == 200
    assert response.json()["overrides"]["profile"] == "thorough"


def _write_snapshot(rd: Path, **overrides):
    """Mimic what `cli run` writes: a masked Settings snapshot the resume path re-reads."""
    import json
    from looplab.core.config import Settings
    (rd / "config.snapshot.json").write_text(
        json.dumps(Settings(**overrides).masked_snapshot(), indent=2), encoding="utf-8")


def test_settings_and_run_config_openapi_contracts_preserve_legacy_runtime(tmp_path):
    """Editor-v2/CAS endpoints must be discoverable without breaking their flat-body clients."""
    from looplab.serve.settings_ui_schema import SETTINGS_UI_SCHEMA_VERSION

    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _write_snapshot(rd, timeout=30.0)
    app = make_app(tmp_path)
    spec = app.openapi()
    paths = spec["paths"]
    components = spec["components"]["schemas"]

    def response_ref(path, method):
        return paths[path][method]["responses"]["200"]["content"][
            "application/json"]["schema"]["$ref"]

    assert response_ref("/api/settings/schema/{version}", "get").endswith(
        "/SettingsUISchemaResponse")
    assert response_ref("/api/settings", "get").endswith("/SettingsSnapshotResponse")
    assert response_ref("/api/settings", "put").endswith("/SettingsUpdateResponse")
    assert response_ref("/api/settings/secret", "put").endswith("/SecretUpdateResponse")
    assert response_ref("/api/runs/{run_id}/config", "get").endswith("/RunConfigResponse")
    assert response_ref("/api/runs/{run_id}/config", "put").endswith(
        "/RunConfigUpdateResponse")
    assert response_ref("/api/runs/{run_id}/state", "get").endswith(
        "/PublicRunStateResponse")

    state_envelope = components["PublicRunStateResponse"]
    assert state_envelope["additionalProperties"] is False
    assert set(state_envelope["required"]) == {
        "state", "seq", "max_seq", "event_count", "generation", "source_integrity",
    }
    # REQUIRED, not optional, and typed rather than an additive key inside the extensible `state`
    # body: it qualifies every number in this envelope, and a receipt a client may find missing is
    # one a client will learn to skip. `additionalProperties: false` on the receipt itself keeps its
    # own shape closed (tests/test_seq_gap_visibility.py drives what it says).
    integrity = components["RunSourceIntegrity"]
    assert integrity["additionalProperties"] is False
    assert set(integrity["required"]) == {"complete"}
    assert {"complete", "good_records", "corrupt_line", "dropped_lines",
            "unreadable"} == set(integrity["properties"])
    state_body = components["PublicRunStateBody"]
    assert state_body["additionalProperties"] is True
    assert {"cards", "cards_projection"} <= set(state_body["required"])
    assert state_body["properties"]["cards_projection"]["$ref"].endswith(
        "/PublicCardsProjectionMetadata")

    # CODEX AGENT: canonical envelopes are strict, while the second documented variant preserves
    # pre-editor-v2 flat dictionaries; both paths still reach the same manual 400/CAS parser.
    def rev_string_variant(prop):
        # `expected_revision` is a NULLABLE optional CAS token: an explicit null == absent (no CAS),
        # matching routers/misc._expected_revision. Pydantic renders that as anyOf[<string...>, null], so
        # the string constraints live on the non-null branch. Assert nullability, then return that branch.
        variants = prop.get("anyOf")
        assert variants is not None, "expected_revision must be a nullable optional (anyOf)"
        assert {"type": "null"} in variants, "expected_revision must accept an explicit null (no CAS)"
        return next(v for v in variants if v.get("type") == "string")

    settings_body = paths["/api/settings"]["put"]["requestBody"]["content"][
        "application/json"]["schema"]
    settings_variants = {variant["title"]: variant for variant in settings_body["anyOf"]}
    canonical_settings = settings_variants["SettingsUpdateRequest"]
    assert canonical_settings["required"] == ["settings"]
    assert canonical_settings["additionalProperties"] is False
    assert rev_string_variant(canonical_settings["properties"]["expected_revision"])["type"] == "string"
    legacy_settings = settings_variants["LegacySettingsUpdateRequest"]
    assert legacy_settings["additionalProperties"] is True
    assert legacy_settings["not"] == {"required": ["settings"]}
    assert rev_string_variant(legacy_settings["properties"]["expected_revision"])["type"] == "string"
    # An invalid reserved envelope matches neither branch: canonical requires an object, while the
    # legacy branch explicitly excludes every body carrying the reserved `settings` member.
    invalid_envelope = {"settings": []}
    assert not isinstance(invalid_envelope["settings"], dict)
    assert all(key in invalid_envelope for key in legacy_settings["not"]["required"])

    secret_body = paths["/api/settings/secret"]["put"]["requestBody"]["content"][
        "application/json"]["schema"]
    assert secret_body["required"] == ["key"]
    assert secret_body["additionalProperties"] is False
    assert secret_body["properties"]["key"]["pattern"] == r"^(?:llm_api_key)$"
    assert rev_string_variant(secret_body["properties"]["expected_revision"])["type"] == "string"

    run_body = paths["/api/runs/{run_id}/config"]["put"]["requestBody"]["content"][
        "application/json"]["schema"]
    run_variants = {variant["title"]: variant for variant in run_body["anyOf"]}
    canonical_run = run_variants["RunConfigUpdateRequest"]
    # The run generation is REQUIRED on both variants and the revision stays optional: the revision
    # is a lost-update guard, the generation says WHICH run this body was composed against. A
    # generated client that omitted it would silently re-configure a replacement run, so it must be
    # visible in the published schema, not only enforced at runtime.
    assert canonical_run["required"] == ["settings", "expected_generation"]
    assert canonical_run["additionalProperties"] is False
    assert rev_string_variant(canonical_run["properties"]["expected_revision"])["pattern"] == r"^[0-9a-f]{64}$"
    assert canonical_run["properties"]["expected_generation"]["pattern"] == r"^[0-9a-f]{64}$"
    legacy_run = run_variants["LegacyRunConfigUpdateRequest"]
    assert legacy_run["additionalProperties"] is True
    assert legacy_run["not"] == {"required": ["settings"]}
    assert legacy_run["required"] == ["expected_generation"]
    assert rev_string_variant(legacy_run["properties"]["expected_revision"])["pattern"] == r"^[0-9a-f]{64}$"
    assert components["RunConfigResponse"]["additionalProperties"] is True
    assert "_looplab_config_meta" in components["RunConfigResponse"]["required"]
    assert components["RunConfigMetadata"]["properties"]["config_revision"][
        "pattern"] == r"^[0-9a-f]{64}$"

    client = TestClient(app)
    public_state = client.get("/api/runs/demo/state")
    assert public_state.status_code == 200
    assert public_state.json()["state"]["cards_projection"]["source_valid"] is True
    ui_schema = client.get(f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}")
    assert ui_schema.status_code == 200 and ui_schema.json()["revision"]
    settings = client.get("/api/settings").json()
    saved = client.put("/api/settings", json={
        "max_nodes": 91,
        "expected_revision": settings["settings_revision"],
    })
    assert saved.status_code == 200
    assert saved.json()["overrides"]["max_nodes"] == 91
    cleared_secret = client.put("/api/settings/secret", json={"key": "llm_api_key"})
    assert cleared_secret.status_code == 200 and cleared_secret.json()["set"] is False

    run_config = client.get("/api/runs/demo/config").json()
    updated = _run_config_put(client, "demo", {
        "timeout": 47.0,
        "expected_revision": run_config["_looplab_config_meta"]["config_revision"],
    })
    assert updated.status_code == 200
    assert updated.json()["config"]["timeout"] == 47.0
    assert updated.json()["config"]["_looplab_config_meta"]["config_revision"]
    assert client.put("/api/settings", json={"settings": []}).status_code == 400
    assert _run_config_put(client, "demo", {"settings": []}).status_code == 400


def test_put_run_config_edits_snapshot_for_resume(tmp_path):
    import json
    from looplab.core.config import settings_from_snapshot
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    # the problematic run's real shape: short timeout, timeout-repair NOT yet enabled
    _write_snapshot(rd, timeout=30.0, inline_repair_reasons=["crash"])
    client = TestClient(make_app(tmp_path))

    r = _run_config_put(client, "demo", {
        "settings": {"timeout": 120.0, "inline_repair_reasons": ["crash", "timeout"]}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and set(body["changed"]) == {"timeout", "inline_repair_reasons"}
    # sending an UNCHANGED value is a no-op (only real diffs are written)
    r2 = _run_config_put(client, "demo", {"settings": {"timeout": 120.0}})
    assert r2.json()["changed"] == []
    # persisted to the snapshot that resume re-reads through the compatibility loader
    snap = json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
    assert snap["timeout"] == 120.0 and snap["inline_repair_reasons"] == ["crash", "timeout"]
    rebuilt = settings_from_snapshot(snap)
    assert rebuilt.timeout == 120.0      # what Engine() would get on resume


def test_legacy_sparse_run_config_is_effective_but_never_backfilled_by_get_or_unrelated_put(
        tmp_path):
    import hashlib
    import json

    _build_run(tmp_path)
    rd = tmp_path / "demo"
    raw = {"timeout": 30.0, "max_parallel": 4, "parallel_build": 3}
    snapshot = rd / "config.snapshot.json"
    snapshot.write_text(json.dumps(raw, separators=(",", ":")), encoding="utf-8")
    before = snapshot.read_bytes()
    expected_revision = hashlib.sha256(json.dumps(
        raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    client = TestClient(make_app(tmp_path))

    shown = client.get("/api/runs/demo/config")
    assert shown.status_code == 200
    body = shown.json()
    assert body["train_monitor"] is False and body["asha_live"] is False
    assert body["watchdog_reflection"] is False
    assert body["concurrent_research_repeat"] is False
    assert body["concurrent_consolidate"] is False
    assert body["eval_parallel"] is None and body["llm_parallel"] is None
    assert body["max_eval_timeout"] == 24 * 3600.0
    assert body["_looplab_config_meta"]["config_revision"] == expected_revision
    assert snapshot.read_bytes() == before

    saved = _run_config_put(client, "demo", {"settings": {"timeout": 45.0}})
    assert saved.status_code == 200
    persisted = json.loads(snapshot.read_text(encoding="utf-8"))
    assert persisted["timeout"] == 45.0
    for field in (
            "train_monitor", "asha_live", "watchdog_reflection",
            "concurrent_research_repeat", "concurrent_consolidate",
            "eval_parallel", "llm_parallel", "max_eval_timeout"):
        assert field not in persisted


def test_run_config_cas_rejects_an_old_delayed_put(tmp_path):
    """A timed-out request prepared from an old GET cannot overwrite a newer same-key save."""
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _write_snapshot(rd, timeout=30.0)
    client = TestClient(make_app(tmp_path))

    loaded = client.get("/api/runs/demo/config").json()
    old_revision = loaded["_looplab_config_meta"]["config_revision"]
    assert len(old_revision) == 64 and int(old_revision, 16) >= 0

    newer = _run_config_put(client, "demo", {
        "settings": {"timeout": 91.0}, "expected_revision": old_revision,
    })
    assert newer.status_code == 200
    new_revision = newer.json()["config"]["_looplab_config_meta"]["config_revision"]
    assert new_revision != old_revision

    delayed = _run_config_put(client, "demo", {
        "settings": {"timeout": 17.0}, "expected_revision": old_revision,
    })
    assert delayed.status_code == 409
    assert delayed.json()["detail"] == {
        "code": "run_config_revision_conflict",
        "resource": "run_config",
        "message": "Run configuration changed after this form was loaded; reload and retry.",
        "expected_revision": old_revision,
        "current_revision": new_revision,
    }
    assert json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))["timeout"] == 91.0
    current = client.get("/api/runs/demo/config").json()
    assert current["_looplab_config_meta"]["config_revision"] == new_revision


# `None` is NOT invalid: an explicit null == absent (no CAS), matching the nullable request schema.
@pytest.mark.parametrize("bad_revision", ["", 1, True, [], "g" * 64, "a" * 65])
def test_put_run_config_rejects_invalid_expected_revision(tmp_path, bad_revision):
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _write_snapshot(rd, timeout=30.0)
    client = TestClient(make_app(tmp_path))

    response = _run_config_put(client, "demo", {
        "settings": {"timeout": 44.0}, "expected_revision": bad_revision,
    })
    assert response.status_code == 400
    assert json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))["timeout"] == 30.0


def test_put_run_config_null_expected_revision_is_no_cas(tmp_path):
    # An explicit `expected_revision: null` means "no optimistic-concurrency check" (== omitting it),
    # so the write proceeds and the snapshot is updated (nullable-schema contract).
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _write_snapshot(rd, timeout=30.0)
    client = TestClient(make_app(tmp_path))

    response = _run_config_put(client, "demo", {
        "settings": {"timeout": 44.0}, "expected_revision": None,
    })
    assert response.status_code == 200
    assert json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))["timeout"] == 44.0


def test_concurrent_run_config_puts_serialize_the_complete_cas_transaction(
        tmp_path, monkeypatch):
    """The loser cannot read the old snapshot while the winner is paused immediately before write."""
    import looplab.serve.routers.runs as runs_router

    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _write_snapshot(rd, timeout=30.0)
    clients = [TestClient(make_app(tmp_path)), TestClient(make_app(tmp_path))]
    revision = clients[0].get(
        "/api/runs/demo/config").json()["_looplab_config_meta"]["config_revision"]

    lock = threading.Lock()
    attempts_lock = threading.Lock()
    second_attempted_lock = threading.Event()
    attempts = 0

    class ObservedLock:
        def __enter__(self):
            nonlocal attempts
            with attempts_lock:
                attempts += 1
                if attempts == 2:
                    second_attempted_lock.set()
            lock.acquire()
            return self

        def __exit__(self, *_exc):
            lock.release()

    monkeypatch.setattr(runs_router, "_run_config_thread_lock", lambda _path: ObservedLock())
    original_write = runs_router.atomic_write_text
    first_at_write = threading.Event()
    release_first = threading.Event()
    writes_lock = threading.Lock()
    writes = 0

    def delayed_first_write(path, payload):
        nonlocal writes
        with writes_lock:
            writes += 1
            is_first = writes == 1
        if is_first:
            first_at_write.set()
            assert release_first.wait(5), "test did not release the first config writer"
        return original_write(path, payload)

    monkeypatch.setattr(runs_router, "atomic_write_text", delayed_first_write)

    def save(client, timeout):
        return _run_config_put(client, "demo", {
            "settings": {"timeout": timeout}, "expected_revision": revision,
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(save, clients[0], 101.0)
        assert first_at_write.wait(5)
        second = pool.submit(save, clients[1], 202.0)
        assert second_attempted_lock.wait(5)
        try:
            assert not second.done(), "the competing PUT crossed the run-config transaction lock"
        finally:
            release_first.set()
        responses = first.result(timeout=10), second.result(timeout=10)

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))["timeout"] == 101.0


def test_put_run_config_null_clears_optional_but_not_required_or_read_only_fields(tmp_path):
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _write_snapshot(rd, timeout=30.0, max_seconds=90.0, profile="default")
    client = TestClient(make_app(tmp_path))
    metadata = client.get("/api/runs/demo/config").json()["_looplab_config_meta"]
    assert metadata["run_read_only_fields"] == ["eval_env", "profile"]

    cleared = _run_config_put(client, "demo", {"settings": {"max_seconds": None}})
    assert cleared.status_code == 200
    assert cleared.json()["changed"] == ["max_seconds"]
    assert json.loads((rd / "config.snapshot.json").read_text(
        encoding="utf-8"))["max_seconds"] is None

    required = _run_config_put(client, "demo", {"settings": {"timeout": None}})
    assert required.status_code == 422
    assert "timeout" in required.json()["detail"]
    profile = _run_config_put(client, "demo", {"settings": {"profile": None}})
    assert profile.status_code == 422
    assert "profile can't be changed per-run" in profile.json()["detail"]
    # `eval_env` is read-only for a DIFFERENT reason and the refusal has to be its own: re-entry
    # restores it from `run_started` (invariant #6), so a saved change would be ignored by the very
    # engine the operator is editing. Accepting it silently is the worst of the three options.
    env = _run_config_put(client, "demo", {"settings": {"eval_env": {"VS_LOCAL_DATA_ROOT": "/x"}}})
    assert env.status_code == 422
    assert "eval_env can't be changed per-run" in env.json()["detail"]
    persisted = json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
    assert persisted["timeout"] == 30.0 and persisted["profile"] == "default"
    assert persisted.get("eval_env") in (None, {})


def test_put_run_config_fails_closed_when_interprocess_lock_is_unavailable(
        tmp_path, monkeypatch):
    from contextlib import contextmanager
    from looplab.events.eventstore import EventStoreLockError
    import looplab.serve.routers.runs as runs_router

    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _write_snapshot(rd, timeout=30.0)

    @contextmanager
    def unavailable(path, *, required=False):
        assert required is True
        raise EventStoreLockError(path, OSError("injected unsupported lock"))
        yield  # pragma: no cover - makes this a context manager; acquisition must fail

    client = TestClient(make_app(tmp_path))
    # Read the fence BEFORE the lock is broken: the PUT must fail on the lock, not on a body the
    # route rejects before it ever tries to acquire one.
    generation = client.get("/api/runs/demo/state").json()["generation"]
    monkeypatch.setattr(runs_router, "_interprocess_lock", unavailable)
    response = _run_config_put(
        client, "demo", {"settings": {"timeout": 44.0}}, generation=generation)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "run_config_lock_unavailable"
    assert json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))["timeout"] == 30.0


def test_put_run_config_rejects_invalid_value_naming_the_field(tmp_path):
    import json
    _build_run(tmp_path)
    _write_snapshot(tmp_path / "demo", timeout=30.0)
    client = TestClient(make_app(tmp_path))
    # the exact bug the user hit: Seeds = -1 (n_seeds has ge=1)
    r = _run_config_put(client, "demo", {"settings": {"n_seeds": -1}})
    assert r.status_code == 422
    assert "n_seeds" in r.json()["detail"]          # the offending field is surfaced, not an opaque 422
    # the bad value never reached disk
    snap = json.loads((tmp_path / "demo" / "config.snapshot.json").read_text(encoding="utf-8"))
    assert snap["n_seeds"] == 3


def test_put_run_config_allowed_while_engine_live(tmp_path):
    """Saving the snapshot while the engine is live is SAFE (the engine never re-reads it) — the write
    succeeds and reports engine_running=True so the UI can say "applies on restart" / offer pause+resume."""
    import json
    from looplab.cli import _engine_singleton
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _write_snapshot(rd, timeout=30.0)
    client = TestClient(make_app(tmp_path))
    with _engine_singleton(rd):          # a live engine holds the lock
        r = _run_config_put(client, "demo", {"settings": {"timeout": 99.0}})
        assert r.status_code == 200
        assert r.json()["engine_running"] is True
    assert json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))["timeout"] == 99.0


def test_put_run_config_preserves_secret_and_unknown_keys(tmp_path):
    import json
    from looplab.core.config import Settings
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    snap = Settings(timeout=30.0).masked_snapshot()
    snap["llm_api_key"] = "***"
    snap["some_future_key"] = "keepme"   # forward-compat key not in Settings
    (rd / "config.snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
    client = TestClient(make_app(tmp_path))

    r = _run_config_put(client, "demo", {"settings": {"timeout": 77.0, "llm_api_key": "leak"}})
    assert r.status_code == 200
    out = json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
    assert out["timeout"] == 77.0
    assert out["llm_api_key"] == "***"            # secret never overwritten via this endpoint
    assert out["some_future_key"] == "keepme"     # unknown key preserved verbatim
    assert r.json()["config"]["some_future_key"] == "keepme"  # response model is forward-compatible
    assert "llm_api_key" not in r.json()["changed"]


def test_run_config_uses_folded_launch_pins_and_repairs_legacy_snapshot_drift(tmp_path):
    """Old UI saves could change the snapshot while re-entry kept using run_started. API/form truth
    must be the effective folded values, and the next ordinary save should heal the stale snapshot."""
    from looplab.core.config import RUN_START_PINNED_FIELDS, Settings

    rd = tmp_path / "pinned"
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "pinned", "task_id": "t", "goal": "g", "direction": "max",
        "holdout_fraction": 0.4, "holdout_select": True,
        "select_verifier": True, "verifier_ci_tie": True,
        "select_verifier_samples": 7,
        "card_driven_selection": True,
        "speculation_depth": 4,
    })
    # Every pinned field must actually DRIFT from the run_started pin above, or the mismatch/heal
    # assertions below silently stop covering it. `card_driven_selection` is spelled False here for
    # exactly that reason: its product default flipped to True on 2026-08-04, so leaving it implicit
    # made the stale snapshot agree with the pin by accident.
    snapshot = Settings(
        timeout=30.0, holdout_fraction=0.1, holdout_select=False,
        select_verifier=False, verifier_ci_tie=False, select_verifier_samples=1,
        card_driven_selection=False, speculation_depth=0,
    ).masked_snapshot()
    (rd / "config.snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    client = TestClient(make_app(tmp_path))

    shown = client.get("/api/runs/pinned/config").json()
    assert shown["holdout_fraction"] == 0.4 and shown["holdout_select"] is True
    assert shown["select_verifier"] is True and shown["verifier_ci_tie"] is True
    assert shown["select_verifier_samples"] == 7
    assert shown["card_driven_selection"] is True
    assert shown["speculation_depth"] == 4
    meta = shown["_looplab_config_meta"]
    assert set(meta["run_start_pinned_fields"]) == RUN_START_PINNED_FIELDS
    assert set(meta["snapshot_mismatch_fields"]) == RUN_START_PINNED_FIELDS

    saved = _run_config_put(client, "pinned", {"settings": {"timeout": 45.0}})
    assert saved.status_code == 200
    assert set(saved.json()["normalized_pinned"]) == RUN_START_PINNED_FIELDS
    healed = json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
    for key in RUN_START_PINNED_FIELDS:
        assert healed[key] == shown[key]


def test_run_config_survives_a_run_that_ratcheted_its_own_speculation_depth(tmp_path):
    """DOOR 2: a routine UI edit must not destroy a run's launch config, and needs no operator intent.

    The sibling above covers only a log with NO settle row, where `run_started`'s depth and the folded
    `speculation_depth` are the same number. Once the AUTO depth is allowed to ratchet itself down,
    they are two different facts, and reading the wrong one here was PERMANENT: measured through this
    route, `run_started` pinned 4, `config.snapshot.json` held the `-1` AUTO sentinel, GET reported 0
    — a value `run_started` never contained — a PUT of an unrelated `timeout` wrote that 0 into the
    snapshot as a "legacy drift repair", a PUT trying to restore `-1` came back 422 "run-start pinned
    settings cannot be changed after creation", and the run's next re-entry refused it outright with
    "this process did not admit Card speculation at all… but the log records a speculative prefix".

    Two rules close it: the pinned contract reads `run_started`'s own value, and the AUTO SENTINEL is
    not drift (`core/config.py::run_start_pinned_disagreement`) — it is the launch-time request the
    pin is the resolution OF, so repairing it would replace the operator's standing spelling with one
    run's resolved integer, irreversibly.
    """
    from looplab.core.config import Settings

    rd = tmp_path / "ratcheted"
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "ratcheted", "task_id": "t", "goal": "g", "direction": "max",
        "card_driven_selection": True, "speculation_depth": 4,
        "speculation_gate_receipt_digest": "sha256:" + "a" * 64,
        "speculation_policy_scope": "greedy",
    })
    store.append("speculation_depth_settled", {"depth": 0, "previous": 4, "reason": "fast evals"})
    assert fold(store.read_all()).speculation_depth == 0            # the run's EFFECTIVE treatment
    assert fold(store.read_all()).speculation_depth_pinned == 4     # what run start committed
    (rd / "config.snapshot.json").write_text(json.dumps(
        Settings(card_driven_selection=True, speculation_depth=-1, timeout=30.0).masked_snapshot()),
        encoding="utf-8")
    client = TestClient(make_app(tmp_path))

    shown = client.get("/api/runs/ratcheted/config").json()
    assert shown["speculation_depth"] == -1                         # the operator's own spelling
    assert "speculation_depth" in shown["_looplab_config_meta"]["run_start_pinned_fields"]
    assert shown["_looplab_config_meta"]["snapshot_mismatch_fields"] == []

    saved = _run_config_put(client, "ratcheted", {"settings": {"timeout": 45.0}})
    assert saved.status_code == 200
    assert saved.json()["normalized_pinned"] == []
    healed = json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
    assert healed["timeout"] == 45.0                                # the edit the operator asked for
    assert healed["speculation_depth"] == -1                        # ...and nothing else moved

    # The sentinel round-trips; a real change of the treatment is still refused.
    assert _run_config_put(client, "ratcheted",
                           {"settings": {"speculation_depth": -1}}).status_code == 200
    refused = _run_config_put(client, "ratcheted", {"settings": {"speculation_depth": 2}})
    assert refused.status_code == 422 and "speculation_depth" in refused.json()["detail"]
    assert json.loads(
        (rd / "config.snapshot.json").read_text(encoding="utf-8"))["speculation_depth"] == -1

    # The run is still resumable: the snapshot the CLI restores from spells AUTO, exactly as launched.
    from looplab.core.config import settings_from_snapshot
    assert settings_from_snapshot(
        json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
    ).speculation_depth == -1


def test_put_run_config_rejects_every_run_start_pinned_field(tmp_path):
    from looplab.core.config import RUN_START_PINNED_FIELDS, Settings

    rd = tmp_path / "pinned"
    EventStore(rd / "events.jsonl").append("run_started", {
        "run_id": "pinned", "task_id": "t", "goal": "g", "direction": "max",
        "holdout_fraction": 0.4, "holdout_select": True,
        "select_verifier": True, "verifier_ci_tie": True,
        "select_verifier_samples": 7,
        "card_driven_selection": True,
        "speculation_depth": 4,
    })
    (rd / "config.snapshot.json").write_text(
        json.dumps(Settings().masked_snapshot()), encoding="utf-8")
    client = TestClient(make_app(tmp_path))
    attempted = {
        "holdout_fraction": 0.2,
        "holdout_select": False,
        "select_verifier": False,
        "verifier_ci_tie": False,
        "select_verifier_samples": 2,
        "card_driven_selection": False,
        "speculation_depth": 0,
    }

    assert set(attempted) == RUN_START_PINNED_FIELDS
    for field, value in attempted.items():
        response = _run_config_put(client, "pinned", {"settings": {field: value}})
        assert response.status_code == 422, field
        assert field in response.json()["detail"]


def test_put_run_config_rejects_malformed_json_and_non_object_shapes(tmp_path):
    _build_run(tmp_path)
    _write_snapshot(tmp_path / "demo", timeout=30.0)
    client = TestClient(make_app(tmp_path))

    assert client.put(
        "/api/runs/demo/config", content="{", headers={"Content-Type": "application/json"},
    ).status_code == 400
    assert client.put("/api/runs/demo/config", json=[]).status_code == 400
    assert _run_config_put(client, "demo", {"settings": []}).status_code == 400
    persisted = json.loads(
        (tmp_path / "demo" / "config.snapshot.json").read_text(encoding="utf-8"))
    assert persisted["timeout"] == 30.0


def test_put_run_config_repairs_trust_gate_after_append_failure_without_duplicate(
        tmp_path, monkeypatch):
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _write_snapshot(rd, trust_gate="audit")
    client = TestClient(make_app(tmp_path))
    original_append = EventStore.append
    failed = False

    def fail_first_gate_append(self, event_type, data, *args, **kwargs):
        nonlocal failed
        if event_type == "trust_gate_changed" and not failed:
            failed = True
            raise OSError("injected append failure")
        return original_append(self, event_type, data, *args, **kwargs)

    monkeypatch.setattr(EventStore, "append", fail_first_gate_append)
    first = _run_config_put(client, "demo", {"settings": {"trust_gate": "gate"}})
    assert first.status_code == 500
    assert json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))["trust_gate"] == "gate"
    assert fold(EventStore(rd / "events.jsonl").read_all()).trust_gate == "audit"

    retry = _run_config_put(client, "demo", {"settings": {"trust_gate": "gate"}})
    assert retry.status_code == 200
    assert retry.json()["changed"] == []
    assert retry.json()["trust_gate_event_appended"] is True
    again = _run_config_put(client, "demo", {"settings": {"trust_gate": "gate"}})
    assert again.status_code == 200 and again.json()["trust_gate_event_appended"] is False
    gate_events = [
        event for event in EventStore(rd / "events.jsonl").read_all()
        if event.type == "trust_gate_changed"
    ]
    assert len(gate_events) == 1
    assert fold(EventStore(rd / "events.jsonl").read_all()).trust_gate == "gate"


def test_boss_command_flags_stalled_run(tmp_path, monkeypatch):
    """On a STALLED run the boss must be TOLD so (its context says RUN STATUS: STALLED) — otherwise it
    can't tell a dead run from a healthy one and only chats instead of resuming/repairing."""
    sr = tmp_path / "z"
    sr.mkdir()                         # engine died after run_started (zombie)
    (sr / "events.jsonl").write_text(
        '{"seq":0,"type":"run_started","data":{"run_id":"z","task_id":"t","goal":"g","direction":"max"}}\n',
        encoding="utf-8")

    class _Capture:                                         # records the system prompt; emits on turn 1
        def __init__(self):
            self.sys = ""
        def chat(self, messages, tools=None, tool_choice=None):
            self.sys = messages[0]["content"]
            return {"tool_calls": [{"id": "e", "function": {
                "name": "emit", "arguments": {"reply": "ok", "actions": []}}}]}
    cap = _Capture()
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s, **_kw: cap)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/z/command", json={"instruction": "what now?"}).json()
    assert r["ok"] is True
    assert "STALLED" in cap.sys                             # boss was told the run is stalled -> can act


def test_put_run_config_404_without_snapshot(tmp_path):
    _build_run(tmp_path)                  # _build_run does NOT write config.snapshot.json
    client = TestClient(make_app(tmp_path))
    r = _run_config_put(client, "demo", {"settings": {"timeout": 50.0}})
    assert r.status_code == 404


def test_tasks_catalogue(tmp_path):
    client = TestClient(make_app(tmp_path))
    tasks = client.get("/api/tasks").json()["tasks"]
    assert any(t["name"] == "toy_task.json" and t["goal"] for t in tasks)


def test_start_validation_and_env(tmp_path, monkeypatch):
    import looplab.serve.server as server
    spawned = {}

    def fake_popen(cmd, **kw):
        spawned["cmd"] = cmd
        spawned["env"] = kw.get("env", {})
        class _P:  # noqa: D401 - stub
            pass
        return _P()
    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)

    client = TestClient(make_app(tmp_path))
    # missing fields / nonexistent task -> 400
    assert client.post("/api/start", json={"run_id": "x"}).status_code == 400
    assert client.post("/api/start", json={"task_file": "nope.json", "run_id": "x"}).status_code == 400
    # a real task spawns the engine with per-run settings as LOOPLAB_* env
    ok = client.post("/api/start", json={
        "task_file": str(TASK), "run_id": "fromui",
        "settings": {"max_nodes": 3, "backend": "toy", "require_approval": True}})
    assert ok.status_code == 200
    assert spawned["env"]["LOOPLAB_MAX_NODES"] == "3"
    assert spawned["env"]["LOOPLAB_REQUIRE_APPROVAL"] == "true"
    assert (tmp_path / "fromui" / "ui_meta.json").exists()
    # a second start on the same id is refused once the run has events
    (tmp_path / "fromui" / "events.jsonl").write_text("{}\n", encoding="utf-8")
    assert client.post("/api/start", json={"task_file": str(TASK), "run_id": "fromui"}).status_code == 409


def test_concurrent_start_reserves_run_before_popen(tmp_path, monkeypatch):
    """Two requests that both pass the advisory no-events preflight still launch exactly one engine.

    The first Popen is held behind a barrier, keeping the engine.lock window open while the second
    request reaches the same run. The durable start lease must make that second request a 409 rather
    than a second detached child.
    """
    import looplab.serve.routers.control as control_router

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocked_spawn(args, **kwargs):
        calls.append((args, kwargs))
        entered.set()
        assert release.wait(3), "test did not release the blocked start"
        # Return a PID the OS can prove belongs to a live process. A made-up dead PID is now
        # intentionally retired immediately by the spawn-lease hardening, which would authorize a
        # later (non-overlapping) retry and make this concurrency fixture test the wrong boundary.
        return os.getpid()

    monkeypatch.setattr(control_router, "_spawn_engine", blocked_spawn)
    client = TestClient(make_app(tmp_path))
    payload = {"task_file": str(TASK), "run_id": "one-owner"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, "/api/start", json=payload)
        assert entered.wait(3), "first request never reached Popen"
        second = pool.submit(client.post, "/api/start", json=payload)
        release.set()
        responses = [first.result(timeout=5), second.result(timeout=5)]

    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len(calls) == 1                                   # exactly one detached child, ever
    conflict = next(r for r in responses if r.status_code == 409)
    detail = conflict.json()["detail"]
    # WHICH fail-closed code the loser gets depends on how far the winner got before the loser
    # reached the sequencer, and that ordering is not something this fixture controls: an unresolved
    # spawn claim answers `start_uncertain`, a live engine `start_in_progress`, and a winner that
    # already recorded its spawn leaves a directory that simply owns the name (`run_id_conflict`).
    # Pinning one of the three made this test hostage to that race. What must hold is the property:
    # the loser is REFUSED, with a code from the fail-closed set — never an overwrite, and never a
    # code that invites one.
    assert detail["code"] in {
        "start_uncertain", "start_in_progress", "run_id_conflict",
        "external_start_uncertain", "external_start_in_progress",
    }, detail
    assert "remediation" in detail or detail.get("field_errors")   # always an actionable next step


def test_inject_node_control_append(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/control", json={"type": "inject_node", "data": {
        "idea": {"operator": "manual", "params": {"x": 0.5}, "rationale": "hand"}, "parent_id": None}})
    assert r.status_code == 200 and r.json()["type"] == "inject_node"
    st = fold(EventStore(tmp_path / "demo" / "events.jsonl").read_all())
    assert st.inject_requests and st.inject_requests[0]["idea"]["operator"] == "manual"


@pytest.mark.parametrize("unavailable", ["tombstoned", "aborted"])
def test_inject_rejects_unavailable_parent(tmp_path, unavailable):
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    store = EventStore(rd / "events.jsonl")
    if unavailable == "tombstoned":
        store.append("node_tombstoned", {"node_ids": [0]})
    else:
        store.append("node_abort", {"node_id": 0, "generation": 0})
    before = sum(event.type == "inject_node" for event in store.read_all())

    client = TestClient(make_app(tmp_path))
    response = client.post("/api/runs/demo/control", json={"type": "inject_node", "data": {
        "idea": {"operator": "manual", "params": {}, "rationale": ""},
        "parent_id": 0,
        "parent_generations": {"0": 0},
    }})

    assert response.status_code == 409
    assert unavailable in response.json()["detail"]
    assert sum(event.type == "inject_node" for event in store.read_all()) == before


@pytest.mark.parametrize("unavailable", ["tombstoned", "aborted"])
def test_cross_run_inject_rejects_unavailable_source(tmp_path, unavailable):
    _build_run(tmp_path, "source")
    _build_run(tmp_path, "destination")
    source_store = EventStore(tmp_path / "source" / "events.jsonl")
    if unavailable == "tombstoned":
        source_store.append("node_tombstoned", {"node_ids": [0]})
    else:
        source_store.append("node_abort", {"node_id": 0, "generation": 0})
    destination_store = EventStore(tmp_path / "destination" / "events.jsonl")
    before = sum(event.type == "inject_node" for event in destination_store.read_all())

    client = TestClient(make_app(tmp_path))
    response = client.post("/api/runs/destination/control", json={
        "type": "inject_node",
        "data": {"source_run": "source", "source_node": 0},
    })

    assert response.status_code == 409
    assert unavailable in response.json()["detail"]
    assert sum(event.type == "inject_node" for event in destination_store.read_all()) == before


def test_chat_suggest_health_softfail(tmp_path):
    # These hit the LLM endpoint; whether or not a model is reachable they must return 200 with a
    # well-formed envelope (ok: bool) — never raise. Asserts the shape, not the model output.
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    c = client.post("/api/runs/demo/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert c.status_code == 200 and "ok" in c.json()
    s = client.post("/api/runs/demo/suggest", json={"instruction": "try a higher degree"})
    assert s.status_code == 200 and "ok" in s.json()
    # The health probe is a revision-fenced POST now (the old GET is retired), and it is a PAID
    # provider call, so it is bound to the exact saved snapshot the caller displayed. The property
    # here is unchanged: whether or not a model is reachable, it answers a well-formed envelope
    # rather than raising.
    snapshot = client.get("/api/settings").json()
    h = client.post("/api/llm/health", json={
        "expected_settings_revision": snapshot["settings_revision"],
        "expected_secret_revision": snapshot["secret_revision"],
        "operation_id": "7c1f1a2e-9b0d-4e6a-8f31-5c2d7e4a9b60",
    })
    assert h.status_code == 200, h.text
    assert "ok" in h.json()


def test_chat_returns_trace_with_user_and_completion(tmp_path, monkeypatch):
    """A successful /chat reply must carry a langfuse-style `trace` whose prompt includes the user's
    ACTUAL message (not just the system prompt) plus the completion — the Dock chat-trace card depends
    on this contract, so a dropped/renamed key must fail CI."""
    _build_run(tmp_path)
    import looplab.serve.server as server

    class _FakeClient:
        model = "fake-model"

        def complete_text(self, messages):
            return "a grounded answer"

    monkeypatch.setattr(server, "make_llm_client", lambda *a, **k: _FakeClient())
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/chat",
                    json={"messages": [{"role": "user", "content": "why did node 1 fail?"}]})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["text"] == "a grounded answer"
    tr = body["trace"]
    assert tr["model"] == "fake-model"
    assert tr["completion"] == "a grounded answer"
    assert tr["user"] == "why did node 1 fail?"          # the real input is captured in the trace
    assert tr["system"]                                   # system prompt (run/node context) present


def test_cors_is_allowlisted_not_wildcard(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    # an arbitrary web page the operator has open must NOT be allowed to drive the control-plane
    evil = client.get("/api/runs", headers={"Origin": "http://evil.example"})
    assert evil.headers.get("access-control-allow-origin") != "*"
    assert evil.headers.get("access-control-allow-origin") in (None, "")
    # the Vite dev server origin is still allowed (dev workflow preserved)
    ok = client.get("/api/runs", headers={"Origin": "http://localhost:5173"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_cross_origin_simple_post_is_rejected_before_mutation(tmp_path, monkeypatch):
    """CORS only hides a response; a simple cross-site POST still executes unless the server checks
    Origin. This matters in the default tokenless local mode, where a web page could otherwise append
    control events to a localhost LoopLab server."""
    _build_run(tmp_path)
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    rd = tmp_path / "demo"
    before = list(iter_jsonl(rd / "events.jsonl"))
    client = TestClient(make_app(tmp_path))

    blocked = client.post(
        "/api/runs/demo/control",
        content='{"type":"run_abort","data":{"reason":"cross-site"}}',
        headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
    )

    assert blocked.status_code == 403
    assert list(iter_jsonl(rd / "events.jsonl")) == before
    allowed = client.post(
        "/api/runs/demo/control",
        json={"type": "pause", "data": {}},
        headers={"Origin": "http://localhost:5173"},
    )
    assert allowed.status_code == 200


def test_dns_rebinding_host_cannot_self_authorize_origin(tmp_path, monkeypatch):
    """Origin and Host are both attacker-controlled during DNS rebinding; equality is not trust."""
    _build_run(tmp_path)
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_UI_HOSTS", raising=False)
    rd = tmp_path / "demo"
    before = list(iter_jsonl(rd / "events.jsonl"))
    client = TestClient(make_app(tmp_path))

    rebound = client.post(
        "/api/runs/demo/control",
        json={"type": "pause", "data": {}},
        headers={"Host": "evil.example:8765", "Origin": "http://evil.example:8765"},
    )
    assert rebound.status_code == 421
    assert list(iter_jsonl(rd / "events.jsonl")) == before

    local = client.post(
        "/api/runs/demo/control",
        json={"type": "pause", "data": {}},
        headers={"Host": "localhost:8765", "Origin": "http://localhost:8765"},
    )
    assert local.status_code == 200


def test_explicit_remote_host_allowlist(tmp_path, monkeypatch):
    _build_run(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_HOSTS", "research.example:9443")
    client = TestClient(make_app(tmp_path))
    response = client.post(
        "/api/runs/demo/control",
        json={"type": "pause", "data": {}},
        headers={"Host": "research.example:9443", "Origin": "http://research.example:9443"},
    )
    assert response.status_code == 200


def test_configured_host_is_trusted_as_mutation_origin_behind_a_proxy(tmp_path, monkeypatch):
    # A jupyter-server-proxy deployment rewrites the Host to the internal backend (127.0.0.1), so the
    # server's request.base_url is internal while the browser's Origin stays the PUBLIC host. A host in
    # LOOPLAB_UI_HOSTS must therefore be trusted as a mutation Origin too — else every POST 403s ("Could
    # not start the chat") even though GETs (unguarded) pass. Host defaults to testserver here (the
    # in-process exception), standing in for the internal Host the proxy supplies.
    _build_run(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_HOSTS", "research.example")
    client = TestClient(make_app(tmp_path))
    ok = client.post("/api/runs/demo/control", json={"type": "pause", "data": {}},
                     headers={"Origin": "https://research.example"})
    assert ok.status_code == 200
    evil = client.post("/api/runs/demo/control", json={"type": "pause", "data": {}},
                       headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403          # a non-configured origin is still rejected


def test_sse_emits_state_snapshot(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    with client.stream("GET", "/api/runs/demo/events") as resp:
        assert resp.status_code == 200
        chunk = next(resp.iter_lines())
        # the very first SSE frame is an id/state/data block
        for _ in range(5):
            if "state" in chunk or "id:" in chunk:
                break
            chunk = next(resp.iter_lines())
        assert "id:" in chunk or "state" in chunk


def test_sse_reemits_for_generation_and_event_count_without_a_seq_change(tmp_path, monkeypatch):
    """Generation/count are snapshot identity too; neither may be hidden by seq-only dedupe."""
    import looplab.serve.appstate as appstate

    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    snapshots = [
        ("a" * 64, 1, True, False),
        ("a" * 64, 2, True, False),
        ("b" * 64, 2, True, False),
        ("b" * 64, 2, False, True),
    ]
    calls = [0]

    def state_payload(_self, _rd, upto_seq=None):
        assert upto_seq is None
        generation, event_count, alive, finished = snapshots[min(calls[0], len(snapshots) - 1)]
        calls[0] += 1
        return {
            "state": {"engine_running": alive, "finished": finished,
                      "phase": "finished" if finished else "search"},
            "seq": 7, "max_seq": 7, "generation": generation,
            "event_count": event_count,
        }

    monkeypatch.setattr(appstate.AppState, "state_payload", state_payload)
    response = TestClient(make_app(tmp_path)).get("/api/runs/demo/events")
    frames = [frame for frame in response.text.split("\n\n") if "event: state" in frame]
    payloads = [json.loads(next(line[6:] for line in frame.splitlines()
                               if line.startswith("data: "))) for frame in frames]

    assert [payload["event_count"] for payload in payloads] == [1, 2, 2, 2]
    assert [payload["generation"] for payload in payloads] == [
        "a" * 64, "a" * 64, "b" * 64, "b" * 64]
    assert {payload["seq"] for payload in payloads} == {7}


def test_sse_done_waits_for_finished_engine_to_exit(tmp_path, monkeypatch):
    """A folded run_finished event is a FINISHING state while the driver still owns engine.lock.

    The stream must deliver the live finished snapshot, stay connected, then emit a second snapshot
    and ``done`` only after liveness flips false. Otherwise the browser closes/reconnects every 2.5s
    throughout terminal write-out.
    """
    import looplab.serve.appstate as appstate

    _build_run(tmp_path)
    probes = iter((True, False))
    monkeypatch.setattr(appstate, "_engine_liveness", lambda _rd: next(probes, False))
    client = TestClient(make_app(tmp_path))

    response = client.get("/api/runs/demo/events")
    assert response.status_code == 200
    frames = [frame for frame in response.text.split("\n\n") if frame]
    state_frames = [frame for frame in frames if "event: state" in frame]
    done_index = next(i for i, frame in enumerate(frames) if "event: done" in frame)

    assert len(state_frames) == 2
    assert '"engine_running": true' in state_frames[0]
    assert '"engine_running": false' in state_frames[1]
    assert done_index > frames.index(state_frames[1])


def test_sse_done_waits_for_error_finalize_recovery(tmp_path, monkeypatch):
    """A dead driver plus run_finished(error) is finalization-stalled, not terminal-ready."""
    rd = tmp_path / "recovering"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "recovering", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("run_abort", {"reason": "operator"})
    store.append("run_finished", {"reason": "error", "error": "late wrap-up failed"})

    # Let the stream expose the stalled/error snapshot first, then model a successful same-intent
    # recovery so the request terminates and the ordering is assertable without an endless client.
    #
    # The recovery is ordered against the stream's OWN first read, not a wall clock. A
    # `threading.Timer(0.6, ...)` armed before `make_app` raced app construction plus request
    # dispatch: on a loaded full-suite host that exceeded 0.6s, so the recovery was already durable
    # when the stream computed its first payload, the opening frame read `finished`, and no state
    # frame ever showed `finalizing`. `state_payload` is the exact seam the stream polls (it is
    # captured by `build_router`, so patch the CLASS before `make_app` rather than the instance).
    recovered = threading.Event()

    def finish_recovery():
        store.append("run_finished", {"reason": "aborted"})
        recovered.set()

    from looplab.serve.appstate import AppState
    original_state_payload = AppState.state_payload

    def payload_then_recover(self, *args, **kwargs):
        payload = original_state_payload(self, *args, **kwargs)
        # Recover only AFTER the stalled snapshot has been computed, so the first emitted frame is
        # guaranteed to be the pre-recovery one no matter how the host is scheduled.
        if not recovered.is_set():
            finish_recovery()
        return payload

    monkeypatch.setattr(AppState, "state_payload", payload_then_recover)
    response = TestClient(make_app(tmp_path)).get("/api/runs/recovering/events")
    assert recovered.is_set() and response.status_code == 200
    frames = [frame for frame in response.text.split("\n\n") if frame]
    states = [frame for frame in frames if "event: state" in frame]
    done_index = next(i for i, frame in enumerate(frames) if "event: done" in frame)
    assert any('"phase": "finalizing"' in frame for frame in states[:-1])
    assert '"phase": "finished"' in states[-1]
    assert done_index > frames.index(states[-1])


def test_scoped_incomplete_finalize_is_visible_and_blocks_reset_and_legacy_control_resume(
        tmp_path, monkeypatch):
    """A durable terminal event is still ``finalizing`` until its scoped projection marker lands.

    The state/list projections must agree. Reset and a legacy ``/control`` resume append fail closed,
    while the stop-aware ``/resume`` driver remains available to finish the same terminal scope. An
    unscoped legacy terminal remains finished for backwards compatibility.
    """
    import looplab.serve.routers.control as control_router

    def seed(name: str, *, scope: str | None):
        rd = tmp_path / name
        rd.mkdir()
        store = EventStore(rd / "events.jsonl")
        store.append("run_started", {
            "run_id": name, "task_id": "t", "goal": "g", "direction": "min"})
        payload = {"reason": "aborted"}
        if scope is not None:
            payload["finalize_scope"] = scope
        store.append("run_finished", payload)
        (rd / "task.snapshot.json").write_text(
            '{"kind":"quadratic","goal":"g","direction":"min"}', encoding="utf-8")
        return rd

    seed("scoped", scope="finish:1")
    seed("legacy", scope=None)
    spawns = []

    def recovery_spawn(args, **kwargs):
        spawns.append((args, kwargs))
        return 9201

    monkeypatch.setattr(control_router, "_spawn_engine", recovery_spawn)
    client = TestClient(make_app(tmp_path))

    scoped_state = client.get("/api/runs/scoped/state").json()["state"]
    legacy_state = client.get("/api/runs/legacy/state").json()["state"]
    listed = {row["run_id"]: row for row in client.get("/api/runs").json()}
    assert scoped_state["finished"] is True
    assert scoped_state["finalization_incomplete"] is True
    assert scoped_state["phase"] == "finalizing"
    assert listed["scoped"]["finalization_incomplete"] is True
    assert listed["scoped"]["phase"] == "finalizing"
    assert legacy_state["finalization_incomplete"] is False
    assert legacy_state["phase"] == listed["legacy"]["phase"] == "finished"

    reset = client.post("/api/runs/scoped/reset")
    legacy_resume = client.post(
        "/api/runs/scoped/control", json={"type": "resume", "data": {}})
    assert reset.status_code == 409 and "projections are incomplete" in reset.json()["detail"]
    assert legacy_resume.status_code == 409
    assert legacy_resume.json()["detail"]["code"] == "finalize_in_progress"
    assert spawns == []

    recovery = client.post("/api/runs/scoped/resume")
    assert recovery.status_code == 200
    assert len(spawns) == 1 and spawns[0][0][0] == "resume"


def test_g1_auth_token_required_on_mutating(tmp_path, monkeypatch):
    """G1 + P1-3 deny-default: with LOOPLAB_UI_TOKEN set, EVERY /api/* request needs the
    X-LoopLab-Token — reads too, not just mutations — except the zero-model /api/health liveness."""
    _build_run(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "s3cret")
    client = TestClient(make_app(tmp_path))
    h = {"X-LoopLab-Token": "s3cret"}
    # P1-3: reads now require the token (deny-default), except zero-model liveness
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/runs", headers=h).status_code == 200
    assert client.get("/api/health").status_code == 200          # sole untokened-OK /api/ route
    # mutating without the token -> 401; with it -> allowed
    assert client.post("/api/runs/demo/control", json={"type": "pause", "data": {}}).status_code == 401
    assert client.post("/api/runs/demo/control", json={"type": "pause", "data": {}},
                       headers=h).status_code == 200


def test_g1_no_token_means_open(tmp_path, monkeypatch):
    """Default (no token) -> the control plane is open, behaviour unchanged."""
    _build_run(tmp_path)
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/control", json={"type": "pause", "data": {}})
    assert r.status_code == 200


def _fake_dist(tmp_path, monkeypatch):
    """A minimal built UI bundle so the index routes are live without a real `npm run build`."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><head></head><body>app</body></html>", encoding="utf-8")
    monkeypatch.setenv("LOOPLAB_UI_DIST", str(dist))
    return dist


def test_g1_owner_token_is_never_injected_into_html(tmp_path, monkeypatch):
    """A review recipient can navigate to `/`, so public HTML must never be an owner-token oracle.
    The operator enters LOOPLAB_UI_TOKEN through the client unlock gate; every document navigation,
    programmatic fetch, frame, and SPA fallback stays tokenless and hardened."""
    _build_run(tmp_path)
    _fake_dist(tmp_path, monkeypatch)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "s3cret")
    client = TestClient(make_app(tmp_path))

    for path, dest in (("/", "document"), ("/", "empty"), ("/", "iframe"),
                       ("/", None), ("/some/spa/route", "empty")):
        headers = {"Sec-Fetch-Dest": dest} if dest else {}
        r = client.get(path, headers=headers)
        assert r.status_code == 200
        assert "ll-token" not in r.text and "s3cret" not in r.text
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert "frame-ancestors 'none'" in (r.headers.get("Content-Security-Policy") or "")
        assert r.headers.get("Cache-Control") == "no-store"


def test_ui_token_injection_payload_is_absent_from_hardened_html(tmp_path, monkeypatch):
    _fake_dist(tmp_path, monkeypatch)
    token = '\"><script>window.pwned=1</script>&'
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", token)

    response = TestClient(make_app(tmp_path)).get(
        "/", headers={"Sec-Fetch-Dest": "document"})
    assert response.status_code == 200
    escaped = "&quot;&gt;&lt;script&gt;window.pwned=1&lt;/script&gt;&amp;"
    assert token not in response.text and escaped not in response.text
    assert "ll-token" not in response.text
    assert response.headers.get("Cache-Control") == "no-store"
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in (
        response.headers.get("Content-Security-Policy") or "")


def test_paid_job_and_report_refresh_responses_are_never_cacheable(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    client = TestClient(make_app(tmp_path))
    owner = {"X-LoopLab-Token": "owner-secret"}

    responses = [
        client.get("/api/jobs/missing"),
        client.get("/api/jobs/missing", headers=owner),
        client.post(
            "/api/runs/missing/report_refresh", headers=owner,
            json={"expected_generation": "a" * 64}),
    ]

    assert [response.status_code for response in responses] == [401, 200, 400]
    for response in responses:
        assert response.headers["cache-control"] == "no-store"
        vary = {item.strip().lower() for item in response.headers["vary"].split(",")}
        assert {"x-looplab-token", "authorization"} <= vary
    assert "idempotency-key" in responses[-1].headers["vary"].lower()


def test_g1_no_token_index_unchanged(tmp_path, monkeypatch):
    """Default local path (no token): the index is served raw — no Sec-Fetch gating, no extra
    security headers — exactly as before."""
    _build_run(tmp_path)
    _fake_dist(tmp_path, monkeypatch)
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path))
    r = client.get("/", headers={"Sec-Fetch-Dest": "empty"})
    assert r.status_code == 200
    assert "ll-token" not in r.text
    assert r.headers.get("X-Frame-Options") is None     # local path untouched


def test_g1_shared_hub_warns(tmp_path, monkeypatch, caplog):
    """On a shared JupyterHub origin we warn that the token is per-deployment (not per-user). No
    warning off-hub.

    The third case MOVED with the behaviour it describes (`serve/owner_token.py`): on-hub with no
    token, the control plane is no longer unauthenticated — it fails closed by minting a credential,
    and the log has to say which one and where. The word "unauthenticated" now belongs to the
    explicit `LOOPLAB_UI_ANONYMOUS` opt-out, which is the only way that state is still reachable, so
    it is asserted there rather than deleted. The request-level halves of both — 401 without the
    token, 200 with it, 200 when opted out — are driven in `tests/test_owner_token.py`."""
    import logging
    _build_run(tmp_path)

    # off-hub (default) -> no shared-origin warning
    monkeypatch.delenv("JUPYTERHUB_SERVICE_PREFIX", raising=False)
    monkeypatch.delenv("JUPYTERHUB_API_TOKEN", raising=False)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "s3cret")
    with caplog.at_level(logging.WARNING, logger="looplab.server"):
        make_app(tmp_path)
    assert "shared" not in " ".join(caplog.messages).lower()

    # on-hub WITH a token -> "per-deployment, not per-user"
    caplog.clear()
    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/user/alice/")
    with caplog.at_level(logging.WARNING, logger="looplab.server"):
        make_app(tmp_path)
    msg = " ".join(caplog.messages).lower()
    assert "shared jupyterhub origin" in msg and "per-deployment" in msg

    # on-hub WITHOUT a token -> fail closed, and name the credential it just minted
    caplog.clear()
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    with caplog.at_level(logging.WARNING, logger="looplab.server"):
        make_app(tmp_path)
    assert "fails closed" in caplog.text.lower()
    from looplab.serve.owner_token import owner_token_path, read_owner_token_file
    assert read_owner_token_file() and str(owner_token_path()) in caplog.text

    # on-hub, token unset AND the opt-out explicitly turned on -> the old open plane, said plainly
    caplog.clear()
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.setenv("LOOPLAB_UI_ANONYMOUS", "1")
    with caplog.at_level(logging.WARNING, logger="looplab.server"):
        make_app(tmp_path)
    assert "unauthenticated" in " ".join(caplog.messages).lower()


def test_supertask_endpoints_round_trip(tmp_path):
    """Create a super-task, assign the run to it (so the run summary carries supertask_id), reassign
    to none, then delete — the whole HTTP flow the start-menu filter/assign UI drives."""
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))

    assert client.get("/api/supertasks").json() == {"supertasks": [], "assignments": {}}
    st = client.post("/api/supertasks", json={"name": "nomad2018"}).json()
    assert st["id"].startswith("st_") and st["name"] == "nomad2018"

    # Run-organization writes are fenced to the run generation the Runs list showed, so a stale tab
    # cannot re-file a run that was reset out from under it.
    generation = {x["run_id"]: x for x in client.get("/api/runs").json()}["demo"]["generation"]
    r = client.post("/api/runs/demo/supertask", json={
        "supertask_id": st["id"], "expected_generation": generation,
        "expected_supertask_id": None})            # CAS on the CURRENT value too, null included
    assert r.status_code == 200, r.text
    summary = {x["run_id"]: x for x in client.get("/api/runs").json()}
    assert summary["demo"]["supertask_id"] == st["id"]          # surfaced in the run summary
    assert len(summary["demo"]["generation"]) == 64

    client.patch(f"/api/supertasks/{st['id']}", json={"name": "MLE-bench"})
    assert client.get("/api/supertasks").json()["supertasks"][0]["name"] == "MLE-bench"

    # assigning an unknown super-task -> 400; assigning a real run to an unknown run -> 404
    assert client.post("/api/runs/demo/supertask", json={
        "supertask_id": "st_x", "expected_generation": generation,
        "expected_supertask_id": st["id"]}).status_code == 400
    assert client.post("/api/runs/ghost/supertask", json={
        "supertask_id": st["id"], "expected_generation": generation,
        "expected_supertask_id": None}).status_code == 404
    # …and a stale generation is refused even when the super-task itself is valid.
    assert client.post("/api/runs/demo/supertask", json={
        "supertask_id": st["id"], "expected_generation": "0" * 64,
        "expected_supertask_id": st["id"]}).status_code == 409

    client.post("/api/runs/demo/supertask", json={          # clear
        "supertask_id": None, "expected_generation": generation,
        "expected_supertask_id": st["id"]})
    assert {x["run_id"]: x for x in client.get("/api/runs").json()}["demo"]["supertask_id"] is None

    client.delete(f"/api/supertasks/{st['id']}")
    assert client.get("/api/supertasks").json()["supertasks"] == []


def test_org_mutations_reject_non_object_and_malformed_json(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    project = client.post("/api/projects", json={"name": "p"}).json()
    supertask = client.post("/api/supertasks", json={"name": "s"}).json()
    routes = [
        ("post", "/api/projects"),
        ("patch", f"/api/projects/{project['id']}"),
        ("post", "/api/runs/demo/project"),
        ("post", "/api/supertasks"),
        ("patch", f"/api/supertasks/{supertask['id']}"),
        ("post", "/api/runs/demo/supertask"),
        ("patch", "/api/runs/demo"),
    ]
    for method, route in routes:
        assert client.request(method, route, json=[]).status_code == 400
        assert client.request(
            method, route, content="{", headers={"Content-Type": "application/json"},
        ).status_code == 400


def test_browser_replay_requires_and_validates_generation(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    headers = {"Origin": "http://testserver"}

    missing = client.post("/api/runs/demo/reset", headers=headers)
    assert missing.status_code == 428
    malformed = client.post(
        "/api/runs/demo/reset", headers=headers, json={"expected_generation": "ABC"})
    assert malformed.status_code == 400
    stale = client.post(
        "/api/runs/demo/reset", headers=headers,
        json={"expected_generation": "0" * 64})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "run_generation_changed"


def test_chat_log_persist_and_restore(tmp_path):
    """The human↔boss transcript is saved WITH the run (chat.jsonl sidecar) so it survives a Dock
    remount/reload: append turns, then GET them back verbatim in order."""
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))

    assert client.get("/api/runs/demo/chat-log").json() == []     # empty before any chat
    turns = [
        {"role": "user", "content": "try a higher degree", "ts": 1.0, "seq": 1e15},
        {"role": "assistant", "content": "**sure** — degree 3 next", "ts": 1.1, "seq": 1e15 + 1},
        {"role": "action", "action": {"type": "pause", "label": "Pause the run"},
         "status": "done", "ts": 1.2, "seq": 1e15 + 2},
    ]
    for t in turns:
        assert client.post("/api/runs/demo/chat-log", json=t).json()["ok"] is True

    got = client.get("/api/runs/demo/chat-log").json()
    assert [m["role"] for m in got] == ["user", "assistant", "action"]
    assert got[0]["content"] == "try a higher degree"
    assert got[2]["action"]["type"] == "pause" and got[2]["status"] == "done"
    # a fresh app (simulating a reload / new server) reads the same persisted transcript
    assert TestClient(make_app(tmp_path)).get("/api/runs/demo/chat-log").json() == got

    # guards: a non-object turn is rejected; an unknown run is 404
    assert client.post("/api/runs/demo/chat-log", json=["nope"]).status_code == 400
    assert client.get("/api/runs/ghost/chat-log").status_code == 404


def test_chat_log_append_is_size_bounded(tmp_path, monkeypatch):
    # Regression: chat.jsonl had no size cap, so unbounded turn appends could exhaust disk and slow
    # every GET (which re-reads the whole file). An over-cap append is refused with 413; the refused
    # turn is not written, and a normal append under the (generous) default cap still works.
    import looplab.serve.routers.boss as boss_router
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    assert client.post("/api/runs/demo/chat-log",
                       json={"role": "user", "content": "hi", "ts": 1.0, "seq": 1}).json()["ok"] is True
    # shrink the cap so the (now non-empty) file is already over it: the next append is refused
    monkeypatch.setattr(boss_router, "_CHAT_LOG_MAX_BYTES", 5)
    assert client.post("/api/runs/demo/chat-log",
                       json={"role": "user", "content": "more", "ts": 2.0, "seq": 2}).status_code == 413
    assert [m["content"] for m in client.get("/api/runs/demo/chat-log").json()] == ["hi"]  # refused turn not written


def test_start_seeds_genesis_chat(tmp_path, monkeypatch):
    """The chat-first creation flow carries its planning conversation into the new run: /api/start with
    a `chat` array writes those turns to the run's chat.jsonl, so the run opens with its creation story
    (and only user/assistant turns, in order, flagged as genesis)."""
    import looplab.serve.server as server
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: type("P", (), {})())
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/start", json={
        "task_file": str(TASK), "run_id": "born",
        "chat": [
            {"role": "user", "content": "run titanic on deepseek, 30 nodes"},
            {"role": "assistant", "content": "naming it titanic-deepseek-30 ✓"},
            {"role": "system", "content": "not a chat turn -> skipped"},
        ]})
    assert r.status_code == 200
    # read chat.jsonl off disk (the engine is mocked, so there's no events.jsonl for the GET guard yet)
    turns = list(iter_jsonl(tmp_path / "born" / "chat.jsonl"))
    assert [m["role"] for m in turns] == ["user", "assistant"]           # the system turn is dropped
    assert turns[0]["content"].startswith("run titanic") and turns[0]["genesis"] is True
    assert turns[0]["ts"] < turns[1]["ts"] and turns[1]["seq"] > turns[0]["seq"]  # stable feed order
    # a run started WITHOUT a chat seeds nothing (no stray chat.jsonl)
    client.post("/api/start", json={"task_file": str(TASK), "run_id": "plain"})
    assert not (tmp_path / "plain" / "chat.jsonl").exists()


def test_reset_archives_chat_log(tmp_path, monkeypatch):
    """Replay (reset) starts a clean conversation: the prior chat.jsonl is archived (renamed), not
    carried into the fresh run. Its trace-append receipts follow the trace source lifecycle too."""
    from looplab.core.trace_append import SPAN_APPEND_JOURNAL_NAME
    from looplab.serve.routers import control as control_router
    _build_run(tmp_path)                                          # a finished run (reset only runs on those)
    rd = tmp_path / "demo"
    # Stub the SPAWN, not `Popen`: Replay withholds its 200 until a replacement generation is
    # durably visible, and a Popen stub that starts nothing leaves the transaction at 425 forever —
    # the archive assertions below would then never run against a completed Replay.
    monkeypatch.setattr(control_router, "_spawn_engine", _replacement_spawn(rd))
    (rd / "ui_meta.json").write_text('{"task_file": "%s"}' % str(TASK).replace("\\", "/"), encoding="utf-8")

    client = TestClient(make_app(tmp_path))
    client.post("/api/runs/demo/chat-log", json={"role": "user", "content": "hello", "ts": 1.0, "seq": 1})
    assert (rd / "chat.jsonl").exists()
    journal = rd / SPAN_APPEND_JOURNAL_NAME
    journal.write_bytes(b'{"derived":"old-generation"}\n')

    assert client.post("/api/runs/demo/reset").status_code == 200
    assert not (rd / "chat.jsonl").exists()                       # archived out of the way
    assert any(p.name.startswith("chat.jsonl.reset-") for p in rd.iterdir())  # ...and recoverable
    assert not journal.exists()
    assert not list(rd.glob(f"{SPAN_APPEND_JOURNAL_NAME}.reset-*"))  # derived, not v1 receipt state


def test_reset_retires_trace_receipts_only_after_the_old_source_is_archived(tmp_path):
    """Never delete receipts that may already belong to a current/replacement trace source."""
    from looplab.core.trace_append import SPAN_APPEND_JOURNAL_NAME
    from looplab.serve.reset_route import _retire_archived_trace_append_journal

    rd = tmp_path / "demo"
    rd.mkdir()
    source = rd / "spans.jsonl"
    journal = rd / SPAN_APPEND_JOURNAL_NAME
    source.write_bytes(b'{}\n')
    journal.write_bytes(b'{"receipt":"current"}\n')

    _retire_archived_trace_append_journal(rd)
    assert journal.read_bytes() == b'{"receipt":"current"}\n'

    source.unlink()
    _retire_archived_trace_append_journal(rd)
    assert not journal.exists()


def test_action_router_maps_plan_to_multiple_controls():
    """The agentic boss emits a _Plan (reply + ordered actions); _plan_to_actions maps each step to a
    control, drops pure-advice steps, and carries per-step rationale. Covers the new note + budget verbs
    and the multi-action shape that makes 'you have 10 more nodes, try some nets' a real batch."""
    from looplab.serve.server import _Action, _Plan, _action_to_control, _plan_to_actions

    class _St:
        best_node_id = 7

    st = _St()
    assert _action_to_control(_Action(action="budget", nodes=10), st)["type"] == "budget_extend"
    assert _action_to_control(_Action(action="budget", nodes=10), st)["data"]["add_nodes"] == 10
    assert _action_to_control(_Action(action="note", node_id=3, text="nice"), st)["type"] == "comment_created"
    assert _action_to_control(_Action(action="budget", nodes=0), st) is None      # no-op budget -> dropped
    assert _action_to_control(_Action(action="advise"), st) is None               # pure advice -> dropped

    plan = _Plan(reply="on it", actions=[
        _Action(action="budget", nodes=10, rationale="more room"),
        _Action(action="hint", text="try a small MLP and a 1-D CNN", rationale="neural nets"),
        _Action(action="inject", operator="draft", params={"hidden": 32}, rationale="MLP baseline"),
        _Action(action="advise", text="just chatting"),                            # dropped
    ])
    acts = _plan_to_actions(plan, st)
    assert [a["type"] for a in acts] == ["budget_extend", "hint", "inject_node"]
    assert acts[0]["rationale"] == "more room"
    assert acts[2]["data"]["idea"]["operator"] == "draft"

    class _N:
        def __init__(self, attempt):
            self.attempt = attempt
    class _BoundSt:
        best_node_id = 5
        awaiting_approval = True
        approval_subject = 7
        approval_generation = 4
        aborted_nodes = []
        nodes = {5: _N(2), 7: _N(4)}
    bound = _action_to_control(_Action(action="approve"), _BoundSt())
    assert bound["data"] == {"node_id": 7, "generation": 4}
    explicit = _action_to_control(_Action(action="approve", node_id=5), _BoundSt())
    assert explicit["data"] == {"node_id": 5, "generation": 2}


def test_boss_hint_replaces_standing_directive():
    """The boss authors the complete current directive each turn, so its hint REPLACES the standing
    one (data.replace=True) rather than stacking contradictory directives."""
    from looplab.serve.server import _Action, _action_to_control

    class _St:
        best_node_id = 1
    ctrl = _action_to_control(_Action(action="hint", text="try several neural nets"), _St())
    assert ctrl["type"] == "hint"
    assert ctrl["data"]["replace"] is True
    assert ctrl["data"]["text"] == "try several neural nets"


def test_budget_action_clamps_nodes():
    """A budget verb only ADDS room and is bounded: non-positive is a no-op, and a hallucinated huge
    delta is capped so the boss LLM can't push max_nodes to a runaway value."""
    from looplab.serve.server import _Action, _action_to_control

    class _St:
        best_node_id = 1
    s = _St()
    assert _action_to_control(_Action(action="budget", nodes=0), s) is None       # zero -> no-op
    assert _action_to_control(_Action(action="budget", nodes=-5), s) is None       # negative -> no-op
    assert _action_to_control(_Action(action="budget", nodes=12), s)["data"]["add_nodes"] == 12
    assert _action_to_control(_Action(action="budget", nodes=10 ** 9), s)["data"]["add_nodes"] == 1000  # capped


def test_budget_extension_survives_policy_swap(tmp_path):
    """Regression (review-found HIGH): a live add_nodes extension must NOT be dropped when a strategy
    swap rebuilds the policy in the SAME reopened loop iteration. The override is applied AFTER the swap
    (just before action selection), so the run runs the granted nodes instead of immediately
    re-finishing. Engine & policy budgets MATCH here so the bug (re-finish at base) would be exposed."""
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    rd = tmp_path / "swap"
    eng0 = Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=4), max_nodes=4)
    st0 = anyio.run(eng0.run)
    n0 = len(st0.nodes)
    assert st0.finished and n0 == 4
    # grant +3 nodes AND swap the policy, both folded into the same reopened iteration
    store = EventStore(rd / "events.jsonl")
    store.append("budget_extend", {"add_nodes": 3})
    store.append("set_strategy", {"strategy": {"policy": "evolutionary"}})
    store.append("run_reopened", {})
    eng1 = Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=4), max_nodes=4)
    st1 = anyio.run(eng1.run)
    assert st1.finished and len(st1.nodes) > n0   # the +3 survived the swap and actually ran


def test_budget_extend_add_nodes_accumulates(tmp_path):
    """budget_extend(add_nodes) is ADDITIVE (several extensions sum) while time ceilings stay absolute —
    so two '+N nodes' from the boss give the run N+M more room."""
    rd = tmp_path / "acc"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("budget_extend", {"add_nodes": 4})
    store.append("budget_extend", {"add_nodes": 6, "max_eval_seconds": 300})
    st = fold(store.read_all())
    assert st.budget_overrides["add_nodes"] == 10           # summed
    assert st.budget_overrides["max_eval_seconds"] == 300   # absolute (last write)


def test_budget_extend_nodes_resumes_and_grows(tmp_path):
    """End-to-end of the agentic 'you have N more nodes' path: a finished run, given add_nodes via
    budget_extend + run_reopened, resumes and actually runs MORE experiments — capped at the extended
    budget (the policy's live effective max_nodes = base + add_nodes)."""
    st0 = _build_run(tmp_path, name="grow")                 # GreedyTree finishes at max_nodes=4
    rd = tmp_path / "grow"
    n0 = len(st0.nodes)
    assert st0.finished and n0 >= 1
    # the boss plan's effect on disk: extend the node budget, then reopen the finished run
    store = EventStore(rd / "events.jsonl")
    store.append("budget_extend", {"add_nodes": 3})
    store.append("run_reopened", {})
    # resume: a fresh Engine on the same dir re-enters the loop with the extended budget
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(rd, task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4))
    st1 = anyio.run(eng.run)
    assert st1.finished
    assert len(st1.nodes) > n0           # it genuinely proposed + ran more experiments
    assert len(st1.nodes) <= n0 + 3      # but never beyond the extended budget


def test_node_logs_surfaces_declared_stage_logs_only(tmp_path):
    # A MULTI-STAGE eval tees to per-stage logs (train.log / score.log), never eval.log — surface each
    # under `stages`. The set is bounded to the node's DECLARED stages (looplab_stages.json) + the
    # reserved `score` stage, so a stray log the training code writes (debug.log) is NOT a phantom stage.
    rd = tmp_path / "demo"
    rd.mkdir()
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("node_building", {"node_id": 0, "operator": "draft", "parent_ids": []})
    nd = rd / "nodes" / "node_0"
    nd.mkdir(parents=True)
    (nd / "looplab_stages.json").write_text(
        '{"stages": [{"name": "train", "command": ["python", "train.py"]}]}')
    (nd / "train.log").write_text("Epoch 0 loss=1.0\nEpoch 1 loss=0.5\n")
    (nd / "score.log").write_text("recall@100: 0.8\n")     # score is the reserved operator stage
    (nd / "debug.log").write_text("noise the training code wrote to its cwd\n")
    client = TestClient(make_app(tmp_path))
    body = client.get("/api/runs/demo/nodes/0/logs").json()
    assert list(body["stages"]) == ["train", "score"]         # declared order (manifest, then score)
    assert "debug" not in body["stages"]                      # stray log is NOT a phantom stage
    assert "Epoch 1 loss=0.5" in body["stages"]["train"]
    assert body["eval"] == ""                                 # no eval.log → empty (no fallback dup)


def test_node_logs_single_command_uses_eval_log(tmp_path):
    # The single-command path still writes eval.log and must win over the (empty) stage set.
    rd = tmp_path / "demo"
    rd.mkdir()
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    nd = rd / "nodes" / "node_0"
    nd.mkdir(parents=True)
    (nd / "eval.log").write_text("metric: 0.42\n")
    client = TestClient(make_app(tmp_path))
    body = client.get("/api/runs/demo/nodes/0/logs").json()
    assert body["eval"].strip() == "metric: 0.42" and body["stages"] == {}
    assert body["node_id"] == 0 and body["attempt"] == 0 and body["run_generation"]


def test_node_logs_legacy_attempt_rejects_cross_node_alias_and_replacement(
        tmp_path, monkeypatch):
    """Legacy attempt-zero compatibility must not make a sibling node directory readable."""
    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    nodes = rd / "nodes"
    sibling = nodes / "node_1"
    sibling.mkdir(parents=True)
    (sibling / "eval.log").write_text("PRIVATE-SIBLING-LOG", encoding="utf-8")
    alias = nodes / "node_0"
    alias.symlink_to(sibling, target_is_directory=True)
    client = TestClient(make_app(tmp_path))

    refused = client.get("/api/runs/demo/nodes/0/logs")
    assert refused.status_code == 404
    assert "PRIVATE-SIBLING-LOG" not in refused.text

    # A direct child is initially a valid legacy node, but replacing it with the sibling while the
    # response is assembled changes the captured inode and must fail the after-read lifecycle CAS.
    alias.unlink()
    alias.mkdir()
    (alias / "eval.log").write_text("OWN-LOG", encoding="utf-8")
    import looplab.serve.routers.runs as runs_router

    def replace_node_dir(_rd):
        alias.rename(nodes / "node_0.old")
        sibling.rename(alias)
        return ()

    monkeypatch.setattr(runs_router, "_operator_stage_names", replace_node_dir)
    raced = client.get("/api/runs/demo/nodes/0/logs")
    assert raced.status_code == 409
    assert raced.json()["detail"]["code"] == "node_attempt_changed"
    assert "PRIVATE-SIBLING-LOG" not in raced.text


def test_node_logs_rejects_manifest_traversal_and_bounds_aggregate_tail(tmp_path, monkeypatch):
    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    nd = rd / "nodes" / "node_0"
    nd.mkdir(parents=True)
    # Previously this unvalidated name read rd/outside.log through node_0/../../outside.log.
    (rd / "outside.log").write_text("PRIVATE-SIBLING-LOG", encoding="utf-8")
    (nd / "looplab_stages.json").write_text(json.dumps({"stages": [
        {"name": "../../outside", "command": ["python", "x.py"]},
    ]}), encoding="utf-8")
    client = TestClient(make_app(tmp_path))
    body = client.get("/api/runs/demo/nodes/0/logs?tail=100").json()
    assert "PRIVATE-SIBLING-LOG" not in json.dumps(body) and body["stages"] == {}

    # Valid bands remain visible, but all returned log tails share one response-size envelope.
    import looplab.serve.routers.runs as runs_router
    monkeypatch.setattr(runs_router, "_LOG_TAIL_MAX", 40)
    (nd / "looplab_stages.json").write_text(json.dumps({"stages": [
        {"name": "prep", "command": ["python", "prep.py"]},
        {"name": "train", "command": ["python", "train.py"]},
    ]}), encoding="utf-8")
    for name in ("prep.log", "train.log", "score.log", "eval.log", "setup.log"):
        (nd / name).write_text("x" * 100, encoding="utf-8")
    (rd / "run_setup.log").write_text("x" * 100, encoding="utf-8")
    bounded = client.get("/api/runs/demo/nodes/0/logs?tail=100").json()
    total = sum(len(value.encode()) for value in (
        bounded["eval"], bounded["setup"], bounded["run_setup"], *bounded["stages"].values()))
    assert total <= 40


def test_agents_md_refuses_a_symlink_and_bounds_the_body(tmp_path):
    # `run_dir` confines the DIRECTORY, but `exists()`/`read_text()` FOLLOW symlinks — so an AGENTS.md
    # symlink (an imported run bundle, or the sandbox writing into the run dir) turned this route into
    # an arbitrary host-file read. The sibling /log and /log-page routes already refuse a symlinked
    # events.jsonl for exactly this reason; agents_md must match. The body is bounded too: an
    # attacker-sized file would otherwise be read whole into the response.
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("TOP-SECRET-HOST-FILE", encoding="utf-8")
    if (rd / "AGENTS.md").exists():
        (rd / "AGENTS.md").unlink()
    (rd / "AGENTS.md").symlink_to(secret)
    client = TestClient(make_app(tmp_path))
    body = client.get("/api/runs/demo/agents_md").text
    assert "TOP-SECRET-HOST-FILE" not in body
    assert body == ""

    # a real (non-symlink) AGENTS.md still serves normally
    (rd / "AGENTS.md").unlink()
    (rd / "AGENTS.md").write_text("# real agents md", encoding="utf-8")
    assert "# real agents md" in client.get("/api/runs/demo/agents_md").text


def test_author_routes_are_bounded_and_name_restricted(tmp_path, monkeypatch):
    # `knowledge_dir` is AGENT-writable (the engine's own `remember` tool), so listing must not read an
    # unbounded number of unbounded files into one response, and a PUT must not be able to drop a
    # non-markdown file (`.env`, `x.py`) into a directory that is hot-reloaded into agent context —
    # `list_author` globs only `*.md`, so such a file would be write-only-invisible.
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(kdir))
    client = TestClient(make_app(tmp_path))

    # name allow-list (asserted first so it stands on its own behaviour, not on a constant import)
    assert client.put("/api/knowledge/.env", content=b"SECRET=1").status_code == 400
    assert client.put("/api/knowledge/evil.py", content=b"import os").status_code == 400
    assert not (kdir / ".env").exists() and not (kdir / "evil.py").exists()
    assert client.put("/api/knowledge/ok.md", content=b"# fine").status_code == 200

    # per-file byte bound, with an explicit truncation receipt rather than a silent whole-file read
    from looplab.serve.routers.misc import _AUTHOR_MAX_BYTES
    (kdir / "big.md").write_text("x" * (_AUTHOR_MAX_BYTES + 5000), encoding="utf-8")
    listed = client.get("/api/knowledge").json()
    entry = next(f for f in listed["files"] if f["name"] == "big.md")
    assert len(entry["text"].encode()) <= _AUTHOR_MAX_BYTES
    assert entry["truncated"] is True


def test_skills_authoring_lists_nested_packages_read_only_and_rejects_nested_writes(
        tmp_path, monkeypatch):
    """Authoring reviews runtime-visible packages without turning relative ids into write paths."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "root.md").write_text("# writable root\n", encoding="utf-8")
    package = skills / "package"
    package.mkdir()
    nested = package / "SKILL.md"
    nested.write_text("# packaged\n", encoding="utf-8")
    (package / "notes.md").write_text("not a package entry", encoding="utf-8")
    deep = skills / "deep" / "child"
    deep.mkdir(parents=True)
    (deep / "SKILL.md").write_text("# deep package\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("HOST SECRET", encoding="utf-8")
    (skills / "escape").symlink_to(outside, target_is_directory=True)
    linked = skills / "linked"
    linked.mkdir()
    (linked / "SKILL.md").symlink_to(outside / "SKILL.md")
    (skills / "linked-root.md").symlink_to(outside / "SKILL.md")

    monkeypatch.setenv("LOOPLAB_SKILLS_DIR", str(skills))
    client = TestClient(make_app(tmp_path))
    response = client.get("/api/skills")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["inventory_incomplete"] is False
    assert payload["truncated_files"] == 0
    by_name = {row["name"]: row for row in payload["files"]}
    assert set(by_name) == {"root.md", "package/SKILL.md", "deep/child/SKILL.md"}
    assert by_name["root.md"]["read_only"] is False
    assert by_name["package/SKILL.md"]["read_only"] is True
    assert by_name["deep/child/SKILL.md"]["read_only"] is True
    assert all(row["revision"].startswith("sha256:") for row in by_name.values())
    assert "HOST SECRET" not in response.text
    assert "notes.md" not in by_name

    # Both compatibility and receipt-backed writes remain basename-only. Percent-encoding cannot
    # smuggle the display id through route decoding; regardless of whether the router answers 400
    # or 404, no nested file is touched and no recovery identity is created for it.
    before = nested.read_bytes()
    nested_url = "/api/skills/package%2FSKILL.md"
    assert client.put(nested_url, content=b"overwrite").status_code in (400, 404, 405)
    operation_id = "12345678-1234-4234-9234-123456789abc"
    operation = client.put(
        f"{nested_url}/operations/{operation_id}",
        json={
            "text": "overwrite",
            "expected_revision": by_name["package/SKILL.md"]["revision"],
            "expected_target_root_id": payload["target_root_id"],
        },
    )
    assert operation.status_code in (400, 404, 405)
    assert nested.read_bytes() == before
    assert client.put("/api/skills/root.md", content=b"# changed root\n").status_code == 200

    # The recursive inventory is bounded independently of content reads. Known overflow is a lower
    # bound; a separate incompleteness flag covers the case where a scan/depth cap makes the unknown
    # suffix uncountable. Either signal prevents the UI treating absence as a proven deletion.
    import looplab.serve.routers.misc as misc
    original_max_files = misc._AUTHOR_MAX_FILES
    monkeypatch.setattr(misc, "_AUTHOR_MAX_FILES", 2)
    bounded = client.get("/api/skills").json()
    assert len(bounded["files"]) == 2
    assert bounded["truncated_files"] >= 1
    assert bounded["inventory_incomplete"] is True
    assert bounded["files"][0]["name"] == "root.md"  # flat writable inventory keeps priority

    monkeypatch.setattr(misc, "_AUTHOR_MAX_FILES", original_max_files)
    monkeypatch.setattr(misc, "_AUTHOR_SKILL_MAX_DEPTH", 0)
    depth_capped = client.get("/api/skills").json()
    assert [row["name"] for row in depth_capped["files"]] == ["root.md"]
    assert depth_capped["truncated_files"] == 0
    assert depth_capped["inventory_incomplete"] is True


def test_cross_run_import_origin_names_the_source_attempt(tmp_path):
    """A cross-run import receipt must name the source ATTEMPT, not just `(run_id, node_id)`.

    A node id SURVIVES `node_reset`, so `(run_id, node_id)` alone stops identifying the bytes that
    were actually imported the moment the source node is re-run: the stored receipt — and the UI link
    built from it — then point at a different experiment than the snapshot came from, silently
    misattributing where a result originated. `source_attempt` is additive, so receipts written before
    this simply lack the key and fold exactly as they always did.
    """
    _build_run(tmp_path, "source")
    _build_run(tmp_path, "destination")
    client = TestClient(make_app(tmp_path))
    response = client.post("/api/runs/destination/control", json={
        "type": "inject_node",
        "data": {"source_run": "source", "source_node": 0},
    })
    assert response.status_code == 200, response.text

    injected = next(event for event in EventStore(tmp_path / "destination" / "events.jsonl")
                    .read_all() if event.type == "inject_node")
    origin = injected.data["origin"]
    assert origin["run_id"] == "source" and origin["node_id"] == 0
    assert "source_attempt" in origin, (
        "the import receipt does not name the source attempt, so a later reset of that node "
        "silently repoints it at different bytes")
    src_state = fold(EventStore(tmp_path / "source" / "events.jsonl").read_all())
    assert origin["source_attempt"] == src_state.nodes[0].attempt


def test_put_run_config_honors_the_run_generation_fence(tmp_path):
    """`expected_revision` fences the snapshot BYTES; only `expected_generation` fences the RUN.

    `config.snapshot.json` is deliberately absent from reset's archive list, so it survives a reset
    byte-identical — which means a revision observed against generation A STILL matches after the run
    is reset into generation B, letting a delayed PUT silently rewrite the replacement run's settings.
    The generation fence is checked inside the config lock, since a reset can land between the request
    arriving and the write. It is now REQUIRED on both body variants — while it was optional, a
    caller that simply never sent it kept the exact hole this test describes.
    """
    _build_run(tmp_path)
    _write_snapshot(tmp_path / "demo", timeout=30.0)
    client = TestClient(make_app(tmp_path))
    meta = client.get("/api/runs/demo/config").json()["_looplab_config_meta"]
    generation = client.get("/api/runs/demo/state").json()["generation"]

    ok = _run_config_put(client, "demo", {
        "timeout": 51.0, "expected_revision": meta["config_revision"],
        "expected_generation": generation})
    assert ok.status_code == 200 and ok.json()["config"]["timeout"] == 51.0

    stale = _run_config_put(client, "demo", {
        "timeout": 99.0,
        "expected_revision": ok.json()["config"]["_looplab_config_meta"]["config_revision"],
        "expected_generation": "0" * 64})
    assert stale.status_code == 409, (
        "a PUT naming a different run generation rewrote this run's settings")
    assert stale.json()["detail"]["code"] == "run_generation_changed"
    assert client.get("/api/runs/demo/config").json()["timeout"] == 51.0


def test_boss_command_retry_with_the_same_key_does_not_pay_twice(tmp_path, monkeypatch):
    """A lost POST response is the normal case: the UI retries. Without a request identity the retry
    starts the whole paid tool/model route again and bills for it — one click, two charges."""
    sr = tmp_path / "z"
    sr.mkdir()
    (sr / "events.jsonl").write_text(
        '{"seq":0,"type":"run_started","data":{"run_id":"z","task_id":"t","goal":"g","direction":"max"}}\n',
        encoding="utf-8")
    calls = []
    started = threading.Event()
    release = threading.Event()

    class _Slow:
        model = "m"
        def chat(self, messages, tools=None, tool_choice=None):
            calls.append(1)
            started.set()
            release.wait(10)                      # still in flight when the retry arrives
            return {"tool_calls": [{"id": "e", "function": {
                "name": "emit", "arguments": {"reply": "ok", "actions": []}}}]}

    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s, **_kw: _Slow())
    client = TestClient(make_app(tmp_path))
    head = {"Idempotency-Key": "one-click"}
    out: dict = {}
    first = threading.Thread(
        target=lambda: out.update(first=client.post("/api/runs/z/command",
                                                    json={"instruction": "go"}, headers=head).json()))
    first.start()
    assert started.wait(10)
    retry = client.post("/api/runs/z/command", json={"instruction": "go"}, headers=head).json()
    release.set()
    first.join(15)
    assert len(calls) == 1                        # the retry rejoined; it did not start a 2nd route
    assert retry.get("job_id") == out["first"].get("job_id") or retry.get("ok") is True


def test_boss_command_without_a_key_keeps_the_historical_behaviour(tmp_path, monkeypatch):
    """The key is opt-in: a client that sends none must behave exactly as before (two independent
    calls), so requiring it breaks nobody."""
    sr = tmp_path / "z"
    sr.mkdir()
    (sr / "events.jsonl").write_text(
        '{"seq":0,"type":"run_started","data":{"run_id":"z","task_id":"t","goal":"g","direction":"max"}}\n',
        encoding="utf-8")
    calls = []

    class _C:
        model = "m"
        def chat(self, messages, tools=None, tool_choice=None):
            calls.append(1)
            return {"tool_calls": [{"id": "e", "function": {
                "name": "emit", "arguments": {"reply": "ok", "actions": []}}}]}

    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s, **_kw: _C())
    client = TestClient(make_app(tmp_path))
    assert client.post("/api/runs/z/command", json={"instruction": "go"}).json()["ok"] is True
    assert client.post("/api/runs/z/command", json={"instruction": "go"}).json()["ok"] is True
    assert len(calls) == 2


def test_author_name_allowlist_rejects_a_trailing_newline(tmp_path):
    """A trailing newline must not pass the authored-markdown allow-list.

    The original bug: `$` also matches immediately BEFORE a trailing newline, so `x.md\\n` passed.
    The write then landed a file that `list_author`'s `*.md` glob can never match again — the
    write-only-invisible outcome the allow-list exists to prevent.
    """
    # The allow-list is now the `_valid_author_name` predicate rather than the `_AUTHOR_NAME_RE`
    # pattern this test was written against: it was deliberately widened to accept spaces and
    # Unicode basenames. Follow it, because what must not drift is the REJECTION set below — the
    # regex was only ever the mechanism. Importing the retired constant made this test an
    # ImportError, which retires the guard instead of checking it.
    from looplab.serve.routers.misc import _valid_author_name

    assert _valid_author_name("note.md")
    assert _valid_author_name("a_b-c.1.md")
    assert _valid_author_name("a note.md")                 # widened: spaces are allowed now
    assert not _valid_author_name("note.md\n"), (
        "a trailing newline still passes the authored-markdown allow-list")
    assert not _valid_author_name("note.txt")
    assert not _valid_author_name("../escape.md")
    assert not _valid_author_name("sub/note.md")           # separators stay rejected
    assert not _valid_author_name(".env")


def test_runs_list_started_date_is_the_runs_start_not_its_last_append(tmp_path):
    """`created` feeds the RunList's "started <date>" tooltip, so it must be the run's START.

    It was `events.jsonl`'s `st_ctime`, which on POSIX is the inode-CHANGE time — every append
    advances it, so "started" silently tracked "updated" and a week-old run looked like it began
    minutes ago. The FIRST event's `ts` is the wall clock the run began at (`setup_started` when the
    task has a setup phase, else `run_started`)."""
    _build_run(tmp_path)
    log = tmp_path / "demo" / "events.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["type"] in ("setup_started", "run_started")   # whichever the engine wrote first
    started = 1_600_000_000.0            # a start long before this test ran, so the two can't tie
    first["ts"] = started
    lines[0] = json.dumps(first)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    client = TestClient(make_app(tmp_path))
    row = next(r for r in client.get("/api/runs").json() if r["run_id"] == "demo")
    assert row["created"] == started                          # ...from run_started.ts
    assert row["created"] < log.stat().st_ctime - 86_400      # ...NOT from the inode-change stat
    assert row["mtime"] == pytest.approx(log.stat().st_mtime)  # "updated" still tracks the file


def test_clear_trace_refuses_a_run_with_an_unserved_resume(tmp_path):
    """A destructive whole-file rewrite must fence the ENGINE THAT IS ABOUT TO START, not just a
    running one.

    `clear_node_trace` held only the command sequencer, but `reconcile_pending_resume` — fired from
    the runs-list poll and the startup timers — spawns engines under the LIFECYCLE lock, which the
    sequencer does not exclude. On a dead-engine run carrying an unserved `resume_requested`, that
    reconciler could Popen a fresh engine between the liveness probe and `write_jsonl_atomic`, and
    the new engine would append spans into a file being replaced under it. `reset_run` already
    refuses on exactly this signal."""
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    spans = [{"span_id": "a", "kind": "operation", "name": "n", "attributes": {"node_id": 0}}]
    (rd / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    before = (rd / "spans.jsonl").read_bytes()
    client = TestClient(make_app(tmp_path))

    # A durable resume intent that no engine has served yet: not alive, but about to be.
    EventStore(rd / "events.jsonl").append("resume_requested", {})
    blocked = _clear_trace(client, "demo", 0, rd=rd, op="d" * 32)
    assert blocked.status_code == 409
    assert (rd / "spans.jsonl").read_bytes() == before      # nothing rewritten under the launch

    # Once an engine has SERVED it, the run is quiet again and the rewrite proceeds.
    EventStore(rd / "events.jsonl").append("resume_served", {})
    cleared = _clear_trace(client, "demo", 0, rd=rd, op="e" * 32)
    assert cleared.status_code == 200 and cleared.json()["removed"] == 1


def test_authoring_writes_are_bounded_and_utf8_and_a_vanished_file_is_skipped(tmp_path, monkeypatch):
    """Three ways this pair could fail on ordinary input. The dirs are hot-reloaded into agent
    context, and `knowledge_dir` is AGENT-writable, so all three are reachable in a live run."""
    from looplab.serve.routers.misc import _AUTHOR_MAX_BYTES

    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(kdir))
    client = TestClient(make_app(tmp_path))

    # (1) A body larger than what the listing will ever DISPLAY was persisted whole and then shown
    # truncated forever — while the full oversized file still reached agent context.
    too_big = client.put("/api/knowledge/big.md", content=b"x" * (_AUTHOR_MAX_BYTES + 1))
    assert too_big.status_code == 400 and "too large" in too_big.text
    assert not (kdir / "big.md").exists()

    # (2) A non-UTF-8 body is a bad REQUEST, not an unhandled 500.
    bad_bytes = client.put("/api/knowledge/bad.md", content=b"\xff\xfe not utf-8")
    assert bad_bytes.status_code == 400 and "UTF-8" in bad_bytes.text

    ok = client.put("/api/knowledge/good.md", content="# notes\n".encode("utf-8"))
    assert ok.status_code == 200
    assert (kdir / "good.md").read_text(encoding="utf-8") == "# notes\n"

    # (3) A file that vanishes between the glob and the open (the agent deleting its own note) must
    # skip that entry, not 500 the whole listing.
    (kdir / "ghost.md").write_text("gone soon", encoding="utf-8")
    import looplab.serve.routers.misc as misc
    real_safe_read = misc._read_author_file_safely

    def vanishing_read(root, path):
        if path.name == "ghost.md":
            return None
        return real_safe_read(root, path)

    monkeypatch.setattr(misc, "_read_author_file_safely", vanishing_read)
    listing = client.get("/api/knowledge")
    monkeypatch.setattr(misc, "_read_author_file_safely", real_safe_read)
    assert listing.status_code == 200
    assert [f["name"] for f in listing.json()["files"]] == ["good.md"]


def test_a_fresh_app_answers_concurrent_first_requests_on_every_route(tmp_path):
    """The route-matching tables must be built BEFORE the first request, not by it.

    `include_router` leaves an `_IncludedRouter` placeholder that materializes its candidate list
    lazily on the first request that walks it (FastAPI 0.13x), memoized by a routes-version
    counter. The build publishes into `self._effective_candidates` incrementally and sets the
    version only at the END, so two requests arriving before the table is warm both see a stale
    version, both reset the list, and one matches against a HALF-BUILT candidate set.

    The symptom is a legitimate request answered as a CLIENT error: a `PUT /api/settings` whose GET
    sibling had been appended but whose PUT route had not comes back `405 Method Not Allowed`
    (`allow: GET`) — a partial path match with no full match. The UI opens several requests at once
    against a freshly started process, so this is a real cold-start bug, and it is what made
    `test_concurrent_disjoint_settings_puts_do_not_lose_updates` fail ~1 run in 20 under load: the
    405'd request never entered the transaction, so its rendezvous barrier timed out.

    Directly asserted here rather than left to that test's race: every request must land on its
    real handler, so a 404/405 anywhere is the regression."""
    app = make_app(tmp_path)
    clients = [TestClient(app) for _ in range(6)]
    probes = [
        ("GET", "/api/settings", None),
        ("PUT", "/api/settings", {"settings": {"max_nodes": 91}}),
        ("GET", "/api/runs", None),
        ("GET", "/api/health", None),
        ("GET", "/api/tasks", None),
        ("GET", "/api/settings", None),
    ]
    with ThreadPoolExecutor(max_workers=len(probes)) as executor:
        responses = list(executor.map(
            lambda pair: pair[0].request(
                pair[1][0], pair[1][1],
                **({"json": pair[1][2]} if pair[1][2] is not None else {})),
            zip(clients, probes)))

    for (method, path, _body), response in zip(probes, responses):
        assert response.status_code not in (404, 405), (
            f"{method} {path} hit a half-built route table: {response.status_code} "
            f"(allow={response.headers.get('allow')}). The lazy include cache was not warmed "
            "before the first concurrent requests.")
