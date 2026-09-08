"""§266: nine batches in, the arm's label was very nearly a synonym for the arm's lane.

17 of 18 treated probes had run on lanes 0-10,48-58 and 11-21,59-69 and 18 of 19 controls on the
other two. §190's test permutes LABELS within a batch, which tests the label only if the four probes
in a batch are exchangeable; with the mapping fixed, "treated" and "ran on the first two lanes" are
one variable with two names. Nothing caught it for nine batches because nothing was looking, and the
check costs one file read per probe.

The ruler could not show the lanes differ (per-sitting contrast positive in 4 of 6 sittings, sign
test p = 0.34) and could not exclude ~3 % either. That is why these tests pin the CHECK rather than
a verdict about the lanes: the failure was never "the lanes are bad", it was "nobody measured".
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import lane_balance  # noqa: E402

LANES = ["0-10,48-58", "11-21,59-69", "22-32,70-80", "33-43,81-91"]


def _readout(tmp_path: Path, batch_list) -> str:
    """A file shaped like `arm_readout.py` -- the real one is parsed, not imported."""
    body = ",\n    ".join(f"({t!r}, {c!r})" for t, c in batch_list)
    path = tmp_path / "arm_readout.py"
    path.write_text(f"ROOT = 'x'\nBATCHES = [\n    {body},\n]\n\n"
                    "def score(name):\n    raise AssertionError('this module must not run')\n",
                    encoding="utf-8")
    return str(path)


def _probe(root: Path, name: str, lane: str):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "INSTRUMENT.txt").write_text(f"probe:          {name}\nlane:           {lane}\n"
                                      "budget_usd:     1.00\n", encoding="utf-8")


def _fixed(tmp_path: Path, n=5):
    """The real history: treatment always on the first two lanes."""
    bl = []
    for i in range(n):
        t = [f"capA{i}", f"capB{i}"]; c = [f"freeA{i}", f"freeB{i}"]
        for name, lane in zip(t + c, LANES):
            _probe(tmp_path, name, lane)
        bl.append((t, c))
    return bl


def _crossed(tmp_path: Path, n=6):
    """The same arm with the mapping alternating batch to batch."""
    bl = []
    for i in range(n):
        t = [f"capA{i}", f"capB{i}"]; c = [f"freeA{i}", f"freeB{i}"]
        order = LANES if i % 2 == 0 else LANES[2:] + LANES[:2]
        for name, lane in zip(t + c, order):
            _probe(tmp_path, name, lane)
        bl.append((t, c))
    return bl


def test_a_label_nailed_to_a_lane_is_named(tmp_path):
    bl = _fixed(tmp_path)
    counts = lane_balance.table(str(tmp_path), bl)
    said = lane_balance.imbalance(counts)
    assert len(said) == 4, said
    assert all("not a free variable" in s for s in said), said


def test_a_crossed_assignment_is_not_flagged(tmp_path):
    """The fix §266 registered -- alternating the mapping -- must read as clean, or the check is a
    permanent alarm and its reader learns to skip it."""
    bl = _crossed(tmp_path)
    assert lane_balance.imbalance(lane_balance.table(str(tmp_path), bl)) == []


def test_a_lane_with_almost_no_probes_on_it_cannot_raise_an_alarm(tmp_path):
    """One crossover probe on a fresh lane is 100 % of that lane and evidence of nothing. The real
    arm has exactly this: lane 22-32,70-80 carried a single treated probe before batch 10."""
    _probe(tmp_path, "capA0", LANES[0]); _probe(tmp_path, "capB0", LANES[0])
    _probe(tmp_path, "freeA0", LANES[1]); _probe(tmp_path, "freeB0", LANES[2])
    counts = lane_balance.table(str(tmp_path), [(["capA0", "capB0"], ["freeA0", "freeB0"])])
    said = lane_balance.imbalance(counts, min_probes=4)
    assert said == [], said
    # ...and the same data with the guard lowered DOES flag, so the guard is what silenced it
    assert lane_balance.imbalance(counts, min_probes=1), "fixture cannot show the guard doing work"


def test_membership_is_parsed_not_imported(tmp_path):
    """The names come out of a module whose job is reading the outcome. Parsing keeps this tool
    unable to reach the rest of it even by accident -- the fixture's `score` raises if run."""
    path = _readout(tmp_path, [(["capA0", "capB0"], ["freeA0", "freeB0"])])
    assert lane_balance.batches(path) == [(["capA0", "capB0"], ["freeA0", "freeB0"])]


def test_it_reads_no_score():
    """The negative property, same as `arm_fidelity`'s: a fidelity tool that prints a number from
    the outcome turns every check into an interim read."""
    src = (Path(__file__).resolve().parents[1] / "benchmarks" / "lane_balance.py").read_text(
        encoding="utf-8")
    body = src.split('"""', 2)[2]
    for token in ("node_evaluated", "speedup", "champion", "metric", "final.json"):
        assert token not in body, f"{token!r} appears below the docstring"


def test_the_real_arm_is_checkable_end_to_end(tmp_path, capsys):
    bl = _fixed(tmp_path)
    rc = lane_balance.main(["--root", str(tmp_path), "--readout", _readout(tmp_path, bl)])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "CONFOUNDED" in out and "Cross the mapping" in out, out
