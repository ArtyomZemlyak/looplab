"""Primary scores and repeat spread survive every result projection without changing selection."""
import json
from pathlib import Path

import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI
from test_result_notices import _read, _run
from test_result_notice_comparison import _pair, _base, KEY

CASES = json.loads((Path(__file__).parent / "fixtures/repeat_result_cases_v1.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_repeat_numbers_agree_across_cached_summary_node_run_and_mcp(tmp_path, monkeypatch, case):
    rd, store, client, gen = _run(tmp_path, monkeypatch)
    parent, child = {}, {"metric": case["score"]}
    if case["base_different"]:
        parent["metric_provenance"] = {"comparability": KEY, "base_revision": _base(0)}
        child["metric_provenance"] = {"comparability": KEY, "base_revision": _base(1, "c" * 64)}
    if case["retarget"]:
        parent.update(extra_metrics={"latency": 2.0}, extra_metrics_provenance={"latency": "declared"})
        child.update(extra_metrics={"latency": 1.2}, extra_metrics_provenance={"latency": "declared"})
    _pair(store, parent, child)
    if case["reset"]:
        store.append("node_reset", {"node_id": 1})
        store.append("node_evaluated", {"node_id": 1, "generation": 1, "metric": case["score"],
                     "metric_provenance": {"comparability": KEY}})
    if case["retarget"]:
        store.append("metric_retarget", {"key": "latency"})
    store.append("node_confirmed", {"node_id": 1, "generation": 0, "mean": .8,
                 "std": case["std"], "seeds": case["seeds"],
                 **({"objective_key": "latency"} if case["retarget"] else {})})
    store.append("run_finished", {"reason": "done"})
    monkeypatch.setattr("looplab.serve.result_notices._engine_liveness", lambda rd: False)
    before = (rd / "events.jsonl").read_bytes()
    page = _read(client, gen).json()
    node = next(row for row in page["items"] if row.get("node_id") == 1)
    run = page["items"][-1]
    assert node["score_comparison"]["status"] == case["comparison"]
    assert node["score"] == case["score"] and node["parents"][0]["score"] == case["parent_score"]
    assert node["confirmed_mean"] == (.8 if case["confirmed"] else None)
    assert node["confirmed_std"] == (case["std"] if case["confirmed"] else None)
    assert run["selected_node"] == case["selected_node"]
    response = client.get("/api/runs", headers={"X-LoopLab-Token": "owner-secret"})
    assert response.status_code == 200, response.text
    summary = response.json()[0]["result_summary"]
    for field in ("first", "selected"):
        receipt = summary[field]
        measured = next(r for r in page["items"] if r.get("node_id") == receipt["node_id"])
        assert receipt["score"] == measured["score"]
        assert receipt["value"] == (measured["confirmed_mean"] if measured["confirmed_mean"] is not None else measured["score"])
        assert receipt["confirmed_std"] == measured["confirmed_std"]
    selected = summary["selected"]
    assert (run["score"], run["confirmed_std"]) == (selected["score"], selected["confirmed_std"])
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda req: httpx.Response(200, json=page)))
    assert api.result_notices("demo", gen) == {"status": 200, "body": page}
    assert (rd / "events.jsonl").read_bytes() == before
