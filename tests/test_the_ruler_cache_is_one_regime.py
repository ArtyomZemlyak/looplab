"""Point 5 of the sweep pins a COUNT, and the count is not the invariant.

The standing list says ".baseline_times holds seven entries, all re-measured HERE". On 2026-09-04
re-timing arm A on `pagerank` and `spectral_clustering` (§193) legitimately wrote two more and the
count became nine. A reader holding "seven" then has to choose between an alarm and a section they
may not have read.

What actually matters is that every entry is in ONE regime with a full set of per-instance timings:
the regime key is what makes two timings comparable, and §149 is the record of a ruler reporting
0.0 because the key came out `__lane22r3` instead of `__w22x1r3`. `benchmarks/ruler_check.py` checks
that and prints the provenance; these tests pin that it does not check the count.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import ruler_check  # noqa: E402


def _cache(tmp_path: Path, names, n: int = 100) -> Path:
    d = tmp_path / ".baseline_times"
    d.mkdir(exist_ok=True)
    for name in names:
        (d / name).write_text(json.dumps({str(i): 1.0 + i for i in range(n)}), encoding="utf-8")
    return d


GOOD = ["edge_expansion__test__w22x1r3.json", "edge_expansion__train__w22x1r3.json"]


def test_growth_alone_is_not_a_problem():
    """A tool that alarms when the cache grows teaches the reader to ignore it."""
    rows = [dict(file=n, ok_name=True, task="t", subset="test", regime="w22x1r3", n=100,
                 median=1.0, mtime=0.0) for n in ("a", "b", "c", "d", "e", "f", "g", "h", "i")]
    assert ruler_check.problems(rows, "w22x1r3") == []


def test_a_second_regime_is(tmp_path):
    d = _cache(tmp_path, GOOD + ["pagerank__test__lane22r3.json"])
    said = ruler_check.problems(ruler_check.entries(d), "w22x1r3")
    assert said and "lane22r3" in said[0], said
    assert "not comparable" in said[0]


def test_a_short_entry_is(tmp_path):
    d = _cache(tmp_path, GOOD)
    (d / "pde_heat1d__test__w22x1r3.json").write_text(json.dumps({"0": 1.0}), encoding="utf-8")
    said = ruler_check.problems(ruler_check.entries(d), "w22x1r3")
    assert any("1 per-instance timings" in s for s in said), said


def test_an_unparseable_name_is(tmp_path):
    d = _cache(tmp_path, GOOD + ["whatever.json"])
    said = ruler_check.problems(ruler_check.entries(d), "w22x1r3")
    assert any("regime is unknown" in s for s in said), said


def test_a_clean_cache_says_nothing(tmp_path):
    assert ruler_check.problems(ruler_check.entries(_cache(tmp_path, GOOD)), "w22x1r3") == []


def test_the_live_cache_is_clean_and_in_one_regime():
    """The bench's own cache, whatever its size today."""
    rows = ruler_check.entries(ruler_check.DEFAULT_DIR)
    if not rows:                      # a checkout without the bench tree
        return
    assert ruler_check.problems(rows, "w22x1r3") == [], ruler_check.problems(rows, "w22x1r3")
