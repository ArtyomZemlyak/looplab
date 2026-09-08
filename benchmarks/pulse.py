#!/usr/bin/env python3
"""Points 1, 2 and 4 of the sweep — liveness, evaluations and stall — WITHOUT reading a score.

WHY THIS FILE EXISTS. `arm_fidelity` was built so the fidelity question could be asked continuously
without an interim look at the outcome, and `test_arm_fidelity_reads_no_scores.py` holds it to that.
Meanwhile the sweep's own point 2 -- "new nodes, zeros and errors" -- was answered every half hour by
a heredoc that printed the node METRICS. §190 forbids reading the arm's outcome before twelve
batches; the tool obeyed and the operator did not. Same shape as everything else this month: not a
breakage, a quiet mismatch between what I thought I was doing and what I was doing.

Point 2 never needed the value. It needs: did new nodes arrive, were any of them ZERO, and if so is
that zero a ruler refusal or a solver failure. The discriminator WAS `eval_seconds` -- a zero in
under five seconds is the harness declining, a zero at 45 s is an evaluation that ran and failed --
and the twelve zeros the corpus held when that was written looked like the second kind.
RE-COUNTED 2026-09-07 with the reasons read instead of the seconds: thirteen zero nodes across
thirteen probes -- `no_valid_speedups` 6, `evaluator_error` 4, `invalid_results` 2,
`compilation_failed` 1 -- spanning 8.3 to 60.9 s. Only TWO are the kind that sentence described
(§324), and ten of the thirteen are the arena's rather than the solver's.

The bridge, though, says why by name: `looplab_eval` classifies every refusal
(`baseline_measured_in_pass`, `regime_not_scorable_for_task`, `evaluator_timeout`, twelve more) and
the reason travels in the node's `stdout_tail`. That is read now, and the stopwatch is the fallback
for a node whose stdout the record did not keep -- because the heuristic is exactly wrong about the
costliest refusal: `evaluator_timeout` returns its zero AFTER the full timeout, so a 900-second
arena failure read as "the evaluation ran and came back invalid".

So this prints counts, seconds, violations, spend, log age and `wchan`, and never a metric. The
test that matters is behavioural: a probe whose node scores 123456.789 must not have that number
appear anywhere in the output.

Usage:
    pulse.py [--root DIR] [--bench DIR] [--stall 2400]
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "algotune"))
import arm_fidelity  # noqa: E402  (score-free by its own test; used only for finished/paused)
import check_money  # noqa: E402  (for the ledger's newest row per arm; reads money, never a score)
# WHOSE ZERO IT IS, from the one place that classifies it (§342). Importing the vocabulary rather
# than re-typing it is what stops the live instrument and the scoring one from saying different
# things about the same node -- which they did, in opposite directions, about remDL13's node 0.
import compare_arms  # noqa: E402
import events_read  # noqa: E402
import lanes  # noqa: E402

DEFAULT_BENCH = "/var/tmp/looplab-bench"
REFUSAL_SECONDS = 5.0     # a zero faster than this is the ruler declining, not the solver failing


# THE BRIDGE SAYS WHY, AND THIS TOOL WAS GUESSING FROM A STOPWATCH. `looplab_eval` classifies every
# refusal by name -- `baseline_measured_in_pass`, `regime_not_scorable_for_task`,
# `evaluator_timeout`, twelve more -- and `compare_arms` keeps a partition of that vocabulary into
# the solver's fault and the arena's. None of it reached here: the reason travels in the node's
# `stdout_tail` (the bridge's own JSON line, kept to 4,000 chars by `evaluate.py`), and this file
# inferred "refusal" from `eval_seconds < 5`.
#
# That heuristic is right for the refusals that cost no time and WRONG for the one that costs the
# most: `evaluator_timeout` returns a zero after the full timeout, so the tool an operator watches
# live called an arena failure a solver's zero -- the same misclassification `compare_arms`'
# NOT_SOLVERS_FAULT list exists to prevent, arriving through the other door.
_REASON = re.compile(r'"no_speedup"\s*:\s*\{[^{}]*?"reason"\s*:\s*"([a-z_]+)"')


def refusal_reason(data: dict) -> str | None:
    """The bridge's own name for why this node has no speedup, or None if it did not say."""
    for field in ("stdout_tail", "stderr_tail", "error_evidence"):
        text = data.get(field)
        if not isinstance(text, str):
            continue
        got = _REASON.search(text)
        if got:
            return got.group(1)
    return None


def pulse(events_path: str) -> dict:
    """Counts and diagnostics for one probe's event log. The metric is CLASSIFIED, never carried."""
    spend = 0.0
    nodes = zeros = errors = 0
    bad = []
    spend_at_last_node = None
    for event in events_read.iter_events(events_path):
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind == "llm_usage":
            try:
                spend += max(0.0, float(data.get("cost") or 0.0))
            except (TypeError, ValueError):
                pass
        elif kind == "node_evaluated":
            # EVERY evaluated node, scored or zero. §347's question is "when did this probe last
            # get an ANSWER back from the arena", and a zero is an answer: the money spent after it
            # bought no further reading either way. Counting only scored nodes would call a probe
            # that keeps earning zeros "productive" while it spends.
            spend_at_last_node = spend
            scored = data.get("metric")
            if isinstance(scored, (int, float)) and scored > 0:
                nodes += 1
            else:
                zeros += 1
                secs = data.get("eval_seconds")
                why = refusal_reason(data)
                # The whole tail is the evidence here, not just `is_solution_errors`: the bridge's
                # JSON line is truncated to 4,000 chars, which is why the reason is read by regex
                # in the first place, and the numba traceback lands in the `stderr_tail` inside it.
                evidence = " ".join(str(data.get(f) or "") for f in
                                    ("stdout_tail", "stderr_tail", "error_evidence"))
                bad.append({"node_id": data.get("node_id"), "eval_seconds": secs,
                            "violations": data.get("violations"), "reason": why,
                            "whose": compare_arms.whose_zero(why or "", evidence),
                            # The stopwatch stays as the FALLBACK, for a node whose stdout the
                            # record did not keep; a reason that was said outranks it either way.
                            "refusal": bool(why) if why else
                            (isinstance(secs, (int, float)) and secs < REFUSAL_SECONDS)})
        elif kind in ("error", "developer_crash", "build_interrupted"):
            errors += 1
    return {"spend": spend, "nodes": nodes, "zeros": zeros, "errors": errors, "bad": bad,
            "spend_at_last_node": spend_at_last_node}


def zero_sentence(z: dict) -> str:
    """The sentence an operator reads for one zero -- a function, so the WORDING is testable.

    §342 was a disagreement in wording, not in data: `pulse` and `compare_arms` classified the same
    node differently and only the printed sentence showed it. A test that reads the classification
    out of a dict would have passed through the whole defect.
    """
    # WHOSE ZERO IT IS. §342: the reason word alone said "harness declined" for a node whose own
    # `@njit` code would not compile -- the solver WAS the question and it lost. The partition lives
    # in `compare_arms`; a reason in neither half is called unclassified rather than filed under
    # whichever side reads more calmly.
    whose = z.get("whose")
    if whose == "candidate":
        return (f'the candidate EARNED this zero ({z["reason"]}: its own code would not build, '
                "import or validate) -- a real zero, and arm A pays for the same")
    if whose == "arena":
        return (f'RULER REFUSAL ({z["reason"]}) -- the harness declined, the solver was never '
                "the question")
    if z.get("reason"):
        return (f'zero with reason {z["reason"]}, which is in NEITHER half of compare_arms\' '
                "partition -- classify it before averaging it")
    # NO REASON AT ALL is the pre-§323 world: the record kept no bridge line, so the stopwatch is
    # all there is. It stays the fallback and says so.
    return ("RULER REFUSAL -- the harness declined, the solver was never the question"
            if z.get("refusal") else "the evaluation ran and came back invalid")


# NOT A LIVE SIGNAL, AND THE TOOL NEXT DOOR SAYS WHY (§347). The share of a probe's spend that has
# landed since its last evaluated node is a real number for a FINISHED probe -- point 9's waste --
# and it is not one for a running one. `probe_summary` records the reason in as many words: "for a
# RUNNING one it is just 'time since the last node', which grows until the next one lands and then
# collapses", which is why it marks the live figure with a `+`.
#
# Driven here, against myself. On 2026-09-08 remDL13 held 55.5 % with one node while the worst
# FINISHED probe on this box had ever held 47.4 %, and a line was added saying it was "still paying
# and no longer learning". Forty minutes later the probe evaluated its second node, scored 5.3676,
# and the same figure read 0.44 %. The alarm was the shape of the metric, not the state of the run.
#
# `spend_at_last_node` stays on the reading because it costs nothing and a finished probe's tail is
# computed from it. Nothing here judges a running probe by it.
def tail_after_the_last_node(got: dict) -> float | None:
    """Share of spend since the last evaluated node -- meaningful only once a probe has ENDED."""
    at = got.get("spend_at_last_node")
    spend = got.get("spend") or 0.0
    if at is None or spend <= 0:
        return None
    return 100.0 * (spend - at) / spend


def unscored_result(root: str, name: str, got: dict) -> str | None:
    """A probe that ENDED holding evaluated nodes and no `final.json` -- money spent, nothing scored.

    §351. `run_probe.sh` runs two steps after the engine: `extract_champion.py`, then a TEST
    evaluation into `final.json`. `resume_paused` (§338) restarts the ENGINE and nothing else, and
    the driver has long exited by then -- so a probe that pauses, is resumed and then finishes has
    no one left to run either step. remDL13 is the case: its driver wrote "чемпион: НЕТ" at
    06:21 and exited; two resumes carried it to $0.978 and a node scoring 5.3676 on TRAIN; and for
    eleven hours nothing said the result was never scored. The champion was still extractable for
    free -- node 1 with its Cython siblings -- and came back 5.1345 on TEST.

    Detection, not repair: the two commands are printed rather than run, because scoring occupies a
    22-cpu lane and that is the operator's call, not a monitor's.
    """
    if not got.get("nodes"):
        return None
    if glob.glob(f"{root}/{name}/final.json"):
        return None
    runs = sorted(glob.glob(f"{root}/{name}/runs/*/run"))
    if not runs:
        return None
    return (f'{got["nodes"]} evaluated node(s) and NO final.json -- the run ended but nobody '
            "extracted or scored its champion (a resume restarts the engine, not the driver). "
            "Recoverable without new spend:\n"
            f'        extract_champion.py --run-dir {runs[-1]} --all-files '
            f'--out {root}/{name}/champion_solver.py\n'
            f'        ALGOTUNE_EVAL_WORKERS=auto taskset -c <lane> looplab_eval.py --subset test '
            "  # the corpus is __w22x1r3; unset means 1 worker and a number nothing compares to")


def log_age(path: str, now: float | None = None) -> float:
    """Seconds since this log last grew -- with the clock read AFTER the stat, not before.

    §357. `pulse` sampled `time.time()` once at the top and used it for every probe's age. Between
    that sample and the stat it reads the meter's ledger (20 MB), walks `/proc` twice and globs the
    probe trees, so the age was understated by the whole preamble; for a probe writing continuously
    it went NEGATIVE and the table printed `-0s`.

    Harmless as printed, and not harmless as a rule: `age > STALL_TIMEOUT` is the only thing that
    calls a probe stalled, and an age that can be negative is an age that can be arbitrarily wrong
    in the direction of "fresh". A log dated in the FUTURE -- a clock step, a file copied off
    another box, an mtime preserved by `cp -p` -- would read as eternally fresh and never trip the
    ceiling. That is reported rather than clamped away: a negative age is a fact about the clock or
    the file, and both are worth saying out loud.

    An injected `now` is used as given, because a test owns its own clock.
    """
    stamp = os.path.getmtime(path)
    return (time.time() if now is None else now) - stamp


def format_age(age: float) -> str:
    """The age as the table prints it -- and a log dated ahead of the clock says so."""
    return "  AHEAD!" if age < -1.0 else f"{max(0.0, age):7.0f}s"


def wchan(pid) -> str:
    try:
        return open(f"/proc/{pid}/wchan", encoding="utf-8").read().strip() or "-"
    except OSError:
        return "gone"


PAUSED_WINDOW_S = 86_400.0     # a pause older than a day is history, not news


def readers_of_the_tree(bench: str, table=None) -> list:
    """Live processes running FROM this checkout -- the ones a `git merge` would edit underneath.

    §336. `run_probe.sh`'s own header records what this costs in bash: the file was edited while
    four probes ran through it, and `remEEctl1` died on `line 291: -c: command not found`, because
    bash reads a script by OFFSET as it executes. Python is not immune, only quieter: a run imports
    most of `looplab` at startup, but every lazy import after that reads whatever is on disk now, so
    a merge mid-run mixes two revisions inside one measurement and the events file says nothing.

    The rule was written for bash and nowhere for the tree. This is it, as a function: before
    editing the checkout, ask who is reading it.

    Matched on the interpreter's argv, not on the cwd: a probe is launched with `taskset … python -m
    looplab.cli`, its cwd is the bench root rather than the checkout, and the module path is what
    binds it to these files.
    """
    table = process_table() if table is None else table
    out = []
    for row in table:
        cmd = row.get("cmdline") or ""
        if "looplab.cli" not in cmd and f"{bench}/looplab" not in cmd:
            continue
        if "pulse.py" in cmd or "pytest" in cmd:
            continue                    # the sweep's own tools are not the measurement
        if any(k in cmd for k in ("run", "resume")):
            out.append(str(row.get("pid")))
    return sorted(set(out))


def phase_budget(events_path: str) -> tuple:
    """The phase a run is in and the seconds it was given, from its own last `agent_phase_started`.

    §339. An open call is not alarming by itself -- §335 exists because a long generation reads as
    silence -- but "17.8 minutes in flight" and "17.8 minutes of a 20-minute phase budget" are
    different sentences, and only the second one can be acted on. The budget is in the event the
    engine already writes; nothing new has to be measured.
    """
    label, budget = None, None
    try:
        for event in events_read.iter_events(events_path):
            if event.get("type") != "agent_phase_started":
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            label = data.get("label") or label
            got = data.get("time_budget_s")
            budget = float(got) if isinstance(got, (int, float)) else budget
    except OSError:
        return (None, None)
    return (label, budget)


def call_in_flight(pid, port: int = 8801, root: str = "/proc") -> bool:
    """Is a request open RIGHT NOW from this process to the meter?

    §335. `remDL13` sat for seven and a half minutes with its log and its last completed call the
    same age, no evaluation workers on its lane and nothing in state R -- which reads exactly like a
    wedge and was a long streamed generation. The ledger records a call when it FINISHES, so
    `call age` cannot see one in progress; an established socket can.

    Read from `/proc/<pid>/fd` (socket inodes) against `/proc/net/tcp` (state 01 = ESTABLISHED), so
    it costs two directory reads and no network. `ss` is not used on purpose: the sweep list records
    that it lies about this box.
    """
    try:
        fds = os.listdir(f"{root}/{pid}/fd")
    except OSError:
        return False
    inodes = set()
    for fd in fds:
        try:
            target = os.readlink(f"{root}/{pid}/fd/{fd}")
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    if not inodes:
        return False
    try:
        lines = open(f"{root}/net/tcp", encoding="utf-8").read().splitlines()[1:]
    except OSError:
        return False
    for line in lines:
        f = line.split()
        if len(f) < 10 or f[9] not in inodes or f[3] != "01":
            continue
        try:
            if int(f[2].split(":")[1], 16) == port:
                return True
        except (ValueError, IndexError):
            continue
    return False


def paused_probes(bench: str, now: float | None = None) -> list:
    """Probes that are PAUSED AND OWED WORK -- not running, not finished, and not out of money.

    §334. `remDL13` auto-paused when the provider went down mid-proposal: the Researcher got a 503,
    returned a degraded fallback, nothing was proposed and no node was built. `run_probe.sh`
    reported `rc=0` and "чемпион: НЕТ", and this tool said "no bench probe running" -- the same
    sentence it says about a probe that finished perfectly. Those are opposite dispositions: one is
    done, the other keeps $0.1114 of paid state and wants `looplab resume`, and re-launching it
    instead pays for that state twice (§213 measured what the reverse mistake costs).

    The disposition comes from `arm_fidelity._paused`, which decides it on the SPEND rather than on
    the word "pause" -- a run that stopped at its ceiling is complete however it was worded.

    Bounded to the last day: a pause from last week is history, and walking every probe tree on the
    box costs more than the answer is worth.
    """
    now = time.time() if now is None else now
    out = []
    for path in glob.glob(f"{bench}/model-probes/*/runs/*/run/events.jsonl"):
        name = path.split("/model-probes/")[1].split("/")[0]
        try:
            if now - os.path.getmtime(path) > PAUSED_WINDOW_S:
                continue
        except OSError:
            continue
        try:
            # NO SEPARATE `_run_finished` CHECK. It was here and a mutation deleting it stayed
            # green: `_paused` decides on the LAST lifecycle event, so a finished run is already
            # not paused. Keeping the line would have been an untested branch dressed as a guard --
            # the fixture for it can only be a state the engine does not produce.
            if not arm_fidelity._paused(f"{bench}/model-probes", name):
                continue
            spend = arm_fidelity._spend(f"{bench}/model-probes", name)
        except Exception:                       # noqa: BLE001 - a probe is not a reason to fail
            continue
        out.append({"probe": name, "spend": spend,
                    "age_s": now - os.path.getmtime(path)})
    return sorted(out, key=lambda r: r["probe"])


def timer_processes(table) -> dict:
    """The snapshot DAEMONS in a process table, told apart from the forks of a cycle in flight.

    Measured 2026-09-08: the sweep's own scan -- "every process whose cmdline holds
    `snapshot_timer.sh` and `_loop`" -- read **five** where there is one. Four were gone a second
    later: bash forks for a pipeline or a command substitution inherit the parent's cmdline, so a
    timer that is mid-cycle looks like five timers to a matcher that only reads `/proc/<pid>/cmdline`.
    A duplicate timer is a real hazard (two snapshots at once, which §313 drove: the loser exits 3
    having written nothing), and that is exactly why the count may not cry wolf every time a cycle
    happens to be running when the sweep looks.

    The discriminator is the PARENT. The daemon is started with `nohup ... &` and reparented to
    init; a fork of its cycle has the daemon as its parent. Neither is a matter of timing, and both
    are in the same table this reads once.

    `table` is a list of `{"pid", "ppid", "cmdline"}` so the rule can be tested against a fabricated
    process tree -- `/proc` cannot be arranged to hold two timers on demand.
    """
    daemons, forks = [], []
    for row in table:
        cmd = row.get("cmdline") or ""
        if "snapshot_timer.sh" not in cmd or "_loop" not in cmd:
            continue
        (daemons if str(row.get("ppid")) == "1" else forks).append(str(row.get("pid")))
    inside = {p for p in forks}
    return {"daemons": daemons, "forks": sorted(inside),
            "duplicate": len(daemons) > 1}


def process_table(root="/proc") -> list:
    """`/proc` as the list `timer_processes` reads. One pass, and a process that exits under us is
    skipped rather than raising -- the scan is a census, not a transaction."""
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for pid in names:
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"{root}/{pid}/cmdline", "rb").read().decode("utf-8", "replace")
            status = open(f"{root}/{pid}/status", encoding="utf-8").read()
            ppid = next(l.split()[1] for l in status.splitlines() if l.startswith("PPid:"))
        except (OSError, StopIteration, IndexError):
            continue
        out.append({"pid": pid, "ppid": ppid, "cmdline": cmd.replace("\0", " ")})
    return out


BENCH_SCRIPTS = ("run_probe.sh", "campaign.sh", "ruler_selfcheck.py", "looplab_eval.py",
                 "snapshot.sh")


def stray_bench_processes(table) -> list:
    """Bench work whose parent is gone AND which is nobody's parent -- a leftover, not a daemon.

    §332. The wide orphan scan the sweep ran by hand counted two on a healthy box, and both were
    MY OWN launcher shells: `bash -c source …/shell-snapshots/… && nohup run_probe.sh …`, reparented
    to init the moment the tool call returned, each holding the live probe as a child. Matching the
    script name ANYWHERE in a command line is the `pkill -f` mistake the sweep list warns about, one
    layer up: the launcher's argv mentions the script it launched.

    Two discriminators, and both are needed:

      * THE PROCESS ITSELF, not a mention. `argv[0]` or `argv[1]` is the script; a wrapper that
        passes it to `bash -c` has it further along.
      * NO CHILDREN. A deliberately detached daemon (`nohup … &`) is reparented to init too -- that
        is what `nohup` is for -- and the thing that tells it from a leftover is whether anything is
        still running under it. The 23 forkservers of §313 had none; the probe launcher has one.

    `table` is `{"pid", "ppid", "cmdline"}` rows so the rule can be tested against a fabricated
    tree; `/proc` cannot be asked to hold an abandoned campaign on demand.
    """
    children: dict = {}
    for row in table:
        children.setdefault(str(row.get("ppid")), []).append(str(row.get("pid")))
    out = []
    for row in table:
        if str(row.get("ppid")) != "1":
            continue
        argv = (row.get("cmdline") or "").split()
        head = argv[:2]
        if not any(any(name in part for name in BENCH_SCRIPTS) for part in head):
            continue
        if children.get(str(row.get("pid"))):
            continue                       # something is running under it: detached, not abandoned
        out.append(str(row.get("pid")))
    return sorted(out)


def orphans(bench: str) -> dict:
    """Bench workers still pinned to a lane whose parent is gone (ppid 1).

    THE HYGIENE CHECK LOOKS FOR STATE Z AND THESE ARE STATE S. Measured 2026-09-06 on a box the
    sweep had just called clean -- zero zombies, no probe running: 23 orphaned `multiprocessing`
    forkservers and resource trackers from runs four to six hours dead, holding 652 MiB and still
    carrying the affinity mask of the lane they were born on. Nothing reported them, because
    "zombie" was the only word the hygiene step knew.

    They cost more than the memory. `busy_cpus_outside_lane` counted them as occupancy, so the
    ruler's own contention field read 22 on an idle box and 22 under a full lane -- the same number
    for both answers. That is fixed at the counting end (§313); this is the other end, the one that
    says out loud that they are there.

    Only what belongs to the bench's own interpreter is counted. An orphan of the host's making is
    not this tool's business, and claiming it would make the number unactionable.
    """
    try:
        hz = os.sysconf("SC_CLK_TCK")
        boot = float(open("/proc/uptime", encoding="utf-8").read().split()[0])
    except (OSError, ValueError):
        return {"count": 0, "rss_mib": 0.0, "oldest_h": 0.0, "cpus": 0}
    mark = f"{bench}/AlgoTune/.venv/bin/python"
    seen: set[int] = set()
    count, rss, oldest = 0, 0.0, 0.0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace")
            if mark not in cmd:
                continue
            fields = open(f"/proc/{pid}/stat", encoding="utf-8").read().split(") ")[-1].split()
            if fields[1] != "1":                       # still has a parent: someone owns it
                continue
            aff = os.sched_getaffinity(int(pid))
            if not aff or len(aff) >= (os.cpu_count() or 0):
                continue                               # unpinned: not a lane worker
            rss += int(open(f"/proc/{pid}/statm", encoding="utf-8").read().split()[1]) * 4096 / 2 ** 20
            oldest = max(oldest, boot - int(fields[19]) / hz)
            seen |= aff
            count += 1
        except (OSError, ValueError, IndexError):
            continue
    return {"count": count, "rss_mib": round(rss), "oldest_h": round(oldest / 3600, 2),
            "cpus": len(seen)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", default=DEFAULT_BENCH)
    ap.add_argument("--root", default=None)
    ap.add_argument("--stall", type=float, default=2400.0)
    # THE LIST ASKS FOR TWO CLOCKS AND THIS TOOL WAS SHOWING ONE. Point 4 says to look at the age of
    # `events.jsonl` AND at the age of the last CALL in the ledger. They come apart, and the way
    # they come apart is the diagnosis: a fresh ledger beside a stale log is a probe that is calling
    # and producing nothing -- the retry storm §175 recorded, where three consecutive 504s at
    # exactly 300 s are the nginx ceiling rather than a hang. A stale ledger beside a fresh log is
    # the opposite and much rarer. Reading only the log cannot tell either from an idle probe.
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--now", type=float, default=None)
    # A PROBE THAT LEAVES THE LANES LOOKS EXACTLY LIKE ONE THAT FINISHED. `pulse` lists what is
    # RUNNING, so a probe killed by a stray signal or an OOM simply stops appearing -- and the only
    # reason I have ever caught that is `check_money`, which found FIVE such probes (capA1, capB1,
    # freeA1, freeB1, svcCacheCheck) as money in the meter with no tree on disk. The liveness tool
    # should not need the money tool to notice a missing probe. Name the batch and it will say, for
    # each absentee, whether it ENDED or VANISHED.
    ap.add_argument("--expect", nargs="*", default=[])
    # EXIT CODE, not prose: this is meant to stand in front of `git merge` in a shell line.
    ap.add_argument("--refuse-edits", action="store_true",
                    help="exit 3 if any run is reading this checkout (do not edit the tree)")
    args = ap.parse_args(argv)
    if args.refuse_edits:
        readers = readers_of_the_tree(args.bench)
        if readers:
            print(f"REFUSING: {len(readers)} run(s) are executing from this checkout "
                  f"(pid {', '.join(readers)}). A merge would swap files under a live measurement; "
                  "wait for them or use a separate worktree.")
            return 3
        print("no run is reading this checkout -- safe to edit")
        return 0

    root = args.root or f"{args.bench}/model-probes"
    now = args.now if args.now is not None else time.time()

    ledger = args.ledger or os.path.join(args.bench, "meter", "meter.jsonl")
    health = check_money.endpoint_health(ledger)
    newest = health["newest"]
    live = lanes.probes(args.bench)
    running = {r["probe"] for r in live if r["probe"]}
    # THE TIMER, ONCE, WITH ITS FORKS NAMED AS FORKS. Point 1 of the sweep asks whether the
    # snapshot timer is alive; a matcher on the cmdline alone answers "five" whenever a cycle is
    # running, and a DUPLICATE timer is a real thing to catch (two snapshots at once; §313 drove
    # the loser exiting 3 with nothing written).
    timers = timer_processes(process_table())
    if timers["duplicate"]:
        print(f'DUPLICATE snapshot timer: daemons {", ".join(timers["daemons"])} -- two timers mean '
              "two snapshots racing for one lock; kill all but one BY PID")
    elif timers["daemons"]:
        cycle = f', {len(timers["forks"])} fork(s) of a cycle in flight' if timers["forks"] else ""
        print(f'snapshot timer {timers["daemons"][0]} alive{cycle}')
    else:
        print("NO snapshot timer running")

    orph = orphans(args.bench)
    if orph["count"]:
        # PRINTED BEFORE THE EARLY RETURN, because an idle box is exactly when nothing else would
        # mention them and exactly when they are pure waste.
        print(f'{orph["count"]} orphaned bench worker(s) pinned across {orph["cpus"]} cpu(s), '
              f'{orph["rss_mib"]} MiB, oldest {orph["oldest_h"]} h -- parent gone (ppid 1), '
              f'state S so the zombie check does not see them')
    held = paused_probes(args.bench, now)
    for row in held:
        print(f'{row["probe"]} is PAUSED and owed work: ${row["spend"]:.4f} of paid state, idle '
              f'{row["age_s"] / 60:.0f} min -- `looplab resume`, do NOT relaunch (that pays twice)')
    if not live and not args.expect:
        print("no bench probe running" + (" (see the paused one(s) above)" if held else ""))
        return 0
    print(f'{"probe":10s} {"lane":12s} {"$":>8s} {"nodes":>5s} {"zeros":>5s} {"errs":>4s} '
          f'{"log age":>8s} {"call age":>9s}  wchan')
    stalled = 0
    for row in sorted(live, key=lambda r: r["probe"] or ""):
        name = row["probe"]
        found = sorted(glob.glob(f"{root}/{name}/runs/*/run/events.jsonl"))
        if not found:
            print(f'{name:10s} {lanes._fmt(row["cpus"]):12s}  no events.jsonl yet')
            continue
        got = pulse(found[0])
        # `args.now` only when it was INJECTED: otherwise the clock is read after the stat.
        age = log_age(found[0], args.now)
        called = newest.get(name)
        call_age = (now - called[0]) if called else None
        print(f'{name:10s} {lanes._fmt(row["cpus"]):12s} {got["spend"]:8.4f} {got["nodes"]:5d} '
              f'{got["zeros"]:5d} {got["errors"]:4d} {format_age(age)} '
              f'{(f"{call_age:8.0f}s" if call_age is not None else "       -"):>9s}  '
              f'{wchan(row["pid"])}')
        if called and called[1] != "200":
            # THE STREAK, NOT THE LAST SAMPLE. §333: this line said "last call came back 503" while
            # the state was 60 consecutive 503s over 22 minutes -- the provider's own pool down
            # (`litellm.ServiceUnavailableError: No available workers`). The list's rule for the
            # other wall is a rule about a streak too ("three consecutive 504s at exactly 300 s =
            # the nginx ceiling, not a hang"), and one sample cannot express either.
            # A STREAK OF ONE IS THE SINGLE SAMPLE, and saying "1 consecutive 401s over 0 min"
            # about it is worse prose for the commonest case -- a lone 401 between 200s (§122 saw
            # four, forty seconds apart, and they mattered). Two or more is a run, which is the
            # thing the streak was added to express.
            run = (health.get("streak") or {}).get(name)
            if run and run["count"] >= 2 and str(run["status"]) == str(called[1]):
                mins = (now - run["since"]) / 60.0
                print(f'      {run["count"]} consecutive {run["status"]}s over {mins:.0f} min '
                      "-- the endpoint is refusing, not this probe stalling")
            else:
                print(f'      last call came back {called[1]}, not 200 -- check the endpoint before '
                      "the probe")
        # A LONG GENERATION IS NOT SILENCE. The ledger records a call when it finishes, so a probe
        # waiting on one looks identical to a probe waiting on nothing -- §335 measured 7.5 minutes
        # of it, with no worker on the lane and nothing in state R.
        if call_age is not None and call_age > 240 and call_in_flight(row["pid"]):
            # AT MOST, NOT AT LEAST -- §339 corrects §335's own wording. The ledger's newest row
            # is the last COMPLETED call; the open one started after it, so `call_age` bounds the
            # open call's age from ABOVE. Saying "at least" turned an upper bound into a lower one
            # and made every long-quiet probe look worse than the evidence allows.
            label, budget = phase_budget(found[0])
            against = ""
            if budget:      # 0.0 means the phase was given no wall -- most of them are
                against = (f'; its {label or "phase"} has a {budget:.0f}s budget')
            print(f'      a call is OPEN to the meter now (the last one COMPLETED {call_age:.0f}s '
                  f'ago, so this one is younger than that{against}) -- a long generation in flight, '
                  "not silence: the ledger records a call when it ends")
        if call_age is not None and age > args.stall / 4 and call_age < age / 4:
            print(f'      CALLING BUT NOT PRODUCING: last call {call_age:.0f}s ago, log last grew '
                  f'{age:.0f}s ago. Three consecutive 504s at exactly 300 s are the nginx ceiling, '
                  "not a hang (§175); check the ledger's statuses before the process")
        for z in got["bad"]:
            # THE ZERO'S OWN SECONDS ARE THE DIAGNOSIS. A zero under five seconds means the harness
            # declined to measure -- a regime mismatch, an unloadable solver -- and blaming the
            # model for it sends the next hour in the wrong direction. All 12 corpus zeros are the
            # other kind: 41-47 s of real evaluation that came back invalid.
            # AND THE BRIDGE'S OWN NAME WHERE IT SAID ONE. The seconds are the fallback now, not
            # the diagnosis: `evaluator_timeout` is a refusal that costs the FULL timeout, so the
            # rule "a zero at 45 s is the solver's" gets that one exactly backwards.
            what = zero_sentence(z)
            print(f'      zero at node {z["node_id"]}: eval_seconds={z["eval_seconds"]}, '
                  f'violations={z["violations"]} -- {what}')
        if age > args.stall:
            stalled += 1
            print(f'      STALLED: {age:.0f}s since the log last grew, past the {args.stall:.0f}s '
                  "ceiling -- check the ledger's newest row and wchan before concluding it is hung")
    vanished = 0
    for name in sorted(set(args.expect) - running):
        got = arm_fidelity.probe_calls(root, name)
        found = sorted(glob.glob(f"{root}/{name}/runs/*/run/events.jsonl"))
        spend = pulse(found[0])["spend"] if found else 0.0
        if got["finished"]:
            print(f'{name:10s} {"(off the lanes)":12s} {spend:8.4f}      ended')
            unscored = unscored_result(root, name, pulse(found[0]) if found else {})
            if unscored:
                print(f"      {unscored}")
        elif got["paused"]:
            print(f'{name:10s} {"(off the lanes)":12s} {spend:8.4f}      PAUSED and owed work -- '
                  "resume it or the batch is short a probe")
            vanished += 1
        else:
            vanished += 1
            print(f'{name:10s} {"(off the lanes)":12s} {spend:8.4f}      VANISHED: no process, no '
                  "ending event, and not at its ceiling -- it did not finish, it stopped")
    return 1 if (stalled or vanished) else 0


if __name__ == "__main__":
    raise SystemExit(main())
