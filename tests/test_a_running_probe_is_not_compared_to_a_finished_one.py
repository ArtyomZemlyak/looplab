"""The in-flight outlier check, and the mistake it made on its very first run.

Three sweeps in a row asked the same question by hand — is this evaluation time / phase share /
first-node point unusual? — and two of the three answers were "ordinary" while one was not, which is
never guessable without the distribution. `outlier_check.py` brings the distribution to the sweep.

Its first run flagged three healthy probes, because it compared a RUNNING probe's
`first_node_at / spend-so-far` against a corpus of FINAL shares: the same node reads 54 % at $0.59
and 32 % at its eventual $1.01. That is §209's mistake in different clothes — a partial quantity held
against a complete one — and it is what these tests pin.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import outlier_check  # noqa: E402


def _run(root: Path, name: str, costs, node_after=None, phase_costs=None):
    d = root / name / "runs" / "edge_expansion" / "run"
    d.mkdir(parents=True)
    events, spent = [], 0.0
    for c in costs:
        events.append({"type": "llm_usage", "data": {"cost": c}})
        spent += c
        if node_after is not None and spent >= node_after:
            events.append({"type": "node_evaluated", "data": {"node_id": 0, "metric": 200.0}})
            node_after = None
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    spans = [{"name": "generation", "attributes": {"phase": p, "cost": str(c)}}
             for p, c in (phase_costs or {"plan_step": 1.0}).items()]
    (d / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    return d


def test_the_first_node_is_measured_in_dollars_not_in_a_share_of_partial_spend(tmp_path):
    """The same node, read twice: once when the run has spent $0.60 and once at $1.00. A share moves;
    dollars do not."""
    _run(tmp_path, "midway", [0.1] * 6, node_after=0.3)
    _run(tmp_path, "done", [0.1] * 10, node_after=0.3)
    mid = outlier_check.measure(str(tmp_path / "midway" / "runs" / "edge_expansion" / "run" /
                                    "events.jsonl"), "nope")
    done = outlier_check.measure(str(tmp_path / "done" / "runs" / "edge_expansion" / "run" /
                                     "events.jsonl"), "nope")
    assert abs(mid["first_node_usd"] - done["first_node_usd"]) < 1e-9, (
        f'{mid["first_node_usd"]} vs {done["first_node_usd"]}: the first node moved because the run '
        "kept spending, which is exactly the bias this replaced")
    assert "first_node_pct" not in mid, "the share is back, and it is not comparable mid-run"


def test_spend_and_node_count_are_not_compared_for_a_running_probe(tmp_path, monkeypatch, capsys):
    """A running probe has spent less and built fewer nodes than any finished one, so comparing them
    flags every live probe every time -- an alarm that means nothing trains its reader to skip the
    ones that do."""
    for i in range(6):
        _run(tmp_path, f"done{i}", [0.1] * 10, node_after=0.3)
    _run(tmp_path, "live", [0.1] * 4, node_after=0.3)
    monkeypatch.setattr(outlier_check.lanes, "probes",
                        lambda *a, **k: [{"probe": "live", "pid": 1, "cpus": {0}}])
    assert outlier_check.main(["--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "spend=" not in out and "nodes=" not in out, out
    assert "nothing outside" in out, out


def test_a_genuine_outlier_is_still_named(tmp_path, monkeypatch, capsys):
    """capA9's shape: a first node at $0.64 where the corpus median is $0.32."""
    for i in range(9):
        _run(tmp_path, f"done{i}", [0.1] * 10, node_after=0.3)
    _run(tmp_path, "late", [0.1] * 8, node_after=0.7)
    monkeypatch.setattr(outlier_check.lanes, "probes",
                        lambda *a, **k: [{"probe": "late", "pid": 1, "cpus": {0}}])
    outlier_check.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "first_node_usd" in out and "OUTSIDE" in out, out


def test_it_reads_no_score(tmp_path):
    """§190 forbids reading the arm's outcome in flight, and a hygiene tool that grew a metric
    column would be an interim read with a friendly name."""
    src = (Path(__file__).resolve().parents[1] / "benchmarks" / "outlier_check.py").read_text(
        encoding="utf-8")
    body = src.split('"""', 2)[2]
    for forbidden in ("final.json", "speedup", "champion"):
        assert forbidden not in body, f"outlier_check reaches for {forbidden!r}"
    got = outlier_check.measure(
        str(_run(tmp_path, "p", [0.1] * 5, node_after=0.2) / "events.jsonl"), "nope")
    assert "metric" not in got and set(got) >= {"spend", "nodes", "first_node_usd"}


def test_a_value_equal_to_many_others_is_not_extreme():
    """Ties are not evidence of extremity. Counting `<= value` puts a probe sitting exactly on a
    much-repeated value at the 100th percentile and flags it for being typical; the midrank
    convention puts it in the middle. Mutation found this: after the constant-corpus guard was
    fixed, no fixture exercised a tie inside a corpus that varies."""
    sample = [0.1, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.9]
    assert 40 <= outlier_check.percentile(sample, 0.3) <= 60, (
        f"{outlier_check.percentile(sample, 0.3)}: a value shared with seven of nine runs is not "
        "an outlier")
    assert outlier_check.percentile(sample, 0.9) > 90
    assert outlier_check.percentile(sample, 0.1) < 10


def _ordered(root: Path, name: str, seq, node_after=None):
    """A run whose generation spans are written in a KNOWN order, so a phase can be front-loaded."""
    d = root / name / "runs" / "edge_expansion" / "run"
    d.mkdir(parents=True)
    events, spent = [], 0.0
    for _, cost in seq:
        events.append({"type": "llm_usage", "data": {"cost": cost}})
        spent += cost
        if node_after is not None and spent >= node_after:
            events.append({"type": "node_evaluated", "data": {"node_id": 0, "metric": 200.0}})
            node_after = None
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    (d / "spans.jsonl").write_text(
        "".join(json.dumps({"name": "generation", "attributes": {"phase": p, "cost": str(c)}}) + "\n"
                for p, c in seq), encoding="utf-8")
    return d


def _front_loaded_corpus(root: Path):
    """Ten finished runs that spend most of their `propose` money early: 60-69 % of the first half
    dollar, but only 35-39.5 % of the whole run. Measured shape -- the real corpus median
    `share_propose` is 31.1 % over the first $0.567 and 25.3 % over the whole run."""
    for i in range(10):
        _ordered(root, f"done{i}",
                 [("propose", 0.30 + 0.005 * i), ("plan_step", 0.20 - 0.005 * i),
                  ("propose", 0.05), ("plan_step", 0.45)], node_after=0.3)


def test_a_young_probe_is_placed_against_the_corpus_at_its_own_age(tmp_path):
    """The whole defect in one assertion: 65 % of the first half-dollar spent on `propose` is
    perfectly ordinary for a probe that has only spent half a dollar, and looks like the corpus
    maximum against runs that were allowed to finish."""
    _front_loaded_corpus(tmp_path)
    whole = outlier_check.corpus(str(tmp_path), "edge_expansion")
    aged = outlier_check.corpus(str(tmp_path), "edge_expansion", cap=0.5)
    assert outlier_check.percentile(whole["share_propose"], 65.0) >= 95.0, (
        "fixture is not front-loaded, so it cannot discriminate")
    assert 5.0 < outlier_check.percentile(aged["share_propose"], 65.0) < 95.0, (
        f"aged corpus {sorted(aged['share_propose'])} still calls 65 % extreme")


def test_truncating_the_corpus_changes_how_a_run_reads_never_whether_it_counts(tmp_path):
    """A run is in the corpus because it FINISHED, which is a fact about the whole run. Capping is
    a reading, not a filter -- otherwise the cap quietly redefines the population it is compared to.
    """
    _front_loaded_corpus(tmp_path)
    _ordered(tmp_path, "tiny", [("propose", 0.1), ("plan_step", 0.1)], node_after=0.05)
    # A FINISHED RUN THAT NEVER REACHED THE CAP. It spent its dollar -- so it belongs in the corpus
    # -- but only $0.30 of that went through generation spans, less than the $0.50 cap. This is the
    # case that tells "read every run as far as it got" apart from "keep only the runs that got
    # this far", and without it a cap-as-filter mutation sails through: every other fixture run has
    # more generation spend than the cap, so dropping the short ones drops nothing.
    d = _ordered(tmp_path, "thin", [("propose", 0.1), ("plan_step", 0.2)], node_after=0.05)
    (d / "events.jsonl").write_text(
        "".join(json.dumps({"type": "llm_usage", "data": {"cost": 0.1}}) + "\n" for _ in range(10)),
        encoding="utf-8")
    assert len(outlier_check.corpus(str(tmp_path), "edge_expansion")["share_propose"]) == 11
    assert len(outlier_check.corpus(str(tmp_path), "edge_expansion", cap=0.5)["share_propose"]) == 11


def test_the_check_itself_stops_flagging_a_probe_for_being_young(tmp_path, monkeypatch, capsys):
    _front_loaded_corpus(tmp_path)
    _ordered(tmp_path, "live", [("propose", 0.325), ("plan_step", 0.175)], node_after=0.3)
    monkeypatch.setattr(outlier_check.lanes, "probes",
                        lambda root: [{"probe": "live", "lane": "0-10", "pid": 1}])
    assert outlier_check.main(["--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "share_propose" not in out, out


def test_a_first_node_that_has_not_arrived_is_a_threshold_not_a_count(tmp_path):
    """A node COUNT is partial for a running probe and complete for a corpus run, so §257 skips it.
    "Has the first node arrived by $S?" is a THRESHOLD and has the same answer whenever it is asked,
    so it IS readable mid-run -- which is how a probe that never gets a node stops reading as clean
    right up until it ends."""
    firsts = [0.1, 0.2, 0.3, 0.4, float("inf")]
    assert outlier_check.late_share(firsts, 0.05) == 0.0
    assert outlier_check.late_share(firsts, 0.25) == 40.0
    assert outlier_check.late_share(firsts, 9.99) == 80.0, (
        "the run that never produced a node left the denominator")


def test_a_run_that_never_produced_a_node_stays_in_the_corpus(tmp_path):
    """It is the most extreme first-node value the corpus has. Dropping it makes every live probe
    look later than it is -- measured: 1 of 108 real edge_expansion runs is exactly this."""
    _run(tmp_path, "never", [0.1] * 10)                      # finished, no node ever
    _run(tmp_path, "early", [0.1] * 10, node_after=0.2)
    firsts = outlier_check.corpus_first_nodes(str(tmp_path), "edge_expansion")
    assert len(firsts) == 2, firsts
    assert float("inf") in firsts, firsts


def test_a_probe_that_is_merely_young_is_not_called_late(tmp_path, monkeypatch, capsys):
    for i in range(10):
        _run(tmp_path, f"done{i}", [0.1] * 10, node_after=0.3 + 0.01 * i)
    _run(tmp_path, "live", [0.1] * 2)                        # $0.20, before anyone's first node
    monkeypatch.setattr(outlier_check.lanes, "probes",
                        lambda root: [{"probe": "live", "lane": "0-10", "pid": 1}])
    outlier_check.main(["--root", str(tmp_path)])
    assert "no node yet" not in capsys.readouterr().out


def test_a_probe_later_than_the_whole_corpus_is_named(tmp_path, monkeypatch, capsys):
    for i in range(10):
        _run(tmp_path, f"done{i}", [0.1] * 10, node_after=0.3 + 0.01 * i)
    _run(tmp_path, "late", [0.1] * 8)                        # $0.80, past every corpus first node
    monkeypatch.setattr(outlier_check.lanes, "probes",
                        lambda root: [{"probe": "late", "lane": "0-10", "pid": 1}])
    outlier_check.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "no node yet at $0.8000" in out and "100 % of finished runs had one" in out, out
