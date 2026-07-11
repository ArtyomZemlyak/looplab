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


def test_pearson_ignores_nan_rows_and_still_flags_a_proxy():
    from looplab.trust.leakage import _pearson, target_leakage
    # a near-perfect proxy with ONE NaN row must still correlate (not collapse to NaN -> hidden)
    feat = [1.0, 2.0, 3.0, float("nan"), 5.0]
    tgt = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert abs(_pearson(feat, tgt)) > 0.99
    v = target_leakage({"proxy": feat}, tgt)
    assert v["leak"] and "proxy" in v["flagged"]
