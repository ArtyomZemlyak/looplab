"""The meter must not land on a lane, and the launcher must be the thing that knows it.

On 2026-08-29 the meter WAS pinned off the lanes -- `run_final-relaunch.log` records
"meter restarted ... pinned off the lanes" -- but the pinning lived in `run_final.sh`, a driver that
was never committed. It went with `/var/tmp` when the container restarted, and when the meter was
brought back up on 2026-08-31 by `start_meter.sh` alone it came back on `0-95`. Measured at 0.0 %
CPU, so nothing was actually spoiled; it was one busy proxy away from putting its own CPU into a
lane's timings, and nothing in the tree would have said so.

The meter is infrastructure every lane talks to. A lane it shares is a lane whose measurement
includes a proxy. So the fact belongs in the box profile and its application in the launcher, where
both are committed, and this test asserts the real process affinity rather than the shape of the
command line.
"""
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

START = Path(__file__).resolve().parents[1] / "benchmarks" / "meter" / "start_meter.sh"
PORT = "8899"          # not 8801: start_meter kills by port, and a live campaign meter must survive


def _affinity(pid: int) -> str:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("Cpus_allowed_list:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("no Cpus_allowed_list")


def _pids():
    out = subprocess.run(["pgrep", "-f", f"meter/proxy.py --port {PORT}"],
                         capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


def _stop():
    for p in _pids():
        try: os.kill(p, signal.SIGTERM)
        except ProcessLookupError: pass
    time.sleep(0.5)


@pytest.fixture
def meter(tmp_path):
    _stop()
    yield
    _stop()


def _start(tmp_path, cpus):
    env = {**os.environ, "METER_PORT": PORT, "METER_UPSTREAM": "http://127.0.0.1:1",
           "METER_API_KEY": "x", "METER_LOG": str(tmp_path / "m.jsonl"),
           "METER_STDOUT": str(tmp_path / "m.log")}
    if cpus is None:
        env.pop("METER_CPUS", None)
    else:
        env["METER_CPUS"] = cpus
    return subprocess.run(["bash", str(START)], env=env, capture_output=True, text=True, timeout=120)


def test_the_meter_lands_on_the_cores_the_box_profile_named(tmp_path, meter):
    result = _start(tmp_path, "44-45")
    pids = _pids()
    assert pids, result.stdout + result.stderr
    assert _affinity(pids[0]) == "44-45", (
        "the meter is on cores the box profile did not give it; on this box that means a lane\n"
        + result.stdout)
    assert "pinned to 44-45" in result.stdout


def test_an_unpinned_meter_says_so_instead_of_going_quietly(tmp_path, meter):
    """The 2026-08-31 shape exactly: it came up on 0-95 and said nothing, so nobody looked."""
    result = _start(tmp_path, None)
    assert "UNPINNED" in result.stderr, result.stdout + result.stderr


# ---------------------------------------------------------------- and "off the lanes" is derivable
#
# `METER_CPUS=44-47,92-95` is justified in the box profile by a sentence -- "the lanes here are
# 0-10+48-58, 11-21+59-69, 22-32+70-80 and 33-43+81-91" -- and NOTHING re-derived it. That sentence
# also contradicts the header of its own file thirty lines up ("20 lanes x 2 cores = 40"), which
# predates the 2026-08-24 move to whole-physical-core lanes and was never revised.
#
# Both are checkable, because `campaign.sh` computes the lanes itself and the finished campaign's
# markers record what it computed. `campaign-final/B-*.done` carries
# `lanes=4 cores_per_lane=22` with `cpus=0,48,1,49,...,10,58`, which is the first of the four ranges
# the profile names. So the planner is run here, for the regime that produced the numbers AND for
# the shipped default, and the pin is asserted disjoint from every lane in both.
import re  # noqa: E402
import sys  # noqa: E402

CAMPAIGN = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "campaign.sh"
PROFILE = Path(__file__).resolve().parents[1] / "benchmarks" / "box-jhub-l40s.sh"


def _cpus(spec: str) -> set:
    out = set()
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        elif part:
            out.add(int(part))
    return out


def _lane_plan(lanes: int, per_lane: int, offset: int = 0) -> list:
    """`campaign.sh`'s OWN lane planner, run against this box's real topology."""
    body = CAMPAIGN.read_text(encoding="utf-8")
    src = body.split("<<'PYEOF'\n", 1)[1].split("\nPYEOF\n", 1)[0]
    out = subprocess.run([sys.executable, "-c", src, str(lanes), str(per_lane), str(offset)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return [line for line in out.stdout.splitlines() if line.strip()]


def _meter_cpus_default() -> str:
    m = re.search(r'METER_CPUS="\$\{METER_CPUS:-([^}]+)\}"', PROFILE.read_text(encoding="utf-8"))
    assert m, "the box profile no longer declares METER_CPUS"
    return m.group(1)


@pytest.mark.parametrize("lanes,per_lane", [(4, 22), (20, 2)])
def test_the_pinned_cores_are_not_in_any_lane_the_campaign_would_hand_out(lanes, per_lane):
    """(4, 22) is the regime the finished campaign's markers record; (20, 2) is the shipped default
    and what the profile's own header still describes. The pin must be off the lanes in both, or
    the sentence that justifies it is true only of a configuration nobody is running."""
    plan = _lane_plan(lanes, per_lane)
    if plan and plan[0] == "FALLBACK":
        pytest.skip("not enough physical cores here for this regime; the planner fell back")
    assert len(plan) == lanes
    meter = _cpus(_meter_cpus_default())
    for i, lane in enumerate(plan):
        overlap = sorted(meter & _cpus(lane))
        assert not overlap, (
            f"lane {i} of {lanes}x{per_lane} owns {overlap}, which METER_CPUS also claims — "
            "the meter would sit inside a lane whose timings are the measurement")


def _profile_prose() -> str:
    """The profile's comments as one string, so a claim that spans two lines is still one claim."""
    return " ".join(line.lstrip("#").strip()
                    for line in PROFILE.read_text(encoding="utf-8").splitlines()
                    if line.lstrip().startswith("#"))


def _lanes_the_profile_names() -> list:
    """The four ranges READ OUT OF the profile, not copied into this file.

    The point of the test below is that the sentence beside METER_CPUS is true. A hand-typed copy
    of it here checks that the PLANNER has not moved and says nothing at all about the sentence --
    which is the artefact an operator reads before deciding what the spare cores are for, and the
    only reason METER_CPUS has the value it has.
    """
    m = re.search(r"the lanes are (.+?), i\.e\.", _profile_prose())
    assert m, ("the box profile no longer names the lanes that justify METER_CPUS; if the sentence "
               "moved, this test must follow it rather than keep its own copy")
    return [sorted(_cpus(part.strip().replace("+", ",")))
            for part in re.split(r",| and ", m.group(1)) if part.strip()]


def _leftover_the_profile_names() -> list:
    m = re.search(r"the four left over are ([\d,\-]+) with their siblings ([\d,\-]+)",
                  _profile_prose())
    assert m, "the box profile no longer says which cores the lanes leave free"
    return sorted(_cpus(m.group(1)) | _cpus(m.group(2)))


def test_the_lanes_the_profile_names_are_the_ones_the_planner_produces():
    """The sentence beside METER_CPUS, re-derived rather than believed. Its four ranges are the
    campaign's own `lanes=4 cores_per_lane=22` allocation, cpu-for-cpu.

    Driven 2026-08-31: this used to compare the planner against `named = [...]`, a hand-copy of the
    sentence kept HERE. Rewriting the profile's ranges to a false set (`0-9+48-57, 10-20+58-68,
    21-31+69-79 and 32-42+80-90`) left the whole file green -- the copy was what was being checked,
    and the sentence an operator actually reads was free to say anything.
    """
    plan = _lane_plan(4, 22)
    if plan and plan[0] == "FALLBACK":
        pytest.skip("not enough physical cores here for the campaign's regime")
    named = _lanes_the_profile_names()
    assert len(named) == 4, f"the profile names {len(named)} lanes for a lanes=4 regime: {named}"
    assert [sorted(_cpus(lane)) for lane in plan] == named, (
        "the profile's sentence describes a lane allocation campaign.sh does not produce, and it "
        "is that sentence -- not this test -- that says what the spare cores are free FOR")


def test_the_cores_the_profile_calls_left_over_are_the_ones_it_pins_the_meter_to():
    """The other half of the same sentence, and the one that decides METER_CPUS.

    "the four left over are 44-47 with their siblings 92-95. That is what they are free FOR" is the
    entire argument for the value exported thirty lines down. Neither end was derived from the
    other, so the sentence and the export could drift apart with nothing to notice.
    """
    plan = _lane_plan(4, 22)
    if plan and plan[0] == "FALLBACK":
        pytest.skip("not enough physical cores here for the campaign's regime")
    leftover = _leftover_the_profile_names()
    assert leftover == sorted(_cpus(_meter_cpus_default())), (
        "METER_CPUS is not the set of cores the profile's own sentence calls left over")
    lanes = set().union(*(set(lane) for lane in _lanes_the_profile_names()))
    assert not (set(leftover) & lanes), "the profile calls a core both a lane and a spare"
