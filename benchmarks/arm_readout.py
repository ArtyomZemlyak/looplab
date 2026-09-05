#!/usr/bin/env python3
"""The probe-cap arm's analysis, written down BEFORE the numbers and executable.

WHY THIS IS A FILE AND NOT A PARAGRAPH. §190 registered the design — twelve batches, two probes per
arm, exact stratified permutation, one-sided, α = 0.05 — and since then the rules for what counts as
a probe have accumulated one incident at a time: `freeB3` excluded at $1.1056 (§213.1), `capB4`
carrying the label with its cap unreached but recorded (§227, §243), pauses at the ceiling that are
really endings (§228), and the per-batch medians that turned out to be a fragile statistic (§254).
Every one of those was decided for a reason at the time, and every one is a degree of freedom that
could be re-decided afterwards to suit whatever number arrives. Written as code, run against a
corpus that is not yet complete, they cannot be.

THE POPULATION. A probe enters the arm if, and only if:
  * its own `config.snapshot.json` records the cap its label claims — 12 for treated, 0 for control
    (§243: `capB4` is in on this rule, and behaviour alone could never have said so);
  * it ended, meaning a `run_finished` event OR a pause at ≥ 99 % of its budget (§228: sixteen of the
    corpus's runs record a normal ending as a Developer crash, and the fix cannot reach the probes
    already recorded);
  * its metered spend is at most $1.05. This is the §213.1 criterion, written before any contrast was
    read, and `freeB3` at $1.1056 is the one probe it excludes.

THE STATISTIC is the sum over batches of (mean treated TEST − mean control TEST), and the test is the
exact permutation over within-batch relabellings — C(4,2) = 6 per batch, 6^12 enumerable — one-sided
in the direction the design predicts. `arm_power.py` computes the same null.

WHAT A NULL MEANS. §234 measured the power at **0.77 against a +44-point effect** on the corpus as it
stands (sd 63.7), so a p above 0.05 says "no effect of that size was detected at power 0.77". It does
NOT say capping does not help, and §190's own falsification clause says so in the same words.

WHAT THIS REFUSES TO DO. It will not read a partial arm. Fewer than `--batches` complete batches and
it prints what is missing and exits 2 — because an interim look at the outcome is the one thing the
design forbids, and a tool that will do it on request is a tool that will be asked.

Usage:
    arm_readout.py --batches 12 [--alpha 0.05] [--max-spend 1.05]
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import json
import statistics
import sys
from itertools import combinations, product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_fidelity  # noqa: E402
import events_read  # noqa: E402

ROOT = arm_fidelity.DEFAULT_ROOT
CEILING_SHARE = arm_fidelity.CEILING_SHARE

# The arm as launched, in order. Extending this list is how a batch joins the readout.
BATCHES = [
    (["capA2", "capB2"], ["freeA2", "freeB2"]),
    (["capA3", "capB3"], ["freeA3", "freeB4"]),
    (["capA4", "capB4"], ["freeA4", "freeB5"]),
    (["capA5", "capB5"], ["freeA5", "freeB6"]),
    (["capA6", "capB6"], ["freeA6", "freeB7"]),
    (["capA7", "capB7"], ["freeA7", "freeB8"]),
    (["capA8", "capB8"], ["freeA8", "freeB9"]),
    (["capA9", "capB9"], ["freeA9", "freeB10"]),
    (["capA10", "capB10"], ["freeA10", "freeB11"]),
    # FROM HERE THE LANE<->LABEL MAPPING IS SWAPPED (§266). Batches 1-9 put 17 of 18 treated probes
    # on lanes 0-10 and 11-21 and 18 of 19 controls on 22-32 and 33-43, which makes the label and
    # the lane the same variable. Six sittings of the reference-against-itself ruler could not show
    # the lanes differ (per-sitting contrast positive in 4 of 6, sign test p = 0.34) but also could
    # not exclude ~3 %, so batches 10-12 run treatment on 22-32 and 33-43 and control on 0-10 and
    # 11-21. Registered here, before any contrast was read, so the swap cannot be chosen by outcome.
    (["capA11", "capB11"], ["freeA11", "freeB12"]),
    (["capA12", "capB12"], ["freeA12", "freeB13"]),
    (["capA13", "capB13"], ["freeA13", "freeB14"]),   # the twelfth and last
]


# A PROBE DELIBERATELY OUTSIDE THE ARM, AND WHY -- so nobody helpfully adds it back.
EXCLUDED = {
    "freeB3": "§213: the meter already showed it at $1.0308 when I resumed it, and the per-process "
              "accountant let it run on to $1.1056. Excluded at a criterion written down before any "
              "contrast had been read.",
}


def design_problems(batches, root: str | None = None) -> list:
    """What is wrong with the DESIGN, before any number is computed from it.

    `BATCHES` is hand-maintained and has been appended to five times. A name repeated across two
    batches counts one probe twice; a batch that is not 2+2 breaks the within-batch permutation the
    whole test conditions on; a name with no tree is a typo that will read as an incomplete batch
    and be blamed on the bench. None of that is visible in the output -- it changes the number
    silently, which is the one failure mode $48 cannot afford.
    """
    said = []
    names = [n for treat, control in batches for n in list(treat) + list(control)]
    seen: dict = {}
    for i, (treat, control) in enumerate(batches, 1):
        if len(treat) != 2 or len(control) != 2:
            said.append(f"batch {i} is {len(treat)}+{len(control)}, not 2+2 -- the within-batch "
                        "permutation the test conditions on is not the one that was registered")
        for name in list(treat) + list(control):
            if name in seen:
                said.append(f"{name} appears in batch {seen[name]} AND batch {i} -- one probe "
                            "counted twice")
            else:
                seen[name] = i
    if root:
        for name in names:
            if not os.path.isdir(f"{root}/{name}"):
                said.append(f"{name} is in the design with no probe tree under {root}")
        try:
            present = {d for d in os.listdir(root)
                       if os.path.isdir(f"{root}/{d}") and d.startswith(("cap", "free"))}
        except OSError:
            present = set()
        for name in sorted(present - set(names) - set(EXCLUDED)):
            said.append(f"{name} looks like an arm probe, is in no batch, and is not in EXCLUDED "
                        "-- either it belongs in the design or its exclusion needs writing down")
    return said


def spend(name: str) -> float:
    total = 0.0
    for path in sorted(glob.glob(f"{ROOT}/{name}/runs/*/run/events.jsonl")):
        for event in events_read.iter_events(path):
            if event.get("type") != "llm_usage":
                continue
            data = event.get("data")
            if isinstance(data, dict):
                try:
                    total += max(0.0, float(data.get("cost") or 0.0))
                except (TypeError, ValueError):
                    pass
    return total


def score(name: str):
    """`(value, None)`, or `(None, why not)`. The last unguarded step from disk to verdict.

    IT WAS READING `speedup` AND NOTHING ELSE. Two things sit beside it in the same file and both
    change what that number means:

      * `subset`. Every node in a run is evaluated on TRAIN and the champion is scored ONCE on TEST
        (§84). A train figure in this field is a different measurement on a different split, and
        arithmetic over the two is meaningless -- but it is a float, and it would go straight into
        the statistic without a word. Measured 2026-09-05: all 44 finished arm probes and all 136
        `final.json` on this box say `test`, so this guards a hole rather than closing a leak.
      * `superseded`. §55 recorded two `final.json` carrying it, both from a scoring pass that took
        `solver.py` from the wrong path -- a score for a solver that is not the champion.

    Neither was checked, and neither would have shown in the output.
    """
    try:
        with open(f"{ROOT}/{name}/final.json", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, f"no readable final.json ({type(exc).__name__})"
    if not isinstance(record, dict):
        return None, "final.json is not an object"
    if record.get("superseded"):
        return None, (f"final.json is marked superseded ({record.get('superseded')}) -- §55: a "
                      "score for a solver that is not the champion")
    subset = record.get("subset")
    if subset != "test":
        return None, (f"final.json records subset {subset!r}, not 'test' -- every node ran on "
                      "TRAIN and the champion is scored once on TEST (§84); the two are different "
                      "measurements and averaging across them means nothing")
    value = record.get("speedup")
    if not isinstance(value, (int, float)) or value <= 0:
        return None, f"speedup is {value!r}"
    return float(value), None


def admit(name: str, arm: str, max_spend: float, budget: float = 1.0):
    """`(score, None)` if this probe enters the arm, else `(None, why not)`."""
    want = 12 if arm == "treat" else 0
    if arm_fidelity.assigned_cap(ROOT, name) != want:
        return None, f"config records {arm_fidelity.assigned_cap(ROOT, name)}, not {want}"
    paid = spend(name)
    ended = arm_fidelity._run_finished(ROOT, name) or paid >= budget * CEILING_SHARE
    if not ended:
        return None, f"has not ended (${paid:.4f})"
    if paid > max_spend:
        return None, f"spent ${paid:.4f}, over the ${max_spend:.2f} ceiling"
    got, why = score(name)
    if got is None:
        return None, why
    return got, None


def stratified_p(batches, alternative_positive: bool = True) -> float:
    obs = sum(statistics.mean(t) - statistics.mean(c) for t, c in batches)
    per = []
    for t, c in batches:
        pool = list(t) + list(c)
        per.append([([pool[i] for i in idx], [x for j, x in enumerate(pool) if j not in idx])
                    for idx in combinations(range(len(pool)), len(t))])
    total = ge = 0
    for combo in product(*per):
        val = sum(statistics.mean(a) - statistics.mean(b) for a, b in combo)
        if (val >= obs) if alternative_positive else (val <= obs):
            ge += 1
        total += 1
    return ge / total


# The lanes treatment ran on for batches 1-9. §266 swapped it for 10-12 so the confound became
# estimable; this is the mapping being compared against, not a claim that it is the right one.
MAPPING_A_TREAT_LANES = ("0-10,48-58", "11-21,59-69")


def mapping_of(treat, root: str) -> str:
    """"A" if this batch ran treatment on the original pair of lanes, "B" if on the swapped pair.

    Read from each probe's own INSTRUMENT.txt, which records the lane at launch.
    """
    lanes = set()
    for name in treat:
        try:
            text = open(f"{root}/{name}/INSTRUMENT.txt", encoding="utf-8",
                        errors="replace").read()
        except OSError:
            return "?"
        got = re.search(r"^lane:\s+(\S+)", text, re.M)
        if not got:
            return "?"
        lanes.add(got.group(1))
    if not lanes:
        return "?"
    if lanes <= set(MAPPING_A_TREAT_LANES):
        return "A"
    if not (lanes & set(MAPPING_A_TREAT_LANES)):
        return "B"
    # MIXED IS NOT UNREADABLE. Batch 1 ran treatment on 0-10 and 22-32 and control on 11-21 and
    # 33-43 -- one lane from each pair on each side. That batch is internally balanced against the
    # lane pairs and so carries no confound at all; calling it "?" would read as a failure to
    # measure something that was in fact measured and came out even.
    return "mixed"


def lane_split(ready, root: str):
    """The treatment contrast computed SEPARATELY under each lane-to-label mapping.

    REGISTERED BEFORE THE OUTCOME WAS SEEN, which is the only time this is worth writing. §266 found
    that 17 of 18 treated probes had run on the same pair of lanes, making "treated" and "ran on
    lanes A or B" one variable with two names, and could not exclude a lane effect of about 3 %.
    Batches 10-12 swapped the mapping precisely so the two could be told apart.

    If the lanes are exchangeable, the contrast is the same under both mappings. If they are not,
    the lane effect adds to the treatment effect under one mapping and subtracts under the other, so
    the DIFFERENCE of the two contrasts is twice the lane effect and the interaction test sees it.
    A small group (three batches) makes this weak, not meaningless: it is reported with its own n,
    and it is a check on the headline number rather than a replacement for it.
    """
    groups: dict = {}
    for i, treat_scores, control_scores in ready:
        key = mapping_of(BATCHES[i - 1][0], root)
        groups.setdefault(key, []).append((treat_scores, control_scores))
    out = {}
    for key, rows in sorted(groups.items()):
        contrast = statistics.mean([statistics.mean(t) - statistics.mean(c) for t, c in rows])
        out[key] = {"n": len(rows), "contrast": contrast, "rows": rows}
    return out


def interaction_p(groups, draws: int = 20000, seed: int = 190) -> float | None:
    """How often relabelling WITHIN each batch produces a gap between the two mappings this large.

    Sampled rather than enumerated: the exact space is 6^12, which is 2.2 billion.
    """
    if set(groups) != {"A", "B"}:
        return None
    import random
    rng = random.Random(seed)
    obs = abs(groups["A"]["contrast"] - groups["B"]["contrast"])
    rows = {k: groups[k]["rows"] for k in ("A", "B")}
    hits = 0
    for _ in range(draws):
        means = {}
        for key in ("A", "B"):
            per = []
            for t, c in rows[key]:
                pool = list(t) + list(c)
                rng.shuffle(pool)
                k = len(t)
                per.append(statistics.mean(pool[:k]) - statistics.mean(pool[k:]))
            means[key] = statistics.mean(per)
        if abs(means["A"] - means["B"]) >= obs:
            hits += 1
    return hits / draws


RECORD_DEFAULT = Path(__file__).resolve().parent / "algotune" / ".arm_readout_taken"


def record_readout(path, payload: dict) -> None:
    """Write the readout where it survives the box.

    `/var/tmp` is ephemeral and has been wiped once, taking 37 unpushed commits and ~69 probe runs
    with it. The readout is the deliverable of $48 and, until now, existed only as text on a
    terminal. This writes it into the repo, which is pushed.

    The file is also the marker `probe_summary.EMBARGO_LIFTED` looks for, and that is deliberate:
    §190 lifts exactly when the readout has been RECORDED, not when somebody decides it has been
    taken. One act, one artefact, and the artefact is the licence.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)     # atomic: a torn marker would lift the embargo over half a readout


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batches", type=int, default=12)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--max-spend", type=float, default=1.05)
    ap.add_argument("--record", nargs="?", const=str(RECORD_DEFAULT), default=None,
                    help="write the readout here (default: the embargo marker itself)")
    args = ap.parse_args(argv)

    broken = design_problems(BATCHES, ROOT)
    for line in broken:
        print(f"  DESIGN: {line}")
    if broken:
        print("\nREFUSING TO READ THE ARM: the design itself is malformed, and a statistic computed "
              "over it would be wrong in a way its own output could not show.")
        return 2

    ready, missing = [], []
    for i, (treat, control) in enumerate(BATCHES, 1):
        rows, why = {}, []
        for name, arm in [(n, "treat") for n in treat] + [(n, "control") for n in control]:
            got, reason = admit(name, arm, args.max_spend)
            if reason:
                why.append(f"{name}: {reason}")
            else:
                rows[name] = got
        if len(rows) == 4:
            ready.append((i, [rows[n] for n in treat], [rows[n] for n in control]))
        else:
            missing.append((i, why))

    print(f"{len(ready)} complete batches of the {args.batches} the design registered")
    for i, why in missing:
        print(f"  batch {i} incomplete: " + "; ".join(why))
    if len(ready) < args.batches:
        print(f"\nREFUSING TO READ THE ARM at {len(ready)} of {args.batches} batches. An interim look "
              "at the outcome is the one thing §190 forbids, and this tool is not the exception.")
        return 2

    batches = [(t, c) for _, t, c in ready[:args.batches]]
    obs = sum(statistics.mean(t) - statistics.mean(c) for t, c in batches)
    p = stratified_p(batches)
    print(f"\nsum of within-batch mean differences: {obs:+.2f}")
    print(f"exact stratified one-sided permutation p = {p:.4f} (alpha {args.alpha})")
    groups = lane_split(ready[:args.batches], ROOT)
    if len(groups) > 1:
        print("\n  the same contrast under each lane-to-label mapping (§266, registered before the "
              "outcome was seen):")
        for key, got in sorted(groups.items()):
            note = ("  (one lane from each pair on each side -- internally balanced)"
                    if key == "mixed" else
                    "  (lanes unreadable)" if key == "?" else "")
            print(f"    mapping {key}: {got['contrast']:+.2f} over {got['n']} batch(es){note}")
        pi = interaction_p(groups)
        if pi is not None:
            print(f"    two-sided sampled interaction p = {pi:.4f} -- a lane effect would add to "
                  "the contrast under one mapping and subtract under the other")
    if args.record:
        record_readout(args.record, {
            "taken": "by arm_readout.py --record",
            "batches": args.batches, "alpha": args.alpha, "max_spend": args.max_spend,
            "design": [[list(t), list(c)] for t, c in BATCHES[:args.batches]],
            "excluded": EXCLUDED,
            "scores": [{"batch": i, "treat": t, "control": c} for i, t, c in ready[:args.batches]],
            "sum_of_within_batch_mean_differences": obs,
            "stratified_one_sided_p": p,
            "lane_split": {k: {"n": v["n"], "contrast": v["contrast"]}
                           for k, v in groups.items()},
            "interaction_p": interaction_p(groups),
            "verdict": "reject" if p <= args.alpha else "do not reject",
        })
        print(f"  recorded to {args.record}")
    print("REJECT the null" if p <= args.alpha else
          f"do NOT reject: no effect of the registered size was detected at power 0.77 (§234). "
          "That is not the same as 'capping does not help'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
