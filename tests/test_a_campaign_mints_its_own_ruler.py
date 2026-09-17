"""§410. A campaign grinds its OWN ruler, and its numbers may not be set beside the corpus's.

Point 5 of the standing sweep says the baseline cache holds two regimes and a third is foreign. On
the 2026-09-10 round it held FOUR: beside the corpus's `w22x1r3` (40 entries) and `lane22r3` (16),
`w2x1r3` (11) and `lane2r3` (9) appeared, all minted at 10:02-10:03 -- by the campaign launched at
09:47.

They are not junk. The campaign cuts the box into a small lane per task: the provenance of the new
rulers records `cpu_affinity: [0, 48]`, a TWO-cpu lane, taken at `loadavg 41` with 44 cpus busy
outside it. The corpus of 156 probes was measured on 22-cpu lanes throughout. The difference is
ONE-DIRECTIONAL, and that is the half a bare "different ruler" leaves out:

    task                       w22x1r3   w2x1r3   ratio
    pagerank                    110.47    64.38   0.58x
    rbf_interpolation            17.54    14.54   0.83x
    sparse_eigenvectors_complex 172.83   146.75   0.85x
    discrete_log                  2.69     2.28   0.85x
    edge_expansion               45.49    42.79   0.94x
    count_riemann_zeta_zeros     75.11    73.32   0.98x

Fewer workers, less contention for the box, the reference runs FASTER, the denominator is SMALLER
-- so every campaign score is lower than the corpus's for the same solver, by up to 42 % on
`pagerank`. Inside one campaign both arms share the ruler, so A against B is fair; what is not fair
is reading a campaign number beside a corpus number.

THIS FILE IS A REWRITE: the original was untracked when the box restarted on 2026-09-10 and
`snapshot.sh` carries only `git diff`, which does not include untracked files. Rebuilt from what
§410 measured, with the direction of the error as a test of its own -- naming the strays is half
the work, and the half that a reader can already see for themselves.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))

import sweep_claims  # noqa: E402

CORPUS = ("w22x1r3", "lane22r3")
CAMPAIGN = ("w2x1r3", "lane2r3")      # what the 2026-09-10 campaign's 2-cpu lanes minted


def _cache(bench: Path, names, n: int = 100) -> Path:
    """The bench tree as `check_baseline_count` walks it: BENCH/looplab/benchmarks/algotune/..."""
    d = bench / "looplab" / "benchmarks" / "algotune" / ".baseline_times"
    d.mkdir(parents=True, exist_ok=True)
    for name in names:
        (d / name).write_text(json.dumps({str(i): 1.0 + i for i in range(n)}), encoding="utf-8")
    return d


def _entries(*regimes, task="edge_expansion", subset="test"):
    return [f"{task}__{subset}__{r}.json" for r in regimes]


def test_the_corpus_own_regimes_say_nothing():
    """MUTATION: every regime is reported, so the line fires on a clean cache.

    Both corpus regimes are legitimate -- §318 measured four tasks in the serial one deliberately
    -- and a note that fires on them is a note the reader learns to skip.
    """
    assert sweep_claims.regime_note(set(CORPUS)) == ""
    assert sweep_claims.regime_note({"w22x1r3"}) == ""
    assert sweep_claims.regime_note(set()) == ""


def test_the_strays_are_named_not_counted():
    """A reader who is told "2 foreign regimes" cannot go and look; one who is told which, can."""
    said = sweep_claims.regime_note(set(CORPUS) | set(CAMPAIGN))
    assert "lane2r3, w2x1r3" in said, said            # sorted, so the sentence is stable
    for good in CORPUS:
        # The corpus's own regimes are not in the list of strays. `w2x1r3` is a substring of
        # nothing here, but `lane2r3` is NOT a substring of `lane22r3` and vice versa -- check the
        # listed part only, not the whole sentence, which mentions the corpus by name.
        assert good not in said.split("BESIDE THE CORPUS'S OWN: ")[1].split(" -- ")[0], said


def test_the_note_says_WHICH_WAY_the_other_ruler_lies():
    """MUTATION: the note names the strays and stops there.

    §410's point: "a different ruler" is half the work. The campaign's lane is narrower, so its
    reference is FASTER and its denominator SMALLER, so its scores come out LOWER than the corpus
    would give the same solver -- a reader comparing the two numbers without that is not merely
    uncertain, they are wrong in a predictable direction.
    """
    said = sweep_claims.regime_note(set(CORPUS) | {"w2x1r3"})
    assert "narrower lane makes the reference faster" in said, said
    assert "campaign scores are smaller than corpus scores" in said, said
    assert "0.58x" in said, said                       # the measured magnitude, not just a sign
    assert "must not be read side by side" in said, said


def test_the_check_carries_the_note(tmp_path):
    """MUTATION: `regime_note` exists and `check_baseline_count` never calls it.

    §399's rule, which this bench keeps re-learning: a check nobody drives is an opinion with a
    green tick. The sweep prints `check_baseline_count`'s sentence, so that is where the note has
    to reach the reader.
    """
    bench = tmp_path / "bench"
    _cache(bench, _entries(*CORPUS) + _entries("w2x1r3", task="pagerank"))
    ok, said = sweep_claims.check_baseline_count(str(bench))
    assert "BESIDE THE CORPUS'S OWN: w2x1r3" in said, said


def test_the_note_is_independent_of_the_count(tmp_path):
    """The count is not the invariant (that is this check's own lesson), so the ruler warning must
    not ride on it: a cache with exactly seven entries and a foreign regime is the dangerous case,
    because the count says everything is fine."""
    bench = tmp_path / "bench"
    names = (_entries(*CORPUS)
             + _entries("w22x1r3", task="pagerank")
             + _entries("w22x1r3", task="discrete_log")
             + _entries("w22x1r3", task="kcenters")
             + _entries("w22x1r3", task="pde_heat1d")
             + _entries("w2x1r3", task="convex_hull"))
    assert len(names) == 7
    _cache(bench, names)
    ok, said = sweep_claims.check_baseline_count(str(bench))
    assert ok is True, said                            # the count is happy
    assert "BESIDE THE CORPUS'S OWN: w2x1r3" in said, said


def test_a_clean_cache_reads_clean(tmp_path):
    """The other direction of the same rule: no strays, no sentence about strays."""
    bench = tmp_path / "bench"
    _cache(bench, _entries(*CORPUS) + _entries("lane22r3", task="max_clique_cpsat"))
    ok, said = sweep_claims.check_baseline_count(str(bench))
    assert "BESIDE" not in said, said
    assert "regimes lane22r3, w22x1r3" in said, said


def test_the_corpus_regimes_are_stated_once_and_shared():
    """MUTATION: the pair is spelled again inside `regime_note`, so the two drift apart.

    `ruler_check` has its own frozen `CAMPAIGN_REGIME`, and §411 is the record of what a frozen
    name costs when the fact it names becomes a property of the RUN. Here the constant is at least
    in one place; this test is what will notice the second copy.
    """
    assert sweep_claims.CORPUS_REGIMES == CORPUS
    for r in sweep_claims.CORPUS_REGIMES:
        assert sweep_claims.regime_note({r}) == ""
