"""The Engine's sub-object delegators, and the lane each one has to carry (doc 25 ES-13).

`Engine` forwards 32 one-line methods to `self.lessons` / `self.holdout` / `self.workspace`. The
delegators exist on purpose — CLAUDE.md documents them as a monkeypatch seam, and `LessonMemory`
routes its own cross-calls back through them so an instance-level patch intercepts every path.

What was NOT deliberate is that adding one is entirely manual, including the `@in_llm_lane`
decoration that eight of them carry. Forgetting the lane does not fail: the call just runs outside
the capped enrichment lane and competes with foreground work for provider concurrency, which
surfaces as an unexplained stall rather than an error. `Engine.FORWARDED_SUBOBJECT_MEMBERS` declares
the mapping and this file checks it BOTH ways.

The three other forwarding shapes in that class are deliberately out of the table, and the last two
tests here pin that they stay out — a property forwards an attribute rather than a call, and a
`staticmethod(LessonMemory.x)` alias binds the class's own function, so re-pointing `self.lessons`
does not intercept it. Folding those into the table would claim a routing guarantee they do not
have.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from looplab.engine.orchestrator import Engine
from tests import _source_scan

REGISTRY = Engine.FORWARDED_SUBOBJECT_MEMBERS


def _engine_class_tree() -> ast.ClassDef:
    source = inspect.getsource(Engine)
    return ast.parse(_source_scan.textwrap.dedent(source)).body[0]


def _plain_delegators() -> dict[str, tuple[str, str, str | None]]:
    """Every `def _x(self, ...): return self.<sub>.<y>(...)` in the class body, from the AST.

    Read off the source rather than `dir(Engine)`: a bound method cannot tell you which sub-object
    it forwards to without calling it, and calling it is exactly what a registry guard must not do.
    """
    found: dict[str, tuple[str, str, str | None]] = {}
    for node in _engine_class_tree().body:
        if not isinstance(node, ast.FunctionDef) or len(node.body) != 1:
            continue
        statement = node.body[0]
        if not isinstance(statement, ast.Return):
            continue
        call = statement.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Attribute)
                and isinstance(call.func.value.value, ast.Name)
                and call.func.value.value.id == "self"):
            continue
        lane = None
        for decorator in node.decorator_list:
            text = ast.unparse(decorator)
            if "in_llm_lane" in text:
                lane = ast.literal_eval(ast.parse(text, mode="eval").body.args[0])
        found[node.name] = (call.func.value.attr, call.func.attr, lane)
    return found


# ------------------------------------------------------------------ the registry is two-way

def test_every_delegator_is_declared():
    """A delegator missing from the table is one nobody checks the lane of."""
    undeclared = sorted(set(_plain_delegators()) - set(REGISTRY))
    assert undeclared == [], (
        f"{undeclared} forward to a sub-object but are not in FORWARDED_SUBOBJECT_MEMBERS, so "
        "nothing checks whether they carry the right @in_llm_lane")


def test_every_declared_member_is_still_a_delegator():
    """The other direction: an entry for a method that was deleted or reshaped is a stale claim, and
    a stale registry is worse than none — it reads as coverage."""
    delegators = _plain_delegators()
    missing = sorted(name for name in REGISTRY if name not in delegators)
    assert missing == [], f"{missing} are declared but no longer one-line sub-object delegators"


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_each_delegator_forwards_where_it_says_and_in_the_lane_it_says(name):
    sub_object, lane = REGISTRY[name]
    actual_sub, target, actual_lane = _plain_delegators()[name]

    assert actual_sub == sub_object, f"{name} forwards to self.{actual_sub}, not self.{sub_object}"
    assert actual_lane == lane, (
        f"{name} carries lane {actual_lane!r} but the registry declares {lane!r}. An enrichment call "
        "outside its lane does not fail — it competes with foreground work for provider concurrency")
    assert target == name.lstrip("_"), (
        f"{name} forwards to {target!r}; the table's checkability rests on the naming rule")


def test_the_registry_is_not_empty_and_covers_all_three_sub_objects():
    """A derivation that silently found nothing would make every test above pass vacuously."""
    assert len(REGISTRY) >= 30
    assert {sub for sub, _lane in REGISTRY.values()} == {"lessons", "holdout", "workspace"}
    # 7, not 8, since 2026-08-29: `_write_reflection_note` left the table when it gained its own
    # `_op_span("reflection")` — run-end reflection was making BILLED calls with no span open, so
    # its money appeared in no trace surface (measured: 105 of 25,430 calls, $0.19 of $100.27,
    # across 49 of 68 probe runs). The floor is a vacuity guard, so it tracks the real count; it is
    # lowered here deliberately rather than by reflex, and the entry it lost is named above.
    assert sum(1 for _sub, lane in REGISTRY.values() if lane == "enrichment") >= 7


# ------------------------------------------------------------------ the seam still intercepts

@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_each_delegator_is_a_real_class_attribute(name):
    """What makes it a monkeypatch seam. `monkeypatch.setattr(Engine, name, ...)` raises unless the
    attribute already exists, so a forwarding scheme that resolved these lazily — a `__getattr__`
    fallback, say — would break every test that patches one, and 15 of the 32 are patched today."""
    assert callable(inspect.getattr_static(Engine, name))


# ------------------------------------------------------------------ the other shapes stay out

def test_property_forwarders_are_not_in_the_table():
    """A property forwards an ATTRIBUTE, both ways through its setter, not a call — so it has no lane
    and the table's `name.lstrip('_') == target` rule does not describe it."""
    properties = {node.name for node in _engine_class_tree().body
                  if isinstance(node, ast.FunctionDef)
                  and any(ast.unparse(d) == "property" for d in node.decorator_list)}

    assert properties, "the property forwarders vanished; re-derive this guard"
    assert not (properties & set(REGISTRY))


def test_staticmethod_aliases_are_not_in_the_table():
    """`_x = staticmethod(LessonMemory.x)` binds the CLASS's function. It does not go through
    `self.lessons`, so re-pointing that instance does not intercept it — the opposite of what every
    table entry guarantees. Listing one here would claim a routing property it does not have."""
    aliases = set()
    for node in _engine_class_tree().body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if getattr(node.value.func, "id", "") != "staticmethod":
            continue
        aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))

    assert aliases, "the staticmethod aliases vanished; re-derive this guard"
    assert not (aliases & set(REGISTRY))
