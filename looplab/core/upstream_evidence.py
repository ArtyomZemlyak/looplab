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


def equivalence(row):
    """Full paired samples and their statistics, including readable failed comparisons."""
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
            and row["passed"] == (row["source_reproduced"] and abs(row["delta"]) <= row["tolerance"]))
    except (OverflowError, ValueError):
        return False


def passing_checks(row):
    """A passing verdict contains all gate legs, bound to actual paired executions."""
    checks, executions = row["checks"], row["executions"]
    kinds = [c["kind"] for c in checks]
    if (kinds.count("equivalence") != 1 or "test" not in kinds or "regression" not in kinds
        or row.get("inputs_unchanged") is not True or not all(c["passed"] for c in checks)):
        return False
    eq = next(c for c in checks if c["kind"] == "equivalence")
    repeats = len(eq["values"][0])
    if len(executions) != kinds.count("test") + 2 * (kinds.count("regression") + kinds.count("repair") + repeats):
        return False
    offset = 0
    for check in checks:
        if check["kind"] == "equivalence":
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
    for i, execution_row in enumerate(executions[-2 * repeats:]):
        side, repeat = i % 2, i // 2
        if (execution_row["label"] != ("old-source" if side == 0 else "new-source") + str(repeat)
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
                and r.get("kind") in ("test", "regression", "repair", "equivalence")
                and (r["kind"] != "equivalence" or equivalence(r)) for r in row["checks"])
        and number(row.get("eval_seconds")) and row["eval_seconds"] >= 0
        and math.isclose(row["eval_seconds"], sum(r["seconds"] for r in row["executions"]), rel_tol=1e-9, abs_tol=1e-9)
        and (not row["passed"] or passing_checks(row)))


def gate_matches_policy(row, declaration, source_metric, *, repair_required=False):
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
    if [(c["kind"], c.get("name")) for c in row["checks"] if c["kind"] != "equivalence"] != [
            (kind, probe["name"]) for kind, probe in probes]:
        return False
    eq = next(c for c in row["checks"] if c["kind"] == "equivalence")
    if len(eq["values"][0]) != declaration["repeats"]:
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


