"""Review 2026-10-14 section 7: a MiniOneRec eval printed ~615 numeric keys on its metric line, the
`extra_metrics` bound kept the first 256 in map order — all auto-captured — and three of the four
DECLARED key metrics vanished from the record with nothing saying so."""
from __future__ import annotations

import json
import sys

import anyio

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.core.models import (EXTRA_METRIC_DECLARED, EXTRA_METRICS_MAX_ITEMS, Node,
                                 extra_metric_channel, extra_metrics_overflow,
                                 normalize_extra_metric_channels, normalize_extra_metrics)
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import _extra_metrics_omitted, fold
from looplab.runtime.command_eval import captured_result_fields
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

DECLARED = ["InCart_FilteredUnseenRecall@20", "FilteredUnseenRecall@200",
            "FilteredUnseenRecall@20"]


def _noisy_map():
    out = {f"auto_{i}": float(i) for i in range(615)}
    out.update({name: 0.5 for name in DECLARED})
    return out


def test_declared_names_survive_the_bound_whatever_their_position():
    value = _noisy_map()
    kept = normalize_extra_metrics(value, priority=DECLARED)
    assert len(kept) == EXTRA_METRICS_MAX_ITEMS
    assert all(name in kept for name in DECLARED)
    omitted = extra_metrics_overflow(value, priority=DECLARED)
    assert len(omitted) == len(value) - EXTRA_METRICS_MAX_ITEMS
    assert not set(omitted) & set(kept)


def test_a_map_under_the_bound_normalizes_exactly_as_before():
    value = {"b": 1, "a": 2.5, "bad": "x", "nan": float("nan"), "flag": True}
    assert normalize_extra_metrics(value, priority=["a"]) == {"b": 1.0, "a": 2.5}
    assert list(normalize_extra_metrics(value, priority=["a"])) == ["b", "a"]
    assert extra_metrics_overflow(value) == []


def test_the_capture_puts_declared_metrics_first():
    line = "{" + ", ".join(f'"auto_{i}": {i}' for i in range(300)) + \
        ', "metric": 0.9, "FilteredUnseenRecall@20": 0.25}'
    fields = captured_result_fields(
        line, ".", {"kind": "stdout_json", "key": "metric"}, 0.9,
        metrics={"FilteredUnseenRecall@20": {"kind": "stdout_json",
                                              "key": "FilteredUnseenRecall@20"}})
    assert next(iter(fields["extra_metrics"])) == "FilteredUnseenRecall@20"
    assert "FilteredUnseenRecall@20" in normalize_extra_metrics(fields["extra_metrics"])


def test_the_omitted_receipt_folds_bounded_and_defaults_empty():
    assert _extra_metrics_omitted(None) == {}
    assert _extra_metrics_omitted({"count": 0, "names": []}) == {}
    assert _extra_metrics_omitted({"count": 359, "names": ["a", 3, "b"]}) == {
        "count": 359, "names": ["a", "b"]}
    assert Node.model_fields["extra_metrics_omitted"].default_factory() == {}


# --------------------------------------------------------------------------- the TAGS (review g8)
# The VALUES above survived the bound by priority while the CHANNEL map beside them was still cut
# at the first 256 in map order — and `command_eval` builds that map auto-first — so a declared
# value kept its number, lost its `declared` tag, read `unknown`, and `auto_extra_metrics=False`
# (`authenticated_extra_metrics_only`) then dropped it from the record.

def _line_with_declared_last(n_auto: int = 300) -> dict:
    line = {f"auto_{i}": float(i) for i in range(n_auto)}
    line["metric"] = 1.0
    line["FilteredUnseenRecall@20"] = 0.25           # printed AFTER every auto key
    return line


def test_the_channel_tags_of_kept_values_survive_the_bound():
    tags = {**{f"auto_{i}": "auto" for i in range(300)}, "decl": EXTRA_METRIC_DECLARED}
    assert "decl" not in normalize_extra_metric_channels(tags)          # the old cut, unchanged
    kept = normalize_extra_metric_channels(tags, priority={"decl": 1.0})
    assert kept["decl"] == EXTRA_METRIC_DECLARED and len(kept) == EXTRA_METRICS_MAX_ITEMS
    # Under the bound: byte-identical to the old rule, order included.
    small = {"b": "auto", "a": EXTRA_METRIC_DECLARED, 3: "auto", "x": "bogus"}
    assert list(normalize_extra_metric_channels(small, priority=["a"]).items()) == \
        list(normalize_extra_metric_channels(small).items()) == [("b", "auto"),
                                                                 ("a", EXTRA_METRIC_DECLARED)]


def test_a_real_eval_keeps_a_declared_key_past_the_bound_through_the_gate(tmp_path):
    """DRIVEN: a real repo eval printing 300 undeclared keys and then the declared one, with the
    undeclared channel gated off. The declared value must reach the record, tagged `declared`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run.py").write_text("import json; print(json.dumps(%s))\n"
                                 % json.dumps(_line_with_declared_last()), encoding="utf-8")
    key = "FilteredUnseenRecall@20"
    task = RepoTask(id="g8", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                    eval=EvalSpec(command=[sys.executable, "run.py"],
                                  metric={"kind": "stdout_json", "key": "metric"},
                                  metrics={key: {"kind": "stdout_json", "key": key}}))
    researcher, developer = task.build_roles()
    engine = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                    auto_extra_metrics=False)
    state = anyio.run(engine.run)
    rows = [e.data for e in EventStore(tmp_path / "run" / "events.jsonl").read_all()
            if e.type == "node_evaluated"]
    assert rows, "the run recorded no terminal to inspect"
    assert rows[0]["extra_metrics"] == {key: 0.25}
    assert rows[0]["extra_metrics_provenance"] == {key: EXTRA_METRIC_DECLARED}
    assert state.nodes[rows[0]["node_id"]].extra_metrics_provenance == {key: EXTRA_METRIC_DECLARED}


def test_the_fold_keeps_the_tag_of_a_kept_value_beside_an_unbounded_auto_first_map(tmp_path):
    """A row written before the writer applied the rule carries 256 kept values (declared among
    them) beside an UNBOUNDED auto-first channel map; the fold must still read `declared`."""
    values = {**{f"auto_{i}": float(i) for i in range(255)}, "decl": 0.5}
    tags = {**{f"auto_{i}": "auto" for i in range(400)}, "decl": EXTRA_METRIC_DECLARED}
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "g8", "task_id": "t", "direction": "max"})
    store.append("node_created", {"node_id": 0, "operator": "draft", "parent_ids": [],
                                  "idea": {"operator": "draft", "params": {}}})
    store.append("node_evaluated", {"node_id": 0, "metric": 0.9, "extra_metrics": values,
                                    "extra_metrics_provenance": tags})
    node = fold(store.read_all()).nodes[0]
    assert "decl" in node.extra_metrics
    assert extra_metric_channel(node.extra_metrics_provenance, "decl") == EXTRA_METRIC_DECLARED
