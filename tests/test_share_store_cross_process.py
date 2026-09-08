"""The share store is the SAME bearer-capability store as the review store (doc 25 SC-10).

`ShareStore` and `ReviewStore` are one concept — one file per capability, sha256 `token_hash` (never
the token), TTL bounds, `revoked_at` tombstones, `public()` views that strip the digest, constant-time
`resolve` — and they had materially different guarantees. `ReviewStore` paired a per-path process lock
with a REQUIRED OS lock, reserved ids with `O_EXCL` and healed abandoned reservations; `ShareStore` had
`threading.Lock()` and a check-then-write.

That gap is not theoretical for a bearer capability: `revoke_session` is a read-modify-write over a
directory of records, so with two uvicorn workers one worker's revocation could be lost behind the
other's write — leaving live a link the owner was told was dead. And a check-then-write id could be
lost between the look and the write, replacing an existing token digest with a new one.

Since 2026-09-08 the shared half is ONE implementation (`serve/capability_store.py`) that both stores
parameterize, so a fix to the protocol reaches both. This file drives the properties rather than
reading source wherever it can: a store whose OS lock cannot be had, an id that collides, a publish
that fails, and a reservation left behind by a worker that died mid-create.

The mutation/read split is deliberate and matches the sibling: MUTATIONS take the store lock, reads do
not. Locking reads would serialize the HTTP read path across workers and turn contention into 503s on
a path that cannot corrupt anything.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import secrets
import textwrap
import threading
import time

import pytest

from looplab.serve import assistant, capability_store, reviews
from looplab.serve.assistant import ShareError, ShareStore

MUTATORS = ("create", "revoke_token", "revoke_session")
READERS = ("resolve", "active_for_session", "active_summary_by_session")

_SECRET = "x" * 43                                   # the shape `resolve` requires of a token secret


def _parse(obj) -> tuple[str, ast.Module]:
    """Source + AST of a method. `dedent`, never `lstrip`: a DECORATED method's source starts with
    the decorator line, so stripping only the leading whitespace leaves `def` indented past it."""
    source = inspect.getsource(obj)
    return source, ast.parse(textwrap.dedent(source))


def _guarded_by(method: str) -> set[str]:
    """The lock context managers a ShareStore method enters."""
    _, tree = _parse(getattr(ShareStore, method))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                found.add(ast.unparse(item.context_expr))
    return found


@pytest.mark.parametrize("method", MUTATORS)
def test_every_mutating_path_takes_the_cross_process_lock(method):
    assert "self._store_lock()" in _guarded_by(method), (
        f"{method} mutates share records under a thread-only lock; two workers can interleave it")


@pytest.mark.parametrize("method", READERS)
def test_reads_deliberately_stay_on_the_thread_lock(method):
    """Matching `ReviewStore`, whose `status`/`list_for_run`/`resolve` also skip the store lock.
    A read cannot corrupt the store, and locking it would convert contention into 503s."""
    guards = _guarded_by(method)
    assert "self._store_lock()" not in guards, f"{method} does not need cross-process exclusion"
    assert "self._lock" in guards, f"{method} lost its in-process lock entirely"


def test_the_split_matches_the_sibling_store():
    """Not a coincidence — the same rule, so a change to one is visibly a change to both."""
    review_source = inspect.getsource(reviews.ReviewStore)
    for mutator in ("def create", "def revoke"):
        body = review_source.split(mutator, 1)[1].split("\n    def ", 1)[0]
        assert "_store_lock()" in body, f"ReviewStore{mutator} no longer takes its store lock"
    for reader in ("def list_for_run", "def resolve"):
        body = review_source.split(reader, 1)[1].split("\n    def ", 1)[0]
        assert "_store_lock()" not in body, f"ReviewStore{reader} started locking a read"


def test_the_process_lock_is_shared_per_store_path(tmp_path):
    """Two ShareStore objects over the same directory must contend on ONE lock. Keyed per instance,
    each server object would hold its own and the process half of the guarantee would be vacuous."""
    first, second = ShareStore(tmp_path), ShareStore(tmp_path)
    assert first._lock is second._lock

    elsewhere = ShareStore(tmp_path / "other")
    assert elsewhere._lock is not first._lock


def test_one_lock_table_and_one_digest_serve_both_capability_stores(tmp_path):
    """The extraction, driven: the two stores are not two implementations that happen to agree.

    A `ReviewStore` over the very directory a `ShareStore` uses gets the SAME lock object, which is
    only true if there is one table; and both spell the stored form of a bearer value with the one
    `token_digest`. Before this, each store owned a private table and a private `_digest`, so the
    "keyed on the store PATH, not the instance" rule had two places to regress from."""
    share = ShareStore(tmp_path)
    sibling = reviews.ReviewStore(share.dir)
    assert sibling._lock is share._lock

    assert ShareStore._digest is capability_store.token_digest
    assert reviews._digest is capability_store.token_digest
    assert not hasattr(assistant, "_SHARE_STORE_LOCKS"), (
        "the share store owns a private lock table again — that is the duplication SC-10 named")


def test_a_contended_store_fails_closed_with_a_retryable_error(tmp_path):
    """No thread-only fallback: when the OS lock cannot be had, the caller gets the store's own
    bounded 503 rather than a silently unsynchronized write."""
    store = ShareStore(tmp_path)
    store.dir.mkdir(parents=True, exist_ok=True)

    held = threading.Event()
    release = threading.Event()

    def _hold():
        with store._store_lock():
            held.set()
            release.wait(timeout=10)

    worker = threading.Thread(target=_hold, daemon=True)
    worker.start()
    assert held.wait(timeout=10), "the holder never acquired the store lock"
    try:
        # Same interpreter: the per-path process lock is what refuses here, with the same error the
        # interprocess contention path raises, so callers see one failure mode either way.
        with pytest.raises(ShareError) as excinfo:
            with store._store_lock():
                pass
    finally:
        release.set()
        worker.join(timeout=10)
    assert getattr(excinfo.value, "status_code", None) == 503


def test_an_unusable_os_lock_refuses_the_mutation_instead_of_running_it_unlocked(tmp_path):
    """The OTHER half of "fails closed", and it is DRIVEN rather than pinned.

    The contention test above races on the per-path PROCESS lock — same interpreter — so it never
    reaches the interprocess block at all. Here the store's `.lock` path is a DIRECTORY, so opening
    it raises and `required=True` turns that into a refusal. Without the refusal a filesystem that
    cannot provide the OS lock silently degrades to thread-only exclusion, which is the whole
    guarantee: the body must not run, and no capability may be minted.
    """
    store = ShareStore(tmp_path)
    store.dir.mkdir(parents=True, exist_ok=True)
    store._lock_path.mkdir()                          # an inode that can never be opened for append

    entered = []
    with pytest.raises(ShareError) as excinfo:
        with store._store_lock():
            entered.append(True)
    assert not entered, "the store lock yielded while the OS lock was unavailable"
    assert getattr(excinfo.value, "status_code", None) == 503

    with pytest.raises(ShareError):
        store.create("a1b2c3d4e5f60718", message_count=2)
    assert not list(store.dir.glob("*.json")), "a capability was minted without the store lock"


def test_the_lock_file_lives_inside_the_store_directory(tmp_path):
    store = ShareStore(tmp_path)
    assert store._lock_path.parent == store.dir
    assert store._lock_path.name.startswith(".")


def test_creating_and_revoking_still_works_under_the_new_lock(tmp_path):
    """End to end: the added exclusion must not have broken the capability lifecycle itself."""
    store = ShareStore(tmp_path)
    sid = "a1b2c3d4e5f60718"                    # ids are minted with `token_hex`, hence the shape
    # message_count must be EVEN: a transcript is user/assistant pairs.
    token, record = store.create(sid, message_count=4, title="t")
    assert record.get("session") == sid
    assert "token_hash" not in record, "the public view must never carry the digest"

    resolved = store.resolve(token)
    assert resolved is not None

    store.revoke_session(sid)
    assert store.resolve(token) is None, "a revoked link must stop resolving"


# --- the reservation protocol: O_EXCL, and what a crash leaves behind ----------------------------

def test_a_colliding_id_never_replaces_a_live_capability(tmp_path, monkeypatch):
    """`O_EXCL`, driven. The old loop looked (`exists()`/`is_symlink()`) and wrote later, so a
    writer that did not hold this store's lock could land between the two and have its capability
    overwritten — its `token_hash` replaced by a new secret's, silently revoking a live link.
    Forcing the first minted id to collide proves the reservation refuses that id instead."""
    store = ShareStore(tmp_path)
    live_token, live_record = store.create("a1b2c3d4e5f60718", message_count=2)
    taken = live_record["id"]
    ids = iter([taken, taken, secrets.token_hex(16)])
    monkeypatch.setattr(assistant.secrets, "token_hex", lambda _n: next(ids))

    second_token, second_record = store.create("b1b2c3d4e5f60718", message_count=2)
    assert second_record["id"] != taken, "the reservation reused an id that was already a capability"
    assert store.resolve(live_token) is not None, "an existing capability was overwritten"
    assert store.resolve(second_token) is not None


def test_an_exhausted_namespace_refuses_rather_than_overwriting(tmp_path, monkeypatch):
    """Practically unreachable with 128 random bits, and it must still be a refusal: every attempt
    colliding means every attempt was somebody else's live record."""
    store = ShareStore(tmp_path)
    _token, record = store.create("a1b2c3d4e5f60718", message_count=2)
    monkeypatch.setattr(assistant.secrets, "token_hex", lambda _n: record["id"])
    with pytest.raises(ShareError) as excinfo:
        store.create("b1b2c3d4e5f60718", message_count=2)
    assert excinfo.value.code == "assistant_share_capacity"


def _reservation(store: ShareStore, *, age: float = 0.0) -> tuple[str, object]:
    """The footprint of a worker that died between reserving an id and publishing its record."""
    store.dir.mkdir(parents=True, exist_ok=True)
    link_id = secrets.token_hex(16)
    path = store.dir / f"{link_id}.json"
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return link_id, path


def test_an_abandoned_reservation_authorizes_nothing_and_is_swept(tmp_path):
    """A crash between `O_EXCL` and the atomic publish leaves an EMPTY file. Empty is fail-closed —
    it can never authenticate a token — and the sweep reclaims the id once it is no longer in
    flight, so a crashed create cannot permanently consume an id or a capacity slot."""
    store = ShareStore(tmp_path)
    link_id, path = _reservation(store, age=5.0)

    assert store.resolve(f"{link_id}.{_SECRET}") is None, "an empty reservation resolved"
    assert store.active_for_session("a1b2c3d4e5f60718") == []

    store.create("a1b2c3d4e5f60718", message_count=2)   # any mutation prunes first
    assert not path.exists(), "the abandoned reservation was never reclaimed"


def test_an_in_flight_reservation_survives_a_concurrent_sweep(tmp_path):
    """The one thing the sweep may NOT do. `create` claims its id and publishes a moment later; a
    prune running in that window (another worker, or an uncoordinated legacy writer whose claim
    predates the store lock) must not delete the claim, or two creators end up holding one id —
    the exact collision `O_EXCL` exists to prevent."""
    store = ShareStore(tmp_path)
    _link_id, path = _reservation(store)                # mtime = now: still in flight

    store.create("a1b2c3d4e5f60718", message_count=2)
    assert path.exists(), "a live reservation was swept away by a concurrent create"


def test_a_failed_publish_heals_its_own_reservation(tmp_path, monkeypatch):
    """The reservation is owned by this caller and still under the store lock, so a publish that
    fails must not leave the id claimed by a record that never landed. It must also not report the
    failure as anything but the store's retryable 503."""
    store = ShareStore(tmp_path)
    store.dir.mkdir(parents=True, exist_ok=True)

    def _explode(*_a, **_k):
        raise OSError("no space left on device")

    monkeypatch.setattr(assistant, "atomic_write_text", _explode)
    with pytest.raises(ShareError) as excinfo:
        store.create("a1b2c3d4e5f60718", message_count=2)
    assert getattr(excinfo.value, "status_code", None) == 503
    assert "no space" not in str(excinfo.value), "the storage error leaked to the caller"
    assert not list(store.dir.glob("*.json")), "the failed create left its reservation behind"


def test_a_publish_that_landed_before_the_error_is_kept(tmp_path, monkeypatch):
    """The uncertain result the sibling's healing was written for: `os.replace` completed and a
    LATER filesystem operation reported failure. Removing the record then would destroy a
    capability the caller was about to be told about."""
    store = ShareStore(tmp_path)
    store.dir.mkdir(parents=True, exist_ok=True)
    real = assistant.atomic_write_text

    def _write_then_fail(path, text, *a, **k):
        real(path, text, *a, **k)
        raise OSError("fsync failed after the rename")

    monkeypatch.setattr(assistant, "atomic_write_text", _write_then_fail)
    token, record = store.create("a1b2c3d4e5f60718", message_count=2)
    monkeypatch.undo()
    assert store.resolve(token) is not None, "a published capability was healed away"
    assert json.loads((store.dir / f"{record['id']}.json").read_text())["id"] == record["id"]


def test_the_reservation_state_rule_is_the_one_both_stores_read(tmp_path):
    """The classification itself, as a truth table — it decides whether a path is free space, and
    both stores now consult the same one. `wait=False` is what a directory SWEEP passes: it must
    answer from the mtime instead of sleeping per entry."""
    directory = tmp_path / "store"
    directory.mkdir()
    absent = directory / "absent.json"
    assert capability_store.reservation_state(absent, wait=False)[0] == "absent"

    fresh = directory / "fresh.json"
    os.close(os.open(fresh, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    assert capability_store.reservation_state(fresh, wait=False)[0] == "fresh_empty"
    stamp = time.time() - 5.0
    os.utime(fresh, (stamp, stamp))
    assert capability_store.reservation_state(fresh, wait=False)[0] == "abandoned_empty"

    corrupt = directory / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert capability_store.reservation_state(corrupt, wait=False) == ("corrupt", None)

    published = directory / "published.json"
    published.write_text(json.dumps({"id": "x"}), encoding="utf-8")
    assert capability_store.reservation_state(published, wait=False) == ("valid", {"id": "x"})

    listed = directory / "listed.json"
    listed.write_text(json.dumps(["not", "a", "record"]), encoding="utf-8")
    assert capability_store.reservation_state(listed, wait=False) == ("corrupt", None), (
        "a non-object JSON body is not free space — it is state nobody may overwrite")
