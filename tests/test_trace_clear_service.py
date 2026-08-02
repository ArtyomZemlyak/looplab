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


def _pending(srv: _StubSrv, rd: Path, *, nid: int = 0, source: str = SPANS,
             result: str | None = None, counts: dict | None = None,
             operation: str = "tc_" + "1" * 32, status: str = "pending") -> tuple[Path, dict]:
    """Write-ahead record for a clear that a previous process never finished."""
    if result is None:
        result_bytes, computed = tc._filtered_trace_snapshot(source.encode("utf-8"), nid)
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
    result, counts = tc._filtered_trace_snapshot(SPANS.encode("utf-8"), 0)
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
    result, counts = tc._filtered_trace_snapshot(source.encode("utf-8"), 0)
    assert counts == {"removed": 2, "kept": 0}
    assert result.decode("utf-8") == (
        '\n'
        '{not json at all\n'
        '   \n'
        '[1, 2, 3]\n'
        '{"span_id": "torn", "attributes": {"node_id": 0}')


def test_filter_matches_node_id_as_text_so_a_string_row_is_not_orphaned(tmp_path):
    source = '{"span_id": "a", "attributes": {"node_id": "7"}}\n'
    result, counts = tc._filtered_trace_snapshot(source.encode("utf-8"), 7)
    assert counts == {"removed": 1, "kept": 0} and result == b""


# --------------------------------------------------------------------------- trace snapshots

def test_snapshot_of_a_missing_trace_is_a_known_empty_not_a_failure(tmp_path):
    _srv, rd = _run(tmp_path, spans=None)
    exists, digest, data = tc._trace_content_snapshot(rd / "spans.jsonl")
    assert exists is False and data == b"" and digest == _digest("")


def test_snapshot_refuses_a_symlinked_trace(tmp_path):
    """spans.jsonl is rewritten in place; following a link would let it retarget the destructive
    write at a file the run does not own."""
    _srv, rd = _run(tmp_path)
    target = rd / "elsewhere.jsonl"
    target.write_text(SPANS, encoding="utf-8")
    (rd / "spans.jsonl").unlink()
    (rd / "spans.jsonl").symlink_to(target)
    with pytest.raises(HTTPException) as exc:
        tc._trace_content_snapshot(rd / "spans.jsonl")
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
        tc._trace_clear_regular_receipt(link)
    assert _detail(exc)["code"] == "trace_clear_receipt_path_invalid"


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
    real = tc._trace_content_snapshot
    reads: list[int] = []

    def _lying_read_back(target: Path):
        reads.append(1)
        # The pre-write snapshot is honest; only the confirmation read comes back wrong.
        return real(target) if len(reads) == 1 else (True, "f" * 64, b"")

    monkeypatch.setattr(tc, "_trace_content_snapshot", _lying_read_back)
    with pytest.raises(HTTPException) as exc:
        tc._apply_prepared_trace_clear(srv, rd, rd / "spans.jsonl", path, receipt)
    assert exc.value.status_code == 503
    assert _detail(exc)["code"] == "trace_clear_outcome_unknown"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "pending"


def test_completion_retires_the_stale_span_index_only_when_the_trace_changed(tmp_path):
    """`spans.index.jsonl` stores byte offsets into spans.jsonl, so it must die with any rewrite —
    and must NOT be dropped for a clear that removed nothing."""
    srv, rd = _run(tmp_path)
    index = rd / "spans.index.jsonl"

    index.write_text("stale\n", encoding="utf-8")
    path, receipt = _pending(srv, rd)
    tc._complete_trace_clear(srv, rd, rd / "spans.jsonl", path, receipt)
    assert not index.exists() and srv.invalidated == [rd]

    index.write_text("still here\n", encoding="utf-8")
    untouched = {**receipt, "result": {"removed": 0, "kept": 3}}
    tc._complete_trace_clear(srv, rd, rd / "spans.jsonl", path, untouched)
    assert index.exists() and srv.invalidated == [rd]


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
