"""One canonical-JSON preimage and one finite-metric contract (doc 25 SE-08).

Two helpers had been re-declared across the package under names that hid what differed:

* `_canonical_json` existed three times — twice strictly identical (both minting RECEIPT preimages)
  and once, in `serve/launch.py`, deliberately looser. One name for two contracts is how a caller
  ends up hashing a fallback rendering of a value it could not represent.
* `_finite_metric` existed three times under ONE name with TWO return types: `float | None` in
  `events/replay.py` and `search/speculation_quality.py`, `bool` in `engine/memory.py`. A reader
  grepping the name could not assume a contract, and `if _finite_metric(x):` is valid — and wrong —
  under one of them.
"""
from __future__ import annotations

import inspect
import json
import math
from pathlib import Path

import pytest

from looplab.core.fitness import finite_metric, is_usable_metric
from looplab.core.jsonutil import canonical_json


# ------------------------------------------------------------------ the strict preimage

def test_object_key_order_does_not_change_the_preimage():
    """Object order is not semantic in JSON, so an unsorted dump makes a receipt's digest depend on
    insertion order — two digests for one logical value."""
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_the_preimage_carries_no_incidental_whitespace():
    assert canonical_json({"a": 1, "b": [2, 3]}) == b'{"a":1,"b":[2,3]}'


def test_non_ascii_is_emitted_as_itself_not_escaped():
    assert canonical_json({"k": "градиент"}) == '{"k":"градиент"}'.encode("utf-8")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_number_is_REFUSED_not_emitted(bad):
    """`json.dumps` emits bare `NaN`/`Infinity` by default, which no strict JSON reader accepts — a
    receipt could be minted over bytes nothing else can parse. `allow_nan=False` is the decisive
    option here, and the only one whose absence produces valid-looking output."""
    with pytest.raises(ValueError, match="not canonical JSON"):
        canonical_json({"metric": bad})


def test_an_unrepresentable_value_RAISES_rather_than_falling_back():
    """Raising is the point. A caller minting a receipt must find out here, before the receipt
    exists, rather than get a digest over `str(obj)`."""
    with pytest.raises(ValueError, match="not canonical JSON"):
        canonical_json({"path": Path("/tmp/x")})


def test_a_cyclic_value_is_refused_rather_than_recursing_forever():
    cycle: dict = {}
    cycle["self"] = cycle
    with pytest.raises(ValueError, match="not canonical JSON"):
        canonical_json(cycle)


def test_the_output_round_trips_through_a_strict_reader():
    payload = {"b": [1, 2.5, None, True], "a": {"n": "x"}}
    assert json.loads(canonical_json(payload).decode("utf-8")) == payload


@pytest.mark.parametrize("module", ["looplab.search.speculation_calibration",
                                    "looplab.search.speculation_quality"])
def test_no_receipt_module_re_declares_the_preimage(module):
    import importlib

    source = inspect.getsource(importlib.import_module(module))
    assert "def _canonical_json" not in source, f"{module} re-declared the preimage helper"
    assert "canonical_json(" in source


def test_the_launch_hash_keeps_its_OWN_looser_contract_under_its_own_name():
    """A launch payload is caller-shaped and may legitimately carry a Path or an enum; its hash is a
    dedup identity, not a receipt preimage. The difference is fine — sharing the NAME was not."""
    from looplab.serve import launch

    assert not hasattr(launch, "_canonical_json"), "the colliding name came back"
    assert launch._lenient_json_bytes({"p": Path("/tmp/x")}) == b'{"p":"/tmp/x"}'
    # ...and it does NOT pretend to be strict: a non-finite number passes through here.
    assert b"NaN" in launch._lenient_json_bytes({"m": float("nan")})


# ------------------------------------------------------------------ the finite-metric contract

@pytest.mark.parametrize("value,expected", [
    (1, 1.0), (2.5, 2.5), (0, 0.0), (-3, -3.0),
    (True, None), (False, None),          # bool is an int subclass; not a metric
    ("1.0", None), (None, None), ([], None),
    (float("nan"), None), (float("inf"), None), (float("-inf"), None),
])
def test_finite_metric_coerces_or_returns_None(value, expected):
    assert finite_metric(value) == expected


def test_an_arbitrary_precision_json_integer_is_rejected_without_raising():
    """A hand-edited or corrupt log can hold `10**400`. It is not representable by the float-valued
    selection contract, and rejecting it must not brick the fold that is trying to reject it."""
    assert finite_metric(10 ** 400) is None
    assert is_usable_metric(10 ** 400) is False


def test_the_coercer_and_the_predicate_agree_on_every_value():
    corpus = [1, 2.5, 0, -3, True, False, "1.0", None, [], {}, float("nan"), float("inf"),
              10 ** 400, -(10 ** 400)]
    for value in corpus:
        assert (finite_metric(value) is not None) is is_usable_metric(value), (
            f"the coercer and the predicate disagree on {value!r}")


@pytest.mark.parametrize("module", ["looplab.events.replay", "looplab.search.speculation_quality"])
def test_both_float_valued_readers_are_the_SAME_object(module):
    """Identity, not equality: a separately-declared twin would pass an equality check today and
    drift the next time either rule is edited."""
    import importlib

    assert getattr(importlib.import_module(module), "_finite_metric") is finite_metric


def test_the_BOOL_variant_no_longer_shares_the_name():
    """`if _finite_metric(x):` is valid under both old spellings and means opposite things: the bool
    one answers "is this acceptable", the float one is falsy for a legitimate metric of 0.0."""
    from looplab.engine import memory

    assert not hasattr(memory, "_finite_metric"), "the colliding name came back"
    assert memory._is_finite_metric(0.0) is True
    assert finite_metric(0.0) == 0.0
    assert not finite_metric(0.0), "0.0 is falsy — which is why the two contracts must not share a name"


def test_the_two_strict_json_NORMALIZERS_stay_separate():
    """`speculation_calibration._strict_json_value` RAISES on a non-JSON value because it builds a
    receipt preimage that must fail closed; `coverage._projection_value` coerces to `str` because it
    builds a best-effort analytics token that must never fail a run. Recorded as a decision: merging
    them would either brick analytics or weaken a receipt."""
    from looplab.search import coverage, speculation_calibration

    with pytest.raises(ValueError):
        speculation_calibration._strict_json_value({"p": Path("/tmp/x")})
    assert coverage._projection_value(Path("/tmp/x")) == "/tmp/x"
    assert coverage._projection_value(float("nan")) == "nan"
