"""Shared Assistant parent links are backed by current, recorded lifecycle receipts."""
import json
from pathlib import Path

import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI
from test_result_notices import _read, _run
from test_result_notice_comparison import KEY, _base

CASES = json.loads((Path(__file__).parent / "fixtures/parent_result_cases_v1.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_current_parent_evidence_through_http_and_mcp(tmp_path, monkeypatch, case):
    rd, store, client, gen = _run(tmp_path, monkeypatch)
    name = case["name"]
    if name == "failed_parent":
        store.append("node_failed", {"node_id": 0, "error": "synthetic failure"})
    else:
        provenance = {"comparability": KEY}
        if name == "different_base":
            provenance["base_revision"] = _base(0)
        store.append("node_evaluated", {"node_id": 0, "metric": None if name == "missing_metric" else 1.0,
                     "metric_provenance": provenance})
    parents = [] if name == "root" else [0, 9] if "merge" in name else [0]
    if "merge" in name:
        store.append("node_created", {"node_id": 9, "operator": "draft", "idea": {"operator": "draft"}})
        store.append("node_evaluated", {"node_id": 9, "metric": 2.0, "metric_provenance": {"comparability": KEY}})
    store.append("node_created", {"node_id": 1, "parent_ids": parents, "operator": "improve",
                 "idea": {"operator": "improve"}, "code": "print(2)"})
    provenance = {"comparability": KEY}
    if name == "different_base":
        provenance["base_revision"] = _base(1, "c" * 64)
    if name == "different_conditions":
        provenance["comparability"] = {**KEY, "keys": {"measured": "d" * 16}}
    store.append("node_evaluated", {"node_id": 1, "metric": .5, "metric_provenance": provenance})
    if name in {"reset_parent", "partial_merge"}:
        nid = 9 if name == "partial_merge" else 0
        store.append("node_reset", {"node_id": nid})
        store.append("node_evaluated", {"node_id": nid, "generation": 1, "metric": .1})
    if name == "aborted_parent":
        store.append("node_abort", {"node_id": 0})
    before = (rd / "events.jsonl").read_bytes()
    response = _read(client, gen)
    assert response.status_code == 200, response.text
    page = response.json()
    row = next(row for row in page["items"] if row.get("node_id") == 1)
    assert row["score"] == .5
    assert row["parents"] == case["parents"]
    assert row["score_comparison"] == {"version": 1, "parent_count": len(parents), "status": case["status"]}
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda req: httpx.Response(200, json=page)))
    assert api.result_notices("demo", gen) == {"status": 200, "body": page}
    assert (rd / "events.jsonl").read_bytes() == before
