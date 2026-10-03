"""Node completion briefs carry advisory evidence without changing selection policy."""
import json
from pathlib import Path

import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI
from test_result_notices import _body, _read, _run
from test_result_notice_comparison import _pair, KEY

CASES = json.loads((Path(__file__).parent / "fixtures/trust_notice_cases_v1.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_http_and_mcp_separate_advisory_signals_from_exclusion(tmp_path, monkeypatch, case):
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
    before = (rd / "events.jsonl").read_bytes()
    page = _read(client, gen).json()
    row = next(r for r in page["items"] if r["node_id"] == 1)
    for field in ("trust_flagged", "trust_advisory", "parent_trust_advisory"):
        assert row[field] is case[field]
    assert row["score_comparison"]["status"] == case["comparison"]
    assert row["score"] == .5 and row["parents"][0]["score"] == 1.0
    assert row["confirmed_mean"] == .4 and row["confirmed_seeds"] == 3
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda req: httpx.Response(200, json=page)))
    assert api.result_notices("demo", gen) == {"status": 200, "body": page}
    assert (rd / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize("target", [0, 1])
def test_advisory_change_withdraws_interpretation_and_cursor_without_retraining(tmp_path, monkeypatch, target):
    _, store, client, gen = _run(tmp_path, monkeypatch)
    _pair(store)
    page = _read(client, gen, limit=1).json()
    row = page["items"][0]
    body, headers = _body(row, gen), {"X-LoopLab-Token": "agent-secret"}
    assert client.post("/api/runs/demo/result-notices", json=body, headers=headers).status_code == 200
    for detail in ("first warning", "new evidence for the same warning"):
        store.append("reward_hack_suspected", {"node_id": target, "generation": 0,
                     "signals": [{"signal": "perfect_metric", "detail": detail}]})
        fresh_page = _read(client, gen, limit=1).json()
        fresh = fresh_page["items"][0]
        assert fresh["score_comparison"]["status"] == "same"
        assert fresh["trust_advisory"] is (target == 1)
        assert fresh["parent_trust_advisory"] is (target == 0)
        assert fresh["score"] == row["score"] and fresh["evidence_token"] != row["evidence_token"]
        assert fresh["commentary"] is None
        assert _read(client, gen, cursor=page["next_cursor"]).status_code == 409
        assert client.post("/api/runs/demo/result-notices", json=body, headers=headers).json() == {"ok": True, "replayed": True}
        assert client.post("/api/runs/demo/result-notices", json={**body, "action_id": "fresh"}, headers=headers).status_code == 409
        page, row = fresh_page, fresh


def test_same_warning_text_with_changed_audit_identity_is_new_evidence(tmp_path, monkeypatch):
    rd, store, client, gen = _run(tmp_path, monkeypatch)
    _pair(store)
    store.append("reward_hack_suspected", {"node_id": 1, "generation": 0,
                 "signals": [{"signal": "perfect_metric", "detail": "same warning"}]})
    row = _read(client, gen).json()["items"][-1]
    body, headers = _body(row, gen), {"X-LoopLab-Token": "agent-secret"}
    assert client.post("/api/runs/demo/result-notices", json=body, headers=headers).status_code == 200
    # A synthetic source replacement changes only the audit identity, not the
    # number of records, warning text, outcome or run generation.
    events = store.read_all()
    events[-1].data.update(evidence_version=2, code_digest="c" * 64)
    (rd / "events.jsonl").write_text("".join(e.model_dump_json() + "\n" for e in events), encoding="utf-8")
    fresh = _read(client, gen).json()["items"][-1]
    assert fresh["score"] == row["score"] and fresh["trust_advisory"] is True
    assert fresh["evidence_token"] != row["evidence_token"] and fresh["commentary"] is None
    assert client.post("/api/runs/demo/result-notices", json={**body, "action_id": "fresh"}, headers=headers).status_code == 409
