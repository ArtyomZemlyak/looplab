"""Canonical JSON bytes for receipt and digest preimages (doc 25 SE-08).

`speculation_calibration` and `speculation_quality` each declared this function identically, and both
use it to build a PREIMAGE — the bytes a receipt's digest is taken over. That is why every option
here is load-bearing rather than stylistic, and why a drifting copy is worse than a duplicated one:
two spellings of "canonical" produce two digests for one logical value, and a receipt written by one
reader stops verifying for the other with nothing to say why.

* ``sort_keys`` — object order is not semantic in JSON, so an unsorted dump makes the digest depend
  on insertion order.
* ``separators=(",", ":")`` — whitespace likewise carries no meaning and would otherwise vary.
* ``ensure_ascii=False`` — one encoding of a non-ASCII string, chosen explicitly.
* ``allow_nan=False`` — the decisive one. `json.dumps` emits bare `NaN`/`Infinity` by default, which
  no strict JSON reader accepts, so a receipt could be minted over bytes nothing else can parse.

`serve/launch.py` keeps a deliberately LOOSER sibling under a different name (`_lenient_json_bytes`):
it hashes launch payloads that may legitimately carry non-JSON values, coerces them with
``default=str`` and never raises. Sharing one name for both contracts is what made that difference
invisible.
"""
from __future__ import annotations

import hashlib
import json


def canonical_json(value: object) -> bytes:
    """Strict canonical JSON bytes, or ``ValueError`` when *value* has no canonical form.

    Raising is the point. A caller minting a receipt must not get a digest over a fallback rendering
    of something it could not represent — it must find out here, before the receipt exists.
    """
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise ValueError(f"value is not canonical JSON: {exc}") from exc


DIGEST_TEXT_CAP = 131_072
"""Preimage byte budget shared by the two BOUNDED identity minters (doc 25 CO-08).

`idea_proposal_digest` and `card_action_digest` both refuse to mint over a preimage larger than this.
It is a fail-closed budget on untrusted agent output, not a serialization limit: the payloads those
two hash come from an LLM, so without a ceiling one pathological proposal turns every later identity
comparison into a 100 KB+ hash. `stable_advisory_ref` deliberately has NO cap — its callers pass an
already-sanitized, deliberately small identity projection, so a size refusal there could only drop a
well-formed advisory rather than reject an oversized one.
"""


def canonical_json_digest(value: object, *, prefix: str = "",
                          cap: int | None = None) -> "str | None":
    """``prefix + sha256(canonical_json(value))``, or ``None`` when there is no canonical form.

    The dump/hash TAIL of the identity minters, which was written out four times (doc 25 CO-08). The
    preimages stay owned by their call sites — each one's bounding/validation walker is versioned and
    frozen, and merging those would be a wire change. What is shared here is the part that must never
    differ between them: the canonical encoding, the hash, and the answer when a value cannot be
    encoded at all.

    Failing closed (``None``) rather than raising is the contract every capped caller already had: a
    digest is an identity, so minting one over a fallback rendering of something that could not be
    represented would let two different payloads claim the same receipt. A caller that must RAISE
    instead calls `canonical_json` and hashes the bytes itself — `fitness.verifier_evidence_digest`
    does exactly that, because its snapshot is built from already-validated scalars and an
    unencodable value there is a bug rather than untrusted input.
    """
    try:
        encoded = canonical_json(value)
    except ValueError:
        return None
    if cap is not None and len(encoded) > cap:
        return None
    return prefix + hashlib.sha256(encoded).hexdigest()
