"""Two serve-layer seams that a grep-and-paste can silently undo (doc 25 SC-04, SR-12).

SC-04 — `command_observation.py` and `log_pages.py` are two incremental indexes over the SAME
append-only file. Their payload grammars differ enough that `_Index`/`_scan` stay per-module, but the
per-path lock registry was copy-pasted BYTE-IDENTICALLY between them, docstring included, and both
constructors re-spelled the same LRU-bound guard. A fix to one copy reached neither the other index
nor the operator waiting behind it, so the shared halves now live in `serve/_log_index.py`.

SR-12 — `routers/genesis.py` imported `_defaults_backend_llm` out of `routers/control.py`, a SIBLING
router's privates, so the route modules were no longer independent leaves. The predicate is launch
policy with no HTTP dependency; it belongs with the launch boundary it has to agree with.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import threading

import pytest

from _source_scan import iter_sources, iter_trees

from looplab.serve import _log_index, command_observation, log_pages
from looplab.serve._log_index import PathLocks, validated_index_bound


# --------------------------------------------------------------------------------------------
# SC-04
# --------------------------------------------------------------------------------------------

def test_neither_index_carries_its_own_copy_of_the_lock_registry():
    """The whole point: one definition. A second `class _PathLocks` anywhere means the copy is back."""
    owners = [path.relative_to(path.parents[2]).as_posix()
              for path, tree in iter_trees()
              for node in tree.body
              if isinstance(node, ast.ClassDef) and node.name in ("PathLocks", "_PathLocks")]
    assert owners == ["looplab/serve/_log_index.py"], owners


@pytest.mark.parametrize("module", [command_observation, log_pages])
def test_both_indexes_use_the_shared_registry_and_bound(module):
    index = module.CommandObservationIndex() if module is command_observation else module.EventLogPager()
    assert isinstance(index._paths, PathLocks)
    assert index.max_indexed_runs == module.MAX_INDEXED_RUNS


@pytest.mark.parametrize("module", [command_observation, log_pages])
@pytest.mark.parametrize("bad", [0, -1, True, False, 1.0, "8", None])
def test_both_indexes_reject_the_same_bad_bounds(module, bad):
    """`bool` is the trap this guard exists for — it is an `int` subclass, so `True` would otherwise
    build a one-entry cache that silently evicts every run but the newest."""
    factory = module.CommandObservationIndex if module is command_observation else module.EventLogPager
    with pytest.raises(ValueError, match="max_indexed_runs must be a positive integer"):
        factory(max_indexed_runs=bad)


def test_both_index_payloads_sit_on_the_shared_cursor():
    """The five byte-cursor fields were declared twice and updated the same way at the end of both
    scans. The PAYLOADS still differ — decoded events with a dense-seq rule vs byte-offset rows with
    a monotonic-seq rule — which is why only the cursor merged."""
    from looplab.serve._log_index import LogIndexCursor

    for module in (command_observation, log_pages):
        assert issubclass(module._Index, LogIndexCursor), module.__name__

    shared = {"identity", "metadata", "revision", "observed_size", "valid_end", "torn_tail"}
    for module in (command_observation, log_pages):
        own = {f.name for f in dataclasses.fields(module._Index)} - shared
        assert own, f"{module.__name__} has no payload of its own left — the merge went too far"
        assert not (own & shared)


def test_the_cursor_records_all_three_scan_outcomes_together():
    """A `valid_end` advanced without its `observed_size` lets the next poll skip the bytes between
    them; a stale `torn_tail` stops the re-scan a writer filling reserved bytes in place depends on.
    They move as one call so a caller cannot update two of the three."""
    from looplab.serve._log_index import LogIndexCursor

    cursor = LogIndexCursor(identity=(1, 2), metadata=(3, 4))
    cursor.note_scanned(120, 200, True)
    assert (cursor.valid_end, cursor.observed_size, cursor.torn_tail) == (120, 200, True)
    cursor.note_scanned(200, 200, False)
    assert (cursor.valid_end, cursor.observed_size, cursor.torn_tail) == (200, 200, False)


def test_minting_a_revision_fails_outstanding_cursors_closed():
    """A rewritten prefix must ROTATE the revision, not extend it — identity, generation and growth
    all survive an in-place rewrite, so without this an old client cursor kept resolving and stitched
    replacement rows into the timeline it had already shown."""
    from looplab.serve._log_index import LogIndexCursor

    cursor = LogIndexCursor(identity=(1, 2), metadata=(3, 4))
    before = cursor.revision
    cursor.mint_revision()
    assert cursor.revision != before and len(cursor.revision) == len(before)


def test_two_fresh_cursors_do_not_share_a_revision():
    """`field(default_factory=...)`, not a class attribute — a shared default would make every index
    in the process answer to the same client cursor."""
    from looplab.serve._log_index import LogIndexCursor

    a = LogIndexCursor(identity=(1, 2), metadata=(3, 4))
    b = LogIndexCursor(identity=(1, 2), metadata=(3, 4))
    assert a.revision != b.revision


def test_the_shared_bound_returns_the_value_it_validated():
    assert validated_index_bound(8) == 8


def test_one_path_gets_one_lock_and_two_paths_do_not_share():
    locks = PathLocks(4)
    held = threading.Event()
    release = threading.Event()
    blocked = threading.Event()

    def _hold():
        with locks.hold("a"):
            held.set()
            release.wait(timeout=10)

    def _contend():
        with locks.hold("a"):
            blocked.set()

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert held.wait(timeout=10)

    waiter = threading.Thread(target=_contend, daemon=True)
    waiter.start()
    assert not blocked.wait(timeout=0.3), "two threads entered the same path's critical section"

    # A DIFFERENT path must not be stalled behind it — that stall is the whole reason this class
    # replaced one process-global lock.
    other = threading.Event()

    def _other():
        with locks.hold("b"):
            other.set()

    threading.Thread(target=_other, daemon=True).start()
    assert other.wait(timeout=10), "an unrelated path queued behind another path's scan"

    release.set()
    assert blocked.wait(timeout=10)
    holder.join(timeout=10)
    waiter.join(timeout=10)


def test_a_held_lock_is_never_evicted_by_the_lru_bound():
    """The LRU bound yields to liveness. Handing a second lock object out for a path whose first is
    still held would let two threads scan and mutate the same `_Index` concurrently — exactly the
    corruption the lock exists to prevent."""
    locks = PathLocks(1)
    held = threading.Event()
    release = threading.Event()

    def _hold():
        with locks.hold("kept"):
            held.set()
            release.wait(timeout=10)

    worker = threading.Thread(target=_hold, daemon=True)
    worker.start()
    assert held.wait(timeout=10)
    try:
        for i in range(5):                       # push the bound hard while "kept" is referenced
            with locks.hold(f"churn{i}"):
                pass
        assert "kept" in locks._locks, "a REFERENCED lock was evicted; a second holder could enter"
    finally:
        release.set()
        worker.join(timeout=10)


def test_the_unreferenced_locks_are_the_ones_that_get_dropped():
    locks = PathLocks(2)
    for i in range(6):
        with locks.hold(f"p{i}"):
            pass
    assert len(locks._locks) <= 2, "the registry grows without bound"


# --------------------------------------------------------------------------------------------
# SR-12
# --------------------------------------------------------------------------------------------

def _router_modules():
    return [(path, tree) for path, tree in iter_trees()
            if path.parent.name == "routers" and path.parent.parent.name == "serve"]


def test_no_router_imports_another_router():
    """Route modules are leaves. A sibling-router import makes them a graph — and the two edges this
    finding named were both domain logic parked in a router only for historical reasons."""
    offenders = []
    for path, tree in _router_modules():
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            names = ([alias.name for alias in node.names]
                     if isinstance(node, ast.Import) else [module or ""])
            for name in names:
                if not name.startswith("looplab.serve.routers."):
                    continue
                sibling = name.split(".")[3]
                if sibling != path.stem:
                    offenders.append(f"{path.name}:{node.lineno} -> {name}")
    assert offenders == [], offenders


def test_the_launch_predicate_lives_with_the_launch_boundary():
    from looplab.serve import launch
    from looplab.serve.routers import control, genesis

    assert genesis._defaults_backend_llm is launch._defaults_backend_llm
    assert inspect.getmodule(launch._defaults_backend_llm) is launch
    assert not hasattr(control, "_defaults_backend_llm"), (
        "a re-export here just re-creates the coupling in the other direction — this router does not "
        "call the predicate")


def test_the_moved_predicate_still_answers_the_generative_kind_rule(tmp_path, monkeypatch):
    """Behaviour, not just placement: a generative kind with nobody choosing a backend defaults to
    llm, and any explicit choice — card, or deployment env — suppresses it. Without this default a
    repo run launched over HTTP got NoOpRepoDeveloper and every node silently re-evaluated the
    unchanged baseline: no error, just a flat run."""
    from looplab.serve.launch import _defaults_backend_llm

    monkeypatch.delenv("LOOPLAB_BACKEND", raising=False)
    generative = {"kind": "repo", "goal": "make it faster", "repo_path": str(tmp_path)}

    assert _defaults_backend_llm(generative, None, {}, {}) is True
    assert _defaults_backend_llm(generative, None, {"backend": "toy"}, {}) is False, "card choice"
    assert _defaults_backend_llm(generative, None, {}, {"backend": "toy"}) is False, "saved UI choice"
    assert _defaults_backend_llm({"kind": "quadratic", "goal": "min x^2"}, None, {}, {}) is False, (
        "an offline-optimizable kind must keep its own default")
    assert _defaults_backend_llm(None, None, {}, {}) is False, "no task at all -> no opinion"


def test_the_predicate_is_named_where_its_readers_are_pointed():
    """The comment trail is the map. A stale pointer at a moved private is how the next reader ends
    up re-adding the router-to-router import this change removed."""
    stale = [f"{path.name}:{n}"
             for path, text in iter_sources()
             for n, line in enumerate(text.splitlines(), 1)
             if "routers/control.py::_defaults_backend_llm" in line
             or "routers.control._defaults_backend_llm" in line]
    assert stale == [], stale

def test_the_scope_report_store_is_not_a_router():
    """`_prior_learnings_index` was the last router-to-router edge. It could not move alone: it reads
    eleven private helpers and three constants of the scope-report STORE, which sat ahead of
    `build_router` in the same file. The store is its own module now, and genesis imports THAT."""
    from looplab.serve import scope_report_store
    from looplab.serve.routers import genesis, reports

    assert genesis._prior_learnings_index is scope_report_store._prior_learnings_index
    assert inspect.getmodule(scope_report_store._prior_learnings_index) is scope_report_store
    assert reports._prior_learnings_index is scope_report_store._prior_learnings_index, (
        "the re-export must keep `reports.<name>` resolving for its own build_router and the tests")


def test_the_store_module_holds_no_http_surface():
    """The reason it could move at all: none of it is HTTP. A route decorator or an APIRouter
    appearing here means the split is being undone from the other side."""
    from looplab.serve import scope_report_store

    source = inspect.getsource(scope_report_store)
    assert "APIRouter" not in source and "@router." not in source


def test_every_star_re_exported_name_is_declared():
    """`routers/reports.py` star-imports the store, and a star import only carries what `__all__`
    lists — a name added without declaring it stops resolving as `reports.<name>` and takes the
    tests that spell it that way with it."""
    from looplab.serve import scope_report_store

    tree = next(t for path, t in iter_trees() if path.name == "scope_report_store.py")
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    defined |= {t.id for n in tree.body if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name) and t.id != "__all__"}
    assert set(scope_report_store.__all__) == defined, (
        set(scope_report_store.__all__) ^ defined)


def test_the_shared_write_seam_is_read_from_both_modules():
    """The hazard this extraction carries: a star import BINDS BY VALUE, so a monkeypatch on one
    module does not reach the other's lookup. `strict_atomic_write_text` is genuinely called from
    both — the store writes receipts and fences, the router writes the report record — so a test
    that injects a write failure has to patch both, which `test_report.py::_patch_store` does."""
    from looplab.serve import scope_report_store
    from looplab.serve.routers import reports

    for module in (scope_report_store, reports):
        source = inspect.getsource(module)
        assert "strict_atomic_write_text(" in source, module.__name__
