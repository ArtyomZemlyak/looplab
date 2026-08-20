"""A watchdog that cannot act must SAY SO. Measured over six real runs, this one never did.

THE MEASUREMENT (`docs/BACKLOG.md` §0.15, 2026-08-19). `asha_rank` has **zero** rows across every
run in `runs/`, with `asha_live: true` AND `asha_live_kill: true` in all five snapshots that carry
them. Not a configuration problem and not the `min_siblings` floor: the tick bails at
`sample is None` because the task prints `RECALL@100:` exactly ONCE, on the last line of a 5-10 hour
training. Re-derived here for the record — the objective appears 0-3 times in a WHOLE training log,
never as a curve:

    e5small-dr-unified-v2 node_2  53 MB train.log, 13,337 lines, 1 x `RECALL@100:` (the last line)
    node 5 trained 10.5 h and was rejected; nodes 2 and 4 ran to completion and scored 0.0 / 2e-05.

An operator reading `asha_live: true` concludes underperformers are being stopped. Nothing was, and
nothing in the record said so — the `continue` above is silent, and this watchdog only opens a span
on a rank TRANSITION, so an inert watchdog produced no span, no event and no log line at all. The
2026-08-07 audit (`docs/audit/2026-08-07-search-loop.md` F3) already asked for exactly this
diagnostic; twelve days later it was still unwritten.

WHAT IS AND IS NOT FIXED HERE. This makes an existing refusal legible and changes nothing about what
may stop a node: `should_asha_kill`'s conjuncts are untouched, no model reading becomes load-bearing,
and the statement reaches only a span and a log line — never the fold. Giving ASHA a curve to read is
a separate question and the corpus answers it in §0.15: the only intermediate signals these logs
carry (`loss`, `eval_loss`) rank NEGATIVELY against the final objective, so no engine-side proxy is
admissible.
"""
from __future__ import annotations

import anyio
import pytest

from looplab.engine.asha_monitor import (
    _ASHA_INERT_NO_CURVE,
    _ASHA_INERT_NO_JSON,
    _ASHA_INERT_NO_LIVE_READ,
    _ASHA_INERT_NO_RESOURCE_KEY,
    _ASHA_SILENT_TICKS,
    AshaMonitorMixin,
    asha_inert_reason,
)
from looplab.events.types import EV_ASHA_RANK, EV_ASHA_VERDICT
from tests.test_asha_monitor import _AshaStub, _run_loop   # the harness IS the production path

# The live run's own metric contract, copied from `runs/e5small-dr-unified-v2/task.snapshot.json`.
_V2_METRIC = {"pattern": "RECALL@100: ([0-9.]+)", "kind": "stdout_regex",
              "subject_glob": ["vectorsearch/experiments/*/final/model.safetensors"]}


class _RecordingSpan:
    def __init__(self, name, attrs, sink):
        self.name, self.attributes, self._sink = name, attrs, sink

    def set(self, key, value):
        self.attributes[key] = value

    def set_many(self, **kw):
        self.attributes.update(kw)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._sink.append(self)
        return False


class _RecordingTracer:
    """The no-op tracer in `test_asha_monitor` discards attributes, and the attribute IS the fix."""

    def __init__(self):
        self.spans: list[_RecordingSpan] = []

    def span(self, name, **attrs):
        return _RecordingSpan(name, dict(attrs), self.spans)


def _watched(stub):
    """Every span this watchdog opened, in order."""
    return [s for s in stub.tracer.spans if s.name == "asha_monitor"]


def _inert(stub):
    return [s for s in _watched(stub) if s.attributes.get("kill_reachable") is False]


# ----------------------------------------------------------- the rule, as a truth table (tier 2)

@pytest.mark.parametrize("spec,expected", [
    # the live task family: readable, rankable, and never killable
    (_V2_METRIC, _ASHA_INERT_NO_JSON),
    ({"kind": "stdout_regex", "pattern": "R: ([0-9.]+)"}, _ASHA_INERT_NO_JSON),
    # the right kind, the declaration simply missing — the shape the 2026-08-07 audit measured
    ({"kind": "stdout_json", "key": "recall"}, _ASHA_INERT_NO_RESOURCE_KEY),
    # a resource_key that is not usable is not a declaration: `_declared_resource_key` refuses it
    ({"kind": "stdout_json", "key": "recall", "resource_key": "recall"}, _ASHA_INERT_NO_RESOURCE_KEY),
    ({"kind": "stdout_json", "key": "recall", "resource_key": ""}, _ASHA_INERT_NO_RESOURCE_KEY),
    ({"kind": "stdout_json", "key": "recall", "resource_key": 7}, _ASHA_INERT_NO_RESOURCE_KEY),
    # no live reader at all: not even the advisory rank exists for these kinds
    ({"kind": "file_json", "key": "recall"}, _ASHA_INERT_NO_LIVE_READ),
    ({"kind": "host_score", "key": "recall"}, _ASHA_INERT_NO_LIVE_READ),
    # the ONE contract a kill is reachable under
    ({"kind": "stdout_json", "key": "recall", "resource_key": "step"}, None),
    ({"key": "recall", "resource_key": "step"}, None),          # stdout_json is the reader default
    # nothing declared is not a claim about reachability
    (None, None),
    ("RECALL@100", None),
])
def test_the_reachability_rule_is_stated_not_buried(spec, expected):
    assert asha_inert_reason(spec) == expected


def test_the_opt_in_being_off_is_not_inertness():
    """`asha_live_kill=false` is an operator's own choice on their own config line, and the training
    monitor does not report `train_monitor_kill=false` as `kill_reachable: false` either. Only the
    METRIC CONTRACT decides, so this rule takes no other argument."""
    import inspect
    sig = inspect.signature(asha_inert_reason)
    assert list(sig.parameters) == ["metric_spec"]


# ------------------------------------------------- the statement, driven through the real loop

def test_the_live_runs_metric_contract_says_it_cannot_kill_on_the_first_tick(tmp_path, monkeypatch):
    """Structural, so it is knowable before any read — the same property that made the training
    monitor's `kill_reachable` readable "from its first tick instead of after the hours it takes for
    a verdict to matter". Driven with the run's OWN metric spec and a tail carrying a value, so the
    rank half really is alive and only the kill half is refused."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("RECALL@100: 0.100000\n", encoding="utf-8")
    stub = _AshaStub(kill=True, quantile=0.5, min_siblings=3)
    stub.tracer = _RecordingTracer()
    # Wait for BOTH halves: the statement is written before the tick's read, so a predicate that
    # only waits for it cancels the loop before the advisory rank row it must not have disturbed.
    _run_loop(stub, wd, _V2_METRIC, "max", {}, monkeypatch, finals=[0.80, 0.70, 0.60],
              until=lambda h: bool(_inert(h)) and [t for (t, _d) in h.store.events
                                                   if t == EV_ASHA_RANK])

    said = _inert(stub)
    assert said, "an unkillable metric contract must say so"
    assert said[0].attributes["inert_reason"] == _ASHA_INERT_NO_JSON
    assert said[0].attributes["metric_kind"] == "stdout_regex"
    # ...and it is said ONCE, however many ticks the eval runs for.
    assert len(said) == 1
    # The advisory half is untouched: the rank row still lands.
    assert [t for (t, _d) in stub.store.events if t == EV_ASHA_RANK]


def test_a_killable_contract_says_nothing(tmp_path, monkeypatch):
    """The negative control. A watchdog that CAN act must not narrate that it can — otherwise the
    statement is noise and an operator learns to ignore it."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text('{"recall": 0.10, "step": 4}\n', encoding="utf-8")
    stub = _AshaStub(kill=True, quantile=0.5, min_siblings=3)
    stub.tracer = _RecordingTracer()
    _run_loop(stub, wd, {"kind": "stdout_json", "key": "recall", "resource_key": "step"}, "max",
              {}, monkeypatch, finals=[0.80, 0.70, 0.60], window=0.12)
    assert _watched(stub), (
        "NON-VACUITY: the watchdog opened no span at all, so `not _inert(stub)` is true of\n"
        "        nothing. This control claims the watchdog RAN and stayed quiet — without this\n"
        "        line it passes just as well on a loop that never ticked, which is what a\n"
        "        sleep-based window makes easy to do by accident on a loaded box.")
    assert not _inert(stub)


def test_a_metric_printed_only_at_the_end_is_named_as_the_missing_curve(tmp_path, monkeypatch):
    """THE measured defect: `sample is None` on every tick of every run in the corpus, silently.

    The contract here is the killable one, so nothing structural is wrong — the training is simply
    printing steps and never its objective, which is what a 5-10 hour `RECALL@100`-at-the-end run
    looks like from inside. The tail is non-empty on every tick, so the streak is real evidence and
    not "the stage has not started yet"."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text(
        "".join(f"{{\"step\": {i}, \"loss\": {11.0 - i * 0.01:.4f}}}\n" for i in range(200)),
        encoding="utf-8")                       # talking steadily, never naming `recall`
    stub = _AshaStub(kill=True, quantile=0.5, min_siblings=3)
    stub.tracer = _RecordingTracer()
    _run_loop(stub, wd, {"kind": "stdout_json", "key": "recall", "resource_key": "step"}, "max",
              {}, monkeypatch, finals=[0.80, 0.70, 0.60], until=lambda h: bool(_inert(h)))

    said = _inert(stub)
    assert said, "a watchdog that never parsed a single sample must say the curve does not exist"
    assert said[0].attributes["inert_reason"] == _ASHA_INERT_NO_CURVE
    assert said[0].attributes["silent_ticks"] >= _ASHA_SILENT_TICKS
    assert len(said) == 1                        # once per eval, not once per tick
    # It decided nothing: no rank row, no verdict row, no kill.
    assert not [t for (t, _d) in stub.store.events if t in (EV_ASHA_RANK, EV_ASHA_VERDICT)]


def test_a_log_that_has_not_started_writing_is_not_evidence_of_a_missing_curve(tmp_path, monkeypatch):
    """An EMPTY tail is "nothing written yet", which the stall watchdog owns and this one must not
    claim. Without this split the statement fires on every eval during dependency install, which is
    exactly the false alarm that teaches an operator to stop reading it."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("", encoding="utf-8")
    stub = _AshaStub(kill=True, quantile=0.5, min_siblings=3, cadence=0.005)
    stub.tracer = _RecordingTracer()
    _run_loop(stub, wd, {"kind": "stdout_json", "key": "recall", "resource_key": "step"}, "max",
              {}, monkeypatch, finals=[0.80, 0.70, 0.60], window=0.2)

    # THE PROPERTY IS THE EMPTINESS, and saying so is the point. This read `assert not _inert(stub)`
    # until 2026-08-20, which is the sentence "it ran and said nothing" — and it was true of NOTHING,
    # because on an empty tail the watchdog opens no span at all. Measured 3/3 deterministically, so
    # it is the design and not a loaded-box flake. A negative control that cannot tell "stayed quiet"
    # from "never ran" is the vacuous green this file exists to prevent one level down.
    assert _watched(stub) == [], (
        f"an empty tail must not even open a span — the stall watchdog owns 'nothing written yet' "
        f"and this one must not narrate it: {[s.attributes for s in _watched(stub)]}")

    # ...and the harness DOES open spans when there is a sample, so the emptiness above is caused by
    # the empty log rather than by a loop that never ticked. Same stub, same window, one line of log.
    live_wd = tmp_path / "node_1"
    live_wd.mkdir()
    (live_wd / "train.log").write_text('{"recall": 0.10, "step": 4}\n', encoding="utf-8")
    live = _AshaStub(kill=True, quantile=0.5, min_siblings=3, cadence=0.005)
    live.tracer = _RecordingTracer()
    _run_loop(live, live_wd, {"kind": "stdout_json", "key": "recall", "resource_key": "step"}, "max",
              {}, monkeypatch, finals=[0.80, 0.70, 0.60], window=0.2)
    assert _watched(live), "the control itself is broken — this harness opens no spans at all"


def test_the_statement_can_never_end_a_node(tmp_path, monkeypatch):
    """docs/36's line, and the reason this half shipped alone: a wider action space must not widen
    the trusted set. A tracer that explodes on every span leaves the eval exactly as it was."""
    class _Boom:
        def span(self, *a, **k):
            raise RuntimeError("tracer is down")

    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("RECALL@100: 0.100000\n", encoding="utf-8")
    kill_signal: dict = {}
    stub = _AshaStub(kill=True, quantile=0.5, min_siblings=3)
    stub.tracer = _Boom()
    _run_loop(stub, wd, _V2_METRIC, "max", kill_signal, monkeypatch, finals=[0.80, 0.70, 0.60],
              window=0.15)
    assert kill_signal == {}


def test_the_statement_is_a_span_and_a_log_line_and_nothing_durable(tmp_path, monkeypatch, caplog):
    """`kill_reachable` is the training monitor's attribute name, deliberately — one vocabulary for
    "nothing here can be stopped" across both watchdogs. The WARNING is the GPU-pool lease's
    precedent: an operator reading a config is reading a console, not a trace. Neither is an event:
    this must not touch the log the fold reads."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("RECALL@100: 0.100000\n", encoding="utf-8")
    stub = _AshaStub(kill=True, quantile=0.5, min_siblings=3)
    stub.tracer = _RecordingTracer()
    with caplog.at_level("WARNING", logger="looplab.engine.asha_monitor"):
        _run_loop(stub, wd, _V2_METRIC, "max", {}, monkeypatch, finals=[0.80, 0.70, 0.60],
                  until=lambda h: bool(_inert(h)))
    assert any("ASHA early-stop watchdog is inert" in r.getMessage() for r in caplog.records)
    assert not [t for (t, _d) in stub.store.events if t == EV_ASHA_VERDICT]
