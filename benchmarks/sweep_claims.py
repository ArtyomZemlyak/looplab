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
import collections
import datetime
import glob
import json
import os
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lanes  # noqa: E402
import arm_fidelity  # noqa: E402
import events_read  # noqa: E402
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


# When `ruler_selfcheck` began recording the denominator of the regime it actually ran in (§353).
# Before this, `cached_ms` was the wide median whatever the run did, so it cannot refute a label.
DENOMINATOR_FOLLOWS_THE_REGIME = "2026-09-08T18:30:00"


def _cache_medians(bench: str) -> dict:
    """`{(task, regime): median ms}` for every baseline entry, so a row can be checked against the
    denominator it claims to have divided by."""
    out = {}
    for row in ruler_check.entries(f"{bench}/looplab/benchmarks/algotune/.baseline_times"):
        if row.get("subset") == "test" and row.get("median"):
            out[(row.get("task"), row.get("regime"))] = float(row["median"])
    return out


def _label_contradicted_by_its_own_denominator(row, medians) -> str | None:
    """§352. Does this reading's `cached_ms` belong to the OTHER regime's cache?

    The strong form on purpose. "Does not match its own cache" would fire on every row written
    before a cache was re-minted, which is a legitimate history; "matches the other regime's cache
    to within 2 %" is a positive identification, and on this box the two differ by 1.9x on
    pde_heat1d and 20x on discrete_log, so there is no ambiguity to split.

    It found four rows §350's fix could not: 2026-09-07T01:58-02:02, all four sweep tasks, stamped
    `lane22r3` while their denominators are the wide cache. They had been sitting in the SERIAL
    pool, and removing them moved the serial-versus-wide gap by 1 to 1.8 points a task.
    """
    task, said, cached = row.get("task"), row.get("regime"), row.get("cached_ms")
    if not said or not isinstance(cached, (int, float)) or cached <= 0:
        return None
    # ONLY ROWS WHOSE DENOMINATOR COULD HAVE BEEN REGIME-AWARE (§353). Until 2026-09-08T18:30
    # `_cached_median_ms` defaulted to the WIDE key for every caller, so a correct SERIAL reading
    # recorded a wide `cached_ms` and this check would convict it of a mislabel it does not have.
    # That is what happened on the first §352 accusation: four rows of 2026-09-07 were called
    # mislabelled on a field that was wide by construction. They are unattributable for §350's
    # reason -- the label came from the newest cache FILE -- and not for this one.
    if str(row.get("stamp") or "")[:19] < DENOMINATOR_FOLLOWS_THE_REGIME:
        return None
    other = (ruler_check.SERIAL_REGIME if said == ruler_check.CAMPAIGN_REGIME
             else ruler_check.CAMPAIGN_REGIME)
    mine, theirs = medians.get((task, said)), medians.get((task, other))
    if not theirs:
        return None
    if mine and abs(cached - mine) / mine <= 0.02:
        return None                          # its own cache accounts for the number
    if abs(cached - theirs) / theirs <= 0.02:
        return other
    return None


def _caveats(task, refs, inferred, contradicted, withdrawn, unattributed, serial_first,
             neighboured=None) -> str:
    """Everything that had to be said about HOW this task's readings were selected.

    Lifted out of the pooled branch (§352): a task whose only mishandled rows are serial falls to
    the single-reading path, and every one of these notes silently vanished with it -- the
    "2 readings refuted by their own denominator" line printed nowhere for exactly the case that
    produced it. A caveat that appears only on one of two paths is worse than none, because its
    absence reads as "nothing to report".
    """
    neighboured = neighboured or {}
    how = ""
    seen_refs = refs.get((task, ruler_check.CAMPAIGN_REGIME)) or set()
    named = {r for r in seen_refs if r}
    if len(named) > 1:
        how += (f" [{len(named)} DIFFERENT reference module(s) across these readings: "
                + ", ".join(sorted(named)) + " -- not one series]")
    elif seen_refs and not named:
        how += " [no reading names the reference it used]"
    if inferred.get(task):
        how += f" ({inferred[task]} of them INFERRED, taken before {serial_first[:16]})"
    if neighboured.get(task):
        how += (f" [{neighboured[task]} reading(s) taken with another probe ALIVE on the box -- "
                "quiet by cpu, not by neighbours]")
    if contradicted.get(task):
        how += (f" [{contradicted[task]} reading(s) whose own cached_ms belongs to the "
                "OTHER regime's cache -- label refuted by the reading]")
    if withdrawn.get(task):
        how += (f" [{withdrawn[task]} reading(s) WITHDRAWN by the record: taken under an "
                "interpreter it marks wrong]")
    if unattributed.get(task):
        how += (f" [{unattributed[task]} later reading(s) DROPPED: no regime recorded and "
                "both regimes existed by then]")
    return how


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
    refs: dict = {}
    withdrawn: dict = {}
    contradicted: dict = {}
    neighboured: dict = {}
    medians = _cache_medians(bench)
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
            # A READING THE RECORD MARKS WRONG IS NOT EVIDENCE (§349). §299 withdrew §296-§298
            # after finding they were all measured under conda instead of the bench venv, and
            # marked the nine readings `interpreter: conda (WRONG -- see §299)` rather than deleting
            # them. Nothing here looked at that field: those nine miss today's verdict only because
            # they predate `busy_cpus_outside_lane`, so the exclusion is an accident of field order.
            # One conda row written a day later, with the busy count on it, would have gone straight
            # into a mean -- carrying the exact error §299 exists to record.
            # THE ROW'S OWN DENOMINATOR OUTRANKS ITS LABEL (§352). `cached_ms` is the cached
            # per-instance median this reading divided by, and the two regimes' caches are nothing
            # alike -- 146.5 ms against 78.2 on pde_heat1d, 2.18 against 1.47 on discrete_log. A
            # label that disagrees with it is refuted by the reading itself, which is the second
            # instrument §350's fix did not have.
            mislabelled = _label_contradicted_by_its_own_denominator(row, medians)
            if mislabelled:
                contradicted[task] = contradicted.get(task, 0) + _n_values(row)
                continue
            said_interp = str(row.get("interpreter") or "")
            if "WRONG" in said_interp:
                # IN READINGS, NOT ROWS -- the unit the sentence beside it uses (§340). One sitting
                # carries four reps, and counting sittings printed "1 reading(s) WITHDRAWN" beside
                # "2 quiet wide read(s)". The same mismatch, one field over, caught by its own test.
                withdrawn[task] = withdrawn.get(task, 0) + _n_values(row)
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
                # AND WHICH REFERENCE EACH READING WAS TAKEN AGAINST (§346). "The reference against
                # itself" is one series only while the reference is one file. Readings that used
                # different ones are two series pooled into a mean measured nowhere -- the §317
                # mistake with a different key. Rows written before the field exists carry None and
                # are counted separately, because "not recorded" is not "the same as the others".
                refs.setdefault((task, reg), set()).add(row.get("reference_sha"))
                # AND WHETHER ANOTHER PROBE WAS ALIVE (§365). `busy_cpus_outside_lane == 0` says no
                # neighbour was burning cpu at the sampled instants; it does not say none was about
                # to. A reading taken beside four sleeping probes is filed as quiet today.
                near = row.get("neighbours_alive")
                if isinstance(near, int) and near > 0:
                    neighboured[task] = neighboured.get(task, 0) + _n_values(row)
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
        how = _caveats(task, refs, inferred, contradicted, withdrawn, unattributed,
                       serial_first, neighboured)
        vals = pool.get((task, ruler_check.CAMPAIGN_REGIME)) or []
        n = len(vals)
        if n >= 2:
            moved, mean, scatter, sem = constant_moved(vals, quoted)
            delta = (mean - quoted) / quoted
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
            # BOTH NUMBERS, NAMED (§394): the scatter is what the constant is judged against, the
            # SEM is how well the mean is known. Printing only the second is what made a constant
            # inside the readings' own range read as a 5-sigma drift.
            said.append(f"{task}: list {quoted:.4f}, {n} quiet wide read(s) mean {mean:.4f} "
                        f"(scatter +-{scatter:.4f}, mean known to +-{sem:.4f}) "
                        f"({100 * delta:+.1f} %){how}{beside}"
                        f"{'  <-- ' if moved else ''}")
            continue
        delta = (got - quoted) / quoted
        mark = "" if abs(delta) <= DRIFT_TOLERANCE else "  <-- "
        if abs(delta) > DRIFT_TOLERANCE:
            off += 1
        said.append(f"{task}: list {quoted:.4f}, ONE reading {got:.4f} on {stamp[:10]}{under} "
                    f"({100 * delta:+.1f} %){how}{mark}")
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
    try:
        have = denominator_halves(Path(bench) / DRIFT_LOG)
    except OSError as exc:
        return False, f"cannot read the drift log: {type(exc).__name__}"
    if not have:
        return False, ("no reading records both halves of the denominator in "
                       f"{ruler_check.CAMPAIGN_REGIME} yet -- run ruler_selfcheck --record once "
                       "per task on a bench lane")
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
            said.append(f"{task}: {share:.0f} % harness ({ruler_check.CAMPAIGN_REGIME}), "
                        f"and a score of {best:.1f} bounds the FIXED "
                        f"part at {fixed:.1f} % of it{'  <-- not bounded below 10 %' if fixed >= 10 else ''}")
        else:
            said.append(f"{task}: {share:.0f} % harness ({ruler_check.CAMPAIGN_REGIME}); "
                        "no score here to bound the fixed part")
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
    # EVERY PROBE TREE, NOT THE THREE NEWEST (§341). The revisit line the foresight panel's blindness
    # carries -- "if either grows past a few per cent" -- is a statement about the corpus, and a
    # three-probe window cannot fail it or clear it honestly. On 2026-09-08 the three newest were
    # all `discrete_log`, the task that ranks highest on this phase, and the window read 3.0 %:
    # the check announced the line crossed. Pooled over all 142 trees that have the span the figure
    # is 2.06 % (per probe: median 2.01, p75 2.63, max 5.56 -- 21 of 142 at or above 3 %). The
    # decision stands; the alarm was the sample. Pooling all of them costs 13 s.
    roots = [str(Path(r).parent) for r in roots if "/_ruler/" not in r]
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
    detail = f"over {len(roots)} probe tree(s): " + detail
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


def live_probes(bench: str) -> set:
    """Probe names with a process on them right now -- the only ones whose shares can still move.

    §360. `before_pct` and `after_pct` are shares of a probe's OWN spend, so both move for the whole
    life of a run: `before_pct` is 100 % at the first node and falls with every dollar after it,
    `after_pct` grows between nodes and collapses on each one (`probe_summary` says the second in as
    many words, and §347 is the record of my building an alarm on it anyway). A live probe's figure
    therefore cannot be compared with a finished probe's, and the corpus bands are built from
    finished ones.

    Driven by the defect it fixes: on 2026-09-08 the running `pgr2` had spent $0.4925 with its first
    node at $0.3877, so its `before_pct` read 79 % -- and pagerank's band, one probe wide until that
    morning, flipped to "OUTSIDE the pinned band: pagerank median 79 % outside 20-60". By the $1
    ceiling the same probe would read about 39 %.

    The reading is `os.sched_getaffinity` through `lanes.probes()`, not a terminal event: `freeB3`
    and `remDL` carry no `run_finished` and stopped moving weeks ago, so "not finished" would have
    dropped two legitimate historical probes out of the corpus.
    """
    try:
        return {p.get("probe") for p in lanes.probes(bench + "/..") if p.get("probe")} | \
               {p.get("probe") for p in lanes.probes() if p.get("probe")}
    except OSError:
        return set()


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
    live = live_probes(bench)
    with_node = [r for r in rows if r.get("reached_a_node") and isinstance(r.get("after_pct"),
                                                                          (int, float))
                 and r.get("probe") not in live]
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
    # HELD OUT AND SAID SO. A check whose corpus silently changes size when a probe is running
    # reports a different band each sweep with no line explaining why (§360).
    aside = ""
    names = {r.get("probe") for r in rows}
    if live & names:
        aside = (f" [{len(live & names)} live probe(s) held out ("
                 + ", ".join(sorted(live & names)) + "): their share of their own spend still moves]")
    detail = (f"{len(with_node)} probe(s) with a node: {corpus:.1f} % of ${total:.4f} lands after "
              f"the last evaluated node (median per probe {median:.1f} %), {holding} end holding an "
              f"unfinished draw -- the quoted figures are 3.6 % and 16 of 69" + aside)
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
    # THE OTHER HALF, DRIVEN (§375). The docstring above records `ref_call_pct`'s distribution as a
    # measurement from 2026-09-08 and the check never re-read it: half this claim was quoted from a
    # comment every sweep while the other half was measured, which is the exact shape this file
    # exists to end. They are not the same number -- they disagree on 37 of 152 probes -- so a drift
    # in the calls half would have been invisible.
    calls = sorted(float(r["ref_call_pct"]) for r in rows
                   if isinstance(r.get("ref_call_pct"), (int, float)))
    other = ""
    if calls:
        other = (f"; by CALLS rather than imports: median {calls[len(calls) // 2]:.1f} % "
                 f"(p25 {calls[len(calls) // 4]:.1f}, p75 {calls[3 * len(calls) // 4]:.1f}, "
                 f"max {calls[-1]:.1f}) over {len(calls)} probe(s)")
    detail = (f"{len(have)} probe(s): reference reached in {med:.1f} % of run_probe spans "
              f"(p25 {lo:.1f}, p75 {hi:.1f}); {inside} of {len(have)} inside the quoted 4.9-8.3 %; "
              f"{split} import it in written code and never from a probe, so the question has two "
              "answers per probe")
    # The band HOLDS only if it describes the corpus -- most of it inside, not a third.
    return inside >= 0.5 * len(have), detail + other


# TEST divided by the best TRAIN, per task, measured 2026-09-08 over the 141 probes that have both.
# Pinned rather than recomputed: a band derived from the probes it judges cannot be failed by them.
#   (low, high, probes it was measured over)
TEST_TRAIN_BANDS = {
    "edge_expansion": (0.892, 1.033, 118),
    "discrete_log": (0.890, 1.260, 11),     # its cached times run p90/p10 = 276; the width is the tail
    "pde_heat1d": (0.991, 1.054, 10),
    # PINNED 2026-09-09 AT TEN PROBES (§376), the bar §364 set: the thinnest band already here rests
    # on ten. Derived the way its neighbours were -- the corpus min and max at pinning time, rounded
    # OUTWARD to three places: 0.9586 -> 0.958, 1.0134 -> 1.014. Checked against the other three
    # before typing it: discrete_log 0.8901-1.2600 -> (0.890, 1.260), edge_expansion
    # 0.8922-1.0326 -> (0.892, 1.033), pde_heat1d 0.9912-1.0538 -> (0.991, 1.054). The ten it is
    # built from cannot fail it, which is why `n` is recorded: the eleventh probe onward can, and
    # that is the whole use of a pinned band.
    #
    # The old note stays, because it is the reason this took ten probes: pagerank was NOT here while
    # it had one probe reading x0.993, and pinning that flagged that very probe on the next run --
    # 0.99296 is outside 0.993-0.993 by rounding alone. One measurement is a point.
    "pagerank": (0.958, 1.014, 10),
}

MIN_PROBES_FOR_A_BAND = 2
# HOW MUCH EVIDENCE A BAND IS PINNED ON HERE -- computed from the table above rather than typed, so
# it cannot drift from what the file actually does. Today: 118, 11 and 10 probes, so the bar is 10.
MIN_PROBES_TO_PIN = min(n for _lo, _hi, n in TEST_TRAIN_BANDS.values())


def band_exposure(by_task: dict) -> list:
    """`[(task, judged, pinned_n)]` -- how many probes each pinned band has actually JUDGED.

    §400. §330's rule is written into this file: a band derived from the probes it judges cannot be
    failed by them, so each band records the `n` it was pinned at. What the line never said is how
    many probes have arrived SINCE. Measured 2026-09-10:

        discrete_log   pinned at 11, 13 now -> 2 judged
        pde_heat1d     pinned at 10, 12 now -> 2 judged
        edge_expansion pinned at 118, 118 now -> 0 judged
        pagerank       pinned at 10, 10 now -> 0 judged

    Two of the four bands still rest entirely on the probes they were derived from. That is not a
    defect in the band -- it is the state of the evidence, and a reader who sees "n=118" takes it
    for 118 probes' worth of testing when it is 118 probes' worth of DERIVING and none of testing.
    The number that says which is the difference between the two.
    """
    out = []
    for task, ratios in sorted(by_task.items()):
        band = TEST_TRAIN_BANDS.get(task)
        if band is None:
            continue
        out.append((task, len(ratios) - band[2], band[2]))
    return out


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
    loud, unpinned, thin, narrow = [], [], set(), []
    for probe, task, best, test in pairs:
        band = TEST_TRAIN_BANDS.get(task)
        if band is None:
            if len(by_task[task]) < MIN_PROBES_FOR_A_BAND:
                thin.add(task)          # one probe is a point, not a band
            elif len(by_task[task]) < MIN_PROBES_TO_PIN:
                # COMPUTABLE, NOT YET PINNABLE (§364). At two probes the band IS its two points:
                # pinning it makes a rule those two can never fail, which is §330 at its smallest
                # scale -- and the pagerank comment above `TEST_TRAIN_BANDS` is the record of that
                # happening at one probe. The bar is not invented: it is the THINNEST band already
                # pinned in this file, so the advice waits for as much evidence as its neighbours.
                narrow.append(task)
            else:
                unpinned.append(task)
            continue
        lo, hi, _n = band
        if not lo <= test / best <= hi:
            loud.append(f"{probe} on {task} x{test / best:.3f} outside {lo:.3f}-{hi:.3f}")
    detail = "; ".join(said)
    # AND HOW MANY EACH BAND HAS JUDGED (§400): pinned-at-n is what it was DERIVED from, and a band
    # that has judged nothing since cannot have been failed by anything.
    exposure = band_exposure(by_task)
    untested = [f"{task} (pinned at {n}, judged 0 since)" for task, judged, n in exposure
                if judged <= 0]
    tested = [f"{task} +{judged}" for task, judged, _n in exposure if judged > 0]
    if tested:
        detail += "; judged since pinning: " + ", ".join(tested)
    if untested:
        detail += ("; STILL RESTING ON ITS OWN DATA: " + ", ".join(untested)
                   + " -- a band no later probe has met cannot have been failed by one")
    if loud:
        detail += "; OUTSIDE the pinned band: " + ", ".join(sorted(loud))
    if unpinned:
        detail += ("; UNPINNED task(s): " + ", ".join(sorted(set(unpinned)))
                   + " -- add the measured band to TEST_TRAIN_BANDS with the date")
    if narrow:
        # STILL UNPINNED, AND STILL A FAILURE. The first cut of §364 let a thin task pass, and the
        # §337 test that guards this claim went red -- rightly. An unpinned task is one NOTHING is
        # judging, and a green "TEST tracks TRAIN, per task" while a task goes unjudged is the
        # silence §337 exists to break. What §364 changes is the ADVICE, not the verdict: the
        # operator is told to run more probes rather than to pin a band its own two probes cannot
        # fail.
        detail += ("; UNPINNED task(s): " + ", ".join(sorted(set(narrow)))
                   + " -- band computable but TOO THIN TO PIN ("
                   + ", ".join(f"{t} n={len(by_task[t])}" for t in sorted(set(narrow)))
                   + f", the thinnest pinned band rests on {MIN_PROBES_TO_PIN}): pinning it now "
                   "would be a rule its own probes cannot fail -- run more probes")
    if thin:
        detail += ("; too few probes for a band: " + ", ".join(sorted(thin))
                   + f" (under {MIN_PROBES_FOR_A_BAND}); not judged")
    return not loud and not unpinned and not narrow, detail


# The bridge stamps every evaluation with which half of the dataset it actually ran on, and why it
# believes that. `patch_eval_subset.py` writes a marker into the harness and `looplab_eval.py` reads
# it back, so `verified` is a check against the patched file rather than a repeat of what was asked.
_SUBSET_EVIDENCE = re.compile(r'"subset_evidence"\s*:\s*(\{[^{}]*\})')


def check_every_node_was_graded_on_train(bench: str):
    """Every LoopLab node is evaluated on TRAIN; TEST is the graded split and the run must not see it.

    NOTHING ON THIS BOX CHECKED IT. The rule is the reason `compare_arms` refuses to put a run's own
    champion metric in the same column as arm A's test result (`_arm_b_final`: "every LoopLab node is
    evaluated on TRAIN, mirroring AlgoTuner's agent loop"), and the whole arm-B column is worthless
    if a single node was scored on the half it is graded against. A silent violation would not look
    like a failure -- it would look like a good score.

    Driven 2026-09-08 over every `node_evaluated` in the corpus: **392 nodes, 392 asked `train`, 392
    verified, 0 without evidence**, every one by `patch_marker_present`. The check exists so that
    stays a measurement instead of a thing everyone knows.

    A node whose record carries NO evidence is reported, not passed over: unverifiable is not the
    same as verified, and it is the state a future harness change would produce.
    """
    nodes = missing = 0
    wrong: list = []
    reasons: dict = {}
    for path in glob.glob(f"{bench}/model-probes/*/runs/*/*/events.jsonl"):
        probe = path.split("/model-probes/", 1)[1].split("/")[0]
        if probe == "_ruler":
            continue
        # THROUGH THE SHARED READER (§361). A line in `events.jsonl` is not an event: the engine
        # writes crash-atomic packets whose real events sit in `data.events`, and the corpus holds
        # 23 of them carrying 17 `node_failed`, 17 `pause` and 6 `node_building`. None is a
        # `node_evaluated` TODAY, which is the only reason this check's own loop was not already
        # wrong -- and "not wrong yet" is not a rule. `events_read` is the one place that knows
        # both spellings of the sentinel and refuses to swallow a row that only looks like one.
        for row in events_read.iter_events(path):
            if row.get("type") != "node_evaluated":
                continue
            nodes += 1
            got = _SUBSET_EVIDENCE.search(str((row.get("data") or {}).get("stdout_tail") or ""))
            try:
                evidence = json.loads(got.group(1)) if got else None
            except ValueError:
                evidence = None
            if not isinstance(evidence, dict):
                missing += 1
                continue
            reasons[evidence.get("reason")] = reasons.get(evidence.get("reason"), 0) + 1
            if evidence.get("asked") != "train" or evidence.get("verified") is not True:
                wrong.append(f"{probe}/node {(row.get('data') or {}).get('node_id')}: "
                             f"asked={evidence.get('asked')!r} "
                             f"verified={evidence.get('verified')!r}")
    if not nodes:
        return False, "no evaluated node on this box, so the split cannot be checked"
    detail = (f"{nodes} evaluated node(s): {nodes - missing - len(wrong)} asked train and verified"
              + (f", by {', '.join(f'{k} x{v}' for k, v in sorted(reasons.items()))}" if reasons else ""))
    if missing:
        detail += (f"; {missing} carry NO subset evidence -- unverifiable, which is not the same "
                   "as verified")
    if wrong:
        detail += "; GRADED ON THE WRONG HALF: " + "; ".join(wrong[:6])
    return not (missing or wrong), detail


def check_arm_a_constants_are_a_file(bench: str):
    """"перемер констант плеча A" -- point 10's last queue item, and the numbers lived in prose.

    §355. §181 re-timed arm A on the verified ruler and §193 added two more, and the results existed
    only as a markdown table in docs/56: `grep -rl 0.9648` over every json, py, txt and jsonl on the
    box returned nothing but coincidental substrings inside probe spans. The figures the whole
    A-versus-B comparison rests on were a sentence.

    Re-measured 2026-09-08 from the surviving campaign logs -- the solver AlgoTuner actually shipped,
    taken after `FILE IN CODE DIR solver.py:` and cut at the first log line -- and stored with every
    field that decides whether a number may be averaged with an arm-B score. The model is read off
    the campaign log (`Model: deepseek-v4-flash`), not assumed, and the campaign's own figures in the
    file match §193's column exactly, which is what says the extraction took the right text.

        task             own campaign   §181     now      regime
        edge_expansion   1.1087         0.9648   0.9759   __w22x1r3
        pde_heat1d       1.1010         1.0267   1.0259   __w22x1r3
        discrete_log     1.5419         1.5133   1.4747   __w22x1r3
        pagerank         None           0.0      0.0      no_valid_speedups

    This check does not re-time anything -- that takes a 22-cpu lane. It asserts the file is there,
    that every entry names the regime it was taken in, and that nothing in it drifted from §181 by
    more than 5 %, which is the band a Python solver on twenty-two concurrent workers moves in.
    """
    path = Path(bench) / "looplab" / "benchmarks" / "algotune" / "arm_a_retimed.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, (f"arm A's re-timed constants are not a file on this box "
                       f"({type(exc).__name__}) -- they are quoted from docs/56 and nothing can "
                       "check them")
    tasks = data.get("tasks") or {}
    if not tasks:
        return False, "arm_a_retimed.json holds no tasks"
    said, bad = [], []
    for task, row in sorted(tasks.items()):
        got, was = row.get("speedup"), row.get("s181_said")
        regime = row.get("regime")
        if not regime:
            bad.append(f"{task} does not name the regime it was taken in")
        if not isinstance(got, (int, float)):
            bad.append(f"{task} has no number")
            continue
        if not isinstance(was, (int, float)):
            bad.append(f"{task} carries no §181 constant to compare against")
            said.append(retimed_line(task, row, None))
            continue
        drift, problem = retimed_verdict(got, was)
        if problem:
            bad.append(f"{task} {problem}")
        said.append(retimed_line(task, row, drift))
    detail = "; ".join(said) + ("; PROBLEM: " + "; ".join(bad) if bad else "")
    return not bad, detail


_CHAMPION_LINE = re.compile(r"champion node (\d+) \(metric=([0-9.]+)\)")


def retimed_verdict(got, was):
    """`(drift_percent_or_None, problem_or_None)` for one re-timed constant against §181's.

    §385. The old line computed `100 * (got - was) / was` behind `if was`, so a task whose §181
    constant is **0.0** produced a drift of exactly `+0.0 %` whatever the re-timing said, and the
    5 % gate could not fire on it. `pagerank` is that task, and it is the one where arm A shipped a
    solver that returns nothing valid -- the row where a change is most worth catching, because
    going from "no valid speedups" to a real number is the difference between an arm that failed
    and an arm that ran.

    A zero is not a small speedup, so the two are never compared in per cent. Crossing between them
    is a change of KIND and is reported as one.
    """
    zero_now, zero_then = not got, not was
    if zero_now and zero_then:
        return None, None
    if zero_then:
        return None, (f"§181 recorded NO valid speedups and the re-timing reads {got:.4f} -- "
                      "a change of kind, not a drift: arm A now produces something scorable")
    if zero_now:
        return None, (f"§181 recorded {was:.4f} and the re-timing reads NO valid speedups -- "
                      "a change of kind, not a drift: arm A stopped producing anything scorable")
    drift = 100.0 * (got - was) / was
    if abs(drift) > 5.0:
        return drift, f"moved {drift:+.1f} % from §181's {was}"
    return drift, None


def retimed_line(task: str, row: dict, drift) -> str:
    """The SENTENCE one constant gets. A function because the wording is the fix (§342): the
    docstring above has always shown `pagerank ... no_valid_speedups`, while the code rendered
    `pagerank 0.0000 (__w22x1r3, +0.0 % vs §181)` -- a zero dressed as a score, next to three real
    ones, with the reason the file records dropped on the floor."""
    got, regime = row.get("speedup"), row.get("regime")
    reason = row.get("no_speedup_reason")
    if not got:
        return f"{task} NO VALID SPEEDUPS ({regime}, reason {reason or 'unrecorded'})"
    drift_part = f"{drift:+.1f} % vs §181" if drift is not None else "no §181 constant"
    return f"{task} {got:.4f} ({regime}, {drift_part})"


def constant_moved(vals, quoted: float, tolerance: float = DRIFT_TOLERANCE):
    """`(moved, mean, scatter, sem)` -- has the quoted constant left the readings' own spread?

    §394. The old rule was `|mean - quoted| > 2 * SEM and |delta| > tolerance`, and its comment says
    the SEM test "stops a +-4 % instrument reporting a 3 % drift every other sitting". It does not:
    SEM shrinks as 1/sqrt(n) while the instrument's scatter does not, so the test only DELAYS that
    report until enough sittings accumulate. `pde_heat1d` is the case -- 12 sittings, mean 1.0416,
    SEM 0.0088, list 0.9958, flagged MOVED at +4.6 % -- while the sitting medians themselves run
    0.9897 to 1.1013 with sd 0.0319. The constant sits INSIDE the range of ordinary readings; what
    the SEM measures is how precisely we know the mean, which is a different question from whether
    a constant is consistent with a reading.

    Measured the same day: within one sitting the spread is 6.2 % (median over 11 sittings), and
    seven fresh reads ran 0.9716 to 1.0556. A constant judged against the mean's precision would
    eventually be "moved" by nothing but patience.

    So the verdict is the SCATTER (2 sd of the sitting medians) AND the tolerance. The SEM is still
    computed and printed, because how well the mean is known is worth seeing -- it is just not what
    decides.
    """
    n = len(vals)
    if n < 2:
        return None, None, None, None
    mean = statistics.fmean(vals)
    scatter = statistics.stdev(vals)
    sem = scatter / (n ** 0.5)
    delta = (mean - quoted) / quoted
    moved = abs(mean - quoted) > 2 * scatter and abs(delta) > tolerance
    return moved, mean, scatter, sem


def denominator_halves(log_path, regime: str = None) -> dict:
    """`{task: (cached_ms, solver_ms, stamp)}` for readings taken in ONE regime.

    §395. The old loop kept whichever row came LAST in the file, whatever regime it was taken in --
    and the two regimes have different denominators: `pde_heat1d` caches 146.49 ms wide against
    78.32 ms serial. So the answer depended on which regime happened to write last. It did:
    2026-09-09 morning the sweep printed "pde_heat1d: 3 % harness ... bounds the FIXED part at
    20.0 %", and the same afternoon, after two wide readings landed, "47 % harness ... 1.3 %". Same
    task, same box, same claim, fifteen times apart, and nothing said a regime had changed under it.

    The pairing has to hold: the bound divides by the best PROBE score, and probes run wide. A
    serial denominator against a wide score is two different measurements in one fraction.

    A reading whose regime was never recorded is not silently adopted -- both regimes existed by the
    time most of them were written, so it cannot be attributed (the same rule §-the constants check
    applies to its own pool).
    """
    want = regime or ruler_check.CAMPAIGN_REGIME
    out = {}
    try:
        fh = open(log_path, encoding="utf-8", errors="replace")
    except OSError:
        raise
    with fh:
        for line in fh:
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get("regime") or "").lstrip("_") != want:
                continue
            cached, solver = row.get("cached_ms"), row.get("solver_ms")
            if isinstance(cached, (int, float)) and isinstance(solver, (int, float)) \
                    and 0 < solver < cached:
                out[row.get("task")] = (float(cached), float(solver), str(row.get("stamp") or ""))
    return out


def check_the_champion_is_the_best_evaluated_node(bench: str):
    """The probe must submit the node the LOOP judged best -- not the newest file on disk.

    §367. `run_probe.sh` carries the reason in its own words: picking a fresh `solver.py` "не то же
    самое, что лучший", and on `convex_hull` on 2026-08-27 node 0 scored 3.7777 on train against
    node 1's 2.7342 while `ls -t` returned node 1, because it was written later. Node 1 was measured
    on test and reported as the probe's result all day; the real champion was never measured at all.
    `extract_champion.py --all-files` reads the fold and knows `state.best()`, and the driver is
    supposed to call it -- "иначе проба меряет не то, что цикл счёл лучшим, — то есть меряет не цикл".

    Nothing checked that it does. Driven here from two independent places that would have to lie
    together: the champion line the extractor prints into `probe.log`, and the node metrics in the
    run's own `events.jsonl`. Measured 2026-09-09 over the corpus: **141 probes agree, 0 disagree.**

    A probe with no champion line is not counted -- it never shipped one, which §351 covers.
    """
    agree, disagree = 0, []
    for probe_dir in sorted(glob.glob(f"{bench}/model-probes/*")):
        name = os.path.basename(probe_dir)
        if name == "_ruler" or not os.path.isdir(probe_dir):
            continue
        try:
            said = _CHAMPION_LINE.search(
                Path(probe_dir, "probe.log").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not said:
            continue
        node_id, metric = int(said.group(1)), float(said.group(2))
        best, best_id = None, None
        for path in glob.glob(f"{probe_dir}/runs/*/*/events.jsonl"):
            for row in events_read.iter_events(path):
                if row.get("type") != "node_evaluated":
                    continue
                data = row.get("data") or {}
                got = data.get("metric")
                if isinstance(got, (int, float)) and (best is None or got > best):
                    best, best_id = got, data.get("node_id")
        if best is None:
            continue
        if abs(best - metric) > 1e-4 or best_id != node_id:
            disagree.append(f"{name} shipped node {node_id} ({metric:.4f}) while node {best_id} "
                            f"scored {best:.4f}")
        else:
            agree += 1
    if not agree and not disagree:
        return False, "no probe on this box records which node it shipped"
    # AND THE EXPOSURE (§396): how many runs could have caught the rule being wrong. A pass rate
    # without it is a green tick on a corpus that may not contain the case at all.
    with_choice, differ, lost = where_the_newest_rule_would_differ(bench)
    exposure = (f"; {differ} of {with_choice} run(s) with a choice have their best node NOT last, "
                f"so the newest-file rule would have shipped a node worth "
                f"{lost:.0f} % less metric (median)" if differ and lost is not None else
                f"; {differ} of {with_choice} run(s) with a choice have their best node NOT last"
                if with_choice else
                "; NOTHING ON THIS BOX HAS TWO EVALUATED NODES -- the rule is untested here")
    detail = (f"{agree} probe(s) shipped the best evaluated node" + exposure
              + ("; NOT THE BEST: " + "; ".join(disagree[:6]) if disagree else ""))
    return not disagree, detail


def where_the_newest_rule_would_differ(bench: str):
    """`(runs_with_a_choice, runs_where_best_is_not_newest, median_metric_at_stake)`.

    §396. "149 probe(s) shipped the best evaluated node" is a green tick that says nothing about
    whether the rule was ever TESTED: if the best node were always the last one, `ls -t` and
    `state.best()` would agree everywhere and the check would pass on a corpus that cannot fail it.
    Measured 2026-09-09 over 143 runs with two or more evaluated nodes: in **103 of them (72 %)**
    the best node is NOT the newest, and the naive rule would have shipped a node worth a median of
    499 % less metric. §367's defect was not a near miss -- it is the common case.

    So the count is reported WITH the number of runs that could have caught it. A guard's pass rate
    and its exposure are two different facts, and only the pair means anything.
    """
    with_choice = differ = 0
    lost = []
    for run in sorted(glob.glob(f"{bench}/model-probes/*/runs/*/run")):
        nodes = []
        for row in events_read.iter_events(os.path.join(run, "events.jsonl")):
            if row.get("type") != "node_evaluated":
                continue
            data = row.get("data") or {}
            metric, ts = data.get("metric"), row.get("ts")
            if isinstance(metric, (int, float)):
                nodes.append((data.get("node_id"), metric, ts or 0))
        if len(nodes) < 2:
            continue
        with_choice += 1
        best = max(nodes, key=lambda r: r[1])
        newest = max(nodes, key=lambda r: r[2])
        if best[0] != newest[0]:
            differ += 1
            if newest[1]:
                lost.append(100.0 * (best[1] - newest[1]) / newest[1])
    return with_choice, differ, (statistics.median(lost) if lost else None)


def check_a_dollar_probe_costs_a_dollar(bench: str):
    """"$1/проба" -- the figure every plan on this bench is built from, never checked against spend.

    §370. Measured over the corpus: **143 of 149 probes with spend went OVER their budget**, median
    excess $0.0096, total $1.5337. That is not a leak and mostly not news -- the engine checks the
    budget before opening work, and the call already in flight completes and is charged, so a small
    overshoot is structural. What was missing is that nobody had ever put a number on it: a $1 probe
    costs about $1.01, and the corpus cost ~1 % more than the arithmetic everyone quotes.

    The line between structural and wrong is not invented here: it is the run's own
    `node_open_budget_floor_usd` (§363). Below that the engine refuses to OPEN work, so an overshoot
    larger than the floor cannot be one last call finishing -- it is work that should never have
    started. Exactly one probe is past it: `freeB3` at +$0.1056, which is §213's double payment, the
    resume of a run that was already at its ceiling.
    """
    root = f"{bench}/model-probes"
    over, worst, total, seen = [], 0.0, 0.0, 0
    for probe_dir in sorted(glob.glob(f"{root}/*")):
        name = os.path.basename(probe_dir)
        if name == "_ruler" or not os.path.isdir(probe_dir):
            continue
        spend = arm_fidelity._spend(root, name)
        if spend <= 0:
            continue
        seen += 1
        budget = arm_fidelity.probe_budget(root, name)
        excess = spend - budget
        if excess <= 0:
            continue
        total += excess
        worst = max(worst, excess)
        floor = arm_fidelity.node_open_floor(root, name) or DEFAULT_NODE_FLOOR
        if excess > floor:
            over.append(f"{name} +${excess:.4f} over ${budget:.2f} (floor ${floor:.2f})")
    detail = (f"{seen} probe(s) with spend; ${total:.4f} spent past the budgets in total, worst "
              f"+${worst:.4f}")
    if over:
        detail += ("; PAST THE NODE-OPEN FLOOR, so not one last call finishing: "
                   + ", ".join(sorted(over)))
    return not over, detail


# What the engine refuses to open new work below, where a run did not record its own (§363: 2 of 145
# snapshots carry the field). Not a tolerance invented for this check -- the same number the engine
# quotes in its own refusal.
DEFAULT_NODE_FLOOR = 0.10


# The loop's own words, as opposed to boilerplate: a package list in `run_started` and the card
# mention every library the box has installed, so counting those would make "considered" meaningless.
_REASONING_EVENTS = ("research_completed", "hypothesis_added", "reflection_note",
                     "novelty_rejected", "hint")
_CYTHON = re.compile(r"cython|\.pyx|cimport", re.I)


def cython_named_in_reasoning(bench: str, task: str):
    """`(probes_that_named_it, probes_total)` -- did the loop CONSIDER Cython on this task?

    §399. "NEVER reached for Cython: pde_heat1d (0 of 12)" reads as blindness, and the measurement
    says the opposite: Cython is named in the loop's own reasoning in **11 of those 12 probes**, and
    at least one says why it was dropped -- `remPde`'s research note reads "@njit or a .pyx extension
    would add nothing because the FFT is already native code and the Python overhead per instance
    is" negligible. The task is FFT-bound, the loop worked that out, and shipped numba twelve times.

    Considered-and-rejected and never-considered call for opposite responses -- one is a finding
    about the task, the other about the loop -- so the sentence has to tell them apart. Boilerplate
    is excluded: the package list in `run_started` names every library installed on the box.
    """
    named = total = 0
    for run in sorted(glob.glob(f"{bench}/model-probes/*/runs/{task}/run/events.jsonl")):
        total += 1
        for row in events_read.iter_events(run):
            if row.get("type") not in _REASONING_EVENTS:
                continue
            if _CYTHON.search(json.dumps(row.get("data") or {}, ensure_ascii=False)):
                named += 1
                break
    return named, total


def never_shipped_sentence(bench: str, never) -> str:
    """The sentence for tasks that ship no Cython -- weighed-and-dropped or never named at all.

    A function because the WORDING is the fix (§342) and because the check around it shells out to
    `probe_summary.py`, so the sentence could otherwise only be driven on a full bench tree.

    §399: "NEVER reached for Cython" reads as blindness. On `pde_heat1d` the loop NAMES Cython in
    its own reasoning in 11 of 12 probes and ships numba every time, with `remPde` recording why --
    "@njit or a .pyx extension would add nothing because the FFT is already native code". A task
    that weighs the lever and drops it is a finding about the task; one that never names it is a
    finding about the loop, and the two want opposite responses.
    """
    weighed = []
    for entry in never:
        task_name = entry.split(" (")[0]
        said_it, seen = cython_named_in_reasoning(bench, task_name)
        weighed.append(f"{entry}, though it is NAMED in the loop's own reasoning in "
                       f"{said_it} of {seen}" if said_it else
                       f"{entry}, and never named in the loop's reasoning either")
    return ("; NEVER SHIPPED Cython: " + ", ".join(weighed)
            + " -- §377 measured that lever at eightfold on edge_expansion, so a task that weighs "
              "it and drops it every time is a finding about the TASK, not blindness")


def check_which_lever_the_loop_reaches_for(bench: str):
    """Which kernel the loop actually reaches for, per task -- the lever §377 measured at eightfold.

    §378. `kernel_kind` (§377) separates Cython from numba, and the first thing it shows is an
    asymmetry nobody had put on a page: over eleven `pde_heat1d` probes the loop wrote **not one**
    `.pyx`, while on the other tasks it does so most of the time.

    The obvious explanation -- "the pde probes are the oldest" -- is REFUTED by the corpus. In the
    same early era (through 2026-09-01) edge_expansion wrote a `.pyx` in 26 of 27 runs and
    discrete_log in 5 of 8, against pde_heat1d's 0 of 11. It is the task, not the era.

    This does not fail on a task that never reaches for Cython. `pde_heat1d`'s numba champions carry
    the highest numba median of any task (117.74), so declining a lever that has nothing to bite on
    -- its reference is `scipy.integrate.solve_ivp`, where the work is inside SciPy rather than in a
    Python loop -- is a defensible answer rather than a miss. What would be wrong is not knowing.
    """
    import subprocess
    tool = Path(bench) / "looplab" / "benchmarks" / "probe_summary.py"
    if not tool.is_file():
        return False, "probe_summary.py is not on this box, so the claim cannot be driven"
    got = subprocess.run([sys.executable, str(tool), "--json"], capture_output=True, text=True,
                         timeout=1200)
    try:
        rows = json.loads(got.stdout)
    except ValueError:
        return False, f"probe_summary produced no json ({got.stdout[-160:]!r})"
    per: dict = {}
    for row in rows:
        kind = row.get("kernel_kind")
        if not kind:
            continue
        per.setdefault(row.get("task") or "?", collections.Counter())[kind] += 1
    if not per:
        return False, ("no probe reports `kernel_kind` -- the field §377 added is missing, so which "
                       "lever the loop reached for cannot be read at all")
    said, never = [], []
    for task, counts in sorted(per.items()):
        total = sum(counts.values())
        said.append(f"{task} " + "/".join(f"{k} {counts[k]}" for k in sorted(counts)))
        if not counts.get("cython"):
            never.append(f"{task} (0 of {total})")
    detail = "; ".join(said)
    if never:
        detail += never_shipped_sentence(bench, never)
    return True, detail


def probes_to_move_the_median_out(vals, band, cap: int = 500):
    """How many NEW probes at 100 % it would take to push this task's median outside `band`.

    §402. The check judges a MEDIAN against a band, and a median is a rank statistic: it does not
    move until a large fraction of the corpus moves. Measured 2026-09-10 it takes **104 new probes
    at 100 %** to push `edge_expansion`'s median out of (20, 45) -- on a corpus of 118. That alarm
    cannot be reached by any plausible run of events, so the green tick beside it says nothing about
    the thing the claim is for: a probe burning its budget before it ever evaluates.

    Individual probes DO fall outside -- 12 of 153 (7.8 %), worst `remDL3` at 77 %, `capA9` at 64 %,
    `remPde9` at 39 % under a floor of 40 -- and the check never mentioned them. `None` when the
    median is already outside, and `cap` when even that many would not do it.
    """
    v = sorted(float(x) for x in vals)
    if not v or not band:
        return None
    lo, hi = band
    if not lo <= v[len(v) // 2] <= hi:
        return None
    for k in range(1, cap + 1):
        w = sorted(v + [100.0] * k)
        if not lo <= w[len(w) // 2] <= hi:
            return k
    return cap


def probes_outside_their_band(by_task: dict, bands: dict) -> tuple:
    """`(total_outside, [(task, probe, value)])` for probes outside their own task's band."""
    out = []
    for task, entries in by_task.items():
        band = bands.get(task)
        if not band:
            continue
        for value, probe in entries:
            if not band[0] <= value <= band[1]:
                out.append((task, probe, value))
    out.sort(key=lambda r: -abs(r[2]))
    return len(out), out


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
    live = live_probes(bench)
    reached = [r for r in rows
               if r.get("reached_a_node") and isinstance(r.get("before_pct"), (int, float))
               and r.get("probe") not in live]
    if not reached:
        return False, "no probe on this box reached an evaluated node"
    by_task = collections.defaultdict(list)
    named_by_task = collections.defaultdict(list)
    for r in reached:
        by_task[r.get("task") or "?"].append(float(r["before_pct"]))
        named_by_task[r.get("task") or "?"].append((float(r["before_pct"]), r.get("probe")))
    said, loud = [], []
    for task, vals in sorted(by_task.items(), key=lambda kv: -len(kv[1])):
        v = sorted(vals)
        med = v[len(v) // 2]
        said.append(f"{task} {med:.0f} % (n={len(v)}, max {max(v):.0f})")
        band = BEFORE_FIRST_NODE_BANDS.get(task)
        if band and not band[0] <= med <= band[1]:
            loud.append(f"{task} median {med:.0f} % outside {band[0]:.0f}-{band[1]:.0f}")
    # WHAT THE MEDIAN ALARM COSTS TO REACH, AND WHO IS ALREADY OUTSIDE (§402). A median moves only
    # when a large share of the corpus does; naming the price is how a reader knows what the tick is
    # worth, and the probes outside their own band are the event the claim is actually about.
    reach = [(task, probes_to_move_the_median_out(vals, BEFORE_FIRST_NODE_BANDS.get(task)))
             for task, vals in sorted(by_task.items(), key=lambda kv: -len(kv[1]))]
    # EVERY pinned task, not the ones whose price happens to exceed their own n. The first cut used
    # `k >= len(vals)` and dropped `edge_expansion` -- 104 probes needed on a corpus of 118 -- which
    # is the MOST unreachable of the four and the one the filter hid. The price is four numbers;
    # print them and let the reader judge.
    dear = [f"{task} {k}" for task, k in reach if k is not None]
    n_out, worst = probes_outside_their_band(named_by_task, BEFORE_FIRST_NODE_BANDS)
    everyone = sorted(float(r["before_pct"]) for r in reached)
    # HELD OUT AND SAID SO. A check whose corpus silently changes size when a probe is running
    # reports a different band each sweep with no line explaining why (§360).
    aside = ""
    names = {r.get("probe") for r in rows}
    if live & names:
        aside = (f" [{len(live & names)} live probe(s) held out ("
                 + ", ".join(sorted(live & names)) + "): their share of their own spend still moves]")
    detail = (f"{len(reached)} probe(s): median {everyone[len(everyone) // 2]:.0f} % of spend goes "
              f"BEFORE the first evaluated node (max {max(everyone):.0f} %); by task: "
              + "; ".join(said) + aside)
    if n_out:
        detail += (f"; {n_out} of {len(reached)} probe(s) sit OUTSIDE their task's band: "
                   + ", ".join(f"{p} ({t}) {v:.0f} %" for t, p, v in worst[:4]))
    if dear:
        detail += ("; new probes at 100 % needed to move each median OUT of its band: "
                   + ", ".join(dear) + " -- a median is a rank statistic, so this alarm answers "
                   "about the corpus, never about one bad run")
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
    ("point 9: every node was graded on TRAIN, never on the graded half",
     check_every_node_was_graded_on_train),
    ("point 10: arm A's re-timed constants are a file, not a sentence",
     check_arm_a_constants_are_a_file),
    ("point 9: the probe submits the node the loop judged best",
     check_the_champion_is_the_best_evaluated_node),
    ("point 3: a $1 probe costs $1", check_a_dollar_probe_costs_a_dollar),
    ("point 8: which kernel the loop reaches for, per task",
     check_which_lever_the_loop_reaches_for),
    ("point 9: the other half of the pair -- spend BEFORE the first node",
     check_waste_before_the_first_node),
    ("point 9: the reference-use baseline is 4.9-8.3 %", check_reference_use_band),
    ("point 9: TEST against TRAIN, per task", check_test_tracks_train),
]


def verdict_line(stale: int, checked: int, broken: int) -> str:
    """The footer sentence, and the reason it is a function (§342: the wording IS the fix).

    §404. A check that RAISES was named `UNCHECKABLE` and then `continue`d -- out of the stale count
    and out of nothing else. The denominator stayed `len(CLAIMS)`, so the line went on saying
    "of 21 CHECKED" about claims nobody checked, and a claim that was STALE until its check broke
    simply left the tally: driven here, breaking one stale check moved the headline from 14 to 13.
    A broken instrument IMPROVED the score.

    Driven to the limit: with every check raising, the tool printed "0 of 21 checked claim(s) no
    longer hold" and exited **0** -- a green result from instruments that measured nothing, which is
    the exact failure this file exists to catch, in the file itself.
    """
    said = f"{stale} of {checked} checked claim(s) no longer hold"
    if broken:
        said += (f"; {broken} claim(s) UNCHECKABLE -- their checks raised, so they are neither "
                 "holding nor stale and nobody measured them")
    return said


def sweep_exit_code(stale: int, broken: int) -> int:
    """`1` when anything is stale OR any check broke. A dead instrument is at least as urgent as a
    stale claim; exiting 0 over it is how a silent sweep looks like a clean one (§404)."""
    return 1 if (stale or broken) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", default=DEFAULT_BENCH)
    args = ap.parse_args(argv)
    print(f"the standing sweep list as worded on {WORDING_DATE}, checked against the bench")
    stale = broken = checked = 0
    for claim, check in CLAIMS:
        try:
            ok, detail = check(args.bench)
        except Exception as exc:                       # noqa: BLE001 - a broken check is not a verdict
            broken += 1
            print(f"  UNCHECKABLE  {claim}\n               {type(exc).__name__}: {exc}")
            continue
        checked += 1
        if not ok:
            stale += 1
        print(f'  {"HOLDS" if ok else "STALE":>11s}  {claim}\n               {detail}')
    print("  " + verdict_line(stale, checked, broken))
    return sweep_exit_code(stale, broken)


if __name__ == "__main__":
    raise SystemExit(main())
