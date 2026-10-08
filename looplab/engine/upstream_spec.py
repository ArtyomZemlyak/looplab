"""Operator-only declaration of observable upstream gate obligations (doc 72)."""
import math
import json
import re


def normalize_request(operation, body):
    from looplab.core.errors import UpstreamRefusal
    common = {"expected_generation", "action_id"}
    fields = {
        "propose": {"source_node_id", "expected_base_revision", "hunk_hashes", "files", "deleted", "recipe_files", "recipe_deleted", "summary", "flag", "documentation_path", "critic"},
        "check": {"proposal_id"},
        "advance": {"proposal_id", "expected_base_revision", "evidence_token"},
        "abandon": {"claim_action_id", "reason"},
    }
    # doc 73 §4.3: a proposal whose source was measured on an OLDER base carries the source's overlay
    # three-way merged onto the current one (`rebase {from_digest, files, deleted}`); OPTIONAL, so
    # every body written before it normalizes, digests and retries exactly as it did.
    optional = {"rebase"} if operation == "propose" else set()
    try:
        if not isinstance(body, dict) or set(body) - (common | fields[operation] | optional):
            raise ValueError("Unknown request fields")
        if operation == "propose":
            body = {"deleted": [], "recipe_deleted": [], **body}
        if set(body) - optional != common | fields[operation]:
            raise ValueError("Incomplete request")
        if "rebase" in body:
            rebase = body["rebase"]
            if (not isinstance(rebase, dict) or set(rebase) != {"from_digest", "files", "deleted"}
                    or not isinstance(rebase["from_digest"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", rebase["from_digest"]) is None
                    or not isinstance(rebase["files"], dict) or len(rebase["files"]) > 128
                    or any(not isinstance(k, str) or not isinstance(v, str) for k, v in rebase["files"].items())
                    or not isinstance(rebase["deleted"], list) or len(rebase["deleted"]) > 128
                    or any(not isinstance(d, str) for d in rebase["deleted"])):
                raise ValueError("Invalid rebase: name the measured base digest and the merged overlay")
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) > 2 * 1024 * 1024:
            raise ValueError("Request exceeds 2 MiB")
        result = json.loads(raw)  # freeze the claim's exact values against caller mutation
        for field in ("expected_generation", "expected_base_revision", "evidence_token"):
            if field in result and (not isinstance(result[field], str) or re.fullmatch(r"[0-9a-f]{64}", result[field]) is None):
                raise ValueError(f"Invalid {field}")
        if operation == "propose" and (type(result["source_node_id"]) is not int or result["source_node_id"] < 0 or not isinstance(result["hunk_hashes"], list) or not 1 <= len(result["hunk_hashes"]) <= 128 or any(not isinstance(h, str) or re.fullmatch(r"[0-9a-f]{64}", h) is None for h in result["hunk_hashes"])):
            raise ValueError("Invalid source or hunk identities")
        if "proposal_id" in result and (not isinstance(result["proposal_id"], str) or re.fullmatch(r"up_[0-9a-f]{24}", result["proposal_id"]) is None):
            raise ValueError("Invalid proposal_id")
        return result
    except (ValueError, TypeError, KeyError) as exc:
        raise UpstreamRefusal("upstream_request_invalid", str(exc)) from exc


def normalize_upstream(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"tests", "regressions", "repair_probes", "repeats", "atol", "rtol", "sigma", "repair_gate"}:
        raise ValueError("upstream accepts tests, regressions, repair_probes, repeats, atol, rtol, sigma and repair_gate")
    out = {"tests": [], "regressions": [], "repair_probes": [], "repeats": raw.get("repeats", 3)}
    # THE LIGHTER GATE FOR A FIX (doc 73 §2.3, track 1), the operator's to declare: a proposal
    # promoting what a REPAIR changed (`repair_trigger_nodes`) had no metric before the fix, so the
    # paired full-source repetitions compare a recipe that crashed with one that runs — they cost
    # hours and decide nothing. `probes` waives them for such a proposal only: its tests, both
    # regression sides, its repair probe (old fails, new passes) and the unchanged scorer bytes still
    # decide. Left OUT of the declaration when `full` (the default), so every pinned `run_started`
    # declaration written before the key compares equal.
    gate_mode = raw.get("repair_gate", "full")
    if gate_mode not in ("full", "probes"):
        raise ValueError("upstream.repair_gate must be 'full' or 'probes'")
    if gate_mode == "probes":
        out["repair_gate"] = "probes"
    if type(out["repeats"]) is not int or not 2 <= out["repeats"] <= 10:
        raise ValueError("upstream.repeats must be an integer between 2 and 10")
    for field, default in (("atol", 0.0), ("rtol", 0.0), ("sigma", 2.0)):
        v = raw.get(field, default)
        if type(v) not in (int, float) or not math.isfinite(v) or v < 0 or (field == "sigma" and v > 4):
            raise ValueError(f"upstream.{field} must be finite and nonnegative (sigma <= 4)")
        out[field] = float(v)
    for field in ("tests", "regressions", "repair_probes"):
        rows = raw.get(field, [])
        if not isinstance(rows, list) or len(rows) > 16:
            raise ValueError(f"upstream.{field} accepts at most 16 probes")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) - {"name", "command", "files", "artifacts", "timeout"}:
                raise ValueError(f"upstream.{field} probe has unknown fields")
            name, argv = row.get("name"), row.get("command")
            if not isinstance(name, str) or not name.isidentifier() or len(name) > 64 or name in seen:
                raise ValueError("upstream probe names must be unique short identifiers")
            seen.add(name)
            if not isinstance(argv, list) or not argv or len(argv) > 128 or any(not isinstance(s, str) or not s or "\0" in s for s in argv):
                raise ValueError("upstream probe command must be an explicit argv")
            timeout = row.get("timeout", 120)
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 600:
                raise ValueError("upstream probe timeout must be in (0, 600]")
            files, artifacts = row.get("files", {}), row.get("artifacts", [])
            if not isinstance(files, dict) or len(files) > 64 or any(not isinstance(v, str) for v in files.values()):
                raise ValueError("upstream probe files must be a bounded text overlay")
            if not isinstance(artifacts, list) or len(artifacts) > 32 or any(not isinstance(v, str) for v in artifacts):
                raise ValueError("upstream artifacts must be a bounded list of workspace-relative files")
            from looplab.core.scorer_boundary import normalize_scorer_boundary
            if files:
                normalize_scorer_boundary({"files": list(files)})
            if artifacts:
                normalize_scorer_boundary({"files": artifacts})
            if field == "regressions" and not artifacts:
                raise ValueError("upstream regression probes require observable artifact files")
            out[field].append({"name": name, "command": argv, "files": files,
                               "artifacts": artifacts, "timeout": float(timeout)})
    if not out["regressions"]:
        raise ValueError("upstream requires at least one operator-declared old-recipe regression probe")
    return out
