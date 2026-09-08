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

from looplab.core import advisory_payloads, cards, fitness, models, parse, profile
from looplab.core.jsonutil import (DIGEST_TEXT_CAP, bounded_int, canonical_json,
                                   canonical_json_digest, valid_digest_ref)
from looplab.core.receipts import (bounded_receipt_count, receipt_field_set, receipt_payload,
                                   receipt_presence)


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


# `cards` carries the two minters that used to live in `models` (doc 25 CO-02 moved the idea/card
# identity subsystem into `core/cards.py`). It is listed HERE and not merely left to the `models`
# entry, because after that move `models` no longer contains a minter at all — the assertion would
# still pass on it while nothing checked the module that now owns the dump.
@pytest.mark.parametrize("module,owner", [
    (models, "models"), (cards, "cards"), (advisory_payloads, "advisory_payloads"),
    (fitness, "fitness"),
])
def test_no_minter_re_derives_the_canonical_dump(module, owner):
    source = inspect.getsource(module)
    assert "sort_keys=True" not in source, f"{owner} still spells out the canonical dump"
    assert "separators=(\",\", \":\")" not in source, f"{owner} still spells out the separators"


def test_the_cap_is_declared_once():
    for module in (models, cards, advisory_payloads, fitness):
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


# --- EV-04: one READER for the format the minter above writes ------------------------------------
#
# The same predicate — "is this a str of prefix + exactly 64 lowercase hex?" — was hand-rolled at ~20
# sites, four times inside `_on_run_started` alone. These pin the clauses that a re-derived copy is
# most likely to drop, because each drop admits something that is not a digest this repo ever minted.

_H = "a" * 64


@pytest.mark.parametrize("value,prefix,why", [
    (None, "", "a hand-edited log can put any JSON type here"),
    (5, "", "an int has no .startswith, and raising inside the fold kills every replay"),
    (True, "", "bools are ints, and `isinstance(True, int)` is the trap next door"),
    ([], "", "a list is len()-able and iterable, so len/membership alone would not reject it"),
    (list(_H), "", "a 64-element list of hex chars passes a len+membership check without isinstance"),
])
def test_a_non_string_is_refused_rather_than_raising(value, prefix, why):
    assert valid_digest_ref(value, prefix=prefix) is False, why


@pytest.mark.parametrize("value,prefix,why", [
    ("a" * 63, "", "a short body is a DIFFERENT identity, not a near match"),
    ("a" * 65, "", "a long body likewise"),
    ("sha256:" + "a" * 63, "sha256:", "prefix match alone would accept a truncated digest"),
    ("sha256:" + "a" * 65, "sha256:", "and a padded one"),
    ("A" * 64, "", "hexdigest() emits lowercase; two spellings of one digest break identity"),
    ("sha256:" + "A" * 64, "sha256:", "same, under a prefix"),
    ("g" * 64, "", "non-hex characters of the right length"),
    ("SHA256:" + _H, "sha256:", "the prefix itself is case-sensitive"),
    (" sha256:" + _H, "sha256:", "leading whitespace is not stripped for us"),
    ("sha256:" + _H + " ", "sha256:", "nor trailing"),
    ("memo:sha256:" + _H, "sha256:", "a different namespace is a different ref"),
    (_H, "sha256:", "a bare digest is not a prefixed one"),
    ("sha256:" + _H, "", "and a prefixed one is not bare"),
])
def test_the_exact_shape_is_required(value, prefix, why):
    assert valid_digest_ref(value, prefix=prefix) is False, why


@pytest.mark.parametrize("prefix", ["", "sha256:", "memo:sha256:", "idea:v1:", "card-action:v2:"])
def test_what_the_minter_produces_is_what_the_reader_accepts(prefix):
    """The property that makes sharing these two worth it: round-trip, not two independent rules."""
    minted = canonical_json_digest({"any": ["payload", 1, None]}, prefix=prefix)
    assert isinstance(minted, str)
    assert valid_digest_ref(minted, prefix=prefix) is True


def test_every_prefixed_call_site_reads_through_the_shared_predicate():
    """The regression that matters is a re-derived copy, not a wrong answer here. Two spellings are
    deliberately EXEMPT and must stay that way, so they are named rather than merely absent."""
    from _source_scan import iter_sources

    # Exemptions are matched on the REASON, not the filename. Exempting whole files would let a
    # genuinely re-derived 64-hex predicate slip in beside an unrelated random-id check — which is
    # exactly what happened to the first draft of this test.
    non_digest_lengths = ("== 32", "!= 32", "{12, 32}")

    offenders = []
    for path, text in iter_sources():
        if path.name == "jsonutil.py":
            continue                                   # the canonical definition
        lines = text.split("\n")
        for index, line in enumerate(lines, 1):
            if "0123456789abcdef" not in line:
                continue
            # A length check may sit on the line above the character-set check, so read the pair.
            window = (lines[index - 2] if index >= 2 else "") + line
            if "ABCDEF" in window:
                continue        # HTTP input normalizer: accepts either case, then lowercases
            if any(marker in window for marker in non_digest_lengths):
                continue        # 12/32-hex RANDOM ids (review link ids, uuid4().hex) — not digests
            offenders.append(f"{path.as_posix()}:{index}")
    assert not offenders, (
        "these re-derive the digest predicate instead of calling `valid_digest_ref`; if the site is "
        f"genuinely not a 64-hex digest, exempt it here with the reason: {offenders}")


# --- EV-04: one bounded-int rule, where replay.py had 21 hand-rolled bounds ----------------------
#
# The digest half of EV-04 shipped as `valid_digest_ref`; the SCALAR half stayed hand-rolled per
# site — and in two spellings, `type(x) is int` at most sites and an `isinstance`/bool pair at
# others. `bounded_int` is the survivor. The fold-side behaviour these decide is driven in
# `tests/test_events_replay.py`; this pins the leaf's own truth table.

def test_a_bool_is_never_a_bounded_int():
    """The same trap as the receipt count: `isinstance(True, int)` is True, so an isinstance-built
    bound accepts `{"priority": true}` and the value then sorts and arithmetics as 1."""
    assert bounded_int(True, 0, 10) is False
    assert bounded_int(False, 0, 10) is False


def test_an_int_subclass_is_never_a_bounded_int():
    """A subclass can override the very comparisons the bound is expressed in, so a bound it can
    talk its way past is not a bound. JSON cannot produce one, which is why the two spellings this
    replaced could disagree here for years without any log distinguishing them."""

    class Sneaky(int):
        def __le__(self, other):        # would answer "in range" for any range
            return True

    assert Sneaky(10 ** 9) <= 5, "the retired isinstance spelling would have accepted this"
    assert bounded_int(Sneaky(10 ** 9), 0, 5) is False


@pytest.mark.parametrize("value", [None, "5", 5.0, [], {}, object()])
def test_a_non_integer_is_never_a_bounded_int(value):
    assert bounded_int(value, 0, 10) is False


@pytest.mark.parametrize("value,lo,hi,expected", [
    (0, 0, 10, True), (10, 0, 10, True), (5, 0, 10, True),          # both ends INCLUSIVE
    (-1, 0, 10, False), (11, 0, 10, False),
    (0, 0, 0, True), (1, 0, 0, False),
    (-5, -10, -1, True), (0, -10, -1, False),                       # a negative range is expressible
    (255, 0, 255, True), (256, 0, 255, False),                      # the `< 256` site, restated
])
def test_the_bounded_int_range_is_inclusive_at_both_ends(value, lo, hi, expected):
    assert bounded_int(value, lo, hi) is expected


# --- EM-12: one bounded-receipt-count rule, where there had been two -----------------------------
#
# The ~8 receipt validators share a leaf guard on a single count field, and it had DIVERGED:
# `claims_health`/`memory` spelled it `type(v) is int`, `concept_steward` spelled it
# `isinstance(v, int) and not isinstance(v, bool)`. Same concept, two rules. These pin the survivor.

def test_a_bool_is_never_a_receipt_count():
    """The trap next door: `isinstance(True, int)` is True, so a receipt reading
    `{"rows_total": true}` passes any isinstance-built guard and then arithmetics as 1."""
    assert bounded_receipt_count(True, 10) is False
    assert bounded_receipt_count(False, 10) is False


def test_an_int_subclass_is_never_a_receipt_count():
    """The divergence itself. The two spellings agreed on everything JSON can produce and disagreed
    ONLY here, so no shipped receipt distinguished them — which is exactly why it went unnoticed."""

    class Count(int):
        pass

    assert isinstance(Count(5), int) and not isinstance(Count(5), bool), (
        "the retired spelling would have accepted this")
    assert bounded_receipt_count(Count(5), 10) is False, "the strict spelling refuses it"


@pytest.mark.parametrize("value", [None, "5", 5.0, [], {}, object()])
def test_a_non_integer_is_never_a_receipt_count(value):
    assert bounded_receipt_count(value, 10) is False


@pytest.mark.parametrize("value,maximum,expected", [
    (0, 10, True), (10, 10, True), (5, 10, True),
    (-1, 10, False), (11, 10, False), (0, 0, True), (1, 0, False),
])
def test_the_bound_is_inclusive_and_non_negative(value, maximum, expected):
    assert bounded_receipt_count(value, maximum) is expected


# The capsule validators moved to their own module (doc 25 EM-10); the registry follows the code.
# This drift-check earned its keep: the move fired the "no longer defines" assertion below rather
# than silently leaving two of the four validators unguarded.
_RECEIPT_VALIDATORS = {
    "claims_health.py": ("_safe_claim_read_segment",),
    "concept_capsules.py": ("_capsule_concept_evidence_completeness", "capsule_completeness"),
    "concept_steward.py": ("_concept_source_receipt",),
}


def test_the_receipt_validators_do_not_re_derive_the_count_guard():
    """Scoped to the named validator FUNCTIONS, not their files.

    A file-wide scan was the first attempt and it was wrong three ways: it flagged unbounded
    coercions that are not receipt counts, matched `int)` inside `fingerprint)`, and reported its own
    explanatory comment. A guard that cries wolf gets exemptions bolted on until it guards nothing.

    Only the LEAF is shared. `_concept_source_receipt`'s consistency predicate is deliberately left
    alone — it is domain logic carrying a ten-line comment about reading both source axes, and
    folding that into a generic spec table would hide the part a reader needs."""
    import ast
    from pathlib import Path

    pkg = Path(__file__).resolve().parents[1] / "looplab" / "engine"
    offenders = []
    for filename, functions in _RECEIPT_VALIDATORS.items():
        text = (pkg / filename).read_text(encoding="utf-8-sig")
        tree = ast.parse(text)
        wanted = {name: node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name in functions
                  for name in [node.name]}
        assert set(wanted) == set(functions), (
            f"{filename} no longer defines {set(functions) - set(wanted)} — this registry and the "
            "receipt validators have drifted apart")
        for name, node in wanted.items():
            # Comments are not in the AST, so unparsing compares CODE only.
            body = ast.unparse(node)
            if "not isinstance" in body and ", int)" in body:
                offenders.append(f"{filename}::{name}")
    assert not offenders, (
        "a receipt validator re-derives the bounded-count guard instead of calling "
        f"`bounded_receipt_count`: {offenders}")


# --- EM-12, the other half: the WRITER and the READER share one field-set declaration -----------
#
# EM-12's first pass shared the leaf and left this open in its own words: "Nothing yet forces a
# receipt's WRITER and its READER to agree on the field set." Both ends spelled it out separately —
# the writer as literal dict keys, the reader as a local `keys = (...)` tuple, one of them in
# another module — so a field added at one end was invisible at the other, silently in the
# optimistic direction. `receipt_field_set` is that declaration; `receipt_payload` and
# `receipt_presence` are how each end is made to consume it.

def test_a_receipt_declaration_refuses_a_shape_that_could_not_be_read():
    """Refusals rather than coercion: a declaration is read once at import and then trusted."""
    with pytest.raises(ValueError):
        receipt_field_set()                                     # a receipt with no fields
    with pytest.raises(ValueError):
        receipt_field_set("a", "a")                             # writer emits one key, reader counts two
    with pytest.raises(ValueError):
        receipt_field_set("a", "")
    with pytest.raises(ValueError):
        receipt_field_set("a", 7)
    assert receipt_field_set("b", "a") == ("b", "a"), "declaration order is preserved, not sorted"


def test_a_writer_cannot_emit_a_field_it_did_not_declare():
    """THE forcing property. A writer that grows a receipt without declaring the new field raises
    where the row is built, instead of shipping a durable row its reader reads as absent."""
    fields = receipt_field_set("rows_total", "rows_omitted")
    assert receipt_payload(fields, {"rows_omitted": 1, "rows_total": 3}) == {
        "rows_total": 3, "rows_omitted": 1}, "the row is built in DECLARATION order"

    with pytest.raises(ValueError, match="undeclared"):
        receipt_payload(fields, {"rows_total": 3, "rows_omitted": 1, "rows_quarantined": 0})
    with pytest.raises(ValueError, match="missing"):
        receipt_payload(fields, {"rows_total": 3})


@pytest.mark.parametrize("row,expected", [
    ({}, "absent"),                                   # predates the receipt: legacy default
    ({"other": 1}, "absent"),
    ({"a": 1}, "partial"),                            # no writer produces this: fail closed
    ({"a": 1, "b": 2}, "complete"),
    ({"a": None, "b": None}, "complete"),             # PRESENCE, not validity
    (None, "absent"),
    ([], "absent"),
])
def test_the_reader_side_presence_is_all_or_nothing(row, expected):
    assert receipt_presence(row, receipt_field_set("a", "b")) == expected


def _capsule():
    from looplab.engine.concept_capsules import build_concept_capsule

    return build_concept_capsule(run_id="r1", fingerprint=["t"], direction="min",
                                 concepts=["c1"], concept_outcomes={"c1": 1.0}, task_id="t1")


def test_the_capsule_writer_emits_exactly_what_its_reader_requires():
    """Driven both ways over a REAL capsule: every declared field is emitted, the reader accepts
    the row, and removing any ONE of them makes the reader fail closed rather than read the rest."""
    from looplab.engine import concept_capsules

    fields = concept_capsules.CAPSULE_EVIDENCE_RECEIPT
    capsule = _capsule()
    assert set(fields) <= set(capsule), "the writer skipped a declared field"
    assert concept_capsules._capsule_concept_evidence_completeness(capsule) is not None

    for name in fields:
        broken = {key: value for key, value in capsule.items() if key != name}
        assert concept_capsules._capsule_concept_evidence_completeness(broken) is None, (
            f"dropping {name} left a TORN receipt readable — the three landed together")


def test_the_later_observed_marker_is_declared_apart_because_it_is_additive():
    """Why `concept_evidence_observed` is its OWN declaration rather than a fourth field: it was
    added after the triple, so a v2 row that predates it must stay readable. It is still READ —
    dropping it from a tombstone (observed=false, empty collections) changes the answer from
    "the classifier observed zero memberships" to "unreadable", which are different facts."""
    from looplab.engine import concept_capsules

    tombstone = concept_capsules.build_concept_capsule(
        run_id="r1", fingerprint=[], direction="min", concepts=[], task_id="t1",
        concept_evidence_observed=False)
    assert set(concept_capsules.CAPSULE_OBSERVED_RECEIPT) <= set(tombstone)
    assert concept_capsules._capsule_concept_evidence_completeness(tombstone) == (0, 0, False, False)

    without = {key: value for key, value in tombstone.items()
               if key not in concept_capsules.CAPSULE_OBSERVED_RECEIPT}
    assert concept_capsules._capsule_concept_evidence_completeness(without) is None


@pytest.mark.parametrize("stem", ["fingerprint", "concepts", "concept_outcomes"])
def test_each_bounded_collection_carries_its_declared_completeness_triple(stem):
    from looplab.engine import concept_capsules

    capsule = _capsule()
    fields = concept_capsules.capsule_completeness_receipt(stem)
    assert set(fields) <= set(capsule)
    included = len(capsule[stem])
    assert concept_capsules.capsule_completeness(capsule, stem, included) is not None
    for name in fields:                                  # a torn triple is corrupt, never partial truth
        broken = {key: value for key, value in capsule.items() if key != name}
        assert concept_capsules.capsule_completeness(broken, stem, included) is None


def test_the_source_receipt_declaration_is_the_same_object_at_both_ends():
    """The widest gap of the three: the writer is `concept_capsules.capsule_source_summary` and the
    reader is `concept_steward._concept_source_receipt`, in another module. Identity, not equality —
    a second module-local copy that happened to be equal today is exactly the drift EM-12 named."""
    from looplab.engine import concept_capsules, concept_steward

    assert concept_steward.CAPSULE_SOURCE_COUNTS is concept_capsules.CAPSULE_SOURCE_COUNTS


def test_the_cross_module_source_receipt_round_trips():
    """Driven: the aggregate the writer really produces is read as KNOWN by the steward's validator,
    and every declared count survives the crossing."""
    from looplab.engine.concept_capsules import CAPSULE_SOURCE_COUNTS, capsule_source_summary
    from looplab.engine.concept_steward import _concept_source_receipt

    summary = capsule_source_summary([_capsule()])
    assert set(CAPSULE_SOURCE_COUNTS) <= set(summary)

    receipt = _concept_source_receipt(summary, [])
    assert receipt["receipt_known"] is True, "the reader could not read what the writer wrote"
    assert all(receipt[name] == summary[name] for name in CAPSULE_SOURCE_COUNTS)

    # Drop one declared count from the aggregate: the reader must NOT go on calling the receipt
    # known. This is the failure the shared declaration exists to make impossible.
    for name in CAPSULE_SOURCE_COUNTS:
        torn = {key: value for key, value in summary.items() if key != name}
        assert _concept_source_receipt(torn, [])["receipt_known"] is False
