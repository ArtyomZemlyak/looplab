"""One canonical-JSON digest tail, and a map of the finiteness rules (doc 25 CO-08, CO-09).

CO-08 — four identity minters each wrote out `json.dumps(sort_keys, separators, allow_nan=False)`,
sha256, prefix, and (two of them) a 131_072-byte cap. Every one of those options is load-bearing on a
DIGEST: it is the preimage a receipt is taken over, so two spellings of "canonical" mean two digests
for one logical value, and a receipt written by one reader silently stops verifying for the other.
The preimages themselves are frozen and stay owned by their call sites; only the tail is shared.

CO-09 — core carries several "is this a usable number" rules that are NOT interchangeable, and
`parse.to_float` claimed to be "the one spelling" of a job it had never owned alone. Only the
genuinely identical pair merged; the rest are now mapped, because a reader picking the wrong one gets
a durable bug (a metric that accepts `"3.5"`) rather than a type error.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math

import pytest

from looplab.core import advisory_payloads, fitness, models, parse, profile
from looplab.core.jsonutil import DIGEST_TEXT_CAP, canonical_json, canonical_json_digest


# ------------------------------------------------------------------ CO-08: one dump/hash tail

def _reference(payload, prefix="", cap=None):
    """The spelling every minter used before the extraction, written out once here."""
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        return None
    if cap is not None and len(encoded) > cap:
        return None
    return prefix + hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize("payload", [
    {}, {"a": 1}, {"b": [1, 2, {"z": None}]}, {"unicode": "цель — μ ✓"},
    {"neg_zero": -0.0}, {"deep": {"x": {"y": [True, False, 3.5]}}},
    {"b": 1, "a": 2},                      # key order must not reach the digest
])
def test_the_shared_tail_is_byte_identical_to_what_it_replaced(payload):
    assert canonical_json_digest(payload) == _reference(payload)


def test_every_option_that_makes_the_encoding_canonical_is_still_applied():
    """Each of these would silently produce a SECOND digest for one logical value."""
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}', "unsorted keys"
    assert canonical_json({"a": 1, "b": 2}) == b'{"a":1,"b":2}', "whitespace"
    assert canonical_json({"k": "é"}) == '{"k":"é"}'.encode("utf-8"), "escaped non-ASCII"
    # The decisive one: bare NaN/Infinity is not JSON, so a receipt could be minted over bytes no
    # strict reader can parse.
    with pytest.raises(ValueError):
        canonical_json({"k": float("nan")})
    with pytest.raises(ValueError):
        canonical_json({"k": math.inf})


def test_a_value_with_no_canonical_form_fails_closed_rather_than_falling_back():
    assert canonical_json_digest({"k": float("nan")}) is None
    assert canonical_json_digest({"k": object()}) is None
    # ...but an arbitrary-precision int is NOT one of those cases: JSON numbers are unbounded, so
    # `10**400` has an exact decimal form and mints a real digest. The float-domain readers reject it
    # (see the CO-09 map); the ENCODER must not, or a durable payload would lose its identity.
    assert canonical_json_digest({"k": 10 ** 400}) == _reference({"k": 10 ** 400})


def test_the_cap_refuses_rather_than_truncates():
    """Truncating would mint ONE digest for two different oversized payloads."""
    assert canonical_json_digest({"k": "x" * 10}, cap=DIGEST_TEXT_CAP) is not None
    assert canonical_json_digest({"k": "x" * (DIGEST_TEXT_CAP + 1)}, cap=DIGEST_TEXT_CAP) is None
    assert canonical_json_digest({"k": "x" * (DIGEST_TEXT_CAP + 1)}) is not None, (
        "the cap is opt-in; an uncapped caller must not start refusing")


@pytest.mark.parametrize("module,owner", [
    (models, "models"), (advisory_payloads, "advisory_payloads"), (fitness, "fitness"),
])
def test_no_minter_re_derives_the_canonical_dump(module, owner):
    source = inspect.getsource(module)
    assert "sort_keys=True" not in source, f"{owner} still spells out the canonical dump"
    assert "separators=(\",\", \":\")" not in source, f"{owner} still spells out the separators"


def test_the_cap_is_declared_once():
    for module in (models, advisory_payloads, fitness):
        assert "131_072" not in inspect.getsource(module), (
            f"{module.__name__} re-declares the preimage budget")


def test_the_idea_identity_still_answers_the_same_digest():
    """End to end through the real minter: the v1 identity is FROZEN, so this value is a wire
    contract, not an implementation detail."""
    idea = models.Idea(title="t", rationale="r", operator="mutate", params={"lr": 0.1})
    digest = models.idea_proposal_digest(idea)
    assert digest is not None and digest.startswith("idea:v1:")
    assert len(digest) == len("idea:v1:") + 64
    assert models.idea_proposal_digest(idea) == digest, "the identity must be stable"


def test_the_card_action_identity_bounds_its_preimage_before_the_shared_cap_sees_it():
    """The shared cap is a BACKSTOP here, not the working limit — and that layering is the point of
    keeping the versioned preimage at the call site. Each field has its own bound (statement length,
    param/space counts), so an oversized action is refused by the rule that knows WHICH field is
    wrong, long before a byte budget that can only say "too big"."""
    ok = models.card_action_digest("c", "s", {"operator": "mutate", "params": {}})
    assert ok is not None and ok.startswith("card-action:v2:")
    assert models.card_action_digest("c", "x" * 5_000,
                                     {"operator": "mutate", "params": {}}) is None, "statement bound"
    assert models.card_action_digest(
        "c", "s", {"operator": "mutate",
                   "params": {f"k{i}": float(i) for i in range(2_000)}}) is None, "param-count bound"
    # ...so the byte cap it passes is never the thing that fires. It still must be PASSED, because a
    # future field without its own bound would otherwise mint a 100 KB identity.
    assert "cap=_DIGEST_TEXT_CAP" in inspect.getsource(models._card_action_digest)


def test_the_advisory_ref_is_deliberately_uncapped():
    """Its callers pass an already-sanitized, deliberately small identity projection, so a size
    refusal could only ever drop a well-formed advisory."""
    source = inspect.getsource(advisory_payloads.stable_advisory_ref)
    assert "cap=" not in source
    big = advisory_payloads.stable_advisory_ref("lesson", {"k": "x" * (DIGEST_TEXT_CAP + 1)})
    assert isinstance(big, str) and big.endswith(hashlib.sha256(
        canonical_json({"k": "x" * (DIGEST_TEXT_CAP + 1)})).hexdigest())


def test_the_verifier_evidence_digest_raises_instead_of_failing_closed():
    """The one minter whose input is already-validated internal state, so an unencodable value is a
    BUG here rather than untrusted input — a silent None would hide it."""

    class _Node:
        id = 1
        attempt = 0
        metric = float("nan")          # survives the snapshot as `None`, so this alone is fine
        confirmed_mean = confirmed_std = confirmed_seeds = holdout_metric = None
        verifier_rationale = "r"

    digest = fitness.verifier_evidence_digest("min", _Node())
    assert len(digest) == 64 and digest == hashlib.sha256(
        canonical_json(fitness.verifier_evidence_snapshot("min", _Node()))).hexdigest()
    # The CALL, not the word — the docstring above it explains why the fail-closed helper is wrong here.
    source = inspect.getsource(fitness.verifier_evidence_digest)
    assert "canonical_json_digest(" not in source, (
        "routing this one through the fail-closed helper would swap a raise for a silent None")
    with pytest.raises(ValueError):
        canonical_json({"unencodable": object()})


# ------------------------------------------------------------------ CO-09: the finiteness map

def test_the_profiler_shares_the_metric_predicate_rather_than_restating_it():
    assert profile._is_number is fitness.is_usable_metric


@pytest.mark.parametrize("value", [True, False, "3.5", None, float("nan"), math.inf, -math.inf,
                                   10 ** 400, object()])
def test_the_shared_predicate_rejects_everything_the_profiler_needed_rejected(value):
    assert profile._is_number(value) is False


@pytest.mark.parametrize("value", [0, -1, 3.5, 10 ** 18, -0.0])
def test_and_still_accepts_a_real_finite_scalar(value):
    assert profile._is_number(value) is True


def test_the_profiler_keeps_the_reasons_written_down():
    """The rule is shared; the CONSEQUENCE is profiler-specific — an oversized cell takes the whole
    profiler and the leakage front-end down through `sum(nonnull)/len(nonnull)`. The shared predicate
    cannot carry that, so moving the code must not lose it."""
    source = inspect.getsource(profile)
    assert "OverflowError" in source and "degrades to categorical" in source


def test_the_one_spelling_claim_is_scoped_to_what_it_actually_owns():
    """`to_float` claimed to be "the one spelling of scalar coercion previously re-implemented per
    module" while at least six strict readers deliberately did not use it. The claim now names its
    own job — COERCING parse — and the map names the rest."""
    doc = parse.to_float.__doc__ or ""
    assert "COERCING" in doc
    assert "one spelling of scalar coercion" not in doc
    source = inspect.getsource(parse)
    for named in ("fitness.is_usable_metric", "comparison.finite_measurement",
                  "llm._safe_token_count", "tracing._token_int"):
        assert named in source, f"the contract map does not mention {named}"


def test_the_mapped_contracts_really_do_differ():
    """Driven, so the map is not just prose: each pair disagrees on a value a caller could hit."""
    from looplab.core.comparison import finite_measurement

    assert parse.to_float("3.5") == 3.5, "the coercing reader accepts wire text"
    assert fitness.finite_metric("3.5") is None, "the strict metric reader must not"

    class _Real(float):
        pass

    assert fitness.is_usable_metric(_Real(1.5)) is True, "a subclass is still a real scalar"
    assert finite_measurement(_Real(1.5)) is None, (
        "a durable comparison claim refuses a subclass that could override __lt__")
