"""Regression tests for the code-review edge-case fixes (durability, malformed input, gating)."""
from __future__ import annotations

from pathlib import Path

from looplab.core.context_budget import truncate_history
from looplab.events.eventstore import EventStore, iter_jsonl
from looplab.core.profile import profile_column
from looplab.trust.redact import redact_secrets


def test_eventstore_heals_torn_final_line(tmp_path: Path):
    p = tmp_path / "events.jsonl"
    es = EventStore(p)
    es.append("a", {"x": 1})
    es.append("b", {"x": 2})
    # Simulate a crash mid-append: a partial final record with no trailing newline.
    with open(p, "ab") as f:
        f.write(b'{"seq":2,"ts":0,"type":"node_ev')
    # A fresh store (resume) must not glue its next record onto the torn line.
    es2 = EventStore(p)
    es2.append("c", {"x": 3})
    types = [r["type"] for r in iter_jsonl(p)]
    assert types == ["a", "b", "c"], types


def test_truncate_history_never_grows():
    # Messages just over the cap: the truncation marker must not make the history larger.
    msgs = [{"role": "user", "content": "x" * 401} for _ in range(50)]
    before = sum(len(m["content"]) for m in msgs)
    out = truncate_history(msgs, max_chars=1000)
    after = sum(len(str(m.get("content") or "")) for m in out)
    assert after <= before


def test_profile_nan_is_missing_and_unhashable_ok():
    c = profile_column([1.0, float("nan"), 3.0])
    assert c["n_missing"] == 1
    assert c["mean"] == 2.0
    # Unhashable (nested-list) column must not raise.
    c2 = profile_column([[1, 2], [3, 4], [1, 2]])
    assert c2["n_unique"] == 2


def test_redact_modern_key_prefixes():
    assert "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX" not in redact_secrets("key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX")
    assert "***" in redact_secrets("token=github_pat_ABCDEFGHIJKLMNOPQRSTUV")
    assert "hf_ABCDEFGHIJKLMNOPQRSTUV" not in redact_secrets("hf_ABCDEFGHIJKLMNOPQRSTUV")


def test_fold_tolerates_metric_less_evaluated_event(tmp_path: Path):
    from looplab.events.replay import fold

    p = tmp_path / "events.jsonl"
    st_store = EventStore(p)
    st_store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    st_store.append("node_created",
                    {"node_id": 0, "parent_ids": [], "operator": "draft",
                     "idea": {"operator": "draft", "params": {}, "rationale": "r"}, "code": "c"})
    # malformed: node_evaluated with no metric key — must fold without KeyError
    st_store.append("node_evaluated", {"node_id": 0})
    st = fold(EventStore(p).read_all())
    assert 0 in st.nodes
    # metric-less node is excluded from the feasible set (can't be sorted/selected)
    assert st.nodes[0] not in st.feasible_nodes()
