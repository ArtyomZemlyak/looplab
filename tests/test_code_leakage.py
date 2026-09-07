"""I3 static code-leakage scan."""
from __future__ import annotations

from looplab.trust.leakage import _pearson, code_leakage_scan


def test_flags_fit_on_test():
    code = "scaler = StandardScaler()\nscaler.fit(X_test)\n"
    r = code_leakage_scan(code)
    assert r["leak"] and any(f["signal"] == "fit_on_test" for f in r["flags"])


def test_flags_fit_before_split():
    code = (
        "import numpy as np\n"
        "scaler = StandardScaler()\n"
        "Xs = scaler.fit_transform(X)\n"          # fit on full data ...
        "X_train, X_test, y_train, y_test = train_test_split(Xs, y)\n"   # ... before the split
    )
    r = code_leakage_scan(code)
    assert r["leak"] and any(f["signal"] == "fit_before_split" for f in r["flags"])


def test_fit_before_split_still_flagged_with_kfold_import():
    # A bare `KFold`/`StratifiedKFold` import must NOT be mistaken for the split point (which would set
    # split_at=0 and silently disable the fit_before_split detector for the whole file). The real split
    # is the `.split(` call / the `KFold(` instantiation.
    code = (
        "from sklearn.model_selection import KFold\n"      # import — NOT a split point
        "scaler = StandardScaler()\n"
        "Xs = scaler.fit_transform(X)\n"                   # fit on full data BEFORE any split -> leak
        "for tr, te in KFold(5).split(X):\n"
        "    pass\n"
    )
    r = code_leakage_scan(code)
    assert r["leak"] and any(f["signal"] == "fit_before_split" for f in r["flags"]), r


def test_str_split_does_not_false_positive_an_honest_full_data_fit():
    # Regression: `_SPLIT_RE` once contained a bare `\.split\(` that matched Python's `str.split(...)`.
    # An honest final refit-on-all-data followed by an unrelated string split set split_at to the
    # str.split line, so the earlier .fit() read as fit_before_split and was HARD-gated (thorough/gate
    # profile). A string split must not anchor a train/test boundary.
    assert not code_leakage_scan(
        'model.fit(X, y)\nname = path.split("/")[-1]\nmodel.save(name)')["leak"]
    assert not code_leakage_scan('kmeans.fit(X)\nparts = line.split(",")')["leak"]


def test_str_split_does_not_suppress_a_real_fit_before_split_leak():
    # The other direction of the same collision: a benign `str.split()` placed ABOVE a genuine
    # fit-on-full-data-before-split used to set split_at=0, so the leaking fit read as post-split and the
    # real leak slipped the hard gate. The split FUNCTION must still anchor the boundary.
    r = code_leakage_scan(
        'cols = header.split(",")\nscaler.fit(X)\nX_tr, X_te = train_test_split(X_scaled, y)')
    assert r["leak"] and any(f["signal"] == "fit_before_split" for f in r["flags"]), r


def test_fit_before_common_cv_splitter_instantiations_is_flagged():
    # Anchoring on the splitter CLASS instantiation (not a bare `.split(`) keeps recall for the common
    # sklearn splitters whose boundary the old bare-`.split(` used to catch via their `.split()` call.
    for splitter in ("TimeSeriesSplit(5)", "GroupKFold(5)", "StratifiedShuffleSplit(5)", "LeaveOneOut()"):
        r = code_leakage_scan(f"scaler.fit(X)\ncv = {splitter}\nfor a, b in cv.split(X):\n    pass\n")
        assert r["leak"] and any(f["signal"] == "fit_before_split" for f in r["flags"]), (splitter, r)


def test_clean_pipeline_no_flags():
    code = (
        "X_train, X_test, y_train, y_test = train_test_split(X, y)\n"
        "scaler = StandardScaler()\n"
        "X_train = scaler.fit_transform(X_train)\n"   # fit AFTER split, on train only -> clean
        "X_test = scaler.transform(X_test)\n"
    )
    r = code_leakage_scan(code)
    assert not r["leak"], r["flags"]


def test_empty_code():
    assert code_leakage_scan("")["leak"] is False


def test_pearson_ragged_columns_still_correlate():
    # a near-perfect proxy that is one row short must NOT silently read as 0.0 (which hides the leak)
    assert abs(_pearson([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0])) > 0.99


# --- eval_set early-stopping: waive a benign val monitor, but still catch a TEST monitor -----------
# (regression for the max-effort review: _FIT_RE truncates at the first ')', so a test tuple in a
# SECOND eval_set entry must be caught by a line-level scan, not the truncated fit-arg capture.)

def test_eval_set_val_monitor_is_not_a_leak():
    r = code_leakage_scan("model.fit(X_train, y_train, eval_set=[(X_val, y_val)])")
    assert not r["leak"], r


def test_eval_set_test_monitor_is_a_leak():
    for code in ("model.fit(X_train, y_train, eval_set=[(X_test, y_test)])",
                 "model.fit(X_train, y_train, eval_set=[(X_val, y_val), (X_test, y_test)])",
                 "model.fit(X_train, y_train, validation_data=(X_test, y_test))"):
        r = code_leakage_scan(code)
        assert r["leak"] and any(f["signal"] == "fit_on_test" for f in r["flags"]), code


def test_plain_fit_on_val_still_flags():
    assert code_leakage_scan("scaler.fit(X_val)")["leak"]


# --- token-anchored fit-arg match: an identifier that merely CONTAINS `val`/`test` is not a leak ---
# (regression for the architecture review H2: the bare-substring `in` test flagged X_trainval /
#  X_latest / X_interval as fit_on_test, and under trust_gate=gate/block that silently barred an
#  honest refit-on-train+validation solution from selection and breeding.)

def test_refit_on_trainval_is_not_a_leak():
    # the standard non-leaking refit on train+validation after CV
    for code in ("model.fit(X_trainval, y_trainval)",
                 "pipe.fit(X_interval, y_interval)",
                 "clf.fit(X_latest, y_latest)",
                 "est.fit(retrieval_features, labels)"):
        r = code_leakage_scan(code)
        assert not any(f["signal"] == "fit_on_test" for f in r["flags"]), code


def test_true_val_test_fits_still_flag_after_anchoring():
    for code in ("scaler.fit(X_val)", "scaler.fit(X_test)", "m.fit(x_valid, y)",
                 "m.fit(X_testing, y)", "m.fit(y_test_final, z)"):
        r = code_leakage_scan(code)
        assert any(f["signal"] == "fit_on_test" for f in r["flags"]), code


def test_file_metric_reader_confined_to_workdir(tmp_path):
    # an absolute / traversal `path` in a metric spec must not escape the workdir (answer-key read)
    from looplab.runtime.command_eval import read_metric
    (tmp_path / "m.json").write_text('{"metric": 0.5}')
    assert read_metric("", str(tmp_path), {"kind": "file_json", "path": "m.json", "key": "metric"}) == 0.5
    assert read_metric("", str(tmp_path), {"kind": "file_json", "path": "/etc/passwd"}) is None
    assert read_metric("", str(tmp_path), {"kind": "file_json", "path": "../../secret.json"}) is None


# ------------------------------------------------------ P1-7: precision, multiline, second-fit, NaN
def test_benign_values_names_are_not_flagged():
    from looplab.trust.leakage import code_leakage_scan
    for code in ("scaler.fit(train_values, y)", "m.fit(values)", "m.fit(X_train, y_train)",
                 "model.fit(X, y, validation_split=0.2)", "clf.fit(feature_values, labels)"):
        r = code_leakage_scan(code)
        assert not any(f["signal"] == "fit_on_test" for f in r["flags"]), code


def test_second_fit_on_same_line_is_flagged():
    from looplab.trust.leakage import code_leakage_scan
    r = code_leakage_scan("m1.fit(X_train, y_train); m2.fit(X_test, y_test)")
    assert any(f["signal"] == "fit_on_test" for f in r["flags"])


def test_multiline_fit_on_test_is_flagged():
    from looplab.trust.leakage import code_leakage_scan
    r = code_leakage_scan("model.fit(\n    X_test,\n    y_test,\n)")
    assert any(f["signal"] == "fit_on_test" for f in r["flags"])


def test_informal_valset_testset_names_are_flagged():
    """Review of P1-7: the common informal `valset`/`testset` held-out names (token + `set`, no
    separator) must stay flagged — the old `(?![a-z])` boundary dropped them; the token set now lists
    them explicitly."""
    from looplab.trust.leakage import code_leakage_scan
    for code in ("clf.fit(valset, y)", "m.fit(testset)", "m.fit(x_valset, y)", "m.fit(X_testset, y)"):
        assert any(f["signal"] == "fit_on_test" for f in code_leakage_scan(code)["flags"]), code
    # `trainset` is benign (not a held-out set) and must NOT flag
    assert not any(f["signal"] == "fit_on_test"
                   for f in code_leakage_scan("m.fit(trainset, y)")["flags"])


def test_pearson_ignores_nan_rows_and_still_flags_a_proxy():
    from looplab.trust.leakage import _pearson, target_leakage
    # a near-perfect proxy with ONE NaN row must still correlate (not collapse to NaN -> hidden)
    feat = [1.0, 2.0, 3.0, float("nan"), 5.0]
    tgt = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert abs(_pearson(feat, tgt)) > 0.99
    v = target_leakage({"proxy": feat}, tgt)
    assert v["leak"] and "proxy" in v["flagged"]


def test_fit_before_split_anchors_on_cv_drivers_not_only_instantiation():
    """`split_at is None` silently disables the whole HARD fit_before_split branch for a file.

    Anchoring only on splitter INSTANTIATION missed two very common shapes — a splitter passed in as
    a parameter, and an INTEGER cv (`cross_val_score(m, X, y, cv=5)`, no splitter object at all) — so
    a genuine full-data `scaler.fit(X)` before `cv.split(X, y)` scanned CLEAN and the node could win
    selection under trust_gate=gate|block. The `str.split()` false positive the class anchors exist
    for must stay clean.
    """
    from looplab.trust.leakage import code_leakage_scan

    def flags(code):
        return {f["signal"] for f in code_leakage_scan(code)["flags"]}

    assert "fit_before_split" in flags(
        "def run(cv, X, y):\n    scaler.fit(X)\n    for tr, va in cv.split(X, y):\n        pass")
    assert "fit_before_split" in flags(
        "scaler.fit(X)\nscores = cross_val_score(model, X, y, cv=5)")

    assert flags('parts = "a,b".split(",")\nscaler.fit(X_train)') == set()
    assert flags("name = line.split()\nmodel.fit(X_train, y_train)") == set()


def test_str_split_receiver_names_do_not_anchor_a_cv_boundary():
    """`ss`/`kf`/`cv` are ordinary STRING variable names, not just splitter names.

    The receiver alternation was keyed on the name alone, so an honest solution whose `X` already IS
    the training split got a HARD `fit_before_split` purely because a later line parsed a header
    through a variable called `ss` — and under `trust_gate='gate'`/`'block'` that silently excludes an
    honest winner from selection and confirmation. A real splitter's `.split()` takes an array
    identifier; `str.split()` takes a quoted separator or nothing, which is the discriminator.
    """
    from looplab.trust.leakage import code_leakage_scan

    def signals(src):
        return {f["signal"] for f in code_leakage_scan(src).get("flags", [])}

    honest_sep = ('X = pd.read_csv("train.csv").drop(columns=["y"])\n'
                  'y = 1\nscaler.fit(X)\nss = header_line\ncols = ss.split(",")\n')
    honest_bare = ('X = load()\ny = 1\nscaler.fit(X)\nkf = line\nparts = kf.split()\n')
    assert "fit_before_split" not in signals(honest_sep), (
        "a plain str.split() through a two-letter variable name still anchors a CV boundary")
    assert "fit_before_split" not in signals(honest_bare)

    # The recall the receiver rule exists for is unchanged: a real splitter call still anchors.
    for real in ('X = load()\ny = load_y()\nscaler.fit(X)\nfor a, b in cv.split(X, y):\n    pass\n',
                 'X = load()\ny = load_y()\nscaler.fit(X)\nfor a, b in skf.split(X, y):\n    pass\n',
                 'X = load()\ny = load_y()\nscaler.fit(X)\na, b = train_test_split(X, y)\n'):
        assert "fit_before_split" in signals(real), real


def test_line_wrapping_cannot_hide_a_test_monitor():
    """Cosmetic wrapping flipped a genuinely leaking fit from leak=True to leak=False.

    The TEST-monitor tell was scanned only against the fit's FIRST source line, so
    `.fit(X, y, eval_set=[\\n  (X_test, y_test)\\n])` escaped both `arg` (`_FIT_RE`'s `[^)]*`
    truncates at the first `)`) and the line-level check. Under trust_gate gate/block that kept a
    fit-on-test node eligible to win, breed and confirm — and unlike this module's other misses it
    was never listed among the ACCEPTED RECALL GAP notes. The line scan still has to stay: it is what
    catches a test tuple in a SECOND eval_set entry, which `arg` truncates away.
    """
    leaking = [
        "model.fit(X_train, y_train, eval_set=[(X_test, y_test)])\n",
        "model.fit(X_train, y_train, eval_set=[\n    (X_test, y_test)\n])\n",
        "model.fit(X, y, eval_set=[(X_val, y_val), (X_test, y_test)])\n",
        "model.fit(\n    X_train, y_train,\n    eval_set=[\n        (X_test, y_test),\n    ],\n)\n",
    ]
    for source in leaking:
        result = code_leakage_scan(source)
        assert result["leak"] is True, source
        assert "fit_on_test" in [f["signal"] for f in result["flags"]], source

    # Precision is unchanged: ordinary early stopping on a VALIDATION set is not leakage, wrapped or
    # not — hard-gating it would exclude every early-stopping solution.
    clean = [
        "model.fit(X_train, y_train, eval_set=[(X_val, y_val)])\n",
        "model.fit(X_train, y_train, eval_set=[\n    (X_val, y_val)\n])\n",
        "model.fit(X_train, y_train)\n",
    ]
    for source in clean:
        assert code_leakage_scan(source)["leak"] is False, source


# ------------------------------------------------------------------ multi-test selection (doc 52 row 22)
# LeakageDetector 2.0's third class: nothing is FITTED on the test split, it is evaluated N times and
# the answer chosen. Every positive below is the shape an agent actually writes; every negative is
# the intended protocol (select on VALIDATION, evaluate on test once) or a loop that touches the
# test split without choosing on it. Both halves are required, so each negative drops exactly one.

def _multi(code):
    return [f for f in code_leakage_scan(code)["flags"] if f["signal"] == "multi_test"]


def test_a_grid_loop_scored_on_the_test_split_and_kept_by_best_is_multi_test():
    code = ("best_acc, best_model = 0, None\n"
            "for C in [0.1, 1, 10]:\n"
            "    model = LogisticRegression(C=C).fit(X_train, y_train)\n"
            "    acc = accuracy_score(y_test, model.predict(X_test))\n"
            "    if acc > best_acc:\n"
            "        best_acc, best_model = acc, model\n")
    flags = _multi(code)
    assert len(flags) == 1 and flags[0]["line"] == 2, flags
    assert flags[0]["evaluations"] == 1, "the nested predict is part of ONE evaluation, not a second"
    assert flags[0]["selection"] == "acc > best_acc"


def test_scores_collected_then_maxed_is_multi_test():
    code = ("scores = []\n"
            "for depth in range(2, 10):\n"
            "    clf = DecisionTreeClassifier(max_depth=depth).fit(X_train, y_train)\n"
            "    scores.append(clf.score(X_test, y_test))\n"
            "best_depth = 2 + scores.index(max(scores))\n")
    assert [f["selection"] for f in _multi(code)] == ["max(scores)"]


def test_a_checkpoint_loop_scored_into_a_dict_and_chosen_is_multi_test():
    code = ("results = {}\n"
            "for ckpt in sorted(glob.glob('ckpt-*')):\n"
            "    results[ckpt] = score_checkpoint(ckpt, X_test, y_test)\n"
            "best_ckpt = max(results, key=results.get)\n")
    assert [f["selection"] for f in _multi(code)] == ["max(results, key=results.get)"]


def test_three_models_unrolled_and_maxed_is_multi_test():
    code = ("a1 = accuracy_score(y_test, m1.predict(X_test))\n"
            "a2 = accuracy_score(y_test, m2.predict(X_test))\n"
            "a3 = accuracy_score(y_test, m3.predict(X_test))\n"
            "winner = max(a1, a2, a3)\n")
    flags = _multi(code)
    assert len(flags) == 1 and flags[0]["evaluations"] == 3 and flags[0]["line"] == 1


def test_selecting_on_validation_and_evaluating_the_test_split_once_is_the_intended_protocol():
    code = ("best_acc = 0\n"
            "for C in [0.1, 1, 10]:\n"
            "    model = LogisticRegression(C=C).fit(X_train, y_train)\n"
            "    acc = accuracy_score(y_val, model.predict(X_val))\n"
            "    if acc > best_acc:\n"
            "        best_acc, best_model = acc, model\n"
            "print(accuracy_score(y_test, best_model.predict(X_test)))\n")
    assert _multi(code) == []


def test_a_test_evaluation_that_is_only_logged_is_not_selection():
    code = ("for epoch in range(5):\n"
            "    train(model)\n"
            "    print('test acc', accuracy_score(y_test, model.predict(X_test)))\n")
    assert _multi(code) == []


def test_bagging_test_predictions_and_a_cv_grid_are_not_multi_test():
    bagging = ("preds = []\n"
               "for seed in range(5):\n"
               "    m = RandomForestClassifier(random_state=seed).fit(X_train, y_train)\n"
               "    preds.append(m.predict_proba(X_test))\n"
               "final = np.mean(preds, axis=0)\n")
    assert _multi(bagging) == [], "averaging test predictions chooses nothing"
    cv = ("scores = []\n"
          "for C in [0.1, 1]:\n"
          "    scores.append(cross_val_score(LogisticRegression(C=C), X, y, cv=5).mean())\n"
          "best_C = [0.1, 1][int(np.argmax(scores))]\n")
    assert _multi(cv) == [], "cross-validation scores no test split"
    single = ("X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)\n"
              "model.fit(X_train, y_train)\nprint(model.score(X_test, y_test))\n")
    assert _multi(single) == [], "one evaluation is the protocol; `test_size` is not a test split"


def test_the_concatenated_scan_surface_is_scanned_part_by_part():
    """`_trust_scan_surface` joins every node file under `# --- <name> ---` markers; two modules
    with their own `from __future__` imports do not parse as one, and a SyntaxError must not turn
    the whole scan blind (or a finding in a later file invisible)."""
    surface = ("from __future__ import annotations\nx = 1\n\n\n# --- helper.py ---\n"
               "from __future__ import annotations\n"
               "scores = []\nfor d in range(3):\n    scores.append(clf.score(X_test, y_test))\n"
               "best = max(scores)\n")
    flags = _multi(surface)
    assert len(flags) == 1 and flags[0]["line"] == 8, flags
    assert code_leakage_scan("def broken(:\n")["flags"] == [], "an unparseable surface is unscanned, never a finding"
