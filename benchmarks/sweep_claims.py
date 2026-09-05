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
            if task not in latest or stamp > latest[task][1]:
                latest[task] = (float(med), stamp)
    except OSError as exc:
        return False, f"cannot read the drift log: {type(exc).__name__}"

    said, off = [], 0
    for task, quoted in sorted(SWEEP_CONSTANTS.items()):
        if task not in latest:
            # WHY IT IS UNMEASURED, because the two reasons need different actions. The self-check
            # inlines the DELIVERED reference module, which only exists where a probe has staged
            # one: `ruler_selfcheck.build_solver` globs `*/ws/<task>/reference_<task>.py`. Measured
            # 2026-09-06 -- the only tasks with probe trees on this box are discrete_log,
            # edge_expansion and pde_heat1d, so `pagerank`'s constant is not merely unchecked, it is
            # UNCHECKABLE here until a probe runs on that task. Reporting the two the same way sends
            # the reader to re-run a tool that will fail the same way every sweep.
            why = ("no probe has staged its reference module here, so the self-check cannot build "
                   "a solver for it -- run a probe on this task first"
                   if not glob.glob(f"{bench}/model-probes/*/ws/{task}/reference_{task}.py")
                   else "no reading recorded yet")
            said.append(f"{task}: UNMEASURED here ({why}; list says {quoted:.4f})")
            off += 1
            continue
        got, stamp = latest[task]
        delta = (got - quoted) / quoted
        mark = "" if abs(delta) <= DRIFT_TOLERANCE else "  <-- "
        if abs(delta) > DRIFT_TOLERANCE:
            off += 1
        said.append(f"{task}: list {quoted:.4f}, measured {got:.4f} on {stamp[:10]} "
                    f"({100 * delta:+.1f} %){mark}")
    return off == 0, "; ".join(said)


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
