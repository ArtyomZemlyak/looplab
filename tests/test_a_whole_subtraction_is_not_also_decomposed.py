"""An arm whose money is subtracted WHOLE must not also be taken apart call by call.

`check_money.py` carries this rule already, in the comment above the `abandoned` dict: an abandoned
arm is subtracted whole, so it must not also be decomposed, because the same dollars would be
removed twice. §112 priced the two halves of getting it wrong -- "the dollar error is one preflight
estimate per abandoned arm and the attention error is a red line on a clean ledger, which is the
more expensive of the two".

§409 then created a SECOND whole-subtraction category, the campaign arm, and took it out of
`abandoned` only. `probes` kept it, and for this category the consequence is not one preflight
estimate. An AlgoTuner arm writes no generation spans at all, by construction, so every call it
makes becomes a surplus call, then an unnamed call, then a term in the in-flight ALLOWANCE, which is
priced at the ledger's p99 per call. Driven on a 42-call stand whose arm A made 40 of them:

    39 call(s) STILL UNNAMED -- neither killed nor empty
    (allowing $0.195000: 39 unnamed call(s) on arms that are still calling ...)

$0.195 of allowance on a stand that had spent $0.09 in total. `abs(residue) > allowance` is the only
thing between a real leak and a green exit, and arm A of a real campaign makes thousands of calls --
so the allowance would sit in the tens of dollars for as long as the reference arm was up. That is
the expensive direction: not an alarm that cries wolf, an alarm that has been turned off, during
exactly the hours this tool exists to watch.

The last test below is the one that matters: a leak of ten cents, beside a campaign arm, is caught.
Against the unfixed line it is FORGIVEN -- the tool exits 0 and prints nothing about it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
SCRIPT = BENCHMARKS / "check_money.py"

ARM = "A"                       # the campaign's reference arm: metered, and no tree, ever
PROBE = "capA1"                 # a LoopLab probe: metered, with a tree and spans
CALL = 0.002


def _stand(tmp: Path, *, arm_calls=100, probe_calls=2, probe_spans=2, leak=0.0):
    """A bench root, a campaign directory and a counter, returned with the port to read it on.

    `leak` is money the COUNTER knows and nothing on disk explains -- the thing this tool is for.
    """
    bench = tmp / "bench"
    (bench / "meter").mkdir(parents=True)
    now = time.time()
    rows = [(ARM, CALL)] * arm_calls + [(PROBE, CALL)] * probe_calls
    with (bench / "meter" / "meter.jsonl").open("w", encoding="utf-8") as fh:
        for i, (arm, cost) in enumerate(rows):
            fh.write(json.dumps({
                "ts": now - 120 + i, "arm": arm, "status": "200", "latency_ms": 900,
                "stream": True, "attempts": 1, "queued_s": 0.0,
                "prompt_tokens": 800, "completion_tokens": 100, "cost": cost,
            }) + "\n")

    # The probe's tree, which is what makes it a probe and not an abandoned arm.
    run = bench / "model-probes" / PROBE / "runs" / "r1" / "run"
    run.mkdir(parents=True)
    with (run / "spans.jsonl").open("w", encoding="utf-8") as fh:
        for i in range(probe_spans):
            fh.write(json.dumps({"name": "generation", "start": now - 100 + i,
                                 "attributes": {"cost": CALL}}) + "\n")

    out = tmp / "campaign-final"
    out.mkdir()
    for t in ("convex_hull", "pagerank"):
        (out / f"{ARM}-{t}.log").write_text("Running task: " + t + "\n", encoding="utf-8")
        (out / f"{ARM}-{t}.attempts").write_text("a1 started=...\n", encoding="utf-8")

    total = sum(c for _a, c in rows) + leak

    class H(BaseHTTPRequestHandler):
        def do_GET(self):                                   # noqa: N802 - http.server's spelling
            body = json.dumps({"cost_usd": total, "calls": len(rows)}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return bench, out, srv


def _run(bench, out, srv):
    env = dict(os.environ)
    env.pop("CAMPAIGN_OUT", None)
    p = subprocess.run([sys.executable, str(SCRIPT), "--bench-root", str(bench),
                        "--port", str(srv.server_address[1]), "--since", "0",
                        "--campaign-out", str(out)],
                       capture_output=True, text=True, env=env, timeout=120)
    return p


def test_a_campaign_arms_calls_are_not_reported_as_unnamed(tmp_path):
    """Its money is named in one line; naming the same money again, call by call, is a second
    accounting of it -- and the line it produces reads as an unexplained hundred calls."""
    bench, out, srv = _stand(tmp_path)
    try:
        p = _run(bench, out, srv)
    finally:
        srv.shutdown()
        srv.server_close()
    assert "STILL UNNAMED" not in p.stdout, p.stdout
    assert f"{ARM}+" not in p.stdout, p.stdout          # the "further call(s)" list, per arm
    assert f"{ARM} $" in p.stdout and "a CAMPAIGN arm" in p.stdout, p.stdout


def test_the_allowance_is_not_inflated_by_an_arm_that_never_writes_spans(tmp_path):
    """The allowance exists for spans the engine has not written YET. An AlgoTuner arm will never
    write one, so every call of it is permanent licence -- which is the opposite of the intent."""
    bench, out, srv = _stand(tmp_path)
    try:
        p = _run(bench, out, srv)
    finally:
        srv.shutdown()
        srv.server_close()
    assert "allowing $" not in p.stdout, p.stdout
    assert p.returncode == 0, p.stdout


def test_a_real_probe_is_still_decomposed(tmp_path):
    """The other direction, so the fix is not "stop looking at arms".

    A LoopLab probe with a tree and one metered call more than it has spans is exactly what the
    surplus/preflight decomposition is FOR, and it still happens.
    """
    bench, out, srv = _stand(tmp_path, probe_calls=3, probe_spans=1)
    try:
        p = _run(bench, out, srv)
    finally:
        srv.shutdown()
        srv.server_close()
    assert "1 preflight call(s)" in p.stdout, p.stdout
    assert f"{PROBE}+1" in p.stdout, p.stdout


def test_a_leak_beside_a_campaign_arm_is_still_caught(tmp_path):
    """THE TEST THIS FILE EXISTS FOR.

    Ten cents the counter knows and nothing explains, while the campaign's reference arm is making
    its hundred calls. Against the unfixed line those hundred calls buy an allowance of
    99 x p99 = $0.495, the residue of $0.10 fits inside it, and the tool exits 0 having printed no
    complaint at all. The leak is not smaller than the alarm; the alarm was bought off by the arm
    standing next to it.
    """
    leak = 0.10
    bench, out, srv = _stand(tmp_path, leak=leak)
    try:
        p = _run(bench, out, srv)
    finally:
        srv.shutdown()
        srv.server_close()
    assert p.returncode == 1, p.stdout
    assert "UNEXPLAINED" in p.stdout, p.stdout
    assert f"${leak:+.6f}" in p.stdout, p.stdout
