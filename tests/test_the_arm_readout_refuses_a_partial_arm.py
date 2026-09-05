"""The arm's analysis, fixed in code before the numbers exist.

§190 registered twelve batches and an exact stratified permutation. The rules for what COUNTS as a
probe accumulated afterwards, one incident at a time — `freeB3` excluded at $1.1056 (§213.1),
`capB4` capped but never reaching its cap (§243), a pause at the ceiling that is really an ending
(§228) — and each is a degree of freedom that could be re-decided after the fact to suit the number.
Written as code and run while the arm is incomplete, they cannot be.

The property these tests pin is the one that costs money to get wrong: the readout must REFUSE a
partial arm, and its admission rules must be the ones already written down.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import arm_readout  # noqa: E402


def _probe(root: Path, name: str, cap, spend: float, score, finished: bool = True):
    d = root / name / "runs" / "edge_expansion" / "run"
    d.mkdir(parents=True)
    (d / "config.snapshot.json").write_text(
        json.dumps({"developer_probe_max_calls": cap}), encoding="utf-8")
    events = [{"type": "llm_usage", "data": {"cost": spend}}]
    if finished:
        events.append({"type": "run_finished", "data": {"reason": "budget_exhausted"}})
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    if score is not None:
        (root / name / "final.json").write_text(json.dumps({"speedup": score}), encoding="utf-8")


def test_it_refuses_to_read_a_partial_arm(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    monkeypatch.setattr(arm_readout.arm_fidelity, "DEFAULT_ROOT", str(tmp_path))
    monkeypatch.setattr(arm_readout, "BATCHES", [(["t1", "t2"], ["c1", "c2"])])
    for n, cap in (("t1", 12), ("t2", 12), ("c1", 0), ("c2", 0)):
        _probe(tmp_path, n, cap, 1.0, 200.0)
    assert arm_readout.main(["--batches", "12"]) == 2
    out = capsys.readouterr().out
    assert "REFUSING TO READ THE ARM" in out, out


def test_a_probe_over_the_spend_ceiling_is_excluded(tmp_path, monkeypatch):
    """§213.1's criterion, written before any contrast was read: over $1.05 is not a $1 probe.
    `freeB3` finished at $1.1056 after I resumed it, and §228 is why the engine let it."""
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    monkeypatch.setattr(arm_readout.arm_fidelity, "DEFAULT_ROOT", str(tmp_path))
    _probe(tmp_path, "over", 0, 1.1056, 260.9543)
    got, why = arm_readout.admit("over", "control", 1.05)
    assert got is None and "over the $1.05 ceiling" in why, why
    _probe(tmp_path, "ok", 0, 1.0156, 258.2564)
    assert arm_readout.admit("ok", "control", 1.05)[0] == 258.2564


def test_a_probe_whose_config_disagrees_with_its_label_is_excluded(tmp_path, monkeypatch):
    """§243: behaviour alone cannot tell a treated probe that never reached its cap from a control.
    The run's own config can, and it is the admission rule."""
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    monkeypatch.setattr(arm_readout.arm_fidelity, "DEFAULT_ROOT", str(tmp_path))
    _probe(tmp_path, "mislabelled", 0, 1.0, 200.0)
    got, why = arm_readout.admit("mislabelled", "treat", 1.05)
    assert got is None and "not 12" in why, why
    _probe(tmp_path, "capB4", 12, 1.0095, 215.3809)
    assert arm_readout.admit("capB4", "treat", 1.05)[0] == 215.3809, (
        "capB4 stopped at eleven probes on its own and must still be in the arm")


def test_a_pause_at_the_ceiling_counts_as_ended(tmp_path, monkeypatch):
    """§228: sixteen corpus runs record a normal ending as a Developer crash, and the engine fix
    cannot reach probes already on disk."""
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    monkeypatch.setattr(arm_readout.arm_fidelity, "DEFAULT_ROOT", str(tmp_path))
    _probe(tmp_path, "paused_at_ceiling", 0, 1.0031, 227.0792, finished=False)
    assert arm_readout.admit("paused_at_ceiling", "control", 1.05)[0] == 227.0792
    _probe(tmp_path, "paused_midway", 0, 0.8645, 260.0, finished=False)
    got, why = arm_readout.admit("paused_midway", "control", 1.05)
    assert got is None and "has not ended" in why, why


def test_the_permutation_null_is_the_registered_one():
    """Six relabellings per batch of four, and the observed split is one of them. A one-sided test
    whose p can reach 0 has lost the observed arrangement from its own null."""
    batches = [([200.0, 210.0], [100.0, 110.0]), ([220.0, 230.0], [120.0, 130.0])]
    p = arm_readout.stratified_p(batches)
    assert p == 1 / 36, p
    flat = [([100.0, 100.0], [100.0, 100.0])]
    assert arm_readout.stratified_p(flat) == 1.0


def _monte_carlo_p(batches, draws=200_000, seed=20260905):
    """The SAME p by a different method: sample relabellings instead of enumerating them.

    Enumeration and sampling can both be wrong, but they are wrong in different ways -- an
    off-by-one in the `combinations` bookkeeping does not reproduce itself in a shuffle. This is the
    check `stratified_p` never had: its two existing cases are both extremes (maximal separation and
    no variation at all), and the number that decides $48 has never been tested in the middle of its
    own range.
    """
    import random
    import statistics as st
    rng = random.Random(seed)
    obs = sum(st.mean(t) - st.mean(c) for t, c in batches)
    hits = 0
    for _ in range(draws):
        total = 0.0
        for t, c in batches:
            pool = list(t) + list(c)
            rng.shuffle(pool)
            k = len(t)
            total += st.mean(pool[:k]) - st.mean(pool[k:])
        if total >= obs:
            hits += 1
    return hits / draws


def test_the_exact_p_agrees_with_the_same_p_sampled(tmp_path):
    """Four batches of four, values chosen so p lands mid-range rather than at either extreme."""
    batches = [([180.0, 95.0], [140.0, 60.0]),
               ([210.0, 40.0], [155.0, 130.0]),
               ([90.0, 205.0], [175.0, 35.0]),
               ([160.0, 120.0], [88.0, 150.0])]
    exact = arm_readout.stratified_p(batches)
    assert 0.02 < exact < 0.98, f"fixture is at an extreme ({exact}) and cannot discriminate"
    sampled = _monte_carlo_p(batches)
    assert abs(exact - sampled) < 0.01, (exact, sampled)


def test_the_observed_arrangement_is_always_in_its_own_null():
    """A one-sided p that can reach 0 has dropped the observed relabelling from the null it is
    compared against -- and 0 < alpha for every alpha, so the arm would 'win' on any data."""
    import random
    rng = random.Random(7)
    for _ in range(40):
        batches = [([rng.uniform(0, 300), rng.uniform(0, 300)],
                    [rng.uniform(0, 300), rng.uniform(0, 300)]) for _ in range(3)]
        p = arm_readout.stratified_p(batches)
        assert p >= 1 / 6 ** 3 - 1e-12, p
        assert p <= 1.0


def test_ties_do_not_fall_out_of_the_null():
    """A strict `>` would return 0 on data that says nothing at all, and 0 < alpha for every alpha.

    With four identical values every relabelling gives the same statistic, so all six are `>= obs`
    and p is 1. With `[5, 7]` against `[5, 7]` the null is NOT flat -- the six relabellings give
    +2, 0, 0, 0, 0, -2 -- so p is 5/6, not 1. Getting that wrong was my first guess here, and the
    code was right: the tie sits at the boundary, and only the arrangements strictly below it are
    excluded."""
    assert arm_readout.stratified_p([([5.0, 5.0], [5.0, 5.0])]) == 1.0
    assert arm_readout.stratified_p([([5.0, 7.0], [5.0, 7.0])]) == 5 / 6


def test_the_direction_is_a_choice_and_both_are_available():
    """The registered alternative is one-sided positive. The negative form must be a real test, not
    `1 - p`: with ties in the null the two do not sum to one."""
    batches = [([200.0, 210.0], [100.0, 110.0])]
    up = arm_readout.stratified_p(batches, alternative_positive=True)
    down = arm_readout.stratified_p(batches, alternative_positive=False)
    assert up == 1 / 6 and down == 1.0, (up, down)


def _tree(root, name):
    (root / name / "runs" / "edge_expansion" / "run").mkdir(parents=True)


def test_a_sound_design_has_nothing_to_say(tmp_path):
    batches = [(["capA1", "capB1"], ["freeA1", "freeB1"]),
               (["capA2", "capB2"], ["freeA2", "freeB2"])]
    for t, c in batches:
        for n in t + c:
            _tree(tmp_path, n)
    assert arm_readout.design_problems(batches, str(tmp_path)) == []


def test_one_probe_in_two_batches_is_one_probe_counted_twice(tmp_path):
    """`BATCHES` is hand-maintained and has been appended to five times. A repeated name changes the
    number silently -- nothing in the readout's own output could show it."""
    said = arm_readout.design_problems([(["capA1", "capB1"], ["freeA1", "freeB1"]),
                                        (["capA1", "capB2"], ["freeA2", "freeB2"])])
    assert any("capA1" in s and "counted twice" in s for s in said), said


def test_a_batch_that_is_not_two_plus_two_is_named(tmp_path):
    """The test conditions on the within-batch margins; a 3+1 batch is not the design that was
    registered, and its permutation set is a different one."""
    said = arm_readout.design_problems([(["capA1", "capB1", "capC1"], ["freeA1"])])
    assert any("3+1" in s for s in said), said


def test_a_name_with_no_tree_is_a_typo_not_an_incomplete_batch(tmp_path):
    """Without this it reads as "batch N incomplete" and gets blamed on the bench."""
    _tree(tmp_path, "capA1")
    said = arm_readout.design_problems([(["capA1", "capB1"], ["freeA1", "freeB1"])],
                                       str(tmp_path))
    assert any("capB1" in s and "no probe tree" in s for s in said), said


def test_an_arm_shaped_tree_in_no_batch_must_be_explained(tmp_path):
    for n in ("capA1", "capB1", "freeA1", "freeB1", "freeB99"):
        _tree(tmp_path, n)
    said = arm_readout.design_problems([(["capA1", "capB1"], ["freeA1", "freeB1"])],
                                       str(tmp_path))
    assert any("freeB99" in s and "EXCLUDED" in s for s in said), said


def test_a_probe_excluded_on_the_record_is_not_an_orphan(tmp_path):
    """§213: freeB3 was resumed past a ceiling the meter had already shown and ran on to $1.1056.
    Excluded at a criterion written before any contrast was read -- and the reason lives in the
    code, so nobody helpfully adds it back."""
    for n in ("capA1", "capB1", "freeA1", "freeB1", "freeB3"):
        _tree(tmp_path, n)
    said = arm_readout.design_problems([(["capA1", "capB1"], ["freeA1", "freeB1"])],
                                       str(tmp_path))
    assert not any("freeB3" in s for s in said), said
    assert "§213" in arm_readout.EXCLUDED["freeB3"]


def test_the_readout_refuses_a_malformed_design_before_reading_anything(tmp_path, monkeypatch,
                                                                        capsys):
    monkeypatch.setattr(arm_readout, "BATCHES",
                        [(["capA1", "capB1"], ["freeA1", "freeB1"]),
                         (["capA1", "capB2"], ["freeA2", "freeB2"])])
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    def boom(*a, **k):
        raise AssertionError("admit() ran on a malformed design")
    monkeypatch.setattr(arm_readout, "admit", boom)
    rc = arm_readout.main(["--batches", "2"])
    out = capsys.readouterr().out
    assert rc == 2 and "DESIGN:" in out, (rc, out)
    assert "complete batches" not in out, out
