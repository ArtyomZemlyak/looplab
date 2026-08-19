"""Tier-1 guards for the run-root service-file reaper and the cascade's blind-spot disclosure.

Every test here drives the REAL artifacts — receipts written to disk and read back through the
strict loader, lock files whose names come from `engine_proc`'s own path builder, event logs read by
`run_memory_identity` — because the properties at stake are all of the form "the effect landed", and
a mock would have let every one of the defects these guard against ship.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from looplab.core.atomicio import atomic_write_text
from looplab.core.run_deletion import RUN_DELETION_FENCE_PREFIX
from looplab.serve.deletion_transaction import (
    DELETE_IDENTITY_PREFIX, DELETE_QUARANTINE_PREFIX, DELETE_RECEIPT_PREFIX,
    load_deletion_identity, save_deletion_identity)
from looplab.serve.memory_cascade import (
    RunIdentity, attributable_memory, orphan_survey, purge_attributable_memory,
    run_memory_identity)
from looplab.serve.service_reaper import (
    DEFAULT_GRACE_S, apply_service_file_reap, plan_service_file_reap)

KEY_A = "a" * 64
KEY_B = "b" * 64
OP_1 = "11111111-1111-1111-1111-111111111111"
OP_2 = "22222222-2222-2222-2222-222222222222"
OP_3 = "33333333-3333-3333-3333-333333333333"


def _receipt(run_id: str, run_key: str, operation_id: str, *, phase: str, status: str) -> dict:
    now = time.time()
    return {"version": 1, "id": operation_id, "run_id": run_id, "run_key": run_key,
            "expected_generation": "c" * 64, "expected_seq": 7,
            "status": status, "phase": phase, "created_at": now, "updated_at": now}


def _write_receipt(root: Path, run_id: str, run_key: str, operation_id: str, *,
                   phase: str, status: str, age_s: float = 0.0) -> Path:
    path = root / f"{DELETE_RECEIPT_PREFIX}{run_key}.{operation_id}.json"
    atomic_write_text(path, json.dumps(_receipt(run_id, run_key, operation_id,
                                                phase=phase, status=status)))
    _age(path, age_s)
    return path


def _write_identity(root: Path, run_key: str, operation_id: str, *, age_s: float = 0.0) -> Path:
    path = root / f"{DELETE_IDENTITY_PREFIX}{run_key}.{operation_id}.json"
    atomic_write_text(path, json.dumps({"version": 1, "run_id": "r", "run_uid": "",
                                        "memory_dir": "/m", "run_uid_source": "pre_uid_run"}))
    _age(path, age_s)
    return path


def _age(path: Path, age_s: float) -> None:
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))


def _lifecycle_lock_path(run_dir: Path) -> Path:
    digest = hashlib.sha256(
        os.path.normcase(str(run_dir.resolve())).encode("utf-8")).hexdigest()[:24]
    return run_dir.resolve().parent / f".looplab-lifecycle-{digest}.lock"


def _named(plan: dict, name: str) -> dict:
    for record in plan["entries"]:
        if record["name"] == name:
            return record
    raise AssertionError(f"{name} not in plan")


# ---------------------------------------------------------------------------------------------
# The reaper's four rules
# ---------------------------------------------------------------------------------------------

def test_succeeded_cold_receipt_and_its_sidecar_go_together(tmp_path: Path):
    """The whole point: a finished deletion stops costing three files forever."""
    receipt = _write_receipt(tmp_path, "gone", KEY_A, OP_1,
                             phase="succeeded", status="succeeded", age_s=48 * 3600)
    identity = _write_identity(tmp_path, KEY_A, OP_1, age_s=48 * 3600)
    plan = plan_service_file_reap(tmp_path)
    assert _named(plan, receipt.name)["remove"] is True
    assert _named(plan, identity.name)["remove"] is True
    outcome = apply_service_file_reap(plan)
    assert sorted(outcome["removed"]) == sorted([receipt.name, identity.name])
    assert not receipt.exists() and not identity.exists()


def test_a_fresh_succeeded_receipt_survives_for_the_idempotent_retry(tmp_path: Path):
    """A succeeded receipt is what makes a retry answer `succeeded` instead of `404 run_not_found`.

    Reaping it the instant the deletion lands would turn the operator's second click from an
    idempotent no-op into an error about a run they just deleted on purpose."""
    receipt = _write_receipt(tmp_path, "gone", KEY_A, OP_1,
                             phase="succeeded", status="succeeded", age_s=30.0)
    identity = _write_identity(tmp_path, KEY_A, OP_1, age_s=30.0)
    plan = plan_service_file_reap(tmp_path)
    assert _named(plan, receipt.name)["remove"] is False
    # And the sidecar follows the receipt rather than being decided on its own age.
    assert _named(plan, identity.name)["remove"] is False
    assert apply_service_file_reap(plan)["removed"] == []
    assert receipt.exists() and identity.exists()


@pytest.mark.parametrize("phase,status", [
    ("quarantining", "pending"), ("quarantined", "pending"),
    ("metadata_committed", "pending"), ("purging", "pending"), ("retiring_fence", "pending"),
    ("prepared", "pending"), ("fenced", "pending"),
])
def test_an_unfinished_deletion_is_never_reaped_however_old(tmp_path: Path, phase, status):
    """A pending receipt is live state: `begin_or_resume_run_deletion` RESUMES from it."""
    receipt = _write_receipt(tmp_path, "half", KEY_A, OP_1,
                             phase=phase, status=status, age_s=365 * 24 * 3600)
    plan = plan_service_file_reap(tmp_path)
    assert _named(plan, receipt.name)["remove"] is False
    assert "resumes" in _named(plan, receipt.name)["rule"]
    apply_service_file_reap(plan)
    assert receipt.exists()


def test_quarantine_ambiguous_is_absorbing_and_never_reaped(tmp_path: Path):
    """The state the design says must never become resumable.

    Its receipt is the only durable record that a human still owes this run a look, and removing it
    would also free the run identity for reuse — exactly what the absorbing state refuses. Age must
    not be able to launder it away, so this is pinned at a year old."""
    receipt = _write_receipt(tmp_path, "ambiguous", KEY_A, OP_1,
                             phase="quarantine_ambiguous", status="pending",
                             age_s=365 * 24 * 3600)
    plan = plan_service_file_reap(tmp_path)
    record = _named(plan, receipt.name)
    assert record["remove"] is False
    assert "absorbing" in record["rule"]
    apply_service_file_reap(plan)
    assert receipt.exists()


def test_an_unpaired_sidecar_waits_out_the_grace_then_goes(tmp_path: Path):
    """The unbounded leak: `save_deletion_identity` runs BEFORE the transaction can refuse.

    Measured on the real deployment — 10 sidecars for two runs that still exist, whose deletions
    were refused and re-pressed. Each press leaks one and nothing ever collected them. But a sidecar
    with no receipt is ALSO the crash window between the sidecar write and the receipt publish, and
    a retry of that operation is entitled to read it, so only a cold one may go."""
    warm = _write_identity(tmp_path, KEY_A, OP_1, age_s=60.0)
    cold = _write_identity(tmp_path, KEY_B, OP_2, age_s=48 * 3600)
    plan = plan_service_file_reap(tmp_path)
    assert _named(plan, warm.name)["remove"] is False
    assert _named(plan, cold.name)["remove"] is True
    apply_service_file_reap(plan)
    assert warm.exists() and not cold.exists()


def test_a_lifecycle_lock_fencing_a_live_run_is_never_reaped(tmp_path: Path):
    """`flock` is per-INODE. Unlinking a held lock does not fail — it lets the next process create a
    fresh inode at the same path and hold the "same" lock simultaneously, which is a resume/reset/
    delete race on one run. So the only safe warrant is that the run it fences is gone."""
    live = tmp_path / "still-here"
    live.mkdir()
    live_lock = _lifecycle_lock_path(live)
    live_lock.write_bytes(b"")
    _age(live_lock, 365 * 24 * 3600)                    # old, and still must not go

    dead_lock = tmp_path / (".looplab-lifecycle-" + "0" * 24 + ".lock")
    dead_lock.write_bytes(b"")
    _age(dead_lock, 48 * 3600)

    plan = plan_service_file_reap(tmp_path)
    assert _named(plan, live_lock.name)["remove"] is False
    assert "still exists" in _named(plan, live_lock.name)["rule"]
    assert _named(plan, dead_lock.name)["remove"] is True
    apply_service_file_reap(plan)
    assert live_lock.exists() and not dead_lock.exists()


def test_the_lifecycle_digest_matches_the_real_writer(tmp_path: Path):
    """The reaper re-derives the lock name instead of importing `engine_proc`'s builder, so the two
    are pinned to agree on a real path. A silent drift here would start reaping LIVE locks."""
    from looplab.serve.engine_proc import _run_lifecycle_lock_path

    run_dir = tmp_path / "some-run"
    run_dir.mkdir()
    assert _run_lifecycle_lock_path(run_dir).name == _lifecycle_lock_path(run_dir).name


def test_fences_and_quarantines_are_never_reaped(tmp_path: Path):
    """A fence is live ownership of a run identity; a quarantine holds the run's own bytes — the one
    thing in this area that is not reconstructible."""
    fence = tmp_path / f"{RUN_DELETION_FENCE_PREFIX}{KEY_A}.json"
    fence.write_text("{}")
    _age(fence, 365 * 24 * 3600)
    quarantine = tmp_path / f"{DELETE_QUARANTINE_PREFIX}{KEY_A}.{OP_1}"
    quarantine.mkdir()
    _age(quarantine, 365 * 24 * 3600)
    plan = plan_service_file_reap(tmp_path)
    assert _named(plan, fence.name)["remove"] is False
    assert _named(plan, quarantine.name)["remove"] is False
    apply_service_file_reap(plan)
    assert fence.exists() and quarantine.exists()


def test_an_unreadable_receipt_is_left_for_a_human(tmp_path: Path):
    """A receipt we cannot parse is not a receipt we can show is finished with."""
    broken = tmp_path / f"{DELETE_RECEIPT_PREFIX}{KEY_A}.{OP_1}.json"
    broken.write_text("{not json")
    _age(broken, 365 * 24 * 3600)
    plan = plan_service_file_reap(tmp_path)
    assert _named(plan, broken.name)["remove"] is False
    apply_service_file_reap(plan)
    assert broken.exists()


def test_apply_removes_exactly_the_plan_and_nothing_else(tmp_path: Path):
    """The plan the operator READ is the plan that executes — nothing is re-derived at apply time,
    so a store changing under the sweep cannot widen it."""
    (tmp_path / "a-real-run").mkdir()
    (tmp_path / "a-real-run" / "events.jsonl").write_text("")
    doomed = _write_receipt(tmp_path, "gone", KEY_A, OP_1,
                            phase="succeeded", status="succeeded", age_s=48 * 3600)
    kept = _write_receipt(tmp_path, "half", KEY_B, OP_2, phase="purging", status="pending",
                          age_s=48 * 3600)
    plan = plan_service_file_reap(tmp_path)
    before = {p.name for p in tmp_path.iterdir()}
    outcome = apply_service_file_reap(plan)
    after = {p.name for p in tmp_path.iterdir()}
    assert before - after == {doomed.name}
    assert outcome["failures"] == []
    assert kept.exists() and (tmp_path / "a-real-run").is_dir()


def test_a_plan_is_idempotent_and_a_vanished_file_is_a_success(tmp_path: Path):
    receipt = _write_receipt(tmp_path, "gone", KEY_A, OP_1,
                             phase="succeeded", status="succeeded", age_s=48 * 3600)
    plan = plan_service_file_reap(tmp_path)
    assert apply_service_file_reap(plan)["failures"] == []
    again = apply_service_file_reap(plan)                # the file is already gone
    assert again["failures"] == [] and again["removed"] == [receipt.name]


def test_grace_default_matches_the_cli_literal():
    """`run_cmds` spells 24.0 rather than importing this constant (the import would drag fastapi into
    the CLI), so the duplication is pinned here."""
    assert DEFAULT_GRACE_S == 24 * 60 * 60


def test_the_reaper_never_proposes_a_run_directory(tmp_path: Path):
    """The whole sweep runs in the run ROOT with an `unlink` in it. A run is not a service file."""
    (tmp_path / "keep-me").mkdir()
    (tmp_path / "keep-me" / "events.jsonl").write_text('{"type":"x"}\n')
    _write_receipt(tmp_path, "gone", KEY_A, OP_1, phase="succeeded", status="succeeded",
                   age_s=48 * 3600)
    plan = plan_service_file_reap(tmp_path)
    assert all(not r["name"].startswith("keep-me") for r in plan["entries"])
    apply_service_file_reap(plan)
    assert (tmp_path / "keep-me" / "events.jsonl").exists()


# ---------------------------------------------------------------------------------------------
# (B) the identity, and the receipt that must not claim a keying it never did
# ---------------------------------------------------------------------------------------------

def _run_with_started(root: Path, name: str, data: dict | None) -> Path:
    run_dir = root / name
    run_dir.mkdir(parents=True)
    # `seq` starts at 0: `iter_event_jsonl` enforces a DENSE logical sequence from zero and stops at
    # the first gap, so a log that starts at 1 yields nothing at all — which would make every case
    # below pass as `no_run_started` for the wrong reason.
    event = ({"type": "run_started", "data": data} if data is not None
             else {"type": "node_started", "data": {}})
    (run_dir / "events.jsonl").write_text(
        json.dumps({"seq": 0, "ts": 0, "v": 1, **event}) + "\n")
    return run_dir


def test_run_uid_source_distinguishes_the_four_meanings_of_an_empty_uid(tmp_path: Path):
    """An empty `run_uid` has four meanings and the cascade behaves differently under each.

    This is the fact the parked sidecar lost: 54 of them on the real deployment carry `run_uid: ""`
    and nothing on disk can now say whether those runs predated uid stamping or whether their logs
    simply could not be read."""
    stamped = _run_with_started(tmp_path, "stamped", {"run_id": "stamped", "run_uid": "deadbeef"})
    assert run_memory_identity(stamped)["run_uid_source"] == "run_started"
    assert run_memory_identity(stamped)["run_uid"] == "deadbeef"

    legacy = _run_with_started(tmp_path, "legacy", {"run_id": "legacy"})
    assert run_memory_identity(legacy)["run_uid_source"] == "pre_uid_run"
    assert run_memory_identity(legacy)["run_uid"] == ""

    never = _run_with_started(tmp_path, "never", None)
    assert run_memory_identity(never)["run_uid_source"] == "no_run_started"

    missing = tmp_path / "absent"
    assert run_memory_identity(missing)["run_uid_source"] in {"no_run_started", "unreadable"}


def test_a_uid_less_purge_against_uid_carrying_rows_says_it_matched_nothing(tmp_path: Path):
    """THE DEFECT, driven end to end.

    A run whose uid could not be read meets a store whose every row carries one. `owns` takes the
    `if row_uid:` branch, which requires the caller's uid, and returns False WITHOUT ever comparing a
    name — so the purge deletes nothing, keeps nothing, and used to answer
    `{"ok": true, "deleted": 0, "kept": 0, "identity": "run_id"}`: a clean success claiming a name
    keying that never happened. Measured on the real deployment: all 216 cascadable rows in
    `~/.looplab/memory` carry a uid and all 54 parked identities carry none."""
    store = tmp_path / "memory"
    store.mkdir()
    (store / "meta_notes.jsonl").write_text("".join(
        json.dumps({"run_id": "run", "run_uid": uid, "task_id": "toy"}) + "\n"
        for uid in ("uid-a", "uid-b", "uid-c")))

    survey = attributable_memory(store, "run", "")
    assert survey["deletable"] == 0
    assert survey["unmatchable"] == 3, "the rows that could not be matched must be counted"
    assert "could not be matched" in survey["advisory"]

    receipt = purge_attributable_memory(store, "run", "")
    assert receipt["ok"] is True and receipt["deleted"] == 0
    assert receipt["unmatchable"] == 3
    assert receipt["advisory"], "a purge that could not have matched must SAY so"
    # And the rows are all still there — the disclosure is not a licence to widen anything.
    assert len((store / "meta_notes.jsonl").read_text().strip().splitlines()) == 3


def test_a_genuinely_pre_uid_run_still_purges_by_name(tmp_path: Path):
    """The legacy path must stay working — this is the case the empty uid is CORRECT for.

    A run that predates uid stamping wrote rows that predate it too, so name matching is the only
    identity either side has, and there is nothing to disclose."""
    store = tmp_path / "memory"
    store.mkdir()
    (store / "meta_notes.jsonl").write_text(
        json.dumps({"run_id": "legacy-run", "task_id": "toy"}) + "\n"
        + json.dumps({"run_id": "other-run", "task_id": "toy"}) + "\n")
    receipt = purge_attributable_memory(store, "legacy-run", "")
    assert receipt["deleted"] == 1
    assert receipt["unmatchable"] == 0
    assert receipt["advisory"] == ""
    assert receipt["identity"] == "run_id"
    survivors = [json.loads(line) for line in
                 (store / "meta_notes.jsonl").read_text().strip().splitlines()]
    assert [row["run_id"] for row in survivors] == ["other-run"]


def test_unmatchable_is_not_folded_into_kept(tmp_path: Path):
    """`kept` means "a tier rule decided to keep this". A row nobody could form an opinion about is
    not a judgement, and reporting it as one would tell the operator a rule fired that never ran."""
    store = tmp_path / "memory"
    store.mkdir()
    (store / "meta_notes.jsonl").write_text(
        json.dumps({"run_id": "run", "run_uid": "uid-a", "task_id": "toy"}) + "\n")
    survey = attributable_memory(store, "run", "")
    assert survey["kept"] == 0 and survey["unmatchable"] == 1


def test_a_uid_keyed_caller_has_no_blind_spot(tmp_path: Path):
    store = tmp_path / "memory"
    store.mkdir()
    (store / "meta_notes.jsonl").write_text(
        json.dumps({"run_id": "run", "run_uid": "mine", "task_id": "toy"}) + "\n"
        + json.dumps({"run_id": "run", "run_uid": "theirs", "task_id": "toy"}) + "\n")
    receipt = purge_attributable_memory(store, "run", "mine")
    assert receipt["deleted"] == 1 and receipt["unmatchable"] == 0 and receipt["advisory"] == ""
    assert receipt["identity"] == "run_uid"


def test_the_sidecar_carries_the_uid_source_and_old_sidecars_still_load(tmp_path: Path):
    """The 54 sidecars already on disk must keep loading — bumping the version for a new field would
    have discarded every one of them and taken the `memory_dir` they carry with it."""
    class _Srv:
        root = tmp_path
        class commands:
            @staticmethod
            def _sequence_path(rd):
                from looplab.core.run_deletion import run_deletion_key
                return tmp_path / f"{run_deletion_key(rd)}.json"

    run_dir = tmp_path / "a-run"
    run_dir.mkdir()
    save_deletion_identity(_Srv, run_dir, OP_1,
                           {"run_id": "a-run", "run_uid": "", "memory_dir": "/m",
                            "run_uid_source": "pre_uid_run"})
    assert load_deletion_identity(_Srv, run_dir, OP_1)["run_uid_source"] == "pre_uid_run"

    from looplab.serve.deletion_transaction import deletion_identity_path
    path = deletion_identity_path(_Srv, run_dir, OP_1)
    atomic_write_text(path, json.dumps({"version": 1, "run_id": "a-run", "run_uid": "",
                                        "memory_dir": "/m"}))          # an OLD sidecar
    old = load_deletion_identity(_Srv, run_dir, OP_1)
    assert old["memory_dir"] == "/m", "an old sidecar must not be discarded"
    assert old["run_uid_source"] == "unknown"


# ---------------------------------------------------------------------------------------------
# The operator-driven orphan sweep
# ---------------------------------------------------------------------------------------------

def test_orphan_survey_never_proposes_a_surviving_runs_rows(tmp_path: Path):
    """THE SAFETY MARGIN. Measured: 172 rows in `/home/jovyan/data/looplab-memory` belong to runs
    that still exist, and no sweep may touch one."""
    runs = tmp_path / "runs"
    _run_with_started(runs, "alive", {"run_id": "alive", "run_uid": "uid-alive"})
    _run_with_started(runs, "legacy-alive", {"run_id": "legacy-alive"})
    store = tmp_path / "memory"
    store.mkdir()
    (store / "meta_notes.jsonl").write_text(
        json.dumps({"run_id": "alive", "run_uid": "uid-alive"}) + "\n"
        + json.dumps({"run_id": "legacy-alive"}) + "\n"          # uid-less, name is live
        + json.dumps({"run_id": "alive", "run_uid": "uid-dead"}) + "\n"   # name reused!
        + json.dumps({"run_id": "vanished"}) + "\n")
    survey = orphan_survey(store, runs)
    assert survey["live_rows"] == 2
    assert survey["orphan_rows"] == 2
    proposed = {(i["run_id"], i["run_uid"]) for i in survey["identities"]}
    assert proposed == {("alive", "uid-dead"), ("vanished", "")}
    assert survey["blind"] is False


def test_orphan_survey_fails_closed_when_a_surviving_runs_identity_is_unreadable(tmp_path: Path):
    """An unknown uid must never read as an absent one — that is how a sweep deletes a live run's
    evidence."""
    runs = tmp_path / "runs"
    runs.mkdir()
    broken = runs / "unreadable"
    broken.mkdir()
    (broken / "events.jsonl").mkdir()          # a DIRECTORY where the log should be
    store = tmp_path / "memory"
    store.mkdir()
    (store / "meta_notes.jsonl").write_text(
        json.dumps({"run_id": "whoever", "run_uid": "uid-unknown"}) + "\n")
    survey = orphan_survey(store, runs)
    assert survey["blind"] is True
    assert survey["unreadable_runs"] == ["unreadable"]
    assert survey["orphan_rows"] == 0, "a uid-carrying row must not be called orphaned while blind"
    assert survey["identities"] == []


def test_orphan_removal_goes_through_the_tier_predicates(tmp_path: Path):
    """A blunt "delete every orphaned row" would destroy the shared corroboration the cascade exists
    to defend. The writing run being gone does not make its contribution to a surviving row somebody
    else's to discard."""
    store = tmp_path / "memory"
    store.mkdir()
    (store / "lessons.jsonl").write_text(
        json.dumps({"run_id": "gone", "evidence_count": 3, "text": "shared"}) + "\n"
        + json.dumps({"run_id": "gone", "evidence_count": 1, "text": "mine alone"}) + "\n")
    receipt = purge_attributable_memory(store, "gone", "")
    assert receipt["deleted"] == 1 and receipt["kept"] == 1
    survivors = [json.loads(line) for line in
                 (store / "lessons.jsonl").read_text().strip().splitlines()]
    assert [row["text"] for row in survivors] == ["shared"]


def test_run_identity_unmatchable_is_exactly_the_legacy_blind_spot():
    """The rule, stated (ladder tier 2) — a truth table nobody has to infer from two call sites."""
    legacy, keyed = RunIdentity("run", ""), RunIdentity("run", "mine")
    assert legacy.unmatchable({"run_id": "run", "run_uid": "theirs"}) is True
    assert legacy.unmatchable({"run_id": "run"}) is False        # name matching still works
    assert keyed.unmatchable({"run_id": "run", "run_uid": "theirs"}) is False
    assert keyed.unmatchable({"run_id": "run"}) is False


def test_memory_dir_source_says_whether_the_store_was_the_runs_own(tmp_path: Path):
    """The question a `deleted: 0` receipt could not answer: was this even the right directory?

    `memory_dir` is a per-RUN setting, so a purge can legitimately run against a store that is NOT
    the server's current global — and after the run is gone nothing can say which happened. Measured:
    `live-cards-0804`'s own `config.snapshot.json` names `~/.looplab/memory` while this box's global
    default is `/home/jovyan/data/looplab-memory`, so its cascade was correctly aimed at the run's
    OWN store; without this field that is indistinguishable from having defaulted."""
    run_dir = _run_with_started(tmp_path, "cfg-run", {"run_id": "cfg-run"})
    (run_dir / "config.snapshot.json").write_text(json.dumps({"memory_dir": "/run/own/store"}))
    identity = run_memory_identity(run_dir, fallback_memory_dir="/server/global")
    assert identity["memory_dir"] == "/run/own/store"
    assert identity["memory_dir_source"] == "run_config"

    bare = _run_with_started(tmp_path, "no-cfg", {"run_id": "no-cfg"})
    defaulted = run_memory_identity(bare, fallback_memory_dir="/server/global")
    assert defaulted["memory_dir"] == "/server/global"
    assert defaulted["memory_dir_source"] == "server_default"

    assert run_memory_identity(bare)["memory_dir_source"] == "none"


# ============ the 2026-08-18 review finding: an ALLOW-LIST that failed OPEN on a transient error

def test_an_unreadable_run_directory_never_licenses_reaping_a_live_lock(tmp_path, monkeypatch):
    """The digest set is an ALLOW-LIST, so a missing entry does not read as "unknown" — it reads as
    "that run is gone". One live run's `resolve()` raising ESTALE on a geesefs mount therefore
    dropped its digest, the plan called its lock "fences no surviving run directory and is cold"
    (a lock's mtime is its CREATION time, so any day-old run is cold), and `--apply` would unlink a
    lock a live engine holds via flock — the per-inode fresh-lock race this module exists to stop.

    `_age_ok` one screen up fails CLOSED on the same error class; now so does this.
    """
    import looplab.serve.service_reaper as sr

    root = tmp_path / "runs"
    (root / "live-run").mkdir(parents=True)
    digest = sr.hashlib.sha256(
        sr.os.path.normcase(str((root / "live-run").resolve())).encode("utf-8")).hexdigest()[:24]
    lock = root / f"{sr._LIFECYCLE_LOCK_PREFIX}{digest}.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(lock, (old, old))

    plan = sr.plan_service_file_reap(root, grace_s=60.0)
    entry = next(e for e in plan["entries"] if e["category"] == "lifecycle_lock")
    assert entry["remove"] is False and "still exists" in entry["rule"]

    # ...and now the walk goes blind on exactly that directory.
    real_resolve = Path.resolve

    def _blind(self, *a, **kw):
        if self.name == "live-run":
            raise OSError(116, "Stale file handle")
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", _blind)
    blinded = sr.plan_service_file_reap(root, grace_s=60.0)
    entry = next(e for e in blinded["entries"] if e["category"] == "lifecycle_lock")
    assert entry["remove"] is False, entry
    assert "incomplete" in entry["rule"], entry


def test_a_lock_whose_run_is_genuinely_gone_is_still_reaped(tmp_path):
    """The fix must not turn the category off: a complete walk that simply does not contain the
    digest still reaps a cold lock."""
    import looplab.serve.service_reaper as sr

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    lock = root / f"{sr._LIFECYCLE_LOCK_PREFIX}{'0' * 24}.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(lock, (old, old))

    plan = sr.plan_service_file_reap(root, grace_s=60.0)
    entry = next(e for e in plan["entries"] if e["category"] == "lifecycle_lock")
    assert entry["remove"] is True and "cold" in entry["rule"], entry
