"""Guard tests for the central event-type registry (looplab/events/types.py).

Unknown event types are deliberately ignored by `replay.fold` (forward compat), so a typo'd
event name silently no-ops. These tests make that a hard error: every string literal used as
an event type at an emission site (`<...>store.append("...", ...)` / `EventStore(...).append`)
anywhere under looplab/ must be registered in ALL_EVENT_TYPES, and every type string the fold
compares against must be registered too.
"""
from __future__ import annotations

import ast
from pathlib import Path

from looplab.events import types as event_types
from looplab.events.types import ALL_EVENT_TYPES

LOOPLAB = Path(event_types.__file__).resolve().parents[1]   # the looplab/ package dir


def _is_event_store_receiver(node: ast.expr) -> bool:
    """True when `node` (the receiver of an `.append(...)` call) looks like an event store:
    a name/attribute ending in `store` (store, self.store, eng.store) or a direct
    `EventStore(...)` construction. Plain list receivers (lines, out, cands, ...) do NOT
    match, so `lines.append("Strongest:")`-style calls can never false-positive."""
    if isinstance(node, ast.Name):
        return node.id == "store" or node.id.endswith("_store")
    if isinstance(node, ast.Attribute):
        return node.attr == "store" or node.attr.endswith("_store")
    if isinstance(node, ast.Call):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        return name == "EventStore"
    return False


def _emitted_string_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """(lineno, literal) for every `.append` call that can only be an event emission:
    the receiver looks like an event store (see above), OR the call passes more than one
    argument (list.append takes exactly one, so `anything.append("type", {...})` must be
    `EventStore.append(type, data)` — this catches emissions through aliased receivers
    like `es = EventStore(p); es.append(...)` that the name heuristic would miss)."""
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append" and node.args):
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        if (_is_event_store_receiver(node.func.value)
                or len(node.args) >= 2 or node.keywords):
            out.append((node.lineno, first.value))
    return out


def test_registry_is_sane():
    assert ALL_EVENT_TYPES, "registry must not be empty"
    for name in dir(event_types):
        if name.startswith("EV_"):
            value = getattr(event_types, name)
            assert isinstance(value, str)
            assert name == "EV_" + value.upper(), f"{name} does not match its value {value!r}"
            assert value in ALL_EVENT_TYPES
    # every collected value round-trips: nothing but EV_* constants feeds the frozenset
    assert ALL_EVENT_TYPES == frozenset(
        getattr(event_types, n) for n in dir(event_types) if n.startswith("EV_"))


def test_every_emitted_event_type_is_registered():
    """AST-scan all of looplab/ for `store.append("<literal>", ...)` emissions — a typo'd
    event name (even in a file still using raw literals) fails here instead of silently
    no-oping in the fold."""
    unknown = []
    sources_with_appends = 0
    for py in sorted(LOOPLAB.rglob("*.py")):
        # utf-8-sig: at least one source file (adapters/repo_task.py) carries a BOM
        tree = ast.parse(py.read_text(encoding="utf-8-sig"), filename=str(py))
        found = _emitted_string_literals(tree)
        sources_with_appends += bool(found)
        for lineno, literal in found:
            if literal not in ALL_EVENT_TYPES:
                unknown.append(f"{py.relative_to(LOOPLAB.parent)}:{lineno}: {literal!r}")
    assert not unknown, (
        "event emissions with unregistered type strings (typo, or add the constant to "
        "looplab/events/types.py):\n" + "\n".join(unknown))
    # meta-guard: the scanner itself must keep working. The engine currently emits via
    # constants, so finding zero literal emissions repo-wide is legitimate — but verify the
    # scanner still recognizes the canonical patterns so a refactor can't blind this test.
    demo = ast.parse(
        "self.store.append('node_created', {})\n"
        "store.append('typo_event', {})\n"
        "EventStore(p).append('pause', {})\n"
        "lines.append('node_created')\n"        # 1-arg list append: never matched
        "es.append('aliased_emit', {})\n"       # aliased receiver: caught by the 2-arg rule
        "es.append('kw_emit', data={})\n")      # keyword data: caught by the keywords rule
    assert [(1, "node_created"), (2, "typo_event"), (3, "pause"),
            (5, "aliased_emit"), (6, "kw_emit")] == _emitted_string_literals(demo)


def test_every_type_string_in_replay_fold_is_registered():
    """Every string literal compared against an event type in replay.py (`t == ...` /
    `e.type == ...` / membership tests) must be in the registry."""
    src = (LOOPLAB / "events" / "replay.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="replay.py")
    unknown = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        is_type_expr = (isinstance(left, ast.Name) and left.id == "t") or (
            isinstance(left, ast.Attribute) and left.attr == "type")
        if not is_type_expr:
            continue
        for comp in node.comparators:
            literals = []
            if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                literals = [comp.value]
            elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                literals = [e.value for e in comp.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            unknown += [f"replay.py:{node.lineno}: {s!r}" for s in literals
                        if s not in ALL_EVENT_TYPES]
    assert not unknown, "replay.py compares unregistered event types:\n" + "\n".join(unknown)
    # the fold must reference registry constants (sanity: the import is real, not vestigial)
    assert "from looplab.events.types import" in src


def test_control_events_subset_of_registry():
    """The UI's allowed control-event vocabulary must be registered event types."""
    from looplab.serve.server import CONTROL_EVENTS
    assert CONTROL_EVENTS <= ALL_EVENT_TYPES
