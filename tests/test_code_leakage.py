"""I3 static code-leakage scan."""
from __future__ import annotations

from looplab.trust.leakage import code_leakage_scan


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
