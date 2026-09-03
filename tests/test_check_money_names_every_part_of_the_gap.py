"""The sweep reconciles money every time, by hand, and the hand agreed for the wrong reason.

On 2026-09-01 23:32, with every probe stopped, spans summed to $43.587947 against a counter of
$43.588034 -- sixty-nine calls apart. Earlier sweeps had "matched to the microcent"; they matched
because probes were mid-flight and their unrecorded spend was larger than $0.000086 in the other
direction. The agreement was real and the precision was luck.

The gap is entirely nameable: one preflight call per probe (10 prompt tokens, 2 completion, about
$0.000002, never a `generation` span) plus the calls the gateway killed during the unstreamed era,
which were billed nothing because they returned nothing. What matters to an operator is the RESIDUE
after those are removed, and that is what the exit code is about.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import sys as _sys
REPO = Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(REPO / "benchmarks"))
import check_money as cm
TOOL = REPO / "benchmarks" / "check_money.py"


def _now() -> str:
    """A ledger timestamp that counts as in flight."""
    import time as _t
    return str(_t.time())


def _serve(payload: dict) -> tuple[int, threading.Thread, socket.socket]:
    """A one-shot /healthz that answers in TWO writes, so a single recv() cannot see the body."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        try:
            conn, _ = srv.accept()
            conn.recv(4096)
            body = json.dumps(payload).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body))
            import time
            time.sleep(0.05)                      # the split that broke `recv(600)` in the sweep
            conn.sendall(body)
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return port, t, srv


def _bench(tmp_path: Path, *, probes: dict, meter_rows: list) -> Path:
    root = tmp_path / "bench"
    for probe, gens in probes.items():
        run = root / "model-probes" / probe / "runs" / "t" / "run"
        run.mkdir(parents=True)
        rows = [{"name": "generation", "start": 2000.0 + i, "duration_s": 1.0,
                 "attributes": {"cost": c}} for i, c in enumerate(gens)]
        (run / "spans.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    md = root / "meter"
    md.mkdir(parents=True, exist_ok=True)
    (md / "meter.jsonl").write_text("".join(json.dumps(r) + "\n" for r in meter_rows))
    return root


def _run(root: Path, port: int, *extra) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--bench-root", str(root), "--port", str(port),
         "--since", "0", *extra],
        capture_output=True, text=True, timeout=300)


def test_it_names_the_preflight_and_leaves_no_residue(tmp_path):
    """One extra metered call per probe, costing almost nothing, is the whole ordinary gap."""
    probes = {"p1": [0.5, 0.5], "p2": [1.0]}
    rows = ([{"ts": "3000", "arm": "p1", "cost": 0.5, "status": "200"} for _ in range(2)]
            + [{"ts": "3000", "arm": "p1", "cost": 0.00000196, "status": "200"}]
            + [{"ts": "3000", "arm": "p2", "cost": 1.0, "status": "200"},
               {"ts": "3000", "arm": "p2", "cost": 0.00000196, "status": "200"}])
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 2.00000392, "calls": 5})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "2 preflight call(s)" in r.stdout, r.stdout + r.stderr
    assert "RESIDUE $+0.000000" in r.stdout, r.stdout
    assert r.returncode == 0, r.stdout + r.stderr


def test_an_unexplained_residue_fails(tmp_path):
    """A leak is the only thing here worth waking someone for, so it is the only thing that exits
    non-zero. Everything else is named and tolerated."""
    probes = {"p1": [1.0]}
    rows = [{"ts": "3000", "arm": "p1", "cost": 1.0, "status": "200"}]
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 1.75, "calls": 1})       # $0.75 nobody can account for
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert r.returncode == 1, r.stdout + r.stderr
    assert "UNEXPLAINED" in r.stdout, r.stdout


def test_it_reads_a_body_that_arrives_after_the_headers(tmp_path):
    """The prescribed `recv(600)` returned headers with no body under load on 2026-09-01 and
    `json.loads` died on the empty string. The stub above splits the response on purpose."""
    root = _bench(tmp_path, probes={"p1": [1.0]},
                  meter_rows=[{"ts": "3000", "arm": "p1", "cost": 1.0, "status": "200"}])
    port, _, srv = _serve({"cost_usd": 1.0, "calls": 1})
    r = _run(root, port)
    srv.close()
    assert "meter   $1.000000" in r.stdout, r.stdout + r.stderr


def test_no_meter_is_an_error_and_not_a_clean_zero(tmp_path):
    """"Nothing to reconcile" and "reconciled" must not share an exit code -- the same rule the
    runs report and the pytest reader already follow."""
    root = _bench(tmp_path, probes={}, meter_rows=[])
    r = subprocess.run(
        [sys.executable, str(TOOL), "--bench-root", str(root), "--port", "9"],
        capture_output=True, text=True, timeout=300)        # no --since, nothing on :9
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "no meter" in r.stderr, r.stderr


def test_an_empty_200_is_named_and_not_left_in_the_unnamed_pile(tmp_path):
    """The four calls this tool could not name, found by asking what they were.

    Status 200, streamed, `attempts: 2`, ZERO prompt and zero completion tokens, latency
    60249-60764 ms: a stream that opened, produced nothing for a minute, and closed successfully --
    on the RETRY, so the first attempt failed the same way. Invisible to every other instrument
    here: not a 504, not unstreamed, costs nothing, no `generation` span. Four in 12,716 calls.
    """
    probes = {"p1": [1.0]}
    rows = [{"ts": "3000", "arm": "p1", "cost": 1.0, "status": "200",
             "prompt_tokens": "100", "completion_tokens": "50"},
            {"ts": "3000", "arm": "p1", "cost": 0.00000196, "status": "200",
             "prompt_tokens": "10", "completion_tokens": "2"},
            {"ts": "3000", "arm": "p1", "cost": 0.0, "status": "200", "stream": "True",
             "attempts": "2", "latency_ms": "60249", "prompt_tokens": "0",
             "completion_tokens": "0"}]
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 1.00000196, "calls": 3})
    r = _run(root, port)
    srv.close()
    assert "1 EMPTY 200s" in r.stdout, r.stdout + r.stderr
    assert "STILL UNNAMED" not in r.stdout, r.stdout


def test_a_call_that_is_neither_killed_nor_empty_is_reported_as_unnamed(tmp_path):
    """The pile must stay visible. A decomposition that silently absorbs what it cannot classify is
    the "answers clean about directories it never looked at" failure in another costume."""
    probes = {"p1": [1.0]}
    rows = [{"ts": "3000", "arm": "p1", "cost": 1.0, "status": "200",
             "prompt_tokens": "100", "completion_tokens": "50"},
            {"ts": "3000", "arm": "p1", "cost": 0.00000196, "status": "200",
             "prompt_tokens": "10", "completion_tokens": "2"},
            {"ts": "3000", "arm": "p1", "cost": 0.0, "status": "200",
             "prompt_tokens": "7", "completion_tokens": "3"}]     # tokens flowed, no span, no kill
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 1.00000196, "calls": 3})
    r = _run(root, port)
    srv.close()
    assert "1 call(s) STILL UNNAMED" in r.stdout, r.stdout + r.stderr


def test_the_spans_are_read_before_the_counter(monkeypatch):
    """The order the sweep prescribes, and the one this file's header already stated.

    MEASURED 2026-09-02 with four probes live: residue $-0.003329 -- the first non-zero one of the
    campaign, about the price of one generation, and NEGATIVE. `_counter` ran before the spans were
    read, so a call that completed between the two reads was in the span sum and not yet in the
    counter snapshot. Re-running seconds later gave $-0.000000: the signature of a race, not a leak.

    Spans first means the counter can only have GAINED between the reads, so the gap is non-negative
    by construction. MUTATION: move `live = _counter(...)` back above `spans_by_probe` and this
    reddens.
    """
    import benchmarks.check_money as cm

    order: list[str] = []
    real_spans = cm.spans_by_probe

    def spans(root, since):
        order.append("spans")
        return real_spans(root, since)

    def counter(port):
        order.append("counter")
        return {"cost_usd": 0.0, "calls": 0}

    monkeypatch.setattr(cm, "spans_by_probe", spans)
    monkeypatch.setattr(cm, "_counter", counter)
    monkeypatch.setattr(cm, "_meter_start", lambda port: 0.0)
    cm.main(["--bench-root", "/nonexistent-bench-root", "--port", "1"])

    assert order == ["spans", "counter"], (
        f"the counter must be read AFTER the span sum, got {order}")


def test_a_missing_meter_log_is_not_a_crash(tmp_path):
    """The graceful path was the one that died.

    `meter_by_probe` returned a THREE-tuple when `meter/meter.jsonl` is absent while `main` unpacks
    four, so a fresh BENCH_ROOT or a mistyped `--bench-root` produced
    `ValueError: not enough values to unpack (expected 4, got 3)` instead of a reconciliation over
    zero rows. Found 2026-09-02 by pointing the tool at a root that does not exist -- which is what
    the order test above happened to do, and what no earlier test had.
    """
    import benchmarks.check_money as cm

    cost, calls, killed, empty = cm.meter_by_probe(str(tmp_path / "absent"), 0.0)
    assert (dict(cost), dict(calls), dict(killed), dict(empty)) == ({}, {}, {}, {})


def test_an_abandoned_probe_is_named_and_not_left_unexplained(tmp_path):
    """A probe stopped and swept away still costs money, and the meter still has it.

    The standing brief carries this as a manual step -- the counter also counts the ABANDONED probe,
    so at reconciliation time you must add it in by hand or read a false discrepancy -- and a step an
    operator has to remember is a step that gets forgotten. Driven 2026-09-02: four probes were
    launched, found mis-designed three minutes in, stopped, and their trees removed; the meter kept
    78 calls and $0.057925 and this tool exited 1 with UNEXPLAINED.

    The signature needs no list to maintain: an arm the METER knows and the probe trees do not.
    `run_probe.sh` writes the tree before the first call, so "meter rows, no tree" cannot be a
    running probe.

    MUTATION: drop `- sum(abandoned.values())` from the residue and this reddens.
    """
    rows = [{"ts": 2000.0, "arm": "alive", "cost": 0.10, "status": 200, "stream": True},
            {"ts": 2001.0, "arm": "alive", "cost": 0.000002, "status": 200, "stream": False},
            {"ts": 2002.0, "arm": "ghost", "cost": 0.04, "status": 200, "stream": True},
            {"ts": 2003.0, "arm": "ghost", "cost": 0.01, "status": 200, "stream": True}]
    root = _bench(tmp_path, probes={"alive": [0.10]}, meter_rows=rows)
    port, _t, srv = _serve({"cost_usd": 0.150002, "calls": 4})
    try:
        done = _run(root, port)
    finally:
        srv.close()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ABANDONED" in done.stdout, done.stdout
    assert "ghost $0.0500" in done.stdout, done.stdout
    assert "UNEXPLAINED" not in done.stdout, done.stdout
    # The claim is that the abandoned dollars leave the residue, not that the residue is exactly
    # zero: the preflight allowance is counted per PROBE and the ghost has one too, which is the
    # $0.000002 that remains. Pinning an exact zero here would be pinning that rounding.
    residue = float([l for l in done.stdout.splitlines() if "RESIDUE" in l][0]
                    .split("$")[1].split()[0])
    assert abs(residue) < 1e-5, done.stdout


def test_a_live_probe_is_never_called_abandoned(tmp_path):
    """MUTATION GUARD: a probe WITH a tree is not abandoned however its calls line up."""
    rows = [{"ts": 2000.0, "arm": "alive", "cost": 0.10, "status": 200, "stream": True},
            {"ts": 2001.0, "arm": "alive", "cost": 0.000002, "status": 200, "stream": False}]
    root = _bench(tmp_path, probes={"alive": [0.10]}, meter_rows=rows)
    port, _t, srv = _serve({"cost_usd": 0.100002, "calls": 2})
    try:
        done = _run(root, port)
    finally:
        srv.close()
    assert "ABANDONED" not in done.stdout, done.stdout


def test_an_abandoned_arm_is_not_also_billed_a_preflight_call(tmp_path):
    """An arm removed WHOLE must not first be decomposed into parts.

    Measured 2026-09-03: two service calls under `svcCacheCheck` (a live cache test, no probe tree)
    left the residue at $-0.000002 and printed "1 call STILL UNNAMED" on an otherwise clean sweep.
    The abandoned arm was being counted twice -- once as a probe owed a preflight call and some
    unexplained extras, and once where its entire cost is subtracted. The money error is one
    preflight estimate; the attention error is a red line on a clean ledger, and §112 is what a red
    line on a clean ledger costs.
    """
    probes = {"p1": [1.0]}
    rows = [{"ts": "3000", "arm": "p1", "cost": 1.0, "status": "200"},
            {"ts": "3000", "arm": "p1", "cost": 0.00000196, "status": "200"},
            # An arm the meter knows and the probe trees do not, with TWO calls, so it would
            # otherwise be read as one preflight plus one unexplained extra.
            {"ts": "3000", "arm": "gone", "cost": 0.004, "status": "200"},
            {"ts": "3000", "arm": "gone", "cost": 0.006, "status": "200"}]
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 1.01000196, "calls": 4})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "1 preflight call(s)" in r.stdout, r.stdout + r.stderr
    assert "STILL UNNAMED" not in r.stdout, r.stdout
    assert "1 ABANDONED probe(s)" in r.stdout and "gone $0.0100" in r.stdout, r.stdout
    assert "RESIDUE $+0.000000" in r.stdout, r.stdout
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_503_refused_in_seconds_is_not_the_nginx_ceiling(tmp_path):
    """Two non-200s that mean opposite things must not share one word.

    On 2026-09-03 six new non-200s appeared inside eight minutes and the tool called all of them
    "killed by the gateway" -- the phrase it uses for the 2026-08-31 catastrophe, when 21 UNSTREAMED
    requests were cut at exactly 300.0 s by nginx's `proxy_read_timeout` and a quarter of one task's
    calls died five minutes at a time. The six were 503s refused in 1.3-15.3 s with streaming ON,
    which the engine retried through without losing a run. The signature separates them: an
    unstreamed 504 at the timeout to the millisecond, against anything else.
    """
    probes = {"p1": [1.0]}
    rows = [{"ts": "3000", "arm": "p1", "cost": 1.0, "status": "200"},
            {"ts": "3000", "arm": "p1", "cost": 0.00000196, "status": "200"},
            # the real thing: unstreamed, cut at the ceiling
            {"ts": "3000", "arm": "p1", "status": "504", "latency_ms": 300000.0, "stream": False},
            # today's: the gateway declining, fast, while streaming
            {"ts": "3000", "arm": "p1", "status": "503", "latency_ms": 5800.0, "stream": True},
            # a 504 that is NOT at the ceiling is not the ceiling either
            {"ts": "3000", "arm": "p1", "status": "504", "latency_ms": 12000.0, "stream": True}]
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 1.00000196, "calls": 5})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "3 non-200" in r.stdout, r.stdout + r.stderr
    assert "1 nginx-300s" in r.stdout, r.stdout
    assert "1 upstream-503" in r.stdout, r.stdout
    assert "1 http-504" in r.stdout, (
        "a 504 that is not at the timeout was folded into the ceiling bucket")
    assert "killed by the gateway" not in r.stdout, (
        "the phrase that made six recoverable refusals read as the catastrophe")
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_failure_kinds_are_decided_by_signature_not_by_status_alone():
    assert cm._failure_kind({"status": "504", "latency_ms": 300000.0, "stream": False}) == "nginx-300s"
    # streaming on: whatever this is, it is not the unstreamed ceiling
    assert cm._failure_kind({"status": "504", "latency_ms": 300000.0, "stream": True}) == "http-504"
    # right status, wrong clock
    assert cm._failure_kind({"status": "504", "latency_ms": 60000.0, "stream": False}) == "http-504"
    assert cm._failure_kind({"status": "503", "latency_ms": 1300.0, "stream": True}) == "upstream-503"
    assert cm._failure_kind({"status": "400", "latency_ms": 100.0, "stream": True}) == "http-400"
    assert cm._failure_kind({}) == "http-?"


def test_the_endpoint_line_dates_every_refusal(tmp_path):
    """"Last call refused" without an age is an alarm that cannot be read.

    Driven the minute the line was added: `oldCK8b` showed as refused for two and a half minutes
    while perfectly healthy — it took a 401 at 16:33:19 and then went into a node evaluation, which
    makes no LLM calls for ~40 s at a time. Meanwhile the other three arms were answering 200. So
    the line has to carry, per arm, the status AND how long ago.
    """
    ledger = tmp_path / "meter" / "meter.jsonl"
    ledger.parent.mkdir(parents=True)
    rows = [{"ts": "1000.0", "arm": "alive", "status": "401", "cost": 0},
            {"ts": "2000.0", "arm": "alive", "status": "200", "cost": 0.5},
            {"ts": "1500.0", "arm": "quiet", "status": "401", "cost": 0}]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    got = cm.endpoint_health(str(ledger))
    assert got["refusing"] == ["quiet"], (
        "an arm whose 401 was followed by a 200 is not refusing; only the NEWEST row counts")
    assert got["newest"]["alive"] == (2000.0, "200")
    assert got["newest"]["quiet"] == (1500.0, "401")


def test_endpoint_health_survives_a_missing_or_torn_ledger(tmp_path):
    assert cm.endpoint_health(str(tmp_path / "nope.jsonl")) == {"newest": {}, "refusing": []}
    torn = tmp_path / "meter.jsonl"
    torn.write_text('{"ts": "1.0", "arm": "a", "status": "200"}\nnot json\n{"ts": ',
                    encoding="utf-8")
    assert cm.endpoint_health(str(torn))["newest"] == {"a": (1.0, "200")}


def test_the_endpoint_line_is_printed_with_the_age(tmp_path):
    probes = {"p1": [1.0]}
    rows = [{"ts": "3000", "arm": "p1", "cost": 1.0, "status": "200"},
            {"ts": "3000", "arm": "p1", "cost": 0.00000196, "status": "200"},
            {"ts": "3001", "arm": "p1", "status": "401", "latency_ms": 6400.0, "stream": True}]
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 1.00000196, "calls": 3})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "endpoint:" in r.stdout, r.stdout + r.stderr
    assert "p1 (401," in r.stdout and "s ago)" in r.stdout, (
        "the refusal is reported without its status or its age")


def test_an_empty_200_says_whether_it_had_queued(tmp_path):
    """The minute in "~60 s" was this proxy's own RPM queue, not a hung stream.

    Measured 2026-09-03 over 26,004 ledger rows: 39 requests waited in `RateLimiter.acquire` (a 60 s
    sliding window at `--rpm 45`) and 23 of them came back an empty 200 — **59 %**, against
    **0.0154 %** of the 25,968 that did not wait. 23 of the 27 empty 200s in the corpus had queued.
    The old wording attributed the minute to the stream; a reader chasing a hung upstream would have
    been chasing the wrong process.
    """
    probes = {"p1": [1.0]}
    # Real rows carry token counts; a fixture that omits them reads as empty and inflates the tally.
    rows = [{"ts": "3000", "arm": "p1", "cost": 1.0, "status": "200",
             "prompt_tokens": 1000, "completion_tokens": 10},
            {"ts": "3000", "arm": "p1", "cost": 0.00000196, "status": "200",
             "prompt_tokens": 10, "completion_tokens": 2},
            # queued a minute, came back empty
            {"ts": "3000", "arm": "p1", "status": "200", "prompt_tokens": 0,
             "completion_tokens": 0, "queued_s": 60.0, "latency_ms": 60200.0},
            # empty but never queued -- a different animal, and it must not be counted as one
            {"ts": "3000", "arm": "p1", "status": "200", "prompt_tokens": 0,
             "completion_tokens": 0, "queued_s": 0.0, "latency_ms": 4900.0}]
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 1.00000196, "calls": 4})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "2 EMPTY 200s" in r.stdout, r.stdout + r.stderr
    assert "1 of them after a >0.5 s wait in THIS proxy's RPM queue" in r.stdout, r.stdout
    assert "~60 s" not in r.stdout, (
        "the wording still blames the stream for a minute the proxy spent queueing")
    assert r.returncode == 0, r.stdout + r.stderr


def test_an_unqueued_empty_200_says_so(tmp_path):
    probes = {"p1": [1.0]}
    rows = [{"ts": "3000", "arm": "p1", "cost": 1.0, "status": "200",
             "prompt_tokens": 1000, "completion_tokens": 10},
            {"ts": "3000", "arm": "p1", "cost": 0.00000196, "status": "200",
             "prompt_tokens": 10, "completion_tokens": 2},
            {"ts": "3000", "arm": "p1", "status": "200", "prompt_tokens": 0,
             "completion_tokens": 0, "queued_s": 0.0}]
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 1.00000196, "calls": 3})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "1 EMPTY 200s" in r.stdout and "none of them queued" in r.stdout, r.stdout


def test_a_call_in_flight_is_not_a_leak(tmp_path):
    """The residue that fired on 2026-09-03 was three calls whose spans were not written yet.

    `RESIDUE $+0.019402` with `3 call(s) STILL UNNAMED` beside it, and `$+0.000000` on each of the
    next three runs seconds later. The meter writes its row when the upstream request completes and
    the engine writes the `generation` span afterwards; this tool reads spans first and the counter
    second, so anything landing in between is in one and not the other. The tolerance is now the
    unnamed count times the ledger's own p99 call price.
    """
    # A call in flight is RECENT by definition -- an ancient ledger is idle and gets no
    # allowance (see test_an_idle_ledger_gets_no_in_flight_allowance).
    probes = {"p1": [0.10]}                                  # one generation recorded
    rows = ([{"ts": _now(), "arm": "p1", "cost": 0.10, "status": "200",
              "prompt_tokens": 900, "completion_tokens": 9}]
            + [{"ts": _now(), "arm": "p1", "cost": 0.00000196, "status": "200",
                "prompt_tokens": 10, "completion_tokens": 2}]
            # three more metered calls whose spans have not been written
            + [{"ts": _now(), "arm": "p1", "cost": 0.10, "status": "200",
                "prompt_tokens": 900, "completion_tokens": 9} for _ in range(3)])
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 0.40000196, "calls": 5})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "3 call(s) STILL UNNAMED" in r.stdout, r.stdout + r.stderr
    assert "unnamed call(s) on arms that are still calling, at the p99 price" in r.stdout, r.stdout
    assert "UNEXPLAINED" not in r.stdout, r.stdout
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_leak_with_nothing_in_flight_still_fires(tmp_path):
    """The allowance is per unnamed call, so with none the old one-cent rule stands."""
    probes = {"p1": [1.0]}
    rows = [{"ts": "3000", "arm": "p1", "cost": 1.0, "status": "200",
             "prompt_tokens": 900, "completion_tokens": 9}]
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 1.75, "calls": 1})     # $0.75 nobody can account for
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "UNEXPLAINED" in r.stdout, r.stdout
    assert r.returncode == 1, r.stdout + r.stderr


def test_a_residue_far_past_the_allowance_still_fires(tmp_path):
    probes = {"p1": [0.10]}
    rows = ([{"ts": "3000", "arm": "p1", "cost": 0.10, "status": "200",
              "prompt_tokens": 900, "completion_tokens": 9}]
            + [{"ts": "3000", "arm": "p1", "cost": 0.00000196, "status": "200",
                "prompt_tokens": 10, "completion_tokens": 2}]
            + [{"ts": "3000", "arm": "p1", "cost": 0.10, "status": "200",
                "prompt_tokens": 900, "completion_tokens": 9} for _ in range(3)])
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 5.00000196, "calls": 5})   # far past 3 calls' worth
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "UNEXPLAINED" in r.stdout, r.stdout
    assert r.returncode == 1, r.stdout + r.stderr


def test_the_allowance_uses_the_p99_not_the_median(tmp_path):
    """A call caught in flight is not a typical call, and a skewed ledger is where that shows.

    The first version of the test above used uniform prices, where median and p99 are the same
    number, so mutating the percentile survived it — the test could not tell which statistic the
    code used. Here 100 calls cost $0.001 and three cost $0.10: the median is a tenth of a cent and
    the p99 is ten cents, and the three unnamed calls are the expensive kind, which is exactly the
    case that produced $0.019402 over three calls when the corpus median was $0.00282.
    """
    # A call in flight is RECENT by definition -- an ancient ledger is idle and gets no
    # allowance (see test_an_idle_ledger_gets_no_in_flight_allowance).
    probes = {"p1": [0.001] * 100}
    rows = ([{"ts": _now(), "arm": "p1", "cost": 0.001, "status": "200",
              "prompt_tokens": 90, "completion_tokens": 2} for _ in range(100)]
            + [{"ts": _now(), "arm": "p1", "cost": 0.00000196, "status": "200",
                "prompt_tokens": 10, "completion_tokens": 2}]
            + [{"ts": _now(), "arm": "p1", "cost": 0.10, "status": "200",
                "prompt_tokens": 9000, "completion_tokens": 90} for _ in range(3)])
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 0.40000196, "calls": 104})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "3 call(s) STILL UNNAMED" in r.stdout, r.stdout + r.stderr
    assert "UNEXPLAINED" not in r.stdout, (
        "a median-priced allowance is $0.003 against a $0.30 residue and would fire here")
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_unstreamed_share_and_its_ceiling_deaths_are_reported(tmp_path):
    """`LOOPLAB_LLM_STREAM=1` is a setting; the traffic is the measurement.

    `core/llm.py:1629` degrades a call to non-streaming after a mid-stream stall, and the unstreamed
    retry is exactly what nginx's `proxy_read_timeout` measures end to end. On 2026-09-03 `oldCK9`
    ran with streaming on, sent 42 unstreamed calls anyway, and two of them were cut at 300.0 s.
    """
    probes = {"p1": [1.0]}
    rows = [{"ts": "3000", "arm": "p1", "cost": 1.0, "status": "200", "stream": True,
             "prompt_tokens": 900, "completion_tokens": 9},
            {"ts": "3000", "arm": "p1", "cost": 0.00000196, "status": "200", "stream": True,
             "prompt_tokens": 10, "completion_tokens": 2},
            # unstreamed and cut at the ceiling
            {"ts": "3000", "arm": "p1", "status": "504", "stream": False, "latency_ms": 300000.0},
            # unstreamed and perfectly fine
            {"ts": "3000", "arm": "p1", "status": "200", "stream": False, "latency_ms": 1900.0,
             "cost": 0.002, "prompt_tokens": 500, "completion_tokens": 20}]
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 1.00200196, "calls": 4})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "2 of 4 calls went out UNSTREAMED (50.0 %)" in r.stdout, r.stdout + r.stderr
    assert "1 of them cut at the 300 s nginx ceiling (p1 x1)" in r.stdout, (
        "the ceiling deaths are counted but not attributed; four in one run and one each in four "
        "runs are different facts")
    assert "unstreamed by arm: p1 2/" in r.stdout, r.stdout


def test_an_all_streamed_ledger_says_so(tmp_path):
    probes = {"p1": [1.0]}
    rows = [{"ts": "3000", "arm": "p1", "cost": 1.0, "status": "200", "stream": True,
             "prompt_tokens": 900, "completion_tokens": 9},
            {"ts": "3000", "arm": "p1", "cost": 0.00000196, "status": "200", "stream": True,
             "prompt_tokens": 10, "completion_tokens": 2}]
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 1.00000196, "calls": 2})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "0 of 2 calls went out UNSTREAMED (0.0 %); none cut at the 300 s ceiling" in r.stdout, r.stdout


def test_an_idle_ledger_gets_no_in_flight_allowance(tmp_path):
    """"Calls in flight" explains a residue only while something IS in flight.

    On 2026-09-03 at 20:26 every probe had finished, the ledger's newest row was 1002 s old, and
    §172's allowance was still forgiving $0.076944 — all of it `oldCK9`'s 18 surplus calls from the
    unstreamed retry storm. An idle ledger cannot have calls in flight, so the allowance expires
    with the ledger's own last row, and the red names the arm it belongs to.
    """
    probes = {"p1": [0.10]}
    old = "1000000.0"                                    # an ancient timestamp: nothing is in flight
    rows = ([{"ts": old, "arm": "p1", "cost": 0.10, "status": "200",
              "prompt_tokens": 900, "completion_tokens": 9}]
            + [{"ts": old, "arm": "p1", "cost": 0.00000196, "status": "200",
                "prompt_tokens": 10, "completion_tokens": 2}]
            + [{"ts": old, "arm": "p1", "cost": 0.10, "status": "200",
                "prompt_tokens": 900, "completion_tokens": 9} for _ in range(3)])
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 0.40000196, "calls": 5})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "no allowance: of 3 unnamed call(s), 0 are on arms still calling" in r.stdout, r.stdout + r.stderr
    assert "UNEXPLAINED" in r.stdout, r.stdout
    assert "by arm (meter minus spans): p1 $+0.30" in r.stdout, (
        "the red names no arm, so nobody can act on it")
    assert r.returncode == 1, r.stdout + r.stderr


def test_a_finished_arm_gets_no_allowance_while_others_are_running(tmp_path):
    """§175 expired the allowance when the LEDGER went quiet. That was the wrong unit.

    One sweep later batch 6 started, the ledger was fresh again, and `oldCK9`'s $0.076943 -- a probe
    that had finished ninety minutes earlier -- was forgiven a second time. A call cannot be in
    flight for an arm that has stopped calling, whatever the other arms are doing.
    """
    probes = {"live": [0.10], "done": [0.10]}
    old = "1000000.0"
    rows = ([{"ts": _now(), "arm": "live", "cost": 0.10, "status": "200",
              "prompt_tokens": 900, "completion_tokens": 9},
             {"ts": _now(), "arm": "live", "cost": 0.00000196, "status": "200",
              "prompt_tokens": 10, "completion_tokens": 2},
             {"ts": _now(), "arm": "live", "cost": 0.10, "status": "200",
              "prompt_tokens": 900, "completion_tokens": 9}]          # in flight: forgiven
            + [{"ts": old, "arm": "done", "cost": 0.10, "status": "200",
                "prompt_tokens": 900, "completion_tokens": 9},
               {"ts": old, "arm": "done", "cost": 0.00000196, "status": "200",
                "prompt_tokens": 10, "completion_tokens": 2},
               {"ts": old, "arm": "done", "cost": 0.10, "status": "200",
                "prompt_tokens": 900, "completion_tokens": 9}])        # finished: NOT forgiven
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 0.40000392, "calls": 6})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    # Two arms hold one unnamed call each; only the live one is forgiven.
    assert "1 unnamed call(s) on arms that are still calling" in r.stdout, r.stdout + r.stderr
    assert "UNEXPLAINED" in r.stdout, (
        "the finished arm's surplus was forgiven because another arm is live")
    assert "done $+0.10" in r.stdout, r.stdout
    assert r.returncode == 1, r.stdout + r.stderr


def test_a_paid_retry_is_a_named_part_of_the_gap(tmp_path):
    """`oldCK9` ended with $0.076945 of metered money that never became a span, and §175 measured
    what it was: one body sent eight times unstreamed, the engine keeping one answer and discarding
    the rest. Grouping by `req_sha` gives $0.101394 on 20 repeats, which covers the gap.

    A gap with a name must stop reddening every sweep — that is how §158's standing red taught
    everyone to ignore the colour — but the subtraction is capped at the arm's actual gap, so it can
    never invent credit.
    """
    probes = {"p1": [0.10]}
    old = "1000000.0"
    rows = [{"ts": old, "arm": "p1", "cost": 0.10, "status": "200", "req_sha": "aaaa",
             "prompt_tokens": 900, "completion_tokens": 9},
            {"ts": old, "arm": "p1", "cost": 0.00000196, "status": "200", "req_sha": "pref",
             "prompt_tokens": 10, "completion_tokens": 2},
            # the same body twice more: paid, never kept
            {"ts": old, "arm": "p1", "cost": 0.10, "status": "200", "req_sha": "aaaa",
             "prompt_tokens": 900, "completion_tokens": 9},
            {"ts": old, "arm": "p1", "cost": 0.10, "status": "200", "req_sha": "aaaa",
             "prompt_tokens": 900, "completion_tokens": 9}]
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 0.30000196, "calls": 4})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "PAID RETRIES" in r.stdout, r.stdout + r.stderr
    assert "p1 $0.200000 of $0.200000 on 2 repeat(s)" in r.stdout, r.stdout
    assert "RESIDUE $-0.000002" in r.stdout or "RESIDUE $+0.000000" in r.stdout, r.stdout
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_retry_credit_cannot_exceed_the_arm_s_own_gap(tmp_path):
    """Capped at the gap, so an arm whose spans DID record its retries earns nothing here."""
    probes = {"p1": [0.10, 0.10, 0.10]}          # all three generations recorded
    old = "1000000.0"
    rows = [{"ts": old, "arm": "p1", "cost": 0.10, "status": "200", "req_sha": "aaaa",
             "prompt_tokens": 900, "completion_tokens": 9} for _ in range(3)]
    rows.append({"ts": old, "arm": "p1", "cost": 0.00000196, "status": "200", "req_sha": "pref",
                 "prompt_tokens": 10, "completion_tokens": 2})
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 0.30000196, "calls": 4})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "PAID RETRIES" not in r.stdout, (
        "credit was given for retries the span stream already accounts for")
    assert r.returncode == 0, r.stdout + r.stderr


def test_an_abandoned_arm_is_not_credited_twice(tmp_path):
    """The abandoned arm is subtracted WHOLE; naming its retries too removes the same dollars twice.

    The first live run of the paid-retry block did exactly that: `svcCacheCheck` was subtracted as an
    abandoned arm AND credited $0.000562 as a retry, leaving the residue at **-$0.000564** — the same
    shape as the echo subtraction reverted before §124, where money was taken off one side of a
    balance that carried it on both. A mutation removing the guard survived every other test here.
    """
    probes = {"p1": [1.0]}
    old = "1000000.0"
    rows = [{"ts": old, "arm": "p1", "cost": 1.0, "status": "200", "req_sha": "p1a",
             "prompt_tokens": 900, "completion_tokens": 9},
            {"ts": old, "arm": "p1", "cost": 0.00000196, "status": "200", "req_sha": "p1pref",
             "prompt_tokens": 10, "completion_tokens": 2},
            # an arm the meter knows and the probe trees do not, whose body repeats
            {"ts": old, "arm": "gone", "cost": 0.05, "status": "200", "req_sha": "gg",
             "prompt_tokens": 400, "completion_tokens": 4},
            {"ts": old, "arm": "gone", "cost": 0.05, "status": "200", "req_sha": "gg",
             "prompt_tokens": 400, "completion_tokens": 4}]
    root = _bench(tmp_path, probes=probes, meter_rows=rows)
    port, _, srv = _serve({"cost_usd": 1.10000196, "calls": 4})
    r = _run(root, port, "--max-residue", "0.01")
    srv.close()
    assert "1 ABANDONED probe(s)" in r.stdout and "gone $0.1000" in r.stdout, r.stdout + r.stderr
    assert "gone $" not in r.stdout.split("PAID RETRIES")[-1] if "PAID RETRIES" in r.stdout else True, (
        "the abandoned arm was credited a second time as a paid retry")
    assert "RESIDUE $+0.000000" in r.stdout or "RESIDUE $-0.000000" in r.stdout, r.stdout
    assert r.returncode == 0, r.stdout + r.stderr
