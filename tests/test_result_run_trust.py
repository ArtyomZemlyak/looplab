"""Run and selected-node briefs retain the same attempt-bound advisory evidence."""
import json
from pathlib import Path

import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI
from test_result_notices import _body, _read, _run
from test_result_notice_comparison import _pair, KEY

FIXTURE = json.loads((Path(__file__).parent / "fixtures/trust_notice_cases_v1.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_selected_warning_agrees_across_run_and_node_notices_and_cached_summary(tmp_path, monkeypatch, case):
    rd, store, client, gen = _run(tmp_path, monkeypatch)
    _pair(store)
    store.append("trust_gate_changed", {"trust_gate": case["mode"]})
    store.append("reward_hack_suspected", {"node_id": case["target"], "generation": 0,
                 "signals": [{"signal": case["signal"]}]})
    if case["reset"]:
        store.append("node_reset", {"node_id": 1})
        store.append("node_evaluated", {"node_id": 1, "generation": 1, "metric": .5,
                     "metric_provenance": {"comparability": KEY}})
    store.append("node_confirmed", {"node_id": 1, "generation": 1 if case["reset"] else 0,
                 "mean": .4, "std": .01, "seeds": 3})
    store.append("run_finished", {"reason": "done"})
    monkeypatch.setattr("looplab.serve.result_notices._engine_liveness", lambda rd: False)
    before = (rd / "events.jsonl").read_bytes()
    page = _read(client, gen).json()
    run = page["items"][-1]
    expected = FIXTURE["run_expected"][case["name"]]
    assert {key: run[key] for key in expected} == expected
    selected = next(r for r in page["items"] if r.get("node_id") == run["selected_node"])
    assert run["trust_advisory"] is selected["trust_advisory"]
    assert (run["score"], run["confirmed_mean"], run["attempt"]) == (
        selected["score"], selected["confirmed_mean"], selected["attempt"])
    response = client.get("/api/runs", headers={"X-LoopLab-Token": "owner-secret"})
    assert response.status_code == 200, response.text
    summary = next(row for row in response.json() if row["run_id"] == "demo")
    chosen = summary["result_summary"]["selected"]
    assert (chosen["node_id"], chosen["attempt"], chosen["trust_advisory"]) == (
        run["selected_node"], run["attempt"], run["trust_advisory"])
    assert chosen["value"] == (run["confirmed_mean"] if run["confirmed_mean"] is not None else run["score"])
    assert summary["best_metric_caveats"] == run["caveats"]
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda req: httpx.Response(200, json=page)))
    assert api.result_notices("demo", gen) == {"status": 200, "body": page}
    assert (rd / "events.jsonl").read_bytes() == before


def test_finished_run_commentary_is_withdrawn_when_selected_advisory_changes(tmp_path, monkeypatch):
    _, store, client, gen = _run(tmp_path, monkeypatch)
    _pair(store)
    store.append("run_finished", {"reason": "done"})
    monkeypatch.setattr("looplab.serve.result_notices._engine_liveness", lambda rd: False)
    page = _read(client, gen, limit=1).json()
    row = page["items"][0]
    body, headers = _body(row, gen), {"X-LoopLab-Token": "agent-secret"}
    assert client.post("/api/runs/demo/result-notices", json=body, headers=headers).status_code == 200
    store.append("reward_hack_suspected", {"node_id": 1, "generation": 0,
                 "signals": [{"signal": "perfect_metric"}]})
    fresh = _read(client, gen, limit=1).json()["items"][0]
    assert fresh["selected_node"] == row["selected_node"] and fresh["score"] == row["score"]
    assert fresh["trust_advisory"] is True and fresh["commentary"] is None
    assert fresh["evidence_token"] != row["evidence_token"]
    assert _read(client, gen, cursor=page["next_cursor"]).status_code == 409
    assert client.post("/api/runs/demo/result-notices", json=body, headers=headers).json() == {"ok": True, "replayed": True}
    assert client.post("/api/runs/demo/result-notices", json={**body, "action_id": "fresh"}, headers=headers).status_code == 409


def test_no_selected_result_has_no_advisory_or_measured_claim(tmp_path, monkeypatch):
    _, store, client, gen = _run(tmp_path, monkeypatch)
    _pair(store)
    store.append("reward_hack_suspected", {"node_id": 1, "generation": 0,
                 "signals": [{"signal": "perfect_metric"}]})
    for nid in (0, 1):
        store.append("node_abort", {"node_id": nid, "generation": 0})
    store.append("run_finished", {"reason": "done"})
    monkeypatch.setattr("looplab.serve.result_notices._engine_liveness", lambda rd: False)
    page = _read(client, gen).json()
    run = page["items"][-1]
    assert run["selected_node"] is None and run["attempt"] is None
    assert run["trust_advisory"] is False and run["score"] is None and run["confirmed_mean"] is None
    assert run["caveats"] == []
    summary = client.get("/api/runs", headers={"X-LoopLab-Token": "owner-secret"}).json()[0]
    assert summary["result_summary"] is None
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda req: httpx.Response(200, json=page)))
    assert api.result_notices("demo", gen) == {"status": 200, "body": page}
