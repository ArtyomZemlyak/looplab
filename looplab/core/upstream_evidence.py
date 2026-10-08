"""Pure upstream gate evidence checks shared by the engine and typed readers.

Hashes do not establish semantic completeness. Failed partial comparisons stay
readable; malformed passing evidence never supplies an advancement verdict.
"""
import math
import re
import statistics


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def stage(row):
    return (isinstance(row, dict) and isinstance(row.get("name"), str) and bool(row["name"])
        and isinstance(row.get("status"), str) and bool(row["status"])
        and type(row.get("exit_code")) is int and number(row.get("seconds")) and row["seconds"] >= 0)


def execution(row):
    return (isinstance(row, dict) and isinstance(row.get("label"), str)
        and type(row.get("exit_code")) is int and type(row.get("timed_out")) is bool
        and type(row.get("valid")) is bool and number(row.get("seconds")) and row["seconds"] >= 0
        and "metric" in row and (row["metric"] is None or number(row["metric"]))
        and isinstance(row.get("workdir"), str) and isinstance(row.get("artifacts"), dict)
        and all(isinstance(k, str) and sha(v) for k, v in row["artifacts"].items())
        and "stages" in row and (row["stages"] is None or isinstance(row["stages"], list)
            and all(stage(s) for s in row["stages"]))
        and (not row["valid"] or not row["timed_out"] and all(
            s["status"] in ("ok", "reused") and s["exit_code"] == 0 for s in row["stages"] or [])))


# THE EQUIVALENCE PROFILES (doc 73 §4.2 G5). `full`: `upstream.repeats` paired full evaluations per
# side and the source's own score reproduced (doc 72). `canary`: ONE old/new pair under the task's
# declared `eval.canary` (`Settings.upstream_verify`) — a different slice, so no source reproduction.
EQUIVALENCE_PROFILES = ("full", "canary")


def _canary_equivalence(row):
    values = row.get("values")
    if (not isinstance(values, list) or len(values) != 2
            or not all(isinstance(v, list) and len(v) <= 1 and all(number(x) for x in v) for v in values)):
        return False
    fields = {"means", "tolerance", "delta"}
    if not fields.intersection(row):
        return row["passed"] is False
    if (not fields <= row.keys() or len(values[0]) != 1 or len(values[1]) != 1
            or not number(row["tolerance"]) or row["tolerance"] < 0 or not number(row["delta"])
            or row.get("means") != [values[0][0], values[1][0]]):
        return False
    try:
        delta = values[1][0] - values[0][0]
        return (math.isclose(row["delta"], delta, rel_tol=1e-9, abs_tol=1e-12)
                and row["passed"] == (abs(delta) <= row["tolerance"]))
    except (OverflowError, ValueError):
        return False


def equivalence(row):
    """Full paired samples and their statistics, including readable failed comparisons."""
    if row.get("profile") == "canary":
        return _canary_equivalence(row)
    values = row.get("values")
    if (row.get("profile") != "full" or not isinstance(values, list) or len(values) != 2
        or not all(isinstance(v, list) and len(v) <= 10 and all(number(x) for x in v) for v in values)):
        return False
    fields = {"means", "sem", "tolerance", "delta", "source_reproduced"}
    if not fields.intersection(row):
        return row["passed"] is False
    if (not fields <= row.keys() or not 2 <= len(values[0]) == len(values[1]) <= 10
        or not all(isinstance(row[k], list) and len(row[k]) == 2 and all(number(x) for x in row[k]) for k in ("means", "sem"))
        or any(x < 0 for x in row["sem"]) or not number(row["tolerance"]) or row["tolerance"] < 0
        or not number(row["delta"]) or type(row["source_reproduced"]) is not bool):
        return False
    try:
        means = [statistics.mean(v) for v in values]
        sem = [statistics.stdev(v) / math.sqrt(len(v)) for v in values]
        return (all(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
                    for a, b in zip(row["means"] + row["sem"] + [row["delta"]], means + sem + [means[1] - means[0]]))
            and row["passed"] == (row["source_reproduced"] and abs(row["delta"]) <= row["tolerance"])
            # Representation closeness is not a scientific allowance. Both
            # sides of the declared tolerance use the actual paired samples,
            # including zero tolerance and a readable failed comparison.
            and row["passed"] == (row["source_reproduced"] and abs(means[1] - means[0]) <= row["tolerance"]))
    except (OverflowError, ValueError):
        return False


def passing_checks(row):
    """A passing verdict contains all gate legs, bound to actual paired executions.

    Exactly one of `equivalence` (the paired full-source repetitions) and `equivalence_waived`
    (doc 73 §2.3, track 1: a REPAIR promoted under the operator's `repair_gate: probes`, which must
    then carry a `repair` leg — a waiver with no trigger probe proves nothing)."""
    checks, executions = row["checks"], row["executions"]
    kinds = [c["kind"] for c in checks]
    waived = kinds.count("equivalence_waived")
    if (kinds.count("equivalence") + waived != 1 or "test" not in kinds or "regression" not in kinds
        or (waived and "repair" not in kinds)
        or row.get("inputs_unchanged") is not True or not all(c["passed"] for c in checks)):
        return False
    eq = next((c for c in checks if c["kind"] == "equivalence"), None)
    repeats = len(eq["values"][0]) if eq is not None else 0
    if len(executions) != kinds.count("test") + 2 * (kinds.count("regression") + kinds.count("repair") + repeats):
        return False
    offset = 0
    for check in checks:
        if check["kind"] in ("equivalence", "equivalence_waived"):
            continue
        name = check.get("name")
        count = 1 if check["kind"] == "test" else 2
        rows = executions[offset:offset + count]
        offset += count
        if not isinstance(name, str) or not name or any(not r["valid"] or r["timed_out"] for r in rows):
            return False
        if count == 1:
            if rows[0]["label"] != "test-" + name or rows[0]["exit_code"] != 0:
                return False
        elif (rows[0]["label"] != "old-" + name or rows[1]["label"] != "new-" + name
              or rows[1]["exit_code"] != 0 or (rows[0]["exit_code"] == 0 if check["kind"] == "repair"
                  else rows[0]["exit_code"] != 0 or not rows[0]["artifacts"]
                  or rows[0]["artifacts"] != rows[1]["artifacts"])):
            return False
    label = "canary" if eq is not None and eq.get("profile") == "canary" else "source"
    for i, execution_row in enumerate(executions[-2 * repeats:] if repeats else []):
        side, repeat = i % 2, i // 2
        if (execution_row["label"] != ("old-" if side == 0 else "new-") + label + str(repeat)
            or not execution_row["valid"] or execution_row["timed_out"] or execution_row["exit_code"] != 0
            or execution_row["metric"] != eq["values"][side][repeat]):
            return False
    return True


def gate(row):
    return (isinstance(row, dict) and type(row.get("passed")) is bool
        and sha(row.get("input_identity")) and isinstance(row.get("checks"), list)
        and isinstance(row.get("executions"), list) and len(row["executions"]) <= 116
        and all(execution(r) for r in row["executions"])
        and all(isinstance(r, dict) and type(r.get("passed")) is bool
                and r.get("kind") in ("test", "regression", "repair", "equivalence",
                                      "equivalence_waived")
                and (r["kind"] != "equivalence" or equivalence(r))
                and (r["kind"] != "equivalence_waived" or r["passed"] is True)
                for r in row["checks"])
        and number(row.get("eval_seconds")) and row["eval_seconds"] >= 0
        # Individually finite JSON numbers can overflow when combined. Reject
        # their aggregate before converting it inside math.isclose.
        and number(total_seconds := sum(r["seconds"] for r in row["executions"]))
        and math.isclose(row["eval_seconds"], total_seconds, rel_tol=1e-9, abs_tol=1e-9)
        and (not row["passed"] or passing_checks(row)))


def _probe_rows(probes, executions):
    offset = 0
    for kind, probe in probes:
        count = 1 if kind == "test" else 2
        yield (kind, probe), executions[offset:offset + count]
        offset += count


def gate_matches_policy(row, declaration, source_metric, *, repair_required=False, repair_only=False,
                        profile="full"):
    """Bind a complete passing result to the launched observable gate obligations.

    Offline readers lack the operator declaration. Before granting a new base,
    the engine also knows every required probe, repetition and numeric tolerance.
    Recompute these from that declaration and the primary measured source, never
    from a retained boolean or the result's own claimed tolerance.
    """
    if not gate(row) or not row["passed"]:
        return False
    probes = [(kind, probe) for kind, field in (("test", "tests"), ("regression", "regressions"),
        ("repair", "repair_probes")) if kind != "repair" or repair_required for probe in declaration[field]]
    if [(c["kind"], c.get("name")) for c in row["checks"]
            if c["kind"] not in ("equivalence", "equivalence_waived")] != [
            (kind, probe["name"]) for kind, probe in probes]:
        return False
    # The waiver is granted by the DECLARATION and the proposal, recomputed here, never by the row:
    # `repair_gate: probes` and a proposal promoting ONLY a repair (doc 73 §2.3, track 1) — the
    # rule `engine/upstream_gate.py::waives_equivalence` applied when it ran the gate.
    waived = declaration.get("repair_gate") == "probes" and repair_required and repair_only
    eq = next((c for c in row["checks"] if c["kind"] == "equivalence"), None)
    if waived:
        return eq is None and any(c["kind"] == "equivalence_waived" for c in row["checks"]) and all(
            set(r["artifacts"]) == set(probe["artifacts"])
            for (kind, probe), rows in _probe_rows(probes, row["executions"])
            for r in (rows[1:] if kind == "repair" else rows))
    # The equivalence profile is the one the ENGINE expects (`upstream_gate.py::gate_profile`, from
    # the launched settings and the task), never the one the result names (doc 73 §4.2 G5).
    if eq is None or eq.get("profile") != profile:
        return False
    if profile == "full" and len(eq["values"][0]) != declaration["repeats"]:
        return False
    offset = 0
    for kind, probe in probes:
        count = 1 if kind == "test" else 2
        rows = row["executions"][offset:offset + count]
        offset += count
        # A repair's failing old side may not produce artifacts. Its passing new
        # side, all tests and both regression sides must observe the full set.
        required_rows = rows[1:] if kind == "repair" else rows
        if any(set(r["artifacts"]) != set(probe["artifacts"]) for r in required_rows):
            return False
    if profile == "canary":
        means = [eq["values"][0][0], eq["values"][1][0]]
        tolerance = declaration["atol"] + declaration["rtol"] * max(map(abs, means))
        return number(tolerance) and eq["tolerance"] == tolerance and abs(means[1] - means[0]) <= tolerance
    means = [statistics.mean(v) for v in eq["values"]]
    sem = [statistics.stdev(v) / math.sqrt(len(v)) for v in eq["values"]]
    tolerance = declaration["atol"] + declaration["rtol"] * max(map(abs, means)) + declaration["sigma"] * math.hypot(*sem)
    source_tolerance = declaration["atol"] + declaration["rtol"] * abs(source_metric) + declaration["sigma"] * sem[0]
    return (number(tolerance) and number(source_tolerance)
        # JSON preserves these floats exactly. Approximate equality would turn
        # an explicitly zero operator tolerance into an undeclared allowance.
        and eq["tolerance"] == tolerance
        and abs(means[1] - means[0]) <= tolerance
        and eq["source_reproduced"] and abs(means[0] - source_metric) <= source_tolerance)


