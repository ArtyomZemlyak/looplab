"""The control-event registry is complete by construction (doc 25 SC-02).

`normalize_control` is the boundary between an HTTP request and an append to the event log. Its
per-event behaviour used to live in three flat if/elif chains — intake normalization, the
append-time precondition recheck, and the engine decision — none of which the two registries
(`CONTROL_SPECS`, `CONTROL_DATA_FIELDS`) knew about. So a new control event could ship having been
added to only some of them, and two branches carried a SECOND hand-maintained copy of a field
allow-list that had already drifted from the first.

What these tests hold is the completeness itself: five per-event tables, each asserted against
`CONTROL_EVENTS` at import, so a control event with a missing handler makes the module refuse to
import rather than quietly inheriting another event's rule. The two collapsed allow-lists are
pinned BEHAVIOURALLY, because the interesting half of `inject_node`'s is that the two lists must
NOT be the same list.
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402

from _source_scan import function_tree  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.events.types import (  # noqa: E402
    EV_BUDGET_EXTEND, EV_CARD_EDITED, EV_INJECT_NODE, EV_PAUSE)
from looplab.serve import run_commands  # noqa: E402
from looplab.serve.protocol import COLLABORATION_EVENTS, CONTROL_EVENTS  # noqa: E402
from looplab.serve.run_commands import (  # noqa: E402
    CONTROL_DATA_FIELDS, CONTROL_SPECS, RunCommandService, normalize_control)

FAKE = "fake_control"

# The five per-event tables, in the order the module asserts them, each with the entry text a new
# event would have to add and the words its own assertion message uses.
TABLES = [
    ("CONTROL_DATA_FIELDS: dict[str, frozenset[str]] = {", f'    "{FAKE}": frozenset(),',
     "data allowlist"),
    ("_CONTROL_NORMALIZERS: dict[str, Optional[Callable]] = {", f'    "{FAKE}": None,',
     "intake normalizer"),
    ("_CONTROL_PRECONDITIONS: dict[str, Optional[Callable]] = {", f'    "{FAKE}": None,',
     "append-time precondition"),
    ("_CONTROL_DECISIONS: dict[str, Optional[Callable]] = {", f'    "{FAKE}": None,',
     "engine decision"),
    ("_CONTROL_POLICIES: dict[str, tuple[EnginePolicy, str]] = {",
     f'    "{FAKE}": (EnginePolicy.NO_SPAWN, "folded_intent"),', "explicit ControlSpec"),
]


def _seed(root: Path, run_id: str = "run") -> Path:
    rd = root / run_id
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": run_id, "task_id": "task", "goal": "g",
                                 "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "base"}, "code": "print(0)"})
    store.append("card_added", {
        "id": "card-1", "statement": "seed", "source": "researcher",
        "idea": {"operator": "draft", "params": {}, "space": {}}})
    return rd


class _Srv:
    def __init__(self, root: Path):
        self.root = root
        self.owner_auth_enabled = False

    def run_dir(self, run_id: str) -> Path:
        return self.root / str(run_id)

    def state(self, rd: Path):
        return fold(EventStore(Path(rd) / "events.jsonl").read_all())


def _exec_source(source: str):
    """Execute a variant of run_commands.py as a fresh module, returning it or raising."""
    name = "run_commands_registry_probe"
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = run_commands.__file__
    # `@dataclass` resolves this module's postponed annotations through `sys.modules[__module__]`,
    # so the probe has to be registered while it executes.
    sys.modules[name] = module
    try:
        exec(compile(source, run_commands.__file__, "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


def _add_entry(source: str, declaration: str, entry: str) -> str:
    assert source.count(declaration) == 1, f"table anchor {declaration!r} is not unique"
    index = source.index(declaration) + len(declaration)
    return source[:index] + "\n" + entry + source[index:]


def _drop_entry(source: str, declaration: str, key: str) -> str:
    """Delete one event's row from ONE table, leaving every other table intact."""
    assert source.count(declaration) == 1, f"table anchor {declaration!r} is not unique"
    start = source.index(declaration)
    end = source.index("\n}\n", start)
    row = f"\n    {key}:"
    body = source[start:end]
    assert body.count(row) == 1, f"row {key!r} appears {body.count(row)} times in this table"
    row_start = start + body.index(row)
    row_end = source.index("\n", row_start + 1)
    return source[:row_start] + source[row_end:]


# ---------------------------------------------------------------- the registry is complete

def test_every_control_event_declares_all_five_behaviours():
    """Key-set completeness, and that `CONTROL_SPECS` really joins the tables rather than
    re-deriving them: a spec pointing at the wrong event's handler is the mismatch this shape
    exists to prevent."""
    assert set(CONTROL_SPECS) == set(CONTROL_EVENTS)
    for event_type, spec in CONTROL_SPECS.items():
        assert spec.event_type == event_type
        assert spec.data_fields is CONTROL_DATA_FIELDS[event_type]
        assert spec.normalize is run_commands._CONTROL_NORMALIZERS[event_type]
        assert spec.precondition is run_commands._CONTROL_PRECONDITIONS[event_type]
        assert spec.decide is run_commands._CONTROL_DECISIONS[event_type]


@pytest.mark.parametrize("declaration,_entry,words", TABLES)
def test_dropping_one_row_from_any_table_refuses_the_import(declaration, _entry, words):
    """Each table's assertion has to bite on its OWN table. A single shared assertion, or one
    written against the wrong table, would let three of these mutations through."""
    source = inspect.getsource(run_commands)
    broken = _drop_entry(source, declaration, "EV_DEEP_RESEARCH")
    assert broken != source
    with pytest.raises(AssertionError) as failure:
        _exec_source(broken)
    assert words in str(failure.value), (
        f"a row missing from {declaration.split(':')[0]} was caught by the wrong assertion")


def test_a_new_control_event_cannot_ship_with_a_missing_handler(monkeypatch):
    """The property doc 25 SC-02 exists for, driven end to end.

    Widen `CONTROL_EVENTS` by one type — exactly what adding a control event does — and satisfy the
    tables ONE at a time. Every stage must still refuse the import, naming the next table that has
    not made a choice, until all five are answered."""
    import looplab.serve.protocol as protocol

    monkeypatch.setattr(protocol, "CONTROL_EVENTS", frozenset({*CONTROL_EVENTS, FAKE}))
    source = inspect.getsource(run_commands)
    for declaration, entry, words in TABLES:
        with pytest.raises(AssertionError) as failure:
            _exec_source(source)
        assert words in str(failure.value), (
            f"expected the {declaration.split(':')[0]} assertion to be the next one to fire")
        source = _add_entry(source, declaration, entry)

    module = _exec_source(source)          # all five answered: the module imports
    spec = module.CONTROL_SPECS[FAKE]
    assert spec.event_type == FAKE and spec.data_fields == frozenset()
    assert spec.normalize is None and spec.precondition is None and spec.decide is None


def test_a_precondition_for_a_non_collaboration_event_is_refused(monkeypatch):
    """The cross-check, which is what makes the `None`s in the precondition table honest: only a
    COLLABORATION event may declare an append-time recheck, because only those reach the strict-lock
    append path that runs one."""
    import looplab.serve.protocol as protocol

    monkeypatch.setattr(protocol, "CONTROL_EVENTS", frozenset({*CONTROL_EVENTS, FAKE}))
    source = inspect.getsource(run_commands)
    for declaration, entry, _words in TABLES:
        if declaration.startswith("_CONTROL_PRECONDITIONS"):
            entry = f'    "{FAKE}": _precondition_card,'
        source = _add_entry(source, declaration, entry)
    with pytest.raises(AssertionError) as failure:
        _exec_source(source)
    assert "collaboration events take the append-time recheck path" in str(failure.value)


def test_only_collaboration_events_carry_a_precondition_and_the_rest_fail_closed(tmp_path):
    """The old chain's final `else` was the COMMENT recheck, so an event type it did not name was
    silently rechecked against a comment id it does not have — and, for a payload that happened to
    look like a comment revision, PASSED. A missing handler is now a refusal."""
    declared = {event for event, handler in run_commands._CONTROL_PRECONDITIONS.items()
                if handler is not None}
    assert declared == set(COLLABORATION_EVENTS)

    state = _Srv(tmp_path).state(_seed(tmp_path))
    for event_type in sorted(set(CONTROL_EVENTS) - set(COLLABORATION_EVENTS)) + ["not_an_event"]:
        error = RunCommandService._collaboration_precondition(state, event_type, {})
        assert error is not None and error["code"] == "collaboration_precondition_missing", (
            f"{event_type} must not borrow another event's append-time recheck")
    # The real collaboration path is untouched: an existing Card still passes its own recheck.
    assert RunCommandService._collaboration_precondition(
        state, EV_CARD_EDITED, {"id": "card-1", "statement": "x"}) is None


# ---------------------------------------------------------------- the collapsed allow-lists

def test_inject_node_refuses_the_import_fields_it_did_not_consume(tmp_path):
    """`inject_node`'s residual allow-list is `data_fields` MINUS the two cross-run import fields,
    and that subtraction is the security rule, not a leftover.

    The import block pops `source_run`/`source_node` only when `source_run` is TRUTHY and
    `source_node` is present. Every other combination reaches the residual check with the fields
    still set, and must be refused — using `data_fields` itself accepts them and writes both into
    the durable event, which is what a naive collapse of the two lists does."""
    rd = _seed(tmp_path)
    srv = _Srv(tmp_path)
    idea = {"operator": "manual", "params": {}, "rationale": "r"}

    assert run_commands._INJECT_IMPORT_FIELDS <= CONTROL_DATA_FIELDS[EV_INJECT_NODE]
    for unconsumed in ({"source_run": "", "source_node": 0},
                       {"source_run": None, "source_node": 0},
                       {"source_run": "somewhere", "source_node": None},
                       {"source_run": "somewhere"},
                       {"source_node": 0}):
        with pytest.raises(HTTPException) as refusal:
            normalize_control(srv, rd, EV_INJECT_NODE, {"idea": idea, **unconsumed})
        assert refusal.value.status_code == 400
        assert "unknown field" in str(refusal.value.detail), unconsumed

    # A payload the import DOES consume still works, and neither field survives into the event.
    donor = _seed(tmp_path, "donor")
    EventStore(donor / "events.jsonl").append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "donor"}, "code": "print(1)"})
    imported = normalize_control(srv, rd, EV_INJECT_NODE,
                                 {"source_run": "donor", "source_node": 0})
    assert run_commands._INJECT_IMPORT_FIELDS.isdisjoint(imported)
    assert imported["origin"] == {"run_id": "donor", "node_id": 0, "metric": None,
                                  "source_attempt": 0}


def test_every_registered_budget_field_satisfies_the_budget_check(tmp_path):
    """The `budget_extend` allow-list is spelled ONCE. It used to be a verbatim second copy a few
    hundred lines from the registry, so a field added to only one of them was accepted by the
    payload allow-list and then refused as 'needs at least one budget field'."""
    rd = _seed(tmp_path)
    srv = _Srv(tmp_path)
    values = {"add_nodes": 5, "max_seconds": 30, "max_eval_seconds": 30, "timeout": 30,
              "eval_parallel": 2, "llm_parallel": 2, "max_parallel": 2, "parallel_build": 2}
    assert set(values) == set(CONTROL_DATA_FIELDS[EV_BUDGET_EXTEND]), (
        "this test must cover exactly the registered budget fields")
    for field, value in values.items():
        assert normalize_control(srv, rd, EV_BUDGET_EXTEND, {field: value})[field] is not None
    with pytest.raises(HTTPException) as empty:
        normalize_control(srv, rd, EV_BUDGET_EXTEND, {})
    assert "at least one budget field" in str(empty.value.detail)
    # The width fields keep their own per-field UPPER BOUNDS. That second tuple is a bounds table,
    # not a second allow-list, and collapsing the allow-list must not have taken a bound with it.
    for field, over in (("eval_parallel", 1025), ("max_parallel", 1025),
                        ("llm_parallel", 65), ("parallel_build", 65)):
        with pytest.raises(HTTPException) as out_of_range:
            normalize_control(srv, rd, EV_BUDGET_EXTEND, {field: over})
        assert "must be between 0 and" in str(out_of_range.value.detail), field


def test_neither_allowlist_is_spelled_a_second_time():
    """NEGATIVE pins: what must not come back is the TEXT of a second copy. A commented-out
    re-derivation is as much of a drift risk as a live one, so these stay substring checks."""
    source = inspect.getsource(run_commands)
    assert "allowed_inject = {" not in source, "inject_node's allow-list is re-declared"
    assert '("add_nodes", "max_seconds"' not in source, "budget_extend's tuple copy came back"
    assert '"parent_generations", "code", "files", "deleted", "origin",\n        }' not in source, (
        "inject_node's residual allow-list is a hand-maintained literal again")


# ---------------------------------------------------------------- the chains are actually gone

@pytest.mark.parametrize("func", [
    run_commands.normalize_control,
    RunCommandService._collaboration_precondition,
    RunCommandService._decision,
])
def test_no_dispatcher_compares_the_event_type_to_a_specific_event(func):
    """AST, not substrings: comments are not AST nodes, so this cannot be satisfied by leaving the
    old chain behind as a comment. Each of the three used to be a flat per-type chain; the only
    membership test any of them may still make is the class-level COLLABORATION_EVENTS one."""
    tree = function_tree(func)
    compares = [node for node in ast.walk(tree) if isinstance(node, ast.Compare)
                if isinstance(node.left, ast.Name) and node.left.id == "event_type"]
    offenders = [ast.unparse(node) for node in compares
                 if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
                 or not any(isinstance(cmp, ast.Name) and cmp.id == "COLLABORATION_EVENTS"
                            for cmp in node.comparators)]
    assert offenders == [], f"{func.__qualname__} still branches per event type: {offenders}"


def test_a_payload_with_no_rule_of_its_own_still_goes_through_both_shared_guards(tmp_path):
    """A `None` normalizer must not mean "unvalidated": the shared preamble and tail still run."""
    rd = _seed(tmp_path)
    srv = _Srv(tmp_path)
    assert CONTROL_SPECS[EV_PAUSE].normalize is None
    assert normalize_control(srv, rd, EV_PAUSE, {}) == {}
    with pytest.raises(HTTPException) as unknown_field:
        normalize_control(srv, rd, EV_PAUSE, {"reason": "x"})
    assert unknown_field.value.status_code == 400
    with pytest.raises(HTTPException) as unknown_event:
        normalize_control(srv, rd, "not_an_event", {})
    assert unknown_event.value.status_code == 400
