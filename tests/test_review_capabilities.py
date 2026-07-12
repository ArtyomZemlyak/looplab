"""Security contract for revocable, server-enforced run review capabilities."""
from __future__ import annotations

import hashlib
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.events.eventstore import EventStore, iter_jsonl  # noqa: E402
from looplab.serve import reviews as reviews_module  # noqa: E402
from looplab.serve.routers import reviews as reviews_router  # noqa: E402
from looplab.serve.reviews import ReviewError, ReviewStore  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


OWNER = {"X-LoopLab-Token": "owner-secret"}


def _seed_run(root, run_id="demo"):
    rd = root / run_id
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": run_id, "task_id": "review-task",
                                  "goal": "review me", "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1}, "rationale": "baseline"},
        "code": "API_KEY='sk-abcdefghijklmnopqrstuvwxyz123456'\nprint('ok')\n",
        "files": {"helper.py": "db_password=ordinarysecretvalue\n"},
    })
    store.append("node_evaluated", {"node_id": 0, "metric": 1.25,
                                     "stdout_tail": "token=ghp_abcdefghijklmnopqrstuvwxyz123456",
                                     "trials": [{"params": {"x": 1}, "metric": 1.25,
                                                 "error": "credential=ordinarytrialsecret"}]})
    return rd


def _create(client, *, evidence=False):
    response = client.post("/api/runs/demo/reviews", headers=OWNER,
                           json={"ttl_seconds": 3600, "include_evidence": evidence})
    assert response.status_code == 200, response.text
    return response.json()


def test_share_requires_a_non_public_owner_principal(tmp_path, monkeypatch):
    _seed_run(tmp_path)
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path))
    response = client.post("/api/runs/demo/reviews",
                           json={"ttl_seconds": 3600, "include_evidence": False})
    assert response.status_code == 409
    assert "LOOPLAB_UI_TOKEN" in response.json()["detail"]
    assert not (tmp_path / ".reviews").exists()


def test_owner_secret_is_never_embedded_in_owner_or_review_html(tmp_path, monkeypatch):
    _seed_run(tmp_path)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><head></head><body>app</body></html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('app')\n", encoding="utf-8")
    monkeypatch.setenv("LOOPLAB_UI_DIST", str(dist))
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    client = TestClient(make_app(tmp_path))

    for path in ("/", "/index.html", "/review"):
        response = client.get(path, headers={"Sec-Fetch-Dest": "document"})
        assert response.status_code == 200
        assert "owner-secret" not in response.text
        assert "ll-token" not in response.text
    assert client.get("/api/auth/status").json() == {"required": True, "authenticated": False}
    assert client.get("/api/auth/status").headers["Cache-Control"] == "no-store"
    assert client.get("/api/auth/status", headers=OWNER).json()["authenticated"] is True
    assert client.post("/api/auth/verify", headers=OWNER, json={}).status_code == 200
    assert client.post("/api/auth/verify", headers={"X-LoopLab-Token": "wrong"}, json={}).status_code == 401
    # Owner auth must not disable caching for content-hashed static assets.  The HTML shell and API
    # remain no-store, while /assets keeps the static server's independent cache policy.
    assert client.get("/assets/app.js").headers.get("Cache-Control") != "no-store"


def test_summary_capability_is_one_run_read_only_and_revocable(tmp_path, monkeypatch):
    _seed_run(tmp_path, "demo")
    _seed_run(tmp_path, "other")
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    client = TestClient(make_app(tmp_path))

    # Owner link administration is itself owner-only, including the GET list.
    assert client.get("/api/runs/demo/reviews").status_code == 401
    created = _create(client)
    token = created["token"]
    review = {"X-LoopLab-Review": token}
    assert created["path"].startswith("review#/") and token not in created["path"].split("#", 1)[0]

    manifest = client.get("/api/review", headers=review)
    assert manifest.status_code == 200
    assert manifest.json()["run_id"] == "demo"
    assert manifest.headers["Cache-Control"] == "no-store"
    assert manifest.headers["Referrer-Policy"] == "no-referrer"
    assert manifest.headers["Vary"] == "X-LoopLab-Review"
    state = client.get("/api/review/state", headers=review)
    assert state.status_code == 200 and state.json()["state"]["run_id"] == "demo"
    # Review links expose the current projection only.  Arbitrary seq values would otherwise force
    # an expensive full fold for every unique cache key.
    assert client.get("/api/review/state?seq=0", headers=review).status_code == 400
    assert client.get("/api/review/nodes/0/metrics", headers=review).status_code == 200
    assert client.get("/api/review/nodes/0", headers=review).status_code == 403
    # A legacy run has no snapshot; never fall back to the current server's Settings.
    assert client.get("/api/review/config", headers=review).status_code == 404

    # A review principal cannot fall through to owner routes or select another run.
    assert client.get("/api/runs/demo/state", headers=review).status_code == 403
    assert client.get("/api/runs/other/state", headers=review).status_code == 403
    before = list(iter_jsonl(tmp_path / "demo" / "events.jsonl"))
    mutation_cases = [
        ("post", "/api/review/state", {}),
        ("post", "/api/runs/demo/control", {"type": "pause", "data": {}}),
        ("put", "/api/runs/demo/config", {"settings": {"timeout": 1}}),
        ("post", "/api/runs/demo/resume", {}),
        ("post", "/api/runs/demo/reset", {}),
        ("delete", "/api/runs/demo", None),
        ("post", "/api/start", {"run_id": "pwned", "task": {"kind": "quadratic"}}),
        ("post", "/api/assistant/sessions", {"title": "nope"}),
    ]
    for method, path, body in mutation_cases:
        response = client.request(method.upper(), path, headers=review, json=body)
        assert response.status_code == 403, (method, path, response.text)
    assert list(iter_jsonl(tmp_path / "demo" / "events.jsonl")) == before
    assert not (tmp_path / "pwned").exists()

    links = client.get("/api/runs/demo/reviews", headers=OWNER).json()["links"]
    assert len(links) == 1 and links[0]["status"] == "active"
    assert client.delete(f"/api/runs/demo/reviews/{created['id']}", headers=OWNER).status_code == 200
    for path in ("/api/review", "/api/review/state"):
        ended = client.get(path, headers=review)
        assert ended.status_code == 410
        assert ended.headers["Cache-Control"] == "no-store"
        assert ended.headers["Referrer-Policy"] == "no-referrer"
        assert ended.headers["Vary"] == "X-LoopLab-Review"


def test_evidence_is_opt_in_redacted_and_digest_only_on_disk(tmp_path, monkeypatch):
    rd = _seed_run(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("annotation", {"node_id": 0, "text": "password=ordinaryannotationsecret"})
    store.append("hint", {"text": "API_KEY=ordinaryhintsecret"})
    store.append("inject_node", {
        "parent_id": 0, "idea": "manual", "code": "SECRET=ordinaryinjectedsecret",
        "files": {"private.py": "token=ordinaryfilesecret"},
    })
    store.append("spec_proposed", {
        "goal": "safe public goal", "adapter_files": {"adapter.py": "password=adaptersecret"},
    })
    (rd / "config.snapshot.json").write_text(json.dumps({
        "max_eval_seconds": 1234,
        "trust_mode": "sandbox",
        "eval_trust_mode": "ratify_freeze_drift",
        "trust_gate": "gate",
        "reward_hack_detect": True,
        "llm_base_url": "https://internal-model.example/v1",
        "llm_api_key": "ordinaryconfigsecret",
        "repo_root": "C:/private/customer/repo",
    }), encoding="utf-8")
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    client = TestClient(make_app(tmp_path))
    created = _create(client, evidence=True)
    token = created["token"]
    detail = client.get("/api/review/nodes/0", headers={"X-LoopLab-Review": token})
    assert detail.status_code == 200
    payload = detail.json()
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in payload["code"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in payload.get("stdout_tail", "")
    assert "ordinarysecretvalue" not in json.dumps(payload)
    assert "ordinarytrialsecret" not in json.dumps(payload)
    assert "ordinaryannotationsecret" not in json.dumps(payload)
    assert payload["trace"]["nodes"] == []
    assert client.get("/api/review/nodes/0?seq=0",
                      headers={"X-LoopLab-Review": token}).status_code == 400

    # Summary is recursively scrubbed and cannot disclose source smuggled through folded control or
    # onboarding fields instead of the normal node.code field.
    summary = client.get("/api/review/state", headers={"X-LoopLab-Review": token}).json()
    summary_raw = json.dumps(summary)
    for secret in ("ordinaryhintsecret", "ordinaryinjectedsecret", "ordinaryfilesecret",
                   "adaptersecret", "ordinaryannotationsecret"):
        assert secret not in summary_raw
    assert "code" not in summary["state"]["inject_requests"][0]
    assert "files" not in summary["state"]["inject_requests"][0]
    assert "adapter_files" not in summary["state"]["proposed_spec"]

    # Only the tiny, UI-consumed trust/budget subset of config is reviewable.
    config = client.get("/api/review/config", headers={"X-LoopLab-Review": token}).json()
    assert config == {
        "max_eval_seconds": 1234,
        "trust_mode": "sandbox",
        "eval_trust_mode": "ratify_freeze_drift",
        "trust_gate": "gate",
        "reward_hack_detect": True,
    }
    assert "internal-model" not in json.dumps(config) and "ordinaryconfigsecret" not in json.dumps(config)

    files = list((tmp_path / ".reviews").glob("*.json"))
    assert len(files) == 1
    raw = files[0].read_text(encoding="utf-8")
    assert token not in raw
    assert hashlib.sha256(token.encode()).hexdigest() in raw


def test_store_expiry_is_checked_on_every_resolution(tmp_path):
    store = ReviewStore(tmp_path / ".reviews")
    token, record = store.create("demo", ttl_seconds=300)
    assert store.resolve(token, now=record["created_at"] + 299)["run_id"] == "demo"
    with pytest.raises(ReviewError, match="expired") as exc:
        store.resolve(token, now=record["expires_at"] + 1)
    assert exc.value.kind == "expired"

    # Malformed and non-finite JSON numbers fail closed instead of becoming immortal.  (Python's
    # json.loads accepts NaN/Infinity unless callers explicitly defend against them.)
    path = next((tmp_path / ".reviews").glob("*.json"))
    base = json.loads(path.read_text(encoding="utf-8"))
    for bad in ("not-a-time", "NaN", float("nan"), float("inf")):
        row = dict(base)
        row["expires_at"] = bad
        path.write_text(json.dumps(row), encoding="utf-8")
        assert store.list_for_run("demo")[0]["status"] == "expired"
        with pytest.raises(ReviewError, match="expired"):
            store.resolve(token)

    row = dict(base)
    row["created_at"] = float("nan")
    path.write_text(json.dumps(row), encoding="utf-8")
    assert store.list_for_run("demo")[0]["created_at"] == 0.0
    with pytest.raises(ReviewError, match="invalid"):
        store.resolve(token)

    row = dict(base)
    row["revoked_at"] = float("nan")
    path.write_text(json.dumps(row), encoding="utf-8")
    listed = store.list_for_run("demo")[0]
    assert listed["status"] == "revoked" and listed["revoked_at"] == 0.0
    with pytest.raises(ReviewError, match="revoked"):
        store.resolve(token)

    row = dict(base)
    row["scopes"] = [{"evidence": True}]
    path.write_text(json.dumps(row), encoding="utf-8")
    assert store.list_for_run("demo")[0]["scopes"] == []
    with pytest.raises(ReviewError, match="invalid"):
        store.resolve(token)


def test_store_collision_never_overwrites_an_existing_capability(tmp_path, monkeypatch):
    store = ReviewStore(tmp_path / ".reviews")
    ids = iter(["a" * 32, "a" * 32, "b" * 32])
    monkeypatch.setattr(reviews_module.secrets, "token_hex", lambda n: next(ids))

    first_token, first = store.create("first", ttl_seconds=300)
    second_token, second = store.create("second", ttl_seconds=300)

    assert first["id"] == "rvl_" + "a" * 32
    assert second["id"] == "rvl_" + "b" * 32
    assert store.resolve(first_token)["run_id"] == "first"
    assert store.resolve(second_token)["run_id"] == "second"
    assert len(list((tmp_path / ".reviews").glob("rvl_*.json"))) == 2


def test_review_metrics_are_bounded_finite_numeric_projections(tmp_path, monkeypatch):
    _seed_run(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    points = [{"step": i, "value": i / 10, "wall_time": 100 + i,
               "path": "C:/private/customer/data", "note": "password=metricssecret"}
              for i in range(5_002)]
    points.append({"step": 9_999, "value": float("nan"), "wall_time": 1})
    monkeypatch.setattr(reviews_router, "read_node_metrics", lambda _: {
        "loss": points,
        "token=ghp_abcdefghijklmnopqrstuvwxyz123456": [
            {"step": 1, "value": 0.5, "wall_time": 1.0, "raw": "metricssecret"}],
        "bad": "C:/private/path",
    })
    client = TestClient(make_app(tmp_path))
    token = _create(client)["token"]

    response = client.get("/api/review/nodes/0/metrics",
                          headers={"X-LoopLab-Review": token})
    assert response.status_code == 200
    payload = response.json()["metrics"]
    raw = json.dumps(payload)
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in raw
    assert "private/customer" not in raw and "metricssecret" not in raw
    assert 0 < len(payload["loss"]) <= 5_000
    assert all(set(point) == {"step", "value", "wall_time"} for point in payload["loss"])
    assert all(point["value"] == point["value"] for series in payload.values() for point in series)
