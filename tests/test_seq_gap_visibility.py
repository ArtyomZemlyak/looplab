"""A run whose event log stops being readable part-way must never render as a smaller run.

The defect this drives, measured on the shipped corpus: `runs/rubertlite-dense-retrieval` holds 1,624
physical rows, EVERY one of them valid JSON, and `EventStore.read_all()` returns 20. Seqs 20-24 are
absent from the file, so `event_sequence_continues`'s dense-prefix fence ends the recoverable prefix
at line 21 and 1,603 durable records are dropped from every fold. The store computed exactly that
(`divergence = {good_records: 20, corrupt_line: 21, dropped_lines: 1603}`) and the receipt went
nowhere an operator looks: on the same server in the same second the timeline pager reported
`total_events: 1624, torn_tail: false` while `/state` reported `event_count: 20`, and the run-list row
read `nodes: 2, best_metric: 0.8077` for a run whose own `budget` row says `nodes: 81`.

The gap itself is NOT an engine defect and this file does not pretend otherwise — the run's own
sibling backup `events.jsonl.bak-node2clear` still holds the five missing rows (node 2's
`node_building`/`novelty_rejected`/`node_created`/`foresight_selected`/`workspace_seeded`), i.e. they
were written by the engine and then deleted from the append-only log by hand. A hand-edited log is
precisely what the dense fence exists to catch, and it caught it. What was missing is the SAYING SO.

Every assertion below drives the shipped path — a real log on disk, folded through the real reader,
served through the real HTTP app or the real Typer command — because the property is "the operator is
told", and a source pin cannot tell a printed sentence from a comment about one.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.events.eventstore import (
    EventStore, integrity_sentence, log_divergence, log_integrity)

REPO_RUNS = Path(__file__).resolve().parents[1] / "runs"


def _row(seq: int, type_: str, data: dict | None = None) -> str:
    return json.dumps({"v": 1, "seq": seq, "ts": 1783555825.0 + seq, "type": type_,
                       "data": data or {}, "trace_id": None, "span_id": None})


def _intact_log(rd: Path, n: int = 12) -> Path:
    """A dense, ordinary log: seq 0..n-1 with a run the fold can actually read."""
    rd.mkdir(parents=True, exist_ok=True)
    rows = [_row(0, "run_started", {"task_id": "t", "goal": "g", "direction": "min"})]
    rows += [_row(i, "node_building", {"node_id": i}) for i in range(1, n)]
    log = rd / "events.jsonl"
    log.write_text("".join(r + "\n" for r in rows), encoding="utf-8")
    return log


def _gapped_log(rd: Path, *, good: int = 6, gap: int = 5, after: int = 40) -> Path:
    """`rubertlite-dense-retrieval`'s shape in miniature: a dense prefix, then a jump of `gap`
    reserved-but-absent seqs, then a long tail of perfectly valid records nobody will ever see."""
    log = _intact_log(rd, good)
    tail = [_row(good + gap + i, "node_building", {"node_id": 100 + i}) for i in range(after)]
    with open(log, "a", encoding="utf-8") as f:
        f.write("".join(r + "\n" for r in tail))
    return log


# --- the receipt itself ---------------------------------------------------------------------------

def test_gapped_log_reports_the_boundary_and_what_is_behind_it(tmp_path):
    log = _gapped_log(tmp_path / "run", good=6, gap=5, after=40)
    receipt = log_integrity(log)
    assert receipt["complete"] is False
    # Derived from the bytes, not inferred: the numbers are the fence's own boundary.
    assert receipt["good_records"] == 6 and receipt["corrupt_line"] == 7
    assert receipt["dropped_lines"] == 39   # 6 good + the boundary row + 39 behind it = 46 lines
    # ...and they are exactly what the store already computed for the writer.
    assert log_divergence(log) == {"good_records": 6, "corrupt_line": 7, "dropped_lines": 39}
    assert len(EventStore(log).read_all()) == 6      # the fold really does see 6 of 46


def test_intact_log_reports_nothing_at_all(tmp_path):
    """The negative control. A notice that fires on a healthy run is a notice operators learn to
    ignore, which costs more than not having one."""
    log = _intact_log(tmp_path / "run", 12)
    assert log_integrity(log) == {"complete": True}
    assert integrity_sentence(log_integrity(log)) == ""
    assert len(EventStore(log).read_all()) == 12


def test_unreadable_log_is_incomplete_and_never_complete(tmp_path):
    """"We could not look" must never render as "we looked and it is fine"."""
    missing = tmp_path / "nope" / "events.jsonl"
    # An absent file has no divergence (nothing to diverge), so this is the DIRECTORY case: a path
    # that cannot be read as a file at all.
    (tmp_path / "adir").mkdir()
    receipt = log_integrity(tmp_path / "adir")
    assert receipt["complete"] is False and receipt["unreadable"] is True
    assert "could not be read" in integrity_sentence(receipt)
    assert log_integrity(missing) == {"complete": True}   # absent == nothing claimed, per log_divergence


def test_the_sentence_states_the_denominator_and_refuses_an_absence_claim(tmp_path):
    note = integrity_sentence(log_integrity(_gapped_log(tmp_path / "run")), run_label="demo")
    assert "6 of 46 records" in note        # good + the boundary row + dropped == the file
    assert "39 durable record(s)" in note   # ...and that the rest is ON DISK
    assert "not evidence that the rest did not happen" in note


def test_one_wire_shape_serves_every_http_surface():
    """Five endpoints publish this receipt about the same file. Two of them describing it in two
    shapes is the ORIGINAL defect's own signature (`total_events: 1624` beside `event_count: 20`), so
    the widened shape and the declared response model are cross-checked rather than kept in step by
    hand — the same rule the duck-typed seams in CLAUDE.md are guarded by."""
    pytest.importorskip("fastapi")
    from looplab.events.eventstore import INTEGRITY_WIRE_FIELDS, integrity_wire
    from looplab.serve.routers.runs import RunSourceIntegrity
    assert set(RunSourceIntegrity.model_fields) == set(INTEGRITY_WIRE_FIELDS)
    assert set(integrity_wire({"complete": True})) == set(INTEGRITY_WIRE_FIELDS)
    # Widening never invents or drops a value.
    assert integrity_wire({"complete": False, "good_records": 20, "corrupt_line": 21,
                           "dropped_lines": 1603}) == {
        "complete": False, "good_records": 20, "corrupt_line": 21,
        "dropped_lines": 1603, "unreadable": None}


# --- the CLI read commands ------------------------------------------------------------------------

def test_replay_states_the_boundary_on_stderr_and_still_prints_the_prefix(tmp_path):
    """`looplab replay` used to fold 20 records of a 1,624-record log and print them as the run.

    stderr, not stdout: a caller piping this into `jq` keeps clean JSON, and a human cannot miss it.
    """
    rd = tmp_path / "run"
    _gapped_log(rd)
    res = CliRunner().invoke(app, ["replay", str(rd)])
    assert res.exit_code == 0                     # a read command still ANSWERS; it does not refuse
    assert json.loads(res.stdout)                 # ...and stdout is still parseable JSON
    assert "[INCOMPLETE RECORD]" in res.stderr and "line 7" in res.stderr


def test_replay_of_an_intact_log_says_nothing(tmp_path):
    rd = tmp_path / "run"
    _intact_log(rd)
    res = CliRunner().invoke(app, ["replay", str(rd)])
    assert res.exit_code == 0 and res.stderr == ""


def test_inspect_states_the_boundary_before_the_result_it_derives(tmp_path):
    """`inspect` bypasses `_require_run_dir` (its input may be a config snapshot with no log), so it
    is the one that most easily drifts back to silent."""
    rd = tmp_path / "run"
    _gapped_log(rd)
    res = CliRunner().invoke(app, ["inspect", str(rd)])
    assert res.exit_code == 0
    assert "[INCOMPLETE RECORD]" in res.stderr


def test_timings_states_the_boundary_because_the_log_is_its_denominator(tmp_path):
    rd = tmp_path / "run"
    _gapped_log(rd)
    res = CliRunner().invoke(app, ["timings", str(rd)])
    assert "[INCOMPLETE RECORD]" in res.stderr


def test_a_mutating_command_still_fails_closed_rather_than_narrating(tmp_path):
    """The read/write split is unchanged: `healthy=True` refuses, it does not warn and continue."""
    rd = tmp_path / "run"
    _gapped_log(rd)
    res = CliRunner().invoke(app, ["stop", str(rd)])
    assert res.exit_code == 2
    assert "repair-log" in res.stderr


# --- the HTTP surfaces ----------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    fastapi = pytest.importorskip("fastapi")           # [ui] extra
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    del fastapi
    return TestClient(make_app(tmp_path)), tmp_path


def test_run_state_carries_the_receipt_beside_the_count_it_qualifies(client):
    api, root = client
    _gapped_log(root / "demo")
    body = api.get("/api/runs/demo/state").json()
    assert body["event_count"] == 6                        # unchanged: the prefix IS what folded
    assert body["source_integrity"] == {
        "complete": False, "good_records": 6, "corrupt_line": 7, "dropped_lines": 39,
        "unreadable": None}
    # Mirrored into the projection too, because that is the object the browser actually receives.
    assert body["state"]["source_integrity"]["complete"] is False


def test_run_state_of_an_intact_log_says_complete(client):
    api, root = client
    _intact_log(root / "demo")
    body = api.get("/api/runs/demo/state").json()
    # All four detail keys are on the wire as null — see `RunSourceIntegrity`: a client must never
    # have to tell "absent because healthy" from "absent because this server is older".
    assert body["source_integrity"] == {
        "complete": True, "good_records": None, "corrupt_line": None,
        "dropped_lines": None, "unreadable": None}
    assert body["state"]["source_integrity"]["complete"] is True


def test_the_receipt_survives_the_state_payload_cache(client):
    """The SSE hot path serves from a file-identity-keyed cache; a receipt that only appeared on the
    MISS would be absent from every frame after the first."""
    api, root = client
    _gapped_log(root / "demo")
    first = api.get("/api/runs/demo/state").json()
    second = api.get("/api/runs/demo/state").json()       # cache hit: same file identity
    assert first["source_integrity"] == second["source_integrity"]
    assert second["state"]["source_integrity"]["complete"] is False


def test_the_run_list_row_cannot_report_two_nodes_without_saying_so(client):
    api, root = client
    _gapped_log(root / "demo")
    _intact_log(root / "clean")
    rows = {r["run_id"]: r for r in api.get("/api/runs").json()}
    assert rows["demo"]["source_integrity"]["complete"] is False
    assert rows["demo"]["source_integrity"]["dropped_lines"] == 39
    assert rows["clean"]["source_integrity"]["complete"] is True


def test_the_lifecycle_probe_carries_it_too(client):
    api, root = client
    _gapped_log(root / "demo")
    body = api.get("/api/runs/demo/lifecycle").json()
    assert body["event_count"] == 6
    assert body["source_integrity"]["complete"] is False


def test_a_historical_fold_is_a_prefix_of_a_prefix_and_says_so(client):
    """An operator scrubbed back to seq 3 of a truncated log is two truncations deep. The receipt is
    a fact about the RECORD, so it must not be silenced by an `upto_seq` read."""
    api, root = client
    _gapped_log(root / "demo")
    body = api.get("/api/runs/demo/state?upto_seq=3").json()
    assert body["source_integrity"]["complete"] is False


def test_the_pager_and_the_fold_stop_contradicting_each_other(client):
    """The sharpest pair in the 2026-08-07 audit: `/log-page` answered `total_events: 1624,
    torn_tail: false` in the same second `/state` answered `event_count: 20`, and neither surface
    knew the other existed. The pager keeps counting PHYSICAL rows — that is the one thing it can do
    that the fold cannot — but it may no longer assert health it never checked."""
    api, root = client
    _gapped_log(root / "demo")
    page = api.get("/api/runs/demo/log-page").json()
    assert page["total_events"] == 46 and page["torn_tail"] is False   # the file, unchanged
    assert page["source_integrity"]["complete"] is False               # ...and now qualified
    assert page["source_integrity"]["good_records"] == 6
    assert api.get("/api/runs/demo/state").json()["event_count"] == 6  # the two now agree on WHY


def test_cost_distinguishes_unreadable_from_absent_from_zero(client):
    """`{"cost": 0.0, "recorded": false}` is this route's way of saying "no roll-up exists". A
    truncated log produces a THIRD state that renders identically — on the real run, a `recorded:
    false` body for 3,497 paid calls whose `llm_cost` rows are behind the boundary."""
    api, root = client
    _gapped_log(root / "demo")
    body = api.get("/api/runs/demo/cost").json()
    assert body["recorded"] is False and body["cost"] == 0.0     # unchanged for every client
    assert body["source_integrity"]["complete"] is False         # ...but no longer indistinguishable
    _intact_log(root / "clean")
    assert api.get("/api/runs/clean/cost").json()["source_integrity"]["complete"] is True


# --- the regression net over the real corpus ------------------------------------------------------

@pytest.mark.skipif(not REPO_RUNS.is_dir(), reason="no runs/ corpus in this checkout")
def test_every_real_run_still_loads_exactly_as_it_does_today():
    """The receipt must be a STATEMENT and never a fence: no real log may start reading differently.

    Ten of these runs legitimately return MORE records than physical lines — an `append_many`
    envelope carries several events on one line — and that is correct behaviour this change must not
    "fix". Exactly one run in the corpus loses anything, and this pins WHICH.
    """
    losses, seen = {}, 0
    for rd in sorted(REPO_RUNS.iterdir()):
        log = rd / "events.jsonl"
        if not log.is_file():
            continue
        seen += 1
        physical = sum(1 for line in log.read_bytes().split(b"\n")[:-1] if line.strip())
        records = len(EventStore(log).read_all())
        receipt = log_integrity(log)
        # The receipt and the reader must agree in BOTH directions on every real log.
        assert receipt["complete"] is (log_divergence(log) is None), rd.name
        if receipt["complete"]:
            # A complete log's reader reached the end; a batch envelope may expand PAST the line
            # count, which is why this is not an equality.
            assert records >= physical or physical - records <= 1, (rd.name, physical, records)
        else:
            losses[rd.name] = (physical, records, receipt)
    assert seen >= 40, f"expected the shipped corpus, found {seen} runs"
    assert list(losses) == ["rubertlite-dense-retrieval"], losses
    physical, records, receipt = losses["rubertlite-dense-retrieval"]
    assert (physical, records) == (1624, 20)
    assert receipt == {"complete": False, "good_records": 20,
                       "corrupt_line": 21, "dropped_lines": 1603}


@pytest.mark.skipif(not (REPO_RUNS / "rubertlite-dense-retrieval").is_dir(),
                    reason="no runs/ corpus in this checkout")
def test_the_real_run_is_visible_through_the_shipped_http_path(tmp_path):
    """The end-to-end proof, on the actual bytes — a COPY, because `runs/` is the authoritative
    record and a lost-events log is evidence. Only `events.jsonl` is copied: the point is that the
    server states the boundary from the log alone, with no sidecar to lean on."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    src = REPO_RUNS / "rubertlite-dense-retrieval" / "events.jsonl"
    rd = tmp_path / "rubertlite-dense-retrieval"
    rd.mkdir()
    (rd / "events.jsonl").write_bytes(src.read_bytes())
    api = TestClient(make_app(tmp_path))
    body = api.get("/api/runs/rubertlite-dense-retrieval/state").json()
    assert body["event_count"] == 20                      # the confident partial view, unchanged...
    assert body["source_integrity"] == {                  # ...and now it is qualified
        "complete": False, "good_records": 20, "corrupt_line": 21, "dropped_lines": 1603,
        "unreadable": None}
    row, = [r for r in api.get("/api/runs").json()
            if r["run_id"] == "rubertlite-dense-retrieval"]
    assert row["nodes"] == 2                              # the row that read as a two-node run...
    assert row["source_integrity"]["dropped_lines"] == 1603   # ...now carries its own denominator
    note = integrity_sentence(log_integrity(rd / "events.jsonl"), run_label="the run")
    assert "20 of 1624 records" in note   # the file's own physical row count, exactly
    assert os.path.getsize(src) == os.path.getsize(rd / "events.jsonl")   # the original is untouched
