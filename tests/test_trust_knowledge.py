"""I9 leakage, I16 profiler, I17 vector store + retrieval, I19 cross-run memory."""
from __future__ import annotations

import os
from pathlib import Path

from looplab.trust.leakage import target_leakage, temporal_leakage, train_test_contamination
from looplab.engine.memory import CaseLibrary
from looplab.core.profile import profile_column, profile_dataset
from looplab.tools.retrieval import grep, glob_files
from looplab.tools.vectorstore import InMemoryVectorStore, hash_embed


# ----------------------------- I9 leakage ---------------------------------- #
def test_train_test_contamination():
    train = [[1, 2], [3, 4]]
    leaky = train_test_contamination(train, test_rows=[[3, 4], [5, 6]])
    clean = train_test_contamination(train, test_rows=[[7, 8], [5, 6]])
    assert leaky["leak"] and leaky["duplicates"] == 1
    assert not clean["leak"]


def test_target_leakage():
    target = [0.0, 1.0, 2.0, 3.0]
    feats = {"leaky": [0.0, 2.0, 4.0, 6.0], "ok": [3.0, 1.0, 4.0, 1.0]}
    res = target_leakage(feats, target, threshold=0.98)
    assert res["leak"] and "leaky" in res["flagged"] and "ok" not in res["flagged"]


def test_temporal_leakage():
    leak = temporal_leakage(train_timestamps=[1, 2, 9], test_timestamps=[5, 6, 7])
    clean = temporal_leakage(train_timestamps=[1, 2, 3], test_timestamps=[5, 6, 7])
    assert leak["leak"] and leak["overlap"] == 1
    assert not clean["leak"]


def test_a_train_row_AT_the_cutoff_is_a_tie_and_not_a_leak():
    """THE OFF-BY-ONE. `t >= cutoff` flagged a train row whose stamp EQUALS the first test stamp, and
    on a coarse clock that is the ordinary case: a daily or hourly stamp puts the last train row and
    the first test row in the same bucket for every split made on a real calendar boundary.

    This detector is WIRED — `engine/audit.py::Engine._leakage_verdicts` returns True on it and the
    trust gate aborts the run — so the equality made a correct coarse split unrunnable.

    MUTATION: restore `>=` -> this reports leak=True and the run aborts.
    """
    boundary = temporal_leakage(train_timestamps=[1, 2, 5], test_timestamps=[5, 6, 7])
    assert boundary["leak"] is False
    assert boundary["overlap"] == 0
    assert boundary["ties"] == 1, "the tie is RECORDED, not dropped — it is a fact worth saying"


def test_a_real_overlap_past_the_cutoff_is_still_a_leak():
    """The strictness that had to survive: a train row STRICTLY after the first test stamp is
    information from the future however coarse the clock is.

    MUTATION: relax to `t > max(test)` or drop the check -> a genuinely interleaved split scans
    clean, which is the dangerous direction for a trust gate.
    """
    interleaved = temporal_leakage(train_timestamps=[1, 5, 6, 9], test_timestamps=[5, 6, 7])
    assert interleaved["leak"] is True
    assert interleaved["overlap"] == 2, "6 and 9 are past the cutoff; the 5 is the tie"
    assert interleaved["ties"] == 1


def test_a_whole_day_bucket_split_runs():
    """The shape the fix is for, at realistic scale: day-resolution stamps split on a day boundary,
    where every row of the last training day ties the first test day."""
    day = 86_400.0
    train = [day * 10 + i for i in (0, 0, 0)] + [day * 11] * 4
    test = [day * 11] * 3 + [day * 12]
    verdict = temporal_leakage(train_timestamps=train, test_timestamps=test)
    assert verdict["leak"] is False and verdict["ties"] == 4


def test_a_nan_timestamp_cannot_hide_a_real_temporal_leak():
    """NaN comparisons are all False, so a LEADING NaN keeps `min()` at NaN and then every
    `t >= cutoff` is False — the detector reports leak=False on data that genuinely overlaps. A
    false negative is the dangerous direction for a trust gate; non-finite stamps are dropped first,
    and a set with nothing finite left ABSTAINS (checked=False) instead of claiming clean."""
    poisoned = temporal_leakage(train_timestamps=[1, 2, 9], test_timestamps=[float("nan"), 5, 6, 7])
    assert poisoned["leak"] is True and poisoned["overlap"] == 1 and poisoned["cutoff"] == 5

    abstained = temporal_leakage(train_timestamps=[1, 2, 9], test_timestamps=[float("nan")])
    assert abstained["leak"] is False and abstained["checked"] is False


def test_contamination_abstains_on_rows_it_cannot_compare_instead_of_raising():
    """A JSON-shaped dataset has nested list/dict cells, which are unhashable — `tuple(row)` in a set
    raised TypeError straight out of the detector, where every sibling degrades gracefully."""
    nested_leak = train_test_contamination([[1, {"a": 2}]], test_rows=[[1, {"a": 2}]])
    assert nested_leak["leak"] is True and nested_leak["duplicates"] == 1
    nested_clean = train_test_contamination([[1, {"a": 2}]], test_rows=[[1, {"a": 3}]])
    assert nested_clean["leak"] is False

    unreadable = train_test_contamination([1, 2], test_rows=[3])   # rows that are not cell sequences
    assert unreadable["leak"] is False and unreadable["checked"] is False


# ----------------------------- I16 profiler -------------------------------- #
def test_profiler_stats_and_flags():
    p = profile_dataset({
        "num": [1.0, 2.0, 3.0, None],
        "const": [5, 5, 5, 5],
        "cat": ["a", "b", "a", "c"],
    })
    assert p["num"]["dtype"] == "numeric" and p["num"]["n_missing"] == 1
    assert p["num"]["mean"] == 2.0
    assert p["const"]["constant"] is True
    assert p["cat"]["dtype"] == "categorical" and p["cat"]["n_unique"] == 3


# ------------------------- I17 vector store + retrieval -------------------- #
def test_vector_store_search_and_swap():
    from looplab.tools.vectorstore import Item
    vs = InMemoryVectorStore()
    vs.upsert("notes", [
        Item("a", hash_embed("xgboost gradient boosting trees"), {"t": "boost"}),
        Item("b", hash_embed("convolutional neural network image"), {"t": "cnn"}),
    ])
    hits = vs.search("notes", hash_embed("boosting trees"), k=1)
    assert hits and hits[0].id == "a"


def test_grep_and_glob(tmp_path):
    (tmp_path / "a.py").write_text("def train():\n    return 42\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("nothing here\n", encoding="utf-8")
    hits = grep(r"def \w+", str(tmp_path), glob="*.py")
    assert len(hits) == 1 and hits[0].lineno == 1
    assert any(p.endswith("a.py") for p in glob_files("*.py", str(tmp_path)))


def test_grep_and_glob_prune_the_noise_dirs_instead_of_walking_them(tmp_path, monkeypatch):
    """RepoTools.repo_grep points these at whole task repos. Descending into .git/node_modules/.venv
    is both the slow part of the walk and how VCS internals leak into an agent-facing result."""
    (tmp_path / "keep.py").write_text("def train():\n    pass\n", encoding="utf-8")
    for noise in (".git", "node_modules", ".venv", "__pycache__", "checkpoints"):
        (tmp_path / noise).mkdir()
        (tmp_path / noise / "buried.py").write_text("def train():\n    pass\n", encoding="utf-8")
    (tmp_path / "src" / "pkg").mkdir(parents=True)          # a REAL nested dir still gets walked
    (tmp_path / "src" / "pkg" / "deep.py").write_text("def train():\n    pass\n", encoding="utf-8")

    visited = []
    real_walk = os.walk
    monkeypatch.setattr(os, "walk", lambda *a, **kw: (visited.append(a[0]), real_walk(*a, **kw))[1])

    found = {Path(h.path).name for h in grep(r"def train", str(tmp_path), glob="*.py")}
    globbed = {Path(p).parent.name for p in glob_files("*.py", str(tmp_path))}

    assert found == {"keep.py", "deep.py"}, found
    assert globbed == {tmp_path.name, "pkg"}, globbed
    assert not [d for d in visited if Path(d).name in
                {".git", "node_modules", ".venv", "__pycache__", "checkpoints"}], visited


# --------------------------- I19 cross-run memory -------------------------- #
def test_case_library_retrieve_and_retain():
    lib = CaseLibrary(InMemoryVectorStore())
    lib.add("c1", "tabular classification with gradient boosting", {"sol": "xgb"})
    lib.add("c2", "image segmentation with unet", {"sol": "unet"})
    hits = lib.retrieve("tabular gradient boosting model", k=1)
    assert hits[0].id == "c1"

    # retain-on-improvement: better metric replaces, worse is rejected.
    assert lib.retain_if_improved("c3", "time series forecast", {"sol": "arima"}, 0.5, "min")
    assert not lib.retain_if_improved("c3", "time series forecast", {"sol": "arima2"}, 0.9, "min")
    assert lib.retain_if_improved("c3", "time series forecast", {"sol": "arima3"}, 0.2, "min")


def test_profile_nan_is_missing_and_unhashable_ok():
    c = profile_column([1.0, float("nan"), 3.0])
    assert c["n_missing"] == 1
    assert c["mean"] == 2.0
    # Unhashable (nested-list) column must not raise.
    c2 = profile_column([[1, 2], [3, 4], [1, 2]])
    assert c2["n_unique"] == 2


def test_an_oversized_json_integer_does_not_crash_the_profiler():
    """Python ints are arbitrary-precision and JSON integers are unbounded, so a cell like 10**400 is
    a FINITE int that sails past any NaN/inf gate — and then `sum(nonnull)/len(nonnull)` raises
    OverflowError ("integer division result too large for a float"), taking the profiler and the
    leakage front-end down over one oversized cell."""
    c = profile_column([1, 2, 10 ** 400])
    assert c["dtype"] == "categorical"      # degraded, not crashed
    assert "mean" not in c and "std" not in c
    assert c["n_missing"] == 0              # the value is still a value, just not a usable stat

    # A column of ordinary ints keeps its numeric stats — the guard must not widen to plain ints.
    assert profile_column([1, 2, 3])["mean"] == 2.0
    # …and the whole dataset front-end survives the same column.
    assert profile_dataset({"huge": [10 ** 400]})["huge"]["dtype"] == "categorical"


# #21/#22 — grep rejects a bad/over-long (ReDoS) pattern and caps file size
def test_grep_guards_bad_and_long_pattern(tmp_path):
    from looplab.tools.retrieval import grep
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    assert grep("(", str(tmp_path)) == []                          # invalid regex -> []
    assert grep("a" * 2000, str(tmp_path)) == []                   # over-long pattern -> []
    assert [h.line for h in grep("hello", str(tmp_path))] == ["hello"]


def test_a_string_attempts_field_cannot_destroy_the_whole_tree_html():
    """The fold stores `attempts` verbatim (`_on_agent_validated` does `d.get("attempts")` with no
    coercion), so a corrupt or hand-edited agent_validated row carrying a STRING made `att > 1`
    raise TypeError and abort `render_html` — which finalize swallows, losing the ENTIRE tree.html
    over one bad field. This module already hardens `dur` and span depth the same way."""
    from looplab.events.htmlview import _agent_badge

    assert "×" not in _agent_badge({"ok": True, "attempts": "2"})     # was: TypeError
    assert "×" not in _agent_badge({"ok": True, "attempts": True})    # bools are not counts
    assert "×" not in _agent_badge({"ok": True, "attempts": 1})       # a single try shows no count
    assert "×3" in _agent_badge({"ok": True, "attempts": 3})          # a real retry count still shows
