"""Block 8: bounded, generation-safe operator collaboration contracts."""
from __future__ import annotations

import json
import os
import re

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from looplab.core.models import Event  # noqa: E402
from looplab.events.comment_projection import (  # noqa: E402
    COMMENT_MAX_PER_NODE_GENERATION, COMMENT_MAX_PER_RUN, COMMENT_MAX_VERSION,
    COMMENT_TEXT_MAX_BYTES, CommentCursorError, apply_comment_event, comments_page,
    history_page, normalize_comment_text, project_comments)
from looplab.events.eventstore import (  # noqa: E402
    EventStore, EventStoreConcurrencyError, EventStoreLockError, _interprocess_lock)
from looplab.events.replay import fold  # noqa: E402
from looplab.events.types import (  # noqa: E402
    EV_ANNOTATION, EV_COMMENT_CREATED, EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED)
from looplab.serve.server import make_app  # noqa: E402


OWNER = {"X-LoopLab-Token": "owner-secret"}
COMMENT_ID_RE = re.compile(r"^cmt_[0-9a-f]{32}$")
AUTH_VARY = {"Authorization", "X-LoopLab-Review", "X-LoopLab-Token"}


def _vary(response):
    return {value.strip() for value in response.headers["Vary"].split(",")}


def _seed(root, run_id="demo"):
    rd = root / run_id
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": run_id, "task_id": "task", "goal": "g", "direction": "min",
    })
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "base"},
        "code": "print(1)",
    })
    return rd


def _generation(client, headers=None):
    response = client.get("/api/runs/demo/state", headers=headers or {})
    assert response.status_code == 200, response.text
    generation = response.json()["generation"]
    assert isinstance(generation, str) and len(generation) == 64
    return generation


def _command(client, event_type, data, key, *, generation=None, headers=None):
    request_headers = {**(headers or {}), "Idempotency-Key": key}
    return client.post(
        "/api/runs/demo/commands", headers=request_headers,
        json={
            "type": event_type,
            "data": data,
            "expected_generation": generation or _generation(client, headers),
        },
    )


def _created_event(rd):
    rows = [event for event in EventStore(rd / "events.jsonl").read_all()
            if event.type == EV_COMMENT_CREATED]
    assert len(rows) == 1
    return rows[0]


def _event(seq, event_type, data, *, ts=None):
    return Event(seq=seq, ts=float(seq if ts is None else ts), type=event_type, data=data)


def _create_data(comment_id, *, node=0, attempt=0, text="hello", actor="local_operator"):
    return {
        "comment_id": comment_id,
        "node_id": node,
        "node_generation": attempt,
        "text": text,
        "actor_kind": actor,
        "version": 1,
    }


def test_create_edit_resolve_reopen_and_owner_history_are_exact(tmp_path):
    rd = _seed(tmp_path)
    client = TestClient(make_app(tmp_path))
    generation = _generation(client)

    created = _command(client, EV_COMMENT_CREATED, {
        "node_id": 0, "node_generation": 0, "text": "first\nline",
    }, "comment-create", generation=generation)
    assert created.status_code == 200 and created.json()["status"] == "succeeded"
    event = _created_event(rd)
    comment_id = event.data["comment_id"]
    assert COMMENT_ID_RE.fullmatch(comment_id)
    assert event.data == {
        "comment_id": comment_id,
        "node_id": 0,
        "node_generation": 0,
        "text": "first\nline",
        "actor_kind": "local_operator",
        "version": 1,
        "_command_id": created.json()["id"],
    }

    mutations = [
        (EV_COMMENT_EDITED, {"text": "edited"}, "comment-edit"),
        (EV_COMMENT_RESOLUTION_CHANGED, {"resolved": True}, "comment-resolve"),
        (EV_COMMENT_RESOLUTION_CHANGED, {"resolved": False}, "comment-reopen"),
    ]
    for version, (event_type, extra, key) in enumerate(mutations, start=1):
        response = _command(client, event_type, {
            "comment_id": comment_id,
            "node_id": 0,
            "node_generation": 0,
            "expected_version": version,
            **extra,
        }, key, generation=generation)
        assert response.status_code == 200 and response.json()["status"] == "succeeded"

    current = client.get("/api/runs/demo/comments").json()
    assert current["run_generation"] == generation
    assert current["has_more"] is False and current["next_cursor"] is None
    assert current["comments"] == [{
        "comment_id": comment_id,
        "node_id": 0,
        "node_generation": 0,
        "text": "edited",
        "actor_kind": "local_operator",
        "actor_label": "Local operator",
        "version": 4,
        "resolved": False,
        "created_at": event.ts,
        "updated_at": pytest.approx(
            [row for row in EventStore(rd / "events.jsonl").read_all()
             if row.type == EV_COMMENT_RESOLUTION_CHANGED][-1].ts),
        "legacy": False,
        "editable": True,
    }]
    history = client.get(f"/api/runs/demo/comments/{comment_id}/history").json()
    assert history["run_generation"] == generation
    assert [row["action"] for row in history["versions"]] == [
        "reopened", "resolved", "edited", "created",
    ]
    assert [row["version"] for row in history["versions"]] == [4, 3, 2, 1]
    assert [row["text"] for row in history["versions"]] == [
        "edited", "edited", "edited", "first\nline",
    ]
    assert all(row["actor_label"] == "Local operator" for row in history["versions"])


def test_server_owns_actor_id_and_enforces_utf8_byte_limit(tmp_path, monkeypatch):
    rd = _seed(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    client = TestClient(make_app(tmp_path))
    generation = _generation(client, OWNER)
    exact = "é" * (COMMENT_TEXT_MAX_BYTES // 2)
    assert len(exact) < COMMENT_TEXT_MAX_BYTES and len(exact.encode()) == COMMENT_TEXT_MAX_BYTES

    accepted = _command(client, EV_COMMENT_CREATED, {
        "node_id": 0, "node_generation": 0, "text": exact,
    }, "max-utf8", generation=generation, headers=OWNER)
    assert accepted.status_code == 200 and accepted.json()["status"] == "succeeded"
    event = _created_event(rd)
    assert event.data["actor_kind"] == "deployment_owner"
    assert COMMENT_ID_RE.fullmatch(event.data["comment_id"])

    too_large = _command(client, EV_COMMENT_CREATED, {
        "node_id": 0, "node_generation": 0, "text": exact + "é",
    }, "too-large", generation=generation, headers=OWNER)
    assert too_large.status_code == 200
    assert too_large.json()["status"] == "rejected"
    assert too_large.json()["error"]["code"] == "invalid_comment_text"
    assert len([row for row in EventStore(rd / "events.jsonl").read_all()
                if row.type == EV_COMMENT_CREATED]) == 1

    forged = _command(client, EV_COMMENT_CREATED, {
        "node_id": 0, "node_generation": 0, "text": "forged",
        "actor_kind": "reviewer", "comment_id": "cmt_" + "f" * 32,
    }, "forged-stamps", generation=generation, headers=OWNER)
    assert forged.status_code == 200 and forged.json()["status"] == "rejected"
    assert forged.json()["error"]["code"] == "invalid_command"
    with pytest.raises(ValueError, match="valid UTF-8"):
        normalize_comment_text("\ud800")


def test_exact_run_node_attempt_and_comment_version_preconditions(tmp_path):
    rd = _seed(tmp_path)
    client = TestClient(make_app(tmp_path))
    generation = _generation(client)

    stale_attempt = _command(client, EV_COMMENT_CREATED, {
        "node_id": 0, "node_generation": 1, "text": "wrong attempt",
    }, "wrong-attempt", generation=generation)
    assert stale_attempt.status_code == 200
    assert stale_attempt.json()["error"]["code"] == "node_generation_changed"

    _command(client, EV_COMMENT_CREATED, {
        "node_id": 0, "node_generation": 0, "text": "v1",
    }, "exact-create", generation=generation)
    comment_id = _created_event(rd).data["comment_id"]
    first_edit = _command(client, EV_COMMENT_EDITED, {
        "comment_id": comment_id, "node_id": 0, "node_generation": 0,
        "expected_version": 1, "text": "v2",
    }, "exact-edit", generation=generation)
    assert first_edit.json()["status"] == "succeeded"

    conflict = _command(client, EV_COMMENT_EDITED, {
        "comment_id": comment_id, "node_id": 0, "node_generation": 0,
        "expected_version": 1, "text": "lost update",
    }, "stale-edit", generation=generation)
    assert conflict.status_code == 200 and conflict.json()["status"] == "rejected"
    assert conflict.json()["error"]["code"] == "comment_version_changed"
    assert conflict.json()["error"]["current_version"] == 2

    wrong_subject = _command(client, EV_COMMENT_EDITED, {
        "comment_id": comment_id, "node_id": 0, "node_generation": 1,
        "expected_version": 2, "text": "wrong lifecycle",
    }, "wrong-subject", generation=generation)
    assert wrong_subject.json()["error"]["code"] == "comment_subject_changed"
    assert client.get("/api/runs/demo/comments").json()["comments"][0]["text"] == "v2"

    (rd / "events.jsonl").rename(rd / "events.jsonl.old")
    EventStore(rd / "events.jsonl").append("run_started", {
        "run_id": "demo", "task_id": "replacement", "goal": "new", "direction": "min",
    })
    stale_run = _command(client, EV_COMMENT_CREATED, {
        "node_id": 0, "node_generation": 0, "text": "old generation",
    }, "stale-run", generation=generation)
    assert stale_run.status_code == 409
    assert stale_run.json()["detail"]["code"] == "run_generation_changed"
    assert [row.type for row in EventStore(rd / "events.jsonl").read_all()] == ["run_started"]


def test_strict_lock_wraps_unsupported_runtime_operations(tmp_path, monkeypatch):
    def unsupported(*_args, **_kwargs):
        raise NotImplementedError("advisory locking disabled")

    if os.name == "nt":
        import msvcrt
        monkeypatch.setattr(msvcrt, "locking", unsupported)
    else:
        import fcntl
        monkeypatch.setattr(fcntl, "flock", unsupported)

    lock = tmp_path / "events.lock"
    with _interprocess_lock(lock, required=False):
        pass
    with pytest.raises(EventStoreLockError, match="lock is unavailable"):
        with _interprocess_lock(lock, required=True):
            pass


def test_lock_failure_is_503_and_exact_retry_appends_once(tmp_path, monkeypatch):
    rd = _seed(tmp_path)
    app = make_app(tmp_path)
    client = TestClient(app)
    generation = _generation(client)
    original_append = EventStore.append
    unavailable = {"value": True}

    def append(self, event_type, data, **kwargs):
        if (event_type == EV_COMMENT_CREATED and kwargs.get("require_lock")
                and unavailable["value"]):
            raise EventStoreLockError(self.path, NotImplementedError("no advisory locks"))
        return original_append(self, event_type, data, **kwargs)

    monkeypatch.setattr(EventStore, "append", append)
    first = _command(client, EV_COMMENT_CREATED, {
        "node_id": 0, "node_generation": 0, "text": "retry me",
    }, "strict-retry", generation=generation)
    assert first.status_code == 503
    assert first.json()["detail"]["code"] == "event_lock_unavailable"
    command_id = first.json()["detail"]["command_id"]
    assert not [row for row in EventStore(rd / "events.jsonl").read_all()
                if row.type == EV_COMMENT_CREATED]

    # A pre-append terminal lock failure is safe for destructive guards; an accepted/executing
    # command would still be rejected by this same guard via its record/claim.
    with app.state.looplab.commands.destructive_guard(rd, "inspect terminal command") as guarded:
        assert guarded == rd

    record_path = rd / ".commands" / f"{command_id}.json"
    reserved_id = json.loads(record_path.read_text(encoding="utf-8"))["data"]["comment_id"]
    unavailable["value"] = False
    retried = client.post(f"/api/runs/demo/commands/{command_id}/retry")
    assert retried.status_code == 200 and retried.json()["status"] == "succeeded"
    event = _created_event(rd)
    assert event.data["comment_id"] == reserved_id
    assert event.data["_command_id"] == command_id
    assert json.loads(record_path.read_text(encoding="utf-8"))["retry_count"] == 1


def test_strict_comment_append_retries_only_unrelated_tail_races(tmp_path, monkeypatch):
    rd = _seed(tmp_path)
    client = TestClient(make_app(tmp_path))
    generation = _generation(client)
    original_append = EventStore.append
    races = {"remaining": 2}

    def append(self, event_type, data, **kwargs):
        if (event_type == EV_COMMENT_CREATED and kwargs.get("require_lock")
                and races["remaining"]):
            races["remaining"] -= 1
            unrelated = original_append(self, "agent_decision", {"action": "wait"})
            raise EventStoreConcurrencyError(
                self.path, int(kwargs["expected_last_seq"]), unrelated.seq)
        return original_append(self, event_type, data, **kwargs)

    monkeypatch.setattr(EventStore, "append", append)
    response = _command(client, EV_COMMENT_CREATED, {
        "node_id": 0, "node_generation": 0, "text": "after engine traffic",
    }, "cas-retry", generation=generation)
    assert response.status_code == 200 and response.json()["status"] == "succeeded"
    assert races["remaining"] == 0
    assert [row.type for row in EventStore(rd / "events.jsonl").read_all()].count(
        EV_COMMENT_CREATED) == 1


def test_idempotency_survives_lost_response_and_run_reset(tmp_path):
    rd = _seed(tmp_path)
    client = TestClient(make_app(tmp_path))
    generation_a = _generation(client)
    body = {"node_id": 0, "node_generation": 0, "text": "exactly once"}
    first = _command(client, EV_COMMENT_CREATED, body, "idempotent", generation=generation_a)
    replay = _command(client, EV_COMMENT_CREATED, body, "idempotent", generation=generation_a)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert len([row for row in EventStore(rd / "events.jsonl").read_all()
                if row.type == EV_COMMENT_CREATED]) == 1

    (rd / "events.jsonl").rename(rd / "events.jsonl.generation-a")
    EventStore(rd / "events.jsonl").append("run_started", {
        "run_id": "demo", "task_id": "replacement", "goal": "b", "direction": "min",
    })
    lost_response = _command(
        client, EV_COMMENT_CREATED, body, "idempotent", generation=generation_a)
    assert lost_response.status_code == 200 and lost_response.json() == first.json()
    assert [row.type for row in EventStore(rd / "events.jsonl").read_all()] == ["run_started"]


def test_legacy_annotations_are_visible_but_immutable_and_unattributed(tmp_path):
    rd = _seed(tmp_path)
    legacy = EventStore(rd / "events.jsonl").append(
        EV_ANNOTATION, {"node_id": 0, "text": "old note"})
    client = TestClient(make_app(tmp_path))
    generation = _generation(client)
    current = client.get("/api/runs/demo/comments").json()["comments"]
    assert current == [{
        "comment_id": f"legacy_{legacy.seq}",
        "node_id": 0,
        "node_generation": None,
        "text": "old note",
        "actor_kind": "legacy_unknown",
        "actor_label": "Legacy note (unattributed)",
        "version": 1,
        "resolved": False,
        "created_at": legacy.ts,
        "updated_at": legacy.ts,
        "legacy": True,
        "editable": False,
    }]
    edit = _command(client, EV_COMMENT_EDITED, {
        "comment_id": f"legacy_{legacy.seq}", "node_id": 0, "node_generation": 0,
        "expected_version": 1, "text": "rewrite history",
    }, "legacy-edit", generation=generation)
    assert edit.status_code == 200 and edit.json()["status"] == "rejected"
    assert edit.json()["error"]["code"] == "legacy_comment_read_only"
    assert not [row for row in EventStore(rd / "events.jsonl").read_all()
                if row.type == EV_COMMENT_EDITED]


def test_state_and_sse_expose_only_revision_not_free_form_text(tmp_path):
    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append(EV_ANNOTATION, {"node_id": 0, "text": "legacy-private-text"})
    comment = store.append(EV_COMMENT_CREATED, _create_data(
        "cmt_" + "1" * 32, text="modern-private-text"))
    # Let TestClient consume the finite initial SSE frame + done frame rather than keeping an
    # intentionally live unfinished-run stream open for the rest of the test process.
    store.append("run_finished", {"reason": "done"})
    client = TestClient(make_app(tmp_path))

    response = client.get("/api/runs/demo/state")
    raw = response.text
    assert response.status_code == 200
    assert "annotations" not in raw and "comments\"" not in raw
    assert "legacy-private-text" not in raw and "modern-private-text" not in raw
    assert response.json()["state"]["comments_revision"] == comment.seq

    with client.stream("GET", "/api/runs/demo/events") as stream:
        assert stream.status_code == 200
        data_line = next(line for line in stream.iter_lines() if line.startswith("data: "))
    assert "legacy-private-text" not in data_line and "modern-private-text" not in data_line
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["state"]["comments_revision"] == comment.seq
    assert "comments" not in payload["state"] and "annotations" not in payload["state"]


def test_review_gets_only_redacted_current_values_and_never_history_or_mutation(
        tmp_path, monkeypatch):
    rd = _seed(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    client = TestClient(make_app(tmp_path))
    generation = _generation(client, OWNER)
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    created = _command(client, EV_COMMENT_CREATED, {
        "node_id": 0, "node_generation": 0, "text": f"credential={secret}",
    }, "review-comment", generation=generation, headers=OWNER)
    assert created.json()["status"] == "succeeded"
    comment_id = _created_event(rd).data["comment_id"]
    edited = _command(client, EV_COMMENT_EDITED, {
        "comment_id": comment_id, "node_id": 0, "node_generation": 0,
        "expected_version": 1, "text": "safe current value",
    }, "review-edit", generation=generation, headers=OWNER)
    assert edited.json()["status"] == "succeeded"
    link = client.post("/api/runs/demo/reviews", headers=OWNER,
                       json={"ttl_seconds": 3600, "include_evidence": False})
    assert link.status_code == 200
    review = {"X-LoopLab-Review": link.json()["token"]}

    projected = client.get("/api/review/comments", headers=review)
    assert projected.status_code == 200
    assert projected.headers["Cache-Control"] == "no-store"
    assert _vary(projected) == AUTH_VARY
    assert projected.json()["comments"][0]["text"] == "safe current value"
    assert projected.json()["comments"][0]["editable"] is False
    assert secret not in projected.text
    assert secret not in client.get("/api/review/state", headers=review).text

    history = client.get(
        f"/api/runs/demo/comments/{comment_id}/history", headers=OWNER)
    assert history.status_code == 200 and secret in history.text
    assert client.get(
        f"/api/review/comments/{comment_id}/history", headers=review).status_code == 404
    for method in ("post", "put", "patch", "delete"):
        denied = client.request(
            method.upper(), "/api/review/comments", headers={**OWNER, **review}, json={})
        assert denied.status_code == 403
    assert len([row for row in EventStore(rd / "events.jsonl").read_all()
                if row.type.startswith("comment_")]) == 2


def test_exact_attempt_filters_pagination_and_stale_cursors(tmp_path):
    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    first = store.append(EV_COMMENT_CREATED, _create_data("cmt_" + "1" * 32, text="old-1"))
    second = store.append(EV_COMMENT_CREATED, _create_data("cmt_" + "2" * 32, text="old-2"))
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    third = store.append(EV_COMMENT_CREATED, _create_data(
        "cmt_" + "3" * 32, attempt=1, text="new-attempt"))
    client = TestClient(make_app(tmp_path))

    assert client.get("/api/runs/demo/comments?node_id=0").status_code == 400
    assert client.get("/api/runs/demo/comments?node_generation=0").status_code == 400
    old = client.get("/api/runs/demo/comments?node_id=0&node_generation=0").json()
    assert [row["text"] for row in old["comments"]] == ["old-2", "old-1"]
    new = client.get("/api/runs/demo/comments?node_id=0&node_generation=1").json()
    assert [row["text"] for row in new["comments"]] == ["new-attempt"]

    page1 = client.get("/api/runs/demo/comments?limit=2").json()
    assert [row["comment_id"] for row in page1["comments"]] == [
        third.data["comment_id"], second.data["comment_id"],
    ]
    assert page1["has_more"] is True and page1["next_cursor"]
    page2 = client.get(
        "/api/runs/demo/comments", params={"limit": 2, "cursor": page1["next_cursor"]})
    assert page2.status_code == 200
    assert [row["comment_id"] for row in page2.json()["comments"]] == [
        first.data["comment_id"],
    ]

    forged = page1["next_cursor"].rsplit(".", 1)[0] + ".7fffffff"
    stale = client.get("/api/runs/demo/comments", params={"limit": 2, "cursor": forged})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "comment_cursor_stale"
    wrong_scope = client.get("/api/runs/demo/comments", params={
        "node_id": 0, "node_generation": 0, "limit": 2, "cursor": page1["next_cursor"],
    })
    assert wrong_scope.status_code == 409

    open_page = client.get(
        "/api/runs/demo/comments?limit=2&include_resolved=false").json()
    store.append(EV_COMMENT_RESOLUTION_CHANGED, {
        "comment_id": second.data["comment_id"], "node_id": 0, "node_generation": 0,
        "base_version": 1, "version": 2, "resolved": True,
        "actor_kind": "local_operator",
    })
    disappeared_anchor = client.get("/api/runs/demo/comments", params={
        "limit": 2, "include_resolved": False, "cursor": open_page["next_cursor"],
    })
    assert disappeared_anchor.status_code == 409


def test_projection_caps_and_malformed_events_are_deterministic_noops():
    comments = {}
    for seq in range(COMMENT_MAX_PER_NODE_GENERATION + 1):
        apply_comment_event(comments, _event(
            seq, EV_COMMENT_CREATED,
            _create_data(f"cmt_{seq:032x}", text=f"comment {seq}")))
    assert len(comments) == COMMENT_MAX_PER_NODE_GENERATION

    run_comments = {}
    for seq in range(COMMENT_MAX_PER_RUN + 1):
        apply_comment_event(run_comments, _event(
            seq, EV_COMMENT_CREATED,
            _create_data(f"cmt_{seq:032x}", node=seq, text=f"comment {seq}")))
    assert len(run_comments) == COMMENT_MAX_PER_RUN

    comment_id = "cmt_" + "a" * 32
    chain = [_event(0, EV_COMMENT_CREATED, _create_data(comment_id, text="v1"))]
    for version in range(2, COMMENT_MAX_VERSION + 2):
        chain.append(_event(version - 1, EV_COMMENT_EDITED, {
            "comment_id": comment_id, "node_id": 0, "node_generation": 0,
            "base_version": version - 1, "version": version,
            "text": f"v{version}", "actor_kind": "local_operator",
        }))
    capped, history = project_comments(chain, include_history=True)
    assert capped[comment_id].version == COMMENT_MAX_VERSION
    assert capped[comment_id].text == f"v{COMMENT_MAX_VERSION}"
    assert len(history[comment_id]) == COMMENT_MAX_VERSION
    assert comments_page(capped, generation="a" * 64, limit=100)["comments"][0][
        "editable"] is False

    malformed = [
        _event(0, EV_COMMENT_CREATED, _create_data("bad", text="invalid id")),
        _event(1, EV_COMMENT_CREATED, _create_data(comment_id, text="valid")),
        _event(2, EV_COMMENT_EDITED, {
            "comment_id": comment_id, "node_id": 1, "node_generation": 0,
            "base_version": 1, "version": 2, "text": "wrong node",
            "actor_kind": "local_operator",
        }),
        _event(3, EV_COMMENT_EDITED, {
            "comment_id": comment_id, "node_id": 0, "node_generation": 0,
            "base_version": 7, "version": 8, "text": "out of order",
            "actor_kind": "local_operator",
        }),
        _event(4, EV_COMMENT_EDITED, {
            "comment_id": comment_id, "node_id": 0, "node_generation": 0,
            "base_version": 1, "version": 2, "text": "bad actor", "actor_kind": "human",
        }),
        _event(5, EV_COMMENT_EDITED, {
            "comment_id": comment_id, "node_id": 0, "node_generation": 0,
            "base_version": 1, "version": 2, "text": "valid v2",
            "actor_kind": "local_operator",
        }),
        _event(6, EV_COMMENT_RESOLUTION_CHANGED, {
            "comment_id": comment_id, "node_id": 0, "node_generation": 0,
            "base_version": 1, "version": 2, "resolved": True,
            "actor_kind": "local_operator",
        }),
    ]
    projected, audit = project_comments(malformed, include_history=True)
    assert projected[comment_id].version == 2 and projected[comment_id].text == "valid v2"
    assert projected[comment_id].resolved is False
    assert [row["action"] for row in audit[comment_id]] == ["created", "edited"]
    state = fold(malformed)
    assert state.comments_revision == 5
    assert state.comments[comment_id].text == "valid v2"


def test_cursor_helpers_reject_missing_anchors_and_invalid_limits():
    comment_id = "cmt_" + "b" * 32
    projected, history = project_comments([
        _event(1, EV_COMMENT_CREATED, _create_data(comment_id)),
        _event(2, EV_COMMENT_EDITED, {
            "comment_id": comment_id, "node_id": 0, "node_generation": 0,
            "base_version": 1, "version": 2, "text": "v2",
            "actor_kind": "local_operator",
        }),
        _event(3, EV_COMMENT_EDITED, {
            "comment_id": comment_id, "node_id": 0, "node_generation": 0,
            "base_version": 2, "version": 3, "text": "v3",
            "actor_kind": "local_operator",
        }),
    ], include_history=True)
    generation = "c" * 64
    with pytest.raises(CommentCursorError):
        comments_page(projected, generation=generation, limit=0)
    with pytest.raises(CommentCursorError):
        comments_page(projected, generation=generation, limit=1, node_id=0)
    first = history_page(comment_id, history[comment_id], generation=generation, limit=2)
    forged = first["next_cursor"].rsplit(".", 1)[0] + ".7fffffff"
    with pytest.raises(CommentCursorError) as exc:
        history_page(
            comment_id, history[comment_id], generation=generation, limit=2, cursor=forged)
    assert exc.value.stale is True


def test_control_compatibility_route_rejects_versioned_comments(tmp_path):
    _seed(tmp_path)
    client = TestClient(make_app(tmp_path))
    before = client.get("/api/runs/demo/state").json()["seq"]
    response = client.post("/api/runs/demo/control", json={
        "type": EV_COMMENT_CREATED,
        "data": {"node_id": 0, "node_generation": 0, "text": "bypass"},
    })
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "command_protocol_required"
    assert client.get("/api/runs/demo/state").json()["seq"] == before


def test_destructive_guard_still_fails_closed_for_active_comment_record(tmp_path):
    rd = _seed(tmp_path)
    app = make_app(tmp_path)
    service = app.state.looplab.commands
    directory = rd / ".commands"
    directory.mkdir()
    command_id = "cmd_" + "d" * 32
    (directory / f"{command_id}.json").write_text(json.dumps({
        "id": command_id,
        "status": "accepted",
        "event_type": EV_COMMENT_CREATED,
    }), encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        with service.destructive_guard(rd, "reset run"):
            pass
    assert exc.value.status_code == 409
    assert command_id in str(exc.value.detail)


def test_edit_by_a_different_actor_preserves_creator_authorship():
    """R7: an edit by a DIFFERENT actor must not rewrite the comment's authorship. The top-level
    actor_kind stays the CREATOR; each history row still records who performed that specific action."""
    cid = "cmt_" + "b" * 32
    events = [
        _event(0, EV_COMMENT_CREATED, _create_data(cid, text="v1", actor="deployment_owner")),
        _event(1, EV_COMMENT_EDITED, {
            "comment_id": cid, "node_id": 0, "node_generation": 0,
            "base_version": 1, "version": 2, "text": "v2", "actor_kind": "local_operator"}),
    ]
    comments, history = project_comments(events, include_history=True)
    assert comments[cid].actor_kind == "deployment_owner"      # authorship = creator, not last editor
    rows = history[cid]
    assert rows[0]["action"] == "created" and rows[0]["actor_kind"] == "deployment_owner"
    assert rows[1]["action"] == "edited" and rows[1]["actor_kind"] == "local_operator"


def test_legacy_annotations_do_not_consume_modern_comment_budget():
    """R7: legacy EV_ANNOTATION notes (uncompactable in an append-only log) must not count against the
    MODERN-comment run cap; otherwise a heavily-annotated run permanently 409s all new attributed
    comments. Validation (run_commands) and fold (comment_projection) both count only modern comments."""
    comments = {}
    for seq in range(COMMENT_MAX_PER_RUN):
        apply_comment_event(comments, _event(seq, EV_ANNOTATION, {"node_id": 0, "text": f"note {seq}"}))
    assert sum(1 for c in comments.values() if c.legacy) == COMMENT_MAX_PER_RUN   # run full of legacy
    # a MODERN comment still folds despite the run being "full" of legacy notes
    cid = "cmt_" + "c" * 32
    row = apply_comment_event(comments, _event(
        COMMENT_MAX_PER_RUN, EV_COMMENT_CREATED, _create_data(cid, node=1, text="modern")))
    assert row is not None and cid in comments and not comments[cid].legacy


def test_legacy_annotations_do_not_block_modern_comment_end_to_end(tmp_path):
    """R8: the APPEND-TIME precondition (_collaboration_precondition) — not just intake + fold — must count
    only MODERN comments. Otherwise a run full of legacy notes accepts a comment at intake, then drops
    it at the append recheck (comment_run_limit_reached) and never appends the domain event. This drives
    the full command path (submit -> precondition -> append), which the fold-level test does not."""
    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    for i in range(COMMENT_MAX_PER_RUN):
        store.append(EV_ANNOTATION, {"node_id": 0, "text": f"legacy {i}"})
    client = TestClient(make_app(tmp_path))
    created = _command(client, EV_COMMENT_CREATED, {
        "node_id": 0, "node_generation": 0, "text": "modern despite 500 legacy notes",
    }, "comment-create-past-legacy", generation=_generation(client))
    assert created.status_code == 200 and created.json()["status"] == "succeeded", created.text
    modern = [e for e in store.read_all() if e.type == EV_COMMENT_CREATED]
    assert len(modern) == 1 and modern[0].data["text"] == "modern despite 500 legacy notes"
