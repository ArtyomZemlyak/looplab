"""Reading ANOTHER run is one implementation with a policy hook (doc 25 TO-05).

`SiblingRunTools`, `AllRunsTools` and `MachineRunsTools` each held the same composition (a
traversal-guarded, (size, mtime)-fingerprinted `RunStateCache` plus an inner `RunTools` bound per
read) and each spelled out the same delegation: resolve the run, refuse a miss, bind the reader,
fetch the source note, prefix it onto the inner tool's answer. The PARTIAL-SOURCE receipt was
triplicated across their listing renderers, verbatim in two of them.

Two properties have to survive the merge, and they pull in opposite directions:

* every foreign read carries the source receipt — a node read from a TRUNCATED log otherwise looks
  authoritative, which is how a prefix read becomes a "no later run beat this" absence claim;
* `SiblingRunTools`'s same-task boundary is enforced on the DIRECT read, not just on discovery — the
  model supplies the run_id, so the id is the authorization boundary, never evidence that the
  scoped listing was called first.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from looplab.events.eventstore import EventStore
from looplab.tools.machine_runs_tools import MachineRunsTools
from looplab.tools.run_tools import AllRunsTools, ForeignRunReader, SiblingRunTools


class _State:
    def __init__(self, run_id, task_id):
        self.run_id, self.task_id = run_id, task_id
        self.direction, self.goal, self.finished = "min", "g", False
        self.nodes = {}


def _run(root, run_id, task_id="task-a", *, truncate=False):
    rd = root / run_id
    rd.mkdir(parents=True, exist_ok=True)
    log = rd / "events.jsonl"
    store = EventStore(log)
    store.append("run_started", {"run_id": run_id, "task_id": task_id, "goal": "g",
                                 "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                  "code": "print(1)"})
    if truncate:
        # A COMPLETE but corrupt row: `iter_event_jsonl` stops there, and the surviving prefix folds
        # into a RunState that looks exactly like a whole one. (A half-written final LINE is the
        # normal mid-append shape and is deliberately not a divergence.)
        with log.open("a", encoding="utf-8") as fh:
            fh.write("{not json at all}\n")
    return rd


# --------------------------------------------------------------------------------------------
# The shared plumbing
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("factory", [SiblingRunTools, AllRunsTools, MachineRunsTools])
def test_every_foreign_reader_shares_the_plumbing(tmp_path, factory):
    tools = factory(tmp_path)
    assert isinstance(tools, ForeignRunReader)
    assert tools._runs is not None and tools._reader is not None


def test_a_missing_run_is_refused_by_name_not_by_exception(tmp_path):
    tools = AllRunsTools(tmp_path)
    assert "no such run" in tools._delegate("ghost", "read_code", {"node_id": 0})


def test_the_sibling_reader_names_what_it_could_not_find(tmp_path):
    """The wording is the model's only clue about WHICH surface refused — through the real tools,
    since that is where the label is chosen."""
    tools = SiblingRunTools(tmp_path)
    tools.bind_state(_State("mine", "task-a"))
    assert "no such sibling run" in tools._read("ghost", 0)
    assert "no such sibling run" in tools._code("ghost", 0)
    assert "no such run" in AllRunsTools(tmp_path)._read("ghost", 0)


def test_a_truncated_source_log_stamps_every_per_node_read(tmp_path):
    """Not just the listing. Without this a node read from a prefix looks authoritative."""
    _run(tmp_path, "other", truncate=True)
    tools = AllRunsTools(tmp_path)
    out = tools._read("other", 0)
    assert "PARTIAL SOURCE" in out, out[:200]


def test_a_complete_source_log_adds_no_receipt(tmp_path):
    """The complement — a receipt on every read would train the model to ignore it."""
    _run(tmp_path, "other")
    assert "PARTIAL SOURCE" not in AllRunsTools(tmp_path)._read("other", 0)


def test_the_listing_receipt_is_the_same_string_everywhere(tmp_path):
    _run(tmp_path, "other", truncate=True)

    def _suffix(factory):
        tools = factory(tmp_path)
        tools._state("other")          # `partial()` answers "was what I just read whole?"
        return tools._partial_suffix("other")

    suffixes = {_suffix(f) for f in (SiblingRunTools, AllRunsTools, MachineRunsTools)}
    assert len(suffixes) == 1 and "PARTIAL SOURCE" in suffixes.pop()


# --------------------------------------------------------------------------------------------
# The policy hook — the part that must NOT merge
# --------------------------------------------------------------------------------------------

def test_a_sibling_direct_read_enforces_the_task_boundary(tmp_path):
    """Discovery is same-task scoped, but the direct read takes a model-supplied run_id."""
    _run(tmp_path, "mine", task_id="task-a")
    _run(tmp_path, "foreign", task_id="task-z")
    tools = SiblingRunTools(tmp_path)
    tools.bind_state(_State("mine", "task-a"))

    assert "not a sibling of task" in tools._read("foreign", 0)
    assert "not a sibling of task" in tools._code("foreign", 0)


def test_an_unbound_sibling_reader_fails_closed(tmp_path):
    """With no authoritative task id there is no boundary to check a guessed run_id against — so
    refuse, rather than letting `x and ...` skip the guard in exactly that state."""
    _run(tmp_path, "foreign", task_id="task-z")
    assert "not a sibling of task" in SiblingRunTools(tmp_path)._read("foreign", 0)


@pytest.mark.parametrize("factory", [AllRunsTools, MachineRunsTools])
def test_the_cross_task_readers_deliberately_apply_no_scope(tmp_path, factory):
    """They give the agent the capability and let it judge relevance; narrowing them here would
    silently re-impose the same-task rule they exist to lift."""
    _run(tmp_path, "foreign", task_id="task-z")
    tools = factory(tmp_path)
    assert tools._scope_denial("foreign", _State("foreign", "task-z")) == ""


def test_the_scope_hook_is_consulted_before_any_content_is_read(tmp_path):
    """A denial that arrived after the delegate would already have leaked the foreign run's text."""
    _run(tmp_path, "foreign", task_id="task-z")
    tools = SiblingRunTools(tmp_path)
    tools.bind_state(_State("mine", "task-a"))
    reads = []
    inner = tools._reader.execute
    tools._reader.execute = lambda name, args: reads.append(name) or inner(name, args)
    assert "not a sibling" in tools._delegate("foreign", "read_code", {"node_id": 0})
    assert reads == [], "the inner reader ran despite the refusal"


# --------------------------------------------------------------------------------------------
# Structural: the copies must not come back
# --------------------------------------------------------------------------------------------

def _calls(fn) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return {node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}


@pytest.mark.parametrize("method", [
    SiblingRunTools._read, SiblingRunTools._code,
    AllRunsTools._read, AllRunsTools._code,
    MachineRunsTools._read_experiment, MachineRunsTools._read_logs,
])
def test_no_provider_re_spells_the_bind_and_receipt_dance(method):
    called = _calls(method)
    assert "_delegate" in called, f"{method.__qualname__} re-implemented the delegation"
    assert "bind_state" not in called and "source_note" not in called, (
        f"{method.__qualname__} still binds the reader / fetches the receipt by hand")


def test_no_provider_re_declares_the_shared_composition():
    """A subclass re-assigning `_runs`/`_reader` in its own `__init__` is the duplication returning:
    two caches over the same root fold every foreign run twice."""
    for factory in (SiblingRunTools, AllRunsTools, MachineRunsTools):
        assigned = {t.attr
                    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(factory.__init__))))
                    if isinstance(node, ast.Assign)
                    for t in node.targets if isinstance(t, ast.Attribute)}
        assert not ({"_runs", "_reader"} & assigned), (factory.__name__, assigned)
        assert "__init__" in inspect.getsource(factory), factory.__name__


def test_the_partial_receipt_has_exactly_one_spelling():
    from _source_scan import iter_sources

    owners = {path.name for path, text in iter_sources()
              if "PARTIAL SOURCE (read incomplete" in text}
    assert owners == {"run_tools.py"}, owners
