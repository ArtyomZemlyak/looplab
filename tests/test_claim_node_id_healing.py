"""A node citation the model wrote as a string is still a citation.

`_ClaimOut.node_ids` is healed `mode="before"`, so whatever the pre-heal drops never reaches
pydantic's own validation — and a bare `list[int]` field ACCEPTS `"3"` and `4.0`. The healer tested
`type(item) is int`, so every stringified id was thrown away before validation could take it.

MEASURED by driving the shipped model: `_ClaimOut.model_validate({"node_ids": ["3", "5"]})` returned
`[]`. Numbers-as-strings are ordinary LLM JSON — `30f6aee6` measured a whole list arriving as one
string in this same module — so a TRUE, correctly-cited claim reached `trust/memo_verify.py` with no
evidence and was durably stamped `unsupported` / "no evidence cited". That verdict then poisons the
`memo_verdict_cue` tally spliced into every proposal prompt and refuses the claim as cross-run
evidence at finalization.

The rule is now: coerce what validation would, drop what it would not.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import pytest

from looplab.agents.deep_research import _ClaimOut, _healed_node_id


def _ids(value):
    return _ClaimOut.model_validate({"statement": "s", "node_ids": value}).node_ids


def test_a_stringified_citation_SURVIVES():
    """The defect itself. Mutation: restore `type(item) is int`, and a claim citing ["3", "5"]
    arrives with no evidence and is stamped `unsupported` — the claim is true, the citation is
    right, and the record says nobody backed it."""
    assert _ids(["3", "5"]) == [3, 5]
    assert _ids(["12"]) == [12]
    assert _ids(["  7 "]) == [7]


def test_an_INTEGRAL_FLOAT_survives_and_a_fractional_one_does_not():
    """`4.0` is what a JSON `4.0` decodes to and pydantic takes it; `3.5` is not a node id.
    Mutation: accept any float and node 3.5 becomes node 3 — a citation nobody wrote."""
    assert _ids([4.0]) == [4]
    assert _ids([3.5]) == []
    assert _ids(["3.5"]) == []


def test_a_BOOL_is_still_refused_and_this_is_the_load_bearing_one():
    """`isinstance(True, int)` is True, so a bool sails through any `isinstance` test and cites
    NODE 1 on a claim that named nothing — the same trap `param_carriers.declared_numeric_params`
    records. Mutation: widen the int test to `isinstance`, and this fails."""
    assert _ids([True]) == []
    assert _ids([False]) == []
    assert _ids([True, "3"]) == [3]


def test_junk_is_dropped_and_the_field_is_KEPT():
    """The drop-the-offender rung this healer belongs to: one bad element must not cost the whole
    field. Mutation: return `None`/raise on junk, and a memo loses every citation it got right."""
    assert _ids(["x", 5]) == [5]
    assert _ids([None, 5]) == [5]
    assert _ids([{"node": 3}, 5]) == [5]
    assert _ids(["", 5]) == [5]


def test_an_ALREADY_CLEAN_list_is_returned_unchanged():
    """The healer must be a no-op on the common case, or every memo pays a repair warning it did
    not earn. Mutation: rebuild the list unconditionally and the `healed == value` identity that
    suppresses the warning stops holding."""
    assert _ids([3, 5]) == [3, 5]
    assert _ids([]) == []


@pytest.mark.parametrize("raw,want", [
    (3, 3), (0, 0), (-2, -2), ("3", 3), ("-2", -2), ("+4", 4), ("  7 ", 7),
    (4.0, 4), (0.0, 0), (3.5, None), ("3.5", None), ("x", None), ("", None),
    (True, None), (False, None), (None, None), ([3], None), ({"a": 3}, None),
    ("--3", None), ("3a", None),
])
def test_the_coercion_truth_table(raw, want):
    """The rule stated as a table so a future widening has to argue with each row. `"--3"` is the
    reason the sign strip takes ONE character rather than `lstrip("+-")`, which would accept it."""
    assert _healed_node_id(raw) == want
