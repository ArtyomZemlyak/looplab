"""Where the tokens went, and the four ways a breakdown lies.

MEASURED on real runs and the reason this shipped: the Developer BUILD (`plan` + `stages` +
`card_build`) is 61.0 % of `rubertlite-dr-unified-v9`'s 108,534,235 tokens and 63.3 % of
`rubertlite-dr-unified-v8`'s 201,041,498, while `deep_research` is 0.2-0.3 %. Nothing in the product
could show that: `llm_usage` records TOTALS with no phase, and no CLI computed the split.

The total and the split come from different places on purpose — the durable ledger is the
denominator, the destructible spans supply the attribution, and the gap is printed. See the module
docstring for why the phase is NOT stamped on `llm_usage` instead (the append runs from an outbox
drain, so the phase at append time is the DRAINING phase).

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import pytest

from looplab.events.token_spend import PHASE_UNATTRIBUTED, token_spend_by_phase


def _gen(phase=None, total=None, prompt=None, completion=None):
    usage = {}
    if total is not None: usage["total"] = total
    if prompt is not None: usage["prompt"] = prompt
    if completion is not None: usage["completion"] = completion
    attributes = {"usage": usage}
    if phase is not None: attributes["phase"] = phase
    return {"kind": "generation", "attributes": attributes}


def test_the_shape_of_a_real_run_is_reproduced():
    """A miniature of v9's real profile: the build dominates and deep research is a rounding error."""
    out = token_spend_by_phase(
        [_gen("plan", 28_027_840), _gen("propose", 27_436_262), _gen("stages", 19_533_608),
         _gen("card_build", 18_636_924), _gen("deep_research", 308_166)],
        ledger_total=93_942_800)

    assert [r["phase"] for r in out["rows"]][:2] == ["plan", "propose"], (
        "MUTATION: sort ascending -> deep_research leads and the headline inverts")
    build = sum(r["tokens"] for r in out["rows"] if r["phase"] in ("plan", "stages", "card_build"))
    assert build / out["attributed"] > 0.60
    assert out["rows"][-1]["phase"] == "deep_research"
    assert out["residual"] == 0
    assert abs(sum(r["share"] for r in out["rows"]) - 1.0) < 1e-9


def test_a_generation_with_no_phase_is_BUCKETED_and_never_dropped():
    """`timings` dropped node-less spans and hid 143 of one run's 174. Same mistake, same file.

    MUTATION: `continue` on a missing phase -> attributed falls to 100 and the run under-reports.
    """
    out = token_spend_by_phase([_gen("plan", 100), _gen(None, 900)])

    assert out["attributed"] == 1000
    assert out["rows"][0]["phase"] == PHASE_UNATTRIBUTED, "the unattributed bucket is the LARGER one"
    assert out["rows"][0]["tokens"] == 900
    assert {r["phase"] for r in out["rows"]} == {PHASE_UNATTRIBUTED, "plan"}


def test_a_provider_total_is_never_re_derived_from_its_parts():
    """A cached-prompt provider reports a `total` its parts do not sum to; that is the BILLED number.

    MUTATION: `total = prompt + completion` always -> 30 instead of the provider's 12.
    """
    out = token_spend_by_phase([_gen("plan", total=12, prompt=20, completion=10)])

    assert out["attributed"] == 12
    assert out["rows"][0]["prompt"] == 20 and out["rows"][0]["completion"] == 10


def test_a_missing_total_falls_back_to_the_two_parts():
    # MUTATION: drop the `or (prompt + completion)` fallback -> 0 tokens for every such call.
    out = token_spend_by_phase([_gen("propose", prompt=10, completion=5)])

    assert out["attributed"] == 15


def test_only_generation_spans_bill_tokens():
    """MUTATION: count every span kind -> a tool span's absent usage inflates `calls`."""
    out = token_spend_by_phase([
        _gen("plan", 100),
        {"kind": "tool", "attributes": {"phase": "plan", "usage": {"total": 999}}},
        {"kind": "operation", "attributes": {"phase": "plan"}},
    ])

    assert out["attributed"] == 100, "a tool span's usage must not be billed"
    assert out["calls"] == 1


def test_a_call_with_no_usage_still_counts_as_a_CALL():
    """MUTATION: skip zero-token spans -> the call vanishes and 'calls' understates the work."""
    out = token_spend_by_phase([_gen("triage", total=0), _gen("triage", 50)])

    assert out["rows"][0]["calls"] == 2
    assert out["rows"][0]["tokens"] == 50


@pytest.mark.parametrize("bad", [True, "12", None, float("nan"), float("inf"), -5])
def test_a_junk_token_count_contributes_nothing(bad):
    """`isinstance(True, int)` is True, so a bool would silently add 1.

    MUTATION: `int(value)` without the bool/NaN/negative guards -> True adds 1, NaN poisons the sum.
    """
    out = token_spend_by_phase([_gen("plan", total=bad)])

    assert out["attributed"] == 0
    assert out["rows"][0]["calls"] == 1, "the call happened even though its count was junk"


def test_the_residual_is_signed_and_absent_means_absent():
    """MUTATION: `max(0, ...)` -> a double-counted span reads as perfect agreement.
       MUTATION: default `ledger_total=0` -> 'no ledger' becomes 'the ledger says zero'."""
    over = token_spend_by_phase([_gen("plan", 150)], ledger_total=100)
    assert over["residual"] == -50, "a retried call opens two spans against one billed row"

    none = token_spend_by_phase([_gen("plan", 150)])
    assert none["residual"] is None and none["ledger_total"] is None

    assert token_spend_by_phase([_gen("plan", 40)], ledger_total=100)["residual"] == 60


def test_junk_rows_are_counted_and_never_raise():
    """MUTATION: let a non-dict span through -> AttributeError kills a read-only diagnostic."""
    out = token_spend_by_phase(["not a span", None, 42, _gen("plan", 10),
                                {"kind": "generation", "attributes": "junk"}])

    assert out["attributed"] == 10
    assert out["damaged"] == 4
    assert out["rows"][0]["phase"] == "plan"
    # The damaged generation still counted as a call under the unattributed bucket.
    assert out["calls"] == 2


def test_an_empty_read_says_nothing_rather_than_zero_percent():
    out = token_spend_by_phase([])

    assert out["rows"] == [] and out["attributed"] == 0 and out["calls"] == 0
    assert out["residual"] is None
