#!/usr/bin/env python3
"""Resume probes the provider paused -- once the provider is answering again.

WHY THIS EXISTS, measured 2026-09-08. The corporate gateway flapped twice in one shift: the two
outages lasted **23.8 and 13.5 minutes**, while the engine's own retry ladder spans about **2.5**
(9 attempts, 2+4+8+16+30+30+30+30 s). An outage an order of magnitude longer than the ladder ends
the same way every time: the Researcher gets a degraded fallback, nothing is proposed, and the run
AUTO-PAUSES holding its paid state. Twice today I cleared that by hand.

The pause is right and stays: `looplab` refuses to run on empty fallback proposals, which is what
would turn an outage into a flat, meaningless result. What is missing is the other half -- noticing
that the endpoint is back and picking the run up where it stands.

THREE REFUSALS, because an automatic resume spends money:

  * only a run `arm_fidelity._paused` calls paused -- that decides on the SPEND, so a run stopped at
    its ceiling is complete however it was worded (§228) and is never resumed;
  * only when the endpoint answers, checked through a SERVICE arm so the probe's own ledger is not
    charged for the check;
  * at most `--max-resumes` times per probe, counted from its own events, so a permanently broken
    provider cannot be paid for in a loop.

Usage:  resume_paused.py [--bench ROOT] [--max-resumes 3] [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import arm_fidelity  # noqa: E402

DEFAULT_BENCH = os.environ.get("BENCH_ROOT", "/var/tmp/looplab-bench")
METER = os.environ.get("LOOPLAB_METER", "http://127.0.0.1:8801")


def probe_lane(bench: str, name: str, default: str = "0-10,48-58") -> str:
    """The lane this probe was measured on, from its own INSTRUMENT.txt.

    A resume on a DIFFERENT lane is a different measurement: §262 recorded 3 % between lanes and
    §314 an order of magnitude between widths. The instrument file records the lane precisely so a
    later run cannot quietly change it; guessing here would undo that.
    """
    try:
        for line in open(f"{bench}/model-probes/{name}/INSTRUMENT.txt", encoding="utf-8"):
            if line.startswith("lane:"):
                return line.split(":", 1)[1].strip() or default
    except OSError:
        pass
    return default


def resumes_so_far(root: str, name: str) -> int:
    """How many times this run has already been picked up, from its own log.

    `run_loop_exited` is written once per loop that ends; a run that has never been resumed has one.
    Counting the events rather than a marker file keeps the number in the same append-only record
    everything else here is read from.
    """
    n = 0
    for path in glob.glob(f"{root}/{name}/runs/*/run/events.jsonl"):
        for line in open(path, encoding="utf-8", errors="replace"):
            if '"run_loop_exited"' in line:
                n += 1
    return max(0, n - 1)


def endpoint_answers(task: str = "discrete_log", label: str = "svcResumeCheck") -> bool:
    """One smoke call through a SERVICE arm, so the probe's own ledger stays clean."""
    url = f"{METER}/m/{label}/{task}/p1/v1"
    env = {**os.environ, "LOOPLAB_LLM_STREAM": "1", "LOOPLAB_LLM_MODEL": "deepseek-v4-flash",
           "LOOPLAB_LLM_BASE_URL": url, "LOOPLAB_LLM_API_KEY_BASE_URL": url,
           "LOOPLAB_LLM_API_KEY": "meter", "OPENAI_BASE_URL": url}
    try:
        got = subprocess.run([sys.executable, "-m", "looplab.cli", "smoke"], env=env,
                             capture_output=True, text=True, timeout=300, cwd=str(HERE.parent))
    except (OSError, subprocess.SubprocessError):
        return False
    return "text OK" in got.stdout


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", default=DEFAULT_BENCH)
    ap.add_argument("--max-resumes", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    # LAUNCHING IS OPT-IN. The default prints the command, because this tool decides to SPEND: a
    # resume picks the run up at its ceiling-minus-spend and keeps going. A reporter that can be
    # read before it is trusted is the version that gets trusted.
    ap.add_argument("--launch", action="store_true", help="actually start the resume(s)")
    args = ap.parse_args(argv)

    root = f"{args.bench}/model-probes"
    paused = []
    for path in glob.glob(f"{root}/*/runs/*/run/events.jsonl"):
        name = path.split("/model-probes/")[1].split("/")[0]
        if name in {p["probe"] for p in paused}:
            continue
        try:
            if not arm_fidelity._paused(root, name):
                continue
        except Exception:                          # noqa: BLE001
            continue
        paused.append({"probe": name, "run": str(Path(path).parent),
                       "spend": arm_fidelity._spend(root, name),
                       "resumes": resumes_so_far(root, name)})
    if not paused:
        print("no probe is paused and owed work")
        return 0
    for p in paused:
        if p["resumes"] >= args.max_resumes:
            print(f'{p["probe"]}: already resumed {p["resumes"]}x, at the --max-resumes ceiling -- '
                  "left alone; a provider that keeps dying is not paid for in a loop")
            continue
        if not endpoint_answers():
            print(f'{p["probe"]}: paused with ${p["spend"]:.4f} of paid state, but the endpoint is '
                  "still refusing -- nothing resumed")
            continue
        if args.dry_run:
            print(f'{p["probe"]}: WOULD resume ({p["resumes"]} so far, ${p["spend"]:.4f} held)')
            continue
        task = p["run"].split("/runs/")[1].split("/")[0]
        url = f'{METER}/m/{p["probe"]}/{task}/p1/v1'
        env = {**os.environ, "LOOPLAB_LLM_STREAM": "1", "LOOPLAB_LLM_MODEL": "deepseek-v4-flash",
               "LOOPLAB_LLM_BASE_URL": url, "LOOPLAB_LLM_API_KEY_BASE_URL": url,
               "LOOPLAB_LLM_API_KEY": "meter", "OPENAI_BASE_URL": url,
               "LOOPLAB_LLM_BUDGET_USD": os.environ.get("LOOPLAB_LLM_BUDGET_USD", "1.00"),
               "ALGOTUNE_BASELINE_CACHE_DIR": f"{args.bench}/looplab/benchmarks/algotune/.baseline_times",
               "ALGOTUNE_EVAL_WORKERS": "auto", "ALGOTUNE_MIN_TIMEOUT_S": "120"}
        lane = probe_lane(args.bench, p["probe"])
        cmd = ["taskset", "-c", lane, sys.executable, "-m", "looplab.cli", "resume", p["run"]]
        if not args.launch:
            print(f'{p["probe"]}: paused with ${p["spend"]:.4f} held and the endpoint answering. '
                  f"Run with --launch, or by hand:\n  taskset -c {lane} python -m looplab.cli "
                  f'resume {p["run"]}')
            continue
        print(f'{p["probe"]}: resuming on lane {lane} ({p["resumes"]} so far, '
              f'${p["spend"]:.4f} held)')
        log = open(f'{args.bench}/logs/{p["probe"]}-autoresume.log', "ab")
        subprocess.Popen(cmd, env=env, stdout=log, stderr=log, start_new_session=True,
                         cwd=str(HERE.parent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
