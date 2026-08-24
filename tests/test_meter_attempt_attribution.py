"""One task-arm attempted twice is two measurements, and the meter could not tell them apart.

THE DEFECT. `campaign.sh` gave every task-arm the meter path `/m/$ARM/$TASK/v1`, so
`meter/proxy.py` attributed each call by `(arm, task)` and by nothing else. A task-arm that is
RE-RUN adds to the same bucket, and no reader can separate the attempts afterwards.

Measured 2026-08-23 on `/var/tmp/looplab-bench/meter/meter.jsonl` (9,456 rows, one live campaign):

    B/kcenters                    $2.0086 over 816 calls, against ONE `.done` marker whose run
                                  really cost $1.0070  -- so a naive per-task cost reads 2x the
                                  $1.00 ceiling and looks like a budget breach that never happened.
                                  A colleague read it as one.
    B/discrete_log                $1.4749 over 526 calls
    B/count_riemann_zeta_zeros    $0.8386 over 127 calls

THE ONLY HANDLE LEFT IN SUCH A LOG IS A GAP HEURISTIC OVER `ts`, and it does not have an answer:
`B/count_riemann_zeta_zeros` splits into 19 / 16 / 14 / 12 / 2 sessions at a 5 / 10 / 15 / 20 /
40-minute gap, and nothing in the log says which is right. That is the "without date arithmetic"
requirement: a reader must be able to sum ONE attempt by equality on a field.

THE FIX IS A THIRD PATH SEGMENT, and the id in it is minted by `campaign.sh::next_attempt` -- the
same place that writes the `.done` marker, so `attempt=aN` in the marker and `aN` in the URL name
the same thing. A proxy that invented an id instead (a start-up counter, a first-seen-at stamp)
would renumber on every meter restart and could be joined to nothing.

Everything here is driven against the REAL proxy over a REAL socket and, for the URL, against the
REAL driver: the campaign is run end to end with a stubbed AlgoTune so the assertion is about the
`OPENAI_BASE_URL` a task actually got, not about a string in the source. Nothing touches the live
campaign: private ports, a private log file, `--rpm 0`, and lanes pinned inside this process's own
CPU affinity.
"""
from __future__ import annotations

import collections
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "benchmarks" / "meter" / "proxy.py"
CAMPAIGN = ROOT / "benchmarks" / "algotune" / "campaign.sh"


def _by_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PX = _by_path(PROXY, "meter_proxy_attempt_under_test")


# ------------------------------------------------------------------------------------------------
# the parser, both shapes
# ------------------------------------------------------------------------------------------------
class _Parse(PX.Handler):
    """`_split_path` with no socket under it: the method reads `self.path` and nothing else."""

    def __init__(self, path: str):
        self.path = path


@pytest.mark.parametrize("path,expected", [
    # the new form
    ("/m/B/kcenters/a4/v1/chat/completions", ("B", "kcenters", "a4", "/v1/chat/completions")),
    ("/m/A/svm/a1/v1", ("A", "svm", "a1", "/v1")),
    ("/m/B/t/a12/v1/models", ("B", "t", "a12", "/v1/models")),
    # THE OLD FORM MUST STILL WORK. `docs/52` and `setup_gateway_arm.py` document it, and a metered
    # call that arrives on the short path has to be metered, not refused.
    ("/m/B/kcenters/v1/chat/completions", ("B", "kcenters", "", "/v1/chat/completions")),
    ("/m/A/svm/v1", ("A", "svm", "", "/v1")),
    # and the shapes that name no task at all
    ("/v1/chat/completions", ("?", "?", "", "/v1/chat/completions")),
    ("/healthz", ("?", "?", "", "/healthz")),
    ("/m/healthz", ("?", "?", "", "/m/healthz")),
])
def test_the_parser_reads_both_url_shapes(path, expected):
    """The two forms have the SAME number of slashes once the tail is long enough
    (`/m/A/t/v1/chat/completions` and `/m/A/t/a3/v1/chat` both split into six), so they are told
    apart by whether the fourth segment is `v1` -- where the UPSTREAM path begins -- and never by a
    length count."""
    assert _Parse(path)._split_path() == expected


# ------------------------------------------------------------------------------------------------
# the real proxy, over a real socket
# ------------------------------------------------------------------------------------------------
_ANSWER = {
    "id": "chatcmpl-1", "model": "deepseek-v4-flash", "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
}


class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps(_ANSWER).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def meter(tmp_path):
    """(post, rows) — the real proxy in front of a fake upstream. Its own port, its own log."""
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()

    log = tmp_path / "meter.jsonl"
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({
        "source": "test", "fetched_at": "now", "cost_basis": "imputed",
        "default": {"input_per_token": 1e-6, "output_per_token": 2e-6}, "models": {}}),
        encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(PROXY), "--port", str(port), "--host", "127.0.0.1",
         "--upstream", f"http://127.0.0.1:{up.server_port}/v1", "--api-key", "k",
         "--log", str(log), "--pricing", str(pricing), "--rpm", "0", "--timeout", "30"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(200):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).read()
            break
        except Exception:                           # noqa: BLE001 - waiting for the listener
            time.sleep(0.05)
    else:                                           # pragma: no cover - the proxy never came up
        proc.kill()
        up.shutdown()
        pytest.fail("meter proxy did not start: " + (proc.stdout.read() if proc.stdout else ""))

    def post(tail: str) -> int:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{tail}",
            data=json.dumps({"model": "deepseek-v4-flash",
                             "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
            return resp.status

    def rows() -> list:
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]

    try:
        yield post, rows
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        up.shutdown()


def test_the_proxy_records_the_attempt_the_campaign_named(meter):
    """It COPIES the segment and never synthesises one -- that is what makes the id joinable to the
    marker `record_done` wrote for the same run."""
    post, rows = meter
    assert post("/m/B/kcenters/a4/v1/chat/completions") == 200
    row = rows()[-1]
    assert (row["arm"], row["task"], row["attempt"]) == ("B", "kcenters", "a4")
    assert row["metered"] is True and row["cost"] > 0


def test_the_two_segment_form_is_still_metered_and_says_it_named_no_attempt(meter):
    """BACK-COMPAT IS A MONEY PROPERTY HERE. A call refused, or metered under `?/?`, because its URL
    is the old shape is a paid request missing from the ledger -- the one thing this file exists to
    prevent. `attempt` is present and EMPTY: the caller named none, which is a different fact from
    a pre-2026-08-23 row where the key is absent entirely."""
    post, rows = meter
    assert post("/m/B/kcenters/v1/chat/completions") == 200
    row = rows()[-1]
    assert (row["arm"], row["task"]) == ("B", "kcenters")
    assert row["attempt"] == "", row
    assert row["metered"] is True and row["cost"] > 0


def test_two_attempts_at_one_task_sum_separately_and_by_equality_alone(meter):
    """THE DEFECT, REPRODUCED AT SCALE 1:1000 AND THEN FIXED.

    Two attempts at `kcenters`, three calls and one call. Summed by `(arm, task)` -- the only key
    the old log has -- they are one $-figure that matches neither run, which is exactly how
    `B/kcenters` came to read $2.0086 against a $1.0070 run. Summed by `(arm, task, attempt)` they
    are two, and getting there needs no timestamps at all.
    """
    post, rows = meter
    for _ in range(3):
        post("/m/B/kcenters/a1/v1/chat/completions")
    post("/m/B/kcenters/a2/v1/chat/completions")

    by_task: dict = collections.defaultdict(float)
    by_attempt: dict = collections.defaultdict(float)
    calls: dict = collections.Counter()
    for row in rows():
        if row.get("task") != "kcenters":
            continue
        by_task[(row["arm"], row["task"])] += row["cost"]
        by_attempt[(row["arm"], row["task"], row["attempt"])] += row["cost"]
        calls[row["attempt"]] += 1

    assert len(by_task) == 1, "the coarse key is unchanged -- that is the defect, not the fix"
    assert set(by_attempt) == {("B", "kcenters", "a1"), ("B", "kcenters", "a2")}
    assert calls == {"a1": 3, "a2": 1}
    # each attempt's own total, and the two of them are the task's total. No `ts` was read.
    assert by_attempt[("B", "kcenters", "a1")] == pytest.approx(3 * by_attempt[
        ("B", "kcenters", "a2")])
    assert sum(by_attempt.values()) == pytest.approx(by_task[("B", "kcenters")])


def test_an_upstream_failure_row_is_attributed_to_its_attempt_too(tmp_path):
    """A 502 costs nothing and still belongs to an attempt: `_fail` writes a row, and a row with no
    attempt is a row that cannot be joined to the run that provoked it. Forty of the live log's
    rows are gateway timeouts on ONE task-arm, and they are three and a half hours of its wall."""
    log = tmp_path / "meter.jsonl"
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({"source": "t", "fetched_at": "n", "cost_basis": "imputed",
                                   "default": {}, "models": {}}), encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(PROXY), "--port", str(port), "--host", "127.0.0.1",
         # nothing is listening on this upstream, so every call is an `_open_upstream` failure
         "--upstream", f"http://127.0.0.1:{_free_port()}/v1", "--api-key", "k",
         "--log", str(log), "--pricing", str(pricing), "--rpm", "0", "--timeout", "5"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).read()
                break
            except Exception:                       # noqa: BLE001
                time.sleep(0.05)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/m/A/count_riemann_zeta_zeros/a2/v1/chat/completions",
            data=b'{"model":"m","messages":[]}', headers={"Content-Type": "application/json"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception:                           # noqa: BLE001 - a 502 is the point
            pass
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    rows = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln]
    assert rows, "no row was written for a failed call"
    assert rows[-1]["attempt"] == "a2", rows[-1]
    assert rows[-1]["metered"] is False


# ------------------------------------------------------------------------------------------------
# the URL the DRIVER actually exports, from the real driver
# ------------------------------------------------------------------------------------------------
_CONFIG = """\
global:
  spend_limit: 0.02
models:
  gateway/deepseek-v4-flash:
    model_name: "openai/deepseek-v4-flash"
    spend_limit: 1.0
"""

# `algotune.sh` is what `run_one` invokes for arm A. This stub records the meter URL it was given
# and exits 0, which is the whole arm: the property is "what URL did the task get", and asking the
# driver is the only honest way to answer it.
_STUB_ALGOTUNE = """\
#!/bin/bash
echo "OPENAI_BASE_URL=$OPENAI_BASE_URL"
echo "LOOPLAB_LLM_BASE_URL=$LOOPLAB_LLM_BASE_URL"
exit 0
"""


def _fake_algotune(tmp_path: Path) -> Path:
    root = tmp_path / "AlgoTune"
    (root / "AlgoTuner" / "config").mkdir(parents=True, exist_ok=True)
    (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (root / ".venv" / "bin" / "activate").write_text(": # nothing to activate\n", encoding="utf-8")
    (root / "AlgoTuner" / "config" / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (root / "algotune.sh").write_text(_STUB_ALGOTUNE, encoding="utf-8")
    (root / "algotune.sh").chmod(0o755)
    return root


def _run_campaign(tmp_path: Path, **extra) -> subprocess.CompletedProcess:
    """The REAL driver, one arm-A task, stubbed AlgoTune.

    LANES/CORE_OFFSET come from this process's OWN cpu affinity, so the stub is pinned inside the
    cores the test runner was given and can never land on a core a live campaign owns. Hardcoding a
    range would be both unsafe here and unportable everywhere else.
    """
    cores = sorted(os.sched_getaffinity(0))
    env = dict(os.environ, ARM="A", ALGOTUNE_ROOT=str(_fake_algotune(tmp_path)),
               BUDGET_USD="1.0", ALGOTUNE_MODEL_KEY="gateway/deepseek-v4-flash",
               METER_BASE="http://127.0.0.1:8899", TASKS="kcenters", SNAPSHOT="0",
               LANES="1", CORES_PER_LANE="1", CORE_OFFSET=str(cores[0]),
               CAMPAIGN_OUT=str(tmp_path / "out"), CAMPAIGN_WS=str(tmp_path / "ws"),
               CAMPAIGN_RUNS=str(tmp_path / "runs"))
    env.update(extra)
    return subprocess.run(["bash", str(CAMPAIGN)], capture_output=True, text=True, timeout=300,
                          env=env)


def test_the_driver_gives_the_task_a_url_that_names_the_attempt(tmp_path):
    """END TO END, THROUGH THE REAL DRIVER. The stub prints the environment `run_one` handed it, so
    this is the URL a real AlgoTuner would have called -- not a template read out of the source."""
    proc = _run_campaign(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = (tmp_path / "out" / "A-kcenters.log").read_text(encoding="utf-8")
    assert "OPENAI_BASE_URL=http://127.0.0.1:8899/m/A/kcenters/a1/v1" in log, log
    assert "LOOPLAB_LLM_BASE_URL=http://127.0.0.1:8899/m/A/kcenters/a1/v1" in log, log
    # the marker names the same attempt, which is what makes the two joinable
    assert "attempt=a1" in (tmp_path / "out" / "A-kcenters.done").read_text()
    assert (tmp_path / "out" / "A-kcenters.attempts").read_text().startswith("a1 ")


def test_a_resume_that_skips_a_done_task_does_not_burn_an_attempt_number(tmp_path):
    """The resume check used to sit BELOW the meter block, inside each arm branch. Harmless while
    the path held no per-attempt state; with an allocator there it would mint an id for every
    already-done task on every resume -- a 20-task arm resumed once would jump to `a2` on all
    twenty, and the numbers in the markers would stop matching the numbers in the log."""
    first = _run_campaign(tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    second = _run_campaign(tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already done" in second.stdout, second.stdout
    ledger = (tmp_path / "out" / "A-kcenters.attempts").read_text().splitlines()
    assert len(ledger) == 1, ledger
    assert "attempt=a1" in (tmp_path / "out" / "A-kcenters.done").read_text()


def test_a_rerun_of_a_wall_cut_task_arm_gets_its_own_attempt(tmp_path):
    """The two fixes meet here. `RETRY_WALL_CUT=1` reopens exactly the wall cuts, and because the
    id is minted per RUN rather than per task, the retry's calls land under `a2` while the cut
    attempt's stay under `a1` -- which is the whole reason the third segment exists."""
    assert _run_campaign(tmp_path).returncode == 0
    marker = tmp_path / "out" / "A-kcenters.done"
    marker.write_text("wall=14400 rc=124 state=wall_cut cpus=0-0 lanes=1 cores_per_lane=1 "
                      "attempt=a1\n")
    again = _run_campaign(tmp_path, RETRY_WALL_CUT="1")
    assert again.returncode == 0, again.stdout + again.stderr
    log = (tmp_path / "out" / "A-kcenters.log").read_text(encoding="utf-8")
    assert "/m/A/kcenters/a2/v1" in log, log
    assert "attempt=a2" in marker.read_text()
    assert len((tmp_path / "out" / "A-kcenters.attempts").read_text().splitlines()) == 2
