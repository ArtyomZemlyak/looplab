"""`tools/log_tools.py` — the log reader and the metric-series query, plus the boundary that bounds them.

Driven, not pinned: every test here builds a real file (or reads a real one from `runs/`) and asserts
on what the tool actually returned. The properties that matter are the ones the module exists for —
a bucket AGGREGATES and never drops, a bounded answer SAYS it was bounded and names the call that
continues it, and a log is reachable only by a NAME the caller declared.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from looplab.tools.log_tools import (
    LogQueryTools,
    LogSource,
    _Clock,
    _squeeze,
    bucket_series,
    scan_series,
)

# The real corpus. READ-ONLY, and every test that touches it skips when it is absent (a checkout
# without `runs/` must still be green), so the suite never depends on a live GPU run's directory.
# `runs/` is untracked, so a git WORKTREE does not have one — `.git` is a FILE there naming the main
# checkout, and following it is what keeps these tests driven rather than permanently skipped for
# anyone developing in a worktree.
def _corpus_roots():
    root = Path(__file__).resolve().parents[1]
    yield root / "runs"
    marker = root / ".git"
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if text.startswith("gitdir:"):
            gitdir = Path(text.split(":", 1)[1].strip())
            # <main>/.git/worktrees/<name> -> <main>
            for parent in gitdir.resolve().parents:
                if parent.name == ".git":
                    yield parent.parent / "runs"
                    return


_V7_NODE1 = next(
    (candidate for base in _corpus_roots()
     for candidate in [base / "rubertlite-dr-unified-v7" / "nodes" / "node_1" / "train.log"]
     if candidate.exists()),
    Path("/nonexistent/train.log"))


def _tools(tmp_path, body: str, name: str = "train.log", **kw):
    path = tmp_path / name
    path.write_bytes(body.encode("utf-8"))
    return LogQueryTools([LogSource(name=name, path=path, role="training", **kw)], default=name)


# ------------------------------------------------------------------------------- rule 1: the boundary
def test_a_log_is_reachable_only_by_a_declared_name(tmp_path):
    """The whole of rule 1. There is no path parameter, so the traversal question never arises — and
    a name that is not declared is refused by LISTING what is, because a model that cannot see the
    map guesses at it."""
    tools = _tools(tmp_path, "hello\n")
    for attempt in ("../../etc/passwd", "/etc/passwd", "train.log/../../secret", "score.log"):
        out = tools.execute("read_log", {"log": attempt, "mode": "tail"})
        assert "no log named" in out
        assert "train.log" in out                      # the map is shown
        assert "Logs are named, not paths" in out
        assert "hello" not in out


def test_an_empty_source_map_refuses_rather_than_reading_anything(tmp_path):
    tools = LogQueryTools([])
    assert "no logs available" in tools.execute("read_log", {})
    assert "no logs available" in tools.execute("metric_series", {"metric": "loss"})


def test_a_resolver_that_raises_is_no_logs_and_not_a_crash():
    """Providers soft-fail (`tools/_base.py`): `execute` returns a string, never raises."""
    def boom():
        raise OSError("mount went away")

    tools = LogQueryTools(boom)
    assert "no logs available" in tools.execute("read_log", {})


def test_the_source_map_is_re_derived_per_call(tmp_path):
    """A CALLABLE source map, because the active stage log MOVES during an eval. A provider frozen at
    construction answers about the stage that was live when the eval started."""
    seen = []

    def resolve():
        seen.append(1)
        path = tmp_path / "train.log"
        path.write_text("step 1\n")
        return [LogSource(name="train.log", path=path)]

    tools = LogQueryTools(resolve, default="train.log")
    tools.execute("read_log", {})
    tools.execute("read_log", {})
    assert len(seen) >= 2


def test_bytes_below_the_attempt_floor_are_never_returned(tmp_path):
    """`LogSource.floor` is the previous eval attempt's bytes. A resumed node must not read a dead
    attempt's curve as its own — the same rule `read_training_tail_raw` enforces on the digest."""
    old = "OLD-ATTEMPT loss: 99.0\n"
    new = "".join(f"new step {i} loss: {10 - i * 0.1}\n" for i in range(40))
    tools = _tools(tmp_path, old + new, floor=len(old.encode()))
    out = tools.execute("read_log", {"mode": "head", "lines": 5})
    assert "OLD-ATTEMPT" not in out
    assert "new step 0" in out
    series = tools.execute("metric_series", {"metric": "loss", "whole_run": True})
    assert "99" not in series


# --------------------------------------------------------------------------------- rule 3: the bounds
def test_a_bounded_answer_says_so_and_names_the_call_that_continues_it(tmp_path):
    """Rule 3. A truncated answer that does not say it was truncated is the failure this whole module
    is about, one level up."""
    body = "".join(f"line {i:05d} padding padding padding\n" for i in range(20_000))
    tools = _tools(tmp_path, body)
    out = tools.execute("read_log", {"mode": "tail", "lines": 5, "max_bytes": 4096})
    assert "earlier bytes not read" in out
    assert 'mode="head"' in out and "max_bytes" in out
    head = tools.execute("read_log", {"mode": "head", "lines": 5, "max_bytes": 4096})
    assert "later bytes not read" in head and 'mode="tail"' in head


def test_the_torn_leading_record_of_a_seek_is_dropped_and_said(tmp_path):
    tools = _tools(tmp_path, "".join(f"record-{i:05d}-xxxxxxxxxxxxxxxx\n" for i in range(5_000)))
    out = tools.execute("read_log", {"mode": "tail", "lines": 3, "max_bytes": 1000})
    assert "torn by the seek" in out
    for row in out.splitlines():
        if row.startswith("record-"):
            assert row.startswith("record-0")          # never half a record


def test_a_range_past_the_scanned_window_says_how_to_reach_it(tmp_path):
    tools = _tools(tmp_path, "a\nb\nc\n")
    out = tools.execute("read_log", {"mode": "range", "start": 99, "lines": 3})
    assert "past the 3 records" in out and "max_bytes" in out


def test_the_result_stays_under_the_agent_loop_cap(tmp_path):
    """Our own honest truncation must be the one that decides, not `drive_tool_loop`'s blunt cut."""
    from looplab.tools._base import RESULT_CAP
    body = "".join(f"{i} loss: {1.0 / (i + 1):.9f} some quite long trailing text here\n"
                   for i in range(50_000))
    tools = _tools(tmp_path, body)
    for args in ({"mode": "tail", "lines": 2000}, {"mode": "search", "pattern": "loss", "lines": 100}):
        assert len(tools.execute("read_log", args)) <= RESULT_CAP
    assert len(tools.execute("metric_series", {"metric": "loss", "whole_run": True})) <= RESULT_CAP


# -------------------------------------------------------------------------------- reading it like a file
def test_a_carriage_return_re_render_is_its_own_record(tmp_path):
    """A tqdm bar writes its whole multi-hour life into ONE newline-delimited line. A line-oriented
    reader answers 'the last 60 lines' with one line, and it is the wrong line."""
    bar = "".join(f"\r{i}%|" + "#" * 30 + f"| {i}/100 [0:00:{i:02d}<0:01:00,  1.00s/it]"
                  for i in range(1, 60))
    tools = _tools(tmp_path, "starting\n" + bar)
    out = tools.execute("read_log", {"mode": "tail", "lines": 4})
    assert out.count("/100") >= 4                       # four separate records, not one


def test_progress_fill_is_squeezed_without_losing_a_number():
    record = "45%|" + "█" * 150 + "▍" + " " * 200 + "| 8004/17650 [3:24:37<4:01:19,  1.50s/it]"
    squeezed = _squeeze(record)
    assert len(squeezed) < len(record) / 4
    for token in ("45%", "8004/17650", "3:24:37", "4:01:19", "1.50s/it"):
        assert token in squeezed


def test_raw_true_keeps_the_fill(tmp_path):
    tools = _tools(tmp_path, "45%|" + "█" * 60 + "| 1/2 [0:00:01<0:00:01,  1.00s/it]\n")
    assert "█" * 40 in tools.execute("read_log", {"mode": "tail", "raw": True})
    assert "█" * 40 not in tools.execute("read_log", {"mode": "tail"})


def test_search_reports_hit_count_and_context(tmp_path):
    body = "".join(("boom Traceback here\n" if i in (10, 400) else f"quiet {i}\n") for i in range(1000))
    tools = _tools(tmp_path, body)
    out = tools.execute("read_log", {"mode": "search", "pattern": "traceback", "context": 1})
    assert "2 match(es)" in out
    assert out.count("Traceback") == 2
    assert "quiet 9" in out and "quiet 11" in out       # context either side


def test_context_zero_is_askable(tmp_path):
    """`int(args.get("context") or 1)` reads naturally and makes 'just the hits' impossible to ask
    for — `_int_arg` is why 0 survives."""
    body = "".join(("HIT\n" if i == 5 else f"quiet {i}\n") for i in range(20))
    out = _tools(tmp_path, body).execute("read_log",
                                         {"mode": "search", "pattern": "HIT", "context": 0})
    assert "HIT" in out and "quiet 4" not in out


def test_a_bad_pattern_is_a_message_not_an_exception(tmp_path):
    out = _tools(tmp_path, "x\n").execute("read_log", {"mode": "search", "pattern": "([unclosed"})
    assert "not a valid regular expression" in out


def test_a_search_that_matches_nothing_says_where_it_looked(tmp_path):
    out = _tools(tmp_path, "quiet\n").execute("read_log", {"mode": "search", "pattern": "zzz"})
    assert "no record matches" in out and "bytes read" in out


# ------------------------------------------------------------------------------------------ the clock
def test_a_nested_concurrent_bar_does_not_advance_the_run_clock():
    """MEASURED failure #1: summing every bar restart reported `runs/rubertlite-dr-unified-v7` node 1
    as 12.5 hours against a real 3.5. A short inner bar restarting inside a long outer one has
    finished nothing the run's clock should count."""
    text = "".join(
        f"\r 1/9999 [1:00:0{i}<9:00:00] inner {i % 3}/3 [0:00:0{i % 3}<0:00:03] loss: {10 - i}\n"
        for i in range(6))
    samples, clock = scan_series(text, "loss")
    assert samples
    # The outer lane advances by 5 s across the six records; the inner lane restarts twice and must
    # contribute at most its own few seconds, never 2 x 3 s x every restart.
    assert 3600 <= samples[-1].t <= 3620


def test_a_restarting_bar_in_its_own_lane_accumulates():
    """MEASURED failure #2: ignoring restarts reported `rubertlite-dense-retrieval` node 36 as 108
    seconds against a real ~1.2 h. A bar that restarts in its OWN lane finished, and its time is real."""
    text = "".join(f"\rEpoch {e}: {n}/100 [0:00:{n // 2:02d}<0:00:30] loss: {10 - e}\n"
                   for e in range(5) for n in (20, 60, 100))
    samples, _ = scan_series(text, "loss")
    # Each epoch bar reaches 0:00:50 and then restarts at 0:00:10; four completed epochs plus the
    # fifth's own elapsed. Ignoring the restart would report 50 seconds for the whole thing.
    assert samples[-1].t == pytest.approx(250.0, abs=1.0)


def test_the_clock_is_monotone_and_names_its_source():
    text = ("2026-08-14 07:23:03 start\n"
            + "".join(f"\r{i}/100 [0:00:{i:02d}<0:01:00] loss: {5 - i * 0.01}\n" for i in range(30)))
    samples, clock = scan_series(text, "loss")
    assert [s.t for s in samples] == sorted(s.t for s in samples)
    assert "timestamps" in clock.source() and "progress-bar" in clock.source()
    assert clock.wall(0.0).startswith("2026-08-14 07:23:03")


def test_a_log_with_no_clock_at_all_says_so():
    _samples, clock = scan_series("loss: 1.0\nloss: 0.9\n", "loss")
    assert "no timestamp and no progress bar" in clock.source()


def test_a_clock_never_regresses_on_a_bogus_timestamp():
    clock = _Clock()
    clock.observe("2026-08-14 07:00:00 a")
    clock.observe("2026-08-14 08:00:00 b")
    high = clock.t
    clock.observe("2026-08-14 06:00:00 c")       # an out-of-order line must not rewind the window
    assert clock.t == high


# ------------------------------------------------------------------------------- aggregation, not dropping
def test_every_sample_lands_in_exactly_one_bucket():
    """THE rule. Coarser buckets aggregate MORE samples, never fewer — a thinned series is the
    sampling artifact this module exists to end."""
    samples, _ = scan_series(
        "".join(f"\r{i}/500 [0:00:{i % 60:02d}<0:10:00] loss: {100 - i * 0.1}\n" for i in range(500)),
        "loss")
    span = samples[-1].t - samples[0].t
    assert span > 0
    for width in (1.0, 5.0, span / 3, span):
        rows = bucket_series(samples, start=samples[0].t, end=samples[-1].t, width=width)
        assert sum(r["n"] for r in rows) == len(samples), width


def test_a_bucket_reports_the_spread_it_reduced():
    samples, _ = scan_series("".join(f"\r{i}/10 [0:00:0{i}<0:00:10] loss: {i}\n" for i in range(10)),
                             "loss")
    rows = bucket_series(samples, start=samples[0].t, end=samples[-1].t, width=1000.0)
    assert len(rows) == 1
    row = rows[0]
    assert row["n"] == 10 and row["min"] == 0.0 and row["max"] == 9.0
    assert row["first"] == 0.0 and row["last"] == 9.0 and row["spread"] > 0


def test_too_many_buckets_is_refused_with_the_width_that_fits(tmp_path):
    """Never a silently thinned series: an over-cap granularity is a refusal that names a workable one,
    because dropping buckets would stop them being an aggregate."""
    body = "".join(f"\r{i}/5000 [{i // 3600}:{(i // 60) % 60:02d}:{i % 60:02d}<0:00:01] loss: 1.0\n"
                   for i in range(0, 40_000, 7))
    out = _tools(tmp_path, body).execute(
        "metric_series", {"metric": "loss", "bucket_s": 1, "whole_run": True})
    assert "above the 200 cap" in out and "never silently dropped" in out and "bucket_s>=" in out


def test_a_non_finite_reading_is_shown_and_counted_not_silently_dropped(tmp_path):
    """`grad_norm: nan` is the earliest honest sign of a blown-up run and is the single most useful
    thing a role can be shown about one. Excluded from aggregates, never from the answer."""
    body = "".join(f"\r{i}/10 [0:00:0{i}<0:00:10] " + "{'loss': 0.0, 'grad_norm': nan}\n"
                   for i in range(9))
    out = _tools(tmp_path, body).execute("metric_series", {"metric": "grad_norm", "whole_run": True})
    assert "non-finite" in out and "NONFINITE" in out


def test_the_series_answer_states_it_is_candidate_authored(tmp_path):
    """docs/36: what a role reads here may inform what happens NEXT and must never become the FACT a
    record rests on. The answer says so where the reader is."""
    out = _tools(tmp_path, "\r1/2 [0:00:01<0:00:01] loss: 1.0\n").execute(
        "metric_series", {"metric": "loss"})
    assert "text the TRAINING SCRIPT printed" in out and "not a measured metric" in out


def test_a_missing_metric_says_how_to_find_what_the_log_does_name(tmp_path):
    out = _tools(tmp_path, "\r1/2 [0:00:01<0:00:01] loss: 1.0\n").execute(
        "metric_series", {"metric": "perplexity"})
    assert "no `perplexity` reading" in out and 'mode="search"' in out


def test_the_default_read_is_a_tail_and_whole_run_is_the_opt_in(tmp_path):
    """Cost: the common call must be the cheap one. The default reads a tail; reaching the run's
    opening values is something the caller asks for, and the receipt states what it cost."""
    body = "\r1/9999 [0:00:01<9:00:00] loss: 999.0\n" + "x" * 400_000 + "\n" \
           + "\r9998/9999 [3:00:00<0:00:01] loss: 1.0\n"
    tools = _tools(tmp_path, body)
    cheap = tools.execute("metric_series", {"metric": "loss"})
    assert "first=999" not in cheap and "earlier bytes not scanned" in cheap
    whole = tools.execute("metric_series", {"metric": "loss", "whole_run": True})
    assert "first=999" in whole


def test_a_window_escalates_the_read_until_it_is_covered(tmp_path):
    """`last_s` says how much to KEEP; the reader escalates the tail until that much is READ. This is
    what keeps 'the last 10 seconds' one small seek while 'the last day' still reaches the start."""
    body = "".join(f"\r{i}/20000 [{i // 3600}:{(i // 60) % 60:02d}:{i % 60:02d}<0:00:01] "
                   f"loss: {20000 - i}\n" for i in range(0, 20_000, 3))
    tools = _tools(tmp_path, body)
    narrow = tools.execute("metric_series", {"metric": "loss", "last_s": 10})
    wide = tools.execute("metric_series", {"metric": "loss", "last_s": 86_400, "bucket_s": 3600})
    assert int(narrow.split("Read ")[1].split(" of ")[0].replace(",", "")) \
        <= int(wide.split("Read ")[1].split(" of ")[0].replace(",", ""))
    assert "in 2 reads" in wide or "in 3 reads" in wide or "in 4 reads" in wide


# ------------------------------------------------------------------- driven against the real corpus
def _named_numbers(text: str) -> dict:
    """The `key=value` tokens of one answer line, for the keys whose value is a plain number."""
    numbers = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep:
            continue
        try:
            numbers[key] = float(value)
        except ValueError:      # `step=2370/17650`, and anything else that is not a reading
            continue
    return numbers


def _bucket_rows(answer: str) -> list[dict]:
    return [_named_numbers(line) for line in answer.splitlines()
            if line.startswith("+") and "med=" in line]


def _window(answer: str) -> dict:
    return _named_numbers(
        next(line for line in answer.splitlines() if line.startswith("window:")))


@pytest.mark.skipif(not _V7_NODE1.exists(), reason="the runs/ corpus is not in this checkout")
def test_the_operators_two_questions_on_a_real_five_hour_run():
    """The two questions the operator named, asked of the log that produced the wrong verdict.

    The hourly answer is the point: aggregating whole hours resolves movement that a ~10-second
    slice of the same file cannot show, which is exactly what "pinned at ~23.0, no learning trend"
    was a correct reading of the slice and a wrong verdict about the run.

    ASSERTED AS PROPERTIES, NOT AS THIS FILE'S NUMBERS, because `runs/` is a live corpus and not a
    fixture: this box runs experiments, and on 2026-08-14 this very node was repaired and RESTARTED,
    which appended a second training run beginning at step 10 with loss back around 45. The old
    `medians[0] > medians[-1]` ("the curve descends from the first hour to the last") was a fact
    about the file's contents on the day it was written, and it stopped being true while every
    property the tool exists for stayed true. Re-pinning the numbers would only re-arm the same trap
    for the next restart, so what is asserted below is what cannot be invalidated by one: the slice
    cannot see the hours, and the aggregate resolves movement orders of magnitude beyond the spread
    inside its quietest bucket — which is precisely what ten consecutive readings can ever show.
    """
    tools = LogQueryTools([LogSource(name="train.log", path=_V7_NODE1, role="training")],
                          default="train.log")
    last_10s = tools.execute("metric_series", {"metric": "loss", "last_s": 10})
    assert "reading(s)" in last_10s and "Read 262,144 of" in last_10s   # ONE small seek

    hourly = tools.execute("metric_series",
                           {"metric": "loss", "last_s": 86_400, "bucket_s": 3600, "whole_run": True})
    assert "buckets of 1:00:00" in hourly
    rows = _bucket_rows(hourly)
    if len(rows) < 3:
        # The precondition, not a result: this is the "real five-hour run" test, and the corpus is
        # mutable. A log that no longer spans three hours (a fresh restart, a rotated file) cannot
        # answer the question, and saying so is honest where asserting over it would be noise.
        pytest.skip("the preserved node no longer holds a multi-hour training log")

    medians = [row["med"] for row in rows]
    # THE TREND, stated so a restart cannot forge or hide it: the run gets better than its own first
    # hour. (Not "the last hour is the best" — a repaired node's restart appends a fresh high-loss
    # run at the end, which is exactly what happened here.)
    assert min(medians) < medians[0]
    # THE REASON THE BUCKETS EXIST. Movement ACROSS hours is more than an order of magnitude larger
    # than the spread WITHIN the quietest hour — i.e. beyond anything a handful of consecutive
    # readings, which all land inside one bucket, could ever reveal.
    assert max(medians) - min(medians) > 10 * min(row["iqr"] for row in rows)
    # ...and the slice the judge actually read shows less movement than the aggregate does, over a
    # window that covers seconds of a run measured in hours.
    slice_window = _window(last_10s)
    assert slice_window["max"] - slice_window["min"] < max(medians) - min(medians)


@pytest.mark.skipif(not _V7_NODE1.exists(), reason="the runs/ corpus is not in this checkout")
def test_a_default_read_of_a_multi_megabyte_live_log_is_cheap():
    """Cost, as a property rather than a claim: the default `read_log` touches a bounded window of a
    file that is megabytes wide."""
    assert os.path.getsize(_V7_NODE1) > 1_000_000
    tools = LogQueryTools([LogSource(name="train.log", path=_V7_NODE1)], default="train.log")
    out = tools.execute("read_log", {"mode": "tail", "lines": 5})
    covered = out.splitlines()[0]
    assert "bytes total" in covered and "this answer covers bytes" in covered
    assert "earlier bytes not read" in out
