"""§409. A running campaign arm was reported as money somebody had deleted.

`check_money.py`'s abandoned-probe rule is "an arm the METER knows and the probe trees do not", and
the comment beside it states the premise out loud: a live probe always has a tree, so "meter rows,
no tree" cannot be a running probe. That premise held while every metered arm WAS a LoopLab probe.
Arm A is AlgoTuner, which never writes a probe tree, by construction. Measured six minutes into the
2026-09-10 campaign:

    351 call(s) from 9 ABANDONED probe(s) -- no tree under model-probes: A $0.0466, capA1 $0.2791 ...
    $0.5078 of that is NOT unexplained ... only the rest has no evidence anywhere

-- the reference arm of the campaign that was running at that moment, filed as deleted money, in the
sentence whose next line says the remainder "has no evidence anywhere". Over a full campaign that is
up to $20 in the loudest category the tool has.

THIS FILE IS A REWRITE. The original was untracked when the box restarted on 2026-09-10 and
`snapshot.sh` captured only `git diff`, which does not carry untracked files; the instrument landed
and its driver did not. It is rebuilt from the mutations §409 names, and each test below says which.

The end-to-end form is not decoration either. §409's own helper tests checked `campaign_evidence`
and the sentence but never the CALL, and so passed against a `main` that computed the split and
never printed or subtracted it -- §399 in the sixth costume. So the first three tests drive the real
script in a subprocess against a fake ledger and a stub counter on a socket.
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

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))

import check_money  # noqa: E402

SCRIPT = BENCHMARKS / "check_money.py"

# The arm, its money and its two neighbours, kept close to what was measured: arm A of the campaign
# at $0.0466, and one probe that really was abandoned so the ABANDONED sentence still has an owner.
ARM = "A"
ARM_COST = 0.0466
GONE = "remGone"
GONE_COST = 0.0100


def _ledger(bench: Path, rows) -> None:
    """A meter ledger with the fields every reader in `check_money` actually touches."""
    d = bench / "meter"
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with (d / "meter.jsonl").open("w", encoding="utf-8") as fh:
        for i, (arm, cost) in enumerate(rows):
            fh.write(json.dumps({
                "ts": now - 60 + i, "arm": arm, "task": "convex_hull", "attempt": "a1",
                "model": "deepseek-v4-flash", "status": "200", "latency_ms": 1200,
                "stream": True, "attempts": 1, "queued_s": 0.0,
                "prompt_tokens": 900, "completion_tokens": 120, "cost": cost,
            }) + "\n")


def _campaign(out: Path, arm: str = ARM, tasks=("convex_hull",)) -> Path:
    """What `campaign.sh` writes BEFORE an arm's first call -- the evidence §409 reads."""
    out.mkdir(parents=True, exist_ok=True)
    for t in tasks:
        (out / f"{arm}-{t}.log").write_text("Running task: " + t + "\n", encoding="utf-8")
        (out / f"{arm}-{t}.attempts").write_text("a1 started=2026-09-10T09:47:05Z\n",
                                                 encoding="utf-8")
    return out


class _Counter(BaseHTTPRequestHandler):
    """The meter's /healthz, read to EOF over a raw socket by `check_money._counter`."""

    payload: dict = {}

    def do_GET(self):                                      # noqa: N802 - http.server's spelling
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                             # keep pytest's output about the test
        pass


@pytest.fixture()
def stand(tmp_path):
    """A bench root, a campaign directory and a counter on an ephemeral port.

    The counter's total is the ledger's total, which is the only configuration in which a residue
    means what this tool says it means: every dollar the counter knows is a dollar the ledger
    explains, so anything left over came from the accounting under test.
    """
    bench = tmp_path / "bench"
    _ledger(bench, [(ARM, ARM_COST / 3)] * 3 + [(GONE, GONE_COST / 2)] * 2)
    out = _campaign(tmp_path / "campaign-final")

    handler = type("H", (_Counter,), {"payload": {"cost_usd": ARM_COST + GONE_COST, "calls": 5}})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield bench, out, srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def _run(bench: Path, port: int, *, campaign_out=None, env_out=None):
    """The real script, in a subprocess, as an operator runs it."""
    env = dict(os.environ)
    env.pop("CAMPAIGN_OUT", None)
    if env_out is not None:
        env["CAMPAIGN_OUT"] = str(env_out)
    argv = [sys.executable, str(SCRIPT), "--bench-root", str(bench), "--port", str(port),
            # A stub server is not a `meter/proxy.py`, so there is no process to read a start time
            # from; the flag exists for exactly this and for an operator whose meter has restarted.
            "--since", "0"]
    if campaign_out is not None:
        argv += ["--campaign-out", str(campaign_out)]
    p = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120)
    return p


def _residue(stdout: str) -> float:
    """The number on the RESIDUE line.

    §409 closes on a measurement error of my own: the first end-to-end test demanded
    `RESIDUE $+0.000000` and the tool carries one preflight-call estimate per arm (-$0.000002), a
    precision it has never claimed. So the assertion reads the NUMBER and asks that the arm's money
    is not in it -- which is the actual question -- rather than pinning six decimal places.
    """
    for line in stdout.splitlines():
        if "RESIDUE" in line:
            return float(line.split("$", 1)[1].split()[0])
    raise AssertionError(f"no RESIDUE line in:\n{stdout}")


def test_the_arm_is_named_a_campaign_arm_and_not_an_abandoned_probe(stand):
    """MUTATION: campaign arms are not separated -- the arm appears in ABANDONED."""
    bench, out, port = stand
    p = _run(bench, port, campaign_out=out)
    assert p.returncode == 0, p.stdout + p.stderr
    assert f"{ARM} ${ARM_COST:.4f} -- a CAMPAIGN arm, not an abandoned probe" in p.stdout, p.stdout
    assert "1 task log(s) and 1 attempt ledger(s)" in p.stdout, p.stdout

    abandoned = [ln for ln in p.stdout.splitlines() if "ABANDONED probe(s)" in ln]
    assert len(abandoned) == 1, p.stdout
    # The sentence still has an owner, and the campaign arm is not in it.
    assert GONE in abandoned[0], abandoned[0]
    assert f"{ARM} $" not in abandoned[0], abandoned[0]


def test_the_arms_money_is_subtracted_and_does_not_become_unexplained(stand):
    """MUTATION: the arm is taken out of `abandoned` and its cost is not subtracted.

    This is the second error of §409, and it is mine: the first fix moved arm A out of the
    abandoned dict while the residue went on subtracting that dict whole, and the RESIDUE jumped
    from -$0.000006 to +$0.045716. "Reported as deleted" had become "reported as unexplained" --
    the same error in a different coat. Both categories are explained, both are subtracted.
    """
    bench, out, port = stand
    residue = _residue(_run(bench, port, campaign_out=out).stdout)
    # A tenth of the arm's money: far below what the mutation produces (the whole $0.0466) and far
    # above the one preflight estimate the tool legitimately carries.
    assert abs(residue) < ARM_COST / 10, residue


def test_without_a_campaign_directory_the_tool_says_it_cannot_tell(stand):
    """MUTATION: the note about a missing `CAMPAIGN_OUT` disappears.

    A tool that cannot distinguish two things is obliged to say that it cannot, rather than
    silently choosing the second -- which is how a running arm was reported as lost money.
    """
    bench, out, port = stand
    p = _run(bench, port)                      # no flag, no environment
    assert "no CAMPAIGN_OUT given" in p.stdout, p.stdout
    assert "would appear above as ABANDONED" in p.stdout, p.stdout
    abandoned = [ln for ln in p.stdout.splitlines() if "ABANDONED probe(s)" in ln]
    assert abandoned and f"{ARM} $" in abandoned[0], p.stdout
    # And with the directory given, the note is NOT printed: a caveat that fires when the question
    # WAS asked is noise, and noise is what the reader stops reading.
    assert "no CAMPAIGN_OUT given" not in _run(bench, port, campaign_out=out).stdout


def test_the_environment_alone_is_enough_and_the_flag_wins_over_it(stand):
    """MUTATION: the flag is ignored in favour of the environment variable.

    `CAMPAIGN_OUT` is exported by the box profile, so a campaign is read correctly with no extra
    flag -- that is the default. But an operator who passes the flag has said something more
    specific than the environment did, and `os.environ.get(...) or a.campaign_out` (the mutation)
    would silently keep pointing at the wrong campaign.
    """
    bench, out, port = stand
    assert "a CAMPAIGN arm" in _run(bench, port, env_out=out).stdout

    elsewhere = out.parent / "another-campaign"
    elsewhere.mkdir()
    p = _run(bench, port, campaign_out=out, env_out=elsewhere)
    assert f"{ARM} ${ARM_COST:.4f} -- a CAMPAIGN arm" in p.stdout, p.stdout
    assert str(out) in p.stdout and str(elsewhere) not in p.stdout, p.stdout


def test_campaign_evidence_counts_logs_and_ledgers_apart(tmp_path):
    """Two counts, not one: a directory with logs and no `.attempts` is a campaign that never
    recorded an attempt, and the sentence should be able to say so."""
    out = _campaign(tmp_path / "c", tasks=("convex_hull", "pagerank"))
    (out / f"{ARM}-kcenters.log").write_text("", encoding="utf-8")     # a third log, no ledger
    assert check_money.campaign_evidence(ARM, str(out)) == (3, 2)


def test_an_arm_name_is_a_name_and_not_a_prefix(tmp_path):
    """MUTATION: `A-` is matched as `A*`, so arm `A` inherits arm `AB`'s evidence.

    Arm names in this bench are short (`A`, `B`, `capA1`, `freeB12`), so a prefix match is not a
    theoretical hazard: it would hand every arm whose name starts with `A` the proof that arm A is
    alive, and the money of a genuinely abandoned probe would stop being reported.
    """
    out = tmp_path / "c"
    _campaign(out, arm="AB")
    assert check_money.campaign_evidence("A", str(out)) == (0, 0)
    assert check_money.campaign_evidence("AB", str(out)) == (1, 1)


def test_no_directory_is_no_evidence_rather_than_a_crash(tmp_path):
    """The default is the empty string -- the common case, not an error."""
    assert check_money.campaign_evidence(ARM, "") == (0, 0)
    assert check_money.campaign_evidence(ARM, str(tmp_path / "never-existed")) == (0, 0)


def test_the_sentence_carries_both_counts_and_the_directory():
    """§342: the wording IS the fix. A reader who sees this line has to be able to go and look."""
    said = check_money.campaign_arm_line(ARM, ARM_COST, 20, 20, "/bench/campaign-final")
    assert "a CAMPAIGN arm, not an abandoned probe" in said
    assert "20 task log(s)" in said and "20 attempt ledger(s)" in said
    assert "/bench/campaign-final" in said
    assert f"${ARM_COST:.4f}" in said
