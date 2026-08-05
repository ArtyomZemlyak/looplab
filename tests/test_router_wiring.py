"""The late-bound cross-router callables and the mount-time check on them (doc 25 XP-05).

Two route modules need something a SIBLING route module computes, and no router may import a router
(`tests/test_serve_module_seams.py`, doc 25 SR-12). So the producer's `build_router` assigns its
route body onto the shared `AppState` and the consumer reads it back off `srv` at REQUEST time. The
failure that shape invites is the one nothing catches: drop the producing router from the mount
list, and the app still imports, still constructs, still serves every other route, and 500s on ONE
endpoint the first time a client asks for it — with a traceback naming the consumer, not the router
that is missing.

Two halves here, and the first is the one that matters:

* the BEHAVIOURAL half drives that exact scenario. It mounts a real app without the producing
  router, shows the endpoint really does fail at request time when the check is bypassed, and shows
  `mount_routers` refusing at construction when it is not. Nothing here is satisfiable by a comment.
* the SOURCE-SCAN half keeps the registry honest in both directions — every `srv.<x>_fn = …` in a
  router is some row's producer, every `srv.<x>_fn` READ is some row's consumer. Without it the
  registry is a list someone has to remember to update, which is the same unwritten protocol one
  indirection further out.
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
    consumer = row.consumers[0]

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
    assert row.name in message and row.producer in message and row.consumers[0] in message, message


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
    assert row.consumers == (), "this test is about the empty-consumer branch specifically"

    srv = make_app(tmp_path).state.looplab
    srv.list_runs_fn = None
    with pytest.raises(RuntimeError, match=row.name):
        assert_router_wiring(srv, router_builders())


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

def _router_trees():
    """Every module that defines a top-level `build_router` — the routers plus `serve/jobs.py`."""
    for path, tree in iter_trees():
        if any(isinstance(n, ast.FunctionDef) and n.name == "build_router" for n in tree.body):
            yield path.stem, tree


def _srv_fn_accesses(tree):
    """`srv.<name>_fn` split into the ones ASSIGNED and the ones READ.

    On the AST because a substring scan cannot tell an assignment from a read, and telling them
    apart IS the registry's content — a name that flips from produced to consumed is exactly the
    change that must not pass silently.
    """
    assigned, read = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                        and target.value.id == "srv" and target.attr.endswith("_fn")):
                    assigned.add(target.attr)
        elif (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name) and node.value.id == "srv"
                and node.attr.endswith("_fn")):
            read.add(node.attr)
    return assigned, read


def test_every_late_bound_assignment_in_a_router_has_a_registry_row():
    actual = {(name, stem) for stem, tree in _router_trees()
              for name in _srv_fn_accesses(tree)[0]}
    declared = {(row.name, row.producer) for row in LATE_BOUND_ROUTER_CALLABLES}
    assert actual == declared, (
        f"routers assign {sorted(actual - declared)} with no registry row, and the registry claims "
        f"{sorted(declared - actual)} that no router assigns")


def test_every_late_bound_read_in_a_router_has_a_registry_row():
    actual = {(name, stem) for stem, tree in _router_trees()
              for name in _srv_fn_accesses(tree)[1]}
    declared = {(row.name, consumer)
                for row in LATE_BOUND_ROUTER_CALLABLES for consumer in row.consumers}
    assert actual == declared, (
        f"routers read {sorted(actual - declared)} with no registry row, and the registry claims "
        f"{sorted(declared - actual)} that no router reads")


def test_the_registry_names_routers_that_are_actually_mounted():
    """A row naming a module stem that no builder answers to would never fire — the check would
    look at a `mounted` set the name is not in and conclude everything is fine."""
    mounted = {_stem(b) for b in router_builders()}
    for row in LATE_BOUND_ROUTER_CALLABLES:
        assert row.producer in mounted, (row.name, row.producer, sorted(mounted))
        assert set(row.consumers) <= mounted, (row.name, row.consumers, sorted(mounted))


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
