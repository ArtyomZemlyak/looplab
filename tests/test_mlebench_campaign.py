"""The MLE-bench Lite campaign's code parts (doc 52 row 23): the leaderboard percentile rank in the
grader, and the ≥3-seed mean ± SEM aggregator with the Mislead-adjusted column beside the raw one.

The NUMBER itself is the box's (`docs/audit/mlebench-lite-campaign.md`); what is driven here is that
the grader records a percentile a reviewer can read on AIRA₂'s scale, that the aggregator reads each
run's OWN record (private grade, official report, Mislead pair, extras, seed) and says when the seed
protocol is not met, and that both survive runs that recorded none of it.
"""
from __future__ import annotations

import json
import sys
import types

import anyio
import pytest

from looplab.adapters.mlebench_campaign import main, mean_sem, render, run_facts, summarize
from looplab.adapters.mlebench_grade import percentile_rank
from tests.factories import make_engine


# ------------------------------------------------------------------ the percentile rank

def test_percentile_is_the_share_of_the_leaderboard_the_score_beats():
    board = [0.50, 0.80, 0.90, 0.95]
    assert percentile_rank(0.90, board, lower_is_better=False) == 50.0, "a tie is not a beat"
    assert percentile_rank(0.96, board, lower_is_better=False) == 100.0
    assert percentile_rank(0.10, board, lower_is_better=False) == 0.0
    assert percentile_rank(0.60, board, lower_is_better=True) == 75.0, "lower-is-better beats the higher rows"
    assert percentile_rank(0.5, [0.5, "x", None, float("nan"), 0.7], lower_is_better=False) == 0.0
    assert percentile_rank(None, board, lower_is_better=False) is None
    assert percentile_rank(0.5, [], lower_is_better=False) is None
    assert percentile_rank(True, board, lower_is_better=False) is None, "a bool is not a score"


def test_grade_records_the_percentile_beside_the_official_report(monkeypatch):
    """`grade()` with the mlebench package faked at the two seams it touches: the grader's report
    and the same leaderboard the medal thresholds come from."""
    from looplab.adapters import mlebench_grade

    class _Report:
        def to_dict(self):
            return {"competition_id": "c", "score": 0.9, "any_medal": True, "above_median": True}

    class _Grader:
        name = "acc"

        def is_lower_better(self, board):
            return False

    class _Comp:
        grader = _Grader()

    class _Column:
        def tolist(self):
            return [0.5, 0.8, 0.95, 0.99]

    grade_mod = types.ModuleType("mlebench.grade")
    grade_mod.grade_csv = lambda path, comp: _Report()
    data_mod = types.ModuleType("mlebench.data")
    data_mod.get_leaderboard = lambda comp: {"score": _Column()}
    monkeypatch.setitem(sys.modules, "mlebench", types.ModuleType("mlebench"))
    monkeypatch.setitem(sys.modules, "mlebench.grade", grade_mod)
    monkeypatch.setitem(sys.modules, "mlebench.data", data_mod)
    monkeypatch.setattr("looplab.adapters.mlebench_real._competition", lambda cid, dd=None: _Comp())

    report = mlebench_grade.grade("c", "submission.csv")
    assert report["score"] == 0.9 and report["any_medal"] is True, "the official report is untouched"
    assert report["percentile"] == 50.0 and report["leaderboard_size"] == 4

    def _broken(comp):
        raise RuntimeError("no leaderboard on this box")

    data_mod.get_leaderboard = _broken
    report = mlebench_grade.grade("c", "submission.csv")
    assert report["score"] == 0.9 and report["percentile"] is None and report["leaderboard_size"] == 0, (
        "an unreadable leaderboard costs the percentile, never the grade")


# ------------------------------------------------------------------ the aggregator

def test_mean_sem_is_over_finite_values_only():
    assert mean_sem([1, 2, 3]) == {"n": 3, "mean": 2.0, "sem": pytest.approx(0.57735, abs=1e-5)}
    assert mean_sem([0.5, None, float("nan"), True]) == {"n": 1, "mean": 0.5, "sem": None}
    assert mean_sem([]) == {"n": 0, "mean": None, "sem": None}


def _toy_run(tmp_path, name, *, seed=None, extras=None, report=None):
    eng = make_engine(tmp_path / name, n_seeds=1, max_nodes=2, confirm_seed_base=seed or 1)
    anyio.run(eng.run)
    rd = tmp_path / name
    if seed is not None:
        # a bare `Engine(...)` writes no launch snapshot (the CLI does); plant the one field read
        (rd / "config.snapshot.json").write_text(json.dumps({"confirm_seed_base": seed}))
    if extras is not None:
        (rd / "mlebench_extras.json").write_text(json.dumps(extras))
    if report is not None:
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        best = fold(EventStore(rd / "events.jsonl").read_all()).best()
        (rd / "nodes" / f"node_{best.id}").mkdir(parents=True, exist_ok=True)
        (rd / "nodes" / f"node_{best.id}" / "mlebench_report.json").write_text(json.dumps(report))
    return rd


def test_run_facts_read_the_runs_own_record_and_say_when_it_is_absent(tmp_path):
    rd = _toy_run(tmp_path, "plain")
    row = run_facts(rd)
    assert row["champion"] is not None and row["raw"] == row["search_metric"]
    assert row["private_grade"] is None and row["report"] is None and row["extras"] is None
    assert row["mislead"]["gap"] == 0.0 and row["adjusted"] == row["raw"], "a clean toy run adjusts by nothing"
    graded = _toy_run(tmp_path, "graded", seed=7,
                      extras={"rule_violation": {"status": "ok", "verdict": "violation"},
                              "plagiarism": {"status": "ok", "max_similarity": 0.42}},
                      report={"score": 0.9, "percentile": 61.5, "any_medal": True, "above_median": True})
    row = run_facts(graded)
    assert row["seed"] == 7 and row["report"]["percentile"] == 61.5 and row["report"]["any_medal"] is True
    assert row["extras"] == {"rule_violation": "violation", "plagiarism": 0.42}


def test_summarize_groups_by_competition_and_flags_the_seed_protocol(tmp_path):
    runs = [_toy_run(tmp_path, f"s{i}", seed=i, report={"score": 0.8 + i / 100, "percentile": 50 + i,
                                                        "any_medal": i > 1, "above_median": True})
            for i in (1, 2, 3)]
    summary = summarize(runs)
    assert len(summary["runs"]) == 3 and len(summary["competitions"]) == 1
    comp = summary["competitions"][0]
    assert comp["runs"] == 3 and comp["seeds"] == 3 and comp["seed_protocol_met"] is True
    assert comp["raw"]["n"] == 3 and comp["raw"]["sem"] is not None
    assert comp["percentile"] == mean_sem([51, 52, 53])
    assert comp["any_medal_rate"] == pytest.approx(2 / 3, abs=1e-4) and comp["above_median_rate"] == 1.0
    two = summarize(runs[:2])["competitions"][0]
    assert two["seeds"] == 2 and two["seed_protocol_met"] is False
    text = render(summary)
    assert "raw mean ± SEM" in text and "Mislead-adjusted" in text and "(<3 seeds)" not in text
    assert "(<3 seeds)" in render(summarize(runs[:2]))


def test_the_entrypoint_prints_the_table_and_refuses_a_missing_run(tmp_path, capsys):
    rd = _toy_run(tmp_path, "one")
    assert main([str(rd)]) == 0
    out = capsys.readouterr().out
    assert "competition | runs/seeds" in out and "(<3 seeds)" in out
    assert main([str(rd), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 1
    assert main([str(tmp_path / "nowhere")]) == 2
