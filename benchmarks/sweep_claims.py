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
import datetime
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


def _n_values(row) -> int:
    """Readings on a row, not rows. One sitting carries four reps, so counting sittings printed
    "1 of them INFERRED" beside "4 quiet wide read(s)" -- one doubtful in four, when all four were
    the same untagged sitting. The unit counted has to be the unit the sentence beside it names."""
    vals = row.get("values")
    return len([v for v in vals if isinstance(v, (int, float))]) if isinstance(vals, list) else 1


def _first_trace_of(bench: str, regime: str) -> str:
    """When `regime` first showed up on this box, as an ISO stamp, or a stamp past every reading."""
    seen = []
    for path in glob.glob(f"{bench}/looplab/benchmarks/algotune/.baseline_times/*__{regime}.json"):
        try:
            seen.append(datetime.datetime.fromtimestamp(os.path.getmtime(path)).isoformat()[:19])
        except OSError:
            continue
    try:
        for line in open(Path(bench) / DRIFT_LOG, encoding="utf-8", errors="replace"):
            if f'"{regime}"' in line:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("regime") == regime and row.get("stamp"):
                    seen.append(str(row["stamp"])[:19])
    except OSError:
        pass
    return min(seen) if seen else "9999"


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
    inferred: dict = {}
    unattributed: dict = {}
    # THE EARLIEST EVIDENCE THAT THE SERIAL REGIME EXISTED HERE, from two independent places: a
    # cache file's mtime and the log's own first regime-tagged row. The earlier of the two is the
    # cutoff, so a copied file or a late-added field can only make the rule STRICTER, never let an
    # unattributable reading through. With no trace at all, the wide regime was the only one there
    # was and every undated row is wide.
    serial_first = _first_trace_of(bench, ruler_check.SERIAL_REGIME)
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
            # ATTRIBUTED FIRST, THEN USED -- for every route, not only the pool. §340's first cut
            # filtered the pool and left `latest`/`quiet` regime-blind, so a dropped reading walked
            # back in through the "ONE reading" fallback that runs when the pool holds fewer than
            # two: the value was refused as evidence for a band and then judged the constant alone.
            reg = row.get("regime")
            if not reg and stamp and stamp[:19] < serial_first:
                reg = ruler_check.CAMPAIGN_REGIME
                if busy == 0:
                    inferred[task] = inferred.get(task, 0) + _n_values(row)
            if not reg:
                if busy == 0:
                    unattributed[task] = unattributed.get(task, 0) + _n_values(row)
                continue
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
                # POOLED PER REGIME. §317 pooled every quiet value together, and the hour both
                # regimes existed for these four tasks that blend changed the verdict: pde_heat1d's
                # eight wide reads (mean 1.0331) and four serial ones (0.9865) came out as twelve
                # reads meaning 1.0177, a number measured nowhere. The same mixing §314 forbade,
                # reintroduced by the fix for a different mistake, in the file about that mistake.
                #
                # A ROW THAT DOES NOT NAME ITS REGIME IS DATED, NOT ASSUMED (§340). `or
                # CAMPAIGN_REGIME` filed every pre-§329 reading under the wide regime silently, and
                # the line it produced was the confident-looking one: `discrete_log: 4 quiet wide
                # read(s) mean 1.0192 +-0.0034` was FOUR INFERRED READS AND ZERO RECORDED ONES, as
                # was edge_expansion's. On pde_heat1d the mixing moved the verdict (1.0331 +-0.0068
                # on 8 against 1.0427 +-0.0094 on 4) and halved the standard error -- tightening a
                # band with evidence that was never taken. Attribution now needs a fact: the row
                # must predate every trace of the serial regime on this box, which is when the wide
                # one was the only cache a self-check COULD have read. Anything later is dropped
                # and counted, because there is nothing on the row to attribute it by.
                pool.setdefault((task, reg), []).extend(
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
            # AND THE THIRD REASON, which the first two hid. A task can have readings and still be
            # unmeasured here: if every one of them was taken without recording a regime, after
            # both regimes existed, there is nothing to attribute them by. Reporting that as "no
            # probe has staged its reference module" sends the reader to stage a tree that is
            # already there, and the readings stay unexplained.
            why = (f"{unattributed[task]} reading(s) recorded but NONE attributable: no regime on "
                   f"the row and both regimes existed by then -- re-read it with the regime named"
                   if unattributed.get(task) else
                   f"no probe has staged its reference module here ({elsewhere} staged for other "
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
        vals = pool.get((task, ruler_check.CAMPAIGN_REGIME)) or []
        n = len(vals)
        if n >= 2:
            mean = statistics.fmean(vals)
            sem = statistics.stdev(vals) / (n ** 0.5)
            delta = (mean - quoted) / quoted
            moved = abs(mean - quoted) > 2 * sem and abs(delta) > DRIFT_TOLERANCE
            off += 1 if moved else 0
            # AND THE OTHER REGIME BESIDE IT, because it is now measured and it is not the same
            # number: -4.7 % on pde_heat1d, -2.4 % on discrete_log, +0.3 % on edge_expansion and
            # pagerank. The gap is a property of the task, not a constant of the box, so it has to
            # be shown rather than assumed either way.
            other = pool.get((task, ruler_check.SERIAL_REGIME)) or []
            beside = (f"; {len(other)} serial read(s) mean {statistics.fmean(other):.4f} "
                      f"({100 * (statistics.fmean(other) - mean) / mean:+.1f} % vs wide)"
                      if len(other) >= 2 else "")
            # HOW MANY OF THEM ARE INFERRED, said out loud. Four of four on two of these tasks:
            # a reader who sees `4 quiet wide read(s)` and a tight error bar has no way to tell
            # that number came from rows that never named a regime.
            how = ""
            if inferred.get(task):
                how = f" ({inferred[task]} of them INFERRED, taken before {serial_first[:16]})"
            if unattributed.get(task):
                how += (f" [{unattributed[task]} later reading(s) DROPPED: no regime recorded and "
                        "both regimes existed by then]")
            said.append(f"{task}: list {quoted:.4f}, {n} quiet wide read(s) mean {mean:.4f} "
                        f"+-{sem:.4f} ({100 * delta:+.1f} %){how}{beside}"
                        f"{'  <-- ' if moved else ''}")
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


def check_denominator_composition(bench: str):
    """"КАЖДУЮ ЗАКОНЧИВШУЮСЯ ПРОБУ РАЗБИРАЙ" -- a speedup divides by "the reference's time", and
    between a third and a half of that number is not the reference solving anything.

    Measured 2026-09-07 at each dataset's own instance size, under the bench interpreter (§299),
    against the cached per-instance median every score on this box divides by:

        pde_heat1d      146.5 ms cached   75.4 ms solving   49 % harness
        pagerank        109.2 ms cached   60.3 ms solving   45 % harness
        discrete_log      2.18 ms cached   1.23 ms solving   43 % harness
        edge_expansion   45.4 ms cached   30.4 ms solving   33 % harness

    THE SHARE IS WHAT MATTERS, AND IT IS NEARLY THE SAME FROM 2 ms TO 146 ms. A FIXED per-instance
    cost would be almost all of a 2 ms number and a rounding error in a 146 ms one; this is
    proportional, so it divides out of a speedup instead of compressing it. That is not an argument,
    it is bounded by a score already on the box: `remEE8` reads 276.7268 on `edge_expansion`, so its
    whole measured per-instance time is 45.4/276.7 = 0.164 ms, and a fixed part of the reference's
    15.0 ms of overhead cannot exceed that -- at most 1.1 % of it is fixed.

    Driven from the recorded readings (`cached_ms`, `solver_ms`) and the probes' own `final.json`,
    so it costs no timing run; a task whose reading predates those fields is reported as unmeasured
    rather than passed over.
    """
    have = {}
    try:
        for line in open(Path(bench) / DRIFT_LOG, encoding="utf-8", errors="replace"):
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            cached, solver = row.get("cached_ms"), row.get("solver_ms")
            if isinstance(cached, (int, float)) and isinstance(solver, (int, float)) \
                    and 0 < solver < cached:
                have[row.get("task")] = (float(cached), float(solver), str(row.get("stamp") or ""))
    except OSError as exc:
        return False, f"cannot read the drift log: {type(exc).__name__}"
    if not have:
        return False, ("no reading records both halves of the denominator yet -- run "
                       "ruler_selfcheck --record once per task")
    said, worst = [], 0.0
    for task, (cached, solver, _stamp) in sorted(have.items()):
        share = 100 * (cached - solver) / cached
        best = max((v for v in _scores(bench, task).values()), default=None)
        if best and best > 1:
            fixed = 100 * (cached / best) / (cached - solver)
            worst = max(worst, fixed)
            # A BOUND IS NOT A MEASUREMENT, and the mark says which one this is. The bound comes
            # from the best score on the box -- a candidate's whole measured time cannot be less
            # than a fixed cost every instance pays -- so a task nobody has beaten badly leaves the
            # question open rather than answered. discrete_log's best is 16.8, which bounds nothing
            # useful; edge_expansion's 276.7 bounds it at 1 %.
            said.append(f"{task}: {share:.0f} % harness, and a score of {best:.1f} bounds the FIXED "
                        f"part at {fixed:.1f} % of it{'  <-- not bounded below 10 %' if fixed >= 10 else ''}")
        else:
            said.append(f"{task}: {share:.0f} % harness (no score here to bound the fixed part)")
    # THE BOOLEAN ANSWERS THE LIST'S CLAIM, which is that a speedup divides by the reference's time.
    # It does not: between a third and a half of the denominator is harness. Reusing this boolean
    # for the second question -- is that overhead fixed or proportional -- would let one answer hide
    # the other, so the second lives in the detail with its own mark.
    return False, "; ".join(said)


def check_money_cue_reaches_the_choosers(bench: str):
    """"денежная подсказка не доходит до plan/foresight_rank/hyp_prioritize"

    DRIVEN on the newest probes with `cue_reach`, which resolves chained prompts -- reading
    `attributes.input` directly reports a phase as blind whenever `input_from` is set, and that
    error has been made by hand twice (31.7 % where the truth was 99.3 %).

    Measured 2026-09-08 over the three newest probe trees:

        plan_step 100 %   propose 90 %   deep_research 90 %   plan 100 %   repropose 88 %
        foresight_rank 0 % (2.1 % of spend)   hyp_prioritize 0 %

    So the claim is two-thirds stale: `plan` was closed on 2026-08-31 through the Developer's own
    note (`repo_developer.py::_propose_plan`) and reads 100 %, and `propose`/`repropose` -- which
    the same list once called blind -- are near ninety. What remains blind is the foresight panel,
    and that is a RECORDED DECISION rather than an oversight (`engine/proposal_cues.py`): a ranker
    choosing between candidates it did not generate has no cheaper option to switch to, so the
    sentence would cost tokens on every call and change nothing. Its own revisit threshold is "a
    few per cent", which is why this check reports the SHARE and not just the reach.
    """
    import subprocess
    tool = Path(bench) / "looplab" / "benchmarks" / "cue_reach.py"
    roots = sorted(glob.glob(f"{bench}/model-probes/*/runs"), key=os.path.getmtime, reverse=True)
    roots = [str(Path(r).parent) for r in roots if "/_ruler/" not in r][:3]
    if not tool.is_file() or not roots:
        return False, "cue_reach.py or a probe tree is missing, so the claim cannot be driven"
    # `--json`, not the columns: §289 measured what parsing this kind of table by eye costs.
    got = subprocess.run([sys.executable, str(tool), "--json", *roots],
                         capture_output=True, text=True, timeout=600)
    try:
        table = json.loads(got.stdout.strip().splitlines()[-1])
        rows = {r["phase"]: (r["reach_pct"], r["share_pct"]) for r in table["phases"]}
    except (ValueError, IndexError, KeyError, TypeError):
        return False, f"cue_reach produced no table ({got.stdout[-160:]!r})"
    if not rows:
        return False, "cue_reach found no spans in the newest probes"
    said, still_blind = [], []
    for phase in ("plan", "propose", "repropose", "foresight_rank", "hyp_prioritize"):
        if phase not in rows:
            said.append(f"{phase}: no spans in these probes")
            continue
        reach, share = rows[phase]
        said.append(f"{phase} {reach:.0f} % of spans, {share:.1f} % of spend")
        if reach == 0.0:
            still_blind.append((phase, share))
    # The claim HOLDS only if all three named phases are still blind. `plan` alone refutes it.
    named = {"plan", "foresight_rank", "hyp_prioritize"}
    blind_named = {p for p, _ in still_blind} & named
    detail = "; ".join(said)
    if blind_named:
        over = [f"{p} at {sh:.1f} %" for p, sh in still_blind if p in named and sh >= 3.0]
        detail += ("; STILL BLIND: " + ", ".join(sorted(blind_named))
                   + (" -- and past its own 'a few per cent' revisit line: " + ", ".join(over)
                      if over else " -- a recorded decision, both under 3 % of spend"))
    return blind_named == named, detail


# The median share of a probe's spend that lands BEFORE its first evaluated node, per task, measured
# 2026-09-08 over 141 probes. Pinned for §330's reason: a band computed from the probes it judges
# cannot be failed by them. `pde_heat1d` sits where it does because four of the corpus's five worst
# probes are on it -- 78.9 to 90.6 % -- and that is a fact about the task, not an accident of one run.
BEFORE_FIRST_NODE_BANDS = {
    "edge_expansion": (20.0, 45.0),
    "discrete_log": (15.0, 55.0),
    "pde_heat1d": (40.0, 95.0),
    "pagerank": (20.0, 60.0),
}


def check_waste_after_the_last_node(bench: str):
    """"$3.6067 of $100.2691 corpus spend (3.6 %) lands AFTER the last evaluated node ... 16 of 69
    runs end holding one" (`engine/proposal_cues.py`)

    That figure is the one the money cue was meant to shrink, and it has been quoted from a
    docstring ever since. Driven here from `probe_summary --json`, which already computes the
    per-probe share (§72: it is only legible beside the spend BEFORE the first node -- `remPde` read
    11 % on this metric while having spent 91 % before its first).

    Measured 2026-09-08 over 141 probes with a node: **$8.9020 of $142.5275 = 6.2 %**, median per
    probe 2.0 %, and **82 of 141 end holding an unfinished draw** -- against 3.6 % and 16 of 69. The
    corpus grew and the share grew with it.

    AND THERE IS NO CONTROL GROUP LEFT. Every probe on this box now carries the money cue in
    `propose` (`cue_reach`: 141 of 141), so this cannot say whether the cue helped -- only that the
    waste is still there with it everywhere. A split that pretended otherwise would be the fixture
    agreeing with the hope.
    """
    import subprocess
    tool = Path(bench) / "looplab" / "benchmarks" / "probe_summary.py"
    if not tool.is_file():
        return False, "probe_summary.py is not on this box, so the claim cannot be driven"
    got = subprocess.run([sys.executable, str(tool), "--json"], capture_output=True, text=True,
                         timeout=900)
    try:
        rows = json.loads(got.stdout)
    except ValueError:
        return False, f"probe_summary produced no json ({got.stdout[-160:]!r})"
    with_node = [r for r in rows if r.get("reached_a_node") and isinstance(r.get("after_pct"),
                                                                          (int, float))]
    if not with_node:
        return False, "no probe on this box reached an evaluated node"
    # THE KEY IS `spent`, and guessing it cost a run: `r.get("spend") or r.get("usd")` summed to
    # $0.0000 and the check reported "0.0 % of $0.0000" without noticing it had no money at all.
    # A denominator of zero is not a measurement, so it is refused below rather than divided by.
    total = sum(float(r.get("spent") or 0.0) for r in with_node)
    # `after_pct` is a share of the probe's own spend; the corpus share needs the money back.
    after = sum(float(r["after_pct"]) / 100.0 * float(r.get("spent") or 0.0) for r in with_node)
    holding = sum(1 for r in with_node if float(r["after_pct"]) > 1.0)
    shares = sorted(float(r["after_pct"]) for r in with_node)
    median = shares[len(shares) // 2]
    if total <= 0:
        return False, "probe_summary reported no spend at all -- the money key changed name"
    corpus = 100.0 * after / total
    detail = (f"{len(with_node)} probe(s) with a node: {corpus:.1f} % of ${total:.4f} lands after "
              f"the last evaluated node (median per probe {median:.1f} %), {holding} end holding an "
              f"unfinished draw -- the quoted figures are 3.6 % and 16 of 69")
    holds = abs(corpus - 3.6) <= 0.5 and holding == 16
    return holds, detail


def check_reference_use_band(bench: str):
    """"База обращений к референсу — 4.9-8.3 % (§69.1), НЕ 3.0 %"

    Driven from `probe_summary --json` over every probe with a `run_probe` span. Measured
    2026-09-08 across 142 probes:

        ref_pct       median 8.3 %   p25 5.3   p75 12.5   max 30.0
        ref_call_pct  median 8.3 %   p25 5.4   p75 12.1   max 38.9

    The list is right that 3.0 % is wrong, and the band it offers instead is the OLD corpus's: only
    **30 of 142** probes fall inside 4.9-8.3 %, and today's spread runs from a quarter below it to
    half again above.

    AND THE QUESTION HAS TWO ANSWERS PER PROBE. `ref_imports`/`ref_calls` count occurrences anywhere
    in the run's text -- all 142 probes have at least one -- while `ref_pct` is the share of the
    model's own `run_probe` spans that carry one. **20 probes** import the reference in code they
    wrote and never touch it from a probe, so "does the model use the reference" answers yes or no
    depending on which of the two a reader picks up. Both are printed here for that reason.
    """
    import subprocess
    tool = Path(bench) / "looplab" / "benchmarks" / "probe_summary.py"
    if not tool.is_file():
        return False, "probe_summary.py is not on this box, so the claim cannot be driven"
    got = subprocess.run([sys.executable, str(tool), "--json"], capture_output=True, text=True,
                         timeout=900)
    try:
        rows = json.loads(got.stdout)
    except ValueError:
        return False, f"probe_summary produced no json ({got.stdout[-160:]!r})"
    have = [r for r in rows if isinstance(r.get("ref_pct"), (int, float))]
    if not have:
        return False, "no probe on this box has a run_probe span to measure"
    pcts = sorted(float(r["ref_pct"]) for r in have)
    med = pcts[len(pcts) // 2]
    lo, hi = pcts[len(pcts) // 4], pcts[3 * len(pcts) // 4]
    inside = sum(1 for x in pcts if 4.9 <= x <= 8.3)
    split = sum(1 for r in have
                if (r.get("ref_imports") or 0) > 0 and float(r["ref_pct"]) == 0.0)
    detail = (f"{len(have)} probe(s): reference reached in {med:.1f} % of run_probe spans "
              f"(p25 {lo:.1f}, p75 {hi:.1f}); {inside} of {len(have)} inside the quoted 4.9-8.3 %; "
              f"{split} import it in written code and never from a probe, so the question has two "
              "answers per probe")
    # The band HOLDS only if it describes the corpus -- most of it inside, not a third.
    return inside >= 0.5 * len(have), detail


# TEST divided by the best TRAIN, per task, measured 2026-09-08 over the 141 probes that have both.
# Pinned rather than recomputed: a band derived from the probes it judges cannot be failed by them.
#   (low, high, probes it was measured over)
TEST_TRAIN_BANDS = {
    "edge_expansion": (0.892, 1.033, 118),
    "discrete_log": (0.890, 1.260, 11),     # its cached times run p90/p10 = 276; the width is the tail
    "pde_heat1d": (0.991, 1.054, 10),
    # pagerank is NOT here on purpose. Its one probe reads x0.993, and pinning that as a band
    # flagged that very probe on the next run -- 0.99296 is outside 0.993-0.993 by rounding alone.
    # One measurement is a point; a band needs at least two, and saying so is cheaper than
    # inventing a tolerance nobody measured.
}

MIN_PROBES_FOR_A_BAND = 2


def check_test_tracks_train(bench: str):
    """"тест против train" -- point 9 asks for the pair per probe; this is what the pair DOES.

    Every node is evaluated on TRAIN and the champion is scored once on TEST (§84), so the two are
    different measurements on different instance sets. Measured 2026-09-08 over the 141 probes that
    have both:

        task              probes   median   min     max
        edge_expansion      118     0.995   0.892   1.033
        discrete_log         11     1.001   0.890   1.260
        pde_heat1d           10     1.022   0.991   1.054
        pagerank              1     0.993   0.993   0.993

    TEST tracks the best TRAIN to about a per cent, and the SPREAD IS THE TASK'S OWN TAIL:
    `discrete_log`, whose cached per-instance times run p90/p10 = 276, swings from 0.890 to 1.260 --
    which is the uncertainty riding on the number the list calls the corpus's finest
    (14.5186 against 2.8369). `edge_expansion`, tail 1.2, holds inside 11 %.

    The verdict is per TASK, not global: a probe outside its own task's measured band is the thing
    worth reading, and a global band would hide `discrete_log`'s width behind `edge_expansion`'s 118
    probes.
    """
    import subprocess
    import collections
    tool = Path(bench) / "looplab" / "benchmarks" / "probe_summary.py"
    if not tool.is_file():
        return False, "probe_summary.py is not on this box, so the pair cannot be driven"
    got = subprocess.run([sys.executable, str(tool), "--json"], capture_output=True, text=True,
                         timeout=900)
    try:
        rows = json.loads(got.stdout)
    except ValueError:
        return False, f"probe_summary produced no json ({got.stdout[-160:]!r})"
    pairs = [(r.get("probe"), r.get("task") or "?", max(r["nodes"]), float(r["test"]))
             for r in rows
             if isinstance(r.get("test"), (int, float)) and r.get("nodes") and max(r["nodes"]) > 0]
    if not pairs:
        return False, "no probe on this box has both a TEST score and an evaluated node"
    by_task = collections.defaultdict(list)
    for _probe, task, best, test in pairs:
        by_task[task].append(test / best)
    said = []
    for task, ratios in sorted(by_task.items(), key=lambda kv: -len(kv[1])):
        r = sorted(ratios)
        said.append(f"{task} x{r[len(r) // 2]:.3f} ({min(r):.3f}-{max(r):.3f}, n={len(r)})")
    # AGAINST A PINNED BAND, NOT AGAINST ITSELF. The first cut derived each task's band from the
    # same probes it then judged, so nothing could ever fall outside it and the check reported
    # HOLDS on its own tautology -- the shape this file exists to catch, written into this file.
    # The bands below are the measurement of 2026-09-08; a future probe outside one is the thing
    # worth reading, and a band that has visibly moved is worth re-pinning WITH a line saying why.
    loud, unpinned, thin = [], [], set()
    for probe, task, best, test in pairs:
        band = TEST_TRAIN_BANDS.get(task)
        if band is None:
            if len(by_task[task]) < MIN_PROBES_FOR_A_BAND:
                thin.add(task)          # one probe is a point, not a band
            else:
                unpinned.append(task)
            continue
        lo, hi, _n = band
        if not lo <= test / best <= hi:
            loud.append(f"{probe} on {task} x{test / best:.3f} outside {lo:.3f}-{hi:.3f}")
    detail = "; ".join(said)
    if loud:
        detail += "; OUTSIDE the pinned band: " + ", ".join(sorted(loud))
    if unpinned:
        detail += ("; UNPINNED task(s): " + ", ".join(sorted(set(unpinned)))
                   + " -- add the measured band to TEST_TRAIN_BANDS with the date")
    if thin:
        detail += ("; too few probes for a band: " + ", ".join(sorted(thin))
                   + f" (under {MIN_PROBES_FOR_A_BAND}); not judged")
    return not loud and not unpinned, detail


def check_waste_before_the_first_node(bench: str):
    """§72: "трата ПОСЛЕ последнего узла" читается только рядом с тратой ДО первого -- и проверялась
    половина пары.

    Driven from `probe_summary --json` over the 141 probes that reached a node. Measured 2026-09-08:

        median 34 %, p25 28, p75 39, max 91

    and the four worst are the SAME TASK: `remPde` 90.6 %, `remPde4` 85.2 %, `remPde5` 83.3 %,
    `remPde2` 78.9 % -- every one of them `pde_heat1d`, every one ending with a single node. On that
    task a dollar buys almost no search: the budget goes on getting to the first evaluation.

    The verdict is the per-task median against a pinned band, for §330's reason: a band computed
    from the probes it judges cannot be failed by them.
    """
    import collections
    import subprocess
    tool = Path(bench) / "looplab" / "benchmarks" / "probe_summary.py"
    if not tool.is_file():
        return False, "probe_summary.py is not on this box, so the pair cannot be driven"
    got = subprocess.run([sys.executable, str(tool), "--json"], capture_output=True, text=True,
                         timeout=900)
    try:
        rows = json.loads(got.stdout)
    except ValueError:
        return False, f"probe_summary produced no json ({got.stdout[-160:]!r})"
    reached = [r for r in rows
               if r.get("reached_a_node") and isinstance(r.get("before_pct"), (int, float))]
    if not reached:
        return False, "no probe on this box reached an evaluated node"
    by_task = collections.defaultdict(list)
    for r in reached:
        by_task[r.get("task") or "?"].append(float(r["before_pct"]))
    said, loud = [], []
    for task, vals in sorted(by_task.items(), key=lambda kv: -len(kv[1])):
        v = sorted(vals)
        med = v[len(v) // 2]
        said.append(f"{task} {med:.0f} % (n={len(v)}, max {max(v):.0f})")
        band = BEFORE_FIRST_NODE_BANDS.get(task)
        if band and not band[0] <= med <= band[1]:
            loud.append(f"{task} median {med:.0f} % outside {band[0]:.0f}-{band[1]:.0f}")
    everyone = sorted(float(r["before_pct"]) for r in reached)
    detail = (f"{len(reached)} probe(s): median {everyone[len(everyone) // 2]:.0f} % of spend goes "
              f"BEFORE the first evaluated node (max {max(everyone):.0f} %); by task: "
              + "; ".join(said))
    if loud:
        detail += "; OUTSIDE the pinned band: " + ", ".join(loud)
    return not loud, detail


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
    ("point 9: a speedup divides by the reference's time", check_denominator_composition),
    ("point 8(c): the money cue misses plan/foresight_rank/hyp_prioritize",
     check_money_cue_reaches_the_choosers),
    ("point 9: 3.6 % of spend lands after the last evaluated node, 16 of 69 runs",
     check_waste_after_the_last_node),
    ("point 9: the other half of the pair -- spend BEFORE the first node",
     check_waste_before_the_first_node),
    ("point 9: the reference-use baseline is 4.9-8.3 %", check_reference_use_band),
    ("point 9: TEST against TRAIN, per task", check_test_tracks_train),
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
