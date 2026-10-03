"""Pin the UI's lifecycle fixtures to real folded HTTP state and completion receipts."""
import json
from pathlib import Path

import pytest

from looplab.events.replay import fold, hard_flagged_ids
from test_result_notices import _run, _read
from test_result_notice_comparison import _pair

CASES = json.loads((Path(__file__).parent / "fixtures/trust_attempt_cases_v1.json").read_text())


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["name"])
def test_trust_history_does_not_flag_a_replacement_attempt(tmp_path, monkeypatch, case):
    rd, store, client, gen = _run(tmp_path, monkeypatch)
    _pair(store)
    for record in case["records"]:
        if record["generation"] == 0:
            store.append("reward_hack_suspected", record)
    store.append("node_reset", {"node_id": 1})
    store.append("node_evaluated", {"node_id": 1, "generation": 1, "metric": .25,
                 "metric_provenance": {"comparability": {"keys": {"measured": "a" * 16}}}})
    store.append("node_confirmed", {"node_id": 1, "generation": 1, "mean": .2, "std": .01, "seeds": 3})
    for record in case["records"]:
        if record["generation"] == 1:
            store.append("reward_hack_suspected", record)
    store.append("run_finished", {"reason": "done"})
    monkeypatch.setattr("looplab.serve.result_notices._engine_liveness", lambda rd: False)
    before = (rd / "events.jsonl").read_bytes()
    response = client.get("/api/runs/demo/state", headers={"X-LoopLab-Token": "owner-secret"})
    assert response.status_code == 200, response.text
    state = response.json()["state"]
    for nid, expected in CASES["nodes"].items():
        assert {key: state["nodes"][nid][key] for key in expected} == expected
    assert [{key: record[key] for key in ("node_id", "generation", "signals")}
            for record in state["reward_hacks"]] == case["records"]
    assert len(hard_flagged_ids(fold(store.read_all()))) == case["current_nodes"]
    page = _read(client, gen).json()
    run = next(row for row in page["items"] if row["kind"] == "run")
    assert ("trust_flagged" in run["caveats"]) == bool(case["current_nodes"])
    assert run["selected_node"] == 1 and run["attempt"] == 1
    assert run["score"] == .25 and run["confirmed_mean"] == .2
    assert (rd / "events.jsonl").read_bytes() == before
