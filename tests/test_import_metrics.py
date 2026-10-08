"""`looplab import-metrics`: metrics measured after the run, imported BESIDE each node's live ones.

Incident 2026-10-06: nodes scored at @20 were re-scored at @200 by a service; a `metric_retarget` to
@200 would have unranked every one of them, and nothing could carry the numbers into the record
(`backfill-score-metrics` writes only into an EMPTY map, from the run's own score.log).
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.models import (extra_metric_is_backfilled, extra_metric_key_is_backfilled,
                                 objective_coverage)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _run(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "max", "direction": "max"})
    for nid, metric in ((0, 0.13), (1, 0.12)):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                      "code": f"print({nid})"})
        store.append("node_evaluated", {
            "node_id": nid, "generation": 0, "metric": metric, "violations": [],
            "extra_metrics": {"FUR@20": metric}, "extra_metrics_provenance": {"FUR@20": "declared"}})
    return rd


def _import(rd, tmp_path, payload, *extra):
    f = tmp_path / "m.json"
    f.write_text(json.dumps(payload))
    return CliRunner().invoke(app, ["import-metrics", str(rd), str(f),
                                    "--source", "scoring service, 200 beams", *extra])


def test_a_dry_run_writes_nothing(tmp_path):
    rd = _run(tmp_path)
    out = _import(rd, tmp_path, {"0": {"FUR@200": 0.36}})
    assert out.exit_code == 0, out.output
    assert "would import (dry run; --apply to write) 1 value(s) on 1 node(s)" in out.output
    assert not [e for e in EventStore(rd / "events.jsonl").read_all()
                if e.type == "extra_metrics_imported"]


def test_imported_keys_sit_beside_live_ones_marked_key_by_key_and_a_retarget_ranks_them(tmp_path):
    rd = _run(tmp_path)
    out = _import(rd, tmp_path, {"nodes": {"0": {"FUR@200": 0.36, "FUR@20": 0.99},
                                           "1": {"FUR@200": 0.38}}}, "--precision", "4", "--apply")
    assert out.exit_code == 0, out.output
    assert "already carries FUR@20 — kept, not overwritten" in out.output
    store = EventStore(rd / "events.jsonl")
    st = fold(store.read_all())
    n0 = st.nodes[0]
    assert n0.extra_metrics == {"FUR@20": 0.13, "FUR@200": 0.36}, "a live value is never overwritten"
    assert n0.extra_metrics_provenance["FUR@200"] == "declared"
    assert extra_metric_is_backfilled(n0)
    assert extra_metric_key_is_backfilled(n0, "FUR@200")
    assert not extra_metric_key_is_backfilled(n0, "FUR@20"), "the live key stays a measurement"
    assert n0.extra_metrics_backfill["precision_decimals"] == {"FUR@200": 4}
    assert "FUR@200" not in (n0.extra_metrics_direction or {}), "an import orients nothing"
    store.append("metric_retarget", {"key": "FUR@200"})
    st = fold(store.read_all())
    assert objective_coverage(st) == ("FUR@200", [0, 1], [])
    assert st.best_node_id == 1, "node 1 leads on @200 while node 0 led on @20"


def test_a_second_apply_is_a_no_op(tmp_path):
    rd = _run(tmp_path)
    _import(rd, tmp_path, {"0": {"FUR@200": 0.36}}, "--apply")
    again = _import(rd, tmp_path, {"0": {"FUR@200": 0.50}}, "--apply")
    assert "imported 0 value(s) on 0 node(s)" in again.output
    rows = [e for e in EventStore(rd / "events.jsonl").read_all()
            if e.type == "extra_metrics_imported"]
    assert len(rows) == 1
    assert fold(EventStore(rd / "events.jsonl").read_all()).nodes[0].extra_metrics["FUR@200"] == 0.36


def test_the_fold_keeps_the_per_key_rule_against_a_hand_written_row(tmp_path):
    rd = _run(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("extra_metrics_imported", {"node_id": 0, "generation": 0, "source": "x",
                                            "imported_at": 1.0,
                                            "extra_metrics": {"FUR@20": 0.9, "NEW": 0.5}})
    store.append("extra_metrics_imported", {"node_id": 0, "generation": 7, "source": "x",
                                            "imported_at": 1.0, "extra_metrics": {"OTHER": 0.1}})
    n0 = fold(store.read_all()).nodes[0]
    assert n0.extra_metrics == {"FUR@20": 0.13, "NEW": 0.5}, "no overwrite; a stale lifecycle binds nothing"
    assert n0.extra_metrics_backfill["keys"] == ["NEW"]


def test_a_whole_map_backfill_marker_joins_the_key_list(tmp_path):
    """A node whose map came from `backfill-score-metrics` (marker without `keys`: the WHOLE map is
    a reconstruction) keeps every old key reconstructed after an import adds one more."""
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "max", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                  "code": "print(0)"})
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.5, "violations": []})
    store.append("score_metrics_backfilled", {"node_id": 0, "generation": 0, "read_at": 1.0,
                                              "extra_metrics": {"ndcg": 0.4}, "unrecoverable": None,
                                              "precision_decimals": {"ndcg": 2}})
    store.append("extra_metrics_imported", {"node_id": 0, "generation": 0, "source": "svc",
                                            "imported_at": 2.0, "extra_metrics": {"mrr": 0.3}})
    n0 = fold(store.read_all()).nodes[0]
    assert n0.extra_metrics == {"ndcg": 0.4, "mrr": 0.3}
    assert n0.extra_metrics_backfill["keys"] == ["mrr", "ndcg"]
    assert n0.extra_metrics_backfill["precision_decimals"] == {"ndcg": 2}


def test_a_bad_file_is_refused_with_its_reason(tmp_path):
    rd = _run(tmp_path)
    out = _import(rd, tmp_path, {"0": {"FUR@200": "high"}})
    assert out.exit_code == 2 and "is not a finite number" in out.output


# ------------------------------------------------------------------ review 2026-10-08


def _bare(tmp_path, name):
    """A node evaluated with NO extras: the population a score-log backfill exists for."""
    rd = tmp_path / name
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "max", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                  "code": "print(0)"})
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.1, "violations": []})
    return store


_IMPORT = ("extra_metrics_imported", {"node_id": 0, "generation": 0, "source": "svc",
                                      "imported_at": 5.0,
                                      "extra_metrics": {"FUR@200": 0.36, "nDCG@10": 0.9}})
_BACKFILL = ("score_metrics_backfilled", {"node_id": 0, "generation": 0, "read_at": 3.0,
                                          "extra_metrics": {"nDCG@10": 0.41, "MAP@10": 0.3},
                                          "precision_decimals": {"nDCG@10": 2, "MAP@10": 2}})


def test_an_import_and_a_backfill_fold_to_the_same_node_in_either_order(tmp_path):
    """An import that landed first made a later score-log backfill a no-op (`if node.extra_metrics:
    return`), while the opposite order kept both: one record, two answers by log order."""
    nodes = []
    for name, rows in (("bf_first", (_BACKFILL, _IMPORT)), ("import_first", (_IMPORT, _BACKFILL))):
        store = _bare(tmp_path, name)
        for kind, data in rows:
            store.append(kind, data)
        nodes.append(fold(store.read_all()).nodes[0])
    a, b = nodes
    assert a.extra_metrics == b.extra_metrics == {"FUR@200": 0.36, "MAP@10": 0.3, "nDCG@10": 0.41}
    for key in a.extra_metrics:
        assert extra_metric_key_is_backfilled(a, key) and extra_metric_key_is_backfilled(b, key)
    from looplab.core.models import extra_metric_source
    for node in nodes:
        assert extra_metric_source(node, "FUR@200") == "svc"
        assert extra_metric_source(node, "nDCG@10") is None, "the score log measured it"
    assert a.extra_metrics_backfill["precision_decimals"] == b.extra_metrics_backfill[
        "precision_decimals"]


def test_a_second_backfill_after_imports_is_still_a_no_op(tmp_path):
    from looplab.core.models import extra_metrics_are_imports_only
    store = _bare(tmp_path, "r")
    store.append(*_IMPORT)
    # The ONE predicate the fold and the backfill planner (`plan_run`) both ask.
    assert extra_metrics_are_imports_only(fold(store.read_all()).nodes[0])
    (tmp_path / "live").mkdir()
    live = EventStore(_run(tmp_path / "live") / "events.jsonl")
    assert not extra_metrics_are_imports_only(fold(live.read_all()).nodes[0])
    store.append(*_BACKFILL)
    first = fold(store.read_all()).nodes[0].extra_metrics
    store.append("score_metrics_backfilled", {**_BACKFILL[1], "extra_metrics": {"MAP@10": 0.9}})
    assert fold(store.read_all()).nodes[0].extra_metrics == first


def test_the_report_says_what_the_fold_keeps_past_the_bound(tmp_path):
    """The fold drops keys past the 256-key bound; the writer used to report them imported."""
    rd = _run(tmp_path)
    many = {f"k{i:03d}": 1.0 for i in range(300)}
    out = _import(rd, tmp_path, {"0": many}, "--apply")
    assert out.exit_code == 0, out.output
    assert "imported 255 value(s) on 1 node(s)" in out.output, out.output
    assert "45 key(s) past the map's 256-key bound, not imported" in out.output
    rows = [e.data for e in EventStore(rd / "events.jsonl").read_all()
            if e.type == "extra_metrics_imported"]
    assert len(rows[0]["extra_metrics"]) == 255
    assert len(fold(EventStore(rd / "events.jsonl").read_all()).nodes[0].extra_metrics) == 256


def test_an_unreadable_run_directory_is_a_one_line_refusal(tmp_path, monkeypatch):
    """review 2026-10-08: the narrowing to `MetricsInputRefusal` let an OSError out with a traceback
    at exit 1; it is the operator's directory, so it is refused (exit 2), wrapped at the boundary."""
    from looplab.maintenance import import_metrics as module
    rd = _run(tmp_path)

    def _unreadable(*_a, **_k):
        raise PermissionError(13, "Permission denied", str(rd / "events.jsonl"))

    monkeypatch.setattr(module, "plan_import", _unreadable)
    out = _import(rd, tmp_path, {"0": {"FUR@200": 0.36}})
    assert out.exit_code == 2 and "PermissionError" in out.output, out.output
