"""The paid scope-report ACTION protocol, driven WITHOUT an ASGI app (doc 25 SR-02).

The point of SR-02 was never the line count. It was that a crash-recovery state machine over
receipts, immutable lease markers, a per-scope fence and two OS byte-range locks lived as closures
inside `build_router`, so every branch of it was reachable only by building the whole app and
driving HTTP. `serve/scope_actions.py` makes it callable; this file is the instrument that proves
the states.

Each test seeds a real on-disk ledger through the STORE's own writers, then calls the service with a
stub `srv` that has exactly two attributes — `reports_dir` and `jobs`. No app, no engine, no run.
The states below are the ones HTTP cannot construct on demand: a claim whose worker died between
the receipt and the terminal, a terminal that is visible while its lock is still held, and an
abandon racing a live provider call.

Also here: the monkeypatch-seam guard. Both extractions (SR-12's store, SR-02's action protocol)
bind store names BY VALUE, so a seam now exists in three modules and a test that patches two of them
passes while injecting nothing. `test_report.py::_STORE_PATCH_MODULE_PATHS` is the sweep;
`test_the_store_patch_sweep_names_every_module_that_binds_a_seam` fails if a fourth reader appears.
"""
from __future__ import annotations

import ast
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from looplab.serve import scope_actions, scope_report_store as store
from looplab.serve.routers import reports as reports_router
from tests._source_scan import PKG, iter_trees
from tests.test_report import _STORE_PATCH_MODULE_PATHS

SCOPE_TYPE = "project"
SCOPE_ID = "family/alpha"


class _StubJobs:
    """The three JobRegistry calls the action protocol makes, and nothing else."""

    def __init__(self):
        self.discarded: list[str] = []
        self.consumable: list[str] = []
        self.polled: list[str] = []

    def discard_orphaned_running(self, job_id):
        self.discarded.append(job_id)

    def mark_consumable(self, job_id):
        self.consumable.append(job_id)

    def poll(self, job_id):
        self.polled.append(job_id)
        return None


def _srv(tmp_path: Path):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(reports_dir=reports_dir, jobs=_StubJobs())


def _action_id() -> str:
    return str(uuid.uuid4())


def _receipt(action_id: str, status: str, result) -> dict:
    return {
        "schema": store._SCOPE_ACTION_SCHEMA,
        "scope_identity": store._scope_identity(SCOPE_TYPE, SCOPE_ID),
        "action_id": action_id,
        "generation_identity": "scope-report:" + "a" * 64,
        "job_id": "0" * 15 + "1",
        "status": status,
        "updated_at": int(time.time() * 1000),
        "result": result,
    }


def _seed_claim(srv, action_id: str, *, status: str = "running", result=None) -> dict:
    """Write the exact durable state a claiming worker leaves behind, using the store's writers."""
    rd = srv.reports_dir
    with store._scope_store_lock(rd):
        store._ensure_lease_marker(
            store._scope_action_lease_path(rd, action_id),
            store._action_lease_marker(SCOPE_TYPE, SCOPE_ID, action_id))
        store._ensure_lease_marker(
            store._scope_action_scope_lease_path(rd, SCOPE_TYPE, SCOPE_ID),
            store._scope_lease_marker(SCOPE_TYPE, SCOPE_ID))
        receipt = store._write_scope_action_receipt(
            rd, SCOPE_TYPE, SCOPE_ID, _receipt(action_id, status, result))
        store._write_scope_action_fence(rd, SCOPE_TYPE, SCOPE_ID, action_id, "active")
    return receipt


@pytest.fixture
def held_leases():
    """Release anything a test acquired, so the module-global liveness registry stays clean."""
    acquired: list = []
    yield acquired
    for lease in reversed(acquired):
        if lease is not None:
            lease.release()


# ----------------------------------------------------------------- the state machine, no app

def test_a_running_claim_whose_worker_died_becomes_a_durable_indeterminate_terminal(tmp_path):
    """The worker process is gone: no lock is held, but the receipt still says running. The only
    honest terminal is indeterminate — the provider may or may not have been billed."""
    srv = _srv(tmp_path)
    action_id = _action_id()
    _seed_claim(srv, action_id)

    with store._scope_store_lock(srv.reports_dir):
        receipt = scope_actions.read_reconciled_action(
            srv, SCOPE_TYPE, SCOPE_ID, action_id)

    assert receipt["status"] == "indeterminate"
    assert receipt["result"] == store._scope_action_failure(
        store._SCOPE_ACTION_INDETERMINATE, action_id)
    # The terminal is DURABLE, not a computed view: a second reader sees it on disk.
    on_disk = store._read_scope_action_receipt(
        srv.reports_dir, SCOPE_TYPE, SCOPE_ID, action_id)
    assert on_disk["status"] == "indeterminate"
    assert srv.jobs.discarded == [receipt["job_id"]]


def test_a_live_action_lease_keeps_a_running_claim_running(tmp_path, held_leases):
    """The OS lease is the cross-process liveness authority. A sibling ASGI worker cannot see this
    process's JobRegistry, so orphaning on volatile state alone would abandon live paid work."""
    srv = _srv(tmp_path)
    action_id = _action_id()
    seeded = _seed_claim(srv, action_id)
    with store._scope_store_lock(srv.reports_dir):
        held_leases.append(store._acquire_scope_action_lease(
            srv.reports_dir, SCOPE_TYPE, SCOPE_ID, action_id))
    assert held_leases[0] is not None

    with store._scope_store_lock(srv.reports_dir):
        receipt = scope_actions.read_reconciled_action(
            srv, SCOPE_TYPE, SCOPE_ID, action_id)

    assert receipt["status"] == "running"
    assert receipt == seeded          # untouched on disk
    assert srv.jobs.discarded == []   # and never orphaned


def test_a_visible_done_terminal_reads_as_running_until_the_lease_is_released(
        tmp_path, held_leases):
    """The worker publishes its terminal BEFORE releasing its leases, so a terminal seen while the
    lock is still held is mid-hand-off. Disclosing it early would let a second caller act on a
    result whose publication has not finished."""
    srv = _srv(tmp_path)
    action_id = _action_id()
    _seed_claim(srv, action_id, status="done",
                result=store._scope_action_success(action_id))
    with store._scope_store_lock(srv.reports_dir):
        held_leases.append(store._acquire_scope_action_lease(
            srv.reports_dir, SCOPE_TYPE, SCOPE_ID, action_id))
    assert held_leases[0] is not None

    with store._scope_store_lock(srv.reports_dir):
        during = scope_actions.read_reconciled_action(
            srv, SCOPE_TYPE, SCOPE_ID, action_id)
    assert during["status"] == "running"
    assert during["result"] is None

    held_leases.pop().release()
    with store._scope_store_lock(srv.reports_dir):
        after = scope_actions.read_reconciled_action(
            srv, SCOPE_TYPE, SCOPE_ID, action_id)
    assert after["status"] == "done"
    assert after["result"] == store._scope_action_success(action_id)


def test_active_scope_action_clears_its_fence_once_the_claim_is_terminal(tmp_path):
    srv = _srv(tmp_path)
    action_id = _action_id()
    _seed_claim(srv, action_id, status="done",
                result=store._scope_action_success(action_id))

    with store._scope_store_lock(srv.reports_dir):
        active = scope_actions.active_scope_action(srv, SCOPE_TYPE, SCOPE_ID)

    assert active is None
    fence = store._read_scope_action_fence(srv.reports_dir, SCOPE_TYPE, SCOPE_ID)
    assert fence["status"] == "clear" and fence["action_id"] == action_id


def test_abandon_refuses_while_the_provider_call_is_live(tmp_path, held_leases):
    """Abandon is the one operation that reopens a scope for new billing. It must lose every race
    against a worker that may still be spending money."""
    srv = _srv(tmp_path)
    action_id = _action_id()
    _seed_claim(srv, action_id)
    with store._scope_store_lock(srv.reports_dir):
        held_leases.append(store._acquire_scope_action_lease(
            srv.reports_dir, SCOPE_TYPE, SCOPE_ID, action_id))
        held_leases.append(store._acquire_scope_action_scope_lease(
            srv.reports_dir, SCOPE_TYPE, SCOPE_ID))
    assert all(lease is not None for lease in held_leases)

    with pytest.raises(Exception) as excinfo:
        scope_actions.durable_abandon_scope_action(
            srv, SCOPE_TYPE, SCOPE_ID, action_id)

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == store._SCOPE_ACTION_ACTIVE["code"]
    fence = store._read_scope_action_fence(srv.reports_dir, SCOPE_TYPE, SCOPE_ID)
    assert fence["status"] == "active"   # the scope stays closed to new paid work


def test_abandon_writes_a_tombstone_and_clears_the_fence(tmp_path):
    srv = _srv(tmp_path)
    action_id = _action_id()
    _seed_claim(srv, action_id)

    response = scope_actions.durable_abandon_scope_action(
        srv, SCOPE_TYPE, SCOPE_ID, action_id)

    assert response["status"] == "abandoned"
    assert response["action_id"] == action_id
    on_disk = store._read_scope_action_receipt(
        srv.reports_dir, SCOPE_TYPE, SCOPE_ID, action_id)
    # The identity is NOT erased: the UUID stays a permanent tombstone so a replay of the old
    # request can never be re-billed.
    assert on_disk["status"] == "abandoned"
    assert store._read_scope_action_fence(
        srv.reports_dir, SCOPE_TYPE, SCOPE_ID)["status"] == "clear"


def test_abandon_of_a_server_unknown_uuid_mints_no_durable_files(tmp_path):
    """An unknown UUID owns no durable action and needs no tombstone. Minting one would let
    arbitrary abandon calls grow permanent root-level marker/receipt files without any paid work."""
    srv = _srv(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))

    response = scope_actions.durable_abandon_scope_action(
        srv, SCOPE_TYPE, SCOPE_ID, _action_id())

    assert response["status"] == "unknown"
    after = sorted(p.name for p in tmp_path.rglob("*"))
    # `_scope_store_lock` legitimately creates its own lock file; nothing scope- or action-bound may
    # appear.
    assert [n for n in after if n not in before and "scope-action" in n] == []


def test_a_receipt_belonging_to_another_scope_is_a_conflict_not_a_terminal(tmp_path):
    """Marker/receipt identity is what stops one tab's UUID from consuming another scope's claim."""
    srv = _srv(tmp_path)
    action_id = _action_id()
    _seed_claim(srv, action_id)

    with pytest.raises(store._ScopeReportActionConflict):
        with store._scope_store_lock(srv.reports_dir):
            scope_actions.read_reconciled_action(
                srv, "task", "some/other/scope", action_id)


# ----------------------------------------------------------------- the seams the move creates

STORE_MODULE = "looplab.serve.scope_report_store"


def _modules_binding_store_names() -> dict[str, set[str]]:
    """Every module that imports a name FROM the store — i.e. holds a by-value COPY of it.

    `from X import y` and `from X import *` both bind the object, not the lookup, so each importer
    is an independent patch site. That is the rule, and it is decidable from the import statement
    alone; enumerating which names happen to be patched today is not, because the next test to
    inject a failure picks a name nobody listed.
    """
    found: dict[str, set[str]] = {}
    for path, tree in iter_trees():
        dotted = "looplab." + ".".join(path.relative_to(PKG).with_suffix("").parts)
        if dotted == STORE_MODULE:
            continue
        bound = {alias.name for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module == STORE_MODULE
                 for alias in node.names}
        if bound:
            found[dotted] = bound
    return found


def test_the_store_patch_sweep_names_every_module_that_binds_a_store_name():
    """The failure mode both extractions carry: an import BINDS BY VALUE, so a seam patched on two
    of its three modules silently injects nothing on the third. SR-02 made `scope_actions.py` the
    ONLY reader of `_read_scope_action_lease_marker` outside the store — a two-module sweep would
    have left `test_report.py`'s marker-loss recovery test green while injecting nothing at all."""
    importers = _modules_binding_store_names()
    assert importers, "the AST scan found no store importers at all — the scan itself is broken"
    missing = {name: sorted(bound) for name, bound in importers.items()
               if name not in _STORE_PATCH_MODULE_PATHS}
    assert not missing, (
        f"these modules bind a scope-report store name but "
        f"`test_report.py::_STORE_PATCH_MODULE_PATHS` does not sweep them, so patching the store "
        f"leaves their copy stale and injects nothing here: {missing}")
    assert "looplab.serve.scope_actions" in importers, (
        "the action protocol stopped importing from the store — re-derive this guard rather than "
        "leaving one that cannot fail")
    assert "_read_scope_action_lease_marker" in importers["looplab.serve.scope_actions"], (
        "the marker seam left `scope_actions` — check which module `test_report.py`'s marker-loss "
        "recovery test now has to patch")


def test_the_router_and_the_service_resolve_to_the_same_objects():
    """`routers/reports.py` imports the protocol by name and star-imports the store. Both bind by
    value, so the only way to know a patch site is the real one is to assert identity."""
    for name in ("action_response", "active_scope_action", "durable_abandon_scope_action",
                 "get_scope_action", "indeterminate_receipt", "read_reconciled_action"):
        assert getattr(reports_router, name) is getattr(scope_actions, name), name
    for name in ("_read_scope_action_lease_marker", "_write_scope_action_receipt",
                 "_read_scope_action_fence", "_scope_action_lease_is_live", "_scope_store_lock"):
        assert getattr(reports_router, name) is getattr(store, name), name
        assert getattr(scope_actions, name) is getattr(store, name), name


def test_the_two_action_routes_are_one_delegating_call_each():
    """What SR-02 asked for: the router shrinks to endpoint wiring. Asserted over the AST, because a
    positive source pin is satisfied by a comment carrying the same literal."""
    tree = next(t for path, t in iter_trees() if path.name == "reports.py"
                and "routers" in path.parts)
    routes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for name, callee in (("get_scope_report_action", "get_scope_action"),
                         ("abandon_scope_report_action", "durable_abandon_scope_action")):
        body = [s for s in routes[name].body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        assert len(body) == 1, f"{name} kept storage case analysis: {len(body)} statements"
        assert isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Call)
        assert body[0].value.func.id == callee
        assert [a.id for a in body[0].value.args][0] == "srv"


def test_the_action_protocol_module_defines_no_route():
    """Same bar the store module is held to: nothing HTTP-SERVING moves down here. Raising
    `HTTPException` is not serving — `deletion_service.py` and `trace_clear.py` both do it — but an
    `APIRouter` or a route decorator would make this a second router."""
    path, tree = next((p, t) for p, t in iter_trees() if p.name == "scope_actions.py")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    assert "APIRouter" not in text
    decorators = {ast.dump(d) for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) for d in n.decorator_list}
    assert not decorators, decorators
