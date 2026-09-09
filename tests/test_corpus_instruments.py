"""The three CORPUS instruments: the reads three backlog markers say must exist before their
decisions may be taken — `looplab belief-key-split`, `looplab card-ladder`, `looplab asha-rungs`.

Each marker had the same shape: a number folded by hand once, quoted ever after, and re-derivable
only by writing the script again. What was missing in every case was the INSTRUMENT, not the
decision — so what these tests hold is that the instrument answers the marker's own question over a
corpus, and that it answers it WITHOUT deciding anything: no key changes, no card is marked, no
watchdog is armed.

The corpora here are AUTHORED with the real `EventStore` and folded by the real `fold`, so what is
driven is the shipped reader. They are fixtures for the instrument and are never a measurement of
the box's own `runs/` — the numbers the markers quote came from a fold of that tree and only a run
of these commands over it can replace them.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from looplab.cli import app
from looplab.events.asha_curve import asha_curve_report, asha_run_curve
from looplab.events.belief_key_split import belief_key_split_report
from looplab.events.card_ladder import card_ladder_report
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _run(root: Path, name: str, rows, *, direction: str = "max") -> Path:
    """Author one run directory with the REAL append path, and return it."""
    run_dir = root / name
    run_dir.mkdir(parents=True)
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "t", "direction": direction})
    for etype, data in rows:
        store.append(etype, data)
    return run_dir


def _folded(run_dir: Path):
    return fold(EventStore(run_dir / "events.jsonl").read_all())


def _card(cid: str, statement: str, **extra) -> tuple[str, dict]:
    return "card_added", {"id": cid, "statement": statement, **extra}


def _tagged_node(node_id: int, card_id: str, statement: str, concepts, metric: float):
    """A node that OWNS a card, carries a concept membership and lands a metric.

    Three events, because that is what the fold needs: `Card.concept_tags` is projected from
    `RunState.node_concepts` for a card a node owns (`card_ledger.py::_card_node_concept_projection`),
    not from the `card_added` seed, and `Card.evidence` fills from the node.
    """
    return [
        ("node_created", {"node_id": node_id, "operator": "improve",
                          "idea": {"operator": "improve", "hypothesis": statement,
                                   "card_id": card_id}}),
        ("node_concepts", {"node_id": node_id, "concepts": list(concepts), "mode": "full",
                           "generation": 0, "at_vocab": 1}),
        ("node_evaluated", {"node_id": node_id, "metric": metric}),
    ]


def _tree(run_dir: Path) -> dict:
    """Every file under a run dir with its size — the fingerprint the read-only claim is held to."""
    return {str(p.relative_to(run_dir)): p.stat().st_size
            for p in sorted(run_dir.rglob("*")) if p.is_file()}


# --------------------------------------------------------------- belief-key-split (the SPLIT read)

def _split_corpus(tmp_path: Path) -> Path:
    """Two runs. In each, one concept set is carried by two cards whose seed TEXT differs — the
    "temperature 0.05 vs 0.01" case the backlog entry names as the reason a merge is a CLAIM."""
    root = tmp_path / "runs"
    for name in ("run-a", "run-b"):
        _run(root, name,
             [_card("card-1", "temperature 0.05 helps"),
              *_tagged_node(1, "card-1", "temperature 0.05 helps", ["loss/temp"], 0.5),
              _card("card-2", "temperature 0.01 helps"),
              *_tagged_node(2, "card-2", "temperature 0.01 helps", ["loss/temp"], 0.9),
              _card("card-3", "a bigger batch helps"),
              *_tagged_node(3, "card-3", "a bigger batch helps", ["data/batch"], 0.7),
              _card("card-4", "an untagged idea")])
    return root


def test_the_split_read_names_the_group_a_concept_key_would_merge(tmp_path):
    root = _split_corpus(tmp_path)
    report = belief_key_split_report([(d.name, _folded(d)) for d in sorted(root.iterdir())])
    # Two runs x (one 2-card temperature group + one 1-card batch group).
    assert report["runs"] == 2 and report["groups"] == 4 and report["split_groups"] == 2
    assert report["distinct_belief_ids"] == {"1": 2, "2": 2}
    assert report["tagged"] == 6 and report["untagged"] == 2 and report["unkeyed"] == 0
    merge = report["merges"][0]
    assert merge["concepts"] == ["loss/temp"] and merge["belief_ids"] == 2 and merge["cards"] == 2
    # WHAT THE MERGE WOULD POOL, which is the whole point of printing it: both statements and the
    # evidence node ids that would end up under one verdict.
    assert {b["statement"] for b in merge["beliefs"]} == {"temperature 0.05 helps",
                                                          "temperature 0.01 helps"}
    assert sorted(n for b in merge["beliefs"] for n in b["evidence"]) == [1, 2]


def test_two_runs_are_never_merged_into_one_group(tmp_path):
    """The scope limit the module states: the same concept set in two runs is TWO groups. Merging
    across runs is a bigger claim than the one under review, and this instrument does not make it."""
    root = _split_corpus(tmp_path)
    report = belief_key_split_report([(d.name, _folded(d)) for d in sorted(root.iterdir())])
    assert {m["run"] for m in report["merges"]} == {"run-a", "run-b"}
    assert all(m["cards"] == 2 for m in report["merges"])


def test_a_card_with_no_concepts_or_no_seed_statement_cannot_disagree(tmp_path):
    """Both are counted and EXCLUDED: a bucket of "unknown" would read as agreement."""
    root = tmp_path / "runs"
    _run(root, "run-a", [_card("card-1", "an untagged idea"), _card("card-2", "another untagged")])
    report = belief_key_split_report([(d.name, _folded(d)) for d in sorted(root.iterdir())])
    assert report["cards"] == 2 and report["untagged"] == 2
    assert report["groups"] == 0 and report["split_groups"] == 0 and report["merges"] == []


def test_a_group_whose_verdicts_already_differ_is_flagged_not_judged(tmp_path):
    """A merge writes ONE verdict over the members. Where the members' verdicts already disagree,
    pooling is the risk the entry names — so the group is flagged, and nothing is called wrong."""
    root = tmp_path / "runs"
    _run(root, "run-a",
         [_card("card-1", "temperature 0.05 helps"),
          *_tagged_node(1, "card-1", "temperature 0.05 helps", ["loss/temp"], 0.5),
          _card("card-2", "temperature 0.01 helps"),
          # An improvement over its parent earns `supported`, where node 1's draft is only `tested`.
          ("node_created", {"node_id": 2, "operator": "improve", "parent_ids": [1],
                            "idea": {"operator": "improve", "hypothesis": "temperature 0.01 helps",
                                     "card_id": "card-2"}}),
          ("node_concepts", {"node_id": 2, "concepts": ["loss/temp"], "mode": "full",
                             "generation": 0, "at_vocab": 1}),
          ("node_evaluated", {"node_id": 2, "metric": 0.9})])
    report = belief_key_split_report([(d.name, _folded(d)) for d in sorted(root.iterdir())])
    merge = report["merges"][0]
    assert merge["conflicting_verdicts"] is True and len(merge["verdicts"]) > 1
    assert report["conflicting_verdict_groups"] == 1


def test_belief_key_split_prints_the_statements_and_changes_no_key(tmp_path):
    root = _split_corpus(tmp_path)
    before = {d.name: _tree(d) for d in sorted(root.iterdir())}
    result = CliRunner().invoke(app, ["belief-key-split", str(root)])
    assert result.exit_code == 0, result.output
    assert "4 concept-equal group(s) over 8 card(s) in 2 run(s)" in result.output
    assert "2 group(s) SPLIT by the seed-TEXT key" in result.output
    assert "temperature 0.05 helps" in result.output and "temperature 0.01 helps" in result.output
    # The instrument READS. A key change would be a selection change (`belief_id` is the retry
    # attach rule's "same question?" test), so the corpus must come back byte-identical.
    assert {d.name: _tree(d) for d in sorted(root.iterdir())} == before


def test_belief_key_split_json_is_the_same_report(tmp_path):
    root = _split_corpus(tmp_path)
    result = CliRunner().invoke(app, ["belief-key-split", str(root), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["split_groups"] == 2 and payload["merges"][0]["belief_ids"] == 2
    assert "would MERGE" in payload["rule"]


def test_an_empty_runs_root_refuses_rather_than_reporting_zero(tmp_path):
    """Exit 2 with the hint: a mistyped root that printed "0 split groups" would read as a corpus
    that agrees with itself."""
    (tmp_path / "empty").mkdir()
    for command in ("belief-key-split", "card-ladder", "asha-rungs"):
        result = CliRunner().invoke(app, [command, str(tmp_path / "empty")])
        assert result.exit_code == 2, (command, result.output)
        assert "no runs found under" in result.output


# ------------------------------------------------------------------ card-ladder (the TRIGGER read)

def _flat_ladder(tmp_path: Path) -> Path:
    """The corpus shape the deferral was measured on: a direction with a child and NO own evidence."""
    root = tmp_path / "runs"
    _run(root, "run-a",
         [_card("card-dir", "teachers help"),
          _card("card-kid", "distill from a bigger teacher", parent_card_id="card-dir"),
          *_tagged_node(1, "card-kid", "distill from a bigger teacher", ["model/distill"], 0.4)])
    return root


def test_a_parent_without_own_evidence_does_not_fire_the_trigger(tmp_path):
    report = card_ladder_report([(d.name, _folded(d))
                                 for d in sorted(_flat_ladder(tmp_path).iterdir())])
    assert report["cards"] == 2 and report["edges"] == 1 and report["max_depth"] == 1
    assert report["depth_histogram"] == {"0": 1, "1": 1}
    assert report["parents"] == 1 and report["parents_without_own_evidence"] == 1
    assert report["trigger"]["fired"] is False and report["trigger"]["rows"] == []


def test_the_trigger_fires_only_on_children_AND_own_level_evidence(tmp_path):
    """Both halves are load-bearing — the state rule 3 acts from is the conjunction."""
    root = tmp_path / "runs"
    _run(root, "run-a",
         [_card("card-dir", "teachers help"),
          _card("card-kid", "distill from a bigger teacher", parent_card_id="card-dir"),
          # The parent's OWN experiment: this is what the corpus has never had.
          *_tagged_node(1, "card-dir", "teachers help", ["model/teacher"], 0.1),
          *_tagged_node(2, "card-kid", "distill from a bigger teacher", ["model/distill"], 0.4)])
    report = card_ladder_report([(d.name, _folded(d)) for d in sorted(root.iterdir())])
    trigger = report["trigger"]
    assert trigger["fired"] is True and trigger["cards"] == 1
    row = trigger["rows"][0]
    assert row["card_id"] == "card-dir" and row["children"] == 1 and row["evidence"] == [1]
    assert row["child_card_ids"] == ["card-kid"] and row["run"] == "run-a"


def test_card_ladder_prints_the_verdict_and_writes_nothing(tmp_path):
    root = _flat_ladder(tmp_path)
    before = {d.name: _tree(d) for d in sorted(root.iterdir())}
    result = CliRunner().invoke(app, ["card-ladder", str(root)])
    assert result.exit_code == 0, result.output
    assert "TRIGGER NOT FIRED" in result.output and "zero possible firings" in result.output
    assert "ladder depth histogram: 0x1 1x1" in result.output
    assert "child_card_ids" in result.output          # the rule is printed with the counts
    assert {d.name: _tree(d) for d in sorted(root.iterdir())} == before


def test_card_ladder_names_the_card_when_the_trigger_fires(tmp_path):
    root = tmp_path / "runs"
    _run(root, "run-a",
         [_card("card-dir", "teachers help"),
          _card("card-kid", "distill from a bigger teacher", parent_card_id="card-dir"),
          *_tagged_node(1, "card-dir", "teachers help", ["model/teacher"], 0.1)])
    result = CliRunner().invoke(app, ["card-ladder", str(root)])
    assert result.exit_code == 0, result.output
    assert "TRIGGER FIRED" in result.output and "card-dir" in result.output
    assert "children: card-kid" in result.output


# ------------------------------------------------------------------- asha-rungs (the CURVE read)

def _span(name: str, **attributes) -> str:
    return json.dumps({"name": name, "kind": "operation", "trace_id": "t", "span_id": "s",
                       "parent_id": None, "run_id": "r", "attributes": attributes,
                       "events": [], "status": "OK", "duration_s": 0.1})


def _asha_run(root: Path, name: str, spans, *, settings=None, rows=()) -> Path:
    run_dir = _run(root, name, list(rows))
    if spans is not None:
        (run_dir / "spans.jsonl").write_text("\n".join(spans) + "\n", encoding="utf-8")
    if settings is not None:
        (run_dir / "config.snapshot.json").write_text(json.dumps(settings), encoding="utf-8")
    return run_dir


def test_an_inert_watchdog_reads_as_inert_and_not_as_a_missing_curve(tmp_path):
    """The engine's own receipt, read where it was WRITTEN: `_state_asha_inert` stamps the reason on
    an `asha_monitor` span, so the contract rung never has to be re-derived here."""
    root = tmp_path / "runs"
    reason = "the metric spec declares no `resource_key`, so no live sample can ever be compared"
    run_dir = _asha_run(root, "run-a", [_span("asha_monitor", node_id=1, generation=0,
                                              kill_reachable=False, inert_reason=reason)],
                        settings={"asha_live": True, "asha_live_kill": True,
                                  "asha_live_min_siblings": 3})
    row = asha_run_curve("run-a", state=_folded(run_dir), events=[],
                         spans=[json.loads(line) for line in
                                (run_dir / "spans.jsonl").read_text().splitlines()],
                         settings={"asha_live": True, "asha_live_kill": True,
                                   "asha_live_min_siblings": 3})
    assert row["inert_reasons"] == {reason: 1} and row["curve_node_count"] == 0
    assert row["settings"] == {"asha_live": True, "asha_live_kill": True,
                               "asha_live_min_siblings": 3}
    assert asha_curve_report([row])["curve_found"] is False


def test_two_distinct_coordinates_for_one_node_are_a_curve_and_one_is_not(tmp_path):
    single = asha_run_curve("run-a", events=[("asha_rank", {"node_id": 1, "resource": 10.0,
                                                            "resource_key": "step"})], spans=[])
    assert single["nodes_with_samples"] == 1 and single["curve_node_count"] == 0
    curve = asha_run_curve("run-a", spans=[], events=[
        ("asha_rank", {"node_id": 1, "resource": 10.0, "resource_key": "step"}),
        ("asha_rank", {"node_id": 1, "resource": 20.0, "resource_key": "step"}),
        ("asha_rank", {"node_id": 2, "resource": 10.0, "resource_key": "step"})])
    assert curve["curve_node_count"] == 1 and curve["curve_nodes"][0]["rungs"] == [10.0, 20.0]
    # The same coordinate seen for two nodes is the comparable population a kill would rank in.
    assert curve["max_rung_siblings"] == 2 and curve["resource_keys"] == ["step"]
    assert asha_curve_report([curve])["curve_found"] is True


def test_an_unreadable_spans_sidecar_is_never_reported_as_an_absent_curve(tmp_path):
    """The distinction the whole instrument rests on: "we looked and there was no curve" and "we
    could not look" are opposite facts about a run."""
    row = asha_run_curve("run-a", events=[], spans=None)
    assert row["spans_available"] is False
    report = asha_curve_report([row])
    assert report["runs_unreadable_spans"] == 1 and report["curve_found"] is False


def test_asha_rungs_reads_the_corpus_and_writes_nothing(tmp_path):
    root = tmp_path / "runs"
    reason = "the training log is being written but has never printed the objective metric"
    _asha_run(root, "run-flat", [_span("asha_monitor", node_id=1, kill_reachable=False,
                                       inert_reason=reason)],
              settings={"asha_live": True, "asha_live_kill": True, "asha_live_min_siblings": 3})
    _asha_run(root, "run-nospans", None)
    before = {d.name: _tree(d) for d in sorted(root.iterdir())}
    result = CliRunner().invoke(app, ["asha-rungs", str(root)])
    assert result.exit_code == 0, result.output
    assert "NO CURVE" in result.output and "nothing to halve" in result.output
    assert reason in result.output
    assert "NO SPANS" in result.output                       # the unreadable run says so per-run
    assert "asha_live=True" in result.output                 # the launch snapshot, verbatim
    assert {d.name: _tree(d) for d in sorted(root.iterdir())} == before


def test_asha_rungs_says_so_when_a_run_finally_publishes_a_curve(tmp_path):
    root = tmp_path / "runs"
    _asha_run(root, "run-curve",
              [_span("asha_monitor", node_id=1, generation=0, intermediate=0.4,
                     resource_key="step", resource=10.0),
               _span("asha_monitor", node_id=1, generation=0, intermediate=0.6,
                     resource_key="step", resource=20.0),
               _span("asha_monitor", node_id=2, generation=0, intermediate=0.3,
                     resource_key="step", resource=10.0)])
    result = CliRunner().invoke(app, ["asha-rungs", str(root)])
    assert result.exit_code == 0, result.output
    assert "CURVE FOUND" in result.output and "1 run(s) published" in result.output
    assert "largest same-rung sibling set is 2" in result.output
    assert "asha_live_min_siblings" in result.output         # what a KILL would still additionally need


def test_a_single_run_directory_is_a_corpus_of_one(tmp_path):
    """Asking one run "does it have a curve?" is a fair question, and refusing it would push an
    operator into inventing a temporary root."""
    root = tmp_path / "runs"
    run_dir = _asha_run(root, "run-a", [])
    result = CliRunner().invoke(app, ["asha-rungs", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "1 run(s)" in result.output
