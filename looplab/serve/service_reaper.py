"""What a FINISHED destructive operation may leave behind in the run root, and when it may go.

Every whole-run deletion writes service files beside the runs — a receipt, an identity sidecar, a
fence, sometimes a quarantine — and `run_projections.py` filters them out of the run list so the UI
never shows them. Nothing ever removed one. Measured on this deployment, 2026-08-17: **152** service
entries under `runs/` against **18** surviving runs — 54 identity sidecars, 47 delete receipts, 50
lifecycle locks, 1 reset receipt — and the oldest predate the newest by two weeks.

They are NOT one population, and a reaper that treated them as one would be the second destructive
bug in this area rather than the fix for the first. Four rules, each stated once:

**A receipt is removable only once it is TERMINAL-SUCCEEDED and cold.** `status == "succeeded"` is
the only status whose receipt no longer answers a question: `begin_or_resume_run_deletion` replies to
a retry of a *pending* operation by RESUMING it, so a pending receipt is live state. Cold means older
than `DEFAULT_GRACE_S` — because a succeeded receipt still answers one question, idempotently, for as
long as a client might press retry: without it the same `operation_id` re-enters as a fresh deletion
of a run that is already gone and gets `404 run_not_found` instead of the `succeeded` it is entitled
to. The grace period is the whole difference between "this operation finished" and "this operation
never happened", and a retry that is entitled to read the receipt takes seconds, not a day.

**`quarantine_ambiguous` is never removable, at any age.** It is the ABSORBING state of deletion's
phase lattice (doc 25 SC-06): the operation could not establish whether the run's bytes reached the
quarantine, and the design's rule is that it must never become resumable. Its receipt is the only
durable record that a human still owes this run a look. Reaping it would not merely lose the record —
it would make the run's identity reusable, which is precisely what the absorbing state refuses.

**An identity sidecar follows its receipt, or waits out the grace alone.** When a receipt exists for
the same `(run_key, operation_id)` the two are removed together or not at all, so a retry never finds
half a pair. When NO receipt exists the sidecar is either a deletion the transaction REFUSED before
publishing anything, or a crash between the sidecar write and the receipt publish — and the second
case is a retry that is entitled to read it, so the grace period decides. That unpaired population is
not hypothetical and is not rare: 10 of the 54 sidecars here belong to `live-cards-0804` and
`live-deps4-0804`, two runs that STILL EXIST, whose deletions were refused (one holds a reset receipt
parked in `archiving`) and re-pressed six and four times. `save_deletion_identity` runs before the
transaction can refuse, so every press leaks one, unboundedly.

**A lifecycle lock is removable only when the run it fences is gone.** These are not receipts; they
are the flock targets of `engine_proc._run_lifecycle_lock`, and mutual exclusion through `flock` is
per-INODE. Unlinking one that a live process holds does not fail — it silently lets the next process
create a fresh inode at the same path and take the lock at the same time, which is a resume/reset/
delete race on one run. The name is `sha256(resolved run path)[:24]`, so a lock whose digest matches
no surviving run directory can be shown to fence nothing; that, plus age, is the only safe warrant.
A digest that DOES match a live run is left alone whatever its age.

**Fences and quarantines are never removable here.** A `.looplab-delete-fence-` file is live
ownership of a run identity, and a `.looplab-delete-quarantine-` directory holds the run's actual
bytes — the one thing in this whole area that is not reconstructible.

The survey is separate from the removal and is the default: `plan_service_file_reap` names every file
and the RULE that decided it, including the ones it refuses, so the operator reads what would go
before anything goes. `apply_service_file_reap` removes only what a plan listed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from looplab.core.run_deletion import RUN_DELETION_FENCE_PREFIX, RUN_DELETION_OPERATION_RE
from looplab.serve.appstate import (_LIFECYCLE_LOCK_PREFIX, _RESET_RECEIPT_PREFIX,
                                    _TRACE_CLEAR_RECEIPT_PREFIX)
from looplab.serve.deletion_transaction import (
    DELETE_IDENTITY_PREFIX, DELETE_QUARANTINE_PREFIX, DELETE_RECEIPT_PREFIX,
    load_deletion_receipt)
from looplab.serve.reset_transaction import load_reset_receipt

# 24 h. A retry that is entitled to read a succeeded receipt arrives while the operator is still
# looking at the page; a day is orders of magnitude past that and still bounds the population.
DEFAULT_GRACE_S = 24 * 60 * 60

# The absorbing phase, respelled nowhere: a reaper that hard-codes the string and then drifts from
# `deletion_transaction` would start removing the one receipt the design says must survive.
QUARANTINE_AMBIGUOUS = "quarantine_ambiguous"

_LIFECYCLE_RE = re.compile(r"^([0-9a-f]{24})\.lock$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _age_ok(path: Path, now: float, grace_s: float) -> tuple[bool, float]:
    """Whether `path` is older than the grace period, and its age.

    An `OSError` answers NOT cold. A file we cannot stat is a file we cannot show to be finished
    with, and the safe direction for a destructive sweep is to leave it."""
    try:
        age = now - path.stat().st_mtime
    except OSError:
        return False, -1.0
    return age >= grace_s, age


def _split_operation(name: str, prefix: str) -> Optional[tuple[str, str]]:
    """`(run_key, operation_id)` for an operation-bound service file, or None if it is not one.

    The operation half is validated with `RUN_DELETION_OPERATION_RE` — the regex the WRITERS build
    these names from — rather than a looser local pattern. A parser that accepts more shapes than the
    writer can emit is a parser that hands unrelated files to a sweep with an `unlink` in it."""
    if not name.startswith(prefix):
        return None
    rest = name[len(prefix):]
    if not rest.endswith(".json"):
        return None
    key, _, operation = rest[:-len(".json")].partition(".")
    if _SHA256_RE.fullmatch(key) is None or RUN_DELETION_OPERATION_RE.fullmatch(operation) is None:
        return None
    return key, operation


def _live_lifecycle_digests(root: Path) -> tuple[set[str], bool]:
    """The lifecycle-lock digest of every run directory that still exists, AND whether that set is
    COMPLETE.

    Recomputed from the same expression `engine_proc._run_lifecycle_lock_path` uses rather than
    imported, because importing that private helper would pull the engine-process module (and its
    thread registries) into a maintenance sweep. The expression is pinned by a test that asserts the
    two agree on a real path — a copy that silently drifts would start reaping LIVE locks, which is
    the one failure this category exists to prevent.

    THE SECOND RETURN VALUE IS THE WHOLE POINT. This is an ALLOW-LIST, so a missing entry does not
    read as "unknown", it reads as "that run is gone". A transient OSError — `iterdir` raising
    mid-iteration, or one live run's `resolve()`/`is_dir()` raising ESTALE/EIO on the geesefs mounts
    these roots live on — silently dropped exactly one live run from it, and the plan then called
    its lock "fences no surviving run directory and is cold". Cold is easy to reach: a lock's mtime
    is its CREATION time (the `a+` open on acquire never bumps it), so any day-old run qualifies.
    `--apply` would then unlink a lock a live engine HOLDS via flock, opening the per-inode
    fresh-lock race this module's header names as the one failure the category exists to prevent.

    So an incomplete walk is reported rather than absorbed, and `_age_ok` one screen up is the
    precedent: it fails CLOSED on the same error class. `blind=True` makes every lifecycle-lock
    entry a KEEP with a stated reason — the sweep does less work, which is the safe direction for a
    maintenance job whose remedy is to run again."""
    digests: set[str] = set()
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return digests, True
    blind = False
    for run_dir in entries:
        try:
            if not run_dir.is_dir() or run_dir.name.startswith("."):
                continue
            key = os.path.normcase(str(run_dir.resolve()))
        except OSError:
            # One unreadable entry is one POSSIBLY-LIVE run whose digest is now missing from an
            # allow-list, which is indistinguishable from a deleted run. Keep walking — the rest of
            # the set is still worth having for the entries it does explain — but say so.
            blind = True
            continue
        digests.add(hashlib.sha256(key.encode("utf-8")).hexdigest()[:24])
    return digests, blind


def _entry(path: Path, category: str, remove: bool, rule: str, age: float,
           **extra: Any) -> dict[str, Any]:
    return {"name": path.name, "category": category, "remove": remove, "rule": rule,
            "age_s": round(age, 1) if age >= 0 else None, **extra}


def _plan_delete_receipt(path: Path, now: float, grace_s: float) -> dict[str, Any]:
    cold, age = _age_ok(path, now, grace_s)
    try:
        receipt = load_deletion_receipt(path)
    except Exception as exc:  # noqa: BLE001 — a receipt we cannot read is one a human must look at
        return _entry(path, "delete_receipt", False,
                      f"unreadable receipt ({type(exc).__name__}); left for inspection", age)
    if receipt is None:
        return _entry(path, "delete_receipt", False, "receipt is absent or malformed", age)
    phase, status = receipt.get("phase"), receipt.get("status")
    if phase == QUARANTINE_AMBIGUOUS:
        return _entry(path, "delete_receipt", False,
                      "quarantine outcome is unknown; this receipt is the only record that a human "
                      "still owes this run a look, and the state is absorbing by design",
                      age, run_id=receipt.get("run_id"), phase=phase, status=status)
    if status != "succeeded":
        return _entry(path, "delete_receipt", False,
                      f"status is {status!r}; a retry of a non-succeeded operation resumes it",
                      age, run_id=receipt.get("run_id"), phase=phase, status=status)
    if not cold:
        return _entry(path, "delete_receipt", False,
                      f"succeeded but only {age:.0f}s old; a retry may still read it idempotently",
                      age, run_id=receipt.get("run_id"), phase=phase, status=status)
    return _entry(path, "delete_receipt", True, "deletion succeeded and the receipt is cold",
                  age, run_id=receipt.get("run_id"), phase=phase, status=status)


def _plan_reset_receipt(path: Path, now: float, grace_s: float) -> dict[str, Any]:
    cold, age = _age_ok(path, now, grace_s)
    try:
        receipt = load_reset_receipt(path)
    except Exception as exc:  # noqa: BLE001
        return _entry(path, "reset_receipt", False,
                      f"unreadable receipt ({type(exc).__name__}); left for inspection", age)
    if receipt is None:
        return _entry(path, "reset_receipt", False, "receipt is absent or malformed", age)
    status = receipt.get("status")
    if status != "succeeded":
        return _entry(path, "reset_receipt", False,
                      f"status is {status!r}; the reset is unfinished and re-enterable",
                      age, run_id=receipt.get("run_id"), phase=receipt.get("phase"), status=status)
    if not cold:
        return _entry(path, "reset_receipt", False, f"succeeded but only {age:.0f}s old",
                      age, run_id=receipt.get("run_id"), phase=receipt.get("phase"), status=status)
    return _entry(path, "reset_receipt", True, "reset succeeded and the receipt is cold",
                  age, run_id=receipt.get("run_id"), phase=receipt.get("phase"), status=status)


_TRACE_CLEAR_OPERATION_RE = re.compile(r"^tc_[0-9a-f]{32}$")


def _plan_trace_clear_receipt(path: Path, now: float, grace_s: float) -> dict[str, Any]:
    """The FIFTH root-level population, and it was classified by nothing.

    A trace clear publishes `.trace-clear.<run key>.<tc_…>.json` beside the run — in the directory
    the run list stats on every poll — and nothing ever removed one. Worse than clutter: every NEW
    clear of that run globs its siblings and strict-loads each, raising 503 on any that will not
    parse, so ONE malformed leftover refuses every future clear of that run forever.

    Same shape as the deletion and reset receipts, and the refusals are the same too. `pending` is
    live state a retry resumes from; `superseded` is the record that an operator's clear was
    overtaken and is the only place that fact survives. Only a SUCCEEDED, cold receipt goes — and a
    succeeded one still answers a retry idempotently until it does, which is what the grace buys.

    An unreadable receipt is KEPT and said so, never removed to tidy the directory: it is exactly
    the file that is breaking this run's clears, so an operator needs to find it rather than have
    the sweep quietly make the symptom go away.
    """
    cold, age = _age_ok(path, now, grace_s)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return _entry(path, "trace_clear_receipt", False,
                      f"unreadable receipt ({type(exc).__name__}); it is what breaks this run's "
                      "clears, so it is left for inspection", age)
    if not isinstance(value, dict):
        return _entry(path, "trace_clear_receipt", False, "receipt is not an object", age)
    status = value.get("status")
    node_id = value.get("node_id") if isinstance(value.get("node_id"), int) else None
    if status != "succeeded":
        return _entry(path, "trace_clear_receipt", False,
                      f"status is {status!r}, not succeeded — a retry resumes from it",
                      age, node_id=node_id, status=status)
    if not cold:
        return _entry(path, "trace_clear_receipt", False,
                      f"succeeded but only {age:.0f}s old", age, node_id=node_id, status=status)
    return _entry(path, "trace_clear_receipt", True,
                  "the trace clear succeeded and the receipt is cold",
                  age, node_id=node_id, status=status)


def plan_service_file_reap(runs_root: str | Path, *, now: Optional[float] = None,
                           grace_s: float = DEFAULT_GRACE_S) -> dict[str, Any]:
    """Name every service file under `runs_root` and the rule that decides its fate. Read-only.

    Returns `{"root", "grace_s", "entries", "remove", "keep", "counts"}` where `entries` carries one
    record per file with `remove` and a `rule` sentence. Nothing is unlinked here — this is the
    thing the operator reads BEFORE agreeing, and a number that only appears after the fact is a
    number nobody consented to.
    """
    now = time.time() if now is None else now
    root = Path(runs_root)
    entries: list[dict[str, Any]] = []
    try:
        names = sorted(p.name for p in root.iterdir())
    except OSError:
        names = []

    # Which operation ids have a receipt on disk, and which of those receipts are going. An identity
    # sidecar is decided by its receipt's fate, so the receipts are planned FIRST and the sidecars
    # read that answer back — the pair must never be split.
    receipt_fate: dict[tuple[str, str], bool] = {}
    receipt_seen: set[tuple[str, str]] = set()
    lifecycle_digests, lifecycle_blind = _live_lifecycle_digests(root)

    for name in names:
        path = root / name
        key_op = _split_operation(name, DELETE_RECEIPT_PREFIX)
        if key_op is not None:
            record = _plan_delete_receipt(path, now, grace_s)
            receipt_seen.add(key_op)
            receipt_fate[key_op] = record["remove"]
            entries.append({**record, "operation_id": key_op[1]})

    for name in names:
        path = root / name
        if name.startswith(DELETE_RECEIPT_PREFIX):
            continue

        key_op = _split_operation(name, DELETE_IDENTITY_PREFIX)
        if key_op is not None:
            cold, age = _age_ok(path, now, grace_s)
            if key_op in receipt_seen:
                paired = receipt_fate[key_op]
                entries.append(_entry(
                    path, "delete_identity", paired,
                    "follows its deletion receipt, which is being removed" if paired
                    else "follows its deletion receipt, which is being kept",
                    age, operation_id=key_op[1]))
            elif cold:
                entries.append(_entry(
                    path, "delete_identity", True,
                    "no receipt was ever published for this operation (a refused or crashed "
                    "attempt) and the sidecar is cold", age, operation_id=key_op[1]))
            else:
                entries.append(_entry(
                    path, "delete_identity", False,
                    f"no receipt yet and only {age:.0f}s old; a retry of this operation is "
                    "entitled to read it", age, operation_id=key_op[1]))
            continue

        if name.startswith(_RESET_RECEIPT_PREFIX):
            entries.append(_plan_reset_receipt(path, now, grace_s))
            continue

        if name.startswith(_TRACE_CLEAR_RECEIPT_PREFIX) and name.endswith(".json"):
            # The operation half is validated with the WRITER's own regex, for `_split_operation`'s
            # reason: a parser that accepts more shapes than the writer can emit hands unrelated
            # files to a sweep with an `unlink` in it.
            operation = name[:-len(".json")].rsplit(".", 1)[-1]
            if _TRACE_CLEAR_OPERATION_RE.fullmatch(operation) is None:
                entries.append(_entry(path, "trace_clear_receipt", False,
                                      "unrecognised trace-clear receipt name",
                                      _age_ok(path, now, grace_s)[1]))
            else:
                entries.append({**_plan_trace_clear_receipt(path, now, grace_s),
                                "operation_id": operation})
            continue

        if name.startswith(_LIFECYCLE_LOCK_PREFIX):
            cold, age = _age_ok(path, now, grace_s)
            match = _LIFECYCLE_RE.match(name[len(_LIFECYCLE_LOCK_PREFIX):])
            if match is None:
                entries.append(_entry(path, "lifecycle_lock", False,
                                      "unrecognised lifecycle lock name", age))
            elif match.group(1) in lifecycle_digests:
                entries.append(_entry(path, "lifecycle_lock", False,
                                      "fences a run directory that still exists", age))
            elif lifecycle_blind:
                # The allow-list is incomplete, so "not in it" no longer means "gone". Stated as
                # its own rule rather than folded into the cold one: an operator reading this needs
                # to know the sweep was blind, not that the lock looked young.
                entries.append(_entry(path, "lifecycle_lock", False,
                                      "the run directory listing was incomplete (an entry could "
                                      "not be read), so a missing digest does not prove this "
                                      "lock's run is gone", age))
            elif not cold:
                entries.append(_entry(path, "lifecycle_lock", False,
                                      f"fences no surviving run but is only {age:.0f}s old; a "
                                      "lifecycle operation may hold this flock", age))
            else:
                entries.append(_entry(path, "lifecycle_lock", True,
                                      "fences no surviving run directory and is cold", age))
            continue

        if name.startswith(RUN_DELETION_FENCE_PREFIX):
            _, age = _age_ok(path, now, grace_s)
            entries.append(_entry(path, "delete_fence", False,
                                  "a fence is live ownership of a run identity", age))
            continue

        if name.startswith(DELETE_QUARANTINE_PREFIX):
            _, age = _age_ok(path, now, grace_s)
            entries.append(_entry(path, "delete_quarantine", False,
                                  "holds the run's own bytes; never reaped", age))
            continue

    entries.sort(key=lambda e: (e["category"], e["name"]))
    remove = [e for e in entries if e["remove"]]
    keep = [e for e in entries if not e["remove"]]
    counts: dict[str, dict[str, int]] = {}
    for record in entries:
        bucket = counts.setdefault(record["category"], {"remove": 0, "keep": 0})
        bucket["remove" if record["remove"] else "keep"] += 1
    return {"root": str(root), "grace_s": grace_s, "entries": entries,
            "remove": remove, "keep": keep, "counts": counts,
            "total": len(entries), "removable": len(remove), "kept": len(keep)}


def apply_service_file_reap(plan: dict[str, Any]) -> dict[str, Any]:
    """Unlink exactly what `plan` marked removable. Returns what actually went.

    Re-derives nothing: the plan the operator read is the plan that executes, so a store changing
    under the sweep cannot widen it. A vanished file is a SUCCESS (another sweep, or the same one
    twice — this is idempotent by construction); any other `OSError` is reported and the sweep
    continues, because one undeletable file must not hide the rest of the work.
    """
    root = Path(plan["root"])
    removed, failures = [], []
    for record in plan.get("remove", []):
        path = root / record["name"]
        try:
            path.unlink()
            removed.append(record["name"])
        except FileNotFoundError:
            removed.append(record["name"])
        except OSError as exc:
            failures.append({"name": record["name"], "error": f"{type(exc).__name__}: {exc}"[:200]})
    return {"root": str(root), "removed": removed, "failures": failures,
            "removed_count": len(removed), "failed_count": len(failures)}


__all__ = ["DEFAULT_GRACE_S", "QUARANTINE_AMBIGUOUS", "plan_service_file_reap",
           "apply_service_file_reap"]
