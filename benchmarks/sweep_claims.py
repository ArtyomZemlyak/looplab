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


CLAIMS = [
    ("point 5: seven entries in .baseline_times", check_baseline_count),
    ("point 3: add the abandoned remDL $0.1292 when reconciling", check_abandoned_remdl),
    ("state: remEE, remDL2 and remPde are running", check_named_probes_running),
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
