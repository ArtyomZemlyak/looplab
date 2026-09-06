#!/usr/bin/env python3
"""The standing sweep list's own factual claims, checked against the bench.

WHY. The list is the operator's instrument, and five of its readings are now false. Each sweep I
re-derive the same corrections by hand and report them again: `.baseline_times` holds nine entries
and not seven (§193 legitimately added two); the three probes it names as running finished days ago;
three snapshot items it marks "НЕ ПРОВЕРЕНО" are shipped; the `campaign.sh` item it marks
"ОСТАЁТСЯ ОТКРЫТЫМ" is closed by the prefix-check supersede (§267). §219's lesson applies to the
list itself: an instrument carrying false readings teaches its reader to discount the true ones.

ONE OF THEM IS NOT MERELY STALE, IT IS BACKWARDS. The money note says the abandoned `remDL` probe
($0.1292) must be ADDED to the sum of the live probes "иначе получишь ложное расхождение". Measured:
`remDL` HAS a tree on disk, 27 generation spans, $0.1292 exactly -- so its money is already in the
span sum, and following the instruction manufactures the very discrepancy it warns about.

WHAT THIS FILE IS NOT: a second source of truth. Every check below delegates to the tool that owns
the question -- `ruler_check` for the cache, `lanes` for what is running, the span files for money --
and reports what that tool says. If the list is edited, the wording here goes stale in turn, which is
why each claim carries the date of the wording it was written against.

Usage:
    sweep_claims.py [--bench DIR]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lanes  # noqa: E402
import ruler_check  # noqa: E402

WORDING_DATE = "2026-09-05"
DEFAULT_BENCH = "/var/tmp/looplab-bench"


def probe_span_cost(bench: str, name: str):
    """(has a tree, dollars of generation spans) for one probe."""
    found = glob.glob(f"{bench}/model-probes/{name}/runs/*/run/spans.jsonl")
    total = 0.0
    for path in found:
        for line in open(path, encoding="utf-8", errors="replace"):
            if not line.startswith("{"):
                continue
            try:
                span = json.loads(line)
            except ValueError:
                continue
            if span.get("name") != "generation":
                continue
            try:
                total += float((span.get("attributes") or {}).get("cost") or 0.0)
            except (TypeError, ValueError):
                pass
    return bool(found), total


def check_baseline_count(bench: str):
    """"В .baseline_times семь записей, все перемерены ЗДЕСЬ." """
    rows = ruler_check.entries(Path(bench) / "looplab" / "benchmarks" / "algotune" / ".baseline_times")
    regimes = {r["regime"] for r in rows if r["ok_name"]}
    ok = len(rows) == 7
    return ok, (f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}, "
                f"regime{'s' if len(regimes) != 1 else ''} {', '.join(sorted(regimes)) or '?'}"
                + ("" if ok else " -- the COUNT is not the invariant; one regime and a full set of "
                                "per-instance timings is (see ruler_check.py)"))


def check_abandoned_remdl(bench: str):
    """"счётчик считает и БРОШЕННУЮ пробу remDL ($0.1292) -- при сверке её надо прибавлять" """
    # THE CRITERION IS THE MONEY IN THE SPAN SUM, NOT THE DIRECTORY. The reconciliation compares
    # the meter against the sum of generation-span costs, so what matters is whether remDL's dollars
    # are IN that sum -- not whether a folder with its name exists. A tree that exists but carries
    # no billed span is money the sum does not have, and the note would be right about it. Keying on
    # the directory looked equivalent and is not; a mutation to the money test survived the first
    # version of these fixtures for exactly that reason.
    has_tree, cost = probe_span_cost(bench, "remDL")
    ok = cost == 0.0
    where = f"{'a' if has_tree else 'no'} tree on disk"
    return ok, (f"remDL has {where}, carrying ${cost:.4f} of generation spans"
                + (" -- none of its money is in the span sum, so the note holds" if ok else
                   " -- already inside the span sum, so adding it by hand MANUFACTURES the "
                   "discrepancy the note warns about"))


def check_named_probes_running(bench: str):
    """"ИДУТ ТРИ ПРОБЫ по $1: remEE, remDL2, remPde" """
    live = {r["probe"] for r in lanes.probes(bench) if r["probe"]}
    named = {"remEE", "remDL2", "remPde"}
    missing = sorted(named - live)
    ok = not missing
    return ok, ("all three are on their lanes" if ok else
                f"not running: {', '.join(missing)}; on the lanes now: "
                f"{', '.join(sorted(live)) or 'nothing'}")


def check_accee_test(bench: str):
    """"edge_expansion -- accEE ТЕСТ 224.4432" """
    import json
    path = Path(bench) / "model-probes" / "accEE" / "final.json"
    try:
        got = float(json.loads(path.read_text(encoding="utf-8"))["speedup"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return False, f"cannot read accEE's own final.json: {type(exc).__name__}"
    ok = abs(got - 224.4432) < 5e-4
    return ok, (f"accEE's own final.json records {got:.4f}"
                + ("" if ok else " -- §73.4 settled this on 2026-09-01: 224.4432 came from "
                                 "elsewhere, and the figure measured HERE, with its Cython kernel "
                                 "building, is the one in the file"))


CEILING_MARKS = ("CEILING ON HOW SLOW YOUR SOLVER MAY BE, PER INSTANCE",
                 "(1 + 5) * reference_time * 10")
# VERBATIM FROM THE SHIPPED CARD, not a paraphrase of it. The first version guessed
# `"best evaluated"` and `"the best EVALUATED node"`; the clause that shipped says
# `BEST **EVALUATED** ONE, NOT YOUR LAST`, so the checker went on reporting the card silent about a
# rule the card states -- a false reading inside the file whose whole job is catching false
# readings. `test_the_champion_marker_is_a_string_the_card_generator_actually_contains` pins it.
CHAMPION_MARKS = ("BEST **EVALUATED** ONE, NOT YOUR LAST",)


def _card_source(bench: str) -> str:
    return (Path(bench) / "looplab" / "benchmarks" / "algotune" / "make_task.py").read_text(
        encoding="utf-8", errors="replace")


def check_card_silent_on_instance_ceiling(bench: str):
    """"(а) карточка не говорит про потолок 10× эталона на инстанс" """
    try:
        src = _card_source(bench)
    except OSError as exc:
        return False, f"cannot read make_task.py: {type(exc).__name__}"
    found = [m for m in CEILING_MARKS if m in src]
    ok = not found
    # NAME WHAT ACTUALLY MATCHED. The first version said "including the worked form ..." while only
    # one of two markers had matched -- a sentence that claims more than the measurement, which is
    # the exact habit this whole file exists to catch.
    return ok, ("no per-instance ceiling text in the card generator" if ok else
                f"the card DOES state it; matched {found!r}. Read out of the generated card, the "
                "goal field carries the rule, the arithmetic and the consequence that a killed "
                "instance is INVALID rather than slow. Item (a) is shipped")


def check_card_silent_on_the_champion_rule(bench: str):
    """"(б) карточка не говорит, что лучший ОЦЕНЁННЫЙ узел сохраняется" """
    try:
        src = _card_source(bench)
    except OSError as exc:
        return False, f"cannot read make_task.py: {type(exc).__name__}"
    # The card's only `champion` sentence is about the held-out SPLIT, not about which node is
    # submitted. §84 measured the rule biting: eleven of seventeen multi-node probes ended on a node
    # that was not their best, none on a better one, paired sign test p = 1/2048.
    found = [m for m in CHAMPION_MARKS if m in src]
    ok = not found
    return ok, ("the card still does not say which node is submitted -- §84's rule, which the "
                "corpus shows biting in eleven of seventeen multi-node probes" if ok else
                f"the card states it: {found}")


SWEEP_CONSTANTS = {"pagerank": 1.0024, "pde_heat1d": 0.9958,
                   "edge_expansion": 0.9847, "discrete_log": 1.0162}
DRIFT_LOG = "looplab/benchmarks/algotune/ruler_selfcheck_log.jsonl"
DRIFT_TOLERANCE = 0.02      # 2 %; the measured disagreements are 5-11 %, so this is not a hair


def check_ruler_constants(bench: str):
    """"Эталон против себя ~1.0: pagerank 1.0024, pde_heat1d 0.9958, edge_expansion 0.9847,
    discrete_log 1.0162"

    Compared against the LATEST reading this box has recorded for each task, not against a fresh
    measurement -- taking one needs a 22-cpu bench lane (§262) and those are usually busy. §219 and
    its neighbours already recorded the disagreements; what was missing is anything that says so
    every sweep, while the list keeps presenting the four numbers as current.

    A task with no reading is reported as UNMEASURED rather than passed over: silence about a
    constant nobody has checked is how it stays quoted.
    """
    latest: dict = {}
    quiet: dict = {}
    pool: dict = {}
    try:
        for line in open(Path(bench) / DRIFT_LOG, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            task, med, stamp = row.get("task"), row.get("median"), str(row.get("stamp") or "")
            if not isinstance(med, (int, float)) or task not in SWEEP_CONSTANTS:
                continue
            busy = row.get("busy_cpus_outside_lane")
            if task not in latest or stamp > latest[task][1]:
                latest[task] = (float(med), stamp, busy)
            # AND THE MOST RECENT ONE TAKEN ON A QUIET BOX, kept separately. §313: `discrete_log`
            # read 0.9380 with three sibling lanes self-checking and 1.0274 alone two hours later.
            # Taking "the latest" without asking makes a busy afternoon look like a drifting ruler,
            # and this check is the thing that would have said so.
            if busy == 0 and (task not in quiet or stamp > quiet[task][1]):
                quiet[task] = (float(med), stamp, busy)
            # AND EVERY QUIET PER-REP VALUE, not only the sitting's median. §317: twelve quiet
            # readings of `pde_heat1d` -- a task whose p90/p10 is 1.4 and whose ruler was verified
            # fresh to 0.02 % -- run from 0.9898 to 1.0683. One reading carries +-4 %, which is
            # twice the tolerance it is judged against, so a single median generates false drift
            # on demand. It generated three predictions here, two of them refuted, before the
            # spread was measured instead of assumed.
            if busy == 0:
                pool.setdefault(task, []).extend(
                    float(v) for v in (row.get("values") or [med])
                    if isinstance(v, (int, float)))
    except OSError as exc:
        return False, f"cannot read the drift log: {type(exc).__name__}"

    said, off = [], 0
    for task, quoted in sorted(SWEEP_CONSTANTS.items()):
        if task not in latest:
            # WHY IT IS UNMEASURED, because the two reasons need different actions. The self-check
            # inlines the DELIVERED reference module, which only exists where a probe has staged
            # one: `ruler_selfcheck.build_solver` globs `*/ws/<task>/reference_<task>.py`.
            #
            # THE NAMES USED TO BE WRITTEN OUT HERE and they were wrong. This comment said the only
            # trees on the box were discrete_log, edge_expansion and pde_heat1d, so pagerank was
            # "UNCHECKABLE until a probe runs on that task" -- while `pgr1/ws/pagerank/` had been
            # sitting on disk the whole time, findable by the very glob one line up, along with
            # sixteen more under `_ruler/ws/`. A remembered list contradicted by the file system,
            # in the file whose subject is remembered numbers. pagerank was then read quietly:
            # 0.9994 against the quoted 1.0024. The reason is GLOBBED at call time and says how
            # many trees the glob found, so the next reader can see the evidence rather than a
            # sentence about it -- reporting the two reasons alike would send them to re-run a tool
            # that fails the same way every sweep.
            staged = glob.glob(f"{bench}/model-probes/*/ws/{task}/reference_{task}.py")
            elsewhere = len(glob.glob(f"{bench}/model-probes/*/ws/*/reference_*.py"))
            why = (f"no probe has staged its reference module here ({elsewhere} staged for other "
                   "tasks), so the self-check cannot build a solver for it -- stage one or run a "
                   "probe on this task"
                   if not staged else "no reading recorded yet")
            said.append(f"{task}: UNMEASURED here ({why}; list says {quoted:.4f})")
            off += 1
            continue
        # THE QUIET READING IS THE VERDICT WHERE ONE EXISTS. Not the newest: newest-wins is what
        # turned a 06:38 rebuild -- four lanes self-checking at once -- into a -7.7 % drift on
        # discrete_log and -5.2 % on pagerank, both of which came back inside 1.1 % alone.
        got, stamp, busy = quiet.get(task) or latest[task]
        under = "" if quiet.get(task) else (
            f", taken with {busy} cpu(s) busy outside its lane" if busy else
            ", taken before the box's load was recorded with the reading")
        # THE VERDICT IS THE POOLED QUIET READS AND THEIR OWN SCATTER, not one median against a
        # fixed 2 %. A constant is called moved only when it sits outside mean +- 2 standard errors
        # AND outside the tolerance: the first test is what stops a +-4 % instrument reporting a
        # 3 % drift every other sitting, the second is what stops a very tight instrument reporting
        # a difference too small to act on.
        vals = pool.get(task) or []
        n = len(vals)
        if n >= 2:
            mean = statistics.fmean(vals)
            sem = statistics.stdev(vals) / (n ** 0.5)
            delta = (mean - quoted) / quoted
            moved = abs(mean - quoted) > 2 * sem and abs(delta) > DRIFT_TOLERANCE
            off += 1 if moved else 0
            said.append(f"{task}: list {quoted:.4f}, {n} quiet read(s) mean {mean:.4f} "
                        f"+-{sem:.4f} ({100 * delta:+.1f} %){'  <-- ' if moved else ''}")
            continue
        delta = (got - quoted) / quoted
        mark = "" if abs(delta) <= DRIFT_TOLERANCE else "  <-- "
        if abs(delta) > DRIFT_TOLERANCE:
            off += 1
        said.append(f"{task}: list {quoted:.4f}, ONE reading {got:.4f} on {stamp[:10]}{under} "
                    f"({100 * delta:+.1f} %){mark}")
    return off == 0, "; ".join(said)


# The figures point 9 tells the operator to compare each finished probe against.
COMPARISON_FIGURES = {
    "edge_expansion": [224.4432],
    "pde_heat1d": [124.63, 99.00, 121.85],
    "discrete_log": [14.5186, 2.8369],
}


def _scores(bench: str, task: str) -> dict:
    """Every TEST score currently on this box for a task, by probe."""
    out = {}
    for path in glob.glob(f"{bench}/model-probes/*/final.json"):
        name = path.split("/model-probes/")[1].split("/")[0]
        try:
            rec = json.loads(open(path, encoding="utf-8").read())
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("subset") != "test":
            continue
        if not glob.glob(f"{bench}/model-probes/{name}/runs/{task}/run/events.jsonl"):
            continue
        val = rec.get("speedup")
        if isinstance(val, (int, float)):
            out[name] = float(val)
    return out


def check_comparison_figures(bench: str):
    """"ЧИСЛА ДЛЯ СРАВНЕНИЯ: edge_expansion 224.4432; pde_heat1d 124.63, 99.00, 121.85;
    discrete_log 14.5186 и 2.8369, разброс 5.1×"

    They are REAL and documented -- §68 and the tables around it -- and their probes are GONE. The
    2026-08-29 container crash took /var/tmp with about 69 runs in it, `dsDL` and `dsDL2` among
    them. So the figures are history, and point 9 reads as though they were the current corpus:
    measured 2026-09-06, not one of the five is within 0.005 of any probe now on this box, and
    `discrete_log`'s stated spread of 5.1x is 4.2x over the eleven probes that are.
    """
    said, missing = [], 0
    for task, figures in sorted(COMPARISON_FIGURES.items()):
        here = _scores(bench, task)
        for fig in figures:
            hit = [n for n, v in here.items() if abs(v - fig) < 5e-3]
            if hit:
                said.append(f"{task} {fig}: still here ({hit[0]})")
            else:
                missing += 1
                said.append(f"{task} {fig}: NOT among the {len(here)} probe(s) on this box")
    return missing == 0, "; ".join(said)


def check_campaign_evidence_overwrite(bench: str):
    """"campaign.sh делает rm -rf каталога задачи ... cp -ru перезаписывает доказательства первой
    попытки ... закрывается только версионированием архива по попыткам"

    DRIVEN, NOT READ. §267 closed this by driving the real `archive_tree`: archive 400 rows, do what
    `campaign.sh` does -- `rm -rf` the task root and write an EQUAL-LENGTH second attempt at the same
    path -- and attempt 1 came back intact as `.superseded-1`. The rule is a PREFIX check, "is the
    source a continuation of the archive", which is why an equal-length attempt 2 is caught where a
    size test would pass it.

    Re-driven every sweep rather than trusted: a source-grep would pass on a function whose
    behaviour had changed underneath its comment.
    """
    import shutil
    import subprocess
    import tempfile
    script = Path(bench) / "looplab" / "benchmarks" / "snapshot.sh"
    if not script.is_file():
        return False, "snapshot.sh not on this box, so the claim cannot be driven"
    work = tempfile.mkdtemp()
    try:
        src = Path(work) / "src" / "runs" / "demo"
        (src / "run").mkdir(parents=True)
        (src / "run" / "events.jsonl").write_text(
            "".join("attempt1 row %d\n" % i for i in range(400)), encoding="utf-8")
        arch = Path(work) / "arch"
        arch.mkdir()
        drive = Path(work) / "drive.sh"
        drive.write_text(
            "set -e\n"
            "sed -n '/^archive_tree() {/,/^}/p' \"$1\" > \"$2/at.sh\"\n"
            ". \"$2/at.sh\"\n"
            "archive_tree \"$3\" \"$4\" >/dev/null 2>&1 || true\n"
            "rm -rf \"$3\"; mkdir -p \"$3/run\"\n"
            "for i in $(seq 0 399); do echo \"attempt2 row $i\"; done > \"$3/run/events.jsonl\"\n"
            "archive_tree \"$3\" \"$4\" >/dev/null 2>&1 || true\n", encoding="utf-8")
        subprocess.run(["bash", str(drive), str(script), work, str(src), str(arch)],
                       check=False, capture_output=True, timeout=180)
        kept = arch / "demo" / "run" / "events.jsonl.superseded-1"
        if not kept.is_file():
            return True, "driven here: attempt 1's evidence was NOT preserved, so the note stands"
        first = kept.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(first) == 400 and first[0].startswith("attempt1"):
            return False, (f"driven here: attempt 1 survives as .superseded-1 with {len(first)} "
                           f"rows, first {first[0]!r} -- the per-attempt versioning the note asks "
                           "for is in place (§267), keyed on a PREFIX check so an equal-length "
                           "second attempt is caught too")
        return True, "driven here: .superseded-1 exists but does not hold attempt 1 intact"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check_snapshot_refusals(bench: str):
    """"НЕ ПРОВЕРЕНО мной: снимок с исчезнувшим каталогом назначения; два снимка одновременно"

    DRIVEN, NOT READ, and driven against the real `snapshot.sh` rather than a copy of its logic.

    Both refusals are the same failure in different clothes: NOTHING WRITTEN reported as success.
    The one this box actually suffered on 2026-08-29 was an empty backup under exit 0, so what is
    checked here is the EXIT CODE and the empty destination, not the wording.

      * destination not a mounted store -> exit 1 ("this is not a skip; it is a failed snapshot")
      * another snapshot holds the lock  -> exit 3, so the timer retries instead of recording a
        fingerprint, and an operator can tell "busy" from "broken"

    The lock arm holds the lock itself and sets SNAPSHOT_LOCK_WAIT_S, so it never starts a real
    snapshot: a drive that let the second run WIN the lock would copy 112 MB of bundles into a
    temporary directory, which is how this check was first written and why the wait is a variable.
    """
    import shutil
    import subprocess
    import tempfile
    script = Path(bench) / "looplab" / "benchmarks" / "snapshot.sh"
    if not script.is_file():
        return False, "snapshot.sh not on this box, so the claim cannot be driven"
    work = tempfile.mkdtemp()
    try:
        store = Path(work) / "store"
        (store / "snaps").mkdir(parents=True)
        # The sentinel is what tells snapshot.sh this is the persistent store and not some path
        # that happens to exist; without it the FIRST arm's refusal would be indistinguishable
        # from the SECOND's, and both would pass for the wrong reason.
        (store / ".persistent-store-id").write_text("drive\n", encoding="utf-8")
        (store / "snaps" / ".persistent-store-id").write_text("drive\n", encoding="utf-8")

        gone = subprocess.run(
            ["bash", str(script)],
            env={**os.environ, "SNAPSHOT_DEST": "/proc/no-such-mount/snaps"},
            check=False, capture_output=True, text=True, timeout=180)

        lockfile = store / "snaps" / ".snapshot.lock"
        lockfile.touch()
        holder = subprocess.Popen(
            ["bash", "-c", 'exec 9>"$1"; flock 9; sleep 120', "_", str(lockfile)])
        try:
            busy = subprocess.run(
                ["bash", str(script)],
                env={**os.environ, "SNAPSHOT_DEST": str(store / "snaps"),
                     "SNAPSHOT_LOCK_WAIT_S": "3"},
                check=False, capture_output=True, text=True, timeout=180)
        finally:
            holder.kill()
            holder.wait(timeout=30)

        wrote = sorted(d.name for d in (store / "snaps").iterdir() if d.is_dir())
        bad = []
        if gone.returncode == 0:
            bad.append(f"a vanished destination exited {gone.returncode}")
        if busy.returncode != 3:
            bad.append(f"a held lock exited {busy.returncode}, not 3")
        if wrote:
            bad.append(f"the refused run still wrote {wrote}")
        if bad:
            return True, "driven here: " + "; ".join(bad)
        return False, (f"driven here: vanished destination -> exit {gone.returncode} "
                       f"({gone.stderr.strip().splitlines()[0][:60] if gone.stderr.strip() else ''}"
                       "...), held lock -> exit 3 with an empty destination; neither refusal can "
                       "report success over nothing written")
    finally:
        shutil.rmtree(work, ignore_errors=True)


CLAIMS = [
    ("point 5: seven entries in .baseline_times", check_baseline_count),
    ("point 3: add the abandoned remDL $0.1292 when reconciling", check_abandoned_remdl),
    ("state: remEE, remDL2 and remPde are running", check_named_probes_running),
    ("point 9: edge_expansion comparison figure accEE TEST 224.4432", check_accee_test),
    ("point 8(a): the card does not mention the 10x per-instance ceiling",
     check_card_silent_on_instance_ceiling),
    ("point 8(b): the card does not say the best EVALUATED node is kept",
     check_card_silent_on_the_champion_rule),
    ("point 5: the reference-against-itself constants", check_ruler_constants),
    ("point 9: the comparison figures are the current corpus", check_comparison_figures),
    ("point 8: campaign.sh's rm -rf still overwrites the first attempt's evidence",
     check_campaign_evidence_overwrite),
    ("point 8: NOT CHECKED -- a snapshot whose destination vanished, and two at once",
     check_snapshot_refusals),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", default=DEFAULT_BENCH)
    args = ap.parse_args(argv)
    print(f"the standing sweep list as worded on {WORDING_DATE}, checked against the bench")
    stale = 0
    for claim, check in CLAIMS:
        try:
            ok, detail = check(args.bench)
        except Exception as exc:                       # noqa: BLE001 - a broken check is not a verdict
            print(f"  UNCHECKABLE  {claim}\n               {type(exc).__name__}: {exc}")
            continue
        if not ok:
            stale += 1
        print(f'  {"HOLDS" if ok else "STALE":>11s}  {claim}\n               {detail}')
    print(f"  {stale} of {len(CLAIMS)} checked claim(s) no longer hold")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
