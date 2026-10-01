"""Reconnect can drain older completion receipts without unsafe offset shifts."""
import pytest

from tests.test_result_notices import _run, _read, _body
from looplab.events.eventstore import EventStore


def _terminal(store, nid):
    if nid:
        store.append("node_created", {"node_id": nid, "operator": "draft", "idea": {"operator": "draft"}})
    store.append("node_evaluated", {"node_id": nid, "metric": float(nid + 1), "metric_provenance": {}})


def test_all_receipts_beyond_the_maximum_page_are_reachable_without_work(tmp_path, monkeypatch):
    rd, store, client, gen = _run(tmp_path, monkeypatch)
    for nid in range(205):
        _terminal(store, nid)
    service = client.app.state.looplab.commands
    monkeypatch.setattr(service, "sequence", lambda *a, **k: pytest.fail("read tried to sequence work"))
    monkeypatch.setattr(service, "_start_worker", lambda *a, **k: pytest.fail("read started worker"))
    before = (rd / "events.jsonl").read_bytes()
    seen, cursor = [], None
    while True:
        result = _read(client, gen, limit=31, **({"cursor": cursor} if cursor else {}))
        assert result.status_code == 200, result.text
        page = result.json()
        assert page["total"] == 205 and len(page["items"]) <= 31
        seen.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        assert page["has_more"] == (cursor is not None)
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 205
    assert set(seen) == {f"node:{nid}:0" for nid in range(205)}
    assert (rd / "events.jsonl").read_bytes() == before and not (rd / "result_commentary.jsonl").exists()


def test_cursor_survives_new_completion_commentary_and_ui_restart(tmp_path, monkeypatch):
    _, store, client, gen = _run(tmp_path, monkeypatch)
    for nid in range(3):
        _terminal(store, nid)
    head = _read(client, gen, limit=1).json()
    row = head["items"][0]
    result = client.post("/api/runs/demo/result-notices", json=_body(row, gen), headers={"X-LoopLab-Token": "agent-secret"})
    assert result.status_code == 200
    _terminal(store, 3)
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    restarted = TestClient(make_app(tmp_path))
    page = _read(restarted, gen, limit=2, cursor=head["next_cursor"])
    assert page.status_code == 200
    assert [r["node_id"] for r in page.json()["items"]] == [0, 1]
    assert page.json()["next_cursor"] is None and not page.json()["has_more"]
    assert _read(restarted, gen, limit=1).json()["items"][0]["node_id"] == 3


@pytest.mark.parametrize("change", ["reset", "confirmation", "provenance"])
def test_changed_anchor_refuses_cursor_with_refresh_advice(tmp_path, monkeypatch, change):
    _, store, client, gen = _run(tmp_path, monkeypatch)
    for nid in range(2):
        _terminal(store, nid)
    cursor = _read(client, gen, limit=1).json()["next_cursor"]
    if change == "reset":
        store.append("node_reset", {"node_id": 1})
    elif change == "confirmation":
        store.append("node_confirmed", {"node_id": 1, "mean": 0.1, "std": 0.01, "seeds": 2})
    else:
        store.append("applied_params_backfilled", {"node_id": 1, "generation": 0, "unrecoverable": "gone"})
    result = _read(client, gen, cursor=cursor)
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "result_notice_cursor_changed"
    assert result.json()["detail"]["remediation"]


def test_cursor_is_bound_to_run_directory_and_generation(tmp_path, monkeypatch):
    rd, store, client, gen = _run(tmp_path, monkeypatch)
    for nid in range(2):
        _terminal(store, nid)
    cursor = _read(client, gen, limit=1).json()["next_cursor"]
    other = tmp_path / "copy"
    other.mkdir()
    for name in ("events.jsonl", "config.snapshot.json", "task.snapshot.json"):
        (other / name).write_bytes((rd / name).read_bytes())
    result = client.get("/api/runs/copy/result-notices", params={"expected_generation": gen, "cursor": cursor},
                        headers={"X-LoopLab-Token": "agent-secret"})
    assert result.status_code == 409 and result.json()["detail"]["code"] == "result_notice_cursor_changed"
    store.path.write_bytes(b"")
    fresh = EventStore(rd / "events.jsonl")
    fresh.append("run_started", {"run_id": "demo", "run_uid": "replacement", "task_id": "task", "goal": "g", "direction": "min"})
    from looplab.events.run_generation import run_generation_token
    new_gen = run_generation_token(fresh.read_all())
    assert _read(client, new_gen, cursor=cursor).status_code == 409


@pytest.mark.parametrize("cursor", ["bad", "rn1." + "x" * 200, "rn1." + "a" * 64 + ".node:-1:0." + "a" * 64])
def test_malformed_cursor_is_refused_not_treated_as_the_latest_page(tmp_path, monkeypatch, cursor):
    _, store, client, gen = _run(tmp_path, monkeypatch)
    _terminal(store, 0)
    result = _read(client, gen, cursor=cursor)
    assert result.status_code == 400
    assert result.json()["detail"]["code"] == "result_notice_cursor_invalid"
