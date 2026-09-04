#!/usr/bin/env python3
"""Did the treatment actually differ from the control? — without looking at the outcome.

§190's design forbids reading the arm before twelve batches are in, and §180 is the record of why.
But treatment FIDELITY is not the outcome: whether the capped probes really made fewer probe calls
than the uncapped ones is a property of the intervention, and finding out late that the two arms
did the same thing is how $48 becomes nothing. §195 is that failure caught four minutes in; this is
the same question asked continuously.

**This tool reads no scores, on purpose.** It counts `run_probe` calls and refusals and stops. A
version that also printed the champion would turn every fidelity check into an interim read of the
arm, which the design forbids and which no amount of discipline reliably prevents once the number
is on the screen.

THE CHANNEL, NOT ONLY THE DOSE. Capping probes is supposed to work by pushing the developer towards
the graded measurement -- the refusal text says in so many words that `run_dev_command("eval_train")`
is what measuring the solver is for -- so a cap that reduced probes and changed nothing else would be
an intervention with no channel. That is a property of the intervention, like the probe count, and it
is counted here for the same reason: finding out at batch twelve that the two arms did the same thing
is how $48 becomes nothing. Measured over the eight finished probes of batches 1 and 2: probes
treat 12.0 vs control 26.0, and `eval_train` **treat 33.0 vs control 24.5** -- the capped runs turn
about fourteen ungraded probes into about eight and a half graded evaluations.

AND THAT IS A DOSE, NOT A MECHANISM. The count says the push LANDED; it says nothing about whether
anything flows through it. Measured over 78 corpus runs of this task (§224): the top and bottom
deciles by score differ in `run_probe` (24 vs 31, p = 0.048) and do NOT differ in `eval_train`
(28 vs 27, p = 1.00). So if the cap helps, this is not yet evidence of HOW -- and a column that
moves is exactly the kind of number that gets read as a mechanism if nobody writes this paragraph.

WHAT TO WATCH. §196 measured that a cap of 12 bites 91 % of `edge_expansion` runs, so most control
probes should land ABOVE 12 and every treated probe at exactly 12. A batch where the control also
sits at nine or ten is a batch with little contrast — it dilutes the effect the power table assumed,
and the honest response is to say so, not to reinterpret it afterwards.

Usage:
    arm_fidelity.py --treat capA2 capB2 --control freeA2 freeB2 [--root DIR]

WHAT "FINISHED" MEANS HERE, and it took two goes. A running probe's probe-count is a lower bound and
a finished one's is the answer, so the contrast is computed over finished probes only. The first fix
asked whether the run's result file EXISTS -- wrong in the direction that matters, because a PAUSED
run writes one too: `freeB3` auto-paused at node 2 on 2026-09-04 ("a Developer session crashed, LLM
unreachable") having spent $0.86 of its $1.00, and it was counted as a completed control. The claim
is an EVENT: every genuinely finished probe of batches 1 and 2 carries `run_finished` with
`reason=budget_exhausted`; the paused one carries a `pause` and no `run_finished`. And the state is
the LAST lifecycle event, not any of them -- the log is append-only, so "a pause exists" answers
PAUSED for ever, and `freeB3` read paused while it was running after a `resume`.
"""
from __future__ import annotations

import argparse
import glob
import os
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events_read  # noqa: E402

DEFAULT_ROOT = "/var/tmp/looplab-bench/model-probes"


def probe_calls(root: str, name: str) -> dict:
    """`{executed, refused, spans, finished}` for one probe tree. No scores are read.

    See the module docstring for what `finished` means and why it is an event. No scores are read
    here: event TYPES only.
    """
    executed = refused = evals = 0
    spans: set = set()
    for path in sorted(glob.glob(f"{root}/{name}/runs/*/run/spans.jsonl")):
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                # `eval_train` arrives as an ARGUMENT to `run_dev_command`, not as a tool name, so
                # the cheap prefilter is the word anywhere in the line and the claim is the parsed
                # span. Counted before the `run_probe` gate below, which returns early.
                if "eval_train" not in line and '"run_probe"' not in line:
                    continue
                try:
                    span = json.loads(line)
                except ValueError:
                    continue
                attrs = span.get("attributes") or {}
                if span.get("kind") != "tool":
                    continue
                if "eval_train" in json.dumps(attrs):
                    evals += 1
                if attrs.get("tool") != "run_probe":
                    continue
                spans.add(attrs.get("phase_span"))
                if "run_probe refused" in str(attrs.get("output", "")):
                    refused += 1
                else:
                    executed += 1
    return {"executed": executed, "refused": refused, "evals": evals, "spans": len(spans),
            "finished": _run_finished(root, name), "paused": _paused(root, name)}


LIFECYCLE = ("run_finished", "pause", "resume")


def _event_types(root: str, name: str) -> set:
    """The set of event TYPES in this probe's run, crash-atomic packets unwrapped. No data read."""
    kinds: set = set()
    for path in sorted(glob.glob(f"{root}/{name}/runs/*/run/events.jsonl")):
        for event in events_read.iter_events(path):
            kind = event.get("type")
            if isinstance(kind, str):
                kinds.add(kind)
    return kinds


def _last_lifecycle(root: str, name: str) -> str:
    """The LAST of `run_finished` / `pause` / `resume`, or "" if the run has had none.

    The LAST, not any: see the module docstring. Same correction `probe_summary::_why_no_test`
    needed -- take the last match.
    """
    last = ""
    for path in sorted(glob.glob(f"{root}/{name}/runs/*/run/events.jsonl")):
        for event in events_read.iter_events(path):
            kind = event.get("type")
            if isinstance(kind, str) and kind in LIFECYCLE:
                last = kind
    return last


def _run_finished(root: str, name: str) -> bool:
    return "run_finished" in _event_types(root, name)


def _paused(root: str, name: str) -> bool:
    return _last_lifecycle(root, name) == "pause"


def report(root: str, treat, control) -> dict:
    """Contrast over FINISHED probes only, with the running ones counted but not compared."""
    rows = {n: probe_calls(root, n) for n in list(treat) + list(control)}

    def started(names):
        return [n for n in names if rows[n]["executed"] or rows[n]["refused"]]

    t = [rows[n]["executed"] for n in started(treat) if rows[n]["finished"]]
    c = [rows[n]["executed"] for n in started(control) if rows[n]["finished"]]
    te = [rows[n]["evals"] for n in started(treat) if rows[n]["finished"]]
    ce = [rows[n]["evals"] for n in started(control) if rows[n]["finished"]]
    running = [n for n in started(list(treat) + list(control)) if not rows[n]["finished"]]
    paused = [n for n in running if rows[n]["paused"]]
    tm = statistics.median(t) if t else 0
    cm = statistics.median(c) if c else 0
    return {"rows": rows, "running": running, "paused": paused,
            "treat_n": len(t), "control_n": len(c),
            "treat_median": tm, "control_median": cm,
            "treat_evals": statistics.median(te) if te else 0,
            "control_evals": statistics.median(ce) if ce else 0,
            "eval_contrast": (statistics.median(te) - statistics.median(ce)) if (te and ce) else None,
            "contrast": (cm - tm) if (t and c) else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--treat", nargs="+", required=True)
    ap.add_argument("--control", nargs="+", required=True)
    args = ap.parse_args(argv)

    got = report(args.root, args.treat, args.control)
    print(f'{"probe":10s} {"arm":>9s} {"executed":>9s} {"refused":>8s} {"eval_train":>11s} '
          f'{"phases":>7s}  state')
    for arm, names in (("treat", args.treat), ("control", args.control)):
        for name in names:
            r = got["rows"][name]
            state = ("finished" if r["finished"]
                     else "PAUSED (owed work)" if r["paused"] else "running")
            print(f'{name:10s} {arm:>9s} {r["executed"]:9d} {r["refused"]:8d} {r["evals"]:11d} '
                  f'{r["spans"]:7d}  {state}')
    if got["contrast"] is None:
        print(f'\nno contrast to report yet: {got["treat_n"]} finished treated probe(s) and '
              f'{got["control_n"]} finished control(s). A running probe\'s count is a LOWER BOUND, '
              "and the treated ones stop at their cap while the controls are still climbing -- "
              "comparing the two mid-flight measures the clock, not the intervention.")
        if got["running"]:
            print("  still running: " + ", ".join(got["running"]))
        if got["paused"]:
            print("  PAUSED and OWED work: " + ", ".join(got["paused"]))
        return 0
    print(f'\nmedian executed over FINISHED probes: treat {got["treat_median"]} '
          f'(n={got["treat_n"]}), control {got["control_median"]} (n={got["control_n"]}), '
          f'contrast {got["contrast"]:+g}')
    if got["eval_contrast"] is not None:
        print(f'  the channel: median eval_train treat {got["treat_evals"]}, '
              f'control {got["control_evals"]}, {got["eval_contrast"]:+g} -- a cap with no channel '
              "would move probes and nothing else")
    if got["running"]:
        print("  still running (not counted): " + ", ".join(got["running"]))
    if got["paused"]:
        print("  PAUSED, not finished, and OWED work: " + ", ".join(got["paused"])
              + " -- resume or the arm is short a probe, and a probe dropped in silence is the "
                "censoring §190 was designed to avoid")
    if got["contrast"] <= 0:
        print("  NO CONTRAST: the control did not out-probe the treatment in the probes that "
              "FINISHED, so nothing separates the arms so far")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
