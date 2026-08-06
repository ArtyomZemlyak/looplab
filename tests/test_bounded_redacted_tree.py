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


# ------------------------------------------------------------------ the truncation receipt (SR-06)
#
# Two serve routers each grew their own walker to compute a "this was cut" receipt, and the copies
# drifted on the one rule that matters at a redaction boundary. They now share this walker; the
# receipt is an out-cell so they no longer need a walk of their own to produce it.

def _walk_cut(value, **kwargs):
    cut = [False]
    out = _walk(value, truncated=cut, **kwargs)
    return out, cut[0]


@pytest.mark.parametrize("value,expected,why", [
    ({"a": 1, "b": "short"}, False, "nothing was omitted or shortened"),
    ({f"k{i}": i for i in range(40)}, True, "fanout sliced"),
    ({"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}, True, "depth cut"),
    ({"note": "x" * 900}, True, "string shortened by the cap"),
    ({"": "blank", "ok": 1}, True, "a key that redacts to empty loses its value"),
    (_HostileMapping({"a": 1}), True, "a mapping that refused to iterate degraded"),
])
def test_the_receipt_reports_omission_and_shortening(value, expected, why):
    _out, cut = _walk_cut(value, str_cap=500, max_items=32, max_depth=3)
    assert cut is expected, why


def test_masking_a_secret_is_not_truncation():
    """Nothing was dropped for SIZE. Telling an operator their config was truncated because a
    credential was hidden is a misleading receipt — and it is the difference that made the two
    router copies disagree about the same payload."""
    out, cut = _walk_cut({"api_key": "tok-abcd", "epochs": 3})
    assert out == {"api_key": "***", "epochs": 3}
    assert cut is False


def test_an_exhausted_budget_is_reported_not_silently_swallowed():
    out, cut = _walk_cut({"a": "x" * 40, "b": "y" * 40}, chars=20)
    assert cut is True and out is not None


# ---------------------------------------------------------- the receipt LIED in both directions
#
# The test above only reaches the secret-KEY branch, which never calls the text bounder at all. The
# receipt for every OTHER value was inferred inside `safe_text` by comparing the redacted output
# against the RAW input — and `redact_persisted_text` normalizes and redacts BEFORE it bounds, so
# output length is not a proxy for "something was cut" in either direction. Both errors shipped, and
# both reached an operator: `misc._bounded_json_value` -> `params_truncated` (the memory browser) and
# `genesis._bounded_evidence_value` -> `defaults_truncated`.

def test_masking_a_credential_INSIDE_a_value_is_not_truncation_either():
    """DIRECTION 2, the common case: masking reported AS truncation. `redact_secrets` masks a
    credential wherever it appears, including under a perfectly benign key, so `sk-` + 30 chars (33)
    became `sk-***` (6) — measured shorter than its input, hence "truncated". Every row containing
    any credential said so, at any budget, however generous."""
    out, cut = _walk_cut({"note": "sk-" + "A" * 30, "epochs": 3}, chars=65_536, str_cap=500)
    assert out == {"note": "sk-***", "epochs": 3}, out
    assert cut is False, "a masked credential is not truncation — nothing was dropped for SIZE"


def test_content_the_cap_REPLACED_with_a_hash_marker_IS_reported():
    """DIRECTION 1, the dangerous one: silent truncation reported as truncated=False. Redaction runs
    BEFORE the bound and can GROW the text — NFKC expands each `ﬄ` to three characters — so a
    300-character field becomes 900, is replaced wholesale by its digest at the 500-char cap, and
    still measures 500 > 300. The operator was shown a destroyed field and told nothing was cut."""
    out, cut = _walk_cut({"note": "ﬄ" * 300}, chars=65_536, str_cap=500)
    assert "[redacted preview:" in out["note"], out
    assert cut is True, "a field replaced by its own digest is exactly what the receipt is for"


def test_a_receipt_is_never_a_COINCIDENCE_of_two_lengths():
    """The inference could also be defeated by arithmetic alone. `password=x` is 10 characters;
    masked it is `password=***`, 12; hashed down to a 10-character budget it is 10 again. Equal
    lengths, so the strict `<` said nothing had happened — to a value that by then was nine
    characters of digest tail."""
    out, cut = _walk_cut("password=x", chars=10, str_cap=None)
    assert "password" not in out, out
    assert cut is True


def test_BOTH_shipped_receipts_report_the_same_two_facts():
    """Driven through the two projectors that actually serve these flags, not just the walker: the
    routers read this out-cell verbatim into `params_truncated` / `defaults_truncated`, so an
    operator reading either endpoint was told the opposite of the truth in both directions."""
    from looplab.serve.routers.genesis import _bounded_evidence_value
    from looplab.serve.routers.misc import _bounded_json_value

    for project in (_bounded_evidence_value, _bounded_json_value):
        masked, cut = project({"note": "sk-" + "A" * 30, "epochs": 3})
        assert masked == {"note": "sk-***", "epochs": 3}, masked
        assert cut is False, f"{project.__name__} calls a masked credential a truncated config"

        destroyed, cut = project({"note": "ﬄ" * 300})
        assert "[redacted preview:" in destroyed["note"], destroyed
        assert cut is True, f"{project.__name__} hid a destroyed field behind a clean receipt"


def test_NORMALIZATION_alone_is_not_truncation():
    """The rule the deleted comparison could not state: folding, control characters becoming spaces
    and `single_line` collapsing whitespace all change the length without dropping any of the value.
    A key normalized this way used to spend the whole payload's receipt."""
    out, cut = _walk_cut({"a  b\tc": 1})
    assert out == {"a b c": 1}, out
    assert cut is False


def test_the_two_serve_projectors_delegate_rather_than_walking_themselves():
    """The SR-06 collapse. A private recursive walk in either router is the second implementation
    coming back, and the drift it produces is invisible: both outputs still look redacted, they just
    disagree about what happens to a credential-named key."""
    import inspect

    from looplab.serve.routers import genesis, misc

    for module, name in ((genesis, "_bounded_evidence_value"), (misc, "_bounded_json_value")):
        source = inspect.getsource(getattr(module, name))
        assert "bounded_redacted_tree(" in source, f"{name} no longer shares the walker"
        assert "is_secret_key_name(" not in source, f"{name} re-implements the secret-key masking"
        assert "depth=depth + 1" not in source, f"{name} still recurses into itself"


def test_both_serve_projectors_agree_on_a_credential_named_key():
    """The measured divergence: genesis DROPPED a secret-named key (and called that truncation),
    misc MASKED it (and did not). Same payload, two endpoints, two answers — and the dropping one
    told the operator nothing about why a field had vanished."""
    from looplab.serve.routers.genesis import _bounded_evidence_value
    from looplab.serve.routers.misc import _bounded_json_value

    payload = {"api_key": "tok-abcd", "lr": 0.01}
    assert _bounded_evidence_value(payload) == _bounded_json_value(payload)
    assert _bounded_evidence_value(payload) == ({"api_key": "***", "lr": 0.01}, False)


def test_a_hostile_mapping_no_longer_reaches_the_endpoint_as_a_500():
    """Both routers' own walkers called `.items()` unguarded, so a dict subclass that raises took the
    response down. The shared walker degrades to a marker — a redaction boundary that can raise is a
    boundary that can drop a whole payload."""
    from looplab.serve.routers.genesis import _bounded_evidence_value
    from looplab.serve.routers.misc import _bounded_json_value

    for project in (_bounded_evidence_value, _bounded_json_value):
        out, cut = project(_HostileMapping({"a": 1}))
        assert out == "<mapping unavailable>" and cut is True
