"""The agent-facing node purge keeps a durable receipt, and a crash mid-transaction is RECOVERABLE.

Doc 34 D-01: `_purge_node_snapshot` rewrites the authoritative `events.jsonl` with renumbered `seq`,
replaces `spans.jsonl`, retires two projections and `rmtree`s node workdirs — and it did all of that
inside a bare `try/finally` with an ad-hoc `events.jsonl.bak-del<N>` copy no reader knew about. A
death between the log rewrite and the trace publish left a renumbered log whose `seq` no longer
matched the unfiltered sidecar, with NOTHING on disk saying an operation was in flight, while the
three sibling destructive operations all keep an operation id, a phase and a crash record.

Nothing here is a source pin. Every case drives the real provider over a real run directory, and the
crash cases INJECT the failure at the exact step doc 34 names, then read the receipt back through
its own strict loader — which is what a human recovering the run would do.

The two properties that make the receipt worth having, and they fail for different reasons:

  * the record is TRUE — its phase names what definitely completed, and the backup it names really
    does restore the pre-purge log;
  * the record has TEETH — an unresolved receipt fences every later purge of that run, and so does
    one that cannot be READ, because "there is no operation" and "there is an operation whose record
    I cannot read" must never collapse into each other.
"""
from __future__ import annotations

import json

import pytest

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.tools.node_purge_receipt import (
    NodePurgeReceiptError, PURGE_PHASES, PURGE_RECEIPT_GLOB, load_purge_receipt,
    prepare_purge_receipt, purge_operation_id, purge_receipt_path, save_purge_receipt,
    unresolved_purge_receipts)
from looplab.tools.run_control_tools import RunControlTools, TraceRewriteFns
from tests.test_run_control_tools import _RecordingCommands, _run, _trace_rewrite_fns


class _ProcessDeath(BaseException):
    """Not an `Exception`: the provider's soft-fail handler must not catch this, exactly as it
    cannot catch a `SIGKILL`. The receipt is then whatever the transaction had committed."""


def _spans(rd, node_id: int) -> None:
    """A trace sidecar with one row belonging to *node_id*, so a filtered snapshot is prepared and
    the publish step (the crash point doc 34 names) is actually reached."""
    (rd / "spans.jsonl").write_bytes(json.dumps({
        "trace_id": "t", "span_id": "root", "parent_id": None, "name": "root",
        "kind": "operation", "start": 1.0, "attributes": {"node_id": node_id},
    }, separators=(",", ":")).encode("utf-8") + b"\n")


def _tool(root, *, trace_rewrite=None):
    return RunControlTools(
        root, alive_fn=lambda _rd: False, mode="auto", approver=lambda _action: "allow_once",
        command_service=_RecordingCommands(root),
        trace_rewrite=trace_rewrite if trace_rewrite is not None else _trace_rewrite_fns())


def _receipts(rd) -> list[dict]:
    return [load_purge_receipt(path) for path in sorted(rd.glob(PURGE_RECEIPT_GLOB))]


def _crash_at_publish():
    """The real trace-rewrite primitives, except the publish — the step doc 34 names."""
    real = _trace_rewrite_fns()

    def die(_prepared, _destination):
        raise _ProcessDeath("the process died between the log rewrite and the trace publish")

    return TraceRewriteFns(
        prepare_filtered_snapshot=real.prepare_filtered_snapshot,
        digest_snapshot=real.digest_snapshot,
        publish_prepared_snapshot=die,
    )


# --------------------------------------------------------------- the record a good purge leaves

def test_a_completed_purge_leaves_a_succeeded_receipt_naming_its_own_backup(tmp_path):
    rd = tmp_path / "clean"
    _run(rd, nodes=(0, 1)).append("pause", {})
    _spans(rd, 1)
    before = (rd / "events.jsonl").read_bytes()

    out = _tool(tmp_path).execute("delete_node", {"run_id": rd.name, "node_id": 1, "purge": True})

    assert "deleted node(s) [1]" in out
    receipt, = _receipts(rd)
    assert receipt["phase"] == "succeeded" and receipt["status"] == "succeeded"
    assert receipt["run_id"] == rd.name and receipt["node_id"] == 1
    assert receipt["subtree"] == [1]
    assert receipt["id"] in out, "the operation id must reach the caller, not only the disk"
    # The receipt's whole recovery value is this: the file it names holds the pre-purge bytes.
    assert (rd / receipt["backup"]).read_bytes() == before
    assert not unresolved_purge_receipts(rd), "a succeeded purge fences nothing"


def test_a_succeeded_receipt_does_not_block_the_next_purge(tmp_path):
    """The receipt is a record, not a lock — only an UNRESOLVED one fences."""
    rd = tmp_path / "twice"
    _run(rd, nodes=(0, 1, 2)).append("pause", {})
    tool = _tool(tmp_path)
    assert "deleted node(s) [2]" in tool.execute(
        "delete_node", {"run_id": rd.name, "node_id": 2, "purge": True})
    assert "deleted node(s) [1]" in tool.execute(
        "delete_node", {"run_id": rd.name, "node_id": 1, "purge": True})
    assert len(_receipts(rd)) == 2, "each operation keeps its own record"
    assert all(r["phase"] == "succeeded" for r in _receipts(rd))


def test_a_refused_purge_leaves_no_record_and_fences_nothing(tmp_path):
    """The receipt is published after the last step that can still refuse. A torn log tail is one of
    those refusals — if the receipt were staged earlier, refusing would leave a `prepared` record
    fencing every later purge of a run nothing was ever done to."""
    rd = tmp_path / "refused"
    _run(rd, nodes=(0, 1)).append("pause", {})
    _spans(rd, 1)
    with (rd / "events.jsonl").open("ab") as handle:
        handle.write(b'{"v":1,"seq":99,"type":"node_created"')
    before = (rd / "events.jsonl").read_bytes()

    out = _tool(tmp_path).execute("delete_node", {"run_id": rd.name, "node_id": 1, "purge": True})

    assert "refusing irreversible purge" in out
    assert (rd / "events.jsonl").read_bytes() == before
    assert _receipts(rd) == [], "a refusal must not stage a record that fences the next attempt"


# ------------------------------------------------------------------------- the crash, injected

def test_a_crash_between_the_log_rewrite_and_the_trace_publish_leaves_a_true_record(tmp_path):
    """THE case doc 34 describes, driven. The log is rewritten, the sidecar is not, and before this
    change nothing on disk said so."""
    rd = tmp_path / "crash"
    _run(rd, nodes=(0, 1)).append("pause", {})
    _spans(rd, 1)
    before_log = (rd / "events.jsonl").read_bytes()
    before_spans = (rd / "spans.jsonl").read_bytes()

    with pytest.raises(_ProcessDeath):
        _tool(tmp_path, trace_rewrite=_crash_at_publish()).execute(
            "delete_node", {"run_id": rd.name, "node_id": 1, "purge": True})

    # What the filesystem is actually in: log compacted, sidecar untouched — the two disagree.
    assert (rd / "events.jsonl").read_bytes() != before_log
    assert set(fold(EventStore(rd / "events.jsonl").read_all()).nodes) == {0}
    assert (rd / "spans.jsonl").read_bytes() == before_spans

    # ...and the receipt says exactly that, through its own strict loader.
    receipt, = _receipts(rd)
    assert receipt["phase"] == "log_rewritten", (
        "the phase must name the last step that COMPLETED, never the one that was interrupted")
    assert receipt["status"] == "pending"
    assert (rd / receipt["backup"]).read_bytes() == before_log, (
        "the receipt names the file that undoes the half-applied purge")


def test_the_next_purge_refuses_over_an_unresolved_receipt_and_changes_nothing(tmp_path):
    rd = tmp_path / "fenced"
    _run(rd, nodes=(0, 1, 2)).append("pause", {})
    _spans(rd, 1)
    with pytest.raises(_ProcessDeath):
        _tool(tmp_path, trace_rewrite=_crash_at_publish()).execute(
            "delete_node", {"run_id": rd.name, "node_id": 1, "purge": True})
    after_crash = (rd / "events.jsonl").read_bytes()
    receipt, = _receipts(rd)

    out = _tool(tmp_path).execute("delete_node", {"run_id": rd.name, "node_id": 0, "purge": True})

    assert "unresolved node purge" in out
    assert receipt["id"] in out and "log_rewritten" in out and receipt["backup"] in out, (
        "the refusal must name the operation, where it stopped and what restores it: " + out)
    assert (rd / "events.jsonl").read_bytes() == after_crash, "a fenced purge writes nothing"
    assert len(_receipts(rd)) == 1, "...and stages no second operation"


def test_the_documented_recovery_restores_the_run_and_lifts_the_fence(tmp_path):
    """The recovery `describe_purge_recovery` prescribes, performed: restore from the named backup,
    delete the receipt. Only then does the run accept another purge."""
    rd = tmp_path / "recovered"
    _run(rd, nodes=(0, 1, 2)).append("pause", {})
    _spans(rd, 1)
    before = (rd / "events.jsonl").read_bytes()
    with pytest.raises(_ProcessDeath):
        _tool(tmp_path, trace_rewrite=_crash_at_publish()).execute(
            "delete_node", {"run_id": rd.name, "node_id": 1, "purge": True})
    receipt, = _receipts(rd)

    (rd / "events.jsonl").write_bytes((rd / receipt["backup"]).read_bytes())
    purge_receipt_path(rd, receipt["id"]).unlink()

    assert (rd / "events.jsonl").read_bytes() == before
    assert not unresolved_purge_receipts(rd)
    out = _tool(tmp_path).execute("delete_node", {"run_id": rd.name, "node_id": 1, "purge": True})
    assert "deleted node(s) [1, 2]" in out
    assert set(fold(EventStore(rd / "events.jsonl").read_all()).nodes) == {0}


def test_a_receipt_that_cannot_be_read_fences_the_purge_rather_than_being_skipped(tmp_path):
    """"No operation" and "an operation whose record I cannot read" must never collapse: a crashed
    transaction with a corrupt record would otherwise read as a clean run and be rewritten again."""
    rd = tmp_path / "corrupt"
    _run(rd, nodes=(0, 1)).append("pause", {})
    _spans(rd, 1)
    with pytest.raises(_ProcessDeath):
        _tool(tmp_path, trace_rewrite=_crash_at_publish()).execute(
            "delete_node", {"run_id": rd.name, "node_id": 1, "purge": True})
    path, = sorted(rd.glob(PURGE_RECEIPT_GLOB))
    path.write_text('{"version": 1, "phase": "who knows"}', encoding="utf-8")
    after_crash = (rd / "events.jsonl").read_bytes()

    out = _tool(tmp_path).execute("delete_node", {"run_id": rd.name, "node_id": 0, "purge": True})

    assert "cannot be read" in out and "refusing irreversible purge" in out
    assert (rd / "events.jsonl").read_bytes() == after_crash


# ------------------------------------------------------------------------------ the lattice

def _prepared(tmp_path, operation_id: str) -> tuple:
    rd = tmp_path / "lattice"
    rd.mkdir(exist_ok=True)
    path = purge_receipt_path(rd, operation_id)
    receipt = prepare_purge_receipt(
        rd, operation_id=operation_id, node_id=1, subtree={1, 2},
        expected_generation="a" * 64, expected_seq=7, backup="events.jsonl.bak-del1")
    return path, save_purge_receipt(path, receipt)


def _at(receipt: dict, phase: str) -> dict:
    return {**receipt, "phase": phase,
            "status": "succeeded" if phase == "succeeded" else "pending",
            "updated_at": receipt["updated_at"] + 1}


def test_the_phase_index_advances_one_rung_at_a_time(tmp_path):
    path, receipt = _prepared(tmp_path, purge_operation_id("k"))
    for phase in PURGE_PHASES[1:]:
        receipt = save_purge_receipt(path, _at(receipt, phase))
    assert receipt["phase"] == "succeeded" and receipt["status"] == "succeeded"


@pytest.mark.parametrize("target", ["log_rewritten", "succeeded"])
def test_a_skipped_rung_is_refused(tmp_path, target):
    """Every rung has already changed the filesystem, so a receipt may never claim one it did not
    reach — the phase is the only thing recovery has."""
    path, receipt = _prepared(tmp_path, purge_operation_id(target))
    with pytest.raises(NodePurgeReceiptError, match="invalid node purge receipt transition"):
        save_purge_receipt(path, _at(receipt, target))


def test_a_back_edge_is_refused_and_a_succeeded_receipt_is_terminal(tmp_path):
    path, receipt = _prepared(tmp_path, purge_operation_id("back"))
    receipt = save_purge_receipt(path, _at(receipt, "backed_up"))
    with pytest.raises(NodePurgeReceiptError, match="invalid node purge receipt transition"):
        save_purge_receipt(path, _at(receipt, "prepared"))
    for phase in ("log_rewritten", "trace_published", "workdirs_removed", "succeeded"):
        receipt = save_purge_receipt(path, _at(receipt, phase))
    with pytest.raises(NodePurgeReceiptError, match="terminal"):
        save_purge_receipt(path, _at(receipt, "succeeded"))


def test_the_identity_this_operation_was_approved_against_is_immutable(tmp_path):
    """A second attempt may not re-point recovery at another node, tail, or backup file."""
    path, receipt = _prepared(tmp_path, purge_operation_id("id"))
    # Each value below is SCHEMA-valid on its own — the refusal has to come from the identity
    # comparison, not from the validator, or this case would pass with the immutable set empty.
    for field, value in (("node_id", 2), ("expected_seq", 8), ("backup", "events.jsonl.bak-del9"),
                         ("subtree", [1, 2, 3])):
        with pytest.raises(NodePurgeReceiptError, match="immutable identity changed"):
            save_purge_receipt(path, {**_at(receipt, "backed_up"), field: value})


def test_a_receipt_may_not_be_read_from_a_path_that_names_another_operation(tmp_path):
    path, receipt = _prepared(tmp_path, purge_operation_id("moved"))
    other = path.with_name(path.name.replace(receipt["id"], purge_operation_id("elsewhere")))
    other.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(NodePurgeReceiptError, match="path and identity disagree"):
        load_purge_receipt(other)


def test_the_operation_id_is_the_turn_journal_key_and_is_reconstructible(tmp_path):
    """Doc 34's first question. A recovered turn rebuilds the same journal key, so it rebuilds the
    same id and can recognise its own unresolved operation."""
    assert purge_operation_id("asst_abc") == purge_operation_id("asst_abc")
    assert purge_operation_id("asst_abc") != purge_operation_id("asst_abd")
    assert purge_operation_id("") != purge_operation_id(""), "no journal -> a fresh id each time"
    # ...and never collides with the whole-run deletion id derived from the SAME key.
    from looplab.tools.run_command_adapter import _deletion_operation_id
    assert purge_operation_id("asst_abc") != _deletion_operation_id("asst_abc")
