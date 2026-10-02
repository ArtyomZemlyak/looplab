"""A damaged receipt source cannot prove absence, replay identity or approval."""
import json

import pytest
from fastapi import HTTPException

from looplab.core.config import read_config_snapshot
from looplab.core.models import Idea
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from tests.test_external_progress import _run
from tests.test_harness_journal_retry import _body


@pytest.mark.parametrize("kind", ["reviews", "decisions"])
@pytest.mark.parametrize("damage", [b'not json\n', b'[]\n', b'\xff\n', b'{}\n', b'{"partial":', "bool_stamp"])
def test_incomplete_journal_refuses_exact_and_fresh_writes_but_keeps_diagnostics(tmp_path, kind, damage):
    rd, store, client = _run(tmp_path)
    gen = run_generation_token(store.read_all())
    body = _body(kind, gen)
    endpoint = "/api/runs/demo/harness-" + kind
    saved = client.post(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    path = rd / f"harness_{kind}.jsonl"
    original = path.read_bytes()
    if damage == "bool_stamp":
        row = json.loads(original)
        damage = (json.dumps({**row, "action_id": "damaged", "at_node": True}) + "\n").encode()
    path.write_bytes(original + damage)
    before = path.read_bytes(), (rd / "events.jsonl").read_bytes()
    progress = client.get("/api/runs/demo/harness-progress", params={"expected_generation": gen})
    assert progress.status_code == 200, progress.text
    assert not progress.json()["complete"]
    assert progress.json()["next_step"]["code"] == "inspect_sources"
    assert progress.json()["history"][kind]["total"] == 1
    for payload in (body, {**body, "action_id": "fresh"}):
        refused = client.post(endpoint, json=payload)
        assert refused.status_code == 503, refused.text
        assert refused.json()["detail"]["code"] == "harness_history_incomplete"
        assert refused.json()["detail"]["source"] == path.name
        assert before == (path.read_bytes(), (rd / "events.jsonl").read_bytes())
    # Only the fixture/operator restores its own known-good bytes. No API repairs.
    path.write_bytes(original)
    assert client.post(endpoint, json=body).json()["replayed"]
    assert not client.post(endpoint, json={**body, "action_id": "fresh"}).json()["replayed"]


@pytest.mark.parametrize("kind", ["reviews", "decisions"])
def test_current_receipts_in_damaged_journal_cannot_satisfy_obligations(tmp_path, kind):
    from looplab.harness.decisions import missing_decisions
    from looplab.harness.reviews import missing_reviews

    rd, store, client = _run(tmp_path)
    gen = run_generation_token(store.read_all())
    body = _body(kind, gen)
    assert client.post("/api/runs/demo/harness-" + kind, json=body).status_code == 200
    path = rd / f"harness_{kind}.jsonl"
    path.write_bytes(path.read_bytes() + b'broken\n')
    before = path.read_bytes(), (rd / "events.jsonl").read_bytes()
    settings = read_config_snapshot(rd / "config.snapshot.json")
    state = fold(store.read_all())
    with pytest.raises(HTTPException) as failure:
        if kind == "decisions":
            missing_decisions(rd, settings, state, Idea(operator="draft"), gen)
        else:
            missing_reviews(rd, settings, state, gen)
    assert failure.value.status_code == 503
    assert failure.value.detail["code"] == "harness_history_incomplete"
    assert before == (path.read_bytes(), (rd / "events.jsonl").read_bytes())


def test_damaged_review_finish_refuses_http_and_cli_and_budget_pauses(tmp_path):
    from typer import BadParameter
    from looplab.cli.run_cmds import finalize
    from looplab.core.config import Settings
    from tests.factories import make_engine

    rd, store, client = _run(tmp_path)
    gen = run_generation_token(store.read_all())
    path = rd / "harness_reviews.jsonl"
    path.write_bytes(b'broken\n')
    before = path.read_bytes(), (rd / "events.jsonl").read_bytes()
    refused = client.post("/api/runs/demo/commands", headers={"Idempotency-Key": "finish"},
        json={"expected_generation": gen, "type": "run_abort", "data": {"reason": "finish"}})
    assert refused.status_code == 200, refused.text
    assert refused.json()["status"] == "rejected"
    assert refused.json()["error"]["code"] == "harness_history_incomplete"
    with pytest.raises(BadParameter, match="harness_history_incomplete"):
        finalize(rd, task_file=rd / "task.snapshot.json")
    assert before == (path.read_bytes(), (rd / "events.jsonl").read_bytes())
    engine = make_engine(rd, external_harness=True)
    # make_engine does not replace this run's snapshots or event store.
    settings = read_config_snapshot(rd / "config.snapshot.json")
    assert isinstance(settings, Settings) and settings.reflection_priors
    events = store.read_all()
    assert engine._settle_terminal_gate(fold(events), "eval_budget", decision_seq=events[-1].seq) == "break"
    last = store.read_all()[-1]
    assert last.type == "pause" and last.data["due"]["source_error"]["code"] == "harness_history_incomplete"
    assert not fold(store.read_all()).finished and path.read_bytes() == before[0]
