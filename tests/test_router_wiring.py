"""The late-bound router-produced callables and the mount-time check on them (doc 25 XP-05).

Code that cannot import a router still needs what a router computes — a sibling route module (no
router may import a router: `tests/test_serve_module_seams.py`, doc 25 SR-12), or the destructive
and command layers, which must not import one at all. So the producer's `build_router` assigns onto
the shared `AppState` and the consumer reads it back off `srv` at REQUEST time. Drop the producing
router from the mount list and the app still imports, still constructs, still serves every other
route, and then either 500s on ONE endpoint — with a traceback naming the consumer, not the router
that is missing — or, for the three consumers that probe `getattr(srv, "<name>", None)`, skips its
step and says nothing at all.

Two halves here, and the first is the one that matters:

* the BEHAVIOURAL half drives those scenarios. It mounts a real app without the producing router and
  shows the endpoint really does fail at request time when the check is bypassed; it shows
  `mount_routers` refusing at construction when it is not; and it drives the asymmetry between the
  two cost flushes, where the silent branch is the failure. Nothing here is satisfiable by a comment.
* the SOURCE-SCAN half keeps the registry honest in both directions — every `srv.<x> = …` in a
  router is some row's producer, every read of one of those names anywhere in `serve/` is some row's
  consumer. Without it the registry is a list someone has to remember to update, which is the same
  unwritten protocol one indirection further out. Deliberately not keyed on the `_fn` suffix the
  finding describes: that convention covers two of the four rows, and the two it misses are the ones
  whose absence costs money rather than raising.
"""
from __future__ import annotations

import ast

import pytest

from _source_scan import iter_trees

pytest.importorskip("fastapi")

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402

from looplab.serve.router_wiring import (                      # noqa: E402
    LATE_BOUND_ROUTER_CALLABLES, LateBoundCallable, assert_router_wiring, mount_routers,
    router_builders)
from looplab.serve.server import make_app                      # noqa: E402


def _stem(builder) -> str:
    return builder.__module__.rsplit(".", 1)[-1]


def _row(name: str) -> LateBoundCallable:
    return next(r for r in LATE_BOUND_ROUTER_CALLABLES if r.name == name)


# --------------------------------------------------------------------------------------------
# The behaviour: a producing router that is not mounted
# --------------------------------------------------------------------------------------------

def test_a_consumer_router_mounted_without_its_producer_really_does_fail_at_request_time(tmp_path):
    """The premise the check exists for. Bypass `mount_routers` and mount the same routers by hand
    minus the producer: the consumer endpoint is reachable, returns a server error, and the
    AppState attribute it needed is still the `None` the constructor left there.

    Without this test the refusal below is a rule with no cost attached — it could be guarding a
    combination that would have worked fine, and nobody would know."""
    row = _row("list_tasks_fn")
    consumer = row.router_consumers[0]

    app = FastAPI()
    srv = make_app(tmp_path).state.looplab          # a fully-built AppState, then re-wired by hand
    srv.list_tasks_fn = None                        # …as if the producing router had never mounted
    for build in router_builders():
        if _stem(build) != row.producer:
            app.include_router(build(srv))

    assert srv.list_tasks_fn is None, "precondition: the late-bound callable is unset"
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/genesis", json={"instruction": "plan a small run"})
    assert response.status_code >= 500, (
        f"the {consumer} endpoint answered {response.status_code} with its producer router absent — "
        "if this is now survivable the registry row is describing a dependency that is gone")


def test_mount_routers_refuses_an_app_whose_producing_router_is_missing(tmp_path):
    """…and the same deficient mount, through the real entry point, fails at CONSTRUCTION instead —
    where the mount list is right there to fix, and the message names the missing router."""
    row = _row("list_tasks_fn")
    srv = make_app(tmp_path).state.looplab
    srv.list_tasks_fn = None
    deficient = [b for b in router_builders() if _stem(b) != row.producer]

    with pytest.raises(RuntimeError) as excinfo:
        mount_routers(FastAPI(), srv, builders=deficient)

    message = str(excinfo.value)
    assert row.name in message and row.producer in message, message
    assert row.router_consumers[0] in message, message


def test_mount_routers_refuses_a_producer_that_stopped_assigning(tmp_path):
    """The other silent failure: the producing router IS mounted, but its assignment moved behind a
    condition or lost its name in a rename. Every consumer inherits the same request-time 500, and
    the mount list looks correct, so the check cannot key on the router list alone."""
    row = _row("list_tasks_fn")
    srv = make_app(tmp_path).state.looplab
    srv.list_tasks_fn = None                        # mounted, but nothing callable was left behind

    with pytest.raises(RuntimeError) as excinfo:
        assert_router_wiring(srv, router_builders())
    assert "did not assign a callable" in str(excinfo.value)
    assert row.name in str(excinfo.value)


def test_a_row_with_no_consumers_is_still_checked_against_its_producer(tmp_path):
    """`list_runs_fn` has no production reader (doc 25 SR-12 gave the scope reports their own
    method). "Nothing reads it today" is a fact about this revision, not a licence for the
    assignment to rot — so a mounted producer that stops assigning is still a refusal."""
    row = _row("list_runs_fn")
    assert row.router_consumers == () and row.service_consumers == (), (
        "this test is about the empty-consumer branch specifically")

    srv = make_app(tmp_path).state.looplab
    srv.list_runs_fn = None
    with pytest.raises(RuntimeError, match=row.name):
        assert_router_wiring(srv, router_builders())


def test_a_service_consumer_keeps_a_row_live_with_no_router_reading_it(tmp_path):
    """`flush_pending_run_costs` is read only by `serve/` SERVICES — the deletion service, the reset
    route and the command lifecycle. None of them is mounted; all of them are imported. A check that
    asked "is a consuming ROUTER mounted?" would find none and pass the deficient mount."""
    row = _row("flush_pending_run_costs")
    assert row.router_consumers == () and row.service_consumers, row

    srv = make_app(tmp_path).state.looplab
    deficient = [b for b in router_builders() if _stem(b) != row.producer]
    with pytest.raises(RuntimeError) as excinfo:
        assert_router_wiring(srv, deficient)
    assert row.name in str(excinfo.value) and row.producer in str(excinfo.value)


def test_the_two_cost_flushes_really_do_differ_on_absence(tmp_path):
    """The reason both rows exist rather than one summarising "the cost flushes".

    With the attribute gone, the DURABLE flush's consumers fail closed — a reset refuses. The
    PENDING flush's consumers probe with `getattr(..., None)` and skip, so the destructive path runs
    to completion with a paid call still awaiting durable accounting and no signal anywhere. That
    silent branch is the whole reason a mount-time check is worth having here: for this row the
    fallback IS the failure, and nothing downstream reports it."""
    from looplab.serve import reset_route

    srv = make_app(tmp_path).state.looplab
    rd = tmp_path / "demo"
    rd.mkdir(exist_ok=True)

    # Durable: absent -> refuse. Present but reporting "pending" -> also refuse, so the check below
    # is reading absence and not merely a helper that always raises.
    with pytest.raises(Exception) as absent:
        reset_route._flush_reset_cost_evidence(_without(srv, "flush_durable_run_costs"), rd)
    assert getattr(absent.value, "status_code", None) == 503, absent.value

    # Pending: absent -> the pre-sequence hook returns without flushing anything, silently.
    calls: list = []
    srv.flush_pending_run_costs = lambda run_dir: (calls.append(run_dir), True)[1]
    reset_route._flush_retained_paid_activity(srv, rd, None, None)
    assert calls == [rd], "precondition: the hook calls the flush when it is present"
    reset_route._flush_retained_paid_activity(_without(srv, "flush_pending_run_costs"), rd, None, None)
    assert calls == [rd], (
        "the pending-cost hook raised or recorded something when the attribute was absent — if it "
        "now fails closed, this row's `why` is wrong and the asymmetry it documents is gone")


class _without:
    """A read-through view of *srv* with one attribute missing, i.e. its producer never mounted.

    A proxy rather than `delattr`, because the two states are NOT the same and the registry's two
    halves reach them differently. `AppState.__init__` declares `list_*_fn = None`, so those exist
    and are falsy; the two `flush_*_run_costs` are declared NOWHERE — they simply do not exist until
    `boss.build_router` runs, which is how unwritten the protocol was. A probe spelled
    `getattr(srv, name, None)` cannot tell the two apart, and it is the probe under test, so the
    proxy reproduces the genuinely-absent case for either kind."""

    def __init__(self, srv, missing: str):
        self._srv, self._missing = srv, missing

    def __getattr__(self, name):
        if name == self._missing:
            raise AttributeError(name)
        return getattr(self._srv, name)


def test_the_real_app_satisfies_its_own_registry(tmp_path):
    srv = make_app(tmp_path).state.looplab
    for row in LATE_BOUND_ROUTER_CALLABLES:
        assert callable(getattr(srv, row.name, None)), row.name
    assert_router_wiring(srv, router_builders())


def test_dropping_a_row_from_the_registry_is_not_how_a_deficient_mount_passes(tmp_path):
    """A registry is only a guard while it is complete, and the cheapest way to make this file green
    is to delete the row. The source scan below is what stops that — this asserts the two are
    actually coupled, i.e. that the scan reads the SAME constant the check does."""
    srv = make_app(tmp_path).state.looplab
    produced = {row.name for row in LATE_BOUND_ROUTER_CALLABLES}
    assert "list_tasks_fn" in produced and "list_runs_fn" in produced, produced
    assert all(hasattr(srv, name) for name in produced), produced


# --------------------------------------------------------------------------------------------
# The source scan: registry vs what the routers actually do
# --------------------------------------------------------------------------------------------

def _serve_trees():
    """Every `serve/` module, with a flag for whether it defines a top-level `build_router`."""
    for path, tree in iter_trees():
        if path.parent.name == "serve" or path.parent.parent.name == "serve":
            is_router = any(isinstance(n, ast.FunctionDef) and n.name == "build_router"
                            for n in tree.body)
            yield path.stem, is_router, tree


def _srv_assignments(tree) -> set:
    """Every `srv.<name>` ASSIGNED, under any name.

    Deliberately NOT filtered by an `_fn` suffix: two of the four rows are `flush_*_run_costs`, and
    those are the ones whose absence is silent rather than a 500. A scan keyed on a spelling
    convention would have declared the registry complete while missing exactly the edges that cost
    something.
    """
    assigned = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == "srv"):
                assigned.add(target.attr)
    return assigned


def _srv_reads(tree, names: set) -> set:
    """Every read of one of *names* off `srv` / `self.srv`, in either spelling.

    `getattr(srv, "<name>", None)` is a Call with a string literal, not an `ast.Attribute`, and it
    is the spelling ALL THREE service consumers use — a scan that only walked attribute loads would
    see none of them and conclude nothing reads the cost flushes.
    """
    read = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                and node.attr in names and _is_srv(node.value)):
            read.add(node.attr)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in names
                and _is_srv(node.args[0])):
            read.add(node.args[1].value)
    return read


def _is_srv(node) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "srv"
    return isinstance(node, ast.Attribute) and node.attr == "srv"


def test_every_late_bound_assignment_in_a_router_has_a_registry_row():
    actual = {(name, stem) for stem, is_router, tree in _serve_trees() if is_router
              for name in _srv_assignments(tree)}
    declared = {(row.name, row.producer) for row in LATE_BOUND_ROUTER_CALLABLES}
    assert actual == declared, (
        f"routers assign {sorted(actual - declared)} with no registry row, and the registry claims "
        f"{sorted(declared - actual)} that no router assigns")


def test_every_read_of_a_router_produced_name_has_a_registry_row():
    """Both consumer kinds at once, across ALL of `serve/` — the readers of the cost flushes are the
    deletion service, the reset route and the command lifecycle, none of which is a router."""
    produced = {row.name for row in LATE_BOUND_ROUTER_CALLABLES}
    producers = {row.name: row.producer for row in LATE_BOUND_ROUTER_CALLABLES}

    actual = {(name, stem) for stem, _is_router, tree in _serve_trees()
              for name in _srv_reads(tree, produced) if producers[name] != stem}
    declared = {(row.name, consumer) for row in LATE_BOUND_ROUTER_CALLABLES
                for consumer in row.router_consumers + row.service_consumers}
    assert actual == declared, (
        f"{sorted(actual - declared)} read a router-produced name with no registry row, and the "
        f"registry claims {sorted(declared - actual)} that nothing reads")


def test_the_registry_splits_router_consumers_from_service_consumers_correctly():
    """The split decides liveness, so getting it backwards is the whole check silently weakening: a
    service listed as a router consumer is never in `mounted`, so its row never fires."""
    routers = {stem for stem, is_router, _t in _serve_trees() if is_router}
    for row in LATE_BOUND_ROUTER_CALLABLES:
        assert row.producer in routers, (row.name, row.producer, sorted(routers))
        assert set(row.router_consumers) <= routers, (row.name, row.router_consumers)
        assert not set(row.service_consumers) & routers, (row.name, row.service_consumers)


def test_the_registry_names_routers_that_are_actually_mounted():
    """A row naming a module stem that no builder answers to would never fire — the check would
    look at a `mounted` set the name is not in and conclude everything is fine."""
    mounted = {_stem(b) for b in router_builders()}
    for row in LATE_BOUND_ROUTER_CALLABLES:
        assert row.producer in mounted, (row.name, row.producer, sorted(mounted))
        assert set(row.router_consumers) <= mounted, (row.name, row.router_consumers, sorted(mounted))


def test_the_wiring_module_imports_no_router_at_module_level():
    """Its imports stay function-local, as they were inside `make_app`: a router may reach back into
    `looplab.serve.server` for the `make_llm_client` late binding, so importing routers while
    `server` is still executing its module body would close that cycle into an ImportError."""
    tree = next(t for path, t in iter_trees() if path.name == "router_wiring.py")
    modules = {n.module or "" for n in tree.body if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith("looplab.serve") for m in modules), modules


def test_the_server_mounts_through_the_wiring_module():
    """`make_app` must not keep a second, unchecked include loop beside the checked one — that is
    how the guard becomes decorative. On the AST: a commented-out loop is not a Call node."""
    from _source_scan import called_names

    from looplab.serve import server

    assert "mount_routers" in called_names(server.make_app), (
        "make_app no longer mounts through router_wiring.mount_routers, so nothing checks the "
        "late-bound contract at construction")
