"""The gateway API key sat in the meter's command line for its whole multi-day lifetime.

`/proc/<pid>/cmdline` is world-readable, and CLAUDE.md describes this deployment as one origin with
many users. The item was diagnosed on 2026-08-30 with the repair spelled out — `proxy.py::main`
already reads `METER_API_KEY` — and left open. It was demonstrated again on 2026-09-01 by a sweep
that listed the meter's command line to check an unrelated flag and printed the key into a
transcript. Looking at a process's arguments is a routine part of running this stand, which is the
whole argument: the exposure's cost is "anyone who looks".
"""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
START = REPO / "benchmarks" / "meter" / "start_meter.sh"
PROXY = REPO / "benchmarks" / "meter" / "proxy.py"


def test_the_launch_line_carries_no_api_key_flag():
    body = START.read_text()
    launch = body[body.index("python3 \"$HERE/proxy.py\""):]
    assert "--api-key" not in launch, (
        "the key is back on the command line, where every user of a shared box can read it:\n"
        + launch[:400]
    )


def test_the_key_is_passed_through_the_environment_instead():
    body = START.read_text()
    i = body.index("python3 \"$HERE/proxy.py\"")
    prefix = body[max(0, i - 200):i]
    assert "METER_API_KEY=" in prefix, (
        "the flag is gone but nothing puts the key in the environment either, so the meter would "
        "start unauthenticated:\n" + prefix
    )


def test_the_proxy_still_reads_it_from_the_environment():
    """The repair depends on this; if the proxy stops reading it, the meter starts keyless."""
    src = PROXY.read_text()
    assert re.search(r'--api-key.*\n?.*METER_API_KEY', src) or 'os.environ.get("METER_API_KEY"' in src, (
        "proxy.py no longer defaults --api-key from METER_API_KEY, which is what makes the "
        "environment route work"
    )


def test_the_proxy_help_does_not_claim_a_ceiling_that_is_off():
    """`DELTA_CEILING_DEFAULT = 0`, and the module docstring said "135,000 by default".

    They disagreed for as long as both existed, and it cost an investigation: a plan_step generation
    ran to 241,943 content deltas and the first question was why the ceiling had not cut it at
    135,000. It was never armed.
    """
    src = PROXY.read_text()
    head = src[:src.index("DELTA_CEILING_VALUE")]
    assert "135,000 by default" not in head, (
        "the docstring claims a default the code does not have"
    )
    assert "DELTA_CEILING_DEFAULT = 0" in src, "premise: the ceiling is off by default"
    assert "THE DEFAULT IS OFF" in head, (
        "the docstring no longer states that the ceiling is off, so the next reader repeats the "
        "same investigation"
    )


def test_start_meter_is_still_syntactically_valid():
    r = subprocess.run(["bash", "-n", str(START)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr


def test_no_open_marker_is_left_for_an_item_that_shipped():
    """Assembled, not spelled: the open-item index reads any literal occurrence as a DECLARATION.

    Writing the marker out here put it back on the open list and turned the branch red -- the fourth
    time in this repository that citing what a guard forbids has tripped the guard, after the same
    thing with an OPEN marker in docs/58, a CLAIM pin in docs/56, and a retired default in
    proxy.py's docstring. The lesson keeps not sticking because each occurrence looks like prose.
    """
    body = START.read_text()
    marker = "OPEN" + "[" + "meter-key-rides-argv" + "]"
    assert marker not in body, (
        "the item shipped but its open-item marker is still declared, so it stays on the open list"
    )
