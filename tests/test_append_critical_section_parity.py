"""`append` and `append_many` share ONE critical section (doc 25 EV-06).

Both public appenders used to carry a verbatim copy of the same ~55-line body: the reset/deletion
write fences, the fail-closed divergence check, the torn-tail heal, the two-source seq derivation,
the explicit-seq CAS, the accepted-bytes reservation on a failed sync, the directory-entry publish,
and the cache/stat bookkeeping. Every rule therefore existed twice, and the copies were extended
several times over their life — which is the shape where a durability or fencing fix lands on one
spelling and is silently missing from the other. Nothing failed when they diverged: `append_many`
simply lost a guarantee its own callers (paid-work claims, control transactions) believe it has.

Two guards, in opposite directions. The source scan pins that neither public method re-grows a
private critical section of its own; the parity table runs each rule against BOTH appenders, so a
rule that stops holding for one of them is a red test rather than a silent asymmetry.
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import orjson
import pytest

from looplab.core.models import Event
from looplab.events import eventstore as es
from looplab.events.eventstore import (
    EventLogCorruptionError,
    EventStore,
    EventStoreConcurrencyError,
    EventStoreLockError,
)

# The vocabulary of the critical section. A public appender that names any of these is doing the
# work itself again instead of delegating — which is exactly how the two copies drifted apart.
_CRITICAL_SECTION_ONLY = (
    "_interprocess_lock", "_append_lock", "_heal_torn_tail", "_disk_last_seq",
    "_publish_dir_entry", "_mark_uncertain_append", "_trusted_growth_stat",
    "strict_fsync", "best_effort_fsync", "assert_run_reset_write_allowed",
    "assert_run_deletion_write_allowed",
)


def _method_code(name: str) -> str:
    """The method's body as code, docstring stripped — a docstring may legitimately DISCUSS fsync."""
    tree = ast.parse(inspect.getsource(getattr(EventStore, name)).lstrip())
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    return ast.unparse(fn)


@pytest.mark.parametrize("method", ["append", "append_many"])
def test_public_appenders_delegate_the_critical_section(method):
    code = _method_code(method)
    assert "_locked_append" in code, (
        f"EventStore.{method} no longer routes through _locked_append — the durability, fencing and "
        "CAS rules it needs are now its own copy again")
    offenders = sorted({name for name in _CRITICAL_SECTION_ONLY if name in code})
    assert not offenders, (
        f"EventStore.{method} re-implements part of the shared critical section ({offenders}); put "
        "the rule in _locked_append so the other appender gets it too")


def _appenders(store: EventStore):
    """The same logical operation through each public method, so one table can drive both."""
    return {
        "append": lambda **kw: store.append("t", {"i": 1}, **kw),
        "append_many": lambda **kw: store.append_many([("t", {"i": 1})], **kw),
    }


@pytest.fixture(params=["append", "append_many"])
def appender(request, tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    return store, _appenders(store)[request.param]


def test_divergence_fails_closed(tmp_path, request):
    """A corrupt COMPLETE line with records after it must stop BOTH appenders, not just one:
    read_all halts there, so anything appended past it is durable and invisible to the fold."""
    path = tmp_path / "events.jsonl"
    good = orjson.dumps(Event(seq=0, ts=1.0, type="t", data={}).model_dump(mode="json"))
    later = orjson.dumps(Event(seq=1, ts=1.0, type="t", data={}).model_dump(mode="json"))
    path.write_bytes(good + b"\n" + b"notjson\n" + later + b"\n")
    store = EventStore(path)
    assert store.divergence is not None
    for name, call in _appenders(store).items():
        with pytest.raises(EventLogCorruptionError):
            call()


def test_explicit_seq_cas_rejects_a_moved_tail(appender):
    store, call = appender
    store.append("seed", {})
    with pytest.raises(EventStoreConcurrencyError):
        call(expected_last_seq=99)
    # ...and lands when the caller names the tail it actually read.
    call(expected_last_seq=store.read_all()[-1].seq)
    assert len(store.read_all()) == 2


def test_torn_tail_is_healed_before_the_record_is_written(appender):
    """Without the heal the new bytes glue onto the partial line and every reader stops there."""
    store, call = appender
    store.append("seed", {})
    with open(store.path, "ab") as f:
        f.write(b'{"seq": 1, "ts": 1.0, "type": "torn"')   # no newline: a crash mid-append
    call()
    events = store.read_all()
    assert [e.type for e in events] == ["seed", "t"]
    assert [e.seq for e in events] == [0, 1]


def test_required_lock_failure_is_reported_not_degraded(appender, monkeypatch):
    """A caller that opted into cross-process serialization must never silently append unlocked."""
    store, call = appender
    if os.name == "nt":   # the msvcrt arm takes a different module; the POSIX arm is what CI runs
        pytest.skip("POSIX advisory-lock arm")
    import fcntl

    def _unsupported(*_a, **_kw):
        raise ValueError("advisory locks unsupported on this filesystem")

    monkeypatch.setattr(fcntl, "flock", _unsupported)
    with pytest.raises(EventStoreLockError):
        call(require_lock=True)


def test_durable_append_that_creates_the_log_also_publishes_its_directory_entry(appender):
    """Content-only fsync loses the WHOLE FILE on power loss when this append created it — the
    exact case `require_durable` exists for. Both appenders serve paid-work claims."""
    store, call = appender
    synced: list[str] = []
    original_file = es.strict_fsync
    original_parent = es.strict_fsync_parent
    try:
        es.strict_fsync = lambda fd: (synced.append("file"), original_file(fd))[1]
        es.strict_fsync_parent = lambda p: (synced.append("parent"), original_parent(p))[1]
        call(require_durable=True)
    finally:
        es.strict_fsync = original_file
        es.strict_fsync_parent = original_parent
    assert synced == ["file", "parent"], (
        "a durable creating append must sync both the contents and the parent directory entry")


def test_uncertain_sync_fences_the_last_logical_seq(tmp_path):
    """The reservation must cover EVERY logical seq the accepted bytes carry.

    `append_many` writes one physical record holding `count` events, so fencing the first seq (or
    the batch's own position when those ever diverge) would let the next append mint a seq already
    on disk. Bytes accepted, sync unconfirmed — the caller fails closed, the store must not reuse."""
    store = EventStore(tmp_path / "events.jsonl")
    fenced: list[int] = []
    original = EventStore._mark_uncertain_append

    def _spy(self, seq):
        fenced.append(seq)
        return original(self, seq)

    def _boom(_fd):
        raise OSError("sync unconfirmed")

    saved_sync, EventStore._mark_uncertain_append = es.strict_fsync, _spy
    es.strict_fsync = _boom
    try:
        with pytest.raises(OSError):
            store.append_many([("a", {}), ("b", {}), ("c", {})], require_durable=True)
        with pytest.raises(OSError):
            store.append("d", {}, require_durable=True)
    finally:
        es.strict_fsync = saved_sync
        EventStore._mark_uncertain_append = original
    assert fenced == [2, 3], f"expected the LAST logical seq of each accepted payload, got {fenced}"


def test_both_appenders_write_one_newline_terminated_record(tmp_path):
    """The torn-tail contract is per-record: a payload without its terminating newline makes the
    NEXT append glue onto it. `_locked_append` writes the builder's bytes verbatim, so the newline
    is the builder's job — pin that both builders still supply it."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("one", {})
    store.append_many([("two", {}), ("three", {})])
    raw = Path(store.path).read_bytes()
    assert raw.endswith(b"\n") and b"\n\n" not in raw
    assert len(raw.split(b"\n")) - 1 == 2          # exactly two PHYSICAL records
    assert [e.type for e in store.read_all()] == ["one", "two", "three"]
