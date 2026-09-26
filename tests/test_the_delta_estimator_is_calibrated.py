"""A cut stream's completion side, priced from a ratio this proxy measured rather than a guess of 1.

The open item (`meter-delta-estimator-is-uncalibrated`, closed 2026-09-18) held the measurement:
over 4,874 complete streams carrying both numbers, `deltas / completion_tokens` has median 0.156
under 100 tokens, 0.803 at 1k-5k and 0.996 above 20k. One token per delta is therefore right for the
runaways on record (226k-238k deltas) and about 5x low for a short tool-call stream -- a different
instrument from the one the old comment described.

It was deferred because re-pricing mid-campaign would charge the tasks before and after by two
different instruments. That reason expired with the stand on 2026-09-10; this lands between
campaigns, which is what the item asked for.

The three properties these tests hold are the ones that make an in-process calibration safe: it
does not speak without evidence, it never goes below the floor it replaces, and every frame it
prices says which of the two it used.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

PROXY = Path(__file__).resolve().parents[1] / "benchmarks" / "meter" / "proxy.py"
_spec = importlib.util.spec_from_file_location("meter_proxy_under_test", PROXY)
proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proxy)

FLOOR = "counted_from_forwarded_deltas"
CALIBRATED = "estimated_from_calibrated_deltas"


def test_with_no_evidence_it_charges_exactly_what_it_charged_before():
    """`PromptTokens`'s rule, applied to the other side: a proxy restarted into a cut stream
    under-reports rather than invents. Until a bucket has streams of its own the number and the
    basis are both the historical ones, so this change is inert on a fresh process."""
    tpd = proxy.TokensPerDelta()
    tokens, basis, ratio, calls = tpd.estimate(50)
    assert (tokens, basis, ratio, calls) == (50, FLOOR, None, 0)


def test_one_priced_stream_is_not_a_ratio():
    """`MIN_CALLS` exists because a ratio from one stream is a rumour: the same abort would be
    priced differently depending on which single call happened to precede it."""
    tpd = proxy.TokensPerDelta()
    tpd.observe(50, 320)
    assert tpd.estimate(50)[1] == FLOOR
    for _ in range(proxy.TokensPerDelta.MIN_CALLS - 1):
        tpd.observe(50, 320)
    assert tpd.estimate(50)[1] == CALIBRATED


def test_a_short_stream_is_priced_at_the_short_ratio():
    """The measurement's own number: under 100 tokens a delta is worth about six."""
    tpd = proxy.TokensPerDelta()
    for _ in range(proxy.TokensPerDelta.MIN_CALLS):
        tpd.observe(50, 320)
    tokens, basis, ratio, _calls = tpd.estimate(50)
    assert basis == CALIBRATED and ratio == 6.4 and tokens == 320


def test_length_buckets_do_not_lend_each_other_their_ratios():
    """The ratio is a FUNCTION OF LENGTH -- 0.156 short against 0.996 long -- so a scalar would be
    wrong in both directions at once. A calibrated short bucket must leave a long stream on the
    floor until long streams have been seen."""
    tpd = proxy.TokensPerDelta()
    for _ in range(proxy.TokensPerDelta.MIN_CALLS):
        tpd.observe(50, 320)                       # only the shortest bucket has evidence
    assert tpd.estimate(30_000)[1] == FLOOR
    for _ in range(proxy.TokensPerDelta.MIN_CALLS):
        tpd.observe(30_000, 30_100)
    tokens, basis, ratio, _c = tpd.estimate(30_000)
    assert basis == CALIBRATED and 1.0 < ratio < 1.01 and tokens == 30_100
    assert tpd.estimate(50)[2] == 6.4, "the short bucket kept its own ratio"


def test_it_never_prices_BELOW_the_floor_it_replaced():
    """A floor is the honest side to be wrong on for a budget. A bucket whose streams happen to
    report fewer tokens than deltas would otherwise trade a known bias for an unknown one."""
    tpd = proxy.TokensPerDelta()
    for _ in range(proxy.TokensPerDelta.MIN_CALLS):
        tpd.observe(200, 50)                       # a ratio of 0.25 in the raw
    tokens, _basis, ratio, _c = tpd.estimate(200)
    assert ratio == 1.0 and tokens == 200


def test_only_a_stream_the_gateway_PRICED_is_evidence():
    """`observe` takes the deltas this proxy forwarded and the tokens the gateway charged. A zero on
    either side is not a cheap stream, it is a stream nobody measured."""
    tpd = proxy.TokensPerDelta()
    for _ in range(proxy.TokensPerDelta.MIN_CALLS * 2):
        tpd.observe(50, 0)
        tpd.observe(0, 320)
        tpd.observe(-5, -5)
    assert tpd.estimate(50)[1] == FLOOR


def test_zero_deltas_is_zero_tokens_and_says_so():
    """A stream that forwarded nothing is not priced by a ratio; the old path returned 0 here and
    the new one must not multiply its way into a charge."""
    assert proxy.TokensPerDelta().estimate(0) == (0, FLOOR, None, 0)


def test_the_synthesised_frame_names_which_instrument_priced_it():
    """The client's accountant never sees this proxy's log, so the frame has to carry the basis, the
    input (`meter_forwarded_deltas`, text; since 2026-09-26 `meter_forwarded_tool_call_deltas`
    beside it, and the estimate prices their sum) and the ratio. Source-pinned because the frame is assembled
    inside the streaming loop, where a test cannot reach it without a live upstream."""
    src = PROXY.read_text(encoding="utf-8")
    for field in ('"meter_completion_tokens_basis": completion_basis',
                  '"meter_forwarded_deltas": deltas',
                  '"meter_tokens_per_delta": tokens_per_delta'):
        assert field in src, field
    # And the prose must not contradict the field: the unconditional "FLOOR" sentence is gone.
    assert 'completion_tokens is a FLOOR counted from forwarded deltas and ' not in src
    assert 'if completion_basis == "counted_from_forwarded_deltas"' in src


def test_the_calibrator_observes_where_both_numbers_exist():
    """The ratio's only evidence is a stream carrying BOTH the forwarded deltas and the gateway's
    own `completion_tokens`, which is the usage frame -- the same place `prompt_scale` learns."""
    src = PROXY.read_text(encoding="utf-8")
    # Text AND tool-call fragments since 2026-09-26 -- the counter the estimate prices with; driven
    # end to end by `tests/test_meter_counts_tool_call_deltas.py`.
    assert "self.server.tokens_per_delta.observe(deltas + tool_call_deltas, pout)" in src
    assert "self.tokens_per_delta = TokensPerDelta()" in src
