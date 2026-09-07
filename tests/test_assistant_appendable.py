"""The assistant tool layer's folded appends are a DECLARED seam, and the writes are shared.

Engine invariant #1 names three writers of folded events — the engine's main task, two registried
thread-side seams, and "UI/CLI append only control intents (allow-listed in
`serve/protocol.py::CONTROL_EVENTS`)". `tools/machine_runs_tools.py::MachineRunsTools` is a FOURTH:
it appends `trust_gate_changed` and `node_tombstoned` directly, neither is in `CONTROL_EVENTS`, and
`node_tombstoned` has no other writer in the tree at all. The seam existed, was reachable by an LLM,
and was declared nowhere.

Two properties, and they fail for different reasons:

  * `ASSISTANT_APPENDABLE` is the registry, guarded in both directions — a fifth folded type must
    not be able to join by being appended, and a registered type nothing appends is a decoy that
    reads as covered;
  * the trust-gate WRITE has one implementation. It had two, and the copies had drifted on all
    three of the properties that matter (idempotence, tail CAS, writer lock), with the weaker copy
    the one an assistant drives.

The registry direction is AST over the provider's own `ast.Call` nodes: a commented-out append is
not a call, so a type cannot be registered — or de-registered — by a comment.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from looplab.events.types import ASSISTANT_APPENDABLE, EV_NODE_TOMBSTONED, EV_TRUST_GATE_CHANGED
from looplab.events import types as event_types
from looplab.serve.protocol import CONTROL_EVENTS

PROVIDER = Path(__file__).resolve().parents[1] / "looplab" / "tools" / "machine_runs_tools.py"
SOURCE = PROVIDER.read_text(encoding="utf-8-sig", errors="replace")


def _appended_event_names(source: str) -> set[str]:
    """Every first argument of a `*.append(...)` call in *source*, resolved to its type string.

    The provider spells its types as `EV_*` constants, so the name is resolved through
    `events.types`. A literal string would be a finding in itself — that is what the
    `test_event_types.py` registry exists to stop — so a non-`EV_` first argument is skipped here
    rather than silently counted.
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in {"append", "append_many"}):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name) and first.id.startswith("EV_"):
            value = getattr(event_types, first.id, None)
            if isinstance(value, str):
                found.add(value)
    return found


# A folded append the provider CAUSES but does not spell. `trust_gate_changed` is written through
# the shared `events/trust_gate.py::apply_trust_gate`, which is the point of that module — so a
# scan of the provider's own text now sees one member of the registry and not the other, and a
# registry checked against the text alone would demand the drift back.
#
# FOLLOW THE CALL rather than widening the scan: this is the shape
# `tests/test_offload_lane_writes_no_folded_events.py` was rewritten into after its predecessor
# walked one function's AST and could not see one helper down. The table is itself checked below —
# an entry naming a function the provider never calls is a decoy.
_DELEGATED_WRITERS = {
    "apply_trust_gate": "looplab.events.trust_gate",
}


def _called_names(source: str) -> set[str]:
    return {node.func.id for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}


def _provider_reached_types() -> set[str]:
    """What the assistant seam writes: appended here, plus appended by a writer it delegates to."""
    import importlib

    reached = _appended_event_names(SOURCE)
    called = _called_names(SOURCE)
    for fn, module_name in _DELEGATED_WRITERS.items():
        if fn not in called:
            continue
        module = importlib.import_module(module_name)
        reached |= _appended_event_names(
            Path(module.__file__).read_text(encoding="utf-8-sig", errors="replace"))
    return reached


def _folded_types() -> set[str]:
    """The event types the fold actually handles — a non-folded append is a different seam."""
    from looplab.events.replay import _HANDLERS  # the fold's own dispatch table
    return set(_HANDLERS)


def test_every_folded_type_the_provider_appends_is_registered():
    """MUTATION: append a fifth folded type here -> invariant #1 is widened with no declaration,
    no argument for why the position is safe, and nothing to review."""
    appended_folded = _provider_reached_types() & _folded_types()
    unregistered = appended_folded - set(ASSISTANT_APPENDABLE) - set(CONTROL_EVENTS)
    assert not unregistered, (
        f"{sorted(unregistered)} are folded types this provider appends that are neither an "
        f"allow-listed control intent nor in ASSISTANT_APPENDABLE")


def test_every_registered_type_is_actually_appended_here():
    """The other direction: a registered word nothing emits reads as covered and is not."""
    unemitted = set(ASSISTANT_APPENDABLE) - _provider_reached_types()
    assert not unemitted, f"registered but never appended by the provider: {sorted(unemitted)}"


def test_every_delegated_writer_is_actually_called_by_the_provider():
    """The delegation table must not carry a decoy: an entry for a writer the provider stopped
    calling silently credits the registry with a type nothing here reaches."""
    called = _called_names(SOURCE)
    stale = sorted(fn for fn in _DELEGATED_WRITERS if fn not in called)
    assert not stale, f"delegation table names writers the provider never calls: {stale}"


def test_the_registry_is_disjoint_from_the_control_allow_list():
    """These are NOT control intents and registering them must not read as making them one. A type
    in both would mean two authorization stories for one write."""
    overlap = set(ASSISTANT_APPENDABLE) & set(CONTROL_EVENTS)
    assert not overlap, f"{sorted(overlap)} is both an assistant append and a control intent"


def test_both_registered_types_are_folded():
    """The registry exists BECAUSE these are folded. A diagnostic type belongs in
    `DIAGNOSTIC_EVENTS`, whose whole argument is that the fold never reads it."""
    assert {EV_TRUST_GATE_CHANGED, EV_NODE_TOMBSTONED} <= _folded_types()
    assert set(ASSISTANT_APPENDABLE) <= _folded_types()


# --- the shared write ---------------------------------------------------------------------------

def test_the_trust_gate_write_has_exactly_one_implementation():
    """MUTATION: re-inline the append in either caller -> the two drift again, and the last time
    they did, the assistant's copy had no idempotence check, no tail CAS and no writer lock.

    AST rather than a substring: a commented-out append is not an `ast.Call`.
    """
    from looplab.events import trust_gate

    appenders = []
    for path in (PROVIDER,
                 Path(__file__).resolve().parents[1] / "looplab" / "serve" / "routers" / "runs.py",
                 Path(trust_gate.__file__)):
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in {"append", "append_many"}):
                continue
            if node.args and isinstance(node.args[0], ast.Name) \
                    and node.args[0].id == "EV_TRUST_GATE_CHANGED":
                appenders.append(path.name)
    assert appenders == [Path(trust_gate.__file__).name], (
        f"trust_gate_changed is appended from {appenders}; the write policy lives in "
        f"events/trust_gate.py and every surface delegates to it")


def test_setting_the_gate_to_the_value_it_already_has_records_nothing(tmp_path):
    """DRIVEN, not pinned. The provider used to append unconditionally, so an assistant confirming
    a gate that already held grew the durable log by a row claiming a change nobody made."""
    from looplab.events.eventstore import EventStore
    from looplab.events.trust_gate import (
        GATE_WRITE_ALREADY_SET, GATE_WRITE_APPENDED, apply_trust_gate)

    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})

    assert apply_trust_gate(rd, "block", source="assistant") == GATE_WRITE_APPENDED
    before = len(store.read_all())
    assert apply_trust_gate(rd, "block", source="config_edit") == GATE_WRITE_ALREADY_SET
    assert len(EventStore(rd / "events.jsonl").read_all()) == before


def test_the_write_refuses_a_gate_the_fold_does_not_understand(tmp_path):
    """A caller bug, not an operator refusal: both surfaces validate the operator's own input
    against their own vocabulary before they get here, so this can only be reached by code."""
    from looplab.events.trust_gate import apply_trust_gate
    with pytest.raises(ValueError):
        apply_trust_gate(tmp_path, "off", source="assistant")


def test_the_shared_writer_phrases_no_refusal():
    """`serve/durable_op.py::refuse_unless_quiescent`'s rule: share the probe and its order, never
    the words. A 409 raised here would put the config editor's HTTP vocabulary inside a module the
    assistant also calls."""
    from looplab.events import trust_gate
    source = Path(trust_gate.__file__).read_text(encoding="utf-8-sig", errors="replace")
    assert "HTTPException" not in source
    assert "raise HTTPException" not in source
