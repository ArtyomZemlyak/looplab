"""Semantic harness reads and acknowledgements cannot trust a damaged event prefix."""
import json

import pytest

from looplab.core.models import hypothesis_id
from looplab.events.run_generation import run_generation_token
from tests.test_external_progress import _run
from tests.test_external_selection import _session
from tests.test_harness_journal_retry import _body


def _action(tmp_path, kind):
    if kind in {"verify", "values", "selection"}:
        rd, store, client, gen = _session(tmp_path, select_verifier=True,
                                         policy="mcts", mcts_value_weight=.4)
        endpoint = "/api/runs/demo/harness-selection"
        observed = client.get(endpoint, params={"expected_generation": gen}).json()
        if kind == "selection":
            return rd, store, client, gen, endpoint, None
        if kind == "verify":
            body = {"expected_generation": gen, "action_id": "original",
                "members": [{"node_id": n["node_id"], "generation": n["generation"],
                    "evidence_digest": n["evidence_digest"], "samples": [True] * 3}
                    for n in observed["tie_groups"][0]]}
        else:
            body = {"expected_generation": gen, "action_id": "original",
                "expected_evidence_revision": observed["evidence_revision"],
                "estimates": [{"node_id": n["node_id"], "generation": n["generation"],
                    "value": .5, "rationale": "Protocol fixture belief"}
                    for n in observed["value_candidates"]]}
        return rd, store, client, gen, endpoint + "/" + kind, body
    rd, store, client = _run(tmp_path)
    gen = run_generation_token(store.read_all())
    if kind in {"decisions", "reviews"}:
        return rd, store, client, gen, "/api/runs/demo/harness-" + kind, _body(kind, gen)
    config = json.loads((rd / "config.snapshot.json").read_text())
    config["track_hypotheses"] = True
    (rd / "config.snapshot.json").write_text(json.dumps(config))
    for statement in ("Regularize", "Warmup", "Narrow model", "Longer training"):
        store.append("hypothesis_added", {"statement": statement, "id": hypothesis_id(statement)})
    endpoint = "/api/runs/demo/harness-hypotheses"
    board = client.get(endpoint, params={"expected_generation": gen}).json()
    body = None if kind == "board" else {"expected_generation": gen, "action_id": "original",
        "expected_board_sha256": board["board_sha256"], "decision": "no_merge",
        "reason": "Four distinct fixture questions"}
    return rd, store, client, gen, endpoint, body


@pytest.mark.parametrize("kind", ["decisions", "reviews", "hypotheses", "verify", "values", "board", "selection"])
@pytest.mark.parametrize("damage", [b'broken event\n', b'[]\n'])
def test_damaged_event_source_refuses_semantics_and_replay_without_changes(tmp_path, kind, damage):
    rd, store, client, gen, endpoint, body = _action(tmp_path, kind)
    if body is not None:
        accepted = client.post(endpoint, json=body)
        assert accepted.status_code == 200, accepted.text
    path = rd / "events.jsonl"
    original = path.read_bytes()
    path.write_bytes(original + damage)
    before = {p.name: p.read_bytes() for p in rd.glob("harness_*.jsonl")}
    progress = client.get("/api/runs/demo/harness-progress", params={"expected_generation": gen})
    assert progress.status_code == 200 and not progress.json()["complete"], progress.text
    assert progress.json()["next_step"]["code"] == "inspect_sources"
    payloads = [None] if body is None else [body, {**body, "action_id": "fresh"}]
    for payload in payloads:
        reply = (client.get(endpoint, params={"expected_generation": gen}) if payload is None else
                 client.post(endpoint, json=payload))
        assert reply.status_code == 503, reply.text
        assert reply.json()["detail"]["code"] == "harness_history_incomplete"
        assert reply.json()["detail"]["source"] == "events.jsonl"
        assert path.read_bytes() == original + damage
        assert before == {p.name: p.read_bytes() for p in rd.glob("harness_*.jsonl")}
    # Only the fixture restores its own known-good source, not the harness.
    path.write_bytes(original)
    recovered = (client.get(endpoint, params={"expected_generation": gen}) if body is None else
                 client.post(endpoint, json=body))
    assert recovered.status_code == 200, recovered.text
    if body is not None:
        assert recovered.json()["replayed"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", ["decisions", "reviews", "hypotheses", "verify", "values", "board", "selection"])
def test_healthy_source_keeps_generation_fence(tmp_path, kind):
    _, _, client, _, endpoint, body = _action(tmp_path, kind)
    reply = (client.get(endpoint, params={"expected_generation": "e" * 64}) if body is None else
             client.post(endpoint, json={**body, "expected_generation": "e" * 64}))
    assert reply.status_code == 409, reply.text


@pytest.mark.parametrize("present", [False, True])
def test_absent_or_empty_event_source_cannot_prove_identity(tmp_path, present):
    from fastapi import HTTPException
    from looplab.harness.journals import read_event_source

    if present:
        (tmp_path / "events.jsonl").write_bytes(b'')
    with pytest.raises(HTTPException) as failure:
        read_event_source(tmp_path)
    assert failure.value.status_code == 503
    assert failure.value.detail == {"code": "harness_history_unavailable", "source": "events.jsonl"}


@pytest.mark.parametrize("damaged", [True, False])
def test_event_source_detects_damage_during_read_but_accepts_valid_append(tmp_path, damaged):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from looplab.harness.journals import read_event_source

    rd, store, _, _, _, _ = _action(tmp_path, "decisions")
    path = rd / "events.jsonl"
    before = path.read_bytes()
    def concurrent_read():
        observed = store.read_all()
        if damaged:
            path.write_bytes(before + b'broken event\n')
        else:
            store.append("research_completed", {"memo": {"summary": "Fixture note"}})
        return observed
    if damaged:
        with pytest.raises(HTTPException) as failure:
            read_event_source(rd, store=SimpleNamespace(read_all=concurrent_read))
        assert failure.value.status_code == 503
        assert failure.value.detail["source_health"]["complete"] is False
        assert path.read_bytes() == before + b'broken event\n'
    else:
        observed = read_event_source(rd, store=SimpleNamespace(read_all=concurrent_read))
        assert len(observed) + 1 == len(store.read_all())


def test_unreadable_event_source_is_not_an_empty_or_healthy_history(tmp_path, monkeypatch):
    from pathlib import Path
    from fastapi import HTTPException
    from looplab.harness.journals import read_event_source

    rd, _, _, _, _, _ = _action(tmp_path, "decisions")
    path = rd / "events.jsonl"
    original = Path.read_bytes
    before = original(path)
    def unavailable(member):
        if member == path:
            raise PermissionError("Fixture event source is unavailable")
        return original(member)
    monkeypatch.setattr(Path, "read_bytes", unavailable)
    with pytest.raises(HTTPException) as failure:
        read_event_source(rd)
    assert failure.value.status_code == 503
    assert failure.value.detail["code"] == "harness_history_unavailable"
    assert failure.value.detail["source_health"]["unreadable"] is True
    assert original(path) == before


def test_normal_torn_tail_keeps_existing_eventstore_prefix_semantics(tmp_path):
    from looplab.harness.journals import read_event_source

    rd, store, _, _, _, _ = _action(tmp_path, "decisions")
    path = rd / "events.jsonl"
    before = path.read_bytes()
    observed = store.read_all()
    path.write_bytes(before + b'{"v":1,"seq":')
    assert read_event_source(rd) == observed
    assert path.read_bytes() == before + b'{"v":1,"seq":'
