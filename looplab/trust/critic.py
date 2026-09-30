"""C4 · Independent critic (ADR-7). A cheap, execution-free critic pass over a generated solution
BEFORE it's trusted: does the code plausibly do what the Idea claims, and is it not an obvious no-op?
Catches the failure modes a validator's syntax check misses — a model that returns a stub, hard-codes
the metric, or ignores the requested hyperparameters. Broad critic warnings remain advisory, while the
narrow ``critic:hardcoded_metric`` signal can exclude selection/breeding or block under
``trust_gate=gate|block``. Findings use the same ``reward_hack_suspected`` event as B5.
"""
from __future__ import annotations

import ast
import os
import re

from looplab.core.models import Idea


def critique(idea: Idea, code: str, *, submission_file: str | None = None,
             scorer_in_tree: bool = True,
             configuration_only: bool = False,
             metric_key: str = "metric",
             source_units: tuple[str, ...] | None = None) -> list[dict]:
    """Return a list of {issue, detail} the critic flags (empty == looks fine).

    `submission_file`: set when the run is graded OUT-OF-PROCESS by a host grader (MLE-bench, and
    any other `host_grader()` task). In that mode the candidate's output contract is to WRITE this
    file — the score is computed by the host from it and *replaces* any self-reported value, so the
    in-code `metric` checks are meaningless. Leaving them on false-positives on every submission
    that merely doesn't happen to use the word "metric" (e.g. an MLE-bench solution that writes
    submission.csv). When host-graded we therefore swap the metric checks for a check that the
    submission file is actually written. Left None for legacy in-workdir grading.

    `scorer_in_tree`: False when the eval command's entrypoint is NOT a file in the candidate's own
    tree — a task-supplied harness that takes the submission as an ARGUMENT and prints the score
    itself — or when the candidate changes only declarative configuration. Then the candidate's
    authored source has no metric-output contract, and `no_metric_output` is the same category
    error the paragraph above describes for MLE-bench. MEASURED on the
    AlgoTune corpus 2026-08-29: the critic ran on 34 nodes and flagged `no_metric_output` on 34 of
    34, because the eval stage runs `benchmarks/algotune/looplab_eval.py --solver solver.py` and
    the solver prints nothing, ever. Requiring the task's DECLARED metric key from these
    out-of-tree solvers would have made it WORSE, not better — that key is `speedup`, and 0 of
    213 solvers reference it against 5 that mention `metric`. The declared key applies where
    the candidate owns scoring output.

    `hardcoded_metric` is NOT suppressed with it: that one is the hard gate, and a literal metric
    value sitting in a candidate is suspicious no matter who computes the score.

    `configuration_only`: inspect unquoted YAML/TOML/INI metric assignments as well as JSON
    fields. A bare ``metric = 0.0`` in executable code can be a legitimate accumulator, so this
    additional syntax is only a hard signal when all authored files are declarative config.

    `metric_key`: the task's declared stdout JSON key. It matters when the candidate
    scores itself or authors a config; an out-of-tree scorer still owns its own output.

    `source_units`: each authored file separately, so one YAML file cannot make
    a neighboring Python scorer unparseable to the direct-output check.
    """
    code = code or ""
    metric_key = metric_key if isinstance(metric_key, str) and metric_key else "metric"
    key_pattern = re.escape(metric_key)
    issues: list[dict] = []
    stripped = code.strip()
    short = len(stripped) < 20
    if short:
        issues.append({"issue": "stub", "detail": "solution is suspiciously short / near-empty"})

    if submission_file:
        if short:
            return issues
        # Out-of-process grading: the deliverable is the submission file, not an in-code metric.
        name = os.path.basename(str(submission_file).replace("\\", "/")) or str(submission_file)
        # Match the name on a token boundary, NOT as a bare substring: nearly every solution reads
        # `sample_submission.csv`, which *contains* "submission.csv" — a plain `in` test is therefore
        # always true and the check is dead. The lookbehind rejects the `_submission.csv` (and
        # `x.submission.csv`) case while still matching the real write target: `./submission.csv`,
        # `'submission.csv'`, `out/submission.csv`, etc.
        if name and not re.search(r"(?<![\w.\-])" + re.escape(name), code):
            issues.append({"issue": "no_submission_output",
                           "detail": f"code never references '{name}' — the host grader would have "
                                     "no submission to score"})
    else:
        # In-workdir grading: the solution must compute and emit the metric itself -- but only
        # when the thing being RUN is the solution. See `scorer_in_tree` in the docstring.
        if not short and scorer_in_tree and metric_key not in code:
            issues.append({"issue": "no_metric_output",
                           "detail": f"code never references {metric_key!r} — it may not emit the required score"})
        # A literal metric value may be a placeholder later overwritten by a
        # computation. Keep that broad guard, but a literal directly serialized
        # to stdout is the output itself and must remain a hard signal.
        # A later field does not make the literal computed: accept a comma.
        hardcoded = re.search(
            rf'["\']{key_pattern}["\']\s*:\s*[0-9.+\-eE]+\s*[,}})]', code)
        if configuration_only and not hardcoded:
            # YAML and TOML also admit digit separators, base-prefixed integers and
            # non-finite float literals. A config's literal metric is still authored
            # evidence regardless of which numeric spelling the parser accepts.
            decimal = r'[+-]?(?:\d[\d_]*(?:\.[\d_]*)?|\.[\d_]+)(?:[eE][+-]?[\d_]+)?'
            radix = r'[+-]?0(?:[xX][0-9a-fA-F_]+|[oO][0-7_]+|[bB][01_]+)'
            special = r'[+-]?\.?(?:inf|nan)'
            number = rf'(?:{radix}|{decimal}|{special})'
            value = rf'(?:{number}|["\']{number}["\'])'
            hardcoded = re.search(
                rf'(?im)(?:^|[{{,])[ \t]*(?:["\']{key_pattern}["\']|{key_pattern})'
                rf'[ \t]*[:=][ \t]*{value}(?=[ \t\r]*(?:$|[,#;}}]))', code)
        # Anchor the `metric` token with a left word boundary. Unanchored, the bare-name alternative
        # matched `metric` as a SUFFIX of any identifier — `is_symmetric = True` (also `asymmetric`,
        # `parametric`, `barometric`, `isometric`) makes `metric = T` match `computed`, so
        # `hardcoded and not computed` is False and the hard-coded-metric cheat slips the HARD
        # `critic:hardcoded_metric` gate (one throwaway `symmetric = x` line defeats it). The quoted
        # `hardcoded`/second-alt regexes already require quotes around `metric`, so only this bare form
        # needs the boundary. Together the two alternatives still admit every legit computed form: the
        # anchored first alt matches `{"metric": score}` and a bare `metric = score`, and the second alt
        # matches the bracket-assignment `result["metric"] = value`.
        computed = re.search(
            rf'(?<![A-Za-z0-9_])["\']?{key_pattern}["\']?\s*[:=]\s*[A-Za-z_]', code) or \
            re.search(rf'\[\s*["\']{key_pattern}["\']\s*\]\s*=\s*[A-Za-z_]', code)
        # A declarative config cannot overwrite its literal with a later
        # computation. In TOML, ``metric = inf`` also looks like a bare-name
        # assignment to the generic computed-value pattern above.
        if (any(_serialized_literal_score(unit, metric_key)
                for unit in (source_units if source_units is not None else (code,)))
                or (hardcoded and (configuration_only or not computed))):
            issues.append({"issue": "hardcoded_metric",
                           "detail": "the metric appears to be a hard-coded constant, not computed"})

    if short:
        return issues

    # Requested hyperparameters should appear in the code; none appearing suggests a no-op that
    # ignores the proposal (the idea isn't actually implemented). Skipped for the `debug` operator:
    # its params describe a diagnostic or repair (e.g. verify_cuda, test_gru_forward) or are
    # inherited from the parent node being fixed — they are NOT modeling knobs that must be threaded
    # into the solution, so demanding they appear in the code is a category error.
    if (idea.operator or "") != "debug":
        pnames = [str(k) for k in (idea.params or {})]
        if pnames and not any(_param_is_referenced(p, code) for p in pnames):
            issues.append({"issue": "params_ignored",
                           "detail": f"none of the proposed params {pnames} are referenced in the code"})
    return issues


def _serialized_literal_score(code: str, metric_key: str) -> bool:
    """Find literal metrics that reach stdout through a local JSON payload.

    A separate assignment to a variable named like the output key cannot make
    that serialized literal computed. Track only straight-line local bindings
    and metric-key writes, so a placeholder overwritten with a computed score
    before serialization is not treated as a reported constant.
    """
    if "dumps" not in code or metric_key not in code:
        return False
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return False

    def literal_dict(payload: ast.AST) -> bool:
        return isinstance(payload, ast.Dict) and any(
            isinstance(key, ast.Constant) and key.value == metric_key
            and _numeric_literal(value)
            for key, value in zip(payload.keys, payload.values))

    def dump_call(call: ast.AST) -> bool:
        return (isinstance(call, ast.Call) and bool(call.args)
                and getattr(call.func, "id", getattr(call.func, "attr", None)) == "dumps")

    def literal_dump(call: ast.AST) -> bool:
        return dump_call(call) and literal_dict(call.args[0])

    def output_call(call: ast.AST) -> bool:
        if not isinstance(call, ast.Call):
            return False
        func = call.func
        return ((isinstance(func, ast.Name) and func.id == "print")
                or (isinstance(func, ast.Attribute) and func.attr == "write"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "stdout"
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "sys"))

    def metric_writes(target: ast.AST) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            if isinstance(target.slice, ast.Constant) and target.slice.value != metric_key:
                return set()
            return {target.value.id}
        if isinstance(target, (ast.Tuple, ast.List)):
            return set().union(*(metric_writes(item) for item in target.elts))
        return set()

    def mutates_metric(call: ast.Call) -> bool:
        method = call.func.attr
        if method == "clear":
            return True
        if method == "update":
            if (len(call.args) > 1 or any(kw.arg is None or kw.arg == metric_key
                                          for kw in call.keywords)):
                return True
            if not call.args:
                return False
            values = call.args[0]
            return (not isinstance(values, ast.Dict)
                    or any(not isinstance(key, ast.Constant) or key.value == metric_key
                           for key in values.keys))
        if method == "pop":
            return not (call.args and isinstance(call.args[0], ast.Constant)
                        and call.args[0].value != metric_key)
        # A tracked literal dictionary already has the metric key; setdefault
        # cannot replace its value.
        return False

    def writes_literal_metric(call: ast.Call) -> bool:
        if call.func.attr != "update":
            return False
        for keyword in reversed(call.keywords):
            if keyword.arg == metric_key:
                return _numeric_literal(keyword.value)
            if keyword.arg is None:
                return False
        if call.args and isinstance(call.args[0], ast.Dict):
            for key, value in reversed(list(zip(call.args[0].keys, call.args[0].values))):
                if isinstance(key, ast.Constant) and key.value == metric_key:
                    return _numeric_literal(value)
        return False

    for emitted in ast.walk(tree):
        if output_call(emitted) and any(
            literal_dump(call) for argument in emitted.args for call in ast.walk(argument)):
            return True

    # Follow only an unmodified local binding in the same statement block. This
    # catches `payload = json.dumps({...}); print(payload)` without treating an
    # unused debug payload or a later computed overwrite as a reported score.
    for owner in ast.walk(tree):
        if not isinstance(owner, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.If, ast.For, ast.AsyncFor, ast.While,
                                  ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler)):
            continue
        for body in (getattr(owner, "body", ()), getattr(owner, "orelse", ())):
            literal_names: set[str] = set()
            dict_names: set[str] = set()

            def serialized_literal(value: ast.AST) -> bool:
                return (literal_dump(value) or
                        (dump_call(value) and isinstance(value.args[0], ast.Name)
                         and value.args[0].id in dict_names))

            for statement in body:
                if isinstance(statement, ast.Assign):
                    names = {target.id for target in statement.targets
                             if isinstance(target, ast.Name)}
                    written = set().union(*(metric_writes(target)
                                            for target in statement.targets))
                    literal_field_names = {
                        target.value.id for target in statement.targets
                        if (isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Name)
                            and isinstance(target.slice, ast.Constant)
                            and target.slice.value == metric_key
                            and _numeric_literal(statement.value))}
                    is_serialized = serialized_literal(statement.value)
                    is_dict = literal_dict(statement.value)
                    literal_names.difference_update(written)
                    dict_names.difference_update(written)
                    if is_serialized:
                        literal_names.update(names)
                    if is_dict:
                        dict_names.update(names)
                    dict_names.update(literal_field_names)
                elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    is_serialized = serialized_literal(statement.value)
                    is_dict = literal_dict(statement.value)
                    literal_names.discard(statement.target.id)
                    dict_names.discard(statement.target.id)
                    if is_serialized:
                        literal_names.add(statement.target.id)
                    if is_dict:
                        dict_names.add(statement.target.id)
                elif isinstance(statement, ast.AugAssign):
                    written = metric_writes(statement.target)
                    literal_names.difference_update(written)
                    dict_names.difference_update(written)
                elif isinstance(statement, ast.Expr) and output_call(statement.value):
                    if any(isinstance(arg, ast.Name) and arg.id in literal_names
                           for arg in statement.value.args):
                        return True
                    if any(dump_call(call) and isinstance(call.args[0], ast.Name)
                           and call.args[0].id in dict_names
                           for arg in statement.value.args for call in ast.walk(arg)):
                        return True
                value = statement.value if isinstance(statement, (
                    ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr)) else None
                if isinstance(value, ast.AST):
                    for call in ast.walk(value):
                        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                                and isinstance(call.func.value, ast.Name)
                                and mutates_metric(call)):
                            dict_names.discard(call.func.value.id)
                            if writes_literal_metric(call):
                                dict_names.add(call.func.value.id)
    return False


def _numeric_literal(value: ast.expr) -> bool:
    if isinstance(value, ast.UnaryOp) and isinstance(value.op, (ast.UAdd, ast.USub)):
        return _numeric_literal(value.operand)
    if not isinstance(value, ast.Constant) or isinstance(value.value, bool):
        return False
    if isinstance(value.value, (int, float)):
        return True
    if isinstance(value.value, str):
        try:
            float(value.value)
            return True
        except ValueError:
            return False
    return False


def _param_is_referenced(pname: str, code: str) -> bool:
    """Does the written surface mention this parameter — under EITHER of the two spellings a
    parameter legitimately has?

    A repo task's params are FLATTENED dotted paths (`train.training.learning_rate`), and the place
    they are set is a nested config file the Developer writes:

        train:
          training:
            learning_rate: 0.001

    That text does not contain the string `train.training.learning_rate` and never will, so the
    literal search reported `params_ignored` on a node that had implemented every single parameter.
    MEASURED on rubertlite-dr-unified-v6: it fired on nodes 2, 3, 4 and 6 — every node the check ran
    on — while `config.yaml` in each of their own `node.files` contained `learning_rate`,
    `batch_size`, `temperature` and `n_epochs` under their nested keys. A signal that fires on 4 of 4
    carries no information, and the cost is not neutral: it is in the operator's attention feed and
    in the durable trust record, and it teaches a reader to skim past the reward-hack channel, which
    is how the one real signal gets missed. (`params_ignored` is advisory — only
    `critic:hardcoded_metric` excludes a node from selection — so nothing was gated on it.)

    So the LEAF segment counts too. That is a weaker bar than the full path, deliberately and only
    just: the check already fires only when NOTHING matches, so with a dozen parameters it still
    takes a surface that mentions not one of their names to trip. A no-op solution does not
    accidentally contain `uniformity_weight`.

    KNOWN LIMITATION, stated rather than implied by the leaf fallback's optimistic wording. The
    scan surface concatenates ALL of `node.files`, which for a repo task includes copied-in configs
    and training scripts that natively contain `learning_rate`/`batch_size`/`epochs`/`seed`. So a
    dotted param whose leaf is any ubiquitous ML token passes even when the Developer threaded
    nothing — executed counterexample: a genuinely-ignored `train.training.learning_rate` does not
    fire, because the seeded repo's own config already carries the token.

    That is a deliberate trade, not an oversight: the exact-dotted-path check this replaced was 100%
    false-positive on the same task shape (a repo param is a flattened config path and never appears
    verbatim in the code), and a signal that fires on every node is worth nothing. Both states carry
    little information, which is the tell that the question is at the wrong layer — whether a param
    was really threaded is one only the task adapter can answer, by resolving the dotted path in the
    written config and diffing it against the base. Until then this stays ADVISORY: it may hint, and
    nothing may gate on it."""
    if re.search(rf"\b{re.escape(pname)}\b", code):
        return True
    leaf = pname.rsplit(".", 1)[-1]
    return bool(leaf) and leaf != pname and bool(re.search(rf"\b{re.escape(leaf)}\b", code))


def scorer_is_in_tree(task) -> bool:
    """Whether the eval command RUNS a file from the candidate's own tree.

    True — today's behaviour — whenever the entrypoint resolves to an in-repo path, and equally
    when there is no task/eval to ask: an unknown answer must not start suppressing checks by
    itself. False only when `entrypoint_candidates` resolves NOTHING from any scoring command,
    which is this codebase's own existing notion of "LoopLab cannot protect the code the score
    stage runs" (`repo_task.py::eval_entrypoint_unprotected`) and is exactly what an out-of-tree
    harness like `looplab_eval.py --solver solver.py` looks like — measured: it resolves to [].
    """
    ev = getattr(task, "eval", None)
    if ev is None:
        return True
    # A HOST SCORER owns the metric (`adapters/repo_task.py`, `eval.host_scorer`): the number the run
    # keeps is the one the host program prints over the candidate's tree, and whatever the candidate
    # prints rides beside it unused — so no file of the candidate's is the scorer, however its stages
    # resolve. Measured on MiniOneRec inf13 (2026-09-29/30): the task's one stage is `python -m
    # compileall -q service optimizations`, which resolves in-tree, the score is the host's
    # `harness/bench_infer.py`, and the critic flagged `no_metric_output` on nodes 33 and 35.
    if getattr(ev, "host_scorer", None) is not None:
        return False
    try:
        from looplab.adapters.repo_task import entrypoint_candidates
        argvs = [getattr(ev, "command", None) or []]
        for st in (getattr(ev, "stages", None) or []):
            argvs.append((st.get("command") if isinstance(st, dict)
                          else getattr(st, "command", None)) or [])
        return any(bool(entrypoint_candidates(argv)) for argv in argvs)
    except Exception:                       # noqa: BLE001 — an advisory rung must never raise
        return True


def candidate_only_configuration(code: str | None, files: dict | None) -> bool:
    """A config-only patch cannot itself print the protected scorer's metric.

    Unknown file types stay eligible for the output check. This only removes
    ``no_metric_output`` when every authored file is a declarative config and
    the candidate has no standalone code body; the hardcoded-metric check still
    examines the same bytes.
    """
    config_suffixes = (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg")
    return (not (code or "").strip() and bool(files)
            and all(str(path).lower().endswith(config_suffixes) for path in files))


def critic_findings(idea, code: str, *, submission_file: str | None = None,
                    scorer_in_tree: bool = True,
                    configuration_only: bool = False,
                    metric_key: str = "metric",
                    source_units: tuple[str, ...] | None = None) -> list[dict]:
    """`critique`'s issues as gate-visible trust findings (doc 25 CT-10).

    The `critic:` namespace decides gating, not presentation: `critic:hardcoded_metric` EXCLUDES a
    node from selection while every other `critic:` issue stays advisory (`is_hard_signal`). It was
    assembled at the consumer; it belongs with the detector that produced the issue.
    """
    from looplab.trust.findings import CRITIC_NS, finding

    return [finding(CRITIC_NS + str(row["issue"]), row["detail"])
            for row in critique(idea, code, submission_file=submission_file,
                                scorer_in_tree=scorer_in_tree,
                                configuration_only=configuration_only,
                                metric_key=metric_key,
                                source_units=source_units)
            if row.get("issue")]
