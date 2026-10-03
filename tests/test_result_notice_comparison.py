"""Completion briefs compare eligible primary scores on their recorded bases."""
import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI
from test_result_notices import _body, _read, _run

KEY = {"version": 1, "authority": "measured", "keys": {"measured": "a" * 16}}


def _base(nid, digest="b" * 64, **changes):
    return {"version": 1, "scope": "seeded_editables_before_mounts_and_overlay",
            "complete": True, "digest": digest, "file_count": 1, "bytes": 5,
            "node_id": nid, "generation": 0, "seed_event_seq": 1,
            "archive": {"version": 1, "status": "stored", "path": f"base_snapshots/{digest}"}, **changes}


def _pair(store, parent=None, child=None, parents=None):
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0,
                 "metric_provenance": {"comparability": KEY}, **(parent or {})})
    store.append("node_created", {"node_id": 1, "parent_ids": [0] if parents is None else parents,
                 "operator": "improve", "idea": {"operator": "improve"}, "code": "print(2)"})
    store.append("node_evaluated", {"node_id": 1, "metric": 0.5,
                 "metric_provenance": {"comparability": KEY}, **(child or {})})


@pytest.mark.parametrize("case,expected", [
    ("legacy", "same"), ("same_base", "same"), ("different_base", "base_different"),
    ("missing_base", "base_unknown"), ("partial_base", "base_unknown"),
    ("wrong_attempt", "base_unknown"), ("wrong_node", "base_unknown"),
    ("boolean_seq", "base_unknown"), ("missing_complete", "base_unknown"),
    ("unsafe_count", "base_unknown"),
    ("bad_archive", "base_unknown"), ("upstream_no_base", "base_unknown"),
    ("parent_constraints", "ineligible"), ("child_constraints", "ineligible"),
    ("parent_trust", "ineligible"), ("child_trust", "ineligible"),
    ("parent_salvaged", "ineligible"), ("child_salvaged", "ineligible"),
    ("parent_no_metric", "ineligible"), ("overflow", "ineligible"),
    ("conditions_unknown", "unknown"), ("conditions_different", "different"),
    ("retarget", "retargeted"), ("parent_reset", "parent_unavailable"),
    ("parent_abort", "parent_unavailable"), ("merge_partial", "multiple_parents"),
])
def test_http_completion_comparison_and_typed_read(tmp_path, monkeypatch, case, expected):
    rd, store, client, gen = _run(tmp_path, monkeypatch,
        run_fields={"upstream": {"version": 1}} if case == "upstream_no_base" else None)
    parent, child = {}, {}
    if case in {"same_base", "different_base", "missing_base", "partial_base", "wrong_attempt",
                "wrong_node", "boolean_seq", "missing_complete", "bad_archive", "unsafe_count"}:
        parent["metric_provenance"] = {"comparability": KEY, "base_revision": _base(0)}
        child["metric_provenance"] = {"comparability": KEY, "base_revision": _base(1)}
        receipt = child["metric_provenance"]["base_revision"]
        if case == "different_base":
            child["metric_provenance"]["base_revision"] = _base(1, "c" * 64)
        elif case == "missing_base":
            del child["metric_provenance"]["base_revision"]
        elif case == "partial_base":
            receipt["complete"] = False
        elif case == "wrong_attempt":
            receipt["generation"] = 1
        elif case == "wrong_node":
            receipt["node_id"] = 0
        elif case == "boolean_seq":
            receipt["seed_event_seq"] = True
        elif case == "unsafe_count":
            receipt["file_count"] = 2**53
        elif case == "missing_complete":
            del receipt["complete"]
        elif case == "bad_archive":
            receipt["archive"]["path"] = "elsewhere"
    if case.endswith("constraints"):
        (parent if case.startswith("parent") else child)["violations"] = [{"kind": "constraint"}]
    if case.endswith("salvaged"):
        (parent if case.startswith("parent") else child)["metric_provenance"] = {"comparability": KEY, "salvaged": True}
    if case == "parent_no_metric":
        parent["metric"] = None
    if case == "overflow":
        parent["metric"], child["metric"] = -1e308, 1e308
    if case == "conditions_unknown":
        child["metric_provenance"] = {}
    if case == "conditions_different":
        child["metric_provenance"] = {"comparability": {**KEY, "keys": {"measured": "d" * 16}}}
    if case == "retarget":
        parent.update(extra_metrics={"latency": 2.0}, extra_metrics_provenance={"latency": "declared"})
        child.update(extra_metrics={"latency": 1.0}, extra_metrics_provenance={"latency": "declared"})
    if case == "merge_partial":
        store.append("node_created", {"node_id": 9, "operator": "draft", "idea": {"operator": "draft"}})
        store.append("node_evaluated", {"node_id": 9, "metric": 2.0})
    _pair(store, parent, child, [0, 9] if case == "merge_partial" else None)
    if case == "merge_partial":
        store.append("node_reset", {"node_id": 9})
    if case.endswith("trust"):
        store.append("trust_gate_changed", {"trust_gate": "gate"})
        store.append("reward_hack_suspected", {"node_id": 0 if case.startswith("parent") else 1,
                     "generation": 0, "signals": [{"signal": "protected_missing"}]})
    if case == "retarget":
        store.append("metric_retarget", {"key": "latency"})
    if case == "parent_reset":
        store.append("node_reset", {"node_id": 0})
        store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.1})
    if case == "parent_abort":
        store.append("node_abort", {"node_id": 0})
    before = (rd / "events.jsonl").read_bytes()
    page = _read(client, gen).json()
    row = next(r for r in page["items"] if r["node_id"] == 1)
    assert row["score_comparison"] == {"version": 1, "parent_count": 2 if case == "merge_partial" else 1, "status": expected}
    if case == "merge_partial":
        assert len(row["parents"]) == 1
    if case == "retarget":
        assert row["score"] == 1.0 and row["parents"][0]["score"] == 2.0
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda req: httpx.Response(200, json=page)))
    assert api.result_notices("demo", gen) == {"status": 200, "body": page}
    assert (rd / "events.jsonl").read_bytes() == before


def test_parent_eligibility_withdraws_commentary_and_cursor_but_exact_retry_survives(tmp_path, monkeypatch):
    _, store, client, gen = _run(tmp_path, monkeypatch)
    _pair(store)
    page = _read(client, gen, limit=1).json()
    row = page["items"][0]
    assert row["score_comparison"]["status"] == "same"
    body, headers = _body(row, gen), {"X-LoopLab-Token": "agent-secret"}
    assert client.post("/api/runs/demo/result-notices", json=body, headers=headers).status_code == 200
    store.append("trust_gate_changed", {"trust_gate": "gate"})
    store.append("reward_hack_suspected", {"node_id": 0, "generation": 0,
                 "signals": [{"signal": "protected_missing"}]})
    fresh = _read(client, gen, limit=1).json()["items"][0]
    assert fresh["score"] == row["score"] and fresh["evidence_token"] != row["evidence_token"]
    assert fresh["score_comparison"]["status"] == "ineligible" and fresh["commentary"] is None
    assert _read(client, gen, cursor=page["next_cursor"]).status_code == 409
    assert client.post("/api/runs/demo/result-notices", json=body, headers=headers).json() == {"ok": True, "replayed": True}
    assert client.post("/api/runs/demo/result-notices", json={**body, "action_id": "fresh"}, headers=headers).status_code == 409
