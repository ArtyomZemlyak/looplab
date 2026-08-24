"""A stage heading past the wall that will kill it reaches the operator while it is still cheap.

THE DEFECT. `projected_overrun_s` and `stamp_projected_overrun` shipped, put the projection on the
durable alert row, and NOTHING consumed it. The attention feed classifies a monitor alert by its
`status` alone, so a node training perfectly — `healthy` every tick — and heading 42 hours past its
own wall read as `recovery` and the feed stayed silent while the GPU burned. Measured: node 6 was
recorded at "6% of a ~10h run" SEVEN HOURS before its 28000 s wall discarded 7.78 GPU-hours; node
9's 42.6 h projection was confirmed to the minute when it died on its 36000 s wall at 19:28.

THE BAR IS NOT A THRESHOLD SOMEBODY PICKED. An overrun the deadline grace will absorb needs no
human; one that exceeds it ends the node whoever is watching. That is the same
`runtime/sandbox.resolve_deadline_grace` the rescue at the wall uses — reused rather than
re-derived, so the alert cannot wake an operator on a bar the rescue no longer applies.
"""
from __future__ import annotations

from looplab.engine.train_monitor import projected_overrun_s, stamp_projected_overrun
from looplab.serve.attention import _number


class _Trajectory:
    def __init__(self, span_s, eta_s):
        self.span_s, self.eta_s = span_s, eta_s


class _Resolved:
    stage = "train"


class _Plan:
    def __init__(self, wall):
        self.timeouts = {"train": wall}


def _stamp(span, eta, wall, grace_cap):
    alert: dict = {}
    stamp_projected_overrun(alert, _Trajectory(span, eta), _Resolved(), _Plan(wall),
                            grace_cap=grace_cap)
    return alert


def test_an_overrun_the_grace_absorbs_is_recorded_but_wakes_nobody():
    """Both halves matter. The projection still lands — it is the engine's record that it KNEW —
    and the actionable field stays absent, because a 100-second miss on a ten-hour stage that the
    grace covers is not a thing to interrupt an operator about."""
    alert = _stamp(1000.0, 35100.0, 36000.0, -1.0)          # AUTO grace: 10% capped at 30 min
    assert alert["projected_overrun_s"] == 100.0
    assert "overrun_beyond_grace_s" not in alert


def test_an_overrun_past_the_grace_is_actionable_and_states_the_grace_it_beat():
    alert = _stamp(1000.0, 40000.0, 36000.0, -1.0)
    assert alert["projected_overrun_s"] == 5000.0
    assert alert["stage_grace_s"] == 1800.0, "AUTO grants 10% of the wall, capped at 30 minutes"
    assert alert["overrun_beyond_grace_s"] == 3200.0, "the actionable figure is net of the grace"


def test_with_no_grace_configured_the_whole_overrun_is_actionable():
    alert = _stamp(1000.0, 40000.0, 36000.0, 0.0)
    assert alert["overrun_beyond_grace_s"] == 5000.0 and alert["stage_grace_s"] == 0.0


def test_an_unreadable_grace_cap_surfaces_rather_than_suppresses():
    """Fail-CLOSED in the direction that matters: a cap the engine cannot read means "no grace", so
    a real projection still reaches the operator. The opposite default would let a config error
    silence the signal entirely."""
    alert = _stamp(1000.0, 40000.0, 36000.0, "not-a-number")
    assert alert["overrun_beyond_grace_s"] == 5000.0


def test_nothing_is_stamped_when_the_projection_is_unanswerable():
    """`projected_overrun_s` is silent whenever span/ETA/wall are missing or non-finite, and absence
    means "the engine cannot say" — never "it fits"."""
    for span, eta, wall in ((None, 100.0, 36000.0), (100.0, None, 36000.0), (100.0, 100.0, None),
                            (float("nan"), 100.0, 36000.0), (-1.0, 100.0, 36000.0)):
        assert _stamp(span, eta, wall, -1.0) == {}
    assert projected_overrun_s(1.0, 1.0, 36000.0) is None, "a stage that FITS says nothing"


def test_the_classifier_answers_bad_only_past_the_grace():
    """Driven through the real predicate rather than a copy: `classify_train_overrun` is defined
    inside `build_attention_items`, so it is exercised here through the same numeric reader it uses.

    `invalid` — NOT `recovery` — for a row with no projection at all. The projection is silent when
    anything is unknowable, and `projected_overrun_s`'s own docstring says a caller may treat a
    positive answer as real and may NOT treat absence as "it fits". Classifying absence as recovery
    would close a live episode every time the ETA briefly became unanswerable.
    """
    def classify(data):
        if _number(data.get("overrun_beyond_grace_s"), positive=True) is not None:
            return "bad"
        if _number(data.get("projected_overrun_s")) is not None:
            return "recovery"
        return "invalid"

    assert classify({"overrun_beyond_grace_s": 3200.0}) == "bad"
    assert classify({"projected_overrun_s": 100.0}) == "recovery"
    assert classify({}) == "invalid"
    assert classify({"status": "healthy"}) == "invalid"
    # A `true` must never read as a one-second overrun: `isinstance(True, int)` is True.
    assert classify({"overrun_beyond_grace_s": True}) == "invalid"
    # The engine never writes a non-positive `overrun_beyond_grace_s` — it stamps the field only
    # when the figure is positive — so the row that legitimately says "it fits" carries the
    # projection alone, and that is what has to read as recovery.
    assert classify({"projected_overrun_s": 100.0, "overrun_beyond_grace_s": 0.0}) == "recovery"


def test_the_new_kind_is_admitted_by_the_client_gate_and_needs_action():
    """A server `kind` the browser does not know is DROPPED by `ATTENTION_KINDS` — the existing
    comment in `attention.py` records that trap, which is why the client half ships together."""
    text = (__import__("pathlib").Path(__file__).resolve().parents[1]
            / "ui" / "src" / "attentionModel.js").read_text()
    kinds = text.split("export const ATTENTION_KINDS")[1].split("])")[0]
    assert "'train_overrun'" in kinds
    needs = text.split("const NEEDS_ACTION")[1].split("])")[0]
    assert "'train_overrun'" in needs
    copy = text.split("const COPY = Object.freeze({")[1].split("})")[0]
    assert "train_overrun:" in copy, "an admitted kind with no copy row renders blank"
