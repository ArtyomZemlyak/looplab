"""The extracted Card ledger must keep folding THROUGH the orchestrator's monkeypatch seam
(doc 25 ES-01).

ES-01 asked for the Card reservation/receipt cluster to leave orchestrator.py, and offered
"import fold from the canonical home (as evaluate.py does)" as one way to handle its six `fold`
calls, naming "the two fold-monkeypatching tests" to re-verify. Measured on a throwaway copy with
`_fold` broken into a direct import:

  * FOUR files patch the seam through the orchestrator module, not two — test_continuous_dispatch,
    test_gpu_resources, test_creation_runaway_guard and test_hypothesis_merge.
  * Counted by wrapping `_fold`, only ONE of them reaches this cluster at all, and only once
    (test_creation_runaway_guard, via a real `Engine`). The two dispatch files drive
    `_dispatch_evals` on a stub host that owns none of these methods; test_hypothesis_merge never
    reaches the ledger.
  * All 59 of those tests stay GREEN with the ledger folding through its own import.

So nothing in the suite held this line, and re-verifying the named files would have proved nothing.
The cost of a direct import is not a red test — it is that `monkeypatch.setattr(orch, "fold", …)`
silently stops covering ~1,100 lines of engine, and test_creation_runaway_guard's own stated
reasoning about the Card lane under an empty fold quietly stops being true of what it runs.

This file is the coverage that was missing, and it is DRIVEN rather than pinned: a real run, a real
patched seam, and an assertion about which module the interceptions actually came from.
"""
from __future__ import annotations

import ast
import inspect
import sys

import anyio

import looplab.engine.card_reservation as card_reservation
import looplab.engine.orchestrator as orch
from tests.factories import make_engine


def _watch_the_seam(monkeypatch):
    """Replace `orchestrator.fold` with the real fold plus a caller ledger, and return the ledger.

    Frame 1 is whoever called `orchestrator.fold`; frame 2 is that function's own caller.  Recording
    both is what distinguishes "the ledger folded through the seam" from "some other module did".
    """
    real = orch.fold
    seen: list[tuple[str, str, str]] = []

    def _watched(events):
        caller = sys._getframe(1)
        outer = caller.f_back
        seen.append((caller.f_globals.get("__name__", "?"), caller.f_code.co_name,
                     outer.f_code.co_name if outer is not None else "?"))
        return real(events)

    monkeypatch.setattr(orch, "fold", _watched)
    return seen


def test_a_real_run_folds_the_card_ledger_through_the_orchestrator_seam(tmp_path, monkeypatch):
    """The property itself. A patched `orchestrator.fold` must still see the ledger's folds."""
    seen = _watch_the_seam(monkeypatch)

    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=2, auto_install_deps=False)
    anyio.run(eng.run)

    assert seen, "the run never folded through the orchestrator seam at all"
    from_ledger = [row for row in seen if row[0] == card_reservation.__name__]
    assert from_ledger, (
        "no fold reached the seam from looplab.engine.card_reservation — the ledger is folding "
        "through its own import, so monkeypatching orchestrator.fold no longer intercepts it and "
        "test_creation_runaway_guard / test_hypothesis_merge now measure nothing there. Modules "
        f"that did reach it: {sorted({row[0] for row in seen})}")
    # ...and it arrived via the named shim, not by some accidental re-export.
    assert {row[1] for row in from_ledger} == {"_fold"}, sorted({row[1] for row in from_ledger})
    # The outer frame is a real ledger method, which is what makes this more than a shim self-test.
    assert any(row[2].startswith("_") for row in from_ledger), sorted(from_ledger)


def test_the_ledger_never_binds_fold_directly():
    """The negative half: nothing in the module may BIND the name `fold`.

    CLAUDE.md keeps negative pins as substrings on purpose, and that is the wrong tool here: the
    module docstring quotes `from looplab.events.replay import fold` precisely to say it must not
    come back, so a text pin fails on the explanation rather than on the defect. An AST walk over
    the import nodes says the same thing about the CODE only — and covers the spellings a substring
    pin would miss (`import ... as fold`, `from looplab.events import replay` + `replay.fold`).
    """
    tree = ast.parse(inspect.getsource(card_reservation))
    bound: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                if name in ("fold", "replay"):
                    bound.append(f"line {node.lineno}: {name}")
    assert not bound, f"the ledger binds a fold source directly: {bound}"


def test_every_fold_call_in_the_ledger_goes_through_the_shim():
    """AST, not substrings: a bare `fold(...)` anywhere in the module is the regression."""
    tree = ast.parse(inspect.getsource(card_reservation))
    bare = [n.func.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "fold"]
    assert not bare, f"bare fold() calls at lines {bare} — route them through _fold"
    shimmed = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_fold"]
    assert len(shimmed) == 6, f"expected the cluster's six folds, found {len(shimmed)}"


def test_the_shim_resolves_the_module_attribute_at_call_time(monkeypatch):
    """Directly: an import-time binding would snapshot the real function and ignore the patch."""
    sentinel = object()
    monkeypatch.setattr(orch, "fold", lambda _events: sentinel)
    assert card_reservation._fold([]) is sentinel


# ------------------------------------------------------------------ the move kept its old spellings

def test_the_reservation_record_still_resolves_through_the_orchestrator():
    """`_BuildReservation` moved with the cluster, so orchestrator.py imports it back — the flat
    compat path and the dotted one must both keep naming the SAME object."""
    import looplab.orchestrator as flat

    assert orch._BuildReservation is card_reservation._BuildReservation
    assert flat._BuildReservation is card_reservation._BuildReservation


def test_the_new_module_is_registered_in_the_compat_layout():
    """`_LAYOUT` is keyed by module STEM; an unregistered new module makes the flat path fail to
    resolve, and every monkeypatch through it a silent no-op rather than an error."""
    import looplab.card_reservation as flat

    assert flat is card_reservation


def test_the_engine_still_owns_every_moved_member():
    """A mixin the Engine forgot to inherit fails as an AttributeError deep inside a run."""
    for name in ("_record_dropped_batch_cards", "_node_id_ceiling", "_canonical_card_id",
                 "_card_id_ceiling", "_next_available_card_id", "_plan_native_card",
                 "_reserve_node_build", "_stage_prepared_card", "_stage_card_creates",
                 "_prepare_existing_card_claim", "_claim_existing_card_builds",
                 "_claim_existing_card_build", "_note_card_claim_refusal", "_refuse_card_claim",
                 "_create_stall_diagnosis", "_drop_card_once", "_record_node_less_card",
                 "_mirror_hypothesis_card_merges", "_build_parent_snapshot", "_proposal_cue_fence",
                 "_implementation_ref", "_card_statement", "_card_action", "_card_added_payload",
                 "_card_event_matches", "_card_score_snapshot", "_card_claim_receipt_action",
                 "_fixed_point_idea", "_rebuilt_claim_idea", "_engine_card_number"):
        assert hasattr(orch.Engine, name), name
        # `getattr_static`, because a classmethod hands back a FRESH bound object on every plain
        # `getattr` — an identity check on that compares two wrappers, not two implementations.
        assert (inspect.getattr_static(orch.Engine, name)
                is inspect.getattr_static(card_reservation.CardReservationMixin, name)), name
