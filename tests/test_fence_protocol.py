"""One durable writer-fence PROTOCOL, two schema owners (doc 25 CO-01).

`run_reset.py` and `run_deletion.py` are two fences with genuinely different schemas, error types and
lifecycles that implemented the same fail-closed marker protocol twice — ~180 of ~400 combined lines.
That is the wrong half to duplicate: this is security-adjacent validation whose realistic failure
mode is one copy quietly gaining a fix the other misses.

It had already happened. The deletion side re-derived `atomicio.file_identity` as a local six-field
lambda *while importing the canonical one two lines above*, so a change to the canonical tuple would
have left the deletion fence comparing a weaker identity with nothing to notice.

This file pins the protocol as BEHAVIOUR — the same refusal, at the same phase, for both fences —
rather than as "the code is shared", because sharing that stops being exercised is just as dead as
duplication that stops being synchronised.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import uuid
from pathlib import Path

import pytest

from looplab.core import run_deletion, run_reset
from looplab.core.fence import (
    FENCE_GENERATION_RE, FENCE_MAX_BYTES, FENCE_OPERATION_RE,
    load_bounded_json_marker, publish_bounded_json_marker,
)

_GEN = "a" * 64


def _reset_fence(tmp_path):
    """(publish, load, marker_path) for the reset marker, plus its storage-error class."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = str(uuid.uuid4())
    run_reset.publish_run_reset_marker(
        run_dir, operation_id=operation, expected_generation=_GEN, receipt_name="r.json")
    return (run_dir, run_reset.reset_marker_path(run_dir),
            run_reset.load_run_reset_marker, run_reset.RunResetStorageError)


def _deletion_fence(tmp_path):
    run_dir = tmp_path / "runs" / "run"
    run_dir.mkdir(parents=True)
    operation = str(uuid.uuid4())
    run_deletion.publish_run_deletion_fence(
        run_dir, operation_id=operation, expected_generation=_GEN, expected_seq=3,
        receipt_name="r.json")
    return (run_dir, run_deletion.run_deletion_fence_path(run_dir),
            run_deletion.load_run_deletion_fence, run_deletion.RunDeletionStorageError)


FENCES = pytest.mark.parametrize("build", [_reset_fence, _deletion_fence],
                                 ids=["reset", "deletion"])


# ------------------------------------------------------- the protocol, proved on BOTH fences

@FENCES
def test_a_published_fence_reads_back_exactly(tmp_path, build):
    run_dir, path, load, _error = build(tmp_path)
    marker = load(run_dir)
    assert marker is not None
    assert json.loads(path.read_bytes().decode("utf-8")) == marker


@FENCES
def test_no_fence_is_none_not_an_error(tmp_path, build):
    """The one case that must NOT raise: an absent marker means no fence, and every writer depends
    on that distinction to make progress at all."""
    run_dir, path, load, _error = build(tmp_path)
    path.unlink()
    assert load(run_dir) is None


@FENCES
def test_an_undecodable_fence_is_a_storage_error_not_an_absent_one(tmp_path, build):
    """Fail CLOSED. A marker that cannot be parsed is an UNKNOWN fence; treating it as "no fence"
    would hand a non-owner permission to mutate the run."""
    run_dir, path, load, error = build(tmp_path)
    path.write_bytes(b"{not json")
    with pytest.raises(error):
        load(run_dir)


@FENCES
def test_a_malformed_fence_is_rejected_by_its_owner_schema(tmp_path, build):
    run_dir, path, load, error = build(tmp_path)
    value = json.loads(path.read_bytes().decode("utf-8"))
    value["operation_id"] = "not-a-uuid"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(error, match="malformed"):
        load(run_dir)


@FENCES
def test_an_extra_key_is_malformed_rather_than_ignored(tmp_path, build):
    """Both schemas compare the key SET exactly. A tolerated extra key is how a second writer's
    dialect gets silently accepted by a fence that was meant to be exact."""
    run_dir, path, load, error = build(tmp_path)
    value = json.loads(path.read_bytes().decode("utf-8"))
    value["extra"] = 1
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(error, match="malformed"):
        load(run_dir)


@FENCES
def test_an_oversized_fence_is_refused_before_it_is_read(tmp_path, build):
    """Two guards, and both matter — so the test proves WHICH one fired.

    The pre-read `st_size` check is what keeps a hostile or corrupt marker from being pulled into
    memory at all; the post-read `len(raw)` check catches a file that GREW between the lstat and the
    read. Asserting only "it raised" would go green with the pre-read guard deleted, since the
    second one still refuses — after doing the very read the first one exists to prevent.
    """
    run_dir, path, load, error = build(tmp_path)
    path.write_text(" " * (FENCE_MAX_BYTES + 10), encoding="utf-8")

    opened: list[str] = []
    real_open = Path.open

    def _open(self, *args, **kwargs):
        if self == path:
            opened.append(str(self))
        return real_open(self, *args, **kwargs)

    Path.open = _open
    try:
        with pytest.raises(error, match="safety limit"):
            load(run_dir)
    finally:
        Path.open = real_open
    assert opened == [], "the oversized marker was opened; the pre-read size guard is gone"


def test_both_size_guards_are_present_because_a_file_can_grow_under_the_read():
    """The post-read guard is unreachable in a normal test — it needs the file to grow between the
    lstat and the read — so it is pinned structurally rather than left unguarded."""
    from looplab.core import fence

    body = inspect.getsource(fence.load_bounded_json_marker)
    assert "before.st_size > max_bytes" in body, "the pre-read size guard is gone"
    assert "len(raw) > max_bytes" in body, "the post-read size guard is gone"


@FENCES
def test_a_directory_where_the_fence_should_be_is_refused(tmp_path, build):
    """`stat.S_ISREG` — a non-regular path is not a service-owned marker, and reading one could
    block or succeed with something that is not the fence at all."""
    run_dir, path, load, error = build(tmp_path)
    path.unlink()
    path.mkdir()
    with pytest.raises(error, match="regular service-owned file"):
        load(run_dir)


# ------------------------------------------------------- the identity check, the actual drift

def test_the_shared_read_refuses_a_marker_replaced_under_it(tmp_path):
    """The second lstat is the whole point of the protocol: a marker replaced mid-read would
    otherwise be parsed as a mix of two generations. Driven directly, because racing a real
    replacement is exactly the kind of thing a wall-clock test gets wrong."""
    path = tmp_path / "marker.json"
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")

    class _Boom(RuntimeError):
        pass

    real_lstat = Path.lstat
    seen = {"n": 0}

    def _lstat(self):
        seen["n"] += 1
        if seen["n"] == 2:                    # the read-back lstat: replace the file first
            real_lstat(self)                  # (touch it the same way the protocol would)
            path.write_text(json.dumps({"version": 1, "changed": True}) + " ", encoding="utf-8")
        return real_lstat(self)

    Path.lstat = _lstat
    try:
        with pytest.raises(_Boom, match="changed while it was being read"):
            load_bounded_json_marker(path, label="marker", error_cls=_Boom,
                                     validate=lambda value: True)
    finally:
        Path.lstat = real_lstat
    assert seen["n"] == 2, "the protocol must lstat again AFTER the read"


def test_the_deletion_fence_no_longer_hand_rolls_the_identity_tuple():
    """The concrete drift CO-01 names: a local six-field lambda shadowing the canonical
    `atomicio.file_identity` the module already imported."""
    import inspect

    source = inspect.getsource(run_deletion)
    assert "identity = lambda info:" not in source
    assert "st_file_attributes" not in source, (
        "the deletion fence is re-deriving the stat tuple that atomicio owns")


def test_both_fences_read_through_the_shared_protocol():
    """If an owner re-inlines the read, this file's parametrized cases would still pass while the
    two copies drifted again — so the sharing itself is asserted, not just its effects."""
    import inspect

    for module, loader in ((run_reset, run_reset.load_run_reset_marker),
                           (run_deletion, run_deletion.load_run_deletion_fence)):
        source = inspect.getsource(module)
        assert "load_bounded_json_marker(" in source, f"{module.__name__} re-inlined the read"
        assert "publish_bounded_json_marker(" in source, f"{module.__name__} re-inlined the write"
        # Scoped to the LOADER: `run_deletion` legitimately opens the event log elsewhere, when it
        # derives an empty run's deletion identity. That is a different file and a different job.
        body = inspect.getsource(loader)
        for inlined in ('path.open("rb")', "lstat()", "json.loads("):
            assert inlined not in body, f"{loader.__name__} still performs the read itself"


def test_the_shapes_are_declared_once():
    """Both fences bind an operation to a UUID shape and a run to a 64-hex generation. Declared
    twice, one could be loosened alone."""
    assert run_reset.RUN_RESET_OPERATION_RE is FENCE_OPERATION_RE
    assert run_deletion.RUN_DELETION_OPERATION_RE is FENCE_OPERATION_RE
    assert run_reset._RUN_GENERATION_RE is FENCE_GENERATION_RE
    assert run_deletion._RUN_GENERATION_RE is FENCE_GENERATION_RE
    assert run_reset.RUN_RESET_MARKER_MAX_BYTES == run_deletion.RUN_DELETION_FENCE_MAX_BYTES

    # ...and the run key keeps its OWN name even though `re.compile` hands back the same cached
    # object for the same pattern, because it means something else entirely: a sha256 of the run's
    # filesystem identity, not a generation. Folding the two names together would invite someone
    # widening one to widen both.
    assert isinstance(run_deletion._RUN_KEY_RE, re.Pattern)
    key = run_deletion.run_deletion_key(Path("/tmp/whatever"))
    assert run_deletion._RUN_KEY_RE.fullmatch(key), "a run key must satisfy its own shape"
    source = inspect.getsource(run_deletion)
    assert '_RUN_KEY_RE = re.compile' in source, (
        "the run key must keep its own declaration, not alias the generation shape")


# ------------------------------------------------------- what each owner still decides alone

def test_the_deletion_fence_is_bound_to_its_exact_run(tmp_path):
    """The reset marker lives INSIDE the run; the deletion fence lives beside it and therefore has
    to prove which run it belongs to. A fence copied next to a different run is malformed, not
    authoritative — that check is the deletion schema's alone and must not migrate to the protocol."""
    run_dir, path, load, error = _deletion_fence(tmp_path)
    other = tmp_path / "runs" / "other"
    other.mkdir()
    foreign = run_deletion.run_deletion_fence_path(other)
    foreign.write_bytes(path.read_bytes())          # same bytes, wrong run
    with pytest.raises(error, match="malformed"):
        load(other)


def test_a_second_deleter_cannot_take_over_an_owned_run(tmp_path):
    """Reset republishes; deletion refuses. Replacing a live deletion fence would hand ownership to
    a second deleter mid-operation, so the two publish paths differ on purpose."""
    run_dir, _path, _load, error = _deletion_fence(tmp_path)
    with pytest.raises(error, match="another deletion operation owns this run"):
        run_deletion.publish_run_deletion_fence(
            run_dir, operation_id=str(uuid.uuid4()), expected_generation=_GEN,
            expected_seq=3, receipt_name="r.json")
    # ...but the SAME operation retrying is idempotent.
    marker = run_deletion.load_run_deletion_fence(run_dir)
    assert run_deletion.publish_run_deletion_fence(
        run_dir, operation_id=marker["operation_id"], expected_generation=_GEN,
        expected_seq=3, receipt_name="r.json") == marker


def test_publication_that_cannot_be_confirmed_is_a_storage_error(tmp_path):
    """A strict write can fail after the replacement became visible. The read-back is the authority
    on every later decision, so a half-published fence has to be loud rather than unsatisfiable."""
    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom, match="could not be confirmed"):
        publish_bounded_json_marker(
            tmp_path / "m.json", {"version": 1}, label="marker", error_cls=_Boom,
            confirm=lambda: None)


def test_the_two_fences_keep_distinct_error_types(tmp_path):
    """Sharing the protocol must not collapse the vocabulary: callers catch one or the other to
    decide whether a run is mid-reset or mid-deletion."""
    assert run_reset.RunResetStorageError is not run_deletion.RunDeletionStorageError
    assert run_reset.RunResetFenceError is not run_deletion.RunDeletionFenceError

    run_dir, path, _load, _error = _reset_fence(tmp_path)
    path.write_bytes(b"{")
    with pytest.raises(run_reset.RunResetStorageError):
        run_reset.assert_run_reset_write_allowed(run_dir)


def test_the_owner_operation_still_passes_and_a_stranger_still_does_not(tmp_path):
    """End to end, because the protocol extraction touched the loader every gate reads through."""
    run_dir, path, _load, _error = _reset_fence(tmp_path)
    marker = run_reset.load_run_reset_marker(run_dir)

    assert run_reset.assert_run_reset_write_allowed(run_dir, marker["operation_id"]) == marker
    with pytest.raises(run_reset.RunResetFenceError):
        run_reset.assert_run_reset_write_allowed(run_dir, str(uuid.uuid4()))

    os.environ[run_reset.RUN_RESET_OPERATION_ENV] = marker["operation_id"]
    try:
        assert run_reset.assert_run_reset_write_allowed(run_dir) == marker
    finally:
        del os.environ[run_reset.RUN_RESET_OPERATION_ENV]

    assert run_reset.clear_run_reset_marker(run_dir, marker["operation_id"]) is True
    assert run_reset.load_run_reset_marker(run_dir) is None
    assert run_reset.assert_run_reset_write_allowed(run_dir) is None
