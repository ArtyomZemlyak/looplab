"""The writer and the reader agree about what a STATED token total is.

`events/token_spend.py::_tokens_of` deliberately preserves a stated ZERO: a fully cache-served turn
legitimately bills 0 against a 12,000-token prompt, and re-deriving prompt+completion would overwrite
the billed number. Its `_states_zero` is a PRESENCE test for exactly that reason.

THE WRITER RAN A TRUTHINESS CHAIN AND WAS WRONG IN BOTH DIRECTIONS. `core/tracing.py::_norm_usage`
read `_token_int(t.get("total_tokens") or t.get("total") or (p + c))`, so:

  * a stated 0 is FALSY and fell through to `p + c` — the cache-served case the reader defends was
    UNWRITABLE, and that rule was guarding a state no span could hold;
  * a junk or negative total ("n/a", -1) is TRUTHY, and `_token_int` maps it to 0 — so the only zero
    the reader could ever meet was MANUFACTURED, and honouring it dropped prompt+completion from
    `attributed`, under-reporting the call against a ledger whose `_normalize_usage` records the sum.

NEVER REACHED ON THIS BOX: scanned 2026-08-30 over every `spans.jsonl` in `runs/` — 1.9 GB across 12
runs — and ZERO spans carry a total of 0. This is a latent write-side defect fixed on the reader's
terms, not a repair of recorded numbers; every existing span normalizes identically.

TWO SPELLINGS ARE UNAVOIDABLE — `core` may not import `events` — so the agreement is driven here,
end to end, rather than asserted in a comment.
"""
from __future__ import annotations

import math

import pytest

from looplab.core.tracing import _norm_usage
from looplab.events.token_spend import token_spend_by_phase


def _span(usage):
    return {"kind": "generation", "span_id": "s1", "parent_id": None,
            "attributes": {"op": "chat", "phase": "propose", "usage": _norm_usage(usage)}}


def _attributed(usage):
    """What the CLI would report for one call whose provider payload was `usage`."""
    return token_spend_by_phase([_span(usage)])["attributed"]


def test_a_stated_ZERO_survives_the_writer_and_is_honoured_by_the_reader():
    """The cache-served turn. Mutation: restore the `or` chain and the written total becomes 12,000,
    so the reader's stated-zero rule is defending a case that can never arrive."""
    written = _norm_usage({"prompt_tokens": 12000, "completion_tokens": 0, "total_tokens": 0})
    assert written["total"] == 0, (
        "a provider that bills 0 against a cached prompt has STATED a figure; replacing it with "
        f"prompt+completion overwrites the billed number, got {written}")
    assert _attributed({"prompt_tokens": 12000, "completion_tokens": 0, "total_tokens": 0}) == 0


def test_a_JUNK_total_no_longer_manufactures_a_zero():
    """The half that actually cost accuracy. Mutation: `_token_int` the raw value again and "n/a"
    becomes 0, which the reader then honours — 400 real tokens reported as 0."""
    written = _norm_usage({"prompt_tokens": 300, "completion_tokens": 100, "total_tokens": "n/a"})
    assert written["total"] == 400, (
        f"an unreadable figure is not a figure of zero; fall back to the parts, got {written}")
    assert _attributed({"prompt_tokens": 300, "completion_tokens": 100, "total_tokens": "n/a"}) == 400


@pytest.mark.parametrize("bad", [-1, True, False, float("nan"), float("inf"), None, "", {}])
def test_every_non_figure_falls_back_to_the_parts(bad):
    """Negative, boolean, non-finite, absent-ish and unparseable are all NOT stated figures.

    `True` is in here on purpose: `isinstance(True, int)` holds, so a bare int check would read a
    boolean as a stated total of 1. Mutation: drop the `isinstance(raw, bool)` clause."""
    written = _norm_usage({"prompt_tokens": 300, "completion_tokens": 100, "total_tokens": bad})
    assert written["total"] == 400, f"{bad!r} must not be read as a stated total, got {written}"


def test_a_real_total_that_DISAGREES_with_its_parts_is_preserved():
    """The rule the docstring has always stated: a provider billing cached prompt tokens differently
    reports a total its parts do not add to, and re-deriving would overwrite the billed number.

    Mutation: always return `p + c` and this reads 400 instead of the billed 250."""
    assert _norm_usage({"prompt_tokens": 300, "completion_tokens": 100,
                        "total_tokens": 250})["total"] == 250


def test_an_ABSENT_total_is_still_the_sum():
    """Unchanged behaviour, and the case every real span on this box takes. Mutation: return None
    from `_stated_total` unconditionally is indistinguishable here — which is why the tests above
    carry the discriminating cases."""
    assert _norm_usage({"prompt_tokens": 300, "completion_tokens": 100})["total"] == 400
    assert _norm_usage({"prompt": 7, "completion": 3})["total"] == 10


def test_the_short_form_key_is_read_too():
    """`{prompt,completion,total}` is our own shape and must obey the same rule."""
    assert _norm_usage({"prompt": 12000, "completion": 0, "total": 0})["total"] == 0
    assert _norm_usage({"prompt": 300, "completion": 100, "total": "n/a"})["total"] == 400
