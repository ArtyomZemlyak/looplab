"""I9 leakage, I16 profiler, I17 vector store + retrieval, I19 cross-run memory."""
from __future__ import annotations

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


# #21/#22 — grep rejects a bad/over-long (ReDoS) pattern and caps file size
def test_grep_guards_bad_and_long_pattern(tmp_path):
    from looplab.tools.retrieval import grep
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    assert grep("(", str(tmp_path)) == []                          # invalid regex -> []
    assert grep("a" * 2000, str(tmp_path)) == []                   # over-long pattern -> []
    assert [h.line for h in grep("hello", str(tmp_path))] == ["hello"]
