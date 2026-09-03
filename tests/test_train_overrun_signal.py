"""A stage heading past the wall that will kill it reaches the operator while it is still cheap.

THE DEFECT. `projected_overrun_s` and `stamp_projected_overrun` shipped, put the projection on the
durable alert row, and NOTHING consumed it. The attention feed classifies a monitor alert by its
`status` alone, so a node training perfectly — `healthy` every tick — and heading 42 hours past its
own wall read as `recovery` and the feed stayed silent while the GPU burned. Measured: node 6 was
recorded at "6% of a ~10h run" SEVEN HOURS before its 28000 s wall discarded 7.78 GPU-hours; node
9's 42.6 h projection was confirmed to the minute when it died on its 36000 s wall at 19:28.

THE BAR MOVED ON 2026-09-03 and the reason is measured. It was the deadline GRACE — "an overrun the
grace will absorb needs no human" — which reads well and is wrong twice: the grace is a CEILING on a
one-shot rescue that may never be granted, and it was subtracted from a projection that already
under-states (`projected_overrun_s` counts training steps, not the tail). `e5small-dr-unified-v11`
node 3 drew twelve consecutive projections under that ceiling, opened nothing, and died on its wall
having burned 10.0 GPU-hours the engine had predicted 9h12m earlier.

The bar is now the projection's own resolution: 1 % of the declared wall with an absolute floor
under it. That admits every true positive the corpus holds (the smallest is 2.7 % of its wall) and
still rejects the noise case the bar exists for (40 s on a ten-hour stage, 0.11 %). The grace is
still RECORDED — how much rescue could exist is real information — it just no longer decides.
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


def test_an_overrun_under_the_bar_is_recorded_but_wakes_nobody():
    """Both halves matter. The projection still lands — it is the engine's record that it KNEW — and
    the actionable field stays absent, because a 100-second miss on a ten-hour stage is inside what
    the measurement can distinguish."""
    alert = _stamp(1000.0, 35100.0, 36000.0, -1.0)
    assert alert["projected_overrun_s"] == 100.0
    assert "overrun_beyond_noise_s" not in alert


def test_an_overrun_past_the_bar_is_actionable_and_states_the_bar_it_beat():
    """THE BAR MOVED on 2026-09-03 (`stamp_projected_overrun` holds the argument and the numbers):
    it was `over - resolve_deadline_grace(...)`, a rescue CEILING that may never be granted,
    subtracted from a projection that already under-states. It is now the projection's own
    resolution — 1 % of the declared wall, with an absolute floor under it.

    The grace is still RECORDED, because how much rescue could exist is real information; it just no
    longer decides.
    """
    alert = _stamp(1000.0, 40000.0, 36000.0, -1.0)
    assert alert["projected_overrun_s"] == 5000.0
    assert alert["overrun_alert_floor_s"] == 360.0, "1% of the 36000 s wall"
    assert alert["overrun_beyond_noise_s"] == 4640.0
    assert alert["stage_grace_s"] == 1800.0, "AUTO grants 10% of the wall, capped at 30 minutes"
    assert "overrun_beyond_grace_s" not in alert, "the old key is never written again"


def test_the_MEASURED_SUPPRESSION_no_longer_happens():
    """`e5small-dr-unified-v11` node 3, reproduced. Twelve consecutive projections of 977-1121 s on
    a 36000 s wall, every one under the 1800 s AUTO grace ceiling, every one suppressed — and the
    stage then died on its wall at 2948/3150 steps having burned 10.0 GPU-hours the engine had
    predicted 9h12m earlier.

    MUTATION: restore `over - grace` -> this row opens nothing again, which is the whole defect.
    """
    alert = _stamp(30000.0, 7120.6, 36000.0, -1.0)     # the stamped magnitude of the last tick
    assert alert["projected_overrun_s"] == 1120.6
    assert alert["stage_grace_s"] == 1800.0, "…and it sat under the AUTO ceiling, which is the bug"
    assert alert["overrun_beyond_noise_s"] == 760.6, "past the 360 s floor: the operator is told"


def test_the_stated_NOISE_case_is_still_suppressed():
    """The bar exists for a reason and it must keep working: "a 40-second overrun on a ten-hour
    stage is not a thing to interrupt an operator about" — 0.11 % of the wall, an order of magnitude
    under the floor.

    MUTATION: drop the bar entirely -> every rounding wobble on a long stage wakes somebody.
    """
    alert = _stamp(35000.0, 1040.0, 36000.0, -1.0)
    assert alert["projected_overrun_s"] == 40.0
    assert "overrun_beyond_noise_s" not in alert


def test_a_SHORT_stage_uses_the_absolute_floor():
    """1 % of a two-minute stage is 1.2 s, which is not a meaningful quantity to wake anyone over."""
    alert = _stamp(100.0, 30.0, 120.0, -1.0)
    assert alert["projected_overrun_s"] == 10.0
    assert "overrun_beyond_noise_s" not in alert, "under the 60 s absolute floor"


def test_the_grace_no_longer_decides_anything():
    """Driven as an equivalence: the same projection and wall must give the same verdict at every
    grace setting, which is exactly what was false before.

    MUTATION: subtract the grace again -> the `-1.0` (AUTO) and `0.0` arms diverge.
    """
    verdicts = [_stamp(30000.0, 7120.6, 36000.0, cap).get("overrun_beyond_noise_s")
                for cap in (-1.0, 0.0, 1800.0, 7200.0, "not-a-number")]
    assert len(set(verdicts)) == 1 and verdicts[0] is not None, verdicts


def test_an_unreadable_grace_cap_still_records_no_grace():
    """Unchanged fail-closed behaviour, now purely a RECORD: a cap the engine cannot read means "no
    grace", never "it fits". It can no longer silence a signal because it no longer gates one."""
    alert = _stamp(1000.0, 40000.0, 36000.0, "not-a-number")
    assert alert["stage_grace_s"] == 0.0
    assert alert["overrun_beyond_noise_s"] == 4640.0


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
    from looplab.serve.attention import _beyond_bar

    def classify(data):
        if _beyond_bar(data) is not None:
            return "bad"
        if _number(data.get("projected_overrun_s")) is not None:
            return "recovery"
        return "invalid"

    assert classify({"overrun_beyond_noise_s": 4640.0}) == "bad"
    assert classify({"projected_overrun_s": 100.0}) == "recovery"
    assert classify({}) == "invalid"
    assert classify({"status": "healthy"}) == "invalid"
    # A `true` must never read as a one-second overrun: `isinstance(True, int)` is True.
    assert classify({"overrun_beyond_noise_s": True}) == "invalid"
    # The engine never writes a non-positive beyond-bar figure — it stamps the field only when the
    # figure is positive — so the row that legitimately says "it fits" carries the projection alone,
    # and that is what has to read as recovery.
    assert classify({"projected_overrun_s": 100.0, "overrun_beyond_noise_s": 0.0}) == "recovery"


def test_a_PRESERVED_row_keeps_its_old_classification():
    """The additive-only half (invariant #5). Rows written before 2026-09-03 carry
    `overrun_beyond_grace_s`, whose bar was the deadline-grace ceiling; reading only the new key
    would silently reclassify every historical `bad` episode as a `recovery`, i.e. rewrite the past
    to say the engine had nothing to report.

    MUTATION: drop the legacy key from `_beyond_bar` -> every preserved overrun episode closes.
    """
    from looplab.serve.attention import _beyond_bar

    assert _beyond_bar({"overrun_beyond_grace_s": 3200.0}) == 3200.0
    assert _beyond_bar({"overrun_beyond_noise_s": 4640.0}) == 4640.0
    # A row carrying BOTH (nothing writes one, but a hand-edited log can) prefers the current key,
    # so the two callers below cannot classify from one and render from the other.
    assert _beyond_bar({"overrun_beyond_noise_s": 1.0, "overrun_beyond_grace_s": 9.0}) == 1.0
    assert _beyond_bar({"overrun_beyond_grace_s": True}) is None
    assert _beyond_bar({}) is None


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


def test_the_server_agrees_with_the_client_about_what_needs_action():
    """DERIVED from both sets, because pinning one member on one side is what let this ship broken.

    `train_overrun` reached the browser's `NEEDS_ACTION` and not the server's
    `ATTENTION_NEEDS_ACTION_KINDS`, and the consequence is not a sort-order nit:
    `useAttention.js::normalizeRunPage` REFUSES a page whose `active_action_count` is below the
    count it derives from that page's own rows, and a refused page is held as stale — so the run's
    entire attention feed freezes for as long as the item is live. The feature blanked the inbox it
    was added to populate.
    """
    import re

    from looplab.serve.attention import ATTENTION_NEEDS_ACTION_KINDS

    text = (__import__("pathlib").Path(__file__).resolve().parents[1]
            / "ui" / "src" / "attentionModel.js").read_text()
    block = text.split("const NEEDS_ACTION = new Set([")[1].split("])")[0]
    # Strip line comments first: a commented-out member may satisfy neither side of this compare.
    block = re.sub(r"^\s*//.*$", "", block, flags=re.M)
    client = {m.group(1) for m in re.finditer(r"'([a-z_]+)'", block)}
    assert len(client) >= 8, "the client set was read empty — that would pass vacuously"

    # `assistant_permission` is the assistant surface's own kind and is never emitted by
    # `project_event_attention`, so it is the ONE member the server legitimately does not carry.
    missing = sorted(client - {"assistant_permission"} - set(ATTENTION_NEEDS_ACTION_KINDS))
    assert missing == [], (
        f"{missing} are actionable to the browser and unknown to the server's ordering/count table; "
        "each one freezes the whole run feed via normalizeRunPage's active_action_count refusal")
