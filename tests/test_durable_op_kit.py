"""One receipt PROTOCOL and one quiescence LADDER, two owners each (doc 25 SC-06).

`reset_transaction.py` and `deletion_transaction.py` are two durable receipts with genuinely
different schemas, phase lattices, size budgets and error types that implemented the same paranoid
read-then-verify load and the same validate/immutable/transition/publish/confirm save twice — ~90 of
~575 combined lines, two of the three shared helpers byte-identical after renaming. The shared half
is now `serve/durable_op.py`.

This file exists because the *risk* of that extraction is the opposite of its motivation. Sharing the
machinery is a cleanup; sharing one line too much is a regression that reads like one. So it has two
halves, and the second is the one that matters:

1. the PROTOCOL, proved on BOTH owners — the same refusal, at the same phase, for both receipts;
2. the ASYMMETRIES, proved to still refuse each other's rules. Reset admits a phase back-edge that
   deletion must refuse; deletion has an absorbing `quarantine_ambiguous` that must never resume;
   their size caps differ by three orders of magnitude; their error types must not collapse. Every
   one of these is a behaviour a "generic phase machine" would have quietly normalised away, and
   these transactions DESTROY run data — a flattened rule here silently deletes a run twice or
   silently fails to delete it at all.

Nothing here is a positive substring pin. The receipt cases drive real receipts produced by real
Replay/deletion transactions through the real loaders and savers; the ladder cases drive the probe
sequence and then the three live HTTP refusals; the two structural checks go through the AST, where
comments are not nodes.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from _source_scan import called_names, function_tree  # noqa: E402
from looplab.serve import (  # noqa: E402
    deletion_service, deletion_transaction, durable_op, reset_route, reset_transaction,
    run_commands)
from looplab.serve.durable_op import (  # noqa: E402
    ReceiptProtocol, load_receipt, refuse_unless_quiescent, save_receipt)
from looplab.serve.server import make_app  # noqa: E402
# `_clear_trace` is the one route that reaches `destructive_guard` over HTTP, and it refuses an empty
# body with 428 before the guard is entered at all — so its identity-computing helper is borrowed
# rather than re-spelled, or these cases would collect a 428 and prove nothing.
from tests.test_run_command_service import _clear_trace  # noqa: E402
from tests.test_server import (  # noqa: E402
    _build_run, _delete_run, _make_resumable, _replacement_spawn)


class _Boom(RuntimeError):
    """A throwaway error class, so the kit-level cases prove the PROTOCOL and not an owner."""


def _boom_protocol(max_bytes: int = 4096) -> ReceiptProtocol:
    return ReceiptProtocol(
        label="marker", error_cls=_Boom, max_bytes=max_bytes,
        validate=lambda value, *, path: value, immutable=frozenset(),
        check_transition=lambda current, value: None)


# ------------------------------------------------------------------ two REAL receipts, built once
#
# Built by the real transactions rather than hand-written, because a hand-written receipt is a
# second, unreviewed spelling of the schema — and the schema is exactly what these cases must not
# get to choose. Module-scoped: each is a full engine run.


@pytest.fixture(scope="module")
def real_reset_receipt(tmp_path_factory) -> tuple[str, bytes]:
    """(filename, bytes) of a receipt a completed Replay actually wrote."""
    from looplab.serve.routers import control as control_router

    root = tmp_path_factory.mktemp("reset")
    _build_run(root)
    rd = root / "demo"
    _make_resumable(rd)
    (rd / "spans.jsonl").write_text('{"span":1}\n', encoding="utf-8")
    spawned = _replacement_spawn(rd)
    original = control_router._spawn_engine
    control_router._spawn_engine = spawned
    try:
        app = make_app(root)
        with TestClient(app) as client:
            assert client.post("/api/runs/demo/reset").status_code == 200
    finally:
        control_router._spawn_engine = original
    written = sorted(root.glob(f"{reset_transaction.RESET_RECEIPT_PREFIX}*.json"))
    assert len(written) == 1, written
    return written[0].name, written[0].read_bytes()


@pytest.fixture(scope="module")
def real_deletion_receipt(tmp_path_factory) -> tuple[str, bytes]:
    """(filename, bytes) of a receipt a completed deletion actually wrote."""
    root = tmp_path_factory.mktemp("deletion")
    _build_run(root)
    rd = root / "demo"
    app = make_app(root)
    with TestClient(app) as client:
        assert _delete_run(client, "demo", rd=rd).status_code == 200
    written = sorted(root.glob(f"{deletion_transaction.DELETE_RECEIPT_PREFIX}*.json"))
    assert len(written) == 1, written
    return written[0].name, written[0].read_bytes()


class _Owner:
    """One receipt store's public surface, so a case can be written once and run against both."""

    def __init__(self, module, load, save, error, max_bytes, name, payload):
        self.module, self.load, self.save = module, load, save
        self.error, self.max_bytes = error, max_bytes
        self.name, self.payload = name, payload


@pytest.fixture
def reset_owner(tmp_path, real_reset_receipt) -> _Owner:
    name, raw = real_reset_receipt
    return _Owner(
        reset_transaction, reset_transaction.load_reset_receipt,
        reset_transaction.save_reset_receipt, reset_transaction.ResetReceiptError,
        reset_transaction.RESET_RECEIPT_MAX_BYTES, name, json.loads(raw.decode("utf-8")))


@pytest.fixture
def deletion_owner(tmp_path, real_deletion_receipt) -> _Owner:
    name, raw = real_deletion_receipt
    return _Owner(
        deletion_transaction, deletion_transaction.load_deletion_receipt,
        deletion_transaction.save_deletion_receipt, deletion_transaction.DeletionReceiptError,
        deletion_transaction.DELETE_RECEIPT_MAX_BYTES, name, json.loads(raw.decode("utf-8")))


@pytest.fixture(params=["reset", "deletion"])
def owner(request) -> _Owner:
    return request.getfixturevalue(f"{request.param}_owner")


def _materialise(owner: _Owner, tmp_path: Path, payload: dict | None = None) -> Path:
    """The real receipt, byte-for-byte, in this test's own directory under its own exact name.

    The name is load-bearing — both validators bind the receipt's identity to its filename — so it is
    carried across rather than regenerated. Written directly rather than through `save`, so a case
    can START a receipt at a mid-transaction phase: the real ones are terminal (`succeeded`), and
    every phase rule below is about what may follow a phase that is NOT.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / owner.name
    path.write_text(json.dumps(
        payload if payload is not None else owner.payload,
        sort_keys=True, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    return path


# ------------------------------------------------------- the protocol, proved on BOTH receipts


def test_a_published_receipt_reads_back_exactly(owner, tmp_path):
    path = _materialise(owner, tmp_path)
    assert owner.load(path) == owner.payload


def test_no_receipt_is_none_not_an_error(owner, tmp_path):
    """The one case that must NOT raise. Every resume path distinguishes "no operation" from "an
    operation whose state cannot be read"; collapsing them restarts a destructive transaction from
    scratch over its own unfinished work."""
    assert owner.load(tmp_path / owner.name) is None


def test_an_undecodable_receipt_is_a_storage_error_not_an_absent_one(owner, tmp_path):
    """Fail CLOSED. A receipt that cannot be parsed is an UNKNOWN operation, never a missing one."""
    path = _materialise(owner, tmp_path)
    path.write_bytes(b"{not json")
    with pytest.raises(owner.error, match="cannot be decoded"):
        owner.load(path)


def test_an_extra_key_is_malformed_rather_than_ignored(owner, tmp_path):
    """Both schemas compare the key SET exactly — a tolerated extra key is how a second writer's
    dialect gets silently accepted by a receipt that was meant to be exact."""
    path = _materialise(owner, tmp_path)
    path.write_text(json.dumps({**owner.payload, "extra": 1}), encoding="utf-8")
    with pytest.raises(owner.error, match="malformed"):
        owner.load(path)


def test_a_directory_where_the_receipt_should_be_is_refused(owner, tmp_path):
    """`stat.S_ISREG` — a non-regular path is not a service-owned receipt, and reading one could
    block or succeed with something that is not the receipt at all."""
    (tmp_path / owner.name).mkdir()
    with pytest.raises(owner.error, match="regular service-owned file"):
        owner.load(tmp_path / owner.name)


def test_a_symlinked_receipt_is_refused_even_when_it_points_at_a_valid_one(owner, tmp_path):
    """A planted link is how one operation's receipt is made to answer for another's."""
    real = _materialise(owner, tmp_path / "real")
    link = tmp_path / owner.name
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create symlinks unprivileged")
    with pytest.raises(owner.error, match="regular service-owned file"):
        owner.load(link)


def test_an_immutable_identity_field_cannot_change_under_an_operation(owner, tmp_path):
    """The receipt's identity is what makes a retry idempotent. If `run_id` could be edited, a
    retried destructive operation would apply its committed phases to a DIFFERENT run."""
    path = _materialise(owner, tmp_path)
    with pytest.raises(owner.error, match="immutable identity changed"):
        owner.save(path, {**owner.payload, "run_id": "somewhere-else"})
    assert owner.load(path) == owner.payload, "the refused write must not have landed"


def test_a_saved_receipt_is_confirmed_by_reading_it_back(owner, tmp_path):
    """Idempotent re-save of the terminal phase, and the value returned is the one on DISK."""
    path = _materialise(owner, tmp_path)
    assert owner.save(path, owner.payload) == json.loads(path.read_text(encoding="utf-8"))


# ---- the read protocol itself, driven on the kit so a size/identity rule is proved once, exactly


def test_an_oversized_receipt_is_refused_before_it_is_ever_opened(tmp_path):
    """Two guards, and both matter — so this proves WHICH one fired.

    The pre-read `st_size` check is what keeps a hostile or corrupt receipt from being pulled into
    memory at all; the post-read `len(raw)` check catches a file that GREW between the lstat and the
    read. Asserting only "it raised" would go green with the pre-read guard deleted, since the second
    one still refuses — after doing the very read the first one exists to prevent.
    """
    protocol = _boom_protocol(max_bytes=512)
    path = tmp_path / "receipt.json"
    path.write_text(" " * 600, encoding="utf-8")

    opened: list[str] = []
    real_open = Path.open

    def _open(self, *args, **kwargs):
        if self == path:
            opened.append(str(self))
        return real_open(self, *args, **kwargs)

    Path.open = _open
    try:
        with pytest.raises(_Boom, match="safety limit"):
            load_receipt(path, protocol)
    finally:
        Path.open = real_open
    assert opened == [], "the oversized receipt was opened; the pre-read size guard is gone"


def test_an_oversized_receipt_is_refused_even_when_the_lstat_under_reported_its_size(tmp_path):
    """The POST-read half of the size guard — the time-of-check/time-of-use hole itself.

    `st_size` is not a promise about what the following `read()` returns: the file can grow between
    the two calls, and on an attribute-caching filesystem the stat can simply be stale. Both lstats
    are made to report a small REGULAR file (so the identity comparison agrees too, which is what
    stops the second lstat from catching this) while the bytes on disk are over the cap.

    The payload is a complete JSON document followed by padding on purpose: a truncated prefix would
    be undecodable BY ACCIDENT and the refusal would look the same with the guard deleted.
    """
    protocol = _boom_protocol(max_bytes=512)
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"version": 1}) + " " * 600, encoding="utf-8")

    decoy = tmp_path / "small.json"
    decoy.write_text(json.dumps({"version": 1}), encoding="utf-8")
    real_lstat = Path.lstat
    stale = real_lstat(decoy)
    assert stale.st_size <= 512

    Path.lstat = lambda self: stale if self == path else real_lstat(self)
    try:
        with pytest.raises(_Boom, match="exceeds its safety limit"):
            load_receipt(path, protocol)
    finally:
        Path.lstat = real_lstat


def test_both_size_guards_are_present_and_the_pre_read_one_comes_first():
    """Order, which neither behavioural case can show: the post-read guard refuses the same file, so
    a test that only asserts "it raised" is green with the PRE-read guard deleted — after doing the
    very unbounded read that guard exists to prevent.

    Structural, but through the AST: comments are not AST nodes, so a commented-out guard carrying
    the same text cannot satisfy this.
    """
    tree = function_tree(durable_op.load_receipt)
    compares = [node for node in ast.walk(tree)
                if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.Gt)
                and any(ast.unparse(comparator) == "protocol.max_bytes"
                        for comparator in node.comparators)]
    guarded = [ast.unparse(node.left) for node in sorted(compares, key=lambda n: n.lineno)]
    assert guarded[:2] == ["before.st_size", "len(raw)"], (
        f"the two size guards, pre-read then post-read, are not both there in order: {guarded}")


def test_the_shared_read_refuses_a_receipt_replaced_under_it(tmp_path):
    """The second lstat is the whole point: a receipt replaced mid-read would otherwise be parsed as
    a mix of two operations, and the phase that comes out decides what gets destroyed next."""
    protocol = _boom_protocol()
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")

    real_lstat = Path.lstat
    seen = {"n": 0}

    def _lstat(self):
        seen["n"] += 1
        if seen["n"] == 2:                    # the read-back lstat: replace the file first
            real_lstat(self)
            path.write_text(json.dumps({"version": 1, "changed": True}) + " ", encoding="utf-8")
        return real_lstat(self)

    Path.lstat = _lstat
    try:
        with pytest.raises(_Boom, match="changed while it was being read"):
            load_receipt(path, protocol)
    finally:
        Path.lstat = real_lstat
    assert seen["n"] == 2, "the protocol must lstat again AFTER the read"


def test_a_receipt_that_turned_into_a_symlink_under_the_read_is_named_as_such(tmp_path):
    """The receipt tier's deliberate divergence from `core/fence.py`, which re-stats with a bare
    `lstat` and would report this as "changed". Both fail closed; the receipt keeps the sharper
    message because an operator has to decide what to repair, and "your receipt is now a symlink" and
    "your receipt was rewritten" call for different repairs."""
    protocol = _boom_protocol()
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"version": 2}), encoding="utf-8")

    real_lstat = Path.lstat
    seen = {"n": 0}

    def _lstat(self):
        seen["n"] += 1
        if seen["n"] == 2 and self == path:
            path.unlink()
            try:
                path.symlink_to(target)
            except (OSError, NotImplementedError):
                pytest.skip("this platform cannot create symlinks unprivileged")
        return real_lstat(self)

    Path.lstat = _lstat
    try:
        with pytest.raises(_Boom, match="regular service-owned file"):
            load_receipt(path, protocol)
    finally:
        Path.lstat = real_lstat


def test_a_publication_that_cannot_be_confirmed_is_a_storage_error(tmp_path):
    """A strict write can fail after the replacement became visible. The read-back is the authority
    on every later decision, so a half-published receipt has to be loud rather than a phase nobody
    can advance."""
    with pytest.raises(_Boom, match="could not be confirmed"):
        save_receipt(tmp_path / "receipt.json", {"version": 1}, _boom_protocol(),
                     load=lambda _path: None)


def test_save_reads_the_current_receipt_through_the_owners_own_loader(tmp_path):
    """`load` is threaded in rather than resolved inside the kit, so an owner's module-level loader
    stays a live monkeypatch seam for both the read-current and the read-back."""
    seen: list[str] = []
    real = durable_op.load_receipt

    def _load(path):
        seen.append(str(path))
        return real(path, _boom_protocol())

    save_receipt(tmp_path / "receipt.json", {"version": 1}, _boom_protocol(), load=_load)
    assert len(seen) == 2, "save must read the current receipt AND read its own write back"


# ------------------------------------------- the asymmetries a shared kit must NOT have flattened


def _reset_at(owner: _Owner, phase: str, status: str) -> dict:
    """A reset receipt at *phase*, with the schema's other phase-coupled fields made consistent."""
    receipt = {**owner.payload, "phase": phase, "status": status}
    if phase != "succeeded":
        receipt.pop("new_generation", None)
        receipt["new_generation"] = None
    return receipt


def test_reset_admits_a_phase_back_edge_that_deletion_must_refuse(reset_owner, deletion_owner,
                                                                  tmp_path):
    """THE asymmetry, driven from both sides at once.

    Reset's lattice is an unordered adjacency TABLE: a launch whose outcome turned out uncertain
    legitimately falls back to `archived` and retries the exact spawn. Deletion's is a MONOTONIC
    index: every rung it has crossed has already mutated the filesystem, so going back would re-run a
    quarantine move over a run that is no longer where the receipt thinks it is.

    A shared "generic phase machine" that picked either rule would break the other one silently, and
    both directions are destructive.
    """
    reset_path = _materialise(
        reset_owner, tmp_path / "reset", _reset_at(reset_owner, "popen_returned", "accepted"))
    rolled_back = reset_owner.save(reset_path, _reset_at(reset_owner, "archived", "pending"))
    assert rolled_back["phase"] == "archived", "Replay must be able to retry an uncertain launch"

    deletion_path = _materialise(deletion_owner, tmp_path / "deletion", {
        **deletion_owner.payload, "phase": "quarantined", "status": "pending"})
    with pytest.raises(deletion_owner.error, match="invalid deletion receipt transition"):
        deletion_owner.save(deletion_path, {
            **deletion_owner.payload, "phase": "fenced", "status": "pending"})
    assert deletion_owner.load(deletion_path)["phase"] == "quarantined"


def test_deletion_refuses_a_skipped_rung_and_reset_has_no_such_rule(reset_owner, deletion_owner,
                                                                    tmp_path):
    """Deletion advances by exactly one rung; reset's table is not ordered at all, and `prepared` ->
    `superseded` jumps six positions in the tuple `DELETE_PHASES` happens to be spelled in."""
    deletion_path = _materialise(deletion_owner, tmp_path / "deletion", {
        **deletion_owner.payload, "phase": "fenced", "status": "pending"})
    with pytest.raises(deletion_owner.error, match="invalid deletion receipt transition"):
        deletion_owner.save(deletion_path, {
            **deletion_owner.payload, "phase": "purging", "status": "pending"})

    reset_path = _materialise(
        reset_owner, tmp_path / "reset", _reset_at(reset_owner, "prepared", "pending"))
    jumped = reset_owner.save(reset_path, _reset_at(reset_owner, "superseded", "superseded"))
    assert jumped["phase"] == "superseded"


def test_deletions_ambiguous_quarantine_is_absorbing_and_reset_has_no_equivalent(deletion_owner,
                                                                                 tmp_path):
    """The phase that must NEVER become resumable.

    Windows reported a failed durable move AFTER the destination became visible: the run is on one
    side or the other and only a human can say which. Every later save must refuse, including the
    ones that would look like normal forward progress — resuming from here is what deletes a run
    twice or purges a quarantine that never received it.
    """
    path = _materialise(deletion_owner, tmp_path, {
        **deletion_owner.payload, "phase": "quarantining", "status": "pending"})
    deletion_owner.save(path, {
        **deletion_owner.payload, "phase": "quarantine_ambiguous", "status": "pending"})
    for phase, status in (("quarantined", "pending"), ("purging", "pending"),
                          ("succeeded", "succeeded"), ("prepared", "pending")):
        with pytest.raises(deletion_owner.error, match="requires manual storage recovery"):
            deletion_owner.save(path, {**deletion_owner.payload, "phase": phase, "status": status})
    assert deletion_owner.load(path)["phase"] == "quarantine_ambiguous"

    assert "quarantine_ambiguous" not in reset_transaction.RESET_PHASES, (
        "reset has no ambiguous-move phase; giving it one from a shared vocabulary would invent a "
        "state its own driver never handles")


def test_the_two_owners_keep_their_own_size_caps(reset_owner, deletion_owner, tmp_path):
    """Three orders of magnitude apart, and for a reason: a reset receipt carries the run's whole
    `effective_config`, a deletion receipt carries eleven scalars. One shared constant would either
    let a deletion receipt grow unbounded or refuse every real Replay."""
    assert reset_owner.max_bytes == 64 * 1024 * 1024
    assert deletion_owner.max_bytes == 64 * 1024
    assert reset_transaction._PROTOCOL.max_bytes != deletion_transaction._PROTOCOL.max_bytes

    # ...and the smaller one is really enforced, not merely declared.
    path = _materialise(deletion_owner, tmp_path)
    path.write_text(
        json.dumps(deletion_owner.payload) + " " * (deletion_owner.max_bytes + 16),
        encoding="utf-8")
    with pytest.raises(deletion_owner.error, match="safety limit"):
        deletion_owner.load(path)


def test_the_two_owners_keep_distinct_error_types(reset_owner, deletion_owner, tmp_path):
    """Sharing the protocol must not collapse the vocabulary: `reset_route` and `deletion_service`
    each catch their own type to decide which transaction is in trouble, and a shared base would
    make one route's storage failure look like the other's."""
    assert reset_transaction.ResetReceiptError is not deletion_transaction.DeletionReceiptError
    assert not issubclass(
        reset_transaction.ResetReceiptError, deletion_transaction.DeletionReceiptError)
    assert not issubclass(
        deletion_transaction.DeletionReceiptError, reset_transaction.ResetReceiptError)

    path = _materialise(reset_owner, tmp_path)
    path.write_bytes(b"{")
    with pytest.raises(reset_transaction.ResetReceiptError):
        try:
            reset_owner.load(path)
        except deletion_transaction.DeletionReceiptError:            # pragma: no cover
            pytest.fail("a reset storage failure was caught as a deletion one")


def test_each_owners_messages_still_name_its_own_receipt(reset_owner, deletion_owner, tmp_path):
    """`label` is what keeps every message byte-identical to what each module raised before the
    protocol was shared. A generic "receipt cannot be decoded" tells an operator nothing about which
    of two in-flight destructive operations to repair."""
    for owner, noun in ((reset_owner, "reset receipt"), (deletion_owner, "deletion receipt")):
        path = _materialise(owner, tmp_path / noun.replace(" ", "-"))
        path.write_bytes(b"{")
        with pytest.raises(owner.error) as caught:
            owner.load(path)
        assert str(caught.value).startswith(f"{noun} cannot be decoded")


# --------------------------------------------------------------------- the drift the kit removes


def test_neither_receipt_store_hand_rolls_the_identity_tuple():
    """The concrete drift, in its SECOND location.

    `core/fence.py` (doc 25 CO-01) removed a local six-field `file_identity` lambda from the deletion
    FENCE. The deletion RECEIPT still had the identical lambda — the same defect, surviving the
    commit that fixed the first. A NEGATIVE pin on purpose (CLAUDE.md): what must not come back is
    the TEXT, and a commented-out copy of a re-derivation is as much of a drift risk as a live one.
    """
    for module in (reset_transaction, deletion_transaction):
        source = inspect.getsource(module)
        assert "identity = lambda info:" not in source, f"{module.__name__} re-derived the tuple"
        assert "st_file_attributes" not in source, (
            f"{module.__name__} is re-deriving the stat tuple that atomicio owns")


def test_both_receipt_stores_read_and_write_through_the_shared_kit():
    """If an owner re-inlines the read, every parametrized case above still passes while the two
    copies drift apart again — so the sharing itself is asserted, through the AST."""
    for load, save in ((reset_transaction.load_reset_receipt,
                        reset_transaction.save_reset_receipt),
                       (deletion_transaction.load_deletion_receipt,
                        deletion_transaction.save_deletion_receipt)):
        assert "load_receipt" in called_names(load), f"{load.__name__} re-inlined the read"
        assert "save_receipt" in called_names(save), f"{save.__name__} re-inlined the write"
        for reinlined in ("path.open", "path.lstat", "json.loads", "json.dumps",
                          "strict_atomic_write_text"):
            assert reinlined not in called_names(load), f"{load.__name__} still reads it itself"
            assert reinlined not in called_names(save), f"{save.__name__} still writes it itself"


# ---------------------------------------------------- the quiescence ladder: set, order, vocabulary


class _FakeCommands:
    """Records which probes ran, in order, and answers each one as configured."""

    def __init__(self, *, active=(), spawn=False, finalize=False):
        self.calls: list[str] = []
        self._active, self._spawn, self._finalize = list(active), spawn, finalize

    def _active_command_ids(self, rd):
        self.calls.append("active")
        return list(self._active)

    def _recent_spawn_claim(self, rd):
        self.calls.append("spawn")
        return self._spawn

    def _finalize_incomplete(self, rd):
        self.calls.append("finalize")
        return self._finalize


def _refusals(seen: list[str]):
    return {
        "active_command": lambda active: HTTPException(409, ("active", tuple(active))),
        "engine_start": lambda: HTTPException(409, "spawn"),
        "finalize_incomplete": lambda: HTTPException(409, "finalize"),
    }


@pytest.mark.parametrize("state, expect_probes, expect_detail", [
    (dict(), ["active", "spawn", "finalize"], None),
    (dict(active=["cmd_a", "cmd_b"]), ["active"], ("active", ("cmd_a", "cmd_b"))),
    (dict(spawn=True), ["active", "spawn"], "spawn"),
    (dict(finalize=True), ["active", "spawn", "finalize"], "finalize"),
])
def test_the_ladder_probes_in_one_order_and_stops_at_the_first_refusal(
        state, expect_probes, expect_detail, tmp_path):
    """The whole shared contract: WHICH probes, in WHAT order, short-circuiting at the first.

    Order is not cosmetic. `_active_command_ids` is the fail-CLOSED census — it counts unreadable
    records and planted symlinks as active — so it must run before the cheaper probes rather than
    after, or a run whose command state cannot be read is judged quiescent by whichever probe
    happened to answer first.
    """
    commands = _FakeCommands(**state)
    seen: list[str] = []
    if expect_detail is None:
        refuse_unless_quiescent(commands, tmp_path, **_refusals(seen))
    else:
        with pytest.raises(HTTPException) as caught:
            refuse_unless_quiescent(commands, tmp_path, **_refusals(seen))
        assert caught.value.status_code == 409
        assert caught.value.detail == expect_detail
    assert commands.calls == expect_probes


@pytest.mark.parametrize("missing", ["active_command", "engine_start", "finalize_incomplete"])
def test_every_rung_demands_its_own_refusal_from_every_caller(missing, tmp_path):
    """The reason the builders are REQUIRED keywords rather than a dict with defaults.

    A fourth rung added to this ladder must not be able to reach production half-wired: a caller that
    does not supply a refusal for it fails at the call, loudly, instead of silently skipping the
    check on one destructive path. That is the failure this extraction exists to prevent.
    """
    kwargs = _refusals([])
    kwargs.pop(missing)
    with pytest.raises(TypeError, match=missing):
        refuse_unless_quiescent(_FakeCommands(), tmp_path, **kwargs)


DESTRUCTIVE_CALLERS = (
    run_commands.RunCommandService.destructive_guard,
    reset_route._admit_destructive_reset,
    deletion_service.begin_or_resume_run_deletion,
)


def test_every_destructive_caller_routes_through_the_one_ladder():
    """AST, so a commented-out probe cannot satisfy it: each destructive path calls the shared ladder
    and none of them still runs a probe of its own. A second copy of the checklist is exactly how one
    of them loses a rung."""
    for caller in DESTRUCTIVE_CALLERS:
        called = called_names(caller)
        assert "refuse_unless_quiescent" in called, (
            f"{caller.__qualname__} does not use the shared quiescence ladder")
        for probe in ("_active_command_ids", "_recent_spawn_claim", "_finalize_incomplete"):
            direct = [name for name in called if name.endswith(f".{probe}")]
            assert not direct, (
                f"{caller.__qualname__} still probes {probe} itself: {direct}")


def test_reject_if_active_is_a_different_ladder_and_is_deliberately_left_out():
    """The finding called this "four spellings"; it is three of one ladder plus one of another.

    `reject_if_active` asks a different question — may this LEGACY mutation overtake a durable
    intent? — and answers it with a different probe set: the authoritative `_active_record` rather
    than the fail-closed census, plus `_unresolved_terminal_record`, plus a finalize check that comes
    FIRST and has an `allow_incomplete_finalize` opt-out. Routing it through the shared helper would
    have handed every destructive path that opt-out, which is a way to destroy a run whose terminal
    projections are still being written.
    """
    reject = run_commands.RunCommandService.reject_if_active
    called = called_names(reject)
    assert "refuse_unless_quiescent" not in called, (
        "reject_if_active answers a different question and must keep its own ladder")
    assert "self._active_record" in called and "self._unresolved_terminal_record" in called
    assert "self._active_command_ids" not in called, (
        "reject_if_active reads the authoritative record, not the fail-closed census")
    assert "allow_incomplete_finalize" in inspect.signature(reject).parameters

    # ...and the opt-out really is exclusive to it: no destructive caller may name it.
    for caller in DESTRUCTIVE_CALLERS:
        assert "allow_incomplete_finalize" not in inspect.getsource(caller), (
            f"{caller.__qualname__} must not reach the finalize opt-out")


# ------------------------------------------- the three refusal vocabularies, over real HTTP


def _run_with_an_active_command(tmp_path) -> Path:
    """A finished run whose durable command record is non-terminal — rung one, for real."""
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _make_resumable(rd)
    commands = rd / ".commands"
    commands.mkdir(exist_ok=True)
    (commands / f"cmd_{'a' * 32}.json").write_text(
        json.dumps({"id": f"cmd_{'a' * 32}", "status": "executing"}), encoding="utf-8")
    return rd


def test_each_destructive_route_keeps_its_own_refusal_body(tmp_path):
    """The half a shared helper is most likely to normalise away, driven end to end.

    All three refuse the SAME condition at the SAME rung and answer with three deliberately
    different bodies — a plain sentence naming the operation, a `_detail` envelope the UI branches
    on, and Replay's own wording. These are live HTTP contracts; a shared refusal builder would have
    silently rewritten two of them.
    """
    rd = _run_with_an_active_command(tmp_path)
    (rd / "spans.jsonl").write_text(
        '{"attributes":{"node_id":0},"name":"target"}\n', encoding="utf-8")
    app = make_app(tmp_path)
    with TestClient(app) as client:
        replay = client.post("/api/runs/demo/reset")
        deletion = _delete_run(client, "demo", rd=rd)
        trace = _clear_trace(client, rd)

    assert replay.status_code == 409
    assert "cannot reset run: active command(s)" in str(replay.json()["detail"])

    assert deletion.status_code == 409
    detail = deletion.json()["detail"]
    assert detail["code"] == "run_command_active"
    assert detail["active_command_ids"] == [f"cmd_{'a' * 32}"]

    # `destructive_guard`'s own wording, reached through the one route that takes it.
    assert trace.status_code == 409
    assert "active command(s)" in str(trace.json()["detail"])
    assert "clear node trace" in str(trace.json()["detail"]), (
        "destructive_guard's refusal names the caller's operation; the other two do not")


def test_a_pending_engine_start_refuses_all_three_with_their_own_second_rung(tmp_path):
    """Rung two, end to end. Pinned as the fail-closed SET rather than one exact string: whether the
    census or the spawn claim answers first is a race on a real run, and the property is that every
    destructive route refuses — not which sentence it chose."""
    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _make_resumable(rd)
    (rd / "spans.jsonl").write_text(
        '{"attributes":{"node_id":0},"name":"target"}\n', encoding="utf-8")
    app = make_app(tmp_path)
    srv = app.state.looplab
    srv.commands.record_external_spawn(rd, "start", os.getpid())
    with TestClient(app) as client:
        replay = client.post("/api/runs/demo/reset")
        deletion = _delete_run(client, "demo", rd=rd)
        trace = _clear_trace(client, rd)

    assert replay.status_code == 409
    assert any(reason in str(replay.json()["detail"])
               for reason in ("engine start is unresolved", "active command(s)"))
    assert deletion.status_code == 409
    assert deletion.json()["detail"]["code"] in {"engine_launching", "run_command_active"}
    assert trace.status_code == 409
    assert any(reason in str(trace.json()["detail"])
               for reason in ("engine start is still in progress", "active command(s)"))
