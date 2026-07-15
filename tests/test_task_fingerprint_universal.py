"""PART IV cross-run Step 0 — universal task-fingerprint tokenization (§21.20.12/§21.20.13).

The legacy `task_fingerprint` tokenizes goal keywords with an ASCII `[a-z0-9]+` allowlist, so a
non-Latin goal (the live `rubertlite` run's Russian goal, or any CJK task) contributes ZERO goal
keywords and its cross-run fingerprint collapses to just kind/dir/metric/param — it can never reach a
SIMILAR-task prior/lesson/case. The `universal` path removes the allowlist without changing anything
else. These tests pin BOTH halves of the contract: legacy stays byte-identical (so a live portfolio is
not silently re-keyed), and universal actually captures other scripts.
"""
from looplab.engine.memory import task_fingerprint, _goal_tokens, fingerprint_similarity


# --------------------------------------------------------------------------- #
# 1. Legacy default is byte-identical to the original regex — the live-run guarantee.
# --------------------------------------------------------------------------- #

def test_legacy_default_is_byte_identical_for_ascii():
    # An English goal: universal must not change the ASCII result at all.
    args = ("dataset", "max", "predict churn using gradient boosting on tabular features", "auc",
            ["learning_rate", "depth"])
    assert task_fingerprint(*args) == task_fingerprint(*args, universal=False)
    assert task_fingerprint(*args, universal=True) == task_fingerprint(*args, universal=False)


def test_legacy_drops_non_latin_goal_keywords():
    # The bug being fixed: a Russian goal yields NO goal tokens under the legacy tokenizer — only the
    # structural kind:/dir: tokens survive.
    fp = task_fingerprint("dataset", "max", "плотный поиск по русским отзывам маркетплейса", "recall")
    goal_toks = [t for t in fp if ":" not in t]
    assert goal_toks == [], f"legacy tokenizer unexpectedly kept non-ASCII tokens: {goal_toks}"


# --------------------------------------------------------------------------- #
# 2. Universal captures any script — the fix.
# --------------------------------------------------------------------------- #

def test_universal_captures_cyrillic():
    fp = task_fingerprint("dataset", "max", "плотный поиск по русским отзывам маркетплейса", "recall",
                          universal=True)
    goal_toks = {t for t in fp if ":" not in t}
    # The salient (>2 char, non-stopword) Russian words are now present.
    assert {"плотный", "поиск", "русским", "отзывам", "маркетплейса"} <= goal_toks


def test_universal_captures_cjk():
    toks = set(_goal_tokens("向量检索 dense retrieval", universal=True))
    assert "dense" in toks and "retrieval" in toks
    assert "向量检索" in toks  # a CJK run survives; legacy would have dropped it


def test_universal_two_russian_goals_now_transfer():
    # Two related Russian retrieval tasks: legacy fingerprints are identical-but-empty (they match only
    # on kind/dir, a false over-match); universal makes their SHARED keywords carry real signal.
    a_legacy = task_fingerprint("dataset", "max", "плотный retrieval по отзывам", "recall")
    b_legacy = task_fingerprint("dataset", "max", "разреженный retrieval по товарам", "recall")
    a_uni = task_fingerprint("dataset", "max", "плотный retrieval по отзывам", "recall", universal=True)
    b_uni = task_fingerprint("dataset", "max", "разреженный retrieval по товарам", "recall", universal=True)
    # Universal keeps the shared "retrieval"/"отзывам"/"товарам" distinction: the two tasks are now
    # DISTINGUISHABLE (similarity < 1.0) where legacy collapsed them to the same near-empty key.
    assert fingerprint_similarity(a_legacy, b_legacy) == 1.0  # legacy: indistinguishable (bug)
    assert fingerprint_similarity(a_uni, b_uni) < 1.0         # universal: real content differs
    assert "retrieval" in set(a_uni) & set(b_uni)             # ...while the shared method still overlaps


# --------------------------------------------------------------------------- #
# 3. Same splitting rules as legacy (underscore is a separator, stopwords/len<=2 filtered).
# --------------------------------------------------------------------------- #

def test_universal_keeps_legacy_splitting_and_filters():
    # underscore splits (not a word char here), 1-2 char tokens and stopwords are dropped — same as legacy.
    assert _goal_tokens("hard_negative to xy", universal=True) == ["hard", "negative"]
    assert _goal_tokens("hard_negative to xy", universal=False) == ["hard", "negative"]


def test_flag_off_matches_legacy_on_mixed_goal():
    # A mixed Latin+Cyrillic goal: with the flag OFF only the Latin survives (unchanged legacy behavior).
    off = task_fingerprint("dataset", "min", "loss для dense encoder обучение", "ndcg")
    goal_toks = {t for t in off if ":" not in t}
    assert goal_toks == {"loss", "dense", "encoder"}  # Cyrillic dropped, exactly as before
