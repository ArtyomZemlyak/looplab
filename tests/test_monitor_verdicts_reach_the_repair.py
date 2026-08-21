"""The training watchdog's verdicts must reach the agent that REPAIRS — and arrive as an OPINION.

THE MEASUREMENT THIS FILE IS BUILT FROM (`runs/e5small-dr-unified-v4` node 3, 2026-08-20/21)
--------------------------------------------------------------------------------------------
Two agents watched one training run for ten hours and reached opposite conclusions; the wrong one
held the pen.

The training watchdog wrote **21** `train_monitor_alert` rows for that node lifecycle. Fifteen of
them named a mechanism, converging on it precisely: the DCL mask sentinel is a FINITE `-1e9`, so a
row whose mask removes every negative drags the batch mean by roughly `-1e9/batch` and the
contrastive objective — bounded below by 0 as written — becomes unbounded below. Its measured
trajectory said the same thing every single time and never wavered: `direction=descending`,
`first=40.07`, `last=-2.4e7`.

The stage then hit its ceiling. The repair judge was handed the stderr tail, the code tail and the
repair history — and NOT one word of any of that — and opened: *"Healthy training run ... a pure
speed failure, not a correctness one."* It cut `n_epochs` 15 -> 3, left `dcl_threshold` and the
sentinel untouched, and the next attempt re-ran the same degenerate objective. ~17 GPU-hours.

So the property is NOT "the repairer should agree with the watchdog". The watchdog was not reliably
right either — on this very node it said `healthy` at confidence 0.85 five separate times, twice
directly between two `broken` verdicts at the same confidence. The property is that **the repair
judge is shown what the watchdog said, told whose opinion it is, and shown the wobble rather than an
average of it** — plus the one field on those rows that is not an opinion at all, the measured loss
trajectory, which never contradicted itself.

`tests/test_train_monitor.py` drives the watchdog that PRODUCES these rows and
`tests/test_repair_log_tools_wiring.py` the tools the same judge may pull; what is driven HERE is the
join between them — that the read is keyed to this node's own lifecycle, that the rendering carries
attribution and per-row confidence, that nothing collapses the series, and that a node with no alerts
asks the historical question byte for byte.
"""
from __future__ import annotations

import json
import pathlib

from looplab.core.models import RunState
from looplab.engine import crash_repair as cr
from looplab.engine.evaluate import _durable_monitor_verdicts
from looplab.events.types import EV_TRAIN_MONITOR_ALERT


# --------------------------------------------------------------------------- the real record
def _node3_alerts() -> list[dict]:
    """Node 3's twenty-one alerts, verbatim from `runs/e5small-dr-unified-v4/events.jsonl`.

    Kept as data rather than as a generated shape because the thing under test is a JOIN over fields
    another module writes: a fixture that invents `{"status": "broken"}` would still pass if the
    watchdog renamed the field tomorrow, and the join would be silently empty in production."""
    return json.loads((pathlib.Path(__file__).parent / "data"
                       / "e5small_v4_node3_train_monitor_alerts.json").read_text(encoding="utf-8"))


class _Row:
    """The narrowest thing `_durable_monitor_verdicts` reads: `.type` and `.data`."""

    def __init__(self, etype: str, data: dict):
        self.type, self.data = etype, data


def _events(alerts, node_id=3, generation=0):
    return [_Row(EV_TRAIN_MONITOR_ALERT, dict(a, node_id=node_id, generation=generation))
            for a in alerts]


# --------------------------------------------------------------------------- the read
def test_the_read_returns_this_lifecycles_verdicts_oldest_first():
    got = _durable_monitor_verdicts(_events(_node3_alerts()), 3, 0)
    assert len(got) == 21
    assert [v["status"] for v in got[:4]] == ["broken", "broken", "broken", "watch"]
    assert got[0]["reason"].startswith("Loss diverges catastrophically from 40.07")


def test_a_sibling_node_or_generation_is_not_this_nodes_evidence():
    """The failure this prevents is not cosmetic: handing a repair another node's diagnosis is worse
    than handing it none, because it is confident and specific and about different code."""
    alerts = _node3_alerts()[:3]
    assert _durable_monitor_verdicts(_events(alerts, node_id=4), 3, 0) == []
    assert _durable_monitor_verdicts(_events(alerts, generation=1), 3, 0) == []
    # …and the two coercions `_durable_row_belongs` exists for, in both directions.
    assert _durable_monitor_verdicts(_events(alerts, node_id=True), 1, 0) == []   # bool is not node 1
    assert len(_durable_monitor_verdicts(_events(alerts, generation="0"), 3, 0)) == 3


def test_only_watchdog_rows_are_read():
    events = _events(_node3_alerts()[:2]) + [_Row("node_repaired", {"node_id": 3, "generation": 0,
                                                                   "status": "broken"})]
    assert len(_durable_monitor_verdicts(events, 3, 0)) == 2


def test_the_read_survives_a_junk_row():
    """Raw log rows are untrusted append-only data; a reader on the repair path must degrade to
    "less evidence", never to an exception that fails the node a second time."""
    events = [_Row(EV_TRAIN_MONITOR_ALERT, {"node_id": 3, "generation": 0}),
              _Row(EV_TRAIN_MONITOR_ALERT, None)] + _events(_node3_alerts()[:1])
    got = _durable_monitor_verdicts(events, 3, 0)
    assert [v["status"] for v in got] == [None, "broken"]
    assert _durable_monitor_verdicts(None, 3, 0) == []


# --------------------------------------------------------------------------- the rendering
def test_no_verdicts_renders_nothing_at_all():
    """The shippability property, and the reason this is safe to turn on for every node at once: a
    node whose watchdog never spoke — the monitor off, no client, or a healthy quiet run — must ask
    the question it has always asked, to the byte."""
    for empty in (None, [], [{}], [{"status": ""}], ["not a dict"]):
        assert cr._format_monitor_verdicts(empty) == ""


def test_every_row_carries_its_own_confidence_and_the_series_is_not_collapsed():
    """The wobble IS the information. Node 3's watchdog said `broken` at 0.85 and `healthy` at 0.85
    within one lifecycle; a rendering that reduced this to a single verdict — most recent, majority,
    or highest-confidence — would state one of those as the answer and delete the fact that the
    watchdog had no settled answer."""
    verdicts = _durable_monitor_verdicts(_events(_node3_alerts()), 3, 0)
    text = cr._format_monitor_verdicts(verdicts)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("verdict=")]
    assert len(lines) == cr._MONITOR_VERDICT_ROWS
    assert all("confidence=" in ln for ln in lines)
    # The window lands on the tail of the series, which on this node holds BOTH answers.
    assert {ln.split()[0] for ln in lines} >= {"verdict=broken", "verdict=healthy"}
    # …at more than one confidence, i.e. the numbers are per row and not one repeated header value.
    assert len({ln.split("confidence=")[1].split()[0] for ln in lines}) > 1


def test_a_verdict_never_appears_without_saying_whose_it_is():
    """The defect this guards is the one measured one layer up in the diagnostician: a MODEL'S
    READING handed on in a voice that reads as the engine's. Any output that carries a verdict must
    carry the attribution, so the property is stated over the pair rather than pinned to wording."""
    for n in (1, 2, 6, 21):
        text = cr._format_monitor_verdicts(_durable_monitor_verdicts(
            _events(_node3_alerts()[:n]), 3, 0))
        assert "verdict=" in text
        head = text.splitlines()[0]
        assert "WATCHDOG" in head and "not an engine observation" in head
        # the attribution is ABOVE the rows, not a footnote after them
        assert text.index("verdict=") > text.index("not an engine observation")


def test_what_is_dropped_is_counted_rather_than_silently_cut():
    """`no silent caps`: a bounded window that does not say it is bounded reads as the whole record.
    Node 3 drew 21 alerts and the prompt shows 6."""
    text = cr._format_monitor_verdicts(_durable_monitor_verdicts(_events(_node3_alerts()), 3, 0))
    assert "showing the 6 most recent of 21" in text
    # …and no count at all when nothing was dropped, so a short series reads as complete.
    short = cr._format_monitor_verdicts(_durable_monitor_verdicts(
        _events(_node3_alerts()[:3]), 3, 0))
    assert "showing" not in short and short.count("verdict=") == 3


def test_the_measured_trajectory_rides_with_the_opinion_and_is_labelled_as_measured():
    """The one field on these rows that is NOT a judgement. It is what settles the disagreement
    without trusting either agent: whatever the prose said, the loss went from +40.07 to -2.4e7."""
    verdicts = _durable_monitor_verdicts(_events(_node3_alerts()), 3, 0)
    assert sum(isinstance(v["trajectory"], dict) for v in verdicts) == 20
    text = cr._format_monitor_verdicts(verdicts)
    measured = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("measured:")]
    # Every measured line carries the two numbers that settle the disagreement, and the direction
    # is carried as the watchdog computed it — `unknown` and `flat` on the first two windows, before
    # there are enough points to call it. A rendering that only reproduced `descending` would be
    # asserting agreement it has not got: the sixth row below says `healthy` at confidence 0.85
    # with `last=-5676245.2` on the line directly above it, and BOTH belong in the prompt.
    assert all("first=38.5723" in ln and "last=-" in ln and "direction=" in ln for ln in measured)
    assert {"unknown", "flat", "descending"} >= {ln.split("direction=")[1].split()[0]
                                                 for ln in measured}
    # The twenty-first row is the real record's own negative control, and it lands INSIDE the shown
    # window: the alert written seconds after the retry started ("Run just started training
    # cleanly") had nothing to measure yet, so the watchdog stamped no trajectory. It keeps its
    # verdict and simply renders no `measured:` line — a missing measurement must not delete an
    # opinion, and an opinion must not manufacture a measurement.
    assert len(measured) == cr._MONITOR_VERDICT_ROWS - 1
    assert text.count("verdict=") == cr._MONITOR_VERDICT_ROWS
    thin = [dict(v, trajectory=None) for v in verdicts[:2]]
    assert cr._format_monitor_verdicts(thin).count("verdict=") == 2
    assert "measured:" not in cr._format_monitor_verdicts(thin)


def test_a_long_reason_is_clipped_visibly():
    """The prompt already carries the repair history, the stderr tail and the code tail. The clip is
    marked so the judge can tell a truncated sentence from a finished one — the exact failure mode
    that produced the wrong repair on this node, one layer down."""
    long = "x" * (cr._MONITOR_REASON_CHARS + 500)
    text = cr._format_monitor_verdicts([{"status": "broken", "confidence": 0.8, "reason": long}])
    assert "…" in text and ("x" * (cr._MONITOR_REASON_CHARS + 1)) not in text


def test_an_unusable_confidence_is_named_rather_than_guessed():
    for bad in (None, "high", float("nan")):
        row = {"status": "broken", "confidence": bad}
        out = cr._format_monitor_verdicts([row])
        assert "confidence=unstated" in out or "confidence=nan" in out
        assert "confidence=0.00" not in out


# --------------------------------------------------------------------------- the wiring
class _Recorder:
    """A triage seam that records the kwargs it was handed and answers without looking."""

    def __init__(self):
        self.calls = []

    def triage_crash(self, node, error, attempt, *, state=None, brief="", history="",
                     stages_passed=None, attempts_left=None, tools=None, engine_facts="",
                     **kw):
        self.calls.append({"history": history, "engine_facts": engine_facts, **kw})
        return {"action": "repair", "rationale": "r", "failure_kind": "crash"}


class _EngineStub(cr.CrashRepairMixin):
    _inline_repair_attempts = 5

    def __init__(self, researcher):
        self.researcher = researcher

        class _Span:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        self.tracer = type("T", (), {"span": lambda *a, **k: _Span()})()


def _triage(monitor_verdicts, repair_log=None):
    rec = _Recorder()
    out = _EngineStub(rec)._triage_crash(RunState(), type("N", (), {"id": 3, "code": "x"})(),
                                         "boom", 1, repair_log=repair_log,
                                         monitor_verdicts=monitor_verdicts)
    assert out["action"] == "repair"
    return rec.calls[0]


def test_the_verdicts_reach_the_judge_that_decides_the_repair():
    """The end-to-end claim, driven through the real triage path rather than asserted about a call
    graph: what node 3's Developer would have been shown."""
    verdicts = _durable_monitor_verdicts(_events(_node3_alerts()), 3, 0)
    history = _triage(verdicts)["history"]
    assert "WATCHDOG" in history and "verdict=broken" in history
    assert "direction=descending" in history
    # the mechanism the watchdog named eleven times and the repair never saw
    assert "-1e9" in history or "1e9" in history


def test_the_ask_is_unchanged_when_the_watchdog_never_spoke():
    """`off == today`, stated over the ARGUMENT the judge receives — the only thing a difference here
    could reach."""
    log = [{"attempt": 1, "action": "repair", "reason": "crash", "fix": "tried a thing"}]
    for empty in (None, []):
        assert _triage(empty, repair_log=log)["history"] == cr._format_repair_log(log)


def test_the_verdicts_are_appended_to_the_history_and_do_not_replace_it():
    """Two independent bodies of evidence about the same node. Neither is allowed to shadow the
    other: the repair trajectory answers "is this ground being re-covered", the verdicts answer
    "what is actually wrong", and the judge needs both to tell an inert fix from a wrong one."""
    log = [{"attempt": 1, "action": "repair", "reason": "crash", "fix": "cut n_epochs 15 -> 3"}]
    verdicts = _durable_monitor_verdicts(_events(_node3_alerts()[:2]), 3, 0)
    history = _triage(verdicts, repair_log=log)["history"]
    assert history.startswith(cr._format_repair_log(log))
    assert "cut n_epochs 15 -> 3" in history and "verdict=broken" in history


def test_an_older_triage_implementation_still_answers():
    """`_accepted_kwargs` is the safety on a DUCK-TYPED seam: an argument passed unconditionally to
    an implementation written against an older signature raises TypeError, which the fail-closed
    handler reads as a dead provider — a stopped node PLUS a run-level pause. The verdicts ride
    inside `history`, which every implementation has always accepted, so this holds for free; the
    test exists because "for free" is a property of the current spelling, not of the design."""
    class _Old:
        def triage_crash(self, node, error, attempt, **kw):
            return {"action": "abandon", "rationale": "old", "failure_kind": "crash"}

    verdicts = _durable_monitor_verdicts(_events(_node3_alerts()), 3, 0)
    out = _EngineStub(_Old())._triage_crash(RunState(), type("N", (), {"id": 3, "code": "x"})(),
                                            "boom", 1, monitor_verdicts=verdicts)
    assert out["action"] == "abandon"
