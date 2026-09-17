#!/usr/bin/env python3
"""Does a Developer session end on its BEST measurement, or on its last edit?

WHY THIS EXISTS. docs/60 §60.9 lists two repairs whose sign is not known and whose decision needs
one pass over data that already exists — this is that pass, for both, so neither has to be argued
from an impression:

**A16 — "the best measured edit becomes the node, not the last one."** The engine already submits
the best EVALUATED NODE rather than the last (docs/56 §84: 17 of 24 runs end on a node worse than
their best, p = 7.45e-09, worth 7.1x the median). One level down, a Developer session makes a
median 11.5 `eval_train` measurements and about 2 of them become nodes (§71) — and nothing applies
§84's rule INSIDE the session. If a session's last edit is routinely worse than its best measured
state, committing the best state costs no generations and converts measurements into nodes; if it
is routinely the best, there is nothing here and the item is closed for free.

**A17 — the mid-session stop.** Eleven nodes in the corpus were opened and then died having burnt
$2.71, nine of them on the spend ceiling and two having written nothing at all (§157). A gate on
the REMAINING budget cannot separate them — `accPde` opened its doomed node holding $0.4792, more
than a median completed cycle costs. What distinguishes them is inside the session: turns spent
with no file written. This counts that, per session, beside what the session went on to do.

WHAT IT READS, and what it refuses to guess. `spans.jsonl` only: `tool` spans for the measurement
calls and the writes, `generation` spans for the money and the phase. A session is a PHASE SPAN
(`plan_step`, `implement`, `repair`), identified by its span id, because that is the unit the two
repairs would act on. A measurement's VALUE is parsed out of the tool result the loop recorded —
`_trace_preview` caps that text, so a value past the cap is `None` and the session is reported as
`unparsed` rather than counted: this instrument would rather report a smaller sample than a wrong
number, which is the rule docs/58 §58.11 exists to enforce.

Usage:
    session_measurements.py [PROBE_ROOT] [--json]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import re

# The Developer's own measurement command. `run_dev_command(name="eval_train")` is the operator-
# pinned evaluation the card tells the model to use; anything else it runs is not a graded number.
MEASURE_TOOLS = {"run_dev_command"}
MEASURE_NAMES = {"eval_train"}
WRITE_TOOLS = {"write_file", "edit_file", "delete_file", "declare_stages"}
# The phases that WRITE. `plan`, `propose` and `stages` cannot commit a working set, so a "best
# edit" there is not a thing that exists.
WRITING_PHASES = {"plan_step", "implement", "repair", "card_build"}
DEFAULT_ROOT = "/var/tmp/looplab-bench/model-probes"
# The graded number as the bridge prints it, and as `looplab_eval.py` writes it into the tool
# result. Both spellings, because the tool returns the bridge's stdout JSON verbatim on one path
# and a rendered line on the other; a value we cannot see is None, never 0.0.
_SPEEDUP = re.compile(r'"speedup"\s*:\s*(-?\d+(?:\.\d+)?)|speedup[ =:]+(-?\d+(?:\.\d+)?)', re.I)
_DEV_NAME = re.compile(r'"name"\s*:\s*"([^"]+)"')


def _measure_value(text: str):
    """The graded number in a measurement's result, or None when it is not visible.

    None is NOT zero: a span preview cut mid-JSON, a failed evaluation and a real 0.0 are three
    different facts, and only the third is a measurement. The caller reports the first two as
    `unparsed` rather than folding them into a best-of.
    """
    found = _SPEEDUP.search(text or "")
    if not found:
        return None
    try:
        return float(found.group(1) or found.group(2))
    except (TypeError, ValueError):
        return None


def sessions(spans_path: str) -> list[dict]:
    """One row per writing phase span: its measurements, its writes, and their ORDER.

    The order is the whole question — "did the last edit come after the best measurement" is not
    answerable from counts — so tool calls are kept as a sequence keyed by the span's parent.
    """
    calls: dict[str, list[tuple[float, str, str]]] = collections.defaultdict(list)
    phase_of: dict[str, str] = {}
    cost: collections.Counter = collections.Counter()
    try:
        fh = open(spans_path, encoding="utf-8", errors="replace")
    except OSError:
        return []
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                span = json.loads(line)
            except ValueError:
                continue
            attrs = span.get("attributes") or {}
            kind = span.get("kind")
            parent = str(span.get("parent_id") or span.get("parent") or "")
            phase = str(attrs.get("phase") or "")
            started = span.get("start")
            try:
                started = float(started)
            except (TypeError, ValueError):
                started = 0.0
            if kind == "generation":
                if parent:
                    phase_of.setdefault(parent, phase)
                    try:
                        cost[parent] += float(attrs.get("cost") or 0.0)
                    except (TypeError, ValueError):
                        pass
                continue
            if kind != "tool" or not parent:
                continue
            phase_of.setdefault(parent, phase)
            tool = str(attrs.get("tool") or "")
            payload = str(attrs.get("input") or "")
            # EVERY TOOL CALL GOES INTO THE SEQUENCE, not only the two kinds this file classifies.
            #
            # A17 asks how many TURNS a session spends without writing a file, and until 2026-09-17
            # the sequence held only measurements and writes -- so `calls_before_first_write`
            # counted MEASUREMENTS before the first write and the docstring said turns. Over the
            # 161-probe corpus that reads `p50 0, p90 1, max 1`, which is a true statement about
            # measurements and says nothing at all about the question: a session that reads eleven
            # files and then writes scores 0 by that metric. A threshold set from it would have been
            # set from the wrong distribution, and the reason it looks so tidy -- a maximum of ONE
            # across 1,166 sessions -- is the signature of a metric that cannot see what it claims.
            #
            # A16 is unaffected: it asks whether any WRITE came after the best MEASUREMENT, which is
            # an order relation between two kinds that are both still in the sequence, and inserting
            # third-kind entries between them shifts every index without changing any comparison.
            if tool in MEASURE_TOOLS:
                named = _DEV_NAME.search(payload)
                if named and named.group(1) in MEASURE_NAMES:
                    calls[parent].append((started, "measure", str(attrs.get("output") or "")))
                else:
                    calls[parent].append((started, "other", tool))
            elif tool in WRITE_TOOLS:
                calls[parent].append((started, "write", tool))
            else:
                calls[parent].append((started, "other", tool))

    rows = []
    for span_id, seq in calls.items():
        phase = phase_of.get(span_id) or "?"
        if phase not in WRITING_PHASES:
            continue
        seq.sort(key=lambda item: item[0])
        values = [(i, _measure_value(text)) for i, (_t, kind, text) in enumerate(seq)
                  if kind == "measure"]
        seen = [(i, v) for i, v in values if v is not None]
        writes = [i for i, (_t, kind, _x) in enumerate(seq) if kind == "write"]
        row = {
            "span": span_id, "phase": phase, "cost": round(cost.get(span_id, 0.0), 6),
            "calls": len(seq), "measurements": len(values), "unparsed": len(values) - len(seen),
            "writes": len(writes),
            # A17: TOOL CALLS made before the session wrote anything at all, and whether it ever
            # did. The index is into the full sequence (above), so this is the count of calls of
            # every kind that preceded the first write -- which is the thing a mid-session stop
            # would be counting when it decides a session is going nowhere.
            "calls_before_first_write": (writes[0] if writes else len(seq)),
            "wrote_nothing": not writes,
        }
        if seen:
            best_i, best_v = max(seen, key=lambda item: item[1])
            last_i, last_v = seen[-1]
            row.update({
                # B3's whole question is the SHAPE of the curve, not its ends: "three `eval_train`
                # without improvement and the session stops" can only be judged by asking how often
                # a session that has gone k measurements without a new best goes on to find one.
                # The values are carried in order, unparsed ones left out (58 §58.11: a value the
                # instrument could not see is not a zero), so the reading is over what was SEEN.
                "curve": [v for _i, v in seen],
                "best": best_v, "last": last_v,
                # A16: the state the session ENDED on is the last WRITE, so the question is whether
                # any write followed the best measurement — that is what makes the shipped state
                # different from the measured best.
                "wrote_after_best": any(w > best_i for w in writes),
                "last_is_best": last_i == best_i,
                "gap": round(best_v - last_v, 6),
            })
        rows.append(row)
    return rows


def scan(root: str) -> list[tuple[str, dict]]:
    out = []
    for spans in sorted(glob.glob(f"{root}/*/runs/*/run/spans.jsonl")):
        name = (spans.split("/model-probes/")[-1].split("/")[0]
                if "/model-probes/" in spans else spans.split("/")[-5])
        for row in sessions(spans):
            out.append((name, row))
    return out


def curve_stop(curves, k: int) -> dict:
    """What a stop after `k` non-improving measurements would do, per session.

    B3 (docs/60 §60.9) proposes ending a session once its measurement curve has gone flat: "three
    `eval_train` without improvement". Whether that is a saving or a loss is one question about the
    corpus -- how often a session that HAS gone k measurements without a new best goes on to find
    one anyway -- and the corpus can answer it without an arm.

    A session is judged only if it is long enough to trigger the rule at all; the rest are neither
    saved nor cut and counting them in either denominator would flatter the answer. `improved_after`
    is the FALSE STOP rate: the share of stopped sessions whose best was still ahead of them.
    `saved` counts the measurements the rule would not have paid for, which is the only benefit
    side there is -- a measurement is the expensive act in these sessions.
    """
    eligible = triggered = improved = saved = 0
    for curve in curves:
        if len(curve) < k + 1:
            continue
        eligible += 1
        best = curve[0]
        flat = 0
        for i, v in enumerate(curve[1:], 1):
            if v > best:
                best, flat = v, 0
                continue
            flat += 1
            if flat >= k:
                triggered += 1
                rest = curve[i + 1:]
                saved += len(rest)
                if any(later > best for later in rest):
                    improved += 1
                break
    return {"eligible": eligible, "triggered": triggered, "improved_after": improved,
            "saved_measurements": saved}


def report(rows: list[tuple[str, dict]]) -> str:
    """The two decisions, and the sample each rests on."""
    lines = []
    scored = [r for _n, r in rows if "best" in r]
    lines.append(f"SESSIONS: {len(rows)} writing phases, {len(scored)} with a readable measurement")
    if scored:
        wrote_after = [r for r in scored if r["wrote_after_best"]]
        worse = [r for r in scored if not r["last_is_best"]]
        lines.append("")
        lines.append("A16 -- would committing the BEST measured state change what shipped?")
        lines.append(f"  sessions that wrote AFTER their best measurement : "
                     f"{len(wrote_after)}/{len(scored)}"
                     f" ({100.0 * len(wrote_after) / len(scored):.1f} %)")
        lines.append(f"  sessions whose LAST measurement is not the best   : "
                     f"{len(worse)}/{len(scored)}")
        if worse:
            gaps = sorted(r["gap"] for r in worse)
            mid = gaps[len(gaps) // 2]
            lines.append(f"  median best-minus-last on those                  : {mid:.4f}")
        lines.append("  READ IT AS: the first line is what the repair would touch. Under ~40 % it "
                     "is not worth an arm;")
        lines.append("  the second is the size of what it would recover.")
    silent = [r for _n, r in rows if r["wrote_nothing"]]
    lines.append("")
    lines.append("A17 -- how long does a session go without writing anything?")
    lines.append(f"  sessions that wrote NOTHING at all : {len(silent)}/{len(rows)}")
    if rows:
        before = sorted(r["calls_before_first_write"] for _n, r in rows)
        lines.append(f"  calls before the first write       : "
                     f"p50 {before[len(before) // 2]}, p90 {before[int(len(before) * 0.9)]}, "
                     f"max {before[-1]}")
        lines.append("  READ IT AS: a stop at N calls-without-a-write must sit ABOVE p90 or it cuts "
                     "healthy sessions.")
    curves = [r["curve"] for _n, r in rows if r.get("curve")]
    if curves:
        lines.append("")
        lines.append("B3 -- would stopping a session after k non-improving measurements lose "
                     "anything?")
        lines.append(f"  {'k':>2} {'eligible':>9} {'stopped':>8} {'improved after':>15} "
                     f"{'false-stop':>11} {'measurements saved':>19}")
        for k in (2, 3, 4, 5):
            got = curve_stop(curves, k)
            rate = (100.0 * got["improved_after"] / got["triggered"]) if got["triggered"] else 0.0
            lines.append(f"  {k:>2} {got['eligible']:>9} {got['triggered']:>8} "
                         f"{got['improved_after']:>15} {rate:>10.1f}% "
                         f"{got['saved_measurements']:>19}")
        lines.append("  READ IT AS: false-stop is the share of stopped sessions whose best was "
                     "STILL AHEAD of them.")
        lines.append("  A rule that stops sessions which would have improved is not a saving, "
                     "whatever it saves.")
    unparsed = sum(r["unparsed"] for _n, r in rows)
    if unparsed:
        lines.append("")
        lines.append(f"NOTE: {unparsed} measurement(s) had no readable value (span preview cut, or "
                     "a failed evaluation) and are counted in NEITHER direction.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default=DEFAULT_ROOT)
    ap.add_argument("--json", action="store_true", help="emit the rows instead of the reading")
    args = ap.parse_args(argv)
    rows = scan(args.root)
    if args.json:
        print(json.dumps([{"run": n, **r} for n, r in rows], indent=1))
        return 0
    if not rows:
        print(f"no writing-phase sessions found under {args.root}")
        return 0
    print(report(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
