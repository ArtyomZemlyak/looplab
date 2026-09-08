#!/usr/bin/env python3
"""Which bench probes are actually running, and on which lanes — by argv, not by text match.

WHY THIS EXISTS. Point 1 of the standing sweep is "looplab.cli run processes on their lanes", and
the obvious way to answer it is to walk `/proc` looking for command lines containing `looplab.cli`
and `run`. That matcher counts **a grep for the probe command line as a probe**, because a search
for a string contains that string. Measured 2026-09-04: `grep -rn "python -m looplab.cli run --out"
…` samples as `argv[0]=grep`, affinity 96 cpus, and the naive matcher calls it a probe while the
rule below does not. Two phantom "run" lines went through the sweep that morning — both unpinned,
both on no lane, both gone within seconds.

`run_probe.sh` already got this right on 2026-09-01, when sampling its lane guard through a full
pytest suite turned up `python -m looplab.cli ui --help`, `python -m looplab.cli resume /tmp/…/run`
and a `ugrep` for the probe line. Its rule lives inside a heredoc where nothing else can call it,
which is how the bench ended up with a fixed copy and a naive one at the same time — the shape §176
is about. `test_a_probe_is_not_a_grep_for_one.py` pins the two to agree.

THE RULE. A bench probe is a PYTHON INTERPRETER running the MODULE with `run` or `resume`, whose
run directory is under the bench root. `argv[0]` is the discriminator no text match can forge.
AlgoTuner's own entry points keep a bare name match: only this bench ever starts them.

Usage:
    lanes.py [--root DIR] [--lane 0-10,48-58]
"""
from __future__ import annotations

import argparse
import os

DEFAULT_ROOT = "/var/tmp/looplab-bench"
THEIRS = ("AlgoTuner", "algotune.sh")


def is_bench_probe(argv, root: str = DEFAULT_ROOT) -> bool:
    """Is this argv a bench probe? The rule `run_probe.sh` uses to decide a lane is occupied."""
    if not argv or "-c" in argv:
        return False
    line = " ".join(argv)
    if any(k in line for k in THEIRS):
        return True
    return (os.path.basename(argv[0]).startswith("python")
            and "-m" in argv and "looplab.cli" in argv
            and any(v in argv for v in ("run", "resume"))
            and root in line)


# THE FOUR BENCH LANES AND THE SERVICE PAIR, as the standing sweep list writes them (point 5). Kept
# here rather than in each caller because a lane is a SET: `48-58,0-10` is the same lane as
# `0-10,48-58`, and two callers comparing strings would disagree about that. `snapshot.sh` carries
# the same service pair as `SNAPSHOT_SERVICE_LANE`; that copy is a shell default and cannot import
# this one, so the pair is named in both and this comment is the link between them.
BENCH_LANES = ("0-10,48-58", "11-21,59-69", "22-32,70-80", "33-43,81-91")
SERVICE_LANE = "44-47,92-95"


def lane_fault(cpus: set[int]) -> str | None:
    """What is wrong with this probe's cpu set, or None when it sits exactly on a bench lane.

    §358. `pulse` printed the lane and never judged it. Two things make that worth checking on this
    box rather than assuming: every measuring tool here runs pinned to the SERVICE pair, so a probe
    that overlapped it would be perturbed by the very sweep that measures it -- the second
    instrument changing what the first measures; and a probe pinned to a SUBSET of a lane is the
    exact shape of the stray rulers of 2026-09-07, which ran on two cpus, `{0, 48}`, and wrote into
    the live cache for hours while every tool reported the lane they were born on.
    """
    if not cpus:
        return "no cpu affinity at all"
    service = parse_lane(SERVICE_LANE) & cpus
    if service:
        return (f"overlaps the SERVICE lane on cpu(s) {sorted(service)} -- every measuring tool on "
                "this box is pinned there, so the sweep would be adding load to what it measures")
    for spec in BENCH_LANES:
        if cpus == parse_lane(spec):
            return None
    for spec in BENCH_LANES:
        want = parse_lane(spec)
        if cpus < want:
            return (f"pinned to {len(cpus)} of the {len(want)} cpus of lane {spec} -- a SUBSET, "
                    "which is how the stray rulers of 2026-09-07 ran on two cpus")
    return f"is on no bench lane: {sorted(cpus)[:6]}{'...' if len(cpus) > 6 else ''}"


def parse_lane(spec: str) -> set[int]:
    """`"0-10,48-58"` -> the cpu set. Single cpus (`"7"`) are allowed."""
    want: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            want.update(range(int(a), int(b) + 1))
        else:
            want.add(int(part))
    return want


def _argv(pid: str, proc: str) -> list[str]:
    with open(f"{proc}/{pid}/cmdline", "rb") as fh:
        return [x.decode("utf-8", "replace") for x in fh.read().split(b"\0")[:-1] if x]


def probes(root: str = DEFAULT_ROOT, proc: str = "/proc", affinity=None) -> list[dict]:
    """`{pid, argv, cpus, probe}` for every bench probe now running.

    `affinity` is injectable so the scan is testable against a fake `/proc`; by default it is
    `os.sched_getaffinity`, which is the only reading of a lane that is not a guess.
    """
    affinity = affinity or os.sched_getaffinity
    out = []
    for pid in sorted(os.listdir(proc), key=lambda p: int(p) if p.isdigit() else 0):
        if not pid.isdigit():
            continue
        try:
            argv = _argv(pid, proc)
            if not is_bench_probe(argv, root):
                continue
            cpus = set(affinity(int(pid)))
        except (OSError, ValueError):
            # A probe that exits mid-scan is not an error: /proc entries vanish under the reader.
            continue
        name = ""
        for part in argv:
            if "/model-probes/" in part:
                name = part.split("/model-probes/", 1)[1].split("/")[0]
                break
        out.append({"pid": int(pid), "argv": argv, "cpus": cpus, "probe": name})
    return out


def lane_busy(lane: str, root: str = DEFAULT_ROOT, proc: str = "/proc", affinity=None) -> list[dict]:
    """The probes occupying `lane` right now. Empty means the lane is free."""
    want = parse_lane(lane)
    return [p for p in probes(root, proc, affinity) if p["cpus"] & want]


def _fmt(cpus: set[int]) -> str:
    """`{0..10, 48..58}` -> `"0-10,48-58"`, so a lane reads back as it was written."""
    if not cpus:
        return "(none)"
    runs, start, prev = [], None, None
    for c in sorted(cpus):
        if start is None:
            start = prev = c
        elif c == prev + 1:
            prev = c
        else:
            runs.append((start, prev))
            start = prev = c
    runs.append((start, prev))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--lane", help="report only probes occupying this lane")
    args = ap.parse_args(argv)

    rows = lane_busy(args.lane, args.root) if args.lane else probes(args.root)
    if not rows:
        print(f"no bench probe running under {args.root}"
              + (f" on lane {args.lane}" if args.lane else ""))
        return 0
    print(f'{"pid":>8s}  {"probe":10s} lane')
    for row in rows:
        print(f'{row["pid"]:8d}  {row["probe"] or "?":10s} {_fmt(row["cpus"])}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
