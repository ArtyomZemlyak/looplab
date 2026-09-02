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

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "benchmarks" / "check_money.py"


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
