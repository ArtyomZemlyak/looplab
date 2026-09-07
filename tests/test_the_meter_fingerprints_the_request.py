"""A re-send should be a fact in the ledger, not an inference from timing.

MEASURED 2026-09-02 (docs/56 §120, §121): `expEEc` carried two meter rows 0.20 s apart with
identical 22,313/25,966 token counts and identical cost, the second reporting 907,902 tok/s over
28.6 ms, while the engine's `spans.jsonl` held ONE generation of 182.1 s spanning both. One call by
the engine's accounting, two requests through the proxy, both priced -- $0.055 across four probes,
money the engine's budget cannot know it spent.

Four hypotheses died first: the reconciler's read order, a double `record()`, end-of-run flushing,
and corpus-wide double metering. None of them could separate a RE-SEND from two honest calls with
the same token counts, because nothing in the row described the request. `req_sha` does.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from benchmarks.meter.proxy import _request_sha  # noqa: E402


def test_the_same_bytes_give_the_same_fingerprint():
    body = b'{"model":"m","messages":[{"role":"user","content":"hello"}]}'
    assert _request_sha(body) == _request_sha(bytes(body))


def test_different_bytes_give_a_different_one():
    a = _request_sha(b'{"messages":[{"role":"user","content":"hello"}]}')
    b = _request_sha(b'{"messages":[{"role":"user","content":"hellp"}]}')
    assert a != b


def test_it_is_short_and_hex_and_not_the_body():
    body = b'{"messages":[{"role":"user","content":"a secret prompt"}]}'
    sha = _request_sha(body)
    assert len(sha) == 16 and all(c in "0123456789abcdef" for c in sha)
    assert b"secret" not in sha.encode()
    assert sha == hashlib.sha256(body).hexdigest()[:16]


def test_an_empty_body_is_empty_not_the_hash_of_nothing():
    """MUTATION GUARD: hashing b"" would give every bodyless request the same non-empty
    fingerprint, which reads in the ledger as "these are all the same request"."""
    assert _request_sha(b"") == ""
    assert _request_sha(None) == ""


def test_both_row_builders_carry_it():
    """The streamed and non-streamed paths build their rows separately -- a fingerprint on one of
    them would leave exactly half the ledger unable to answer the question it was added for."""
    src = (REPO / "benchmarks" / "meter" / "proxy.py").read_text(encoding="utf-8")
    assert src.count('"req_sha": _request_sha(') == 2, (
        "both the streaming and non-streaming row builders must stamp req_sha")
