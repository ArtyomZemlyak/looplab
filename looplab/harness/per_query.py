"""Per-query evaluation arrays, shared-test identity and LoopLab-computed paired comparisons (doc 81).

A node's scalar metric cannot answer "is T better than C on the same test?" with an honest
interval: the agent of the 2026-10-13/14 MiniOneRec campaign therefore re-scored every model on a
frozen 40k-user test, wrote per-query ``.npz`` arrays (hits, denominators, user ids) and computed
paired, user-clustered intervals and Holm adjustments with twelve copies of its own scripts — none
of which LoopLab could see. This module moves that computation INTO LoopLab:

* an UPLOAD binds per-query arrays to one node lifecycle of one run. LoopLab parses them itself
  (`core/npyio.py`, pickle-free), reduces them to per-query metric vectors with the declared column
  selection, derives the SHARED TEST ID from the query ids and their clusters (users) —
  ``qs1-`` + 32 hex of SHA-256 over the sorted ``query_id<TAB>cluster`` lines — and, when asked,
  checks the upload's mean against the metric LoopLab itself measured for that node
  (``consistency``). Uploads are stored under ``per_query/`` in the run directory, the query/cluster
  vector once per test id, each upload's metric vectors in the test's canonical (sorted) order;
* a COMPARISON is computed on read, from those stored vectors only, by `trust/paired.py`: paired
  differences with user-clustered normal intervals (optionally a cluster bootstrap), two-sided
  p-values and Holm's adjustment over each declared contrast family. Every number carries its
  provenance — which upload (sha256, author, node lifecycle, measured-metric consistency), which
  method version — and whether the experiment was registered before the result existed;
* a LEADERBOARD lists every node of every run under the run root that uploaded for one test id.

The arrays come from the agent (or from the node's own workdir); the arithmetic never does.
"""
from __future__ import annotations

import base64
import functools
import gzip
import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Optional

import orjson
from fastapi import HTTPException

from looplab.core.atomicio import append_jsonl_bytes_locked, atomic_write_bytes
from looplab.core.npyio import NpyFormatError, read_npz
from looplab.events.eventstore import EventStore, EventStoreLockError, interprocess_lock
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.campaign import (METRIC_RE, SLUG_RE, author_of, experiments_of,
                                      read_journal)
from looplab.harness.journals import read_event_source, read_source
from looplab.trust import paired

PER_QUERY_DIR = "per_query"
MANIFEST = "manifest.jsonl"
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_QUERIES = 2_000_000
CONSISTENCY_TOLERANCE = 1e-6


def manifest_path(rd: Path) -> Path:
    return Path(rd) / PER_QUERY_DIR / MANIFEST


def test_id_for(query_ids: list[str], clusters: list[str]) -> str:
    lines = "\n".join(f"{q}\t{c}" for q, c in sorted(zip(query_ids, clusters)))
    return "qs1-" + hashlib.sha256(lines.encode("utf-8")).hexdigest()[:32]


# ------------------------------------------------------------------------------------- parsing
def _arrays_from_payload(fmt: str, raw: bytes) -> dict[str, tuple[tuple, list]]:
    if fmt == "npz":
        try:
            return read_npz(raw)
        except NpyFormatError as exc:
            raise HTTPException(400, {"code": "per_query_unreadable", "message": str(exc)}) from None
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, {"code": "per_query_unreadable", "message": str(exc)[:300]}) from None
    if not isinstance(doc, dict):
        raise HTTPException(400, "a JSON upload is an object of named arrays")
    out = {}
    for name, value in doc.items():
        if not isinstance(value, list):
            raise HTTPException(400, f"array {name!r} must be a list")
        if value and isinstance(value[0], list):
            width = len(value[0])
            if any(not isinstance(row, list) or len(row) != width for row in value):
                raise HTTPException(400, f"array {name!r} is ragged")
            out[name] = ((len(value), width), [x for row in value for x in row])
        else:
            out[name] = ((len(value),), value)
    return out


def _vector(arrays: dict, name: str, columns, n: int) -> list[float]:
    """One per-query vector: a 1-D array, or the row sum over the selected columns of a 2-D one."""
    if name not in arrays:
        raise HTTPException(400, f"no array named {name!r} in the upload")
    shape, values = arrays[name]
    if not shape or shape[0] != n:
        raise HTTPException(400, f"array {name!r} has {shape[0] if shape else 0} rows, not {n}")
    if len(shape) == 1:
        if columns not in (None, "all"):
            raise HTTPException(400, f"array {name!r} is 1-D; columns do not apply")
        picked = values
    else:
        width = shape[1]
        cols = list(range(width)) if columns in (None, "all") else columns
        if not isinstance(cols, list) or not cols or any(
                not isinstance(c, int) or isinstance(c, bool) or not 0 <= c < width for c in cols):
            raise HTTPException(400, f"columns for {name!r} must be 'all' or indices < {width}")
        picked = [math.fsum(float(values[i * width + c]) for c in cols) for i in range(n)]
    out = []
    for value in picked:
        if isinstance(value, bool):
            value = float(value)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise HTTPException(400, f"array {name!r} holds a non-finite or non-numeric value")
        out.append(float(value))
    return out


def _labels(arrays: dict, name: str, n: Optional[int] = None) -> list[str]:
    if name not in arrays:
        raise HTTPException(400, f"no array named {name!r} in the upload")
    shape, values = arrays[name]
    if len(shape) != 1 or (n is not None and shape[0] != n):
        raise HTTPException(400, f"{name!r} must be a 1-D array with one label per query")
    return [str(v) for v in values]


# ----------------------------------------------------------------------------------- storage
def _write_gz(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, gzip.compress(orjson.dumps(payload), mtime=0))


@functools.lru_cache(maxsize=96)
def _read_gz_cached(path: str, mtime_ns: int, size: int) -> Any:
    return orjson.loads(gzip.decompress(Path(path).read_bytes()))


def _read_gz(path: Path) -> Any:
    st = path.stat()
    return _read_gz_cached(str(path), st.st_mtime_ns, st.st_size)


def read_manifest(rd: Path) -> tuple[list[dict], dict]:
    rows, health = read_source(manifest_path(rd))
    valid = [row for row in rows if isinstance(row.get("upload_id"), str)
             and isinstance(row.get("test_id"), str) and isinstance(row.get("node_id"), int)]
    health["invalid_record_rows"] = len(rows) - len(valid)
    return valid, health


def current_uploads(rows: list[dict], state=None) -> list[dict]:
    """The latest upload per (node, test id), each stamped `stale` when its node lifecycle was
    reset after the upload (the arrays describe an abandoned attempt)."""
    latest: dict[tuple, dict] = {}
    for row in rows:
        latest[(row["node_id"], row["test_id"])] = row
    out = []
    for row in latest.values():
        node = state.nodes.get(row["node_id"]) if state is not None else None
        stale = bool(node is not None and row.get("node_generation") is not None
                     and getattr(node, "attempt", None) != row.get("node_generation"))
        out.append({**row, "stale": stale})
    return out


# ------------------------------------------------------------------------------------ the upload
def _measured(node, key: Optional[str]) -> Optional[float]:
    if node is None:
        return None
    value = node.metric if not key else (node.extra_metrics or {}).get(key)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def publish_upload(srv, rd: Path, body, *, agent_token: bool) -> dict:
    """Validate, reduce and store one node's per-query arrays; idempotent on `action_id`."""
    from looplab.serve.control_validation import run_is_external
    if agent_token and not run_is_external(rd):
        raise HTTPException(403, {"code": "agent_token_refused",
                                  "message": "the agent token may upload only to an external_harness run",
                                  "retryable": False})
    raw = _upload_bytes(rd, body)
    source_sha = hashlib.sha256(raw).hexdigest()
    arrays = _arrays_from_payload(body.format, raw)
    queries = _labels(arrays, body.query_key)
    n = len(queries)
    if not 2 <= n <= MAX_QUERIES:
        raise HTTPException(400, f"an upload holds 2..{MAX_QUERIES} queries")
    if len(set(queries)) != n:
        raise HTTPException(400, "query ids must be unique within an upload")
    clusters = _labels(arrays, body.cluster_key, n) if body.cluster_key else list(queries)
    order = sorted(range(n), key=lambda i: queries[i])
    test_id = test_id_for(queries, clusters)
    metrics: dict[str, dict] = {}
    vectors: dict[str, dict] = {}
    if not body.metrics or len(body.metrics) > 64:
        raise HTTPException(400, "declare 1..64 metrics")
    for name, spec in body.metrics.items():
        if not METRIC_RE.fullmatch(name) or not isinstance(spec, dict):
            raise HTTPException(400, f"metric {name!r}: invalid name or spec")
        kind = spec.get("kind", "mean")
        columns = spec.get("columns", "all")
        if kind == "mean":
            if set(spec) - {"kind", "array", "columns"}:
                raise HTTPException(400, f"metric {name!r}: a mean metric is {{array, columns?}}")
            values = _vector(arrays, spec.get("array"), columns, n)
            values = [values[i] for i in order]
            summary = paired.mean_with_ci(values, [clusters[i] for i in order])
            vectors[name] = {"kind": "mean", "values": values}
        elif kind == "ratio":
            if set(spec) - {"kind", "num", "den", "columns"}:
                raise HTTPException(400, f"metric {name!r}: a ratio metric is {{num, den, columns?}}")
            num = _vector(arrays, spec.get("num"), columns, n)
            den = _vector(arrays, spec.get("den"), columns, n)
            num, den = [num[i] for i in order], [den[i] for i in order]
            try:
                summary = paired.ratio_with_ci(num, den, [clusters[i] for i in order])
            except paired.PairedInputError as exc:
                raise HTTPException(400, f"metric {name!r}: {exc}") from None
            vectors[name] = {"kind": "ratio", "num": num, "den": den}
        else:
            raise HTTPException(400, f"metric {name!r}: kind must be mean or ratio")
        metrics[name] = {"kind": kind, "source": {k: v for k, v in spec.items() if k != "kind"},
                         "value": summary["estimate"], "se": summary["se"],
                         "ci95_low": summary["ci95_low"], "ci95_high": summary["ci95_high"]}
    if body.test_label is not None and not SLUG_RE.fullmatch(body.test_label):
        raise HTTPException(400, "test_label must be a short slug")
    if body.experiment_id is not None and not SLUG_RE.fullmatch(body.experiment_id):
        raise HTTPException(400, "experiment_id must be a slug")
    if (body.arm is None) != (body.experiment_id is None):
        raise HTTPException(400, "arm and experiment_id are given together")
    path = manifest_path(rd)
    try:
        with srv.commands.sequence(rd):
            events = read_event_source(rd)
            generation = run_generation_token(events)
            if not generation or generation != body.expected_generation.lower():
                raise HTTPException(409, {"code": "run_generation_changed",
                                          "current_generation": generation})
            state = fold(events)
            node = state.nodes.get(body.node_id)
            if node is None or getattr(node, "tombstoned", False):
                raise HTTPException(404, f"no node {body.node_id} in this run")
            status = getattr(node.status, "value", str(node.status))
            if status not in ("evaluated", "failed") or node.terminal_event_seq is None:
                raise HTTPException(409, {"code": "node_not_terminal",
                                          "message": "upload per-query arrays for a terminal node"})
            consistency = {"status": "unchecked"}
            if body.measured_check is not None:
                check = body.measured_check
                metric = check.get("metric")
                if metric not in metrics:
                    raise HTTPException(400, "measured_check.metric must be an uploaded metric")
                key = check.get("node_metric")
                measured = _measured(node, key)
                value = metrics[metric]["value"]
                if measured is None:
                    consistency = {"status": "no_measured_value", "metric": metric,
                                   "node_metric": key or "objective"}
                else:
                    gap = abs(value - measured)
                    ok = gap <= CONSISTENCY_TOLERANCE * max(1.0, abs(measured))
                    consistency = {"status": "matches_measured" if ok else "differs_from_measured",
                                   "metric": metric, "node_metric": key or "objective",
                                   "measured": measured, "uploaded_mean": value, "abs_gap": gap}
            if body.experiment_id is not None:
                rows, _h = read_journal(rd)
                exp = experiments_of(rows, state.run_uid).get(body.experiment_id)
                if exp is None:
                    raise HTTPException(404, f"no registered experiment {body.experiment_id!r}")
                if body.arm not in exp["arms"]:
                    raise HTTPException(400, f"experiment {body.experiment_id!r} has no arm {body.arm!r}")
                fixed = exp["arms"][body.arm]
                if fixed.get("run_id") not in (None, Path(rd).name) or (
                        fixed.get("node_id") is not None and fixed["node_id"] != body.node_id):
                    raise HTTPException(409, {"code": "arm_bound_elsewhere",
                                              "message": "the experiment declared this arm on another node"})
            path.parent.mkdir(parents=True, exist_ok=True)
            with interprocess_lock(Path(str(path) + ".lock"), required=True):
                rows, health = read_manifest(rd)
                if not health["read_complete"]:
                    raise HTTPException(503, {"code": "harness_history_incomplete",
                                              "source": f"{PER_QUERY_DIR}/{MANIFEST}"})
                request_digest = hashlib.sha256(orjson.dumps(
                    body.request_payload(source_sha), option=orjson.OPT_SORT_KEYS)).hexdigest()
                for old in rows:
                    if old.get("run_uid") == state.run_uid and old.get("action_id") == body.action_id:
                        if old.get("request_sha256") != request_digest:
                            raise HTTPException(409, "upload action_id was reused with different content")
                        return {"ok": True, "replayed": True, "upload": old}
                if body.experiment_id is not None:
                    for old in rows:
                        if (old.get("run_uid") == state.run_uid
                                and old.get("experiment_id") == body.experiment_id
                                and old.get("arm") == body.arm and old.get("node_id") != body.node_id):
                            raise HTTPException(409, {
                                "code": "arm_bound_elsewhere",
                                "message": (f"arm {body.arm!r} is already bound to node "
                                            f"{old.get('node_id')}; an arm is one node")})
                upload_id = f"pq{len(rows) + 1}-{request_digest[:10]}"
                tests_dir = Path(rd) / PER_QUERY_DIR / "tests"
                test_file = tests_dir / f"{test_id}.json.gz"
                if not test_file.exists():
                    _write_gz(test_file, {"test_id": test_id,
                                          "query_ids": [queries[i] for i in order],
                                          "clusters": [clusters[i] for i in order]})
                data_file = f"{PER_QUERY_DIR}/{upload_id}.json.gz"
                _write_gz(Path(rd) / data_file, {"upload_id": upload_id, "test_id": test_id,
                                                 "metrics": vectors})
                row = {"v": 1, "upload_id": upload_id, "action_id": body.action_id,
                       "request_sha256": request_digest, "run_uid": state.run_uid,
                       "run_id": Path(rd).name, "generation": generation,
                       "node_id": body.node_id, "node_generation": node.attempt,
                       "node_status": status, "node_terminal_seq": node.terminal_event_seq,
                       "test_id": test_id, "test_label": body.test_label,
                       "n_queries": n, "n_clusters": len(set(clusters)),
                       "metrics": metrics, "consistency": consistency,
                       "experiment_id": body.experiment_id, "arm": body.arm,
                       "source": {"format": body.format, "sha256": source_sha, "bytes": len(raw),
                                  "origin": ("node_workdir:" + body.workdir_path
                                             if body.workdir_path else "agent_upload"
                                             if agent_token else "operator_upload"),
                                  "query_key": body.query_key, "cluster_key": body.cluster_key},
                       "author": author_of(agent_token), "recorded_at": time.time(),
                       "at_event_seq": events[-1].seq, "data_file": data_file}
                append_jsonl_bytes_locked(path, orjson.dumps(row, option=orjson.OPT_SORT_KEYS))
                return {"ok": True, "replayed": False, "upload": row}
    except HTTPException:
        raise
    except (OSError, EventStoreLockError, ValueError, KeyError) as exc:
        raise HTTPException(503, "per-query store unavailable") from exc


def _upload_bytes(rd: Path, body) -> bytes:
    if (body.content_b64 is None) == (body.workdir_path is None):
        raise HTTPException(400, "supply exactly one of content_b64 or workdir_path")
    if body.content_b64 is not None:
        if len(body.content_b64) > MAX_UPLOAD_BYTES * 4 // 3 + 8:
            raise HTTPException(413, f"an upload is at most {MAX_UPLOAD_BYTES} bytes")
        try:
            return base64.b64decode(body.content_b64, validate=True)
        except (ValueError, TypeError):
            raise HTTPException(400, "content_b64 is not base64") from None
    # A file the node's OWN evaluation wrote into its workdir: the provenance a reviewer trusts most.
    from looplab.core.node_evidence import read_bounded_regular_file
    from looplab.core.pathsafe import contained_member
    workdir = Path(rd) / "nodes" / f"node_{body.node_id}"
    target = contained_member(workdir, body.workdir_path)
    if target is None:
        raise HTTPException(400, "workdir_path must be inside the node's workdir")
    data = read_bounded_regular_file(target, MAX_UPLOAD_BYTES)
    if data is None:
        raise HTTPException(404, "workdir_path is not a readable regular file within the size bound")
    return data


# --------------------------------------------------------------------------------- comparisons
class _Root:
    """Read-side cache of run folds and manifests for ONE request (runs are read at most once)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._runs: dict[str, Any] = {}

    def run(self, run_id: str):
        if run_id not in self._runs:
            rd = self.root / run_id
            try:
                events = EventStore(rd / "events.jsonl").read_all()
                state = fold(events)
                rows, _health = read_manifest(rd)
                proto, _ph = read_journal(rd)
                self._runs[run_id] = (rd, state, events, current_uploads(rows, state), proto)
            except (OSError, ValueError):
                self._runs[run_id] = None
        return self._runs[run_id]


def _load_vectors(rd: Path, upload: dict, metric: str) -> Optional[dict]:
    try:
        doc = _read_gz(Path(rd) / upload["data_file"])
    except (OSError, ValueError, KeyError):
        return None
    return (doc.get("metrics") or {}).get(metric)


def _load_clusters(rd: Path, test_id: str) -> Optional[list]:
    try:
        return _read_gz(Path(rd) / PER_QUERY_DIR / "tests" / f"{test_id}.json.gz").get("clusters")
    except (OSError, ValueError):
        return None


def _upload_provenance(run_id: str, upload: dict) -> dict:
    return {"run_id": run_id, "node_id": upload["node_id"],
            "node_generation": upload.get("node_generation"), "upload_id": upload["upload_id"],
            "source_sha256": (upload.get("source") or {}).get("sha256"),
            "origin": (upload.get("source") or {}).get("origin"),
            "author": upload.get("author"), "recorded_at": upload.get("recorded_at"),
            "consistency": (upload.get("consistency") or {}).get("status"),
            "stale": upload.get("stale", False), "test_label": upload.get("test_label")}


def _resolve_arm(cache: _Root, home_run: str, exp: dict, label: str, test: dict) -> dict:
    """The upload an arm reads for a test: the declared (run, node) or, in the home run, the node an
    upload bound to this experiment's arm."""
    arm = exp["arms"][label]
    run_id = arm.get("run_id") or home_run
    loaded = cache.run(run_id)
    if loaded is None:
        return {"error": f"run {run_id!r} is unreadable"}
    rd, state, events, uploads, _proto = loaded
    node_id = arm.get("node_id")
    if node_id is None:
        bound = [u for u in uploads if u.get("experiment_id") == exp["experiment_id"]
                 and u.get("arm") == label and run_id == home_run]
        node_id = bound[-1]["node_id"] if bound else None
    if node_id is None:
        return {"error": f"arm {label!r} has no node yet (upload with experiment_id/arm, or declare node_id)"}
    matches = [u for u in uploads if u["node_id"] == node_id and (
        u["test_id"] == test["test_id"] if test.get("test_id")
        else u.get("test_label") == test["label"])]
    if not matches:
        return {"error": f"node {node_id} of run {run_id!r} has no upload for test {test['label']!r}",
                "run_id": run_id, "node_id": node_id}
    upload = sorted(matches, key=lambda u: u.get("recorded_at") or 0)[-1]
    node = state.nodes.get(node_id)
    terminal_ts = None
    if node is not None and node.terminal_event_seq is not None:
        terminal_ts = next((e.ts for e in events if e.seq == node.terminal_event_seq), None)
    return {"rd": rd, "run_id": run_id, "node_id": node_id, "upload": upload,
            "terminal_seq": getattr(node, "terminal_event_seq", None), "terminal_ts": terminal_ts,
            "protocol_version": getattr(node, "protocol_version", None)}


def compare_experiment(root: Path, run_id: str, experiment_id: str, *,
                       bootstrap: Optional[int] = None, seed: int = 0) -> dict:
    cache = _Root(root)
    loaded = cache.run(run_id)
    if loaded is None:
        raise HTTPException(404, "run is unreadable")
    rd, state, _events, _uploads, proto = loaded
    exp = experiments_of(proto, state.run_uid).get(experiment_id)
    if exp is None:
        raise HTTPException(404, f"no registered experiment {experiment_id!r}")
    replicates = bootstrap if bootstrap is not None else (
        exp["bootstrap_replicates"] if exp.get("ci") == "bootstrap" else None)
    metric_kind = {m["name"]: m["kind"] for m in exp["metrics"]}
    direction = {m["name"]: m["direction"] for m in exp["metrics"]}
    tests = {t["label"]: t for t in exp["tests"]}
    registration = exp["registration"]
    rows = []
    for contrast in exp["contrasts"]:
        test = tests[contrast["test"]]
        t = _resolve_arm(cache, run_id, exp, contrast["treatment"], test)
        c = _resolve_arm(cache, run_id, exp, contrast["control"], test)
        row: dict[str, Any] = {**contrast, "direction": direction[contrast["metric"]]}
        problems = [x["error"] for x in (t, c) if "error" in x]
        if problems:
            rows.append({**row, "status": "unavailable", "reason": "; ".join(problems)})
            continue
        tu, cu = t["upload"], c["upload"]
        if tu["test_id"] != cu["test_id"]:
            rows.append({**row, "status": "unavailable",
                         "reason": "the two arms were scored on different query sets"})
            continue
        if test.get("test_id") and tu["test_id"] != test["test_id"]:
            rows.append({**row, "status": "unavailable",
                         "reason": "the uploads do not match the experiment's frozen test id"})
            continue
        clusters = _load_clusters(t["rd"], tu["test_id"])
        tv = _load_vectors(t["rd"], tu, contrast["metric"])
        cv = _load_vectors(c["rd"], cu, contrast["metric"])
        if clusters is None or tv is None or cv is None:
            rows.append({**row, "status": "unavailable",
                         "reason": f"metric {contrast['metric']!r} is missing from an upload"})
            continue
        kind = metric_kind[contrast["metric"]]
        if tv.get("kind") != kind or cv.get("kind") != kind:
            rows.append({**row, "status": "unavailable",
                         "reason": f"uploads declare {contrast['metric']!r} as a different kind"})
            continue
        try:
            if kind == "mean":
                stats = paired.paired_mean_delta(tv["values"], cv["values"], clusters)
                boot_in = {"t": tv["values"], "c": cv["values"]}
            else:
                stats = paired.paired_ratio_delta(tv["num"], tv["den"], cv["num"], cv["den"], clusters)
                boot_in = {"nt": tv["num"], "dt": tv["den"], "nc": cv["num"], "dc": cv["den"]}
        except paired.PairedInputError as exc:
            rows.append({**row, "status": "unavailable", "reason": str(exc)})
            continue
        row.update({"status": "computed", "test_id": tu["test_id"],
                    "delta": stats["estimate"], "se": stats["se"],
                    "ci95_low": stats["ci95_low"], "ci95_high": stats["ci95_high"],
                    "p_two_sided": stats["p_two_sided"], "n": stats["n"],
                    "clusters": stats["clusters"],
                    "treatment_value": stats["treatment_value"],
                    "control_value": stats["control_value"],
                    "treatment_source": _upload_provenance(t["run_id"], tu),
                    "control_source": _upload_provenance(c["run_id"], cu),
                    "registered_before_result": _before_result(registration, run_id, t),
                    # doc 81 §8 leftover: the protocol version each arm's node was CREATED under
                    # (`Node.protocol_version`, folded from its `node_created` row). Differing
                    # versions in one run are a caveat, never a refusal: the reader decides.
                    "treatment_protocol_version": t.get("protocol_version"),
                    "control_protocol_version": c.get("protocol_version"),
                    "same_protocol": (None if t["run_id"] != c["run_id"]
                                      or t.get("protocol_version") is None
                                      or c.get("protocol_version") is None
                                      else t["protocol_version"] == c["protocol_version"])})
        if replicates:
            row["bootstrap"] = paired.cluster_bootstrap(kind, boot_in, clusters,
                                                        replicates=replicates, seed=seed)
        rows.append(row)
    families: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        if row.get("status") == "computed":
            families.setdefault(row["family"], []).append(i)
    for family, indices in families.items():
        adjusted = paired.holm([rows[i]["p_two_sided"] for i in indices]) \
            if exp["correction"] == "holm" else [rows[i]["p_two_sided"] for i in indices]
        for i, p in zip(indices, adjusted):
            rows[i]["p_adjusted"] = p
            rows[i]["family_size"] = len(indices)
            rows[i]["significant"] = bool(p is not None and p < exp["alpha"])
    return {"version": 1, "run_id": run_id, "experiment": exp, "contrasts": rows,
            "computed_by": {"engine": "looplab", "method": paired.METHOD_VERSION,
                            "ci": "normal approximation, user-clustered (G/(G-1))",
                            "correction": exp["correction"], "alpha": exp["alpha"],
                            "bootstrap_replicates": replicates, "seed": seed,
                            "computed_at": time.time()},
            "caveats": _caveats(rows)}


def _before_result(registration: dict, home_run: str, arm: dict) -> Optional[bool]:
    """Was the experiment registered before the TREATMENT arm's result existed? In the home run by
    event seq; for a node of another run by wall clock. None when either side is unknown."""
    if arm["run_id"] == home_run:
        seq = registration.get("at_event_seq")
        if isinstance(seq, int) and isinstance(arm.get("terminal_seq"), int):
            return seq < arm["terminal_seq"]
        return None
    at, ts = registration.get("recorded_at"), arm.get("terminal_ts")
    return (at < ts) if isinstance(at, (int, float)) and isinstance(ts, (int, float)) else None


def _caveats(rows: list[dict]) -> list[str]:
    out = []
    if any(r.get("registered_before_result") is False for r in rows):
        out.append("some contrasts were registered AFTER their treatment result existed (post hoc)")
    if any(r.get("same_protocol") is False for r in rows):
        out.append("some contrasts compare nodes created under DIFFERENT protocol versions of the "
                   "run (a task/files amendment landed between them): see the protocol journal")
    if any((r.get(side) or {}).get("stale") for r in rows for side in ("treatment_source", "control_source")):
        out.append("some uploads describe a node lifecycle that was later reset (stale)")
    if any((r.get(side) or {}).get("consistency") == "differs_from_measured"
           for r in rows for side in ("treatment_source", "control_source")):
        out.append("some uploads do not reproduce the metric LoopLab measured for their node")
    if rows:
        out.append("intervals are per contrast (not simultaneous); clusters are users, so "
                   "training-seed variance is not included")
    return out


def run_ids_under(root: Path) -> list[str]:
    try:
        return sorted(p.name for p in Path(root).iterdir()
                      if p.is_dir() and (p / PER_QUERY_DIR / MANIFEST).is_file())
    except OSError:
        return []


def shared_test_leaderboard(root: Path, test_id: str, *, metric: Optional[str] = None,
                            reference: Optional[str] = None, run_ids: Optional[list] = None) -> dict:
    """Every (run, node) under `root` with an upload for `test_id`: absolute values with clustered
    intervals and, against a reference `run:node`, paired deltas."""
    if not re.fullmatch(r"qs1-[0-9a-f]{32}", test_id or ""):
        raise HTTPException(400, "test_id must be a shared test id (qs1-…)")
    cache = _Root(root)
    entries = []
    for rid in (run_ids if run_ids is not None else run_ids_under(root)):
        loaded = cache.run(rid)
        if loaded is None:
            continue
        rd, state, _events, uploads, _proto = loaded
        for upload in uploads:
            if upload["test_id"] == test_id:
                node = state.nodes.get(upload["node_id"])
                idea = getattr(node, "idea", None)
                entries.append({"rd": rd, "run_id": rid, "upload": upload,
                                "label": upload.get("arm") or getattr(idea, "theme", None)
                                or getattr(idea, "operator", None)})
    metric_names = sorted({m for e in entries for m in (e["upload"].get("metrics") or {})})
    chosen = metric or (metric_names[0] if metric_names else None)
    ref = None
    if reference:
        rid, _, nid = reference.partition(":")
        ref = next((e for e in entries if e["run_id"] == rid and str(e["upload"]["node_id"]) == nid), None)
        if ref is None:
            raise HTTPException(404, "reference run:node has no upload for this test")
    rows = []
    clusters = _load_clusters(entries[0]["rd"], test_id) if entries else None
    ref_vec = _load_vectors(ref["rd"], ref["upload"], chosen) if ref and chosen else None
    for e in entries:
        m = (e["upload"].get("metrics") or {}).get(chosen) if chosen else None
        row = {**_upload_provenance(e["run_id"], e["upload"]), "label": e["label"],
               "metric": chosen, "kind": (m or {}).get("kind"), "value": (m or {}).get("value"),
               "ci95_low": (m or {}).get("ci95_low"), "ci95_high": (m or {}).get("ci95_high")}
        if ref_vec is not None and m is not None and e is not ref and clusters is not None:
            vec = _load_vectors(e["rd"], e["upload"], chosen)
            try:
                if vec and vec.get("kind") == "mean" and ref_vec.get("kind") == "mean":
                    d = paired.paired_mean_delta(vec["values"], ref_vec["values"], clusters)
                elif vec and vec.get("kind") == "ratio" and ref_vec.get("kind") == "ratio":
                    d = paired.paired_ratio_delta(vec["num"], vec["den"], ref_vec["num"],
                                                  ref_vec["den"], clusters)
                else:
                    d = None
            except paired.PairedInputError:
                d = None
            if d is not None:
                row["vs_reference"] = {k: d[k] for k in ("estimate", "se", "ci95_low",
                                                         "ci95_high", "p_two_sided")}
        rows.append(row)
    rows.sort(key=lambda r: (r["value"] is None, -(r["value"] or 0.0)))
    return {"version": 1, "test_id": test_id, "metric": chosen, "metrics": metric_names,
            "reference": reference, "entries": rows,
            "computed_by": {"engine": "looplab", "method": paired.METHOD_VERSION,
                            "ci": "normal approximation, user-clustered", "computed_at": time.time()}}


def run_per_query_view(rd: Path) -> dict:
    rows, health = read_manifest(rd)
    try:
        state = fold(EventStore(Path(rd) / "events.jsonl").read_all())
    except (OSError, ValueError):
        state = None
    uploads = [{k: v for k, v in row.items() if k != "request_sha256"}
               for row in current_uploads(rows, state)]
    tests: dict[str, dict] = {}
    for u in uploads:
        t = tests.setdefault(u["test_id"], {"test_id": u["test_id"], "labels": set(),
                                            "n_queries": u.get("n_queries"), "nodes": 0})
        t["labels"].add(u.get("test_label") or "")
        t["nodes"] += 1
    return {"version": 1, "uploads": uploads, "history_rows": len(rows),
            "tests": [{**t, "labels": sorted(x for x in t["labels"] if x)} for t in tests.values()],
            "source_health": health}


__all__ = ["MANIFEST", "PER_QUERY_DIR", "compare_experiment", "current_uploads", "publish_upload",
           "read_manifest", "run_ids_under", "run_per_query_view", "shared_test_leaderboard",
           "test_id_for"]
