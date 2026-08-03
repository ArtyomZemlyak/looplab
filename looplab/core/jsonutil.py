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
