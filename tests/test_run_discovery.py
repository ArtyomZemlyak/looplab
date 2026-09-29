"""A run under a runs ROOT is found at any depth, and the walk stops AT a run (`core/run_discovery.py`).

The readers that listed only `root.iterdir()` saw `runs/<campaign>` and never `runs/<campaign>/<seed>`.
For the corpus instruments that was a silent under-count; for the orphan survey a live nested run's
uid was unknown, so every cross-run row it wrote read as an ORPHAN, and `looplab memory-orphans
--apply` would have offered to remove it. Both are driven below on a real tree.
"""
from __future__ import annotations

import json
import os

import pytest

from looplab.core.run_discovery import discover_run_dirs

LIVE_UID = "uid-nested-live"
GONE_UID = "uid-gone"


def _run(path, uid="", *, nodes=0):
    path.mkdir(parents=True)
    data = {"run_uid": uid} if uid else {}
    (path / "events.jsonl").write_text(json.dumps(
        {"seq": 0, "ts": 0, "v": 1, "type": "run_started", "data": data}) + "\n")
    for node in range(nodes):
        # a node workdir that happens to hold an events.jsonl is NOT a second run
        (path / f"node_{node}").mkdir()
        (path / f"node_{node}" / "events.jsonl").write_text("{}\n")
    return path


def test_runs_are_found_at_depth_and_the_walk_stops_at_a_run(tmp_path):
    top = _run(tmp_path / "solo", nodes=2)
    seed1 = _run(tmp_path / "campaign" / "seed1", nodes=1)
    seed2 = _run(tmp_path / "campaign" / "seed2")
    deep = _run(tmp_path / "a" / "b" / "c" / "run")
    (tmp_path / ".hidden" / "run").mkdir(parents=True)
    (tmp_path / ".hidden" / "run" / "events.jsonl").write_text("{}\n")
    (tmp_path / "empty").mkdir()

    found = discover_run_dirs(tmp_path)
    assert found.runs == sorted([top, seed1, seed2, deep])
    assert found.unwalked == []
    # the root itself, when it is a run, is the one run
    assert discover_run_dirs(top).runs == [top]


def test_a_subtree_past_the_bound_is_reported_never_dropped(tmp_path):
    _run(tmp_path / "a" / "b" / "c" / "run")
    found = discover_run_dirs(tmp_path, max_depth=2)
    assert found.runs == []
    assert found.unwalked == [tmp_path / "a" / "b"]
    # one level is the old listing: a campaign directory holding runs is said, not silently empty
    assert discover_run_dirs(tmp_path, max_depth=1).unwalked == [tmp_path / "a"]


@pytest.mark.posix_only("symlink")
def test_a_link_cycle_is_walked_once(tmp_path):
    run = _run(tmp_path / "group" / "run")
    os.symlink(tmp_path / "group", tmp_path / "group" / "loop")
    assert discover_run_dirs(tmp_path).runs == [run]


def test_a_live_nested_run_is_not_an_orphan(tmp_path):
    from looplab.serve.memory_cascade import orphan_survey, surviving_run_identities

    runs = tmp_path / "runs"
    _run(runs / "campaign" / "seed1", LIVE_UID)
    store = tmp_path / "memory"
    store.mkdir()
    (store / "lessons.jsonl").write_text("".join(json.dumps(row) + "\n" for row in (
        {"run_id": "seed1", "run_uid": LIVE_UID, "task_id": "t", "lesson": "kept"},
        {"run_id": "gone", "run_uid": GONE_UID, "task_id": "t", "lesson": "orphan"},
    )))

    live = surviving_run_identities(runs)
    assert LIVE_UID in live["uids"] and "seed1" in live["names"]
    survey = orphan_survey(store, runs)
    assert not survey["blind"]
    assert [entry["run_uid"] for entry in survey["identities"]] == [GONE_UID]


def test_an_unwalked_subtree_blinds_the_survey(tmp_path):
    from looplab.serve.memory_cascade import orphan_survey

    runs = tmp_path / "runs"
    _run(runs / "a" / "b" / "c" / "d" / "run", LIVE_UID)
    store = tmp_path / "memory"
    store.mkdir()
    (store / "lessons.jsonl").write_text(json.dumps(
        {"run_id": "run", "run_uid": LIVE_UID, "task_id": "t", "lesson": "x"}) + "\n")
    survey = orphan_survey(store, runs)
    # a run the walk could not reach has an unknown uid: fail closed, propose nothing
    assert survey["blind"] and survey["identities"] == []
    assert any("not walked" in name for name in survey["unreadable_runs"])


def test_the_corpus_instruments_count_a_nested_run(tmp_path):
    from typer.testing import CliRunner

    from looplab.cli import app

    _run(tmp_path / "solo")
    _run(tmp_path / "campaign" / "seed1")
    result = CliRunner().invoke(app, ["card-ladder", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    assert "campaign/seed1" in result.output and "solo" in result.output
