"""Live UI server (the [ui] extra). Skipped entirely when fastapi isn't installed, so the base
offline suite is unaffected. Builds a real finished run, then exercises the read API, time-travel,
node detail, the control append, and config masking through FastAPI's TestClient.
"""
from __future__ import annotations

import json
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


def test_artifacts_list_and_view(tmp_path):
    """Artifacts: list the run dir AND a declared separate repo path, view text content, flag binary,
    and block path traversal / unknown roots."""
    _build_run(tmp_path)                                   # tmp_path/demo with events.jsonl, nodes/, …
    rd = tmp_path / "demo"
    (rd / "out.txt").write_bytes(b"hello artifact\n")      # bytes: no CRLF translation, exact-content check
    (rd / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    # a RepoTask-style snapshot pointing at a SEPARATE repo path on disk (not under runs/)
    repo = tmp_path / "myrepo"; (repo / "outputs").mkdir(parents=True)
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
    """The artifact routes serve raw file CONTENT, so when LOOPLAB_UI_TOKEN is set they're gated like a
    mutation — while other (projection-only) read routes stay open."""
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "sekret")
    _build_run(tmp_path)
    (tmp_path / "demo" / "out.txt").write_bytes(b"hi\n")
    client = TestClient(make_app(tmp_path))
    # raw-file reads require the token
    assert client.get("/api/runs/demo/artifacts").status_code == 401
    assert client.get("/api/runs/demo/artifact",
                      params={"root": "run", "path": "out.txt"}).status_code == 401
    # a folded-projection GET stays open (unchanged behaviour)
    assert client.get("/api/runs/demo/state").status_code == 200
    # with the token, content is served
    h = {"X-LoopLab-Token": "sekret"}
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
    # light projection reads stay open without the token (the established contract) — including the
    # LIGHT /nodes/{nid}/metrics projection, which must NOT be over-gated by the node-detail rule
    assert client.get("/api/runs/demo/state").status_code == 200
    assert client.get("/api/runs/demo/events").status_code == 200
    assert client.get("/api/runs/demo/nodes/0/metrics").status_code == 200
    assert client.get("/api/runs").status_code == 200


def test_assistant_session_transcript_is_token_gated(tmp_path, monkeypatch):
    """An assistant session transcript returns `raw` (the full model-facing instruction incl. attached
    file contents), so it must be gated like a raw-file read when LOOPLAB_UI_TOKEN is set."""
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "sekret")
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    # the session-transcript GET requires the token (not a 200 open read)
    assert client.get("/api/assistant/sessions/whatever").status_code == 401
    # a folded-projection GET still stays open
    assert client.get("/api/runs/demo/state").status_code == 200


def test_reserved_run_id_is_case_insensitive(tmp_path):
    """arch-review §5 P2: a reserved run id (assistant/reports) must be refused case-INSENSITIVELY —
    on a case-insensitive FS `ASSISTANT` would otherwise alias the reserved service store."""
    client = TestClient(make_app(tmp_path))
    for rid in ("assistant", "ASSISTANT", "Assistant", "REPORTS"):
        r = client.post("/api/start", json={"run_id": rid, "task": {"kind": "quadratic",
                                                                     "goal": "g", "direction": "min"}})
        assert r.status_code == 400 and "reserved" in r.json()["detail"], rid


def test_public_state_drops_stdout_and_redacts_error(tmp_path):
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
    s.append("node_failed", {"node_id": 1, "error": f"crashed with key={secret}", "reason": "crash"})
    client = TestClient(make_app(tmp_path))
    nodes = client.get("/api/runs/demo/state").json()["state"]["nodes"]
    assert "stdout_tail" not in nodes["0"]                       # raw captured stdout dropped from /state
    assert secret not in (nodes["1"].get("error") or "")         # error snippet redacted
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
    from looplab.serve.server import _engine_alive

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
                                 params={"seq": created_seq}).json()
    live_node = client.get(f"/api/runs/demo/nodes/{nid}").json()
    assert historical_node["annotations"] == []
    assert live_node["annotations"] == ["added after the snapshot"]
    assert historical_node["trace"] == {"nodes": []}
    assert historical_node["historical_seq"] == created_seq

    # A future node is an explicit 404 at an earlier prefix rather than a live-detail fallback.
    later = next(e for e in raw if e["type"] == "node_created" and e["seq"] > created_seq)
    assert client.get(f"/api/runs/demo/nodes/{later['data']['node_id']}",
                      params={"seq": created_seq}).status_code == 404


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

    sr = tmp_path / "stalled"; sr.mkdir()                       # engine died without run_finished
    (sr / "events.jsonl").write_text('{"seq":0,"type":"run_started","data":{}}\n', encoding="utf-8")
    r = client.delete("/api/runs/stalled")
    assert r.status_code == 200 and not sr.exists()             # was a spurious 409 before the fix


def test_engine_alive_unsupported_flock_reads_not_alive(tmp_path, monkeypatch):
    """On a FUSE/S3 mount (geesefs) flock is unsupported and raises a plain OSError — that must read as
    NOT alive (can't tell), not 'engine held'. The old code returned True for ANY OSError, which made
    every run look live forever and blocked deleting a stalled run. Only BlockingIOError = held."""
    fcntl = pytest.importorskip("fcntl")        # POSIX-only; Windows uses the msvcrt branch
    from looplab.serve.server import _engine_alive
    rd = tmp_path / "r"; rd.mkdir()
    (rd / "engine.lock").write_text("", encoding="utf-8")

    def _raise(exc):
        def _f(*a, **k):
            raise exc
        return _f
    monkeypatch.setattr(fcntl, "flock", _raise(OSError("flock not supported on this fs")))
    assert _engine_alive(rd) is False           # unsupported -> not alive (delete not blocked)
    monkeypatch.setattr(fcntl, "flock", _raise(BlockingIOError("held")))
    assert _engine_alive(rd) is True            # genuinely held by a live engine


def test_engine_singleton_fails_open_on_unsupported_flock(tmp_path, monkeypatch):
    """The OTHER half of the lock: on a FUSE/S3 mount where flock raises a plain OSError, the engine
    singleton must DEGRADE TO A NO-OP (yield True, run anyway) — NOT misread it as 'another engine holds
    it' and silently refuse to run. Before the fix a bare `except OSError` failed CLOSED, so on a
    JupyterHub geesefs home EVERY `run`/`resume` saw a phantom 'already running' and exited. Only a
    genuine BlockingIOError = held. Mirrors _engine_alive's fail-open so the two halves agree on FUSE."""
    fcntl = pytest.importorskip("fcntl")        # POSIX-only; Windows uses the msvcrt branch
    from looplab.cli import _engine_singleton
    rd = tmp_path / "r"

    def _raise(exc):
        def _f(*a, **k):
            raise exc
        return _f
    monkeypatch.setattr(fcntl, "flock", _raise(OSError("flock not supported on this fs")))
    with _engine_singleton(rd) as ok:
        assert ok is True            # unsupported lock -> fail OPEN (engine still runs)
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
    sr = tmp_path / "z"; sr.mkdir()                         # engine died after run_started (zombie)
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


def test_inject_node_control_append(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/control", json={"type": "inject_node", "data": {
        "idea": {"operator": "manual", "params": {"x": 0.5}, "rationale": "hand"}, "parent_id": None}})
    assert r.status_code == 200 and r.json()["type"] == "inject_node"
    st = fold(EventStore(tmp_path / "demo" / "events.jsonl").read_all())
    assert st.inject_requests and st.inject_requests[0]["idea"]["operator"] == "manual"


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


def test_g1_auth_token_required_on_mutating(tmp_path, monkeypatch):
    """G1: with LOOPLAB_UI_TOKEN set, mutating /api/* needs the X-LoopLab-Token header; reads stay open."""
    _build_run(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "s3cret")
    client = TestClient(make_app(tmp_path))
    # reads are open
    assert client.get("/api/runs").status_code == 200
    # mutating without the token -> 401
    r = client.post("/api/runs/demo/control", json={"type": "pause", "data": {}})
    assert r.status_code == 401
    # with the token -> allowed
    r = client.post("/api/runs/demo/control", json={"type": "pause", "data": {}},
                    headers={"X-LoopLab-Token": "s3cret"})
    assert r.status_code == 200


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


def test_g1_token_injected_only_on_top_level_navigation(tmp_path, monkeypatch):
    """Shared-origin hardening: the ll-token <meta> is handed out ONLY on a genuine top-level
    document navigation. A programmatic fetch()/XHR (Sec-Fetch-Dest: empty) or a framed load
    (iframe) gets a tokenless page, so a same-origin different-path page can't scrape the token.
    The token-bearing doc is also un-frameable and un-cacheable."""
    _build_run(tmp_path)
    _fake_dist(tmp_path, monkeypatch)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "s3cret")
    client = TestClient(make_app(tmp_path))

    # genuine top-level navigation -> token present + hardened
    r = client.get("/", headers={"Sec-Fetch-Dest": "document"})
    assert r.status_code == 200
    assert 'name="ll-token" content="s3cret"' in r.text
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in (r.headers.get("Content-Security-Policy") or "")
    assert r.headers.get("Cache-Control") == "no-store"

    # a programmatic fetch()/XHR must NOT receive the token, but stays un-frameable
    r = client.get("/", headers={"Sec-Fetch-Dest": "empty"})
    assert r.status_code == 200
    assert "ll-token" not in r.text
    assert r.headers.get("X-Frame-Options") == "DENY"

    # a framed load (would let a same-origin parent read contentDocument) gets no token
    r = client.get("/", headers={"Sec-Fetch-Dest": "iframe"})
    assert "ll-token" not in r.text

    # a client too old to send Sec-Fetch (header absent) still works -> fail-open on absence
    r = client.get("/")
    assert 'name="ll-token" content="s3cret"' in r.text

    # the SPA fallback (client-side route) is gated the same way
    r = client.get("/some/spa/route", headers={"Sec-Fetch-Dest": "empty"})
    assert "ll-token" not in r.text


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

    class _St:  # _action_to_control only reads st.best_node_id (for the approve verb)
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
    task = ToyTask.load(TASK); r, d = task.build_roles()
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
    rd = tmp_path / "acc"; rd.mkdir()
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
    task = ToyTask.load(TASK); r, d = task.build_roles()
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
