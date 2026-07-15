"""Advisory verifier (PART IV keystone B, §12/§21.13) — grounded + repeated + criteria-decomposed.

These lock in the properties the `rubertlite` measurements demanded (§21.10/§21.12): the judgment is
DECOMPOSED into named criteria; it is REPEATED so cross-sample variance is measurable (the single-shot
flip §21.12 caught); it is strictly ADVISORY and degrades to `method="unavailable"` without a client;
and the `calibrate` harness reports whether the score TRACKS a gold outcome (§12's evaluation gate) —
including honestly returning `pearson=None` on the degenerate zero-variance case foresight hit."""
from __future__ import annotations

from looplab.trust.verifier import (LabelledCase, calibrate, lesson_overgeneralization_criteria,
                                    reexamination_criteria, verify, _pearson, _verdict_to_score)


class _FixedClient:
    """Fake LLM: every sample returns the same verdict list (tool_call only)."""
    def __init__(self, verdicts, rationales=None):
        self.verdicts = verdicts
        self.rationales = rationales or ["r"] * len(verdicts)
        self.calls = 0

    def complete_tool(self, messages, json_schema):
        self.calls += 1
        return {"verdicts": self.verdicts, "rationales": self.rationales}

    def complete_text(self, messages):
        return "not json"


class _FlakyClient:
    """Fake LLM whose verdict FLIPS across samples — the single-shot-variance failure mode (§21.12).
    Deterministic by call index (no RNG) so the test is stable."""
    def __init__(self, sequence):
        self.sequence = sequence   # list of verdict-lists, one per successive call
        self.calls = 0

    def complete_tool(self, messages, json_schema):
        v = self.sequence[self.calls % len(self.sequence)]
        self.calls += 1
        return {"verdicts": v, "rationales": ["r"] * len(v)}

    def complete_text(self, messages):
        return "not json"


class _BadClient:
    def complete_tool(self, messages, json_schema):
        raise RuntimeError("boom")

    def complete_text(self, messages):
        return "nope"


# --------------------------------------------------------------------------- #
# Verdict scale
# --------------------------------------------------------------------------- #

def test_verdict_scale_maps_ordinal_and_synonyms_and_floats():
    assert _verdict_to_score("strong_yes") == 1.0
    assert _verdict_to_score("no") == 0.25
    assert _verdict_to_score("unclear") == 0.5
    assert _verdict_to_score("true") == 0.75          # synonym -> yes
    assert _verdict_to_score(0.8) == 0.8              # a bare float in range
    assert _verdict_to_score("0.3") == 0.3            # float-in-string
    assert _verdict_to_score("garbage") is None       # unparseable -> dropped, not silently 0.5
    assert _verdict_to_score(True) is None            # a bool is not a score


# --------------------------------------------------------------------------- #
# The scorer
# --------------------------------------------------------------------------- #

def test_decomposed_repeated_scoring():
    crit = lesson_overgeneralization_criteria()
    client = _FixedClient(["strong_yes", "yes"])
    rep = verify("do not correct false negatives",
                 "node_63 was a loss-side hack that failed once", crit, client=client, samples=3)
    assert rep.method == "llm" and rep.n_samples == 3
    assert client.calls == 3                                  # REPEATED, not single-shot
    assert rep.per_criterion["over_generalizes"]["mean"] == 1.0
    assert rep.per_criterion["direction_sound"]["mean"] == 0.75
    assert rep.score == round((1.0 + 0.75) / 2, 4)            # weight-averaged
    assert rep.agreement == 1.0                               # every sample agreed


def test_repeated_sampling_detects_variance():
    # the node_63 archetype flipping "re-examine" <-> "don't" across samples (§21.12): agreement < 1
    crit = reexamination_criteria()
    flaky = _FlakyClient([["strong_yes", "yes"], ["no", "no"], ["strong_yes", "yes"]])
    rep = verify("re-examine the false-negative direction?", "it failed once (impl bug)", crit,
                 client=flaky, samples=3)
    assert rep.n_samples == 3
    assert rep.agreement < 1.0                                # instability is visible in the report
    # the mean still summarises the distribution (2 of 3 said implementation_bound=strong_yes)
    assert 0.0 < rep.per_criterion["implementation_bound"]["mean"] < 1.0
    assert rep.per_criterion["implementation_bound"]["std"] > 0.0


def test_unavailable_without_client():
    rep = verify("x", "y", lesson_overgeneralization_criteria(), client=None)
    assert rep.method == "unavailable" and rep.score is None and rep.n_samples == 0


def test_no_criteria_is_unavailable():
    rep = verify("x", "y", [], client=_FixedClient([]))
    assert rep.method == "unavailable" and rep.score is None


def test_degrades_when_all_samples_fail():
    # a client that always raises -> every sample dropped -> method llm, n_samples 0, score None
    rep = verify("x", "y", reexamination_criteria(), client=_BadClient(), samples=3)
    assert rep.method == "llm" and rep.n_samples == 0 and rep.score is None


def test_partial_verdict_list_is_tolerated():
    # a model that emitted ONE verdict for TWO criteria: the present one scores, the missing one is None
    crit = reexamination_criteria()
    rep = verify("s", "e", crit, client=_FixedClient(["yes"]), samples=2)
    assert rep.per_criterion["implementation_bound"]["mean"] == 0.75
    assert rep.per_criterion["reexamine"]["mean"] is None


# --------------------------------------------------------------------------- #
# Calibration harness (§12 evaluation gate)
# --------------------------------------------------------------------------- #

def test_calibration_tracks_gold():
    crit = lesson_overgeneralization_criteria()

    def vf(subject, evidence, criteria, **kw):
        # a mis-lesson over-generalizes (high); a sound lesson does not (low)
        v = ["strong_yes", "no"] if "mis" in subject else ["no", "yes"]
        return verify(subject, evidence, criteria, client=_FixedClient(v), samples=2)

    cases = [
        LabelledCase("mis-lesson", "ev", crit, gold=1.0, criterion_key="over_generalizes", name="a"),
        LabelledCase("sound-lesson", "ev", crit, gold=0.0, criterion_key="over_generalizes", name="b"),
    ]
    cal = calibrate(cases, verify_fn=vf)
    assert cal["n"] == 2
    assert cal["pearson"] == 1.0            # score perfectly tracks the gold label
    assert cal["accuracy"] == 1.0
    assert cal["best_threshold"] is not None
    assert cal["mean_agreement"] == 1.0


def test_best_threshold_represents_all_negative_operating_point():
    from looplab.trust.verifier import _best_threshold
    # gold all-negative: the best cutoff predicts NOTHING positive (representable via the +inf candidate)
    thr, acc = _best_threshold([0.9, 0.8, 0.7], [0.0, 0.0, 0.0])
    assert thr is None and acc == 1.0
    # a separable set still finds a real threshold
    thr2, acc2 = _best_threshold([0.9, 0.2], [1.0, 0.0])
    assert thr2 == 0.9 and acc2 == 1.0


def test_n_samples_counts_samples_with_a_usable_verdict():
    # a client that returns parseable JSON but UNMAPPABLE verdict strings -> every sample dropped
    class _Garbage:
        def complete_tool(self, m, s):
            return {"verdicts": ["banana", "kiwi"], "rationales": ["r", "r"]}
        def complete_text(self, m):
            return "x"
    rep = verify("s", "e", reexamination_criteria(), client=_Garbage(), samples=3)
    assert rep.n_samples == 0 and rep.score is None   # no sample produced a usable verdict


def test_pearson_none_on_zero_variance():
    # the foresight-confidence degenerate case §21.12 had to report honestly (all-equal -> undefined)
    assert _pearson([0.5, 0.5, 0.5], [0.1, 0.9, 0.5]) is None
    assert _pearson([0.4], [0.4]) is None   # <2 points
    assert _pearson([0.1, 0.9], [0.1, 0.9]) == 1.0


def test_calibration_ignores_unavailable_scores():
    crit = reexamination_criteria()

    def vf(subject, evidence, criteria, **kw):
        return verify(subject, evidence, criteria, client=None)   # always unavailable

    cases = [LabelledCase("x", "e", crit, gold=1.0, name="a")]
    cal = calibrate(cases, verify_fn=vf)
    assert cal["n"] == 0 and cal["pearson"] is None
