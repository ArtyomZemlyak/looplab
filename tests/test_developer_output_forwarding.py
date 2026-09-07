"""What the engine reads off `self.developer` must survive the facade.

Under the shipped default (`Settings.unified_agent`) the engine's developer is a `UnifiedAgent`,
i.e. a `WrapsDeveloper`. Two things were set on the INNER developer and read off that wrapper:

  * `last_rollback_stage` / `last_budget_exhausted` — registered in `DEVELOPER_OUTPUT_ATTRS` but
    never mirrored by `_sync_audit`, so every `node_repaired` row recorded "no rollback was
    requested" and "the session finished on its own terms". Both readers default to the FALSY
    value, which is exactly the reading each attribute exists to stop being the only one available.

  * `bind_state` — `engine/node_build.py` binds with `getattr(developer, "bind_state", None)`, and
    the wrapper had none, so the Developer's `QuestionBoardTools` answered "no run state bound" on
    every call and its `CrossRunTools` answered nothing. Both shipped inert under the default.

DERIVED, not pinned: the required set comes from the engine's own `getattr(self.developer, "…")`
sites by AST, so an attribute added to the registry and read by the engine goes red here rather
than reading as a permanently absent feature. `tests/_source_scan.py`'s rule applies — a comment is
not an AST node.
"""
from __future__ import annotations

import ast
import pathlib

from looplab.agents.roles import DEVELOPER_OUTPUT_ATTRS, WrapsDeveloper
from tests._source_scan import iter_trees

_ENGINE = pathlib.Path(__file__).resolve().parents[1] / "looplab" / "engine"


def _attrs_read_off_the_facade() -> set[str]:
    """Registry members the engine reads with `getattr(self.developer, "<name>", …)`.

    Through `_source_scan.iter_trees` rather than a local `rglob`: the shared walk owns the decoding
    (`utf-8-sig`, `errors="replace"`) that keeps a BOM'd file from raising `SyntaxError` out of an
    unrelated scan, and it skips `.ipynb_checkpoints` autosaves that would report the PAST as a
    present violation. `tests/test_source_scan_helper.py` is the guard that says so.
    """
    found: set[str] = set()
    for path, tree in iter_trees(_ENGINE):
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr" and len(node.args) >= 2):
                continue
            target, name = node.args[0], node.args[1]
            # `self.developer` at the historical inline sites, or the bare `developer` the envelope
            # capture (`node_build._capture_developer_result`, doc 52 row 12) reads through — the
            # ONE place every side channel is now read, on behalf of every build and repair site.
            reads_developer = ((isinstance(target, ast.Attribute) and target.attr == "developer")
                               or (isinstance(target, ast.Name) and target.id == "developer"))
            if not (reads_developer and isinstance(name, ast.Constant)
                    and isinstance(name.value, str)):
                continue
            if name.value in DEVELOPER_OUTPUT_ATTRS:
                found.add(name.value)
    return found


class _Inner:
    """A developer that sets every registry attribute and takes a state binding."""

    def __init__(self) -> None:
        self.bound = None
        for attr in DEVELOPER_OUTPUT_ATTRS:
            setattr(self, attr, f"<{attr}>")

    def bind_state(self, state, parent=None) -> None:
        self.bound = state


class _Facade(WrapsDeveloper):
    def __init__(self, inner) -> None:
        self.inner = inner


def test_every_engine_read_survives_the_wrapper():
    required = _attrs_read_off_the_facade()
    assert required, "the AST scan found no `getattr(self.developer, ...)` site — it read nothing"

    facade = _Facade(_Inner())
    facade._sync_audit()

    lost = sorted(attr for attr in required
                  if not str(getattr(facade, attr, "") or "").startswith("<"))
    assert lost == [], (
        f"{lost} are read off the facade by the engine and do not survive it — each reads as its "
        "own falsy default on every node, i.e. the feature silently ceasing to exist")


def test_the_wrapper_forwards_the_state_binding():
    """`node_build.py` calls this on the facade; the tools that need it live on the inner."""
    inner = _Inner()
    _Facade(inner).bind_state("STATE")
    assert inner.bound == "STATE", (
        "MUTATION: delete `WrapsDeveloper.bind_state` and the Developer's question board and "
        "cross-run tools answer 'no run state bound' for the whole run under the default config")


def test_a_one_argument_bind_state_is_tolerated():
    """A developer is not a ToolProvider, so nothing obliges it to take `parent` — and a TypeError
    here would kill the build rather than skip an optional binding."""

    class _OneArg:
        def bind_state(self, state):
            self.bound = state

    inner = _OneArg()
    _Facade(inner).bind_state("STATE", None)
    assert inner.bound == "STATE"


def test_a_developer_with_no_binding_is_a_no_op():
    """Most developers (draft, offline, template) have none; forwarding must not invent one."""

    class _Bare:
        pass

    _Facade(_Bare()).bind_state("STATE")     # must not raise


def test_a_TypeError_from_inside_the_callee_is_not_retried():
    """The arity fallback is decided from the SIGNATURE, not by catching TypeError around the call.

    A `TypeError` raised from inside the callee's own body is indistinguishable at the boundary from
    an arity mismatch, so a `try: fn(state, parent) except TypeError: fn(state)` would run a state
    binding TWICE on a developer whose bind_state merely happened to raise one.
    """
    calls = []

    class _Raises:
        def bind_state(self, state, parent=None):
            calls.append(state)
            raise TypeError("raised from inside the body")

    import pytest
    with pytest.raises(TypeError):
        _Facade(_Raises()).bind_state("STATE")
    assert calls == ["STATE"], "bound once, not retried"


def test_the_parent_is_forwarded_when_the_developer_takes_one():
    class _Two:
        def bind_state(self, state, parent=None):
            self.got = (state, parent)

    inner = _Two()
    _Facade(inner).bind_state("STATE", "PARENT")
    assert inner.got == ("STATE", "PARENT")


def test_every_parameter_KIND_that_can_take_a_parent_gets_one():
    """The arity rule is decided by BINDING the call, never by COUNTING parameters.

    `len(signature.parameters) >= 2` is true of `(state, **kw)` and of `(state, *, parent=None)` —
    the natural way to write "accepted and ignored" — and then makes the positional two-argument
    call that raises the very TypeError the rule exists to avoid, out of an unguarded forwarder and
    into `node_build._implement`. It is false of `(*args)`, which silently drops a `parent` the
    callee would have taken.

    MUTATION: `accepts_parent = len(inspect.signature(fn).parameters) >= 2` -> `_KwArgs` and
    `_KeywordOnly` raise TypeError and `_StarArgs` records `None`.
    """
    class _KwArgs:
        def bind_state(self, state, **kw):
            self.got = (state, kw.get("parent"))

    class _KeywordOnly:
        def bind_state(self, state, *, parent=None):
            self.got = (state, parent)

    class _StarArgs:
        def bind_state(self, *args):
            self.got = (args[0], args[1] if len(args) > 1 else None)

    for cls in (_KwArgs, _KeywordOnly, _StarArgs):
        inner = cls()
        _Facade(inner).bind_state("STATE", "PARENT")
        assert inner.got == ("STATE", "PARENT"), f"{cls.__name__} did not receive the parent"
