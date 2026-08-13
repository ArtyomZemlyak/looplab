"""Unit tests for the durable trace-clear state machine (doc 25 SR-03).

The finding that motivated `serve/trace_clear.py` was not just its size: seventeen closures inside
`build_router` meant every branch of a crash-recovery protocol — pending/succeeded/superseded,
digest-CAS on spans.jsonl, recovery ownership — was reachable only by building the whole ASGI app
and driving HTTP. That is the wrong instrument for a state machine whose interesting states are
"a previous process died between the write-ahead record and the replacement".

So these drive the module directly against a stub `srv`: no FastAPI app, no engine, no run. The
HTTP-level behaviour stays pinned where it already was (`test_server.py`, `test_run_command_service.py`,
`test_span_index.py`); what is new here is the ability to construct the post-crash states those
tests cannot reach, and to assert the ONE property that matters in each — that an unconfirmed or
unreconstructable outcome never authorizes another deletion.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException

from looplab.serve import trace_clear as tc


# --------------------------------------------------------------------------- stub server

class _StubCommands:
    """The three `srv.commands` surfaces the trace-clear machine touches."""

    def __init__(self, root: Path, generation: str):
        self.root = root
        self.generation = generation
        self.guarded: list[str] = []

    def _sequence_path(self, rd: Path) -> Path:
        # Mirrors the real derivation closely enough for receipt naming: a `.lock` file one level
        # below the root, so `parent.parent` is the root the receipts live in.
        directory = self.root / ".command-locks"
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(rd).encode("utf-8")).hexdigest()
        return directory / f"{digest}.lock"

    def run_generation(self, rd: Path) -> str:
        return self.generation

    @contextmanager
    def destructive_guard(self, rd: Path, operation: str):
        self.guarded.append(operation)
        yield rd


class _StubNode:
    def __init__(self, attempt: int = 0, tombstoned: bool = False):
        self.attempt = attempt
        self.tombstoned = tombstoned


class _StubState:
    def __init__(self, nodes=None, resume=False, aborted=()):
        self.nodes = nodes if nodes is not None else {0: _StubNode()}
        self.aborted_nodes = set(aborted)
        self._resume = resume

    def resume_pending(self) -> bool:
        return self._resume


class _StubSrv:
    def __init__(self, root: Path, *, generation: str = "a" * 64, state: _StubState | None = None):
        self.root = root
        self.commands = _StubCommands(root, generation)
        self._state = state if state is not None else _StubState()
        self.invalidated: list[Path] = []

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def state(self, rd: Path) -> _StubState:
        return self._state

    def invalidate_trace_view(self, rd: Path) -> None:
        self.invalidated.append(rd)


SPANS = (
    '{"span_id": "a", "attributes": {"node_id": 0}}\n'
    '{"span_id": "b", "attributes": {"node_id": 1}}\n'
    '{"span_id": "c", "attributes": {}}\n'
)


def _run(tmp_path: Path, *, spans: str | None = SPANS) -> tuple[_StubSrv, Path]:
    root = tmp_path / "runs"
    rd = root / "demo"
    rd.mkdir(parents=True)
    (rd / "events.jsonl").write_text("", encoding="utf-8")
    if spans is not None:
        (rd / "spans.jsonl").write_text(spans, encoding="utf-8")
    return _StubSrv(root), rd


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _filter_bytes(directory: Path, source: bytes, node_ids: set[int | str]) -> tuple[bytes, dict]:
    """Test adapter around the production disk-backed streaming filter."""
    path = directory / ".trace-clear-filter-source.jsonl"
    path.write_bytes(source)
    prepared = tc._prepare_filtered_trace_snapshot(path, node_ids)
    try:
        result = (prepared.temporary.read_bytes()
                  if prepared.temporary is not None else path.read_bytes())
        assert hashlib.sha256(result).hexdigest() == prepared.result_digest
        return result, prepared.result
    finally:
        prepared.cleanup()
        path.unlink(missing_ok=True)


def _pending(srv: _StubSrv, rd: Path, *, nid: int = 0, source: str = SPANS,
             result: str | None = None, counts: dict | None = None,
             operation: str = "tc_" + "1" * 32, status: str = "pending") -> tuple[Path, dict]:
    """Write-ahead record for a clear that a previous process never finished."""
    if result is None:
        result_bytes, computed = _filter_bytes(rd, source.encode("utf-8"), {nid})
        result = result_bytes.decode("utf-8")
        counts = counts if counts is not None else computed
    receipt = {
        "version": 2,
        "id": operation,
        "status": status,
        "expected_generation": srv.commands.generation,
        "expected_trace_revision": "b" * 64,
        "node_id": nid,
        "node_generation": 0,
        "source_exists": True,
        "source_digest": _digest(source),
        "result_exists": True,
        "result_digest": _digest(result),
        "result": counts,
        "created_at": 1.0,
    }
    path = tc._trace_clear_receipt_path(srv, rd, operation)
    tc._save_trace_clear_receipt(path, receipt)
    return path, receipt


def _detail(exc_info) -> dict:
    detail = exc_info.value.detail
    return detail if isinstance(detail, dict) else {"message": detail}


# --------------------------------------------------------------------------- the row filter

def test_filter_removes_only_the_named_node_and_preserves_every_other_byte(tmp_path):
    result, counts = _filter_bytes(tmp_path, SPANS.encode("utf-8"), {0})
    assert counts == {"removed": 1, "kept": 2}
    assert result.decode("utf-8") == (
        '{"span_id": "b", "attributes": {"node_id": 1}}\n'
        '{"span_id": "c", "attributes": {}}\n')


def test_filter_keeps_malformed_blank_and_torn_rows_byte_for_byte(tmp_path):
    """A row the exporter mangled must not shadow the valid spans after it, and must not be eaten.

    The torn final row is the one that matters most: it is the row a killed engine was mid-write on,
    so it has no terminating newline and cannot be parsed. Dropping it would silently truncate a
    diagnostics file this operation is only supposed to filter."""
    source = (
        '\n'
        '{"span_id": "a", "attributes": {"node_id": 0}}\n'
        '{not json at all\n'
        '   \n'
        '[1, 2, 3]\n'
        '{"span_id": "b", "attributes": {"node_id": 0}}\n'
        '{"span_id": "torn", "attributes": {"node_id": 0}'
    )
    result, counts = _filter_bytes(tmp_path, source.encode("utf-8"), {0})
    assert counts == {"removed": 2, "kept": 0}
    assert result.decode("utf-8") == (
        '\n'
        '{not json at all\n'
        '   \n'
        '[1, 2, 3]\n'
        '{"span_id": "torn", "attributes": {"node_id": 0}')


def test_filter_matches_node_id_as_text_so_a_string_row_is_not_orphaned(tmp_path):
    source = '{"span_id": "a", "attributes": {"node_id": "7"}}\n'
    result, counts = _filter_bytes(tmp_path, source.encode("utf-8"), {7})
    assert counts == {"removed": 1, "kept": 0} and result == b""


def test_filter_accepts_a_node_set_for_reusable_snapshot_purge(tmp_path):
    result, counts = _filter_bytes(tmp_path, SPANS.encode("utf-8"), {0, "1"})
    assert counts == {"removed": 2, "kept": 1}
    assert result == b'{"span_id": "c", "attributes": {}}\n'


def test_filter_removes_unstamped_children_of_a_legacy_node_trace(tmp_path):
    """Clear follows the same root fallback as the UI, so captured child I/O cannot survive it."""
    source = (
        '{"trace_id":"trace-a","span_id":"child","parent_id":"root",'
        '"name":"generation","kind":"generation","start":2,'
        '"attributes":{"input":[{"role":"user","content":"private"}]}}\n'
        '{"trace_id":"trace-b","span_id":"other","parent_id":null,'
        '"name":"generation","kind":"generation","start":1,'
        '"attributes":{"node_id":1,"output":"keep"}}\n'
        '{"trace_id":"trace-a","span_id":"root","parent_id":null,'
        '"name":"create_node","kind":"operation","start":1,'
        '"attributes":{"node_id":0}}\n'
    )

    result, counts = _filter_bytes(tmp_path, source.encode("utf-8"), {0})

    assert counts == {"removed": 2, "kept": 1}
    assert result.decode("utf-8") == (
        '{"trace_id":"trace-b","span_id":"other","parent_id":null,'
        '"name":"generation","kind":"generation","start":1,'
        '"attributes":{"node_id":1,"output":"keep"}}\n'
    )


def test_prepare_replace_and_readback_stay_streamed_across_chunk_boundaries(
        tmp_path, monkeypatch):
    source = (
        b'{"span_id":"a","attributes":{"node_id":0},"payload":"secret"}\n'
        b'{"span_id":"b","attributes":{"node_id":1},"payload":"keep"}\n'
        b'{"span_id":"torn","attributes":{"node_id":0}'
    )
    path = tmp_path / "spans.jsonl"
    path.write_bytes(source)
    monkeypatch.setattr(tc, "_TRACE_CLEAR_STREAM_CHUNK", 7)

    def _whole_file_read_forbidden(self):
        raise AssertionError("the streaming clear must not call Path.read_bytes")

    monkeypatch.setattr(Path, "read_bytes", _whole_file_read_forbidden)
    prepared = tc._prepare_filtered_trace_snapshot(path, {0})
    expected = (
        b'{"span_id":"b","attributes":{"node_id":1},"payload":"keep"}\n'
        b'{"span_id":"torn","attributes":{"node_id":0}'
    )
    try:
        assert prepared.result == {"removed": 1, "kept": 1}
        assert prepared.result_size == len(expected)
        assert prepared.result_digest == hashlib.sha256(expected).hexdigest()
        tc._strict_replace_prepared_trace(prepared, path)
        assert tc._trace_digest_snapshot(path).digest == hashlib.sha256(expected).hexdigest()
        assert path.read_text(encoding="utf-8") == expected.decode("utf-8")
    finally:
        prepared.cleanup()


def test_filter_refuses_an_oversized_physical_row_before_staging(tmp_path, monkeypatch):
    """A destructive filter cannot rebuild truth behind a row trace readers refuse to admit."""
    path = tmp_path / "spans.jsonl"
    before = b'{"span_id":"a","attributes":{"node_id":0}}\n'
    hidden = b'{"span_id":"b","attributes":{"node_id":1}}\n'
    ceiling = max(len(before), len(hidden)) + 8
    oversized = (
        b'{"span_id":"too-large","attributes":{"node_id":0},"payload":"'
        + b"x" * (ceiling * 5) + b'"}\n'
    )
    original = before + oversized + hidden
    path.write_bytes(original)
    monkeypatch.setattr(tc, "TRACE_JSONL_ROW_MAX_BYTES", ceiling)
    monkeypatch.setattr(tc, "_TRACE_CLEAR_STREAM_CHUNK", 17)

    with pytest.raises(HTTPException) as exc:
        tc._prepare_filtered_trace_snapshot(path, {0})

    assert exc.value.status_code == 409
    assert _detail(exc)["code"] == "trace_row_too_large"
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".spans.jsonl.clear.*.tmp")) == []


def test_filter_caps_root_attribution_metadata_before_staging(tmp_path, monkeypatch):
    """Tiny valid rows are payload-bounded yet could otherwise grow the in-memory graph forever."""
    path = tmp_path / "spans.jsonl"
    rows = [
        json.dumps({
            "span_id": f"s{index}", "trace_id": "t", "parent_id": None,
            "attributes": {"node_id": index},
        }, separators=(",", ":")).encode("utf-8") + b"\n"
        for index in range(3)
    ]
    original = b"".join(rows)
    path.write_bytes(original)
    monkeypatch.setattr(tc, "_TRACE_CLEAR_METADATA_ROW_MAX", 2)
    monkeypatch.setattr(tc, "_TRACE_CLEAR_STREAM_CHUNK", 11)

    with pytest.raises(HTTPException) as exc:
        tc._prepare_filtered_trace_snapshot(path, {0})

    assert exc.value.status_code == 409
    assert _detail(exc)["code"] == "trace_metadata_too_large"
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".spans.jsonl.clear.*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="resource.getrusage is not available on Windows")
def test_streaming_clear_peak_rss_is_bounded_below_the_source_payload(tmp_path):
    """Source, filtered result and read-back must never coexist as in-memory byte strings."""
    script = r'''
import gc
import json
import resource
import sys
from pathlib import Path
from looplab.serve import trace_clear as tc

path = Path(sys.argv[1])
payload = "x" * (512 * 1024)
target = json.dumps(
    {"span_id": "a", "attributes": {"node_id": 0}, "payload": payload},
    separators=(",", ":"),
).encode() + b"\n"
keep = json.dumps(
    {"span_id": "b", "attributes": {"node_id": 1}, "payload": payload},
    separators=(",", ":"),
).encode() + b"\n"
with path.open("wb") as stream:
    for _ in range(48):
        stream.write(target)
        stream.write(keep)
source_size = path.stat().st_size
del payload, target, keep
gc.collect()
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
prepared = tc._prepare_filtered_trace_snapshot(path, {0})
try:
    result_size = prepared.result_size
    result_digest = prepared.result_digest
    tc._strict_replace_prepared_trace(prepared, path)
    readback = tc._trace_digest_snapshot(path)
    assert readback.digest == result_digest
    assert readback.size == result_size
finally:
    prepared.cleanup()
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
unit = 1 if sys.platform == "darwin" else 1024
print(json.dumps({
    "source": source_size,
    "result": result_size,
    "peak_delta_bytes": max(0, after - before) * unit,
}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "spans.jsonl")],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    measured = json.loads(completed.stdout.strip().splitlines()[-1])
    assert measured["source"] > 48 * 1024 * 1024
    assert 23 * 1024 * 1024 < measured["result"] < 25 * 1024 * 1024
    # The former read_bytes/split/join path retained well over one source-sized allocation.  Leave
    # generous allocator/platform headroom while keeping the fence below even the source alone.
    assert measured["peak_delta_bytes"] < 32 * 1024 * 1024


# --------------------------------------------------------------------------- trace snapshots

def test_snapshot_of_a_missing_trace_is_a_known_empty_not_a_failure(tmp_path):
    _srv, rd = _run(tmp_path, spans=None)
    snapshot = tc._trace_digest_snapshot(rd / "spans.jsonl")
    assert snapshot.exists is False and snapshot.size == 0 and snapshot.digest == _digest("")


def test_snapshot_refuses_a_symlinked_trace(tmp_path):
    """spans.jsonl is rewritten in place; following a link would let it retarget the destructive
    write at a file the run does not own."""
    _srv, rd = _run(tmp_path)
    target = rd / "elsewhere.jsonl"
    target.write_text(SPANS, encoding="utf-8")
    (rd / "spans.jsonl").unlink()
    (rd / "spans.jsonl").symlink_to(target)
    with pytest.raises(HTTPException) as exc:
        tc._trace_digest_snapshot(rd / "spans.jsonl")
    assert exc.value.status_code == 409 and _detail(exc)["code"] == "trace_path_invalid"


# --------------------------------------------------------------------------- receipt storage

def test_absent_receipt_reads_as_none_not_as_an_error(tmp_path):
    srv, rd = _run(tmp_path)
    assert tc._load_trace_clear_receipt(
        tc._trace_clear_receipt_path(srv, rd, "tc_" + "0" * 32)) is None


@pytest.mark.parametrize("body", [
    "{not json",                                             # unreadable
    '{"version": 1, "status": "pending"}',                    # wrong version
    '{"version": 2, "status": "invented"}',                   # unknown status
    '{"version": 2, "status": "pending", "id": "nope"}',      # malformed operation id
])
def test_an_unreadable_or_malformed_receipt_is_503_never_a_fresh_operation(tmp_path, body):
    """Fail closed: a receipt that cannot be interpreted may still describe a paid deletion."""
    srv, rd = _run(tmp_path)
    path = tc._trace_clear_receipt_path(srv, rd, "tc_" + "0" * 32)
    path.write_text(body, encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        tc._load_trace_clear_receipt(path)
    assert exc.value.status_code == 503
    assert _detail(exc)["code"] == "trace_clear_receipt_unavailable"


def test_a_directory_or_symlink_in_the_receipt_namespace_is_409(tmp_path):
    srv, rd = _run(tmp_path)
    directory = tc._trace_clear_receipt_path(srv, rd, "tc_" + "0" * 32)
    directory.mkdir()
    with pytest.raises(HTTPException) as exc:
        tc._trace_clear_regular_receipt(directory)
    assert exc.value.status_code == 409
    assert _detail(exc)["code"] == "trace_clear_receipt_path_invalid"

    link = tc._trace_clear_receipt_path(srv, rd, "tc_" + "1" * 32)
    target = rd / "target.json"
    target.write_text("{}", encoding="utf-8")
    link.symlink_to(target)
    with pytest.raises(HTTPException) as exc:
        tc._load_trace_clear_receipt(link)
    assert _detail(exc)["code"] == "trace_clear_receipt_path_invalid"


def test_a_hardlinked_receipt_is_rejected_without_reading_its_alias(tmp_path):
    srv, rd = _run(tmp_path)
    path = tc._trace_clear_receipt_path(srv, rd, "tc_" + "2" * 32)
    target = rd / "outside-receipt.json"
    target.write_text("{}", encoding="utf-8")
    try:
        os.link(target, path)
    except (AttributeError, NotImplementedError, OSError) as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    with pytest.raises(HTTPException) as exc:
        tc._load_trace_clear_receipt(path)
    assert exc.value.status_code == 409
    assert _detail(exc)["code"] == "trace_clear_receipt_path_invalid"
    assert target.read_text(encoding="utf-8") == "{}"


def test_an_oversized_receipt_fails_closed_before_json_materialization(tmp_path):
    srv, rd = _run(tmp_path)
    path = tc._trace_clear_receipt_path(srv, rd, "tc_" + "3" * 32)
    path.write_bytes(b"{" + b"x" * tc._TRACE_CLEAR_RECEIPT_MAX_BYTES + b"}")
    with pytest.raises(HTTPException) as exc:
        tc._load_trace_clear_receipt(path)
    assert exc.value.status_code == 503
    assert _detail(exc)["code"] == "trace_clear_receipt_unavailable"


def test_a_receipt_that_cannot_be_re_emitted_is_a_publication_failure(tmp_path):
    """`json.loads` accepts NaN; `json.dumps(allow_nan=False)` refuses it. The republish path must
    surface that as an unavailable receipt rather than raising through the route."""
    srv, rd = _run(tmp_path)
    path = tc._trace_clear_receipt_path(srv, rd, "tc_" + "0" * 32)
    with pytest.raises(HTTPException) as exc:
        tc._save_trace_clear_receipt(path, {"pad": float("nan")})
    assert exc.value.status_code == 503
    assert _detail(exc)["code"] == "trace_clear_receipt_unavailable"


def test_a_bad_operation_id_never_reaches_the_filesystem(tmp_path):
    srv, rd = _run(tmp_path)
    with pytest.raises(HTTPException) as exc:
        tc._trace_clear_receipt_path(srv, rd, "tc_NOPE")
    assert exc.value.status_code == 400


# --------------------------------------------------------------------------- terminal reconciliation

def test_a_receipt_from_a_different_lifecycle_is_a_conflict_not_a_replay(tmp_path):
    srv, rd = _run(tmp_path)
    _path, receipt = _pending(srv, rd)
    with pytest.raises(HTTPException) as exc:
        tc._trace_clear_receipt_result(
            receipt, receipt_path=rd / "unused.json", operation_id=receipt["id"],
            expected_generation=receipt["expected_generation"],
            expected_trace_revision=receipt["expected_trace_revision"], nid=0, node_generation=9)
    assert exc.value.status_code == 409
    assert _detail(exc)["code"] == "trace_clear_operation_conflict"


def test_a_pending_receipt_defers_to_the_sequencer_instead_of_answering(tmp_path):
    srv, rd = _run(tmp_path)
    path, receipt = _pending(srv, rd)
    assert tc._trace_clear_receipt_result(
        receipt, receipt_path=path, operation_id=receipt["id"],
        expected_generation=receipt["expected_generation"],
        expected_trace_revision=receipt["expected_trace_revision"],
        nid=0, node_generation=0) is None


def test_a_succeeded_receipt_replays_its_exact_counts_and_republishes(tmp_path):
    srv, rd = _run(tmp_path)
    path, receipt = _pending(srv, rd, status="succeeded")
    path.unlink()                                   # prove the republish, not a leftover file
    answer = tc._trace_clear_receipt_result(
        receipt, receipt_path=path, operation_id=receipt["id"],
        expected_generation=receipt["expected_generation"],
        expected_trace_revision=receipt["expected_trace_revision"], nid=0, node_generation=0)
    assert answer == {"ok": True, "status": "succeeded", "operation_id": receipt["id"],
                      "removed": 1, "kept": 2}
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "succeeded"


def test_a_superseded_receipt_stays_closed_and_is_republished_before_it_is_reported(tmp_path):
    srv, rd = _run(tmp_path)
    path, receipt = _pending(srv, rd, status="superseded")
    path.unlink()
    with pytest.raises(HTTPException) as exc:
        tc._trace_clear_receipt_result(
            receipt, receipt_path=path, operation_id=receipt["id"],
            expected_generation=receipt["expected_generation"],
            expected_trace_revision=receipt["expected_trace_revision"], nid=0, node_generation=0)
    assert exc.value.status_code == 409
    assert _detail(exc)["code"] == "trace_clear_operation_superseded"
    assert path.exists(), "a terminal outcome must be durable before it is exposed"


def test_a_completed_receipt_without_a_valid_result_is_unavailable_not_a_zero_clear(tmp_path):
    srv, rd = _run(tmp_path)
    path, receipt = _pending(srv, rd, status="succeeded")
    receipt = {**receipt, "result": {"removed": -1, "kept": 2}}
    with pytest.raises(HTTPException) as exc:
        tc._trace_clear_receipt_result(
            receipt, receipt_path=path, operation_id=receipt["id"],
            expected_generation=receipt["expected_generation"],
            expected_trace_revision=receipt["expected_trace_revision"], nid=0, node_generation=0)
    assert exc.value.status_code == 503


# --------------------------------------------------------------------------- recovery

def test_recovery_applies_a_pending_clear_whose_trace_is_still_the_recorded_source(tmp_path):
    srv, rd = _run(tmp_path)
    path, receipt = _pending(srv, rd)
    answer = tc._apply_prepared_trace_clear(srv, rd, rd / "spans.jsonl", path, receipt)
    assert answer["status"] == "succeeded" and answer["removed"] == 1
    assert (rd / "spans.jsonl").read_text(encoding="utf-8") == (
        '{"span_id": "b", "attributes": {"node_id": 1}}\n'
        '{"span_id": "c", "attributes": {}}\n')
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "succeeded"


def test_recovery_completes_without_deleting_twice_when_the_replacement_already_landed(tmp_path):
    """The crash window this whole machine exists for: the atomic replace committed, the success
    receipt did not. Re-running the filter would be harmless here but is not the point — the
    recorded outcome must be reported, not recomputed."""
    srv, rd = _run(tmp_path)
    path, receipt = _pending(srv, rd)
    (rd / "spans.jsonl").write_text(
        '{"span_id": "b", "attributes": {"node_id": 1}}\n'
        '{"span_id": "c", "attributes": {}}\n', encoding="utf-8")
    answer = tc._apply_prepared_trace_clear(srv, rd, rd / "spans.jsonl", path, receipt)
    assert answer == {"ok": True, "status": "succeeded", "operation_id": receipt["id"],
                      "removed": 1, "kept": 2}


def test_recovery_supersedes_when_the_trace_moved_to_a_third_state(tmp_path):
    """Neither the source nor the result: the original outcome can no longer be reconstructed, so
    the operation closes WITHOUT another deletion."""
    srv, rd = _run(tmp_path)
    path, receipt = _pending(srv, rd)
    (rd / "spans.jsonl").write_text('{"span_id": "z", "attributes": {}}\n', encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        tc._apply_prepared_trace_clear(srv, rd, rd / "spans.jsonl", path, receipt)
    assert _detail(exc)["code"] == "trace_clear_operation_superseded"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["status"] == "superseded"
    assert stored["superseded_reason"] == "trace_changed_after_pending"
    assert (rd / "spans.jsonl").read_text(encoding="utf-8") == '{"span_id": "z", "attributes": {}}\n'


def test_recovery_supersedes_when_the_recomputed_counts_contradict_the_record(tmp_path):
    """A no-op clear has source == result, so BOTH digest comparisons match and the digests alone
    cannot tell a sound record from a corrupt one. Only the recomputed counts can."""
    srv, rd = _run(tmp_path, spans='{"span_id": "b", "attributes": {"node_id": 1}}\n')
    path, receipt = _pending(
        srv, rd, source='{"span_id": "b", "attributes": {"node_id": 1}}\n',
        counts={"removed": 5, "kept": 0})
    receipt = {**receipt, "result": {"removed": 5, "kept": 0}}
    tc._save_trace_clear_receipt(path, receipt)
    with pytest.raises(HTTPException) as exc:
        tc._apply_prepared_trace_clear(srv, rd, rd / "spans.jsonl", path, receipt)
    assert _detail(exc)["code"] == "trace_clear_operation_superseded"
    assert json.loads(path.read_text(encoding="utf-8"))["superseded_reason"] == (
        "prepared_postcondition_changed")


def test_an_unconfirmable_postcondition_keeps_the_operation_pending(tmp_path, monkeypatch):
    """If the read-back cannot prove the replacement landed, the answer is `outcome_unknown` and the
    receipt STAYS pending — a same-id retry then compares hashes instead of deleting again."""
    srv, rd = _run(tmp_path)
    path, receipt = _pending(srv, rd)
    real = tc._trace_digest_snapshot
    reads: list[int] = []

    def _lying_read_back(target: Path):
        reads.append(1)
        # Initial and pre-replace snapshots are honest; only confirmation comes back wrong.
        if len(reads) < 3:
            return real(target)
        return tc._TraceDigestSnapshot(True, "f" * 64, 0)

    monkeypatch.setattr(tc, "_trace_digest_snapshot", _lying_read_back)
    with pytest.raises(HTTPException) as exc:
        tc._apply_prepared_trace_clear(srv, rd, rd / "spans.jsonl", path, receipt)
    assert exc.value.status_code == 503
    assert _detail(exc)["code"] == "trace_clear_outcome_unknown"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "pending"


def test_parent_sync_failure_after_visible_replace_recovers_from_the_pending_wal(
        tmp_path, monkeypatch):
    srv, rd = _run(tmp_path)
    path, receipt = _pending(srv, rd)
    expected, _counts = _filter_bytes(rd, SPANS.encode("utf-8"), {0})

    def _failed_parent_sync(_target):
        raise OSError("simulated directory fsync failure after rename")

    real_parent_sync = tc.strict_fsync_parent
    monkeypatch.setattr(tc, "strict_fsync_parent", _failed_parent_sync)
    with pytest.raises(HTTPException) as exc:
        tc._apply_prepared_trace_clear(srv, rd, rd / "spans.jsonl", path, receipt)
    assert _detail(exc)["code"] == "trace_clear_outcome_unknown"
    assert (rd / "spans.jsonl").read_bytes() == expected
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "pending"

    monkeypatch.setattr(tc, "strict_fsync_parent", real_parent_sync)
    answer = tc._apply_prepared_trace_clear(
        srv, rd, rd / "spans.jsonl", path, receipt)
    assert answer == {"ok": True, "status": "succeeded", "operation_id": receipt["id"],
                      "removed": 1, "kept": 2}
    assert (rd / "spans.jsonl").read_bytes() == expected


def test_completion_retires_the_stale_span_index_only_when_the_trace_changed(tmp_path):
    """`spans.index.jsonl` stores byte offsets into spans.jsonl, so it must die with any rewrite —
    and must NOT be dropped for a clear that removed nothing."""
    srv, rd = _run(tmp_path)
    index = rd / "spans.index.jsonl"
    journal = rd / ".spans-append.jsonl"

    index.write_text("stale\n", encoding="utf-8")
    journal.write_text("stale\n", encoding="utf-8")
    path, receipt = _pending(srv, rd)
    tc._complete_trace_clear(srv, rd, rd / "spans.jsonl", path, receipt)
    assert not index.exists() and not journal.exists() and srv.invalidated == [rd]

    index.write_text("still here\n", encoding="utf-8")
    journal.write_text("still here\n", encoding="utf-8")
    untouched = {**receipt, "result": {"removed": 0, "kept": 3}}
    tc._complete_trace_clear(srv, rd, rd / "spans.jsonl", path, untouched)
    assert index.exists() and journal.exists() and srv.invalidated == [rd]


def test_a_sibling_pending_receipt_for_the_same_lifecycle_is_found_and_directories_are_skipped(
        tmp_path):
    """A pre-upgrade run directory may already occupy the reserved namespace; it is not a receipt."""
    srv, rd = _run(tmp_path)
    tc._trace_clear_receipt_path(srv, rd, "tc_" + "9" * 32).mkdir()
    sibling_path, sibling = _pending(srv, rd, operation="tc_" + "2" * 32)
    mine = tc._trace_clear_receipt_path(srv, rd, "tc_" + "3" * 32)

    found = tc._pending_trace_clear_for_lifecycle(
        srv, rd, receipt_path=mine, expected_generation=sibling["expected_generation"],
        expected_trace_revision=sibling["expected_trace_revision"], nid=0, node_generation=0)
    assert found is not None and found["id"] == sibling["id"]

    # ...and the scan skips the requester's own record rather than reporting itself as a rival.
    assert tc._pending_trace_clear_for_lifecycle(
        srv, rd, receipt_path=sibling_path, expected_generation=sibling["expected_generation"],
        expected_trace_revision=sibling["expected_trace_revision"], nid=0,
        node_generation=0) is None


# --------------------------------------------------------------------------- the entry point

@pytest.mark.parametrize("payload,status", [
    ({}, 428),
    ({"expected_generation": "a" * 64, "expected_trace_revision": "b" * 64,
      "node_generation": 0}, 428),                                       # no operation id
    ({"expected_generation": "nope"}, 400),
    ({"expected_generation": "a" * 64, "node_generation": -1}, 400),
    ({"expected_generation": "a" * 64, "node_generation": "0"}, 400),
    ({"expected_generation": "a" * 64, "expected_trace_revision": "nope"}, 400),
    ({"expected_generation": "a" * 64, "operation_id": "tc_zz"}, 400),
])
def test_the_identity_ladder_answers_before_any_run_directory_is_touched(tmp_path, payload, status):
    srv, _rd = _run(tmp_path)

    def _never(rd, operation):
        raise AssertionError("liveness must not be consulted before identities validate")

    with pytest.raises(HTTPException) as exc:
        tc.durable_clear_node_trace(srv, "demo", 0, payload, known_engine_liveness=_never)
    assert exc.value.status_code == status


def test_a_fresh_clear_refuses_when_the_router_reports_the_engine_live(tmp_path):
    """The verdict stays the router's: an unknown owner is 409 there, and trace-clear must not
    second-guess it into a deletion."""
    srv, rd = _run(tmp_path)
    calls = []

    def _live(rd_arg, operation):
        calls.append(operation)
        return True

    with pytest.raises(HTTPException) as exc:
        tc.durable_clear_node_trace(srv, "demo", 0, {
            "expected_generation": srv.commands.generation,
            "expected_trace_revision": tc.trace_file_revision(rd / "spans.jsonl"),
            "node_generation": 0, "operation_id": "tc_" + "4" * 32,
        }, known_engine_liveness=_live)
    assert exc.value.status_code == 409 and calls == ["clear the node trace"]
    assert (rd / "spans.jsonl").read_text(encoding="utf-8") == SPANS


def test_a_fresh_clear_writes_the_record_before_the_replacement_and_settles_succeeded(tmp_path):
    srv, rd = _run(tmp_path)
    answer = tc.durable_clear_node_trace(srv, "demo", 0, {
        "expected_generation": srv.commands.generation,
        "expected_trace_revision": tc.trace_file_revision(rd / "spans.jsonl"),
        "node_generation": 0, "operation_id": "tc_" + "5" * 32,
    }, known_engine_liveness=lambda rd_arg, operation: False)
    assert answer == {"ok": True, "status": "succeeded", "operation_id": "tc_" + "5" * 32,
                      "removed": 1, "kept": 2}
    assert srv.commands.guarded == ["clear node trace"]
    receipt = json.loads(
        tc._trace_clear_receipt_path(srv, rd, "tc_" + "5" * 32).read_text(encoding="utf-8"))
    assert receipt["status"] == "succeeded" and receipt["source_digest"] == _digest(SPANS)


def test_a_fresh_clear_holds_required_trace_writer_ownership_through_replace_and_invalidate(
        tmp_path, monkeypatch):
    srv, rd = _run(tmp_path)
    real_guard = tc.span_destructive_write_guard
    real_replace = tc._strict_replace_prepared_trace
    real_invalidate = tc._invalidate_trace_clear
    active = False
    calls = []

    @contextmanager
    def _tracked_guard(path, *, required=False):
        nonlocal active
        calls.append((Path(path), required))
        with real_guard(path, required=required):
            active = True
            try:
                yield
            finally:
                active = False

    def _tracked_replace(prepared, destination):
        assert active
        return real_replace(prepared, destination)

    def _tracked_invalidate(server, run_dir, spans_path):
        assert active
        return real_invalidate(server, run_dir, spans_path)

    monkeypatch.setattr(tc, "span_destructive_write_guard", _tracked_guard)
    monkeypatch.setattr(tc, "_strict_replace_prepared_trace", _tracked_replace)
    monkeypatch.setattr(tc, "_invalidate_trace_clear", _tracked_invalidate)
    tc.durable_clear_node_trace(srv, "demo", 0, {
        "expected_generation": srv.commands.generation,
        "expected_trace_revision": tc.trace_file_revision(rd / "spans.jsonl"),
        "node_generation": 0, "operation_id": "tc_" + "d" * 32,
    }, known_engine_liveness=lambda rd_arg, operation: False)

    assert calls == [(rd / "spans.jsonl", True)]


def test_a_stale_run_generation_refuses_before_the_writer_lock(tmp_path):
    srv, rd = _run(tmp_path)
    with pytest.raises(HTTPException) as exc:
        tc.durable_clear_node_trace(srv, "demo", 0, {
            "expected_generation": "c" * 64,
            "expected_trace_revision": tc.trace_file_revision(rd / "spans.jsonl"),
            "node_generation": 0, "operation_id": "tc_" + "6" * 32,
        }, known_engine_liveness=lambda rd_arg, operation: False)
    assert exc.value.status_code == 409 and _detail(exc)["code"] == "run_generation_changed"
    assert (rd / "spans.jsonl").read_text(encoding="utf-8") == SPANS


def test_a_rebuilt_node_refuses_and_leaves_the_trace_alone(tmp_path):
    srv, rd = _run(tmp_path)
    srv._state = _StubState(nodes={0: _StubNode(attempt=3)})
    with pytest.raises(HTTPException) as exc:
        tc.durable_clear_node_trace(srv, "demo", 0, {
            "expected_generation": srv.commands.generation,
            "expected_trace_revision": tc.trace_file_revision(rd / "spans.jsonl"),
            "node_generation": 0, "operation_id": "tc_" + "7" * 32,
        }, known_engine_liveness=lambda rd_arg, operation: False)
    assert _detail(exc)["code"] == "node_generation_changed"
    assert (rd / "spans.jsonl").read_text(encoding="utf-8") == SPANS


def test_an_unserved_resume_refuses_because_an_engine_is_about_to_write_spans(tmp_path):
    srv, rd = _run(tmp_path)
    srv._state = _StubState(resume=True)
    with pytest.raises(HTTPException) as exc:
        tc.durable_clear_node_trace(srv, "demo", 0, {
            "expected_generation": srv.commands.generation,
            "expected_trace_revision": tc.trace_file_revision(rd / "spans.jsonl"),
            "node_generation": 0, "operation_id": "tc_" + "8" * 32,
        }, known_engine_liveness=lambda rd_arg, operation: False)
    assert exc.value.status_code == 409 and "unserved resume" in _detail(exc)["message"]


def test_a_second_operation_over_the_same_lifecycle_is_told_to_verify_the_first(tmp_path):
    """Two ids racing one confirmed lifecycle would each be entitled to delete. The later one gets
    425 naming the ORIGINAL id, so the client verifies that operation instead of paying twice."""
    srv, rd = _run(tmp_path)
    revision = tc.trace_file_revision(rd / "spans.jsonl")
    _path, first = _pending(srv, rd, operation="tc_" + "a" * 32)
    tc._save_trace_clear_receipt(
        tc._trace_clear_receipt_path(srv, rd, "tc_" + "a" * 32),
        {**first, "expected_trace_revision": revision})
    with pytest.raises(HTTPException) as exc:
        tc.durable_clear_node_trace(srv, "demo", 0, {
            "expected_generation": srv.commands.generation,
            "expected_trace_revision": revision,
            "node_generation": 0, "operation_id": "tc_" + "b" * 32,
        }, known_engine_liveness=lambda rd_arg, operation: False)
    assert exc.value.status_code == 425
    assert _detail(exc)["operation_id"] == "tc_" + "a" * 32
    assert (rd / "spans.jsonl").read_text(encoding="utf-8") == SPANS


def test_a_same_id_retry_after_success_replays_instead_of_clearing_again(tmp_path):
    srv, rd = _run(tmp_path)
    payload = {
        "expected_generation": srv.commands.generation,
        "expected_trace_revision": tc.trace_file_revision(rd / "spans.jsonl"),
        "node_generation": 0, "operation_id": "tc_" + "c" * 32,
    }
    first = tc.durable_clear_node_trace(
        srv, "demo", 0, payload, known_engine_liveness=lambda rd_arg, operation: False)
    after = (rd / "spans.jsonl").read_bytes()

    def _never(rd_arg, operation):
        raise AssertionError("a settled operation must answer from its receipt")

    assert tc.durable_clear_node_trace(
        srv, "demo", 0, payload, known_engine_liveness=_never) == first
    assert (rd / "spans.jsonl").read_bytes() == after


def test_a_dead_stagings_temporary_is_reclaimed_before_the_next_one(tmp_path):
    """The staged copy is the size of spans.jsonl, and only an in-process `finally` unlinks it.

    A SIGKILL between staging and publish orphans it permanently: the durable receipt records only
    digests, nothing on disk names the `.tmp`, and the recovery path stages a FRESH full copy rather
    than adopting it — so every interrupted retry added another multi-hundred-MB file to a run
    directory that usually lives on a paid object-store mount, with no sweeper anywhere (the only
    startup GC handles `.lock` files, and the reset artifact manifest never enumerates `.tmp`).

    Both staging sites hold the exclusive writer lock, so a matching temporary present at that moment
    provably belongs to a dead attempt. Driven, not pinned: a real orphan is planted, a real staging
    runs, and the orphan's bytes are gone while the live staging's are not.
    """
    from looplab.serve.trace_clear import _prepare_filtered_trace_snapshot

    spans = tmp_path / "spans.jsonl"
    spans.write_bytes(b"".join(
        json.dumps({"trace_id": "t", "span_id": f"s{i}", "parent_id": None, "name": "n",
                    "attributes": {"node_id": 1 if i else 0}}).encode() + b"\n"
        for i in range(3)))
    orphan = tmp_path / ".spans.jsonl.clear.deadbeef.tmp"
    orphan.write_bytes(b"x" * 4096)

    prepared = _prepare_filtered_trace_snapshot(spans, {1})
    try:
        assert not orphan.exists(), "a dead staging's temporary survived the next staging"
        assert prepared.temporary is not None and prepared.temporary.exists(), (
            "the sweep must not eat the staging it is about to publish")
    finally:
        prepared.cleanup()
