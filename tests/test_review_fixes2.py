"""Regression tests for the second whole-codebase review pass: corrupt-log tolerance (fold/archive),
security (SSRF, value coercion), and the ragged-column leakage scan. Each would fail before its fix."""
from __future__ import annotations

from looplab.search.archive import DiversityArchive
from looplab.events.eventstore import EventStore
from looplab.trust.leakage import _pearson
from looplab.core.parse import _coerce_value
from looplab.serve.projects import ProjectStore
from looplab.events.replay import fold
from looplab.tools.web import _ssrf_blocked


def _seed(store: EventStore) -> None:
    store.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"x": 1.0}}, "code": ""})
    store.append("node_evaluated", {"node_id": 0, "metric": 0.5, "violations": []})


def test_fold_tolerates_null_metric_node(tmp_path):
    # a hand-edited/BYO node_evaluated with metric=null folds to an evaluated node — best-selection and
    # the diversity archive must skip it, not crash with TypeError(None < float) and brick every re-fold.
    s = EventStore(tmp_path / "events.jsonl")
    _seed(s)
    s.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "improve",
                              "idea": {"operator": "improve", "params": {"x": 2.0}}, "code": ""})
    s.append("node_evaluated", {"node_id": 1, "metric": None, "violations": []})
    st = fold(s.read_all())                  # raised TypeError before the fix
    assert st.best_node_id == 0              # null-metric node skipped; node 0 wins
    DiversityArchive(0.1).summary(st)        # archive must also tolerate the null-metric node


def test_fold_skips_malformed_node_created(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    _seed(s)
    s.append("node_created", {"node_id": 2})  # missing operator/idea — skip, don't crash the whole fold
    st = fold(s.read_all())
    assert 2 not in st.nodes and 0 in st.nodes


def test_pearson_ragged_columns_still_correlate():
    # a near-perfect proxy that is one row short must NOT silently read as 0.0 (which hides the leak)
    assert abs(_pearson([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0])) > 0.99


def test_coerce_int_rounds_not_truncates_and_rejects_bool():
    assert _coerce_value(3.9, int) == 4       # round, not truncate to 3
    assert _coerce_value("3.9", int) == 4
    assert _coerce_value(True, int) is True   # a JSON bool is not silently flipped to 1


def test_ssrf_blocks_internal_addresses():
    assert _ssrf_blocked("http://127.0.0.1/x")                          # loopback
    assert _ssrf_blocked("http://169.254.169.254/latest/meta-data/")   # cloud metadata (link-local)
    assert _ssrf_blocked("http://localhost:8765/")                     # resolves to loopback


def test_projects_load_tolerates_non_dict_json(tmp_path):
    p = tmp_path / "projects.json"
    p.write_text("[]", encoding="utf-8")      # valid JSON, wrong shape — must not raise AttributeError
    data = ProjectStore(p).load()
    assert isinstance(data, dict) and "projects" in data
