"""Read-only completion receipts and evidence-bound external chat commentary.

Receipts are derived from the event fold, so reconnecting cannot duplicate a
completion message. Commentary is separate from metrics and the owner's chat;
it never contains executable actions or fulfills a research/report obligation.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import orjson
from fastapi import HTTPException

from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.core.config import read_config_snapshot
from looplab.core.fitness import is_usable_metric
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.engine.champion_caveats import champion_metric_caveats
from looplab.engine.comparability import comparability_status, record_of
from looplab.events.replay import flagged_node_ids, fold
from looplab.events.eventstore import EventStoreLockError, interprocess_lock
from looplab.events.eval_occupancy import _lifecycle
from looplab.events.types import EV_NODE_EVALUATED, EV_NODE_FAILED
from looplab.events.run_generation import run_generation_token
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.serve.engine_proc import _engine_liveness
from looplab.serve.http import refusal
from looplab.serve.node_comparison import completion_score_comparison
from looplab.core.redact import redact_secrets

_MAX_BYTES = 2 * 1024 * 1024
_FILE = "result_commentary.jsonl"
_CURSOR = re.compile(r"rn1\.([0-9a-f]{64})\.(run|node:[0-9]+:[0-9]+)\.([0-9a-f]{64})")


def _digest(value) -> str:
    return hashlib.sha256(orjson.dumps(value, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _comments(rd: Path) -> list[dict]:
    path = rd / _FILE
    try:
        path.lstat()
    except FileNotFoundError:
        return []
    raw = read_bounded_regular_file(path, _MAX_BYTES + 1)
    if raw is None or len(raw) > _MAX_BYTES or (raw and not raw.endswith(b"\n")):
        raise HTTPException(503, "result commentary ledger unavailable or incomplete")
    try:
        rows = [orjson.loads(line) for line in raw.splitlines()]
        fields = {"generation", "action_id", "receipt_id", "evidence_token", "summary"}
        if any(not isinstance(row, dict) or set(row) != fields
               or any(not isinstance(row.get(key), str) for key in fields)
               or len(row["summary"]) > 700 for row in rows):
            raise ValueError("invalid commentary row")
        return rows
    except (ValueError, RecursionError) as exc:
        raise HTTPException(503, "result commentary ledger incomplete") from exc


def _score(node):
    return float(node.metric) if is_usable_metric(node.metric) else None


def _measurement(node):
    # A changed provenance/confirmation/constraint can invalidate interpretation
    # even if the displayed scalar and violation count happen to stay the same.
    return node.model_dump(mode="json", include={
        "attempt", "status", "metric", "metric_provenance", "violations",
        "confirmed_mean", "confirmed_std", "confirmed_seeds", "confirmed_scores",
        "error", "error_reason", "tombstoned"})


def _receipts(srv, rd: Path, expected_generation: str) -> tuple[str, list[dict]]:
    from looplab.harness.obligations import evidence_revision

    rd, observed_generation = srv.commands.generation_fence(rd)
    if observed_generation != expected_generation.lower():
        raise HTTPException(409, {"code": "run_generation_changed"})
    events = srv.events(rd)
    generation = run_generation_token(events)
    if not generation or generation != expected_generation.lower():
        raise HTTPException(409, {"code": "run_generation_changed"})
    if (not srv.log_integrity(rd).get("complete")
            or read_bounded_regular_file(rd / "events.jsonl", 1, tail=True) != b"\n"):
        raise HTTPException(503, "result event source incomplete")
    state = fold(events)
    flagged = set(flagged_node_ids(state))
    terminal_seq = {}
    for event in events:
        if event.type in (EV_NODE_EVALUATED, EV_NODE_FAILED):
            lifecycle = _lifecycle(event)
            if lifecycle is not None:
                terminal_seq.setdefault(lifecycle, event.seq)
    rows = []
    for node in state.nodes.values():
        if node.tombstoned or node.status not in ("evaluated", "failed"):
            continue
        aborted = node.id in state.aborted_nodes
        parents = []
        for pid in node.parent_ids[:8]:
            parent = state.nodes.get(pid)
            if parent is None or parent.tombstoned or parent.status != "evaluated" or pid in state.aborted_nodes:
                continue
            if node.parent_generations.get(str(pid)) != parent.attempt:
                continue  # Resetting a parent cannot rewrite the child's historical comparison.
            parents.append({"node_id": pid, "attempt": parent.attempt, "score": _score(parent),
                            "comparability": comparability_status(record_of(node), record_of(parent))})
        row = {"id": f"node:{node.id}:{node.attempt}", "kind": "node", "node_id": node.id,
               "attempt": node.attempt, "status": "aborted" if aborted else node.status.value,
               "completed_seq": terminal_seq.get((node.id, node.attempt), -1),
               "score": _score(node) if not aborted and node.status == "evaluated" else None, "feasible": bool(node.feasible),
               "objective": state.objective_key or "task metric", "direction": state.direction,
               "confirmed_mean": node.confirmed_mean if not aborted and node.status == "evaluated" and is_usable_metric(node.confirmed_mean) else None,
               "confirmed_seeds": node.confirmed_seeds, "parents": parents,
               "score_comparison": completion_score_comparison(node, parents, state, flagged),
               "trust_flagged": node.id in flagged, "violations": len(node.violations),
               "salvaged": bool((node.metric_provenance or {}).get("salvaged"))
               if isinstance(node.metric_provenance, dict) else False,
               "failure": redact_secrets(node.error_reason or node.error or "")[:160] if node.status == "failed" else ""}
        row["evidence_token"] = _digest({"receipt": row, "measurement": _measurement(node),
            "parents": [_measurement(state.nodes[p["node_id"]]) for p in parents]})
        rows.append(row)
    rows.sort(key=lambda row: (row["completed_seq"], row["node_id"], row["attempt"]))
    # A trainer exit / stop request / finalization in progress is not a run result.
    incomplete = incomplete_finalize_scope(events) is not None or state.finalization_pending()
    if (state.finished and srv.phase(state, finalize_incomplete=incomplete) == "finished"
            and _engine_liveness(rd) is False):
        best = state.best()
        row = {"id": "run", "kind": "run", "status": "finished", "reason": state.stop_reason,
               "direction": state.direction, "objective": state.objective_key or "task metric",
               "evaluated": sum(n.status == "evaluated" and not n.tombstoned and n.id not in state.aborted_nodes
                                for n in state.nodes.values()),
               "failed": sum(n.status == "failed" and not n.tombstoned for n in state.nodes.values()),
               "selected_node": best.id if best else None, "attempt": best.attempt if best else None,
               "score": _score(best) if best else None,
               "confirmed_mean": best.confirmed_mean if best and is_usable_metric(best.confirmed_mean) else None,
               "confirmed_seeds": best.confirmed_seeds if best else None,
               "caveats": champion_metric_caveats(state), "evidence_revision": evidence_revision(state)}
        row["evidence_token"] = _digest({"receipt": row, "nodes": [r["evidence_token"] for r in rows]})
        rows.append(row)
    return generation, rows


def _snapshot(srv, rd: Path, expected_generation: str, *, limit: int = 50, cursor: str | None = None) -> dict:
    generation, rows = _receipts(srv, rd, expected_generation)
    # Page backwards from a real receipt, not a shifting offset. Appending new
    # completions or publishing commentary cannot skip/duplicate older receipts.
    # This is a current-evidence view, not an immutable snapshot of the whole run.
    scope = _digest({"run_dir": str(rd.resolve()), "generation": generation})
    end = len(rows)
    if cursor is not None:
        match = _CURSOR.fullmatch(cursor)
        if match is None:
            raise HTTPException(400, {"code": "result_notice_cursor_invalid",
                "message": "Invalid result notice cursor.", "remediation": "Read the latest page and use its next_cursor unchanged."})
        anchor = next((i for i, row in enumerate(rows) if row["id"] == match[2]
                       and row["evidence_token"] == match[3]), None)
        if match[1] != scope or anchor is None:
            raise HTTPException(409, {"code": "result_notice_cursor_changed",
                "message": "Result notice cursor belongs to another run or changed completion evidence.",
                "remediation": "Refresh state/generation and the latest result page; reconcile existing commentary before continuing."})
        end = anchor
    start = max(0, end - limit)
    page = rows[start:end]
    comments = _comments(rd)
    by_receipt = {(c["generation"], c["receipt_id"], c["evidence_token"]): c["summary"] for c in comments}
    for row in page:
        row["commentary"] = by_receipt.get((generation, row["id"], row["evidence_token"]))
    if srv.commands.generation_fence(rd)[1] != generation:
        raise HTTPException(409, {"code": "run_generation_changed"})
    next_cursor = f"rn1.{scope}.{page[0]['id']}.{page[0]['evidence_token']}" if start and page else None
    return {"version": 1, "generation": generation, "total": len(rows), "items": page,
            "has_more": start > 0, "next_cursor": next_cursor}


def snapshot(srv, rd: Path, expected_generation: str, *, limit: int = 50, cursor: str | None = None) -> dict:
    try:
        return _snapshot(srv, rd, expected_generation, limit=limit, cursor=cursor)
    except OSError as exc:
        raise refusal("run_path_unreadable") from exc


def publish(srv, rd: Path, body) -> dict:
    try:
        with srv.commands.sequence(rd):
            try:
                settings = read_config_snapshot(rd / "config.snapshot.json")
            except (OSError, ValueError, KeyError) as exc:
                raise refusal("config_snapshot_unreadable") from exc
            if not settings.external_harness:
                raise HTTPException(409, "external commentary requires an external harness run")
            generation, receipts = _receipts(srv, rd, body.expected_generation)
            row = {"generation": generation, "action_id": body.action_id,
                   "receipt_id": body.receipt_id, "evidence_token": body.evidence_token,
                   "summary": redact_secrets(body.summary)[:700]}
            old = _comments(rd)
            for saved in old:
                if saved.get("generation") == generation and saved.get("action_id") == body.action_id:
                    if saved != row:
                        raise HTTPException(409, "commentary action_id reused with different content")
                    return {"ok": True, "replayed": True}
            receipt = next((r for r in receipts if r["id"] == body.receipt_id), None)
            if receipt is None or receipt["evidence_token"] != body.evidence_token:
                raise HTTPException(409, "completion evidence changed or is not terminal")
            if any(c.get("generation") == generation and c.get("receipt_id") == body.receipt_id
                   and c.get("evidence_token") == body.evidence_token for c in old):
                raise HTTPException(409, "this result already has external commentary")
            payload = orjson.dumps(row)
            path = rd / _FILE
            if path.exists() and path.stat().st_size + len(payload) + 1 > _MAX_BYTES:
                raise HTTPException(413, "result commentary ledger full")
            with interprocess_lock(Path(str(path) + ".lock"), required=True):
                append_jsonl_bytes_locked(path, payload)
            return {"ok": True, "replayed": False}
    except (OSError, EventStoreLockError) as exc:
        raise HTTPException(503, "result commentary ledger unavailable") from exc
