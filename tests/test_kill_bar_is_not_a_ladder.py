"""The kill bar is a BAR, not a ladder — and the refusal is a corpus number, not a taste.

`train_monitor_kill_confidence` is 0.8 and a `broken` verdict under it does nothing. On
`runs/e5small-dr-unified-v8` node 2 that cost 36 minutes of GPU: three sub-bar `broken` verdicts
(0.70 at 08:31:58, 0.65 at 08:44:38, 0.70 at 08:56:39) before the 0.90 kill at 09:07:14.

The obvious answer — kill on K consecutive sub-bar `broken` verdicts — was measured against all 259
`train_monitor_alert` rows this box has recorded (35 node-generations, 114 `broken`) and REFUSED.
Four node-generations reach 2+ consecutive sub-0.8 `broken` verdicts; a K=3 ladder fires on three,
and two of those recorded real metrics:

    rubertlite-dr-unified-v6 node 1    0.62, 0.62, 0.75  ->  0.715142
    e5small-dr-unified-v4    node 3    0.75, 0.70, 0.75  ->  0.790898
    e5small-dr-unified-v8    node 2    0.70, 0.65, 0.70  ->  failed not_learning
    e5small-dr-unified-v4    node 12   0.62, 0.70        ->  idea_rejected, never trained

Two good nodes destroyed per node saved. This file exists so the refusal cannot rot into a bare
opinion: it pins the decision to the marker that carries the number, and pins the ladder's absence to
the code that would have to grow one.
"""
from __future__ import annotations

import inspect
import re

from looplab.engine import train_monitor


def _kill_site() -> str:
    """The source region that decides the kill. Located by the knob rather than by line number."""
    src = inspect.getsource(train_monitor)
    i = src.index('_kc = getattr(self, "_train_monitor_kill_confidence", 0.8)')
    return src[max(0, i - 2200):i + 400]


def test_the_refusal_is_recorded_where_the_kill_is_decided():
    """A decline that lives only in a commit message is a decline nobody will find. The open-item
    index requires a `measured:` clause with a number and a docs page; `tests/test_open_item_index.py`
    enforces the FORM. What this asserts is that the marker sits at the DECISION, not in a docstring
    three files away."""
    region = _kill_site()
    assert "broken-verdict-ladder" in region and "DECLINED[" in region
    assert "measured:" in region
    assert "docs/guide/llm-and-agents.md" in region


def test_the_refusal_carries_the_two_metrics_that_refute_the_ladder():
    """The whole argument is those two numbers: a K=3 ladder kills nodes that recorded 0.715142 and
    0.790898 to catch one true positive. A marker that lost them would be an assertion, not a
    measurement — and the next reader would re-propose the ladder."""
    region = _kill_site()
    assert "0.715142" in region and "0.790898" in region
    assert "not_learning" in region          # …and names the ONE true positive it would have caught


def test_repetition_is_already_required_and_is_not_what_holds_the_gun():
    """The correction this file's first draft needed. `broken_streak` ALREADY exists and
    `should_monitor_kill` already requires `broken_streak >= confirm_ticks`, so "kill once the
    verdict has repeated K times" is shipped, not missing. The streak counts ANY `broken` verdict at
    any confidence — its increment is `if verdict.status == "broken"` and never reads the number — so
    the two conjuncts are independent and the confidence bar is the one doing the holding."""
    src = inspect.getsource(train_monitor)
    assert "if broken_streak < max(1, needed):" in src        # repetition IS a required conjunct
    # Located by the INCREMENT and read backwards, not by the first `status == "broken"` in the
    # file: there is more than one, and a locator that finds the wrong one passes for the wrong
    # reason. (A mutant that confidence-gated the increment exposed exactly that fragility.)
    j = src.index("broken_streak += 1")
    guard = src[max(0, j - 200):j]
    assert 'if verdict.status == "broken":' in guard.splitlines()[-1].strip() + \
           guard.splitlines()[-2].strip()
    # the increment must not become confidence-gated without this decline being re-argued: that
    # would make sub-bar verdicts reset the streak and change what `confirm_ticks` means.
    last_guard_line = [ln for ln in guard.splitlines() if ln.strip().startswith("if ")][-1]
    assert last_guard_line.strip() == 'if verdict.status == "broken":', last_guard_line


def test_the_comment_no_longer_claims_the_streak_is_confidence_gated():
    """A comment that misdescribes a kill gate is a defect, not a typo: "counts CONSECUTIVE
    confident-broken verdicts" made the two conjuncts look like one, which is exactly the confusion
    that makes a ladder look like the missing piece."""
    src = inspect.getsource(train_monitor)
    assert "counts CONSECUTIVE confident-broken verdicts" not in src
    i = src.index("Phase 3 arming state.")
    assert "at ANY confidence" in src[i:i + 700]


def test_the_bar_itself_is_unchanged_and_still_fails_closed():
    """Lowering the bar is the same trade at a worse price — it discards the confidence signal
    instead of counting it — so the decline is only honest while the bar is still 0.8 and still
    refuses to coerce a non-numeric knob to zero."""
    from looplab.core.config import Settings

    assert Settings().train_monitor_kill_confidence == 0.8
    region = _kill_site()
    assert 'getattr(self, "_train_monitor_kill_confidence", 0.8)' in region
    # the `x or 0.0` coercion would turn an unset knob into a ZERO threshold — every `broken` kills
    assert "_train_monitor_kill_confidence\", 0.8) or 0.0" not in region
