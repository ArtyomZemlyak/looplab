"""HOW FAR DID THIS CANDIDATE MOVE FROM THE SEED PROGRAM — the run's own displacement diagnostic.

THE DEFECT THIS CLOSES (doc 52 row 31, `no-distance-from-seed-signal`; doc 17 §11). A run reports
metrics per node and, since 2026-09-07, what KIND of edit each parent->child STEP made
(`tools/node_diff.py` + `looplab edit-types`). Nothing said how far the thing being measured had
travelled from where it started. That is the question MLGym's finding is phrased in — "models
usually improve by finding better hyperparameters" — and it is unanswerable from a per-step table:
twelve hyperparameter steps and one rewrite look alike in a per-step tally, and are opposite runs.

DISPLACEMENT, NOT PATH LENGTH, and the difference is the point. This diffs a node against its
LINEAGE ROOT — the seed program it descends from — in ONE comparison, so a node that changed a line
and changed it back has moved zero even though `edit-types` recorded two edits. The path is not
discarded: `tools/node_diff.py::reintroduced_lines` walks the same first-parent chain and counts the
lines this node re-adds that its own ancestry deleted, so a big path over a small displacement is
visible as a re-introduction share rather than being averaged away. First-parent, because that is
what a lineage IS here (`node_diff.lineage`); an ensemble's second parent is a different descent.

THE VOCABULARY IS `node_diff.EDIT_TYPES` AND NOTHING ELSE. A second edit taxonomy beside the first
would be two answers to one question with no way to tell which a number came from — the exact defect
`docs/BACKLOG.md` §0.8 measured elsewhere. What this module adds is a PARTITION of that closed
vocabulary into the three bands the question needs (tuning / structural / cosmetic) plus the
vocabulary's own residue, asserted total at import so a tenth edit type cannot land uncounted.

IT DECIDES NOTHING. No selection, no gate, no proposal cue reads it; the only caller is
`looplab seed-distance`, which prints it. That is deliberate and not a staging post: "novel != good"
is doc 17 §11's own warning about exactly this family of signal, and a distance maximised is a run
rewarded for churn. It is an instrument for the operator, and it stays one until something measures
that acting on it helps.
"""
from __future__ import annotations

from typing import Optional

from looplab.tools.node_diff import (EDIT_TYPES, classify_edits, lineage, node_record,
                                     reintroduced_lines)

# The three bands, and the residue. The partition is the only judgement this module makes on top of
# the shared vocabulary, so it is stated once, here, rather than inside a renderer.
#
#   TUNING       — the knobs. `hyperparameter` is a literal assignment; `call_argument` is the SAME
#                  knob written as a keyword (`Adam(model.parameters(), lr=1e-4)` is the canonical
#                  tuning line and classifies as `call_argument`). Splitting them would put the same
#                  edit in two bands depending on where the author typed it.
#   STRUCTURAL   — what the program IS: its dependencies, its shape, its path, and where its data
#                  comes from. `data_io` is here and not in tuning because a changed read is a
#                  changed experiment (it is where leakage lives), not a changed setting.
#   COSMETIC     — what a reader sees and the machine does not. `logging` is here on the vocabulary's
#                  own terms ("observation, not behaviour"): a run that added forty print statements
#                  has not moved, and counting it as movement is how a churn signal lies.
#   `other`      — the vocabulary's named residue, counted in the total and claimed by no band.
TUNING_TYPES: tuple[str, ...] = ("hyperparameter", "call_argument")
STRUCTURAL_TYPES: tuple[str, ...] = ("import", "definition", "control_flow", "data_io")
COSMETIC_TYPES: tuple[str, ...] = ("comment", "whitespace", "logging")
RESIDUE_TYPES: tuple[str, ...] = ("other",)

# TOTAL over the shared vocabulary, checked at import rather than in a test: a tenth `EDIT_TYPES`
# entry lands in no band, and a band-share denominator that silently drops it reads as a run that
# moved less than it did. The one place this can be caught for free is where the bands are written.
_PARTITION = TUNING_TYPES + STRUCTURAL_TYPES + COSMETIC_TYPES + RESIDUE_TYPES
assert sorted(_PARTITION) == sorted(EDIT_TYPES), (
    "the seed-distance bands no longer partition node_diff.EDIT_TYPES: "
    f"{sorted(set(EDIT_TYPES) ^ set(_PARTITION))}")


def _band_total(counts: dict, band: tuple[str, ...]) -> int:
    return sum(int(counts.get(kind, 0) or 0) for kind in band)


def seed_distance(state, node_id: int, *, with_reintroductions: bool = True) -> Optional[dict]:
    """How far one node moved from the seed program it descends from, or None if there is no node.

    `None` means "this run has no such node" — a different answer from "it did not move", and the
    two must never render the same (`node_diff`'s headline property, one level down).

    `recoverable` is False when either file set is missing from the record: a node whose files were
    never recorded must not read as a node identical to its seed. Every count is then 0 and the
    caller is expected to say so rather than print a zero.
    """

    chain = lineage(state, node_id)
    if not chain:
        return None
    seed_id = chain[0]
    subject = node_record(state, node_id)
    seed = node_record(state, seed_id)
    if subject is None:
        return None
    row = {
        "node_id": node_id,
        "seed_node_id": seed_id,
        # The number of first-parent steps taken, not the number of nodes in the chain: a seed is at
        # depth 0 from itself.
        "depth": len(chain) - 1,
        "metric": subject.get("metric"),
        "seed_metric": None if seed is None else seed.get("metric"),
        "recoverable": False,
        "files": 0,
        "added": {}, "removed": {},
        "lines": 0, "substantive": 0, "tuning": 0, "structural": 0, "cosmetic": 0, "other": 0,
        "tuning_share": None,
        "reintroduced": 0, "reintroduced_added": 0,
    }
    if seed is None:
        return row
    if seed_id == node_id:
        # A seed IS its own reference. It is recoverable and its distance is zero — which is a fact,
        # unlike the zero an unreadable node would report.
        row["recoverable"] = True
        return row

    counts = classify_edits(seed, subject)
    if not counts.get("recoverable"):
        return row
    added, removed = counts["added"], counts["removed"]
    tuning = _band_total(added, TUNING_TYPES) + _band_total(removed, TUNING_TYPES)
    structural = _band_total(added, STRUCTURAL_TYPES) + _band_total(removed, STRUCTURAL_TYPES)
    cosmetic = _band_total(added, COSMETIC_TYPES) + _band_total(removed, COSMETIC_TYPES)
    other = _band_total(added, RESIDUE_TYPES) + _band_total(removed, RESIDUE_TYPES)
    substantive = tuning + structural + other
    row.update({
        "recoverable": True,
        "files": int(counts.get("files", 0) or 0),
        "added": dict(added), "removed": dict(removed),
        "lines": tuning + structural + cosmetic + other,
        "substantive": substantive,
        "tuning": tuning, "structural": structural, "cosmetic": cosmetic, "other": other,
        # Over the SUBSTANTIVE lines, never the total: a node that moved four tuning lines and
        # reformatted two hundred is a tuning move, and dividing by the reformatting would say the
        # opposite. `None` and not 0.0 when nothing substantive changed — "no tuning" and "nothing
        # to take a share of" are different answers.
        "tuning_share": (tuning / substantive) if substantive else None,
    })
    if with_reintroductions:
        # The PATH beside the displacement: lines this node adds that its own lineage deleted. A
        # large re-introduction count under a small distance is a lineage cycling in place, which is
        # the field's own measured failure mode (~30 % of added lines across 121 EvoTrace runs).
        cycling = reintroduced_lines(state, node_id)
        row["reintroduced"] = int(cycling.get("count", 0) or 0)
        row["reintroduced_added"] = int(cycling.get("added", 0) or 0)
    return row


def run_seed_distances(state) -> dict:
    """Every node's displacement from its seed, plus what the run's own metrics say about it.

    `improved_tuning_share` / `regressed_tuning_share` are the pair the MLGym sentence is about: the
    mean tuning share of the nodes that BEAT their seed against those that did not. It is an
    observation over one run's handful of nodes and is not a claim about anything; the renderer says
    so beside the number rather than letting the reader supply the confidence.
    """

    # COST, stated because it is quadratic-ish and this is a post-run instrument rather than a loop
    # step: one seed diff per node, plus one lineage walk per node for the re-introduction count,
    # each bounded by `reintroduced_lines`' own `max_lines`. A run is tens of nodes; nothing here is
    # on a path that holds a GPU.
    nodes = getattr(state, "nodes", None) or {}
    minimize = str(getattr(state, "direction", "min") or "min").lower() != "max"
    rows: list[dict] = []
    seeds = 0
    unreadable = 0
    for node_id in sorted(nodes):
        row = seed_distance(state, node_id)
        if row is None:
            continue
        if row["seed_node_id"] == node_id:
            seeds += 1
            continue
        if not row["recoverable"]:
            # A missing file set is not a node that made no edit — it is a node this run cannot be
            # asked about, and it is COUNTED so the roll-up's own coverage is visible.
            unreadable += 1
            continue
        metric, seed_metric = row["metric"], row["seed_metric"]
        gain = None
        if isinstance(metric, (int, float)) and isinstance(seed_metric, (int, float)):
            gain = (seed_metric - metric) if minimize else (metric - seed_metric)
        row["gain"] = gain
        rows.append(row)

    scored = [row for row in rows if row["gain"] is not None and row["tuning_share"] is not None]
    improved = [row for row in scored if row["gain"] > 0]
    regressed = [row for row in scored if row["gain"] <= 0]

    def _mean_share(group: list[dict]) -> Optional[float]:
        return (sum(row["tuning_share"] for row in group) / len(group)) if group else None

    return {
        "direction": "min" if minimize else "max",
        "rows": rows,
        "seeds": seeds,
        "measured": len(rows),
        "unreadable": unreadable,
        "scored": len(scored),
        "improved": len(improved),
        "regressed": len(regressed),
        "improved_tuning_share": _mean_share(improved),
        "regressed_tuning_share": _mean_share(regressed),
        "max_distance": max((row["substantive"] for row in rows), default=0),
    }


def render_seed_distances(report: dict) -> list[str]:
    """The instrument's lines. Pure, so the CLI stays a fold and a loop over these."""

    rows = report["rows"]
    out = [
        f"distance from the seed program over {report['measured']} descendant node(s) "
        f"({report['seeds']} seed(s) are their own reference; direction={report['direction']})",
    ]
    if report["unreadable"]:
        out.append(f"  {report['unreadable']} node(s) NOT measured — a file set is missing from the "
                   "record, which is not the same as a node that never moved.")
    if not rows:
        out.append("  no node in this run has a readable seed to be measured against. "
                   "(A run of seeds only has no distance to travel.)")
        return out
    out.append(f"{'node':>5}{'seed':>6}{'depth':>7}{'files':>7}{'lines':>7}"
               f"{'tuning':>8}{'struct':>8}{'cosmetic':>10}{'tuning%':>9}{'re-added':>10}"
               f"{'gain':>12}")
    for row in rows:
        share = "n/a" if row["tuning_share"] is None else f"{100 * row['tuning_share']:.0f}%"
        # `.get`: a row handed here straight from `seed_distance` carries no metric comparison at
        # all, and a KeyError in a renderer is a worse answer than "this run cannot say".
        gain = "—" if row.get("gain") is None else f"{row['gain']:+.6g}"
        out.append(f"{row['node_id']:>5}{row['seed_node_id']:>6}{row['depth']:>7}"
                   f"{row['files']:>7}{row['lines']:>7}{row['tuning']:>8}{row['structural']:>8}"
                   f"{row['cosmetic']:>10}{share:>9}{row['reintroduced']:>10}{gain:>12}")
    out.append("  lines = added + removed against the SEED, so a change and its undo cancel; "
               "`re-added` is what this node's own lineage had already deleted.")
    improved, regressed = report["improved_tuning_share"], report["regressed_tuning_share"]
    if improved is None and regressed is None:
        out.append("  no node carries both its own and its seed's metric, so this run says nothing "
                   "about which KIND of movement paid. That is not evidence tuning does not pay.")
        return out
    out.append(
        "  tuning share of the movement, "
        + ("improved: n/a" if improved is None
           else f"improved ({report['improved']} node(s)): {100 * improved:.0f}%")
        + ("; not improved: n/a" if regressed is None
           else f"; not improved ({report['regressed']} node(s)): {100 * regressed:.0f}%"))
    out.append("  MLGym reports that models usually improve by finding better hyperparameters. "
               f"{report['scored']} node(s) is an observation about this run, not a test of that.")
    return out
