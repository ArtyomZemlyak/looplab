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

from looplab.engine.orchestrator import Engine  # noqa: E402
from looplab.search.policy import GreedyTree  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.events.eventstore import EventStore, iter_jsonl  # noqa: E402
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


def test_artifacts_list_and_view(tmp_path):
    """Artifacts: list the run dir AND a declared separate repo path, view text content, flag binary,
    and block path traversal / unknown roots."""
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
    roots = {r["id"]: r for r in client.get("/api/runs/demo/artifacts").json()["roots"]}
    assert "run" in roots and "editable:." in roots         # run dir + the separate repo path
    run_files = {f["path"] for f in roots["run"]["files"]}
    assert {"out.txt", "blob.bin"} <= run_files
    repo_files = {f["path"] for f in roots["editable:."]["files"]}
    assert {"train.py", "outputs/submission.csv"} <= repo_files

    # view a text file in the run dir
    v = client.get("/api/runs/demo/artifact", params={"root": "run", "path": "out.txt"}).json()
    assert v["is_text"] is True and v["content"] == "hello artifact\n"
    # view a file under the SEPARATE repo root (incl. a nested subdir)
    v2 = client.get("/api/runs/demo/artifact",
                    params={"root": "editable:.", "path": "outputs/submission.csv"}).json()
    assert "id,pred" in v2["content"]
    # binary file → flagged, no inline content
    vb = client.get("/api/runs/demo/artifact", params={"root": "run", "path": "blob.bin"}).json()
    assert vb["is_text"] is False and vb["content"] is None
    # path-traversal and unknown root are both rejected
    assert client.get("/api/runs/demo/artifact",
                      params={"root": "run", "path": "../../secret"}).status_code == 404
    assert client.get("/api/runs/demo/artifact",
                      params={"root": "nope", "path": "x"}).status_code == 404


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
    # with the token, content is served
    h = {"X-LoopLab-Token": "sekret"}
    assert client.get("/api/runs/demo/state", headers=h).status_code == 200
    assert client.get("/api/runs/demo/artifacts", headers=h).status_code == 200
    assert client.get("/api/runs/demo/artifact", params={"root": "run", "path": "out.txt"},
                      headers=h).json()["content"] == "hi\n"


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
                 # arch-review §4 P1-3: node DETAIL (full code/files/stdout_tail/parent code) and the
                 # billable model-completion health check were open; gate them.
                 "/api/runs/demo/nodes/0", "/api/llm/health"):
        assert client.get(path).status_code == 401, f"{path} should be gated"
        assert client.get(path, headers={"X-LoopLab-Token": "sekret"}).status_code == 200, path
    # unauthenticated health must NOT reach a (billable) model completion — 401 first
    assert client.get("/api/llm/health").status_code == 401
    # assistant progress is gated too (session query required once past auth)
    assert client.get("/api/assistant/progress?session=x").status_code == 401
    # P1-3 deny-default: even LIGHT projection reads now require the token (a new sensitive route can no
    # longer leak by being omitted from an allow-list). The zero-model /api/health is the sole exception.
    for light in ("/api/runs/demo/state",
                  "/api/runs/demo/nodes/0/metrics", "/api/runs"):
        assert client.get(light).status_code == 401, light
        assert client.get(light, headers={"X-LoopLab-Token": "sekret"}).status_code == 200, light
    assert client.get("/api/health").status_code == 200          # zero-model liveness stays open
    # F3: the live-state SSE stream is the ONE deny-default exception. The browser consumes it via a
    # headerless EventSource that CANNOT send X-LoopLab-Token, and its payload is already redacted, so
    # it MUST serve without the token — else every live update 401-loops and the dashboard freezes.
    with client.stream("GET", "/api/runs/demo/events") as resp:
        assert resp.status_code == 200


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
    """P1: a human posts a hypothesis to the board through /control (it's in CONTROL_EVENTS), it folds
    into state.hypotheses as `open`, and an abandon control event flips it to `abandoned`."""
    from looplab.core.models import hypothesis_id
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    stmt = "a log transform of the target helps"
    r = client.post("/api/runs/demo/control",
                    json={"type": "hypothesis_added", "data": {"statement": stmt, "source": "human"}})
    assert r.status_code == 200
    hyps = client.get("/api/runs/demo/state").json()["state"]["hypotheses"]
    hid = hypothesis_id(stmt)
    assert hid in hyps and hyps[hid]["status"] == "open" and hyps[hid]["source"] == "human"

    client.post("/api/runs/demo/control",
                json={"type": "hypothesis_updated", "data": {"id": hid, "status": "abandoned"}})
    hyps = client.get("/api/runs/demo/state").json()["state"]["hypotheses"]
    assert hyps[hid]["status"] == "abandoned"


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
        mutate = (pool.submit(client.post, "/api/runs/demo/reset") if mutation == "reset"
                  else pool.submit(client.delete, "/api/runs/demo"))
        _time.sleep(0.1)
        assert not mutate.done(), "lifecycle mutation crossed the in-flight launch fence"
        release.set()
        assert resume.result(timeout=2.0).status_code == 200
        assert mutate.result(timeout=2.0).status_code == 409


def test_reset_rename_failure_rolls_back_everything_and_never_spawns(tmp_path, monkeypatch):
    from looplab.serve.routers import control as control_router

    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _make_resumable(rd)
    (rd / "spans.jsonl").write_text('{"span":1}\n', encoding="utf-8")
    approved_archive = rd / "spans.jsonl.reset-1"
    approved_archive.write_text('{"approved":true}\n', encoding="utf-8")
    real_rename = Path.rename
    real_replace = Path.replace

    def _fail_source_of_truth(self, target):
        if self.name == "events.jsonl":
            raise OSError("injected event-log rename failure")
        if self.name.startswith("spans.jsonl.reset-"):
            raise AssertionError("rollback must not re-enter Path.rename")
        return real_rename(self, target)

    def _replace_with_windows_shadow(self, target):
        result = real_replace(self, target)
        if self.name.startswith("spans.jsonl.reset-"):
            self.with_name(f"{self.name.upper()}.tmp").write_text(
                "transaction shadow\n", encoding="utf-8")
        return result

    spawns = []
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", _fail_source_of_truth)
        patch.setattr(Path, "replace", _replace_with_windows_shadow)
        patch.setattr(
            control_router, "_spawn_engine", lambda *a, **kw: spawns.append((a, kw)))
        with TestClient(make_app(tmp_path)) as client:
            response = client.post("/api/runs/demo/reset")

    assert response.status_code == 500
    assert (rd / "events.jsonl").exists() and (rd / "spans.jsonl").exists()
    assert approved_archive.read_text(encoding="utf-8") == '{"approved":true}\n'
    assert [path for path in rd.glob("*.reset-*") if path != approved_archive] == []
    assert not spawns


def test_reset_spawn_failure_restores_archived_run(tmp_path, monkeypatch):
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
    assert response.status_code == 500
    assert (rd / "events.jsonl").read_bytes() == before
    assert (rd / "spans.jsonl").read_text(encoding="utf-8") == '{"span":1}\n'
    assert not list(rd.glob("*.reset-*"))


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
    raw = list(iter_jsonl(rd / "events.jsonl"))
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
    assert historical_node["annotations"] == []
    assert live_node["annotations"] == ["added after the snapshot"]
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
    raw = list(iter_jsonl(log))
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
        r = client.post("/api/runs/demo/nodes/0/clear_trace")
        assert r.status_code == 409
    assert len(list(iter_jsonl(rd / "spans.jsonl"))) == 4      # nothing removed while live

    # not live -> node 0's two spans gone, node 1 + unscoped kept, event log untouched
    r = client.post("/api/runs/demo/nodes/0/clear_trace")
    assert r.status_code == 200 and r.json()["removed"] == 2 and r.json()["kept"] == 2
    left = list(iter_jsonl(rd / "spans.jsonl"))
    assert {s["span_id"] for s in left} == {"c", "d"}
    assert (rd / "events.jsonl").read_bytes() == events_before

    # idempotent: clearing again removes nothing
    assert client.post("/api/runs/demo/nodes/0/clear_trace").json()["removed"] == 0


def test_trace_tail_survives_a_huge_recent_span_line(tmp_path):
    """Mega-review 07-06: the live trace feed read a FIXED 256KB tail window of spans.jsonl. A single
    span line can be 100KB+ (a repo-Developer generation carries the whole prompt+output on it), so one
    giant most-recent line could fill the window and blank the feed exactly during the heavy generations
    a user most wants to watch. The backward reader must still surface it."""
    _build_run(tmp_path)                                   # a real run dir at tmp_path/demo
    rd = tmp_path / "demo"
    big = "Z" * 400_000                                    # one generation span > the 256KB window
    spans = [
        {"span_id": "s1", "kind": "generation", "start": 1.0, "duration_s": 0.5, "status": "ok",
         "attributes": {"model": "m", "output": "early small gen"}},
        {"span_id": "s2", "kind": "generation", "start": 2.0, "duration_s": 9.0, "status": "ok",
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
    assert client.delete("/api/runs/done").status_code == 200
    assert not (tmp_path / "done").exists()

    sr = tmp_path / "stalled"
    sr.mkdir()                       # engine died without run_finished
    (sr / "events.jsonl").write_text('{"seq":0,"type":"run_started","data":{}}\n', encoding="utf-8")
    r = client.delete("/api/runs/stalled")
    assert r.status_code == 200 and not sr.exists()             # was a spurious 409 before the fix


def test_delete_and_reset_fail_closed_when_engine_liveness_is_unknown(tmp_path, monkeypatch):
    from looplab.serve.routers import control as control_router
    from looplab.serve.routers import org as org_router

    _build_run(tmp_path, "delete-unknown")
    _build_run(tmp_path, "reset-unknown")
    client = TestClient(make_app(tmp_path))
    monkeypatch.setattr(org_router, "_engine_liveness", lambda _rd: None)
    monkeypatch.setattr(control_router, "_engine_liveness", lambda _rd: None)

    deleted = client.delete("/api/runs/delete-unknown")
    assert deleted.status_code == 409
    assert deleted.json()["detail"]["code"] == "engine_liveness_unknown"
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


def _write_snapshot(rd: Path, **overrides):
    """Mimic what `cli run` writes: a masked Settings snapshot the resume path re-reads."""
    import json
    from looplab.core.config import Settings
    (rd / "config.snapshot.json").write_text(
        json.dumps(Settings(**overrides).masked_snapshot(), indent=2), encoding="utf-8")


def test_put_run_config_edits_snapshot_for_resume(tmp_path):
    import json
    from looplab.core.config import Settings
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    # the problematic run's real shape: short timeout, timeout-repair NOT yet enabled
    _write_snapshot(rd, timeout=30.0, inline_repair_reasons=["crash"])
    client = TestClient(make_app(tmp_path))

    r = client.put("/api/runs/demo/config",
                   json={"settings": {"timeout": 120.0, "inline_repair_reasons": ["crash", "timeout"]}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and set(body["changed"]) == {"timeout", "inline_repair_reasons"}
    # sending an UNCHANGED value is a no-op (only real diffs are written)
    r2 = client.put("/api/runs/demo/config", json={"settings": {"timeout": 120.0}})
    assert r2.json()["changed"] == []
    # persisted to the snapshot that resume re-reads via Settings(**snap)
    snap = json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
    assert snap["timeout"] == 120.0 and snap["inline_repair_reasons"] == ["crash", "timeout"]
    rebuilt = Settings(**{k: v for k, v in snap.items() if k != "llm_api_key"})
    assert rebuilt.timeout == 120.0      # what Engine() would get on resume


def test_put_run_config_rejects_invalid_value_naming_the_field(tmp_path):
    import json
    _build_run(tmp_path)
    _write_snapshot(tmp_path / "demo", timeout=30.0)
    client = TestClient(make_app(tmp_path))
    # the exact bug the user hit: Seeds = -1 (n_seeds has ge=1)
    r = client.put("/api/runs/demo/config", json={"settings": {"n_seeds": -1}})
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
        r = client.put("/api/runs/demo/config", json={"settings": {"timeout": 99.0}})
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

    r = client.put("/api/runs/demo/config",
                   json={"settings": {"timeout": 77.0, "llm_api_key": "leak"}})
    assert r.status_code == 200
    out = json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
    assert out["timeout"] == 77.0
    assert out["llm_api_key"] == "***"            # secret never overwritten via this endpoint
    assert out["some_future_key"] == "keepme"     # unknown key preserved verbatim
    assert "llm_api_key" not in r.json()["changed"]


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
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: cap)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/z/command", json={"instruction": "what now?"}).json()
    assert r["ok"] is True
    assert "STALLED" in cap.sys                             # boss was told the run is stalled -> can act


def test_put_run_config_404_without_snapshot(tmp_path):
    _build_run(tmp_path)                  # _build_run does NOT write config.snapshot.json
    client = TestClient(make_app(tmp_path))
    r = client.put("/api/runs/demo/config", json={"settings": {"timeout": 50.0}})
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
    assert len(calls) == 1
    conflict = next(r for r in responses if r.status_code == 409)
    assert conflict.json()["detail"]["code"] == "start_uncertain"


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
    h = client.get("/api/llm/health").json()
    assert "ok" in h and "model" in h


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


def test_sse_done_waits_for_error_finalize_recovery(tmp_path):
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
    recovered = threading.Event()

    def finish_recovery():
        store.append("run_finished", {"reason": "aborted"})
        recovered.set()

    timer = threading.Timer(0.6, finish_recovery)
    timer.start()
    try:
        response = TestClient(make_app(tmp_path)).get("/api/runs/recovering/events")
    finally:
        timer.join(timeout=2)
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
    """On a shared JupyterHub origin we warn that the token is per-deployment (not per-user), and
    that with no token the control plane is unauthenticated. No warning off-hub."""
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

    # on-hub WITHOUT a token -> control plane unauthenticated
    caplog.clear()
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
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

    r = client.post("/api/runs/demo/supertask", json={"supertask_id": st["id"]})
    assert r.status_code == 200
    summary = {x["run_id"]: x for x in client.get("/api/runs").json()}
    assert summary["demo"]["supertask_id"] == st["id"]          # surfaced in the run summary

    client.patch(f"/api/supertasks/{st['id']}", json={"name": "MLE-bench"})
    assert client.get("/api/supertasks").json()["supertasks"][0]["name"] == "MLE-bench"

    # assigning an unknown super-task -> 400; assigning a real run to an unknown run -> 404
    assert client.post("/api/runs/demo/supertask", json={"supertask_id": "st_x"}).status_code == 400
    assert client.post("/api/runs/ghost/supertask", json={"supertask_id": st["id"]}).status_code == 404

    client.post("/api/runs/demo/supertask", json={"supertask_id": None})  # clear
    assert {x["run_id"]: x for x in client.get("/api/runs").json()}["demo"]["supertask_id"] is None

    client.delete(f"/api/supertasks/{st['id']}")
    assert client.get("/api/supertasks").json()["supertasks"] == []


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
    carried into the fresh run."""
    import looplab.serve.server as server
    _build_run(tmp_path)                                          # a finished run (reset only runs on those)
    # patch AFTER the build — Popen is the shared module symbol the sandbox uses to run solution.py too
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: type("P", (), {})())
    rd = tmp_path / "demo"
    (rd / "ui_meta.json").write_text('{"task_file": "%s"}' % str(TASK).replace("\\", "/"), encoding="utf-8")

    client = TestClient(make_app(tmp_path))
    client.post("/api/runs/demo/chat-log", json={"role": "user", "content": "hello", "ts": 1.0, "seq": 1})
    assert (rd / "chat.jsonl").exists()

    assert client.post("/api/runs/demo/reset").status_code == 200
    assert not (rd / "chat.jsonl").exists()                       # archived out of the way
    assert any(p.name.startswith("chat.jsonl.reset-") for p in rd.iterdir())  # ...and recoverable


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
    assert _action_to_control(_Action(action="note", node_id=3, text="nice"), st)["type"] == "annotation"
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
    (nd / "looplab_stages.json").write_text('{"stages": [{"name": "train"}]}')
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
