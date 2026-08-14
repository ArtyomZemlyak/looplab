"""The process diagram's GEOMETRY, which nothing checked until 2026-08-13.

`docs/infographic/agent-architecture.html` is data-driven — a `B` block map and an `E` edge list in
its inline script — and `CLAUDE.md` tells every agent to edit that data rather than hand-placed SVG.
Eight branches did exactly that on one day, and three separate merge defects got through review
because the file is one 300-line script that no test evaluated:

* two branches independently paid for new content out of the SAME 14px of a neighbour's slack, so a
  textual union either dropped a bullet or overflowed a box;
* a union left DUPLICATE box definitions (`e_ir`, `e_sal`, `e_mon`, `e_stg`), which render twice and
  silently move everything below them;
* collapsing those duplicates deleted a line that carried a SECOND box (`e_ll`), losing it entirely.

None of that is visible in a diff, and `test_architecture_infographic.py` only checks that certain
labels are present. This module evaluates the script the browser runs and measures the result.

The baseline is now ZERO, and the structural defect it used to pin is gone. It was real: the
EVALUATE column at x=1000 grows downward every time an engine feature is documented, while `HY`,
`WY`, `GY`, `TY`, the spine's `y` and `SY` were hand-picked literals, so the column grew through the
rows below it — 13 pairs before the 2026-08-13 content landed and 16 after. Fixed 2026-08-14 by
making those constants MEASURE their datum (`below(gap)` in the diagram = the deepest bottom placed
so far) instead of naming it, so every row follows the column automatically. The rows moved down
460-588px, all of it derived.

Two things the old baseline asserted were simply wrong, and are worth not re-learning:

* the nine "intentional" `spine` container overlaps were six, and they were NOT the `sd_*` column.
  `sd_*` sits ~100px BELOW the spine and has never intersected it; the six were the memory-TIER row
  (`mA`..`sim`, whose `mC` is 108 tall against its siblings' 72) growing into a spine `y` that was
  still the literal 2214. A container overlap was assumed and never measured.
* the "564px" figure recorded here was not reproducible. Re-derived against the same content: the
  first row below the column (`kanban`/`h_spec` at `HY-8`) needed 460px to clear `e_dr`'s bottom of
  2072 with a 24px gutter, and the rows below it needed 492 and 588 because two of the gutters they
  inherited were themselves negative.

So: zero is the number, and any non-zero result is a real collision. Do not re-pin it upward to make
a change green — a box that intersects another is drawn ON TOP of it and hides its text.
"""
from __future__ import annotations

from pathlib import Path
import json
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
DIAGRAM = ROOT / "docs" / "infographic" / "agent-architecture.html"

# Measured 2026-08-14, after the row constants were made to derive their own datum. ZERO remaining
# pairs: no box in the diagram intersects any other, container or otherwise, and no pair was left
# unseparated. Moving this is part of changing the layout: RAISING it means a new overlap shipped and
# the fix is to pay for the growth (rows below a growing column now move themselves — see the CARD
# row's comment in the diagram), never to re-pin. There is no longer any intentional overlap to
# exempt, which is why `_SPINE_CONTAINER` below survives only as the detail formatter's filter.
_EXPECTED_INTERSECTIONS = 0
_SPINE_CONTAINER = "spine"


def _node() -> str:
    """A Node new enough to run the extracted module, or skip.

    The system Node here is 18 and cannot; 22.x is staged under /tmp per the environment note. This
    is a SKIP rather than a failure because the property is about the diagram, not about the box.
    """
    for candidate in (shutil.which("node"), "/tmp/node-v22.20.0-linux-x64/bin/node"):
        if not candidate or not Path(candidate).exists():
            continue
        out = subprocess.run([candidate, "--version"], capture_output=True, text=True)
        major = re.match(r"v(\d+)", out.stdout.strip())
        if major and int(major.group(1)) >= 20:
            return candidate
    pytest.skip("needs Node >= 20 to evaluate the diagram's inline script")


def _boxes() -> dict[str, dict]:
    """Evaluate the diagram's own geometry code and return its block map.

    Evaluating rather than regex-scraping is the whole point: `stack()` computes each item's `y`
    from the heights above it, so a regex over `box(...)` calls cannot see where a block actually
    lands, which is exactly the failure mode being guarded.
    """
    node = _node()
    script = """
    const fs = require('fs');
    const html = fs.readFileSync(process.argv[1], 'utf8');
    const m = html.match(/<script[^>]*>([\\s\\S]*?)<\\/script>/);
    if (!m) { console.error('no inline script'); process.exit(2); }
    const lines = m[1].split('\\n');
    let cut = lines.findIndex(l => l.includes('svg.setAttribute("viewBox"'));
    if (cut < 0) cut = lines.length;
    const shim = 'const document={getElementById:()=>({}),' +
                 'createElementNS:()=>({setAttribute(){},appendChild(){},style:{}})};\\n';
    const B = new Function(shim + lines.slice(0, cut).join('\\n') + '\\n; return B;')();
    process.stdout.write(JSON.stringify(B));
    """
    out = subprocess.run([node, "-e", script, str(DIAGRAM)], capture_output=True, text=True)
    assert out.returncode == 0, f"the diagram's geometry did not evaluate:\n{out.stderr[-2000:]}"
    return json.loads(out.stdout)


def _overlap(a: dict, b: dict) -> tuple[int, int]:
    return (min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]),
            min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))


def test_every_diagram_box_is_defined_exactly_once():
    """A duplicate renders twice AND shifts everything below it — invisible in a diff."""
    text = DIAGRAM.read_text(encoding="utf-8")
    ids = re.findall(r'id:"([a-z_0-9]+)"', text)
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, (
        f"box(es) defined more than once: {duplicates}. A textual merge of two branches that both "
        f"edited the same box leaves both copies; collapse them into one carrying both bullet sets.")


def test_diagram_box_overlaps_do_not_grow():
    boxes = _boxes()
    assert len(boxes) >= 80, "the block map collapsed; the extraction is measuring nothing"
    names = sorted(boxes)
    hits = [(a, b) for i, a in enumerate(names) for b in names[i + 1:]
            if all(d > 0 for d in _overlap(boxes[a], boxes[b]))]
    # EVERY pair, `spine` included. The old formatter filtered the spine out as "the container",
    # which would now report a count with an empty list — and the spine growing into the tier row
    # above it is exactly one of the collisions this file was mis-crediting to that exemption.
    detail = "\n  ".join(
        f"{a} x {b}: {_overlap(boxes[a], boxes[b])[0]}x{_overlap(boxes[a], boxes[b])[1]}px"
        for a, b in hits)
    assert len(hits) == _EXPECTED_INTERSECTIONS, (
        f"the diagram now has {len(hits)} overlapping box pairs, pinned at "
        f"{_EXPECTED_INTERSECTIONS}:\n  {detail}\n"
        f"A box that intersects another is drawn over it and hides its text. The rows below the "
        f"loop-stage columns derive their own y from `below(gap)`, so GROWING a column is free and "
        f"an overlap here means something the derivation cannot see: two boxes in the SAME row "
        f"(pay for the width/height out of that row), or a new row declared out of top-to-bottom "
        f"order (so `below()` measured a datum that did not exist yet).")


def test_the_spine_is_the_only_intentional_container():
    """The `sd_*` column is drawn to read as sitting INSIDE the event spine — horizontally.

    This used to be offered as the reason nine intersections were legitimate. It never was: the
    spine ends ~100px above `SY` and the two rows do not touch, so the containment is a horizontal
    alignment (every `sd_*` box within the spine's x-span, so the band reads as one object) and not
    a nesting. It is still worth pinning, because the alignment is what the two rows MEAN together
    and a re-layout that widened `sd_*` past the spine would break that silently — but it explains
    no overlap, and the baseline above is zero.
    """
    boxes = _boxes()
    assert _SPINE_CONTAINER in boxes, "the spine container is gone; the baseline above is stale"
    spine = boxes[_SPINE_CONTAINER]
    inside = [n for n, b in boxes.items()
              if n != _SPINE_CONTAINER and n.startswith("sd_")
              and b["x"] >= spine["x"] and b["x"] + b["w"] <= spine["x"] + spine["w"]]
    assert len(inside) >= 8, (
        "the sd_* column no longer sits inside the spine; if that column moved, the container "
        "overlaps in the baseline are no longer the ones this test explains")
