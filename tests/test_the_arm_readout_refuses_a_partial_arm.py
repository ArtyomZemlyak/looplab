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
        # `subset` BECAUSE THE REAL FILE HAS IT. All 136 final.json on this box record
        # `"subset": "test"`, and a fixture without it was not the shape being tested -- which is
        # how three tests here went red the moment `score` started checking it.
        (root / name / "final.json").write_text(json.dumps({"speedup": score, "subset": "test"}),
                                                encoding="utf-8")


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


def _final(tmp_path, name, record):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "final.json").write_text(json.dumps(record), encoding="utf-8")


def test_a_test_score_is_admitted(tmp_path, monkeypatch):
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    _final(tmp_path, "p", {"speedup": 224.8846, "subset": "test"})
    assert arm_readout.score("p") == (224.8846, None)


def test_a_train_score_is_not_a_test_score(tmp_path, monkeypatch):
    """Every node in a run is evaluated on TRAIN and the champion is scored once on TEST (§84). A
    train figure in this field is a different measurement on a different split -- and it is a float,
    so it would go into the statistic without a word. All 44 finished arm probes and all 136
    final.json on this box say `test`, so this guards a hole rather than closing a leak."""
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    _final(tmp_path, "p", {"speedup": 224.8846, "subset": "train"})
    got, why = arm_readout.score("p")
    assert got is None and "train" in why and "§84" in why, (got, why)


def test_a_missing_subset_is_not_assumed_to_be_test(tmp_path, monkeypatch):
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    _final(tmp_path, "p", {"speedup": 224.8846})
    got, why = arm_readout.score("p")
    assert got is None and "None" in why, (got, why)


def test_a_superseded_record_is_a_score_for_another_solver(tmp_path, monkeypatch):
    """§55: two final.json carried this, both from a scoring pass that took solver.py from the
    wrong path."""
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    _final(tmp_path, "p", {"speedup": 300.0, "subset": "test", "superseded": "wrong solver path"})
    got, why = arm_readout.score("p")
    assert got is None and "superseded" in why, (got, why)


def test_a_nonpositive_or_absent_speedup_is_named(tmp_path, monkeypatch):
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    _final(tmp_path, "zero", {"speedup": 0.0, "subset": "test"})
    _final(tmp_path, "null", {"speedup": None, "subset": "test"})
    assert arm_readout.score("zero")[0] is None and "0.0" in arm_readout.score("zero")[1]
    assert arm_readout.score("null")[0] is None and "None" in arm_readout.score("null")[1]


def test_the_reason_reaches_the_operator_through_admit(tmp_path, monkeypatch):
    """A generic "no usable score" sends the reader to the wrong half of the bench. `admit` must
    pass through what `score` actually found."""
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    _final(tmp_path, "p", {"speedup": 224.0, "subset": "train"})
    monkeypatch.setattr(arm_readout.arm_fidelity, "assigned_cap", lambda root, name: 12)
    monkeypatch.setattr(arm_readout, "spend", lambda name: 1.0)
    monkeypatch.setattr(arm_readout.arm_fidelity, "_run_finished", lambda root, name: True)
    got, why = arm_readout.admit("p", "treat", 1.05)
    assert got is None and "train" in why, (got, why)


def _instrument(root: Path, name: str, lane: str):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "INSTRUMENT.txt").write_text(f"probe:          {name}\nlane:           {lane}\n",
                                      encoding="utf-8")


A1, A2, B1, B2 = "0-10,48-58", "11-21,59-69", "22-32,70-80", "33-43,81-91"


def test_a_batch_on_the_original_lanes_is_mapping_a(tmp_path):
    _instrument(tmp_path, "capA1", A1); _instrument(tmp_path, "capB1", A2)
    assert arm_readout.mapping_of(["capA1", "capB1"], str(tmp_path)) == "A"


def test_a_batch_on_the_swapped_lanes_is_mapping_b(tmp_path):
    _instrument(tmp_path, "capA1", B1); _instrument(tmp_path, "capB1", B2)
    assert arm_readout.mapping_of(["capA1", "capB1"], str(tmp_path)) == "B"


def test_a_batch_with_one_lane_from_each_pair_is_mixed_not_unreadable(tmp_path):
    """Batch 1 of the real arm is exactly this: treatment on 0-10 and 22-32, control on 11-21 and
    33-43. It is internally balanced against the lane pairs and carries no confound at all, so
    calling it "?" would report a failure to measure something that was measured and came out
    even."""
    _instrument(tmp_path, "capA1", A1); _instrument(tmp_path, "capB1", B1)
    assert arm_readout.mapping_of(["capA1", "capB1"], str(tmp_path)) == "mixed"


def test_an_unreadable_instrument_is_a_question_mark(tmp_path):
    assert arm_readout.mapping_of(["nobody"], str(tmp_path)) == "?"


def test_the_contrast_is_reported_under_each_mapping(tmp_path):
    """§266 swapped the mapping for batches 10-12 so the lane confound became estimable. Registered
    here BEFORE any outcome was read, which is the only time such a check is worth writing."""
    for n, lane in (("capA1", A1), ("capB1", A2), ("capA2", B1), ("capB2", B2)):
        _instrument(tmp_path, n, lane)
    monkey = [(["capA1", "capB1"], ["x", "y"]), (["capA2", "capB2"], ["x", "y"])]
    old = arm_readout.BATCHES
    try:
        arm_readout.BATCHES = monkey
        ready = [(1, [200.0, 210.0], [100.0, 110.0]), (2, [150.0, 160.0], [140.0, 130.0])]
        got = arm_readout.lane_split(ready, str(tmp_path))
    finally:
        arm_readout.BATCHES = old
    assert got["A"]["n"] == 1 and abs(got["A"]["contrast"] - 100.0) < 1e-9, got
    assert got["B"]["n"] == 1 and abs(got["B"]["contrast"] - 20.0) < 1e-9, got


def test_the_interaction_test_needs_both_mappings():
    assert arm_readout.interaction_p({"A": {"contrast": 1.0, "rows": [], "n": 1}}) is None


def test_a_lane_effect_of_the_size_that_was_feared_is_visible(tmp_path):
    """A lane effect adds to the contrast under one mapping and subtracts under the other, so the
    gap between the two is twice the effect. With a large enough gap the interaction test must
    notice; with no gap it must not."""
    same = {"A": {"n": 3, "contrast": 20.0,
                  "rows": [([120.0, 130.0], [105.0, 105.0])] * 3},
            "B": {"n": 3, "contrast": 20.0,
                  "rows": [([120.0, 130.0], [105.0, 105.0])] * 3}}
    assert arm_readout.interaction_p(same) > 0.20

    apart = {"A": {"n": 3, "contrast": 90.0,
                   "rows": [([200.0, 200.0], [110.0, 110.0])] * 3},
             "B": {"n": 3, "contrast": -90.0,
                   "rows": [([110.0, 110.0], [200.0, 200.0])] * 3}}
    assert arm_readout.interaction_p(apart) < 0.05


def test_the_interaction_test_is_two_sided_and_that_decides_cases_at_alpha():
    """A lane effect can go either way, so the registered check is two-sided. A one-sided version
    reports about HALF the p on a symmetric null, and that factor decides cases at the boundary:
    this fixture reads 0.0571 two-sided at the default 20 000 draws -- do NOT reject -- against
    0.0283 one-sided, which would call a lane effect real. Registered before any outcome was seen,
    so the side cannot be chosen after looking."""
    import statistics as st
    rows_a = [([120.0, 130.0], [100.0, 105.0])]
    rows_b = [([100.0, 105.0], [120.0, 130.0])]

    def contrast(rows):
        return st.mean([st.mean(t) - st.mean(c) for t, c in rows])

    groups = {"A": {"n": 1, "contrast": contrast(rows_a), "rows": rows_a},
              "B": {"n": 1, "contrast": contrast(rows_b), "rows": rows_b}}
    p = arm_readout.interaction_p(groups)
    assert 0.05 < p < 0.07, p


def _full_arm(tmp_path, monkeypatch, scores=None):
    """Two complete batches that `admit` lets through, with lanes on record."""
    batches = [(["capA1", "capB1"], ["freeA1", "freeB1"]),
               (["capA2", "capB2"], ["freeA2", "freeB2"])]
    for n, lane in (("capA1", A1), ("capB1", A2), ("freeA1", B1), ("freeB1", B2),
                    ("capA2", B1), ("capB2", B2), ("freeA2", A1), ("freeB2", A2)):
        _instrument(tmp_path, n, lane)
    table = scores or {"capA1": 200.0, "capB1": 210.0, "freeA1": 100.0, "freeB1": 110.0,
                       "capA2": 150.0, "capB2": 160.0, "freeA2": 140.0, "freeB2": 130.0}
    monkeypatch.setattr(arm_readout, "BATCHES", batches)
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    monkeypatch.setattr(arm_readout, "admit", lambda n, arm, ms: (table[n], None))
    return batches


def test_a_refusal_writes_no_marker(tmp_path, monkeypatch, capsys):
    """The marker is what lifts §190 for `probe_summary`. A partial readout that left one behind
    would open the embargo over half an answer."""
    monkeypatch.setattr(arm_readout, "BATCHES", [(["capA1", "capB1"], ["freeA1", "freeB1"])])
    monkeypatch.setattr(arm_readout, "ROOT", str(tmp_path))
    for n in ("capA1", "capB1", "freeA1", "freeB1"):
        _instrument(tmp_path, n, A1)
    monkeypatch.setattr(arm_readout, "admit", lambda n, arm, ms: (None, "has not ended"))
    out = tmp_path / "marker.json"
    rc = arm_readout.main(["--batches", "1", "--record", str(out)])
    assert rc == 2 and not out.exists(), (rc, out.exists())


def test_a_full_readout_records_what_it_used(tmp_path, monkeypatch, capsys):
    """`/var/tmp` is ephemeral and has been wiped once, taking 37 unpushed commits and ~69 runs.
    The readout is the deliverable of $48 and existed only as terminal text."""
    _full_arm(tmp_path, monkeypatch)
    out = tmp_path / "marker.json"
    rc = arm_readout.main(["--batches", "2", "--record", str(out)])
    assert rc == 0, capsys.readouterr().out
    got = json.loads(out.read_text(encoding="utf-8"))
    assert got["batches"] == 2 and got["verdict"] in ("reject", "do not reject")
    assert got["design"][0][0] == ["capA1", "capB1"], got["design"]
    assert got["scores"][0]["treat"] == [200.0, 210.0], got["scores"]
    assert "stratified_one_sided_p" in got and "lane_split" in got
    assert "freeB3" in got["excluded"], got["excluded"]


def test_the_record_carries_the_lane_split_the_swap_was_for(tmp_path, monkeypatch, capsys):
    """§266 swapped the mapping so the confound became estimable; §279 computes it. A record
    without it loses the only evidence about the confound."""
    _full_arm(tmp_path, monkeypatch)
    out = tmp_path / "marker.json"
    arm_readout.main(["--batches", "2", "--record", str(out)])
    got = json.loads(out.read_text(encoding="utf-8"))
    assert set(got["lane_split"]) == {"A", "B"}, got["lane_split"]
    assert got["lane_split"]["A"]["n"] == 1 and got["lane_split"]["B"]["n"] == 1


def test_the_record_is_written_atomically(tmp_path, monkeypatch, capsys):
    """A torn marker would lift the embargo over half a readout."""
    _full_arm(tmp_path, monkeypatch)
    out = tmp_path / "marker.json"
    arm_readout.main(["--batches", "2", "--record", str(out)])
    assert out.exists() and not out.with_suffix(".json.tmp").exists()
    json.loads(out.read_text(encoding="utf-8"))       # complete, not torn


def test_the_registered_design_size_is_computable_at_all():
    """THE TEST THIS FILE DID NOT HAVE. §274 checked the statistic across the range of its own
    VALUE -- p near 0 to p near 1 -- on FOUR batches, 1296 relabellings. The size of the DESIGN was
    never a variable. The design is twelve batches: 6**12 = 2 176 782 336, and on the sweep in which
    the twelfth batch landed the gate opened and the function did not return."""
    batches = [([200.0 + i, 210.0 + i], [100.0 + i, 110.0 + i]) for i in range(12)]
    # THE DECISION, NOT A STOPWATCH. Timing the call and asserting afterwards cannot fail when the
    # call does not return: it hangs, and a hanging test reads as "still running" rather than
    # "broken" -- which is exactly how the first version of this test behaved under the mutation
    # that put the ceiling back up.
    space = arm_readout.relabel_space(batches)
    assert space == 6 ** 12, space
    assert space > arm_readout.EXACT_CEILING, (space, arm_readout.EXACT_CEILING)
    got = arm_readout.stratified_p_detail(batches, draws=2_000)
    assert not got["exact"] and got["space"] == space, got


def test_a_computable_design_is_still_computed_exactly(tmp_path):
    """Below the ceiling nothing changes: the answer is the enumeration, not an estimate of it."""
    got = arm_readout.stratified_p_detail([([200.0, 210.0], [100.0, 110.0]),
                                           ([220.0, 230.0], [120.0, 130.0])])
    assert got["exact"] and got["p"] == 1 / 36 and got["se"] == 0.0, got


def test_a_sampled_p_still_contains_its_own_observed_arrangement():
    """§274's property has to survive the switch to sampling. The observed relabelling is one of the
    null's, so a p that can come back exactly 0 -- and 0 beats every alpha -- has dropped it. The
    plus-one estimator keeps it in and turns "no draw reached it" into `< 1/draws`."""
    batches = [([1000.0, 1000.0], [0.0, 0.0])] * 12
    # 20 000 draws, not the default 200 000: the property is about the ESTIMATOR, not the precision,
    # and a suite nobody will sit through stops being run.
    got = arm_readout.stratified_p_detail(batches, draws=20_000)
    assert not got["exact"]
    assert 0.0 < got["p"] <= 2 / (got["draws"] + 1), got


def test_a_sampled_p_is_reproducible():
    """A verdict that moves between runs of the same tool on the same data is not a verdict."""
    batches = [([200.0 + i, 150.0 + i], [140.0 + i, 130.0 + i]) for i in range(12)]
    assert (arm_readout.stratified_p_detail(batches, draws=20_000)["p"]
            == arm_readout.stratified_p_detail(batches, draws=20_000)["p"])


def test_a_flat_arm_reads_one_under_sampling_too():
    assert arm_readout.stratified_p_detail([([100.0, 100.0], [100.0, 100.0])] * 12,
                                           draws=20_000)["p"] == 1.0


def test_the_sampled_p_agrees_with_the_exact_one_where_both_can_be_had():
    """Six batches is 46 656 relabellings -- under the ceiling, so it is enumerated -- and the
    sampled estimate of the same quantity must land on it.

    Six and not eight: the enumeration costs 0.3 s at 6**5, 2.0 s at 6**6 and 14.3 s at 6**7 on this
    box, so an eight-batch fixture makes this test take a hundred seconds. A test nobody will run is
    not a test."""
    batches = [([180.0, 95.0], [140.0, 60.0]), ([210.0, 40.0], [155.0, 130.0]),
               ([90.0, 205.0], [175.0, 35.0]), ([160.0, 120.0], [88.0, 150.0]),
               ([170.0, 100.0], [130.0, 90.0]), ([155.0, 111.0], [120.0, 140.0])]
    exact = arm_readout.stratified_p_detail(batches)
    assert exact["exact"], exact["space"]
    old = arm_readout.EXACT_CEILING
    try:
        arm_readout.EXACT_CEILING = 10
        sampled = arm_readout.stratified_p_detail(batches)
    finally:
        arm_readout.EXACT_CEILING = old
    assert not sampled["exact"]
    assert abs(sampled["p"] - exact["p"]) < 4 * sampled["se"] + 0.005, (sampled, exact)


def test_a_sampled_p_carries_a_standard_error_and_an_exact_one_does_not():
    """The SE is what lets a p near alpha be reported as NEAR alpha instead of read as a decision.
    Without it the sampled number wears the same face as the exact one it replaced."""
    exact = arm_readout.stratified_p_detail([([200.0, 210.0], [100.0, 110.0]),
                                             ([220.0, 230.0], [120.0, 130.0])])
    assert exact["exact"] and exact["se"] == 0.0, exact
    sampled = arm_readout.stratified_p_detail(
        [([180.0, 95.0], [140.0, 60.0]), ([210.0, 40.0], [155.0, 130.0]),
         ([90.0, 205.0], [175.0, 35.0]), ([160.0, 120.0], [88.0, 150.0]),
         ([170.0, 100.0], [130.0, 90.0]), ([155.0, 111.0], [120.0, 140.0]),
         ([133.0, 177.0], [150.0, 99.0]), ([144.0, 122.0], [131.0, 118.0]),
         ([151.0, 129.0], [140.0, 120.0]), ([149.0, 131.0], [128.0, 142.0]),
         ([160.0, 140.0], [139.0, 121.0]), ([158.0, 122.0], [141.0, 119.0])],
        draws=4_000)
    assert not sampled["exact"] and sampled["se"] > 0.0, sampled
    # the plain binomial SE, which is what the printed +/- claims to be
    expected = (sampled["p"] * (1 - sampled["p"]) / sampled["draws"]) ** 0.5
    assert abs(sampled["se"] - expected) < 1e-12, (sampled["se"], expected)


def test_a_p_within_two_standard_errors_of_alpha_is_not_reported_as_a_decision(tmp_path,
                                                                               monkeypatch,
                                                                               capsys):
    """The whole point of carrying the SE: at 200 000 draws it is about 0.0005 near alpha, so this
    fires rarely -- but when it fires, "reject" would be a coin toss dressed as a finding."""
    _full_arm(tmp_path, monkeypatch)
    monkeypatch.setattr(arm_readout, "stratified_p_detail",
                        lambda *a, **k: {"p": 0.0501, "exact": False, "space": 6 ** 12,
                                         "draws": 200_000, "se": 0.0050})
    arm_readout.main(["--batches", "2"])
    out = capsys.readouterr().out
    assert "NOT a decision" in out, out


def test_the_interaction_survives_a_third_group():
    """§279 established in its own text that batch 1 is `mixed`, and then demanded the group set be
    EXACTLY {A, B}. So the registered check returned None on the real design and the readout printed
    no interaction p at all, at the one moment it existed for. `mixed` is excluded from the
    comparison on purpose -- one lane from each pair on each side carries no confound to estimate --
    but its presence must not silence the check."""
    rows = [([120.0, 130.0], [100.0, 105.0])]
    groups = {"A": {"n": 1, "contrast": 22.5, "rows": rows},
              "B": {"n": 1, "contrast": -22.5, "rows": [([100.0, 105.0], [120.0, 130.0])]},
              "mixed": {"n": 1, "contrast": -70.0, "rows": rows}}
    got = arm_readout.interaction_p(groups)
    assert got is not None and 0.0 <= got <= 1.0, got


def test_the_interaction_still_needs_both_mappings_present():
    assert arm_readout.interaction_p({"A": {"contrast": 1.0, "rows": [], "n": 1},
                                      "mixed": {"contrast": 0.0, "rows": [], "n": 1}}) is None
