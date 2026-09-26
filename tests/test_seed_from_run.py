"""`Settings.seed_from_run` (doc 67 67.2): a NEW run's first experiment is a prior run's node.

Rounds were chained by hand and each started from scratch. The launch form imports the prior
champion (or a named node) as an operator inject the engine serves before its first creation turn,
with the evaluation-contract receipt beside it. Driven through the real CLI on real toy runs.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.errors import ConfigRefusal
from looplab.engine.seed_from_run import resolve_seed, seed_intent
from looplab.events.eventstore import EventStore
from looplab.events.node_import import node_import_payload
from looplab.events.replay import fold

_RUN = ["run", "--no-genesis", "--kind", "quadratic", "--goal", "min (x-3)^2", "--direction", "min",
        "--backend", "toy"]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(tmp_path / "kn"))
    return tmp_path


def _run(out, *extra):
    return CliRunner().invoke(app, [*_RUN, *extra, "--out", str(out)])


def _source(tmp_path):
    src = tmp_path / "runs" / "prior"
    assert _run(src, "--max-nodes", "5").exit_code == 0
    return src, fold(EventStore(src / "events.jsonl").read_all())


def test_the_prior_champion_is_the_new_runs_first_experiment(isolated):
    src, prior = _source(isolated)
    champion = prior.best()
    new = isolated / "runs" / "next"
    out = _run(new, "--max-nodes", "3", "-s", f"seed_from_run={src}")
    assert out.exit_code == 0, out.output
    assert f"seeded from run prior #{champion.id}" in out.output
    events = EventStore(new / "events.jsonl").read_all()
    assert events[0].type == "inject_node", "the intent precedes the engine's own setup"
    state = fold(events)
    seed = state.nodes[0]
    assert seed.origin["run_id"] == "prior" and seed.origin["node_id"] == champion.id
    assert seed.origin["seed_from_run"] is True and seed.origin["eval_contract"] == "unknown"
    assert seed.origin["source_attempt"] == champion.attempt
    assert seed.idea.params == champion.idea.params and seed.code == champion.code
    # Evaluated HERE, under this run's protocol; the source's number is provenance only.
    assert seed.metric is not None and seed.origin["metric"] == champion.robust_metric
    assert state.injects_done == 1


def test_a_named_node_a_sibling_id_and_a_rerun_that_never_re_seeds(isolated):
    src, prior = _source(isolated)
    other = next(n for n in prior.nodes.values() if n.id != prior.best().id)
    new = isolated / "runs" / "named"
    # `prior` alone is a SIBLING run id under the new run's own runs root (the web UI's ids).
    assert _run(new, "--max-nodes", "2", "-s", f"seed_from_run=prior#{other.id}").exit_code == 0
    first = fold(EventStore(new / "events.jsonl").read_all())
    assert first.nodes[0].origin["node_id"] == other.id
    # A `run` on the SAME directory continues it and does not import a second time.
    again = _run(new, "--max-nodes", "3", "-s", f"seed_from_run=prior#{other.id}")
    assert again.exit_code == 0, again.output
    assert "ignored: this run directory already has events" in again.output
    events = EventStore(new / "events.jsonl").read_all()
    assert sum(1 for e in events if e.type == "inject_node") == 1


@pytest.mark.parametrize("spec,words", [
    ("nowhere", "no run directory at 'nowhere'"),
    ("prior#99", "has no node #99"),
    ("prior#x", "is not an integer id"),
    ("#3", "empty — name a run directory"),
])
def test_a_bad_seed_is_refused_before_anything_is_created(isolated, spec, words):
    _source(isolated)
    new = isolated / "runs" / "refused"
    out = _run(new, "--max-nodes", "2", "-s", f"seed_from_run={spec}")
    assert out.exit_code == 2, out.output
    assert words in out.output
    assert not new.exists(), "refused at launch, before the run directory was created"


def test_a_run_cannot_seed_itself_or_from_a_withdrawn_node(isolated):
    src, prior = _source(isolated)
    with pytest.raises(ConfigRefusal, match="this run's own directory"):
        resolve_seed(str(src), src)
    store = EventStore(src / "events.jsonl")
    store.append("node_tombstoned", {"node_ids": [prior.best().id]})
    with pytest.raises(ConfigRefusal, match="tombstoned"):
        resolve_seed(f"{src}#{prior.best().id}", isolated / "runs" / "x")


def test_the_receipt_names_a_different_evaluation_contract(isolated):
    """Two runs whose own `task.snapshot.json` declare different eval commands: the receipt says
    `different` and names the facet (`engine/eval_contract.py::contract_notice`)."""
    src, _prior = _source(isolated)
    snap = json.loads((src / "task.snapshot.json").read_text())
    (src / "task.snapshot.json").write_text(json.dumps(
        {**snap, "eval": {"command": ["python", "score_v1.py"],
                          "metric": {"kind": "stdout_json", "key": "loss"}}}))
    new = isolated / "runs" / "new"
    new.mkdir(parents=True)
    (new / "task.snapshot.json").write_text(json.dumps(
        {**snap, "eval": {"command": ["python", "score_v2.py"],
                          "metric": {"kind": "stdout_json", "key": "loss"}}}))
    payload, verdict, note = seed_intent(resolve_seed(str(src), new), new)
    assert verdict == "different" and "eval command" in note
    assert payload["origin"]["eval_contract"] == "different"
    assert payload["origin"]["eval_contract_note"] == note
    (new / "task.snapshot.json").write_text((src / "task.snapshot.json").read_text())
    same = seed_intent(resolve_seed(str(src), new), new)
    assert same[1] == "same" and "eval_contract_note" not in same[0]["origin"]


def test_the_server_import_and_the_launch_form_import_the_same_snapshot(isolated):
    """ONE spelling: the server's `import` action and the seed both build their payload with
    `events/node_import.py::node_import_payload` — the launch form adds only its receipt."""
    src, prior = _source(isolated)
    champion = prior.best()
    built = node_import_payload(prior, champion.id, "prior")
    seeded = resolve_seed(str(src), isolated / "runs" / "y").payload
    assert seeded == built
    assert built["origin"] == {"run_id": "prior", "node_id": champion.id,
                               "metric": champion.robust_metric,
                               "source_attempt": champion.attempt}
    assert built["idea"]["rationale"].endswith(f"imported from run prior #{champion.id}")
