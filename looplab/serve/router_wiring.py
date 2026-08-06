"""The router mount ORDER and the registry of late-bound router-produced callables (doc 25 XP-05).

Some code needs something a ROUTER computes. No router may import a router
(`tests/test_serve_module_seams.py` pins that, doc 25 SR-12), and the command/destructive layers
must not import a router at all, so the producer's `build_router` assigns its route body onto the
shared `AppState` bag and the consumer reads it back off `srv` at REQUEST time. That is a
stringly-typed contract between modules that never mention each other, and the failures it invites
are the two nothing catches. Drop the producing router from the mount list, or move its assignment
behind a condition, and:

* a consumer that calls the attribute straight gets `NoneType is not callable` — a 500 on ONE
  endpoint, the first time a client asks, with a traceback naming the consumer rather than the
  router that is missing;
* a consumer that probes `getattr(srv, "<name>", None)` first SILENTLY SKIPS its step. Three of the
  consumers below are that shape, and the step they skip is the non-paid flush of a retained paid
  call's run-cost accounting before an irreversible reset or delete. That failure has no traceback
  at all — the destructive operation succeeds and the accounting is simply not done.

Nothing fails at import and nothing fails at construction. `LATE_BOUND_ROUTER_CALLABLES` makes the
contract enumerable and `assert_router_wiring` turns both into a refusal at mount time, where the
mount list is right there to fix. The registry is the duck-typed-seam pattern the rest of the repo
already uses (CLAUDE.md, "Duck-typed seams are REGISTRY-GUARDED"): a production constant plus a
two-way source scan in `tests/test_router_wiring.py`, so a name assigned onto `srv` in a router, or
read back off `srv` anywhere in `serve/`, without a row here fails the suite instead of joining an
unwritten protocol. The scan is keyed on the produced NAMES and not on a spelling convention — the
`_fn` suffix the finding describes covers only two of the four rows, and the two it misses are the
ones with the money consequence.

The consumers' own `getattr(..., None)` guards are deliberate and stay: each is defensive code at a
site that cannot import the router. What changes is that in a correctly mounted app the fallback is
now unreachable, so it stops being the thing that quietly decides an operator's cost accounting.

Why a registry and not a shared-services object built before any router: every row is a route BODY
or a closure over `srv` built inside `build_router`. `list_tasks` reads the on-disk catalogue
relative to the repo, `list_runs` overlays engine-liveness facts with a best-effort resume re-spawn,
and the two cost flushes are `paid_work` functions partially applied to `srv`. Moving any of them
into the state bag would put HTTP and paid-work concerns there, which is the opposite of what SR-12
decided one day earlier and for the same reason. So the edges stay and get a guard.
"""
from __future__ import annotations

import dataclasses
from typing import Iterable, Optional, Sequence


@dataclasses.dataclass(frozen=True)
class LateBoundCallable:
    """One `srv.<name>` callable a router's `build_router` assigns for other code to read.

    `producer` is a router module STEM (`misc`, `boss`, …) — the last dotted component of the module
    a `build_router` is defined in, which is how `assert_router_wiring` identifies the builders it
    was handed without importing any router itself.

    The consumers are split because their liveness differs, and the split is what makes the check
    correct rather than approximately right. A `router_consumer` only exists when that router is
    mounted; a `service_consumer` is ordinary imported `serve/` code that is ALWAYS live, so its
    producer must be mounted unconditionally. Folding the two into one tuple would let a row whose
    only readers are services pass a mount that omits its producer.
    """

    name: str
    producer: str
    router_consumers: tuple[str, ...]
    service_consumers: tuple[str, ...]
    why: str


# The two-way source scan in `tests/test_router_wiring.py` is what keeps this honest: every
# `srv.<x> = …` in a router must be some row's producer, and every read of one of those names —
# `srv.<x>` or `getattr(srv, "<x>", …)`, anywhere in `serve/` — must be a row's consumer. Adding
# either half without a row here is a red test, not a silent new protocol.
LATE_BOUND_ROUTER_CALLABLES: tuple[LateBoundCallable, ...] = (
    LateBoundCallable(
        name="list_tasks_fn",
        producer="misc",
        router_consumers=("genesis",),
        service_consumers=(),
        why=("the task catalogue `GET /api/tasks` returns; the genesis boss grounds its plan on the "
             "same list, and reads it through the bag so the two routers stay independent leaves"),
    ),
    LateBoundCallable(
        name="list_runs_fn",
        producer="runs",
        router_consumers=(),
        service_consumers=(),
        why=("the runs list WITH its live-fact overlay (an engine-liveness lock probe and a "
             "best-effort resume re-spawn). It has no production consumer: the scope reports read "
             "`AppState.run_membership()` instead, precisely because a report GET must not probe "
             "every run's lock (doc 25 SR-12). The attribute survives as the handle "
             "`tests/test_report.py` uses to assert the runs list still DOES probe — the contrast "
             "that makes that split meaningful — so an empty consumer tuple is the fact, not an "
             "omission, and the scan below fails if a production reader appears without a row."),
    ),
    # The two rows the `_fn` naming convention hides, and the ones whose absence is not a 500.
    LateBoundCallable(
        name="flush_pending_run_costs",
        producer="boss",
        router_consumers=(),
        service_consumers=("deletion_service", "reset_route", "run_commands"),
        why=("the non-paid recovery flush for a retained paid call, offered ONCE before a "
             "destructive operation takes the command sequencer — a successful flush closes the "
             "retained `run_activity` context, whose own cleanup acquires that same sequencer, so "
             "the destructive paths cannot call it from inside their guard and cannot import this "
             "router to reach it. ALL THREE consumers probe with `getattr(..., None)` and skip the "
             "flush when it is absent, so an unmounted boss router does not fail the reset or the "
             "delete — it lets an irreversible mutation proceed with a paid call still waiting for "
             "durable run-cost accounting, and says nothing."),
    ),
    LateBoundCallable(
        name="flush_durable_run_costs",
        producer="boss",
        router_consumers=(),
        service_consumers=("deletion_service", "reset_route"),
        why=("the durable-only twin, invoked a second time while the caller already holds the "
             "command sequencer: it cannot close activity contexts or recursively acquire it. Its "
             "two consumers DO fail closed on absence (503), which is the behaviour its sibling "
             "above lacks — the asymmetry is real, is driven by a test, and is the reason both rows "
             "are here rather than one summarising 'the cost flushes'."),
    ),
)


def router_builders() -> tuple:
    """The `build_router` callables `make_app` mounts, in the order it mounts them.

    Router include ORDER (load-bearing for the overlapping patterns — see routers/__init__.py):
      1. runs      — the runs list + per-run read model (also late-binds srv.list_runs_fn)
      2. attention — owner-only redacted event/liveness projection (observation-only)
      3. collaboration — owner-only bounded comment current/history projections
      4. reviews   — owner link management + the token-scoped reviewer manifest
      5. org       — projects / super-tasks / label / delete-run
      6. control   — /control appends + resume/reset//api/start engine spawns
      7. genesis   — /api/research + /api/genesis (reads srv.list_tasks_fn at request time)
      8. assistant — sessions + the HITL permission registry
      9. boss      — chat-log / chat / suggest / command / report_refresh (also late-binds the two
                     srv.flush_*_run_costs hooks the destructive and command layers call)
     10. jobs      — GET /api/jobs/{id} over the shared JobRegistry
     11. reports   — cross-run scope reports
     12. misc      — settings/secret/tasks/health/gpu, then the generic `GET /api/{kind}`
                     authoring route, which MUST register after every other /api route it would
                     otherwise shadow (and before /api/memory, preserving the original order).
    The static mounts + the SPA catch-all `GET /{path:path}` come after ALL /api routers.

    Note the mount order does NOT satisfy the late-binding contract by itself: genesis (7) mounts
    BEFORE its producer misc (12) and works only because it reads at request time. That is exactly
    why the wiring is checked against the registry rather than argued from this list.

    Imports stay function-local, as they were inside `make_app`: a router module may reach back into
    `looplab.serve.server` for the `make_llm_client` late binding, so importing routers while
    `server` is still executing its own module body would be a cycle.
    """
    from looplab.serve import jobs as _jobs_router
    from looplab.serve.routers import (
        assistant as _assistant_router, attention as _attention_router,
        boss as _boss_router, collaboration as _collaboration_router,
        control as _control_router, cross_run as _cross_run_router,
        genesis as _genesis_router, misc as _misc_router, org as _org_router,
        reports as _reports_router, reviews as _reviews_router, runs as _runs_router)

    return (_runs_router.build_router, _attention_router.build_router,
            _collaboration_router.build_router,
            _reviews_router.build_router, _org_router.build_router,
            _control_router.build_router, _genesis_router.build_router,
            _assistant_router.build_router, _boss_router.build_router,
            _jobs_router.build_router, _reports_router.build_router,
            _cross_run_router.build_router, _misc_router.build_router)


def _module_stem(builder) -> str:
    return (getattr(builder, "__module__", "") or "").rsplit(".", 1)[-1]


def assert_router_wiring(srv, builders: Iterable) -> None:
    """Refuse a mounted app whose late-bound callables are not wired.

    Two failures, both of which are silent until a request arrives:

    * a live CONSUMER without its PRODUCER — the endpoint 500s the first time it is called, with a
      traceback naming the consumer and not the missing router, or (for the three `getattr`-probing
      consumers) skips its step and says nothing at all;
    * a producer router IS mounted but the attribute is not callable — the assignment moved behind
      a condition, or lost its name in a rename, and every consumer inherits the same outcome.

    A `service_consumer` is always live: it is ordinary imported `serve/` code, not something a
    mount list can leave out. A `router_consumer` is live only when that router is mounted.

    A row with NO consumers is still checked for the second failure when its producer is mounted,
    because "nothing reads it today" is a fact about this revision, not a licence for the assignment
    to rot. Raised as a plain `RuntimeError`, deliberately not an `OperatorRefusal`: a mount list
    that does not satisfy its own registry is a defect in this package, not a mistake in something
    the operator typed.
    """
    mounted = {_module_stem(b) for b in builders}
    problems: list[str] = []
    for row in LATE_BOUND_ROUTER_CALLABLES:
        waiting = sorted([c for c in row.router_consumers if c in mounted]
                         + list(row.service_consumers))
        if waiting and row.producer not in mounted:
            problems.append(
                f"srv.{row.name} is read by live consumer(s) {', '.join(waiting)} but its "
                f"producer router '{row.producer}' is not mounted — {row.why}")
            continue
        if row.producer in mounted and not callable(getattr(srv, row.name, None)):
            problems.append(
                f"router '{row.producer}' is mounted but did not assign a callable srv.{row.name} "
                f"— {row.why}")
    if problems:
        raise RuntimeError(
            "late-bound router wiring is incomplete (looplab/serve/router_wiring.py):\n  "
            + "\n  ".join(problems))


def mount_routers(app, srv, builders: Optional[Sequence] = None) -> None:
    """Include every router on *app*, then prove the late-bound contract holds.

    `builders` exists so a test can mount a DEFICIENT app — the only way to drive the check against
    real routers rather than a mock. Production always passes the full `router_builders()`.
    """
    chosen = tuple(router_builders() if builders is None else builders)
    for build in chosen:
        app.include_router(build(srv))
    assert_router_wiring(srv, chosen)
