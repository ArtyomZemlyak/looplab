"""Doc 72 §11: event-bound seed archives in the reviewer bundle.

Only receipts present in the log nominate files. Historical seed/terminal rows
stay separate; identical valid content identities share a copy. Export availability
is an appendix, never a rewrite of the run's receipt or a replay/equivalence claim.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from looplab.core.atomicio import atomic_write_text
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.core.pathsafe import contained_member, is_reparse

BASE_INDEX = "base_snapshots/index.json"
MAX_INDEX_BYTES = 32 * 1024 * 1024


def seed_archive_records(events):
    """References exactly as recorded, including superseded lifecycles and unknown receipts."""
    records = []
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        source = data
        if event.type == "node_evaluated":
            source = data.get("metric_provenance")
        elif event.type != "workspace_seeded":
            continue
        if not isinstance(source, dict) or "base_revision" not in source:
            continue
        row = {"event_seq": event.seq, "event_type": event.type,
               "node_id": data.get("node_id"), "receipt": source["base_revision"]}
        if "generation" in data:
            row["generation"] = data["generation"]
        records.append(row)
    return records


def export_seed_archives(run_dir, out_dir, events, members):
    from looplab.engine.seed_archive import copy_seed_archive, seed_archive_digest
    records = seed_archive_records(events)
    if not records:
        return None  # legacy bundles retain their existing shape
    out_dir = Path(out_dir)
    root = out_dir / "base_snapshots"
    if os.path.lexists(root) and (is_reparse(root.lstat()) or not root.is_dir()):
        raise ValueError("refusing a linked or non-directory bundle archive root")
    copied, entries = {}, []
    for record in records:
        receipt = record["receipt"]
        digest = seed_archive_digest(receipt)
        key = (digest, receipt["file_count"], receipt["bytes"]) if digest is not None else None
        if key is None:
            result = {"status": "unavailable", "reason": "unusable_archive_receipt", "path": None}
        elif key in copied:
            result = copied[key]
        else:
            archive, names = copy_seed_archive(run_dir, out_dir, receipt)
            result = {"status": "exported" if archive["status"] == "stored" else "unavailable",
                      "reason": archive["reason"], "path": archive["path"]}
            copied[key] = result
            for name in names:
                members.append((f"{archive['path']}/{name}", "the recorded copied-base file before node overlay"))
        entries.append({**record, **result})
    body = json.dumps({"version": 1, "entries": entries}, indent=1, ensure_ascii=False)
    if len(body.encode("utf-8")) > MAX_INDEX_BYTES:
        raise ValueError("seed archive index exceeds its 32 MiB verification budget")
    root.mkdir(mode=0o700, exist_ok=True)
    if is_reparse(root.lstat()):
        raise ValueError("refusing a linked bundle archive root")
    atomic_write_text(out_dir / BASE_INDEX, body)
    members.append((BASE_INDEX, "recorded seed references and their export-time availability"))
    exported = sum(row["status"] == "exported" for row in entries)
    return {"recorded_receipts": len(entries), "exported_receipts": exported,
            "unavailable_receipts": len(entries) - exported,
            "archives": sum(result["status"] == "exported" for result in copied.values())}


def verify_seed_archives(out_dir, listed_files):
    from looplab.engine.seed_archive import seed_archive_digest, verified_seed_archive
    from looplab.events.eventstore import EventStore
    out_dir = Path(out_dir)
    try:
        event_path = out_dir / "events.jsonl"
        entry = event_path.lstat()
        if (is_reparse(entry) or not stat.S_ISREG(entry.st_mode)
                or contained_member(out_dir, "events.jsonl") is None):
            return ["unsafe event log for seed archive verification"]
        records = seed_archive_records(EventStore(event_path).read_all())
    except (OSError, ValueError):
        return ["unreadable event log for seed archive verification"]
    if not records:
        return []
    if BASE_INDEX not in listed_files:
        return ["seed archive index is missing from the crate"]
    try:
        root = out_dir / "base_snapshots"
        if is_reparse(root.lstat()) or not root.is_dir():
            return ["unsafe seed archive index directory"]
    except OSError:
        return ["missing seed archive index directory"]
    body = read_bounded_regular_file(out_dir / BASE_INDEX, MAX_INDEX_BYTES + 1)
    if body is None or len(body) > MAX_INDEX_BYTES:
        return ["missing or unreadable seed archive index"]
    try:
        index = json.loads(body)
    except (ValueError, UnicodeError):
        return ["invalid seed archive index"]
    if (not isinstance(index, dict) or type(index.get("version")) is not int
            or index["version"] != 1 or not isinstance(index.get("entries"), list)
            or len(index["entries"]) != len(records)):
        return ["seed archive index does not match recorded references"]
    defects, checked = [], set()
    for row, record in zip(index["entries"], records):
        if (not isinstance(row, dict) or
                {k: v for k, v in row.items() if k not in {"status", "reason", "path"}} != record):
            defects.append("seed archive reference differs from the event log")
            continue
        digest = seed_archive_digest(record["receipt"])
        if row.get("status") == "unavailable" and row.get("path") is None and isinstance(row.get("reason"), str) and row["reason"]:
            continue  # explicitly omitted, never a verified base
        if (row.get("status") != "exported" or digest is None or row.get("reason") is not None
                or row.get("path") != f"base_snapshots/{digest}"):
            defects.append("invalid seed archive export outcome")
            continue
        key = (digest, record["receipt"]["file_count"], record["receipt"]["bytes"])
        if key not in checked:
            checked.add(key)
            prefix = f"base_snapshots/{digest}/"
            names = set()
            archive = verified_seed_archive(out_dir, record["receipt"],
                on_file=lambda name, data, executable: names.add(prefix + name))
            if archive is None:
                defects.append(f"seed archive content or executable bits mismatch {digest}")
            elif {name for name in listed_files if name.startswith(prefix)} != names:
                defects.append(f"seed archive members differ from the crate {digest}")
    return defects
