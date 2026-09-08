"""Two defects about a run's DURATION, and the command that is supposed to explain it.

1. `budget.elapsed_s` measured the PROCESS, not the run. A stopped run wrapped up later by
   `looplab finalize` is finalized by a different process, so `time.time() - start` there described
   the wrap-up: a real ~4.6-minute run published `elapsed_s: 0.027`. The truth lives in the event
   log, which carries a `ts` on every row and is the one record that spans both processes.
2. `looplab timings` DROPPED every span with no `node_id` — the researcher, the strategist, the
   lesson passes, the report and the Card-build producer — and reconciled against nothing, so on a
   28-minute run it accounted for 2.7 minutes and nobody could see what was missing.

Every test here drives the real code (a real log on disk, a real `finalize_run`, the real Typer
command, the real producer method) rather than pinning source text.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.models import Event, Idea, RunState
from looplab.engine.finalize import finalize_run
from looplab.events.eventstore import EventStore
from looplab.cli.run_report import run_opening_split
from looplab.events.replay import event_timestamp, run_wall_clock_seconds

from tests.factories import make_engine


# --------------------------------------------------------------------------- the duration itself

def _ev(seq: int, ts: float, type_: str = "note", data: dict | None = None) -> Event:
    return Event(seq=seq, ts=ts, type=type_, data=data or {})


def test_the_run_duration_comes_from_the_logs_own_first_and_last_timestamps():
    events = [_ev(0, 1000.0), _ev(1, 1120.5), _ev(2, 1300.25)]

    assert run_wall_clock_seconds(events) == pytest.approx(300.25)


def test_the_duration_is_order_tolerant_like_every_other_read_of_the_log():
    """`fold` is order-tolerant (invariant 5) and a duration read beside it must be too — min/max,
    never "the first and last rows I happened to iterate"."""
    events = [_ev(2, 1300.25), _ev(0, 1000.0), _ev(1, 1120.5)]

    assert run_wall_clock_seconds(events) == pytest.approx(300.25)


@pytest.mark.parametrize("ts", [
    0.0,                       # Event.ts DEFAULT — "no timestamp", not 1970
    -1.0,
    True,                      # JSON `true`; isinstance(True, int) is True, so this is the trap
    float("nan"),
    float("inf"),
    253_402_300_800.0,         # past 9999-12-31 — corruption or a milliseconds unit mix-up
    "1000.0",
    None,
])
def test_an_unusable_timestamp_is_skipped_rather_than_defining_the_span(ts):
    """One damaged row must cost one row, not redefine how long the run took."""
    good = [_ev(0, 1000.0), _ev(2, 1300.0)]
    assert event_timestamp(_ev(1, 0.0).model_copy(update={"ts": ts})) is None
    assert run_wall_clock_seconds(
        good + [_ev(1, 0.0).model_copy(update={"ts": ts})]) == pytest.approx(300.0)


def test_a_log_with_no_usable_timestamp_says_unknown_instead_of_zero():
    assert run_wall_clock_seconds([_ev(0, 0.0), _ev(1, 0.0)]) is None
    assert run_wall_clock_seconds([]) is None


# ------------------------------------------------------- the receipt, across a process boundary

class _FinalizeEngine:
    """The optional finalization surface, mirroring `test_finalization_recovery._EngineStub` —
    spelled explicitly so a future gate change fails in a test body, not as a missing attribute."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.store = EventStore(run_dir / "events.jsonl")
        self.archive_resolution = 1.0
        self.researcher = None
        self.developer = None
        self._cross_run_concepts = False
        self._cross_run_curation = False

    def _store_case(self, _state):
        pass

    def _store_research_claims(self, _state):
        pass

    def _store_concept_capsule(self, _state):
        pass

    def _store_concept_curation(self, _state):
        pass

    def _store_claim_curation(self, _state):
        pass

    def _store_task_facets(self, _state):
        pass

    def _write_reflection_note(self, _state):
        pass

    def _sync_card_enrichments(self, _state):
        pass


def _log_that_took(run_dir: Path, seconds: float) -> EventStore:
    """A finished run whose log spans `seconds`, written with explicit timestamps.

    Hand-written rather than appended so the timestamps are the ones a LONG run leaves behind. This
    is also exactly the shape of an OLD log: nothing but `ts`, which every envelope version has had.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    base = time.time() - seconds
    rows = [
        {"v": 1, "seq": 0, "ts": base, "type": "run_started",
         "data": {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"}},
        {"v": 1, "seq": 1, "ts": base + seconds / 2, "type": "node_created",
         "data": {"node_id": 0, "operator": "draft", "parent_ids": []}},
        {"v": 1, "seq": 2, "ts": base + seconds, "type": "run_finished",
         "data": {"reason": "aborted", "after_seq": 1, "finalization_required": True}},
    ]
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return EventStore(run_dir / "events.jsonl")


def _budget(store: EventStore) -> dict:
    rows = [e.data for e in store.read_all() if e.type == "budget"]
    assert rows, "finalization published no budget receipt"
    return rows[-1]


def test_a_run_finalized_by_a_later_process_reports_the_runs_duration_not_the_wrap_ups(tmp_path):
    """The defect, driven end to end: a ~5-minute run, wrapped up by a process that has been alive
    for milliseconds. Before the fix this receipt said 0.0-something."""
    engine = _FinalizeEngine(tmp_path / "run")
    _log_that_took(tmp_path / "run", 300.0)

    # `start_time=time.time()` is what `looplab finalize` passes: THIS process just started.
    finalize_run(engine, entry_finished=False, start_time=time.time())

    receipt = _budget(engine.store)
    # EXACT: the receipt reads the log's own timestamps, so it does not depend on how long this
    # wrap-up took (which is the entire point, and what made the old number load-dependent).
    assert receipt["elapsed_s"] == 300.0
    # …and the process measurement is still published, under a name that says what it measures.
    # A loose bound on purpose — the property is "this is not the run's duration", and the wrap-up's
    # own cost is a real, machine-dependent number that must not be pinned.
    assert receipt["process_s"] < receipt["elapsed_s"] / 5


def test_the_receipt_matches_the_log_it_was_written_from(tmp_path):
    engine = _FinalizeEngine(tmp_path / "run")
    store = _log_that_took(tmp_path / "run", 900.0)

    finalize_run(engine, entry_finished=False, start_time=time.time())

    receipt = _budget(engine.store)
    # Recomputed over the SAME projection a reader would use, on the log as it stood when the
    # receipt was written (finalization keeps appending after it — see the `timings` note).
    prefix = [e for e in store.read_all() if e.type != "budget"][:3]
    assert receipt["elapsed_s"] == run_wall_clock_seconds(prefix) == 900.0


def test_a_log_with_no_usable_timestamps_falls_back_to_the_process_measurement(tmp_path):
    """Reader tolerance at the receipt: never publish a confident 0.0 for "I could not tell"."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"v": 1, "seq": 0, "ts": 0.0, "type": "run_started",
         "data": {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"}},
        {"v": 1, "seq": 1, "ts": 0.0, "type": "run_finished",
         "data": {"reason": "aborted", "after_seq": 0, "finalization_required": True}},
    ]
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    engine = _FinalizeEngine(run_dir)

    finalize_run(engine, entry_finished=False, start_time=time.time() - 42.0)

    receipt = _budget(engine.store)
    # The two are EQUAL — that identity is the fallback, and it holds however slow this box is.
    # (`abs=30` on the 42 s only says the process measurement is the one being reported.)
    assert receipt["elapsed_s"] == receipt["process_s"]
    assert receipt["elapsed_s"] == pytest.approx(42.0, abs=30.0)


# ------------------------------------------------------------------------------ `looplab timings`

def _span(name: str, kind: str, start: float, duration: float, *, span_id: str,
          parent_id: str | None = None, node_id=None) -> dict:
    attributes: dict = {}
    if node_id is not None:
        attributes["node_id"] = node_id
    return {"name": name, "kind": kind, "trace_id": "t" * 32, "span_id": span_id,
            "parent_id": parent_id, "run_id": "r", "attributes": attributes,
            "events": [], "status": "OK", "start": start, "duration_s": duration}


def _run_with_spans(tmp_path: Path, spans: list[dict], *, wall: float = 600.0) -> Path:
    run_dir = tmp_path / "run"
    _log_that_took(run_dir, wall)
    first_ts = json.loads((run_dir / "events.jsonl").read_text().splitlines()[0])["ts"]
    lines = []
    for sp in spans:
        sp = dict(sp)
        sp["start"] = first_ts + sp["start"]     # spans are placed RELATIVE to the log's start
        lines.append(json.dumps(sp))
    (run_dir / "spans.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_dir


def _timings(run_dir: Path, *args: str):
    return CliRunner().invoke(app, ["timings", str(run_dir), *args])


def _row(output: str, section: str, category: str) -> float:
    """The minutes on one row of one section, so a test asserts the NUMBER, not a substring."""
    block = output.split(f"\n{section} — ", 1)[1].split("\n\n", 1)[0]
    for line in block.splitlines():
        parts = line.split()
        if parts and parts[0] == category:
            return float(parts[1])
    raise AssertionError(f"no `{category}` row under `{section}` in:\n{output}")


def test_run_level_work_is_reported_instead_of_dropped(tmp_path):
    """The Researcher/producer half of a real run: spans with no node_id, which used to vanish."""
    run_dir = _run_with_spans(tmp_path, [
        _span("create_node", "operation", 0.0, 120.0, span_id="a", node_id=0),
        _span("generation", "generation", 0.0, 120.0, span_id="b", parent_id="a", node_id=0),
        _span("propose", "operation", 130.0, 180.0, span_id="c"),
        _span("generation", "generation", 130.0, 180.0, span_id="d", parent_id="c"),
        _span("card_build", "operation", 320.0, 60.0, span_id="e"),
        _span("generation", "generation", 320.0, 60.0, span_id="f", parent_id="e"),
    ])

    result = _timings(run_dir)

    assert result.exit_code == 0, result.output
    # The per-node view is unchanged: create_node's SELF time is 0 (its child carries it).
    assert _row(result.output, "node 0", "LLM") == 2.0
    # …and the 4 minutes of researcher + producer work now has a home.
    assert _row(result.output, "run-level", "LLM") == 4.0
    assert "run-level — 4.0 min" in result.output


def test_the_denominator_is_the_runs_own_wall_clock_and_the_residual_is_named(tmp_path):
    """10-minute run, 3 minutes of spans: the missing 7 must be PRINTED, not left to inference."""
    run_dir = _run_with_spans(tmp_path, [
        _span("propose", "operation", 0.0, 60.0, span_id="a"),
        _span("create_node", "operation", 100.0, 120.0, span_id="b", node_id=0),
    ], wall=600.0)

    result = _timings(run_dir)

    assert result.exit_code == 0, result.output
    assert "run wall clock 10.0 min" in result.output
    assert "attributed    3.0 min  (30%)" in result.output
    assert "traced        3.0 min  (30%)" in result.output
    assert "untraced      7.0 min  (70%)" in result.output


def test_concurrent_spans_cannot_make_the_residual_negative(tmp_path):
    """Two overlapping 5-minute spans inside a 6-minute run: the sum exceeds the wall clock, so the
    residual has to come from the UNION of the intervals, not from the sum."""
    run_dir = _run_with_spans(tmp_path, [
        _span("propose", "operation", 0.0, 300.0, span_id="a"),
        _span("deep_research", "operation", 30.0, 300.0, span_id="b"),
    ], wall=360.0)

    result = _timings(run_dir)

    assert "attributed   10.0 min  (167%)" in result.output   # honest about the overlap
    assert "traced        5.5 min  (92%)" in result.output    # union: 0 -> 330 s
    assert "untraced      0.5 min  (8%)" in result.output     # never negative


def test_the_receipt_is_cross_checked_against_the_log_so_an_old_run_shows_the_disagreement(tmp_path):
    """An OLD log still carries the broken receipt. The command computes the truth from `ts`
    anyway and prints both, which is how an operator learns which number to trust."""
    run_dir = _run_with_spans(tmp_path, [
        _span("propose", "operation", 0.0, 60.0, span_id="a"),
    ], wall=600.0)
    EventStore(run_dir / "events.jsonl").append("budget", {"elapsed_s": 0.027, "nodes": 0})

    result = _timings(run_dir)

    assert "run wall clock 10.0 min" in result.output
    assert "budget.elapsed_s says 0.0 min" in result.output


def test_a_damaged_span_line_is_counted_rather_than_charged_to_the_run_as_idle(tmp_path):
    run_dir = _run_with_spans(tmp_path, [
        _span("propose", "operation", 0.0, 60.0, span_id="a"),
    ], wall=600.0)
    with open(run_dir / "spans.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json at all\n")
        f.write("123\n")            # valid JSON, wrong shape — also damage, not a span

    result = _timings(run_dir)

    assert result.exit_code == 0, result.output
    assert "spans.jsonl: 2 of 3 lines unreadable and skipped" in result.output


def test_a_junk_field_on_an_otherwise_valid_span_costs_that_span_not_the_report(tmp_path):
    """The tracer's exporter serializes with `default=str` and promises never to raise into the
    operation it observes, so a non-numeric duration reaches a reader intact. A bare `float()` on it
    would take the whole command down over one span."""
    run_dir = _run_with_spans(tmp_path, [
        _span("propose", "operation", 0.0, 60.0, span_id="a"),
        _span("deep_research", "operation", 100.0, 60.0, span_id="b"),
    ], wall=600.0)
    lines = (run_dir / "spans.jsonl").read_text(encoding="utf-8").splitlines()
    broken = json.loads(lines[1])
    broken["duration_s"] = "a while"
    broken["start"] = None
    (run_dir / "spans.jsonl").write_text(lines[0] + "\n" + json.dumps(broken) + "\n",
                                         encoding="utf-8")

    result = _timings(run_dir)

    assert result.exit_code == 0, result.output
    assert "attributed    1.0 min  (10%)" in result.output
    assert "spans.jsonl: 1 spans carry no `start`" in result.output


def test_a_run_with_tracing_off_still_learns_how_long_it_took(tmp_path):
    """`spans.jsonl` is an optional high-volume sidecar. Its absence must not also cost the one
    number this command can always answer."""
    run_dir = tmp_path / "run"
    _log_that_took(run_dir, 600.0)

    result = _timings(run_dir)

    assert result.exit_code == 2
    assert "run wall clock 10.0 min" in result.output
    assert "no spans.jsonl" in result.output


def test_a_typod_path_is_still_an_error_rather_than_an_empty_report(tmp_path):
    result = _timings(tmp_path / "nope")

    assert result.exit_code == 2
    assert "no run found" in result.output


def test_the_per_node_filter_keeps_its_node_scope(tmp_path):
    """`--node N` is a per-node question; the run-level block and the reconciliation are run-scope
    statements and would be wrong under it. It says so instead of printing them."""
    run_dir = _run_with_spans(tmp_path, [
        _span("create_node", "operation", 0.0, 120.0, span_id="a", node_id=0),
        _span("create_node", "operation", 200.0, 60.0, span_id="b", node_id=1),
        _span("propose", "operation", 300.0, 180.0, span_id="c"),
    ])

    result = _timings(run_dir, "--node", "0")

    assert result.exit_code == 0, result.output
    assert "node 0 — 2.0 min" in result.output
    assert "node 1" not in result.output
    assert "\nrun-level — " not in result.output
    assert "reconciliation vs" not in result.output
    assert "--node 0: run-level work and the reconciliation below are run-scope" in result.output


def test_a_node_id_of_minus_one_stays_its_own_node_not_the_run_bucket(tmp_path):
    """`-1` is a REAL node id here (the run-setup span uses it), which is why the run-level bucket
    cannot be an in-band sentinel."""
    run_dir = _run_with_spans(tmp_path, [
        _span("setup", "operation", 0.0, 60.0, span_id="a", node_id=-1),
        _span("propose", "operation", 100.0, 120.0, span_id="b"),
    ])

    result = _timings(run_dir)

    assert "node -1 — 1.0 min" in result.output
    assert "run-level — 2.0 min" in result.output


# ------------------------------------------------------- the largest thing `timings` could not see

class _TracingDeveloper:
    """A Developer that opens a generation observation the way the real LLM client does."""

    last_files: dict = {}
    last_deleted: tuple = ()

    def implement(self, idea):
        from looplab.core import tracing
        with tracing.generation(op="chat", model="m") as obs:
            obs.usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        return "print('built')"


def _spans_of(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line in
            (run_dir / "spans.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_card_build_producer_is_traced_so_its_llm_calls_reach_spans_jsonl(tmp_path):
    """The producer ran on a worker thread with no span open, so `tracing.generation` — which keys
    off the `_current_tracer` contextvar a live span sets — no-opped for the WHOLE Card build.
    Measured on two real runs, that hid 238 s of one and ~21 min of another from `spans.jsonl`
    while the cost ledger billed every call. This drives it: a Developer that opens a generation,
    and the assertion that the generation reached disk under a `card_build` parent."""
    developer = _TracingDeveloper()
    engine = make_engine(tmp_path / "run", developer=developer)
    idea = Idea(operator="draft", params={"x": 0.25}, rationale="r", card_id="card-0")
    reservation = SimpleNamespace(
        state=RunState(), idea=idea, kind="draft", parent_ids=())
    engine._producer_card_reservation = lambda _request: ({"kind": "draft"}, reservation, None)

    result = engine._build_requested_card(
        {"card_id": "card-0", "generation": 0}, (object(), developer))

    assert result.success is True
    # Engine tracing is intentionally asynchronous.  A direct internal-method test does not cross
    # the Engine.run shutdown barrier, so establish the same owner/read boundary explicitly before
    # inspecting the sidecar.
    assert engine.tracer.force_flush()
    spans = _spans_of(tmp_path / "run")
    build = [s for s in spans if s["name"] == "card_build"]
    assert len(build) == 1, spans
    # A ROOT of its own trace, and node-less: the node it is building does not exist yet, which is
    # exactly why per-node attribution is the wrong home for this cost.
    assert build[0]["parent_id"] is None
    assert build[0]["attributes"].get("node_id") is None
    generations = [s for s in spans if s["kind"] == "generation"]
    assert len(generations) == 1, spans
    assert generations[0]["parent_id"] == build[0]["span_id"]
    assert generations[0]["trace_id"] == build[0]["trace_id"]


def test_the_traced_producer_lands_in_the_run_level_bucket(tmp_path):
    """…and therefore shows up in the report, which is the point of tracing it."""
    developer = _TracingDeveloper()
    engine = make_engine(tmp_path / "run", developer=developer)
    idea = Idea(operator="draft", params={"x": 0.25}, rationale="r", card_id="card-0")
    engine._producer_card_reservation = lambda _request: (
        {"kind": "draft"},
        SimpleNamespace(state=RunState(), idea=idea, kind="draft", parent_ids=()),
        None,
    )
    engine._build_requested_card({"card_id": "card-0", "generation": 0}, (object(), developer))
    assert engine.tracer.force_flush()
    engine.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g",
                                        "direction": "min"})

    result = _timings(tmp_path / "run")

    assert "run-level" in result.output
    assert "op:card_build" in result.output


def test_the_producer_span_survives_an_engine_built_without_a_tracer(tmp_path):
    """`_op_span` is a null context when nothing is traced (tests build `Engine` via `__new__`).
    Tracing must never be able to change whether a Card gets built."""
    from looplab.engine.orchestrator import Engine

    engine = Engine.__new__(Engine)
    idea = Idea(operator="draft", params={"x": 0.25}, rationale="r", card_id="card-0")
    engine._producer_card_reservation = lambda _request: (
        {"kind": "draft"},
        SimpleNamespace(state=RunState(), idea=idea, kind="draft", parent_ids=()),
        None,
    )
    engine._discard_node_build_telemetry = lambda **_kw: None
    engine._reset_developer_footprint = lambda _developer: None
    engine._directed_idea = lambda given, _state: given
    # The card lane hands the finalizer the envelope's own `footprint=` since doc 52 row 12
    # (`speculation.py::_build_requested_card` reads it off the `DeveloperResult`), so the
    # stub accepts the keyword the real method takes.
    engine._finalize_developer_footprint = lambda given, _dev, _code, **_kw: (given, True)

    # No `self.tracer` at all on this engine: the build must still produce its result.
    result = engine._build_requested_card(
        {"card_id": "card-0", "generation": 0}, (object(), _TracingDeveloper()))

    assert result.success is True
    assert result.code == "print('built')"


# ------------------------------------------------- the head of the run (`first-propose-…-gpu-idle`)
# The opening propose is systemically the longest phase of a run and the one nothing can overlap: at
# `n == 0` no node exists, so no evaluation is running and every GPU on the box is idle for the whole
# of it. The 2026-08-25 corpus measured that stretch only as a SUM — run-opening think plus first
# propose — and the split it asks for cannot be reconstructed after the fact, because it lives in
# each run's `spans.jsonl`, a sidecar `fold` never rebuilds and which none of the seven measured runs
# still has. These drive the instrument that makes a run PRODUCE the split instead.

def _opening_log(run_dir: Path, rows: list[dict], *, t0: float | None = None) -> list[dict]:
    """A run log written from `(offset_seconds, type, data)` triples, oldest first."""
    run_dir.mkdir(parents=True, exist_ok=True)
    base = (time.time() - 10_000.0) if t0 is None else t0
    out = [{"v": 1, "seq": i, "ts": base + row["at"], "type": row["type"],
            "data": row.get("data", {})} for i, row in enumerate(rows)]
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in out), encoding="utf-8")
    return out


_OPENING_ROWS = [
    {"at": 0.0, "type": "setup_started", "data": {"phase": "task+data", "repo": False, "goal": "g"}},
    {"at": 180.0, "type": "setup_finished", "data": {"seconds": 180.0, "manifest": {}}},
    {"at": 200.0, "type": "research_attempted",
     "data": {"at_node": 0, "attempt_id": "a1", "manual": False, "trigger": "run_start"}},
    {"at": 700.0, "type": "research_completed",
     "data": {"at_node": 0, "memo": {}, "served_manual": 0, "trigger": "run_start"}},
    {"at": 720.0, "type": "node_building", "data": {"node_id": 0}},
    {"at": 1920.0, "type": "node_created", "data": {"node_id": 0, "operator": "draft",
                                                    "parent_ids": []}},
    {"at": 1980.0, "type": "node_eval_started", "data": {"node_id": 0, "generation": 0}},
    {"at": 9000.0, "type": "node_evaluated", "data": {"node_id": 0}},
]


def _propose_span(events: list[dict], *, at: float, seconds: float) -> dict:
    """The `propose` operation span, placed at an offset from the log's own first row."""
    return _span("propose", "operation", events[0]["ts"] + at, seconds, span_id="p")


def test_the_split_the_item_asks_for_is_a_number_the_run_produces(tmp_path):
    """Run start -> the run-opening think complete, and -> the first propose complete.

    These two are the whole question: the corpus recorded their SUM (a `propose` span of 19.9 min on
    `e5small-dr-unified-v2`, 138.0 min on v3) and nothing that could say which half was the think.
    """
    events = _opening_log(tmp_path / "run", _OPENING_ROWS)
    split = run_opening_split(events, [_propose_span(events, at=720.0, seconds=1194.0)])

    assert split["to_think_seconds"] == pytest.approx(700.0)
    assert split["to_propose_seconds"] == pytest.approx(1914.0)
    assert split["think_to_propose_seconds"] == pytest.approx(1214.0)
    # …and the window they sit in is the one in which nothing could be evaluating.
    assert split["opening_seconds"] == pytest.approx(1980.0)
    assert split["propose_outside_opening"] is False
    assert split["notes"] == []


def test_every_phase_of_the_opening_is_charged_to_a_boundary_the_run_wrote(tmp_path):
    """Disjoint phases plus a SIGNED residual, the same reconciliation shape the command already
    prints — a number that is not explained must be visible, not folded into a neighbour."""
    events = _opening_log(tmp_path / "run", _OPENING_ROWS)
    split = run_opening_split(events, [_propose_span(events, at=720.0, seconds=1194.0)])

    assert {p["name"]: round(p["seconds"], 1) for p in split["phases"]} == {
        "setup": 180.0, "run-start think": 500.0, "first propose": 1194.0,
        "first build": 6.0, "dispatch": 60.0}
    named = sum(p["seconds"] for p in split["phases"])
    assert split["unattributed_seconds"] == pytest.approx(split["opening_seconds"] - named)
    assert split["unattributed_seconds"] == pytest.approx(40.0)     # setup_finished -> the attempt


def test_a_run_that_never_thought_says_so_instead_of_reporting_a_zero(tmp_path):
    """An ABSENT run-opening think is not a think that took no time. Deep research off, no
    researcher wired and a log written before `_ground_run_start` all land here, and reporting 0.0
    would make the item's own question ("which half of those minutes is which") answer itself
    wrongly on every such run."""
    rows = [r for r in _OPENING_ROWS if not r["type"].startswith("research_")]
    events = _opening_log(tmp_path / "run", rows)
    split = run_opening_split(events, [_propose_span(events, at=720.0, seconds=1194.0)])

    assert split["to_think_seconds"] is None
    assert split["think_to_propose_seconds"] is None
    assert "run-opening think" in " ".join(split["notes"])
    assert [p["name"] for p in split["phases"]] == ["setup", "first propose", "first build",
                                                    "dispatch"]


def test_a_cleared_trace_costs_the_propose_row_and_says_which_row_it_lost(tmp_path):
    """`spans.jsonl` is the ONE source here that a run can be missing (it is a sidecar replay never
    rebuilds), so the propose is the one row that can go absent — and when it does, the build row
    falls back to the durable reservation the log does carry rather than vanishing with it."""
    events = _opening_log(tmp_path / "run", _OPENING_ROWS)
    split = run_opening_split(events, [])

    assert split["to_propose_seconds"] is None
    assert split["to_think_seconds"] == pytest.approx(700.0)        # the durable half still answers
    build = next(p for p in split["phases"] if p["name"] == "first build")
    assert build["boundary"] == "node_building -> node_created"
    assert build["seconds"] == pytest.approx(1200.0)
    assert "propose` span" in " ".join(split["notes"])


def test_the_reading_is_order_tolerant_like_every_other_read_of_the_log(tmp_path):
    """Invariant #5's house rule reaches here too: earliest by TIMESTAMP, never by position, so a
    background row spliced into the head of the log cannot move a published number."""
    events = _opening_log(tmp_path / "run", _OPENING_ROWS)
    spans = [_propose_span(events, at=720.0, seconds=1194.0)]
    forward = run_opening_split(events, spans)
    shuffled = run_opening_split(list(reversed(events)), spans)

    assert shuffled["to_think_seconds"] == forward["to_think_seconds"]
    assert shuffled["to_propose_seconds"] == forward["to_propose_seconds"]
    assert shuffled["opening_seconds"] == forward["opening_seconds"]


def test_a_run_that_never_evaluated_is_measured_to_its_last_row_and_says_so(tmp_path):
    """`e5small-dr-unified-v3` spent 2 h 18 min in this window and then died. A run with no
    `node_eval_started` must still report its opening — as a lower bound, named as one."""
    rows = [r for r in _OPENING_ROWS if r["type"] not in ("node_eval_started", "node_evaluated")]
    events = _opening_log(tmp_path / "run", rows)
    split = run_opening_split(events, [_propose_span(events, at=720.0, seconds=1194.0)])

    assert split["open"] is True
    assert split["opening_seconds"] == pytest.approx(1920.0)        # to the last row it has
    assert "lower bound" in " ".join(split["notes"])


def test_a_later_nodes_propose_is_never_charged_to_the_opening(tmp_path):
    """MEASURED on a real offline run, which is why this is a window and not a `min()`: the seed
    path that mints node 0 opens no `propose` span, so the earliest span in the file belongs to a
    LATER node and starts after the first evaluation is already burning. Charging it here would
    publish a number for this phase that was measured somewhere else entirely — and the absence is a
    DIFFERENT fact from "tracing was off", which is why it gets its own note."""
    events = _opening_log(tmp_path / "run", _OPENING_ROWS)
    later = _propose_span(events, at=4000.0, seconds=30.0)      # well past node 0's eval start

    split = run_opening_split(events, [later])
    assert split["to_propose_seconds"] is None
    assert split["propose_outside_opening"] is True
    assert "none of them inside the opening window" in " ".join(split["notes"])
    assert "tracing was off" not in " ".join(split["notes"])


@pytest.mark.parametrize("ts", [0.0, True, "1000.0", float("nan"), None])
def test_a_damaged_timestamp_costs_its_own_row_not_the_split(tmp_path, ts):
    """The same rule `run_wall_clock_seconds` is held to, reached through the same one spelling:
    a row whose `ts` is unusable is skipped, and the phases either side of it still report."""
    events = _opening_log(tmp_path / "run", _OPENING_ROWS)
    events.append({"v": 1, "seq": 99, "ts": ts, "type": "note", "data": {}})
    split = run_opening_split(events, [_propose_span(events, at=720.0, seconds=1194.0)])

    assert split["to_think_seconds"] == pytest.approx(700.0)
    assert split["to_propose_seconds"] == pytest.approx(1914.0)


def test_a_junk_span_duration_costs_that_span_not_the_report(tmp_path):
    """`spans.jsonl` is written by a tracer that promises never to raise into the operation it
    observes, so a non-numeric `duration_s` reaches this reader intact and must not take it down."""
    events = _opening_log(tmp_path / "run", _OPENING_ROWS)
    junk = _propose_span(events, at=720.0, seconds=1194.0)
    junk["duration_s"] = "1194.0"

    split = run_opening_split(events, [junk])
    assert split["to_propose_seconds"] == pytest.approx(720.0)      # placed, but zero-length


def test_the_command_prints_the_opening_under_the_occupancy_block(tmp_path):
    """End to end through the real Typer command: the section exists, names both headlines, and
    lands below the `eval occupancy` bootstrap it explains."""
    run_dir = tmp_path / "run"
    events = _opening_log(run_dir, _OPENING_ROWS)
    (run_dir / "spans.jsonl").write_text(
        json.dumps(_propose_span(events, at=720.0, seconds=1194.0)) + "\n", encoding="utf-8")

    result = _timings(run_dir)
    assert result.exit_code == 0, result.output
    assert "run opening" in result.output
    assert "no evaluation was running for any of it" in result.output
    assert "run start -> the run-opening think complete: 11.7 min" in result.output
    assert "run start -> the first propose complete:     31.9 min" in result.output
    assert result.output.index("eval occupancy") < result.output.index("run opening")


def test_a_spans_only_directory_keeps_its_historical_report(tmp_path):
    """No event log means no boundary at all, and the section prints NOTHING rather than a block of
    absences — `timings` still answers for a spans-only directory, which is why it does not require
    `events.jsonl` in the first place."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "spans.jsonl").write_text(
        json.dumps(_span("propose", "operation", time.time(), 60.0, span_id="p")) + "\n",
        encoding="utf-8")

    result = _timings(run_dir)
    assert "run opening" not in result.output
    assert run_opening_split(None, [])["available"] is False
