"""The share store mutates under a CROSS-PROCESS lock, like its sibling (doc 25 SC-10).

`ShareStore` and `ReviewStore` are the same concept — one file per capability, sha256 `token_hash`
(never the token), TTL bounds, `revoked_at` tombstones, `public()` views that strip the digest,
constant-time `resolve` — with materially different guarantees. `ReviewStore` pairs a per-path
process lock with a REQUIRED OS lock; `ShareStore` had only `threading.Lock()`.

That gap is not theoretical for a bearer capability: `revoke_session` is a read-modify-write over a
directory of records, so with two uvicorn workers one worker's revocation could be lost behind the
other's write — leaving live a link the owner was told was dead.

The split is deliberate and matches the sibling: MUTATIONS take the store lock, reads do not. Locking
reads would serialize the HTTP read path across workers and turn contention into 503s on a path that
cannot corrupt anything.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import threading

import pytest

from looplab.serve import assistant, reviews
from looplab.serve.assistant import ShareStore

MUTATORS = ("create", "revoke_token", "revoke_session")
READERS = ("resolve", "active_for_session", "active_summary_by_session")


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
        with pytest.raises(assistant.ShareError) as excinfo:
            with store._store_lock():
                pass
    finally:
        release.set()
        worker.join(timeout=10)
    assert getattr(excinfo.value, "status_code", None) == 503


def test_the_interprocess_failure_path_raises_rather_than_yielding(tmp_path):
    """The OTHER half of "fails closed", which the contention test above cannot reach.

    That test contends on the per-path PROCESS lock — same interpreter — so it raises before the
    interprocess block runs. Deleting the `raise` around `_interprocess_lock` therefore leaves it
    green while a filesystem that cannot provide the OS lock silently degrades to thread-only
    exclusion, which is exactly the guarantee this change adds. Pinned structurally: every handler
    in `_store_lock` must raise, and none may `yield`.
    """
    source, tree = _parse(ShareStore._store_lock)
    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    assert handlers, "_store_lock no longer guards the interprocess acquisition at all"
    for handler in handlers:
        yields = [n for n in ast.walk(handler) if isinstance(n, (ast.Yield, ast.YieldFrom))]
        assert not yields, (
            "_store_lock yields from an exception handler — a filesystem that cannot provide the OS "
            "lock would silently degrade to thread-only exclusion")
        raises = [n for n in ast.walk(handler) if isinstance(n, ast.Raise)]
        assert raises, "_store_lock swallows a lock failure instead of failing closed"

    assert "required=True" in source, "the OS lock must be REQUIRED, not best-effort"
    assert "blocking=False" in source, (
        "a blocking acquisition parks an HTTP thread behind another worker indefinitely")


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
