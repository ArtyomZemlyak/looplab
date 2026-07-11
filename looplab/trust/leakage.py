"""Leakage detectors (I9, ADR-15) — the differentiator, pure-Python (no deps).

No library models ML-pipeline leakage, so these are custom. Each returns a small
dict so verdicts attach to nodes/events. Datasets are plain dict[col, list] +
explicit row lists / timestamp lists — adapter-agnostic.
"""
from __future__ import annotations

import re
from typing import Sequence


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    # Compare the overlapping prefix when columns are ragged (a mismatched slice) rather than silently
    # returning 0.0 — returning 0 would HIDE a near-perfect proxy that happens to be one row short.
    # DROP non-finite PAIRS (a stray NaN/inf in either column) instead of letting them propagate: a NaN
    # anywhere poisons cov/var to NaN, and `abs(NaN) >= threshold` is False, so a leaking feature with a
    # single NaN row would slip through the hard gate (arch-review §4 P1-7). Dropping the pair keeps the
    # correlation on the clean rows, so the proxy is still caught.
    import math
    n0 = min(len(a), len(b))
    pairs: list[tuple[float, float]] = []
    for x, y in zip(list(a)[:n0], list(b)[:n0]):
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fx) and math.isfinite(fy):
            pairs.append((fx, fy))
    n = len(pairs)
    if n < 3:   # a 2-point overlap is always perfectly collinear -> meaningless |r|==1.0 against the gate
        return 0.0
    ax = [p[0] for p in pairs]
    bx = [p[1] for p in pairs]
    ma, mb = sum(ax) / n, sum(bx) / n
    cov = sum((x - ma) * (y - mb) for x, y in pairs)
    va = sum((x - ma) ** 2 for x in ax)
    vb = sum((y - mb) ** 2 for y in bx)
    if va == 0.0 or vb == 0.0:
        return 0.0
    r = cov / (va * vb) ** 0.5
    return r if math.isfinite(r) else 0.0


def train_test_contamination(train_rows: list, test_rows: list) -> dict:
    """Detect identical rows shared between train and test splits."""
    train = {tuple(r) for r in train_rows}
    dups = [r for r in test_rows if tuple(r) in train]
    frac = len(dups) / len(test_rows) if test_rows else 0.0
    return {"detector": "train_test_contamination",
            "leak": len(dups) > 0, "duplicates": len(dups), "fraction": round(frac, 6)}


def target_leakage(features: dict[str, list[float]], target: list[float],
                   threshold: float = 0.98) -> dict:
    """Flag feature columns near-perfectly correlated with the target (a proxy/leak)."""
    flagged = {}
    for name, col in features.items():
        r = _pearson(col, target)
        if abs(r) >= threshold:
            flagged[name] = round(r, 6)
    return {"detector": "target_leakage", "leak": bool(flagged),
            "threshold": threshold, "flagged": flagged}


_FIT_RE = re.compile(r"\.(fit|fit_transform)\s*\(([^)]*)\)")
_SPLIT_RE = re.compile(r"train_test_split\s*\(|KFold|StratifiedKFold|\.split\s*\(")
# The early-stopping monitor kwarg (split off from the fit ARGS so a benign eval_set on VALIDATION
# isn't read as fit-on-validation) and the TEST-monitor tell. The monitor tell matches the substring
# `test` (NOT `\bx_test\b`): a suffixed name like `X_test_scaled`/`X_testing`/`y_test_final` has no
# word boundary after `test`, so the anchored form silently missed a real test-set monitor — while a
# validation-named monitor (`X_val`, `X_holdout`) never contains `test`, so the substring stays safe.
# Benign monitor/holdout kwargs split off the fit ARGS so they don't read as fit-on-val/test:
# eval_set / validation_data / eval_names carry a `(X_val, y_val)` monitor tuple (early stopping, not
# leakage), and `validation_split=0.1` is Keras internally holding out a fraction of the TRAINING data
# (also not leakage). Without validation_split here its `validation` token false-flagged every Keras
# fit as fit-on-val (arch-review §4 P1-7).
_EVALSET_KW_RE = re.compile(r"\b(?:eval_set|validation_data|validation_split|eval_names)\s*=")
_TEST_MONITOR_RE = re.compile(r"\b(?:eval_set|validation_data|eval_names)\s*=[^=]*test")
# Fit-on-val/test detector: match a WHOLE held-out token — one of {val, valid, validation, test,
# testing} — bounded on BOTH sides (not preceded by a letter, not FOLLOWED by a letter). The trailing
# `(?![a-z])` is what fixes the P1-7 false positives: `values`/`train_values` are `val`+`ues` and
# `validation_split` is handled as a kwarg above, so none hard-gate an honest node any more. The token
# SET (not a bare `val`/`test` prefix) is what keeps the true positives the old anchor caught —
# `x_valid` (`valid`), `x_testing` (`testing`), `y_test_final` (`test`) — flagged. Benign words that
# merely CONTAIN the letters — `x_trainval`, `x_interval`, `x_latest`, `eval`, `retrieval`, `contest`,
# `values` — do NOT match (the letter before/after breaks the boundary).
# ACCEPTED RECALL GAP (precision-over-recall, on purpose): a NO-separator held-out name — `Xtest`,
# `Xval` (lowercased -> `xtest`/`xval`) — is NOT flagged, because anchoring cannot tell `xtest` (a
# leak) from `contest` (benign) without a name whitelist. A false NEGATIVE is far less harmful than a
# false positive that silently kills an honest winner on a hard gate; sklearn convention is
# overwhelmingly the separated `X_test`/`X_val`, which IS caught.
_LEAKY_FIT_ARG_RE = re.compile(r"(?<![a-z])(?:validation|valid|testing|test|val)(?![a-z])")


def code_leakage_scan(code: str) -> dict:
    """I3 data-centric: static-dataflow-lite scan of solution CODE for train->test information flow
    (beyond exact-row contamination). Flags the classic anti-patterns: fitting a preprocessor on the
    FULL data before the split, and calling .fit() on test data. Heuristic + dependency-free.

    NOTE on gating: under `trust_gate='audit'` (the default) these flags are advisory — surfaced to
    the operator and the agent only. But the engine emits them as `data_leakage:<signal>` signals,
    which `is_hard_signal` treats as HARD — so under `trust_gate='gate'`/`'block'` a flagged node is
    excluded from best-selection AND from breeding/confirmation (`_apply_trust_gate`). The fit-arg
    match is therefore token-anchored (see `_LEAKY_FIT_ARG_RE`) so a benign identifier can't hard-gate
    an honest solution. Keep this scan's precision high whenever it feeds a non-audit trust gate."""
    flags: list[dict] = []
    lines = code.splitlines()
    split_at = next((i for i, l in enumerate(lines) if _SPLIT_RE.search(l)), None)
    # finditer over the FULL code (not per-line via `.search`): `.search` returned only the FIRST fit on
    # a line and could not see an argument that spans lines, so `model.fit(X_train); m2.fit(X_test)` and
    # a multiline `.fit(\n  X_test\n)` both slipped through (arch-review §4 P1-7). `_FIT_RE`'s `[^)]*`
    # already matches newlines, so a full-code finditer catches every fit and multiline args; the line
    # number is derived from the match offset.
    for m in _FIT_RE.finditer(code):
        arg = m.group(2).lower()
        line_i = code.count("\n", 0, m.start())          # 0-based line index of the fit
        snippet = (lines[line_i].strip()[:90] if line_i < len(lines) else m.group(0).strip()[:90])
        # Split off the EARLY-STOPPING monitor kwargs: `.fit(X_train, y_train, eval_set=[(X_val,
        # y_val)])` is the standard LightGBM/XGBoost call, NOT leakage — the `val` inside `eval_set`
        # would else read as fit-on-validation and hard-gate every early-stopping solution. `head` = the
        # fit args BEFORE the monitor kwarg (a plain `.fit(X_val,y_val)` has no kwarg → its `val` stays
        # flagged). The TEST-monitor check scans the fit's SOURCE LINE, not `arg`: `_FIT_RE`'s `([^)]*)`
        # truncates at the first `)`, so a test tuple in a SECOND eval_set entry
        # (`eval_set=[(X_val,y_val),(X_test,y_test)]`) never reaches `arg` — the line-level scan sees it.
        head = _EVALSET_KW_RE.split(arg, maxsplit=1)[0]
        line_src = lines[line_i].lower() if line_i < len(lines) else m.group(0).lower()
        test_monitor = _TEST_MONITOR_RE.search(line_src)
        if (_LEAKY_FIT_ARG_RE.search(head)                             # val/test token in the fit args = leak
                or test_monitor):                                       # test INSIDE the monitor = leak
            flags.append({"signal": "fit_on_test", "line": line_i + 1, "code": snippet})
        elif split_at is not None and line_i < split_at and "train" not in arg:
            # a fit/fit_transform on (apparently full) data BEFORE the split leaks test statistics
            flags.append({"signal": "fit_before_split", "line": line_i + 1, "code": snippet})
    return {"detector": "code_leakage", "leak": bool(flags), "flags": flags}


def temporal_leakage(train_timestamps: list[float], test_timestamps: list[float]) -> dict:
    """For a forward (train-before-test) split, flag train rows at/after the test
    cutoff — i.e. training on future information."""
    if not train_timestamps or not test_timestamps:
        return {"detector": "temporal_leakage", "leak": False, "overlap": 0}
    cutoff = min(test_timestamps)
    overlap = sum(1 for t in train_timestamps if t >= cutoff)
    return {"detector": "temporal_leakage", "leak": overlap > 0,
            "cutoff": cutoff, "overlap": overlap}
