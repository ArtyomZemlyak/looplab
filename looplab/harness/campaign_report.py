"""The CROSS-RUN campaign report of a super-task: comparisons LoopLab computed + the agent's narrative.

A super-task (`serve/projects.py`) groups runs that attack one research campaign. Its report view
(doc 81) is assembled here from three sources, each labelled with who produced it:

* the runs assigned to the super-task, and for each run its registered experiments with their
  contrast tables — computed by LoopLab (`harness/per_query.py::compare_experiment`);
* a leaderboard per shared test id any of those runs uploaded for — every node of every run under
  the root scored on that test (`shared_test_leaderboard`), computed by LoopLab;
* the NARRATIVE: markdown written by the external agent (or the operator), versioned in
  `<run-root>/.campaign-reports/<supertask_id>.jsonl`. Each version names its author and the revision
  it replaced (compare-and-swap on `expected_revision`); the narrative never carries a number the
  table does not, and the UI renders the two apart so a reader can tell prose from measurement.

The field names follow the run report's (`body_markdown`, `key_findings`) so a reader that renders
one renders the other.
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Optional

import orjson
from fastapi import HTTPException

from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.events.eventstore import EventStoreLockError, interprocess_lock
from looplab.harness.campaign import author_of, experiments_of, read_journal
from looplab.harness.journals import history_incomplete, read_source, shape_checked
from looplab.harness.per_query import (compare_experiment, read_manifest,
                                       shared_test_leaderboard)

REPORT_DIR = ".campaign-reports"
MAX_MARKDOWN = 64 * 1024
_SID_RE = re.compile(r"^st_[0-9a-f]{10}$")


def report_path(root: Path, supertask_id: str) -> Path:
    if not _SID_RE.fullmatch(supertask_id or ""):
        raise HTTPException(400, "supertask_id must be a super-task id (st_…)")
    return Path(root) / REPORT_DIR / f"{supertask_id}.jsonl"


def _narrative_row(row: dict) -> bool:
    return isinstance(row.get("revision"), int) and isinstance(row.get("body_markdown"), str)


def narrative_versions(root: Path, supertask_id: str) -> list[dict]:
    rows, health = read_source(report_path(root, supertask_id))
    return shape_checked(rows, health, _narrative_row)         # display keeps the readable rows


def publish_narrative(root: Path, supertask_id: str, body, *, agent_token: bool,
                      supertask_exists) -> dict:
    if not supertask_exists(supertask_id):
        raise HTTPException(404, f"no super-task {supertask_id!r}")
    text = body.body_markdown
    if len(text.encode("utf-8")) > MAX_MARKDOWN:
        raise HTTPException(413, f"body_markdown is at most {MAX_MARKDOWN} bytes")
    path = report_path(root, supertask_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with interprocess_lock(Path(str(path) + ".lock"), required=True):
            rows, health = read_source(path)
            # The SHAPE check downgrades the health too (`journals.shape_checked`): a dropped row
            # may be the revision this action_id already published (review 2026-10-16, A5).
            versions = shape_checked(rows, health, _narrative_row)
            if not health["read_complete"]:
                raise history_incomplete(path, f"{REPORT_DIR}/{path.name}", _narrative_row)
            request = {"title": body.title, "body_markdown": text,
                       "key_findings": list(body.key_findings),
                       "expected_revision": body.expected_revision}
            digest = hashlib.sha256(orjson.dumps(request, option=orjson.OPT_SORT_KEYS)).hexdigest()
            for old in versions:
                if old.get("action_id") == body.action_id:
                    if old.get("request_sha256") != digest:
                        raise HTTPException(409, "narrative action_id was reused with different content")
                    return {"ok": True, "replayed": True, "narrative": old}
            current = versions[-1]["revision"] if versions else 0
            if body.expected_revision != current:
                raise HTTPException(409, {"code": "narrative_changed", "current_revision": current,
                                          "message": "the narrative changed since you read it",
                                          "remediation": "read the report again and edit the current revision"})
            row = {"v": 1, "supertask_id": supertask_id, "revision": current + 1,
                   "action_id": body.action_id, "request_sha256": digest, **request,
                   "author": author_of(agent_token), "recorded_at": time.time()}
            append_jsonl_bytes_locked(path, orjson.dumps(row, option=orjson.OPT_SORT_KEYS))
            return {"ok": True, "replayed": False, "narrative": row}
    except HTTPException:
        raise
    except (OSError, EventStoreLockError, ValueError) as exc:
        raise HTTPException(503, "campaign report store unavailable") from exc


def campaign_report(root: Path, supertask: dict, run_ids: list[str], *,
                    bootstrap: Optional[int] = None) -> dict:
    """Everything the super-task report view shows, computed now from the runs' own records."""
    runs, test_ids = [], {}
    for rid in sorted(run_ids):
        rd = Path(root) / rid
        if not (rd / "events.jsonl").is_file():
            continue
        proto, _health = read_journal(rd)
        uploads, _uh = read_manifest(rd)
        for up in uploads:
            test_ids.setdefault(up["test_id"], up.get("test_label"))
        experiments = []
        run_uid = next((row.get("run_uid") for row in reversed(proto) if row.get("run_uid")), None)
        for eid, exp in experiments_of(proto, run_uid).items():
            try:
                comparison = compare_experiment(root, rid, eid, bootstrap=bootstrap)
            except HTTPException as exc:
                comparison = {"error": exc.detail}
            experiments.append({"experiment_id": eid, "title": exp.get("title"),
                                "question": exp.get("question"), "hypothesis": exp.get("hypothesis"),
                                "superseded": exp.get("superseded"),
                                "registration": exp.get("registration"),
                                "contrasts": comparison.get("contrasts", []),
                                "caveats": comparison.get("caveats", []),
                                "error": comparison.get("error")})
        amendments = [{k: row.get(k) for k in ("amendment_id", "version", "kind", "author",
                                               "reason", "recorded_at", "summary")}
                      for row in proto if not run_uid or row.get("run_uid") == run_uid]
        runs.append({"run_id": rid, "experiments": experiments, "amendments": amendments,
                     "uploads": len(uploads)})
    leaderboards = []
    for test_id, label in test_ids.items():
        try:
            board = shared_test_leaderboard(root, test_id)
        except HTTPException:
            continue
        leaderboards.append({**board, "test_label": label})
    versions = narrative_versions(root, supertask["id"])
    return {"version": 1, "supertask": supertask, "runs": runs, "leaderboards": leaderboards,
            "narrative": versions[-1] if versions else None,
            "narrative_revisions": len(versions),
            "computed_by": {"engine": "looplab", "computed_at": time.time(),
                            "note": "tables are LoopLab's computation over uploaded per-query "
                                    "arrays; the narrative is authored text"}}


__all__ = ["REPORT_DIR", "campaign_report", "narrative_versions", "publish_narrative",
           "report_path"]
