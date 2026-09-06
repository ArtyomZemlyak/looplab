"""Leakage detectors (I9, ADR-15) — the differentiator, pure-Python (no deps).

No library models ML-pipeline leakage, so these are custom. Each returns a small
dict so verdicts attach to nodes/events. Datasets are plain dict[col, list] +
explicit row lists / timestamp lists — adapter-agnostic.
"""
from __future__ import annotations

import ast
import re
from typing import Sequence


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    # Compare the overlapping prefix when columns are ragged (a mismatched slice) rather than silently
    # returning 0.0 — returning 0 would HIDE a near-perfect proxy that happens to be one row short.
    # DROP non-finite PAIRS (a stray NaN/inf in either column) instead of letting them propagate: a NaN
    # anywhere poisons cov/var to NaN, and `abs(NaN) >= threshold` is False, so a leaking feature with a
    # single NaN row would slip through the hard gate (arch-review §4 P1-7). Dropping the pair keeps the
    # correlation on the clean rows, so the proxy is still caught.
    # THE CLEANING IS `_finite_pairs` BELOW, called rather than repeated. The docstring there says
    # the rule was "hoisted ... so the two coefficients can never disagree about which ROWS they
    # describe" — and this copy was left standing forty lines above it, so the rule was written
    # twice in one file and the guarantee was false the day it was made. `target_leakage` flags on
    # `_pearson` OR `_spearman`, and this gate can abort a run.
    import math
    pairs = _finite_pairs(a, b)
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
    # ABSTAIN on rows this comparison cannot express, rather than raising out of the detector: a
    # JSON-shaped dataset has nested list/dict cells, which are unhashable, and every other detector
    # here degrades gracefully on input it cannot read. Abstaining is also the honest answer — an
    # un-comparable row is not evidence of no contamination, so it reports `checked=False`.
    def _key(row):
        return tuple(x if x.__hash__ is not None else repr(x) for x in row)

    try:
        train = {_key(r) for r in train_rows}
        dups = [r for r in test_rows if _key(r) in train]
    except TypeError:                     # a row that is not even iterable into cells
        return {"detector": "train_test_contamination", "leak": False, "duplicates": 0,
                "fraction": 0.0, "checked": False,
                "reason": "rows are not comparable as tuples of cells"}
    frac = len(dups) / len(test_rows) if test_rows else 0.0
    return {"detector": "train_test_contamination",
            "leak": len(dups) > 0, "duplicates": len(dups), "fraction": round(frac, 6)}


def _finite_pairs(a: Sequence[float], b: Sequence[float]) -> list[tuple[float, float]]:
    """The cleaned (x, y) pairs both correlations are computed over — `_pearson`'s own rule, hoisted.

    Extracted rather than re-derived so the two coefficients can never disagree about which ROWS
    they describe: a leak flagged by one and cleared by the other, on different row sets, is not a
    comparison an operator can act on.
    """
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
    return pairs


def _ranks(values: Sequence[float]) -> list[float]:
    """Fractional (tie-averaged) ranks. Ties MUST share a rank: assigning them arbitrary distinct
    ranks would make a constant column look perfectly ordered against anything."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = shared
        i = j + 1
    return out


def _spearman(a: Sequence[float], b: Sequence[float]) -> float:
    """Rank correlation — `_pearson` over the tie-averaged ranks of the same cleaned pairs.

    WHY IT IS HERE AT ALL. `target_leakage` was one Pearson coefficient, so a feature that IS the
    target under any monotone transform — `y**3`, `log(y)`, `exp(y)`, a rank-preserving rescale,
    a quantile bucket — measured well below the threshold and the gate reported CLEAN. That is
    worse than having no detector: this gate can abort a run, so the operator reads silence as
    assurance, and the whole point of the leakage family is that it is the differentiator.

    This is not a NEW judgment, it is the SAME judgment made robust to a reparameterization. A
    column ranking ~identically to the target is the answer written in different units, exactly as a
    column correlating ~1.0 with it is; so it shares the threshold rather than getting one nobody
    has calibrated. The residue is stated and not guessed at: a NON-MONOTONE leak (`y**2` about a
    symmetric mean, a categorical id that maps to the label) is invisible to both coefficients, and
    the rung that would see it has false positives — a binary feature perfectly predicting a binary
    target is routine and legitimate — so it needs a measurement before it can gate anything.
    """
    pairs = _finite_pairs(a, b)
    if len(pairs) < 3:
        return 0.0
    return _pearson(_ranks([p[0] for p in pairs]), _ranks([p[1] for p in pairs]))


def target_leakage(features: dict[str, list[float]], target: list[float],
                   threshold: float = 0.98) -> dict:
    """Flag feature columns near-perfectly correlated with the target (a proxy/leak).

    BOTH a linear and a RANK coefficient, and either alone flags — see `_spearman` for why the rank
    rung exists and what it deliberately still cannot see. `flagged` keeps the same
    `{name: coefficient}` shape every existing reader expects, carrying whichever coefficient is
    larger in magnitude; `flagged_detail` is additive and says which rung fired, so an operator
    looking at an abort can tell "this column is the target" from "this column is a monotone
    function of the target" without re-deriving anything.
    """
    flagged: dict[str, float] = {}
    detail: dict[str, dict] = {}
    for name, col in features.items():
        r = _pearson(col, target)
        rho = _spearman(col, target)
        if abs(r) >= threshold or abs(rho) >= threshold:
            flagged[name] = round(r if abs(r) >= abs(rho) else rho, 6)
            detail[name] = {"pearson": round(r, 6), "spearman": round(rho, 6),
                            "rung": "linear" if abs(r) >= threshold else "monotone"}
    return {"detector": "target_leakage", "leak": bool(flagged),
            "threshold": threshold, "flagged": flagged, "flagged_detail": detail}


_FIT_RE = re.compile(r"\.(fit|fit_transform)\s*\(([^)]*)\)")
# Anchor the train/test boundary on the split FUNCTION or a CV-splitter INSTANTIATION — never a bare
# `.split(`. Two reasons each alternative is a CALL/USE site, not a bare name:
#   * `KFold`/`StratifiedKFold` without `\s*\(` matched their own IMPORT line
#     (`from sklearn.model_selection import KFold`), which — being at the top of the file — set
#     `split_at` to line 0 and silently disabled fit_before_split for the WHOLE solution.
#   * a BARE `\.split\s*\(` (once used to catch `cv.split(X)`) collides with Python's ubiquitous
#     `str.split(...)` — any `path.split("/")`, `header.split(",")`, tokenizer, log parse — so an
#     unrelated string split corrupted the boundary anchor, BOTH hard-gating an honest full-data fit as
#     `fit_before_split` (false positive) AND suppressing a real fit-before-split leak placed after a
#     benign string split (false negative). Anchor on the splitter CLASS instantiation instead: it is the
#     robust boundary for the common sklearn splitters (KFold/Stratified/Group/Repeated *KFold*, the
#     *ShuffleSplit* family, TimeSeriesSplit, PredefinedSplit, LeaveOneOut/LeavePOut). ACCEPTED RECALL
#     GAP (precision-over-recall, on purpose): a PRE-BUILT splitter passed in and only used via
#     `cv.split(X)`, with no instantiation in the scanned code, no longer anchors a boundary — far better
#     than hard-gating every solution that calls `str.split()`.
#     NARROWED GAP (review follow-up): "no instantiation in the scanned code" is not rare — a splitter
#     passed in as a parameter, built by a factory, or an INTEGER cv (`cross_val_score(m, X, y, cv=5)`,
#     an extremely common form with no splitter object at all) all left `split_at = None`, which
#     silently disables the whole HARD `fit_before_split` branch for that file — a genuine full-data
#     `scaler.fit(X)` before `cv.split(X, y)` scanned CLEAN. So anchor additionally on the CV-driver
#     calls and on a `.split(` whose RECEIVER is a conventional splitter name. Neither can match the
#     `str.split()` collision the class anchors were introduced to avoid: `"a,b".split(",")` has no
#     cross_val*/check_cv token, and the receiver alternation additionally requires the FIRST
#     ARGUMENT to be an identifier: `cv.split(X, y)` matches, `ss.split(",")` and a bare
#     `ss.split()` do not. That argument test is what makes the SHORT names safe — `ss`, `kf` and
#     `cv` are also ordinary string-variable names, and keying on the receiver alone hard-gated an
#     honest `scaler.fit(X)` whenever a later line parsed a header with one of them.
_SPLIT_RE = re.compile(
    r"train_test_split\s*\("
    r"|[A-Za-z]*KFold\s*\("
    r"|[A-Za-z]*ShuffleSplit\s*\("
    r"|TimeSeriesSplit\s*\(|PredefinedSplit\s*\("
    r"|Leave[A-Za-z]*Out\s*\("
    r"|cross_val\w*\s*\(|check_cv\s*\("
    r"|\b(?:cv|cvs|skf|skfold|kf|kfold|sss|gss|ss|splitter|folds?)"
    r"\s*\.\s*split\s*\(\s*(?![\"\'\)])"
)
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
# Fit-on-val/test detector: match a WHOLE held-out token — one of {val, valid, validation, valset,
# test, testing, testset} — bounded on BOTH sides (not preceded by a letter, not FOLLOWED by a
# letter). The trailing `(?![a-z])` is what fixes the P1-7 false positives: `values`/`train_values`
# are `val`+`ues` and `validation_split` is handled as a kwarg above, so none hard-gate an honest node
# any more. The token SET (not a bare `val`/`test` prefix) is what keeps the true positives the old
# anchor caught — `x_valid` (`valid`), `x_testing` (`testing`), `y_test_final` (`test`), and the
# common informal `valset`/`testset`/`x_valset` — flagged. Benign words that merely CONTAIN the
# letters — `x_trainval`, `x_interval`, `x_latest`, `eval`, `retrieval`, `contest`, `values` — do NOT
# match (the letter before/after breaks the boundary).
# ACCEPTED RECALL GAP (precision-over-recall, on purpose): a NO-separator, NON-listed held-out name —
# `Xtest`, `Xval`, `trainset` (lowercased) — is NOT flagged, because anchoring cannot tell `xtest` (a
# leak) from `contest` (benign) without a name whitelist, and only the concrete `valset`/`testset`
# suffixes are enumerated. A false NEGATIVE is far less harmful than a false positive that silently
# kills an honest winner on a hard gate; sklearn convention is overwhelmingly the separated
# `X_test`/`X_val`, which IS caught.
_LEAKY_FIT_ARG_RE = re.compile(
    r"(?<![a-z])(?:validation|valset|valid|testset|testing|test|val)(?![a-z])")


# ------------------------------------------------------------------ multi-test selection
# LeakageDetector 2.0's third class (doc 52 row 22): REPEATED evaluation against the same TEST split
# followed by SELECTION on those scores — a grid loop that scores every configuration on `X_test`
# and keeps the best, three models scored on the test set and `max()`ed, an epoch loop that
# `evaluate()`s on the test loader and keeps the best epoch. Every single evaluation in it is
# legitimate on its own, which is why `fit_on_test` cannot see it: nothing is FITTED on the test
# split, it is merely asked N times and the answer chosen. On `repo_task` the candidate's own scorer
# IS that split, so a checkpoint loop that scores each checkpoint and picks the winner is this class
# exactly. AST-based and dependency-free like the rest of the file, and precision-over-recall for the
# same reason: `data_leakage:*` is HARD under `trust_gate=gate/block`, so a flag needs BOTH halves —
# ≥1 test-scored evaluation INSIDE a loop (or ≥2 unrolled at one block) AND a selection tell over
# those scores (a `max`/`argmax`/`sorted` over the collected scores, or a `> best` comparison that
# keeps a winner). A loop that scores on a VALIDATION split is the intended protocol and is never
# flagged (the token set is `test`/`testset`/`testing` only, bounded like `_LEAKY_FIT_ARG_RE`); a loop
# that evaluates on the test split and only LOGS it is not selection and is not flagged either.
# ACCEPTED RECALL GAP: a selection made on a NAME the scan cannot tie to the scoring call (scores
# written to a file and re-read, or chosen by a hand-typed literal index) is invisible here.
_SURFACE_MARKER_RE = re.compile(r"^# --- .+ ---$", re.M)
_TEST_TOKEN_RE = re.compile(r"(?<![a-z])(?:test|testset|testing)(?![a-z])")
_SCORING_NAME_RE = re.compile(
    r"(?:^|_)(?:score|scores|scoring|evaluate|eval|accuracy|acc|precision|recall|f1|auc|roc_auc|"
    r"rmse|mse|mae|r2|log_loss|logloss|error|metric|metrics|ndcg|mrr|map|recall_at)(?:_|$)"
    r"|^predict(?:_proba)?$")
_SELECTION_NAME_RE = re.compile(r"^(?:max|min|argmax|argmin|sorted|nlargest|nsmallest|idxmax|idxmin)$")
_BEST_NAME_RE = re.compile(r"best|winner|top|chosen|selected|champion", re.I)


def _call_name(call: "ast.Call") -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _test_scored(call: "ast.Call") -> bool:
    """A call that EVALUATES on the test split: a scoring-family name over test-named arguments."""
    if not _SCORING_NAME_RE.search(_call_name(call).lower()):
        return False
    args = " ".join([ast.unparse(a) for a in call.args] + [ast.unparse(k.value) for k in call.keywords])
    return bool(_TEST_TOKEN_RE.search(args.lower()))


def _scored_names(stmts: list, scored: set, collectors: set) -> int:
    """Walk *stmts* (a loop body or a block): bind the names a test-scored call lands in, the
    containers those names are appended/assigned into, and return the evaluation count."""
    n = 0
    nested: set = set()       # calls INSIDE a counted call's arguments — `acc(y_test, m.predict(X_test))` is ONE
    for node in ast.walk(ast.Module(body=stmts, type_ignores=[])):
        if isinstance(node, ast.Call) and _test_scored(node) and id(node) not in nested:
            n += 1
            for inner in ast.walk(node):
                if inner is not node:
                    nested.add(id(inner))
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            value = node.value
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {ast.unparse(t) for t in targets}
            if any(isinstance(c, ast.Call) and _test_scored(c) for c in ast.walk(value)):
                for t in targets:                      # `results[k] = score(...)` collects too
                    if isinstance(t, ast.Subscript):
                        collectors.add(ast.unparse(t.value))
                    else:
                        scored.add(ast.unparse(t))
            elif any(isinstance(c, ast.Name) and c.id in scored for c in ast.walk(value)):
                for t in targets:                      # `results[k] = acc` collects; `x = acc` aliases
                    if isinstance(t, ast.Subscript):
                        collectors.add(ast.unparse(t.value))
                    else:
                        scored.add(ast.unparse(t))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("append", "extend", "add", "insert")
                and any((isinstance(c, ast.Name) and c.id in scored)
                        or (isinstance(c, ast.Call) and _test_scored(c))
                        for a in node.args for c in ast.walk(a))):
            collectors.add(ast.unparse(node.func.value))
    return n


def _selection_tell(tree: "ast.AST", scored: set, collectors: set):
    """The statement that CHOOSES on the scores, or None: a selection call over a scored name or a
    collector, or a comparison of a scored name against a `best`-named value."""
    names = scored | collectors
    if not names:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _SELECTION_NAME_RE.match(_call_name(node)):
            text = " ".join(ast.unparse(a) for a in node.args)
            receiver = ast.unparse(node.func.value) if isinstance(node.func, ast.Attribute) else ""
            if any(re.search(r"(?<![\w.])" + re.escape(nm) + r"(?![\w])", text) or receiver == nm
                   for nm in names):
                return node
        if isinstance(node, ast.Compare):
            sides = [node.left] + list(node.comparators)
            uses_score = any(isinstance(c, ast.Name) and c.id in scored
                             for sd in sides for c in ast.walk(sd))
            if uses_score and any(_BEST_NAME_RE.search(ast.unparse(sd)) for sd in sides):
                return node
    return None


def _surface_parts(code: str) -> list[tuple[int, str]]:
    """The scan surface as (line offset, text) parts: `engine/evaluate.py::_trust_scan_surface`
    concatenates every node file under `# --- <name> ---` markers, and two modules concatenated do
    not parse as one (a second `from __future__` import is a SyntaxError). Each part parses alone."""
    parts, start = [], 0
    lines = code.split("\n")
    for i, line in enumerate(lines):
        if _SURFACE_MARKER_RE.match(line) and i > start:
            parts.append((start, "\n".join(lines[start:i])))
            start = i + 1
    parts.append((start, "\n".join(lines[start:])))
    return parts


def multi_test_scan(code: str) -> list[dict]:
    """Flags for `multi_test`: `{"signal", "line", "code", "evaluations", "selection"}` per site."""
    flags: list[dict] = []
    for offset, text in _surface_parts(code):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue                       # an unparseable part is unscanned, never a finding
        lines = text.split("\n")
        # (1) the loop form: N test-scored evaluations inside one loop, then a selection.
        for loop in ast.walk(tree):
            if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)):
                continue
            scored, collectors = set(), set()
            n = _scored_names(loop.body, scored, collectors)
            if n == 0:
                continue
            tell = _selection_tell(tree, scored, collectors)
            if tell is None:
                continue
            flags.append({"signal": "multi_test", "line": loop.lineno + offset,
                          "code": lines[loop.lineno - 1].strip()[:90], "evaluations": n,
                          "selection": ast.unparse(tell)[:90]})
        # (2) the unrolled form: ≥2 test-scored assignments in one block, then a selection over them.
        for block in ast.walk(tree):
            body = getattr(block, "body", None)
            if not isinstance(body, list) or isinstance(block, (ast.For, ast.AsyncFor, ast.While)):
                continue
            scored, collectors = set(), set()
            sites = [st for st in body if isinstance(st, (ast.Assign, ast.AnnAssign))
                     and st.value is not None
                     and any(isinstance(c, ast.Call) and _test_scored(c) for c in ast.walk(st.value))]
            if len(sites) < 2:
                continue
            _scored_names(body, scored, collectors)
            tell = _selection_tell(tree, scored, collectors)
            if tell is None:
                continue
            first = sites[0]
            flags.append({"signal": "multi_test", "line": first.lineno + offset,
                          "code": lines[first.lineno - 1].strip()[:90], "evaluations": len(sites),
                          "selection": ast.unparse(tell)[:90]})
    # one flag per site, in source order, even when both forms saw the same block
    seen, out = set(), []
    for flag in sorted(flags, key=lambda f: f["line"]):
        if flag["line"] in seen:
            continue
        seen.add(flag["line"])
        out.append(flag)
    return out


def code_leakage_scan(code: str) -> dict:
    """I3 data-centric: static-dataflow-lite scan of solution CODE for train->test information flow
    (beyond exact-row contamination). Flags the classic anti-patterns: fitting a preprocessor on the
    FULL data before the split, calling .fit() on test data, and — since doc 52 row 22 — evaluating on
    the test split REPEATEDLY and selecting on the result (`multi_test`, the rung above). Heuristic +
    dependency-free.

    NOTE on gating: under `trust_gate='audit'` (the default) these flags are advisory — surfaced to
    the operator and the agent only. But the engine emits them as `data_leakage:<signal>` signals,
    which `is_hard_signal` treats as HARD — so under `trust_gate='gate'`/`'block'` a flagged node is
    excluded from best-selection AND from breeding/confirmation (`_apply_trust_gate`). The fit-arg
    match is therefore token-anchored (see `_LEAKY_FIT_ARG_RE`) so a benign identifier can't hard-gate
    an honest solution. Keep this scan's precision high whenever it feeds a non-audit trust gate."""
    flags: list[dict] = []
    lines = code.splitlines()
    split_at = next((i for i, line in enumerate(lines) if _SPLIT_RE.search(line)), None)
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
        # Scan BOTH the fit's source LINE and the MATCHED CALL text — they miss different things and
        # neither alone is enough:
        #   * the LINE catches a test tuple in a SECOND eval_set entry
        #     (`eval_set=[(X_val,y_val),(X_test,y_test)]`), which `[^)]*` truncates out of `arg`;
        #   * the matched CALL catches a monitor kwarg wrapped across lines
        #     (`.fit(X, y, eval_set=[\n  (X_test, y_test)\n])`), which the line-level horizon missed —
        #     so purely cosmetic wrapping flipped a genuinely leaking fit from leak=True to leak=False
        #     and, under trust_gate gate/block, kept a fit-on-test node eligible to win, breed and
        #     confirm. That miss was never among this file's ACCEPTED RECALL GAP notes.
        # `[^=]` in `_TEST_MONITOR_RE` is a negated class, so it spans newlines; no re.S needed.
        line_src = lines[line_i].lower() if line_i < len(lines) else m.group(0).lower()
        test_monitor = (_TEST_MONITOR_RE.search(line_src)
                        or _TEST_MONITOR_RE.search(m.group(0).lower()))
        if (_LEAKY_FIT_ARG_RE.search(head)                             # val/test token in the fit args = leak
                or test_monitor):                                       # test INSIDE the monitor = leak
            flags.append({"signal": "fit_on_test", "line": line_i + 1, "code": snippet})
        elif split_at is not None and line_i < split_at and "train" not in arg:
            # a fit/fit_transform on (apparently full) data BEFORE the split leaks test statistics
            flags.append({"signal": "fit_before_split", "line": line_i + 1, "code": snippet})
    flags.extend(multi_test_scan(code))
    return {"detector": "code_leakage", "leak": bool(flags), "flags": flags}


def temporal_leakage(train_timestamps: list[float], test_timestamps: list[float]) -> dict:
    """For a forward (train-before-test) split, flag train rows at/after the test
    cutoff — i.e. training on future information."""
    if not train_timestamps or not test_timestamps:
        return {"detector": "temporal_leakage", "leak": False, "overlap": 0}
    # Drop non-finite stamps BEFORE the cutoff. A NaN poisons `min()` — every NaN comparison is
    # False, so a leading NaN keeps the min at NaN — and then every `t >= cutoff` is False too: the
    # detector reports leak=False on data that genuinely overlaps. That silent false negative is the
    # same class `_pearson` guards against above (arch-review §4 P1-7), and it is the dangerous
    # direction for a trust gate. NaN train stamps are dropped for the same reason: they can never
    # satisfy the comparison, so counting them would understate nothing but confuse the total.
    import math
    test_finite = [t for t in test_timestamps if isinstance(t, (int, float)) and math.isfinite(t)]
    train_finite = [t for t in train_timestamps if isinstance(t, (int, float)) and math.isfinite(t)]
    if not test_finite or not train_finite:
        return {"detector": "temporal_leakage", "leak": False, "overlap": 0, "checked": False,
                "reason": "no finite timestamps to compare"}
    cutoff = min(test_finite)
    # STRICTLY AFTER the cutoff, not at it. `t >= cutoff` flags a train row whose stamp EQUALS the
    # first test stamp, and on a coarse clock that is the ordinary case rather than a leak: a daily
    # or hourly stamp puts the last train row and the first test row in the same bucket for every
    # split made on a real calendar boundary. This detector is WIRED — `engine/audit.py::_leakage_
    # verdicts` returns `leak=True` on it and the trust gate aborts — so the equality made a coarse
    # split unrunnable while the split it describes is correct: nothing in a same-bucket train row is
    # information from the future, because the bucket is the resolution of the clock, not an ordering.
    #
    # A GENUINE overlap survives: a train row strictly after the first test stamp is still counted,
    # which is every case where the split really interleaves. The direction of the change is the safe
    # one for the one thing this cannot know — whether the stamps are exact instants or buckets —
    # because the alternative it replaces reported a leak it could not distinguish from a tie.
    #
    # `ties` is recorded rather than dropped: a split whose boundary bucket holds train rows is worth
    # SAYING, and an operator on an exact-instant clock reading `ties: 4000` is looking at a split
    # that really does share instants. It is a fact on the verdict, never a leak.
    #
    # AND IT HAS TO REACH SOMEBODY, which is the half that shipped missing. The whole verdict lands
    # on `EV_DATA_LEAKAGE` (`engine/audit.py::_leakage_blocks`), but nothing in `looplab/` or `ui/`
    # read the field, and the one surface that renders the event printed `leakage scan: clean` off
    # `leak` alone — so the disclosure this narrowing was traded for was invisible, and a split with
    # four thousand boundary ties read as assurance. `ui/src/narration.js::data_leakage` names the
    # count beside `clean` now. A number recorded and never read is the same silence as dropping it.
    overlap = sum(1 for t in train_finite if t > cutoff)
    ties = sum(1 for t in train_finite if t == cutoff)
    return {"detector": "temporal_leakage", "leak": overlap > 0,
            "cutoff": cutoff, "overlap": overlap, "ties": ties}


def code_leakage_findings(src: str) -> list[dict]:
    """`code_leakage_scan`'s flags as gate-visible trust findings (doc 25 CT-10).

    The `data_leakage:` namespace used to be minted by `engine/evaluate.py`, three files from the
    detector that knows what it found — and that namespace is what `is_hard_signal` keys gating on.
    Owned here now; `code_leakage_scan` keeps its own richer shape for callers that want the line
    numbers and the raw flags.
    """
    from looplab.trust.findings import LEAKAGE_NS, namespaced

    return namespaced(LEAKAGE_NS, code_leakage_scan(src)["flags"], signal_key="signal",
                      detail=lambda flag: f"line {flag['line']}: {flag['code']}")
