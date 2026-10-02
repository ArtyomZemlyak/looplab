"""Doc 72 §15: immutable initial archive selection, not live base advancement.

The operator names a recorded seed and exact digest in a run/bundle. Every consumer
reads that verified archive; no supplied metric/receipt, git discovery or live source
fallback. A copied destination is checked before mounts/overlay/scoring. The origin
log/archive remain explicit required inputs; this is not equivalence or a gate.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from looplab.core.atomicio import atomic_write_bytes
from looplab.core.errors import ConfigRefusal
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.core.pathsafe import contained_member, is_reparse
from looplab.engine.seed_archive import seed_archive_digest, verified_seed_archive
from looplab.events.eventstore import decode_event_record, event_sequence_continues

MAX_PIN_LOG_BYTES = 32 * 1024 * 1024


def normalize_seed_base(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"run_dir", "event_seq", "digest"}:
        raise ValueError("seed_base requires exactly run_dir, event_seq and digest")
    root, seq, digest = value["run_dir"], value["event_seq"], value["digest"]
    if not isinstance(root, str) or not root or len(root) > 4096 or "\x00" in root:
        raise ValueError("seed_base.run_dir must name an absolute run or bundle directory")
    root = os.path.expandvars(os.path.expanduser(root))
    if not Path(root).is_absolute():
        raise ValueError("seed_base.run_dir must be absolute")
    if type(seq) is not int or seq < 0:
        raise ValueError("seed_base.event_seq must be a nonnegative integer")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("seed_base.digest must be the complete lower-case SHA-256")
    return {"run_dir": root, "event_seq": seq, "digest": digest}


def selected_seed_base(selector):
    """Read one bounded COMPLETE event snapshot; verify its named seed archive."""
    root = Path(selector["run_dir"])
    def refuse(reason):
        raise ConfigRefusal(f"seed_base: {reason}; restore the selected run/bundle log and archive, "
                            "or explicitly launch a new task with another initial base. No live fallback.")
    try:
        if is_reparse(root.lstat()) or not root.is_dir():
            refuse("origin is not a plain directory")
        body = read_bounded_regular_file(root / "events.jsonl", MAX_PIN_LOG_BYTES + 1)
        if body is None or len(body) > MAX_PIN_LOG_BYTES or not body.endswith(b"\n"):
            refuse("origin event log is missing, unreadable, oversized or incomplete")
        selected, expected = None, 0
        for line in body.splitlines():
            events = decode_event_record(json.loads(line), strict=True)
            if not event_sequence_continues(events, expected):
                refuse("origin event sequence is incomplete")
            for event in events:
                if event.seq == selector["event_seq"]:
                    selected = event
            expected = events[-1].seq + 1
        if selected is None or selected.type != "workspace_seeded":
            refuse("event_seq does not name a workspace_seeded event")
        receipt = selected.data.get("base_revision")
        if seed_archive_digest(receipt) != selector["digest"]:
            refuse("recorded seed does not match the selected digest or stored receipt")
        archive = verified_seed_archive(root, receipt)
        if archive is None:
            refuse("recorded seed archive is missing, changed or unsupported")
        return archive, receipt
    except ConfigRefusal:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        refuse("origin event log or archive is invalid")


def pinned_editables(mounts, selector):
    """Scouts/write tools/probes resolve the same source as evaluation."""
    archive, _ = selected_seed_base(selector)
    return [{**ed, "origin_path": ed.get("origin_path", ed["path"]), "path": str(archive if ed["name"] in ("", ".")
             else archive / ed["name"])} for ed in mounts]


def enforce_initial_seed_base(events, current, upstream=None):
    start = next((event for event in events if event.type == "run_started"), None)
    if start is not None and start.data.get("seed_base") != current:
        raise ConfigRefusal("seed_base differs from run_started; restore the initial selection or launch a new run. "
                            "Changing a live base requires the explicit upstream advancement gate.")
    if start is not None and start.data.get("upstream") != upstream:
        raise ConfigRefusal("upstream differs from run_started; restore the launched declaration or launch a new run")
    if current is not None:
        selected_seed_base(current)  # missing/damaged origin refuses BEFORE recovery or workspace cleanup


def seed_pinned_workspace(selector, mounts, work):
    from looplab.engine.workspace_seed import seeded_base_revision
    work = Path(work)
    if is_reparse(work.lstat()) or not stat.S_ISDIR(work.lstat().st_mode) or any(work.iterdir()):
        raise ConfigRefusal("seed_base requires an empty plain candidate directory; rebuild the workspace first")
    archive, receipt = selected_seed_base(selector)
    counts = {ed["name"]: [0, 0] for ed in mounts}
    ordered = sorted(mounts, key=lambda ed: 0 if ed["name"] in ("", ".") else len(ed["name"]), reverse=True)
    def add(name, data, executable):
        owner = next((ed["name"] for ed in ordered if ed["name"] in ("", ".")
                      or name.startswith(ed["name"].rstrip("/") + "/")), None)
        if owner is None:
            raise ConfigRefusal("seed_base contains files outside the declared editable namespaces")
        target = contained_member(work, name)
        if target is None:
            raise ConfigRefusal("seed_base contains an unsafe destination path")
        atomic_write_bytes(target, data, mode=0o644 | executable)
        counts[owner][0] += 1
        counts[owner][1] += len(data)
    try:
        copied = seeded_base_revision(archive, on_file=add)
        actual = seeded_base_revision(work)
    except OSError as exc:
        raise ConfigRefusal("seed_base copy failed; restore candidate storage and the selected archive") from exc
    if any(not row["complete"] or any(row[k] != receipt[k]
            for k in ("version", "scope", "digest", "file_count", "bytes")) for row in (copied, actual)):
        raise ConfigRefusal("seed_base changed during copy or destination verification failed; no evaluation permitted")
    return [{"kind": "editable", "name": ed["name"], "mode": "pinned",
             "count": counts[ed["name"]][0], "bytes": counts[ed["name"]][1], "fallback": None} for ed in mounts]
