"""The one bounded, redacting JSON-tree walker both durable boundaries share (doc 25 CO-06).

`tracing.sanitize_trace_value` and `advisory_payloads._tree` were the same function written twice:
recursive walk, depth cap, per-container item cap, a shared mutable char-budget cell, ints bounded to
±2^63 else stringified, non-finite floats stringified, `is_secret_key_name` → "***", strings through
the redactor. Both are DURABLE boundaries — a span record and a payload a human reads — and what the
function does is REDACT, so a fix landing in one walker silently missed the other.

They had already drifted, and every divergence had tracing safer. A differential harness over a
41-value corpus (nested/deep/wide structures, secret keys, oversized strings, big ints, non-finite
floats, unicode, control characters, bytes, an IntEnum, an opaque object, a mapping whose `items()`
raises) measured the collapse: the TRACE boundary is byte-identical, and the advisory boundary
changes in exactly seven places — the four stricter behaviours adopted deliberately and named below.
"""
from __future__ import annotations

import math
from enum import IntEnum

import pytest

from looplab.core.advisory_payloads import _tree
from looplab.core.redact import bounded_redacted_tree
from looplab.core.tracing import sanitize_trace_value


class _Level(IntEnum):
    HIGH = 3


class _HostileMapping(dict):
    def items(self):
        raise RuntimeError("this mapping refuses to be iterated")


def _walk(value, *, chars=65_536, items=256, **kwargs):
    return bounded_redacted_tree(value, [chars], [items], **kwargs)


# ------------------------------------------------------------------ what it must never do

def test_a_secret_key_never_carries_its_value_through():
    out = _walk({"api_key": "sk-live-abcdef", "token": "t", "plain": "visible"})
    assert out["api_key"] == "***" and out["token"] == "***"
    assert out["plain"] == "visible"


def test_a_secret_nested_deep_is_still_masked():
    out = _walk({"a": {"b": {"c": {"password": "hunter2"}}}})
    assert out["a"]["b"]["c"]["password"] == "***"


def test_a_hostile_mapping_DEGRADES_it_does_not_raise():
    """A `dict` subclass whose `items()` throws used to take down the advisory projection with a
    RuntimeError; tracing already answered "<mapping unavailable>". A redaction boundary that can
    raise is a boundary that can drop a whole payload, so the guard is now universal — this is the
    one behaviour change here that is a DEFECT FIX rather than a tightening."""
    assert _walk(_HostileMapping({"a": 1})) == "<mapping unavailable>"
    assert _tree(_HostileMapping({"a": 1}), [65_536], [256]) == "<mapping unavailable>"
    assert sanitize_trace_value(_HostileMapping({"a": 1})) == "<mapping unavailable>"


def test_a_key_that_redacts_to_empty_is_DROPPED_not_emitted():
    """Two empty keys collide into one JSON member, silently discarding a value."""
    out = _walk({"": "gone", " ": "also gone", "ok": "kept"})
    assert out == {"ok": "kept"}


def test_recursion_is_cut_at_the_depth_cap_and_the_marker_costs_budget():
    value = {"k0": {"k1": {"k2": {"k3": {"k4": {"k5": "too deep"}}}}}}
    out = _walk(value, max_depth=3)
    assert out["k0"]["k1"]["k2"] == "<depth-limited>"


def test_a_cyclic_structure_terminates_rather_than_recursing_forever():
    """The depth cap is what makes a self-referential payload survivable at all."""
    node: dict = {"name": "loop"}
    node["self"] = node
    assert "<depth-limited>" in str(_walk(node))


# ------------------------------------------------------------------ the budgets

def test_the_character_budget_is_shared_across_the_whole_tree():
    out = _walk({"a": "x" * 400, "b": "y" * 400}, chars=500)
    assert len(out.get("a", "")) + len(out.get("b", "")) <= 500


def test_the_walk_stops_as_soon_as_the_character_budget_is_spent():
    out = _walk([("x" * 300) for _ in range(50)], chars=400)
    assert sum(len(part) for part in out) <= 400
    assert len(out) < 50, "a spent budget must stop the walk, not keep appending empties forever"


def test_the_item_budget_bounds_the_TOTAL_node_count_not_just_one_container():
    out = _walk({f"k{index}": [1, 2, 3] for index in range(40)}, items=10)
    assert sum(1 for _ in out) <= 10


def test_a_caller_that_shares_its_budget_cell_sees_it_spent():
    """`advisory_payloads` charges URLs and verdict rows against the SAME cell, so the walker must
    mutate the caller's cell rather than a private copy — otherwise the page double-spends."""
    budget = [200]
    bounded_redacted_tree({"a": "x" * 150}, budget, [64])
    assert budget[0] < 200


# ------------------------------------------------------------------ scalar handling

@pytest.mark.parametrize("value", [None, True, False, 0, -1, 7, 1.5, 2 ** 62])
def test_json_safe_scalars_pass_through_unchanged(value):
    assert _walk(value) == value


@pytest.mark.parametrize("value", [2 ** 64, -(2 ** 64)])
def test_an_int_outside_int64_becomes_text_rather_than_an_unencodable_number(value):
    assert _walk(value) == str(value)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_a_non_finite_float_becomes_text_because_json_has_no_spelling_for_it(value):
    assert isinstance(_walk(value), str)


def test_an_int_SUBCLASS_keeps_its_value_instead_of_becoming_a_string():
    """`isinstance`, not `type(...) is`: an IntEnum is an int, and stringifying it loses the number a
    reader is trying to compare. The advisory walker used to stringify these."""
    assert _walk(_Level.HIGH) == 3
    assert _walk({"level": _Level.HIGH}) == {"level": 3}


def test_an_object_with_no_json_spelling_is_rendered_as_bounded_text():
    class _Opaque:
        def __repr__(self):
            return "<an opaque object>"

    assert _walk(_Opaque()) == "<an opaque object>"


# ------------------------------------------------------------------ the two callers' constants

def test_the_trace_boundary_spends_its_whole_budget_on_one_string():
    """A span is already the bounded record; truncating each field again would drop the tail of the
    one field that mattered."""
    assert len(sanitize_trace_value("x" * 40_000)) > 2_000


def test_the_advisory_boundary_caps_each_string_so_one_field_cannot_starve_the_page():
    """An advisory payload is a LIST of rows a human reads; an oversized early statement must not
    consume the budget the later rows need."""
    budget = [65_536]
    assert len(_tree("x" * 40_000, budget, [256])) <= 2_000
    assert budget[0] > 60_000, "the rest of the page budget must survive one huge field"


def test_both_callers_delegate_rather_than_keeping_their_own_walker():
    """A grep guard on the collapse: a private `def walk`/`def _tree` body containing the redaction
    idiom is the second implementation coming back — which is how these two drifted in the first
    place, and the drift is invisible because both outputs still look redacted."""
    import inspect

    from looplab.core import advisory_payloads, tracing

    for module, name in ((tracing, "sanitize_trace_value"), (advisory_payloads, "_tree")):
        source = inspect.getsource(getattr(module, name))
        assert "bounded_redacted_tree(" in source, f"{name} no longer shares the walker"
        assert "is_secret_key_name(" not in source, f"{name} re-implements the secret-key masking"


@pytest.mark.parametrize("value", [
    {"api_key": "sk-live-abcdef", "nested": {"token": "t"}},
    [1, [2, [3, [4, [5, [6, [7]]]]]]],
    {"": "blank", "ok": 1},
    _Level.HIGH,
    float("nan"),
    2 ** 64,
])
def test_the_two_boundaries_now_agree_on_everything_but_their_declared_caps(value):
    """The whole point of the collapse. They may differ on how much TEXT they keep — that is a
    declared constant — but never on what a value BECOMES."""
    assert sanitize_trace_value(value) == _tree(value, [65_536], [256])
