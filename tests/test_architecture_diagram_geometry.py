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

The intersection baseline is a LITERAL and is deliberately not zero: the diagram ships with real
overlaps, and pinning the number is what makes a new one a red test instead of a rendering surprise.
Nine of them are the `spine` container the `sd_*` column sits inside, which is intentional. The rest
are a structural defect that predates this file: the EVALUATE column at x=1000 grows downward while
`kanban`, `h_prio` and the `w_*`/`g_*` rows sit at fixed `y`, so the column crosses them. Measured
2026-08-13: closing it needs the rows below moved 564px, which is a re-layout to do with the rendered
page in view, not a merge-time edit. Until then this pin stops it getting quietly worse.
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

# Measured on 2026-08-13 after the eight-branch integration. Moving it is part of changing the
# layout: RAISING it means a new overlap shipped, LOWERING it means one was fixed and the pin should
# follow so the next regression is still caught.
_EXPECTED_INTERSECTIONS = 19
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
    detail = "\n  ".join(
        f"{a} x {b}: {_overlap(boxes[a], boxes[b])[0]}x{_overlap(boxes[a], boxes[b])[1]}px"
        for a, b in hits if _SPINE_CONTAINER not in (a, b))
    assert len(hits) == _EXPECTED_INTERSECTIONS, (
        f"the diagram now has {len(hits)} overlapping box pairs, pinned at "
        f"{_EXPECTED_INTERSECTIONS}. Non-container overlaps:\n  {detail}\n"
        f"If you grew a block, it pushed its column into a neighbouring row — pay for the height "
        f"out of that block's own slack or move the rows below, and re-measure. If you FIXED one, "
        f"lower the pin in the same change.")


def test_the_spine_is_the_only_intentional_container():
    """Pins WHY nine of the overlaps are legitimate, so the count above stays reviewable."""
    boxes = _boxes()
    assert _SPINE_CONTAINER in boxes, "the spine container is gone; the baseline above is stale"
    spine = boxes[_SPINE_CONTAINER]
    inside = [n for n, b in boxes.items()
              if n != _SPINE_CONTAINER and n.startswith("sd_")
              and b["x"] >= spine["x"] and b["x"] + b["w"] <= spine["x"] + spine["w"]]
    assert len(inside) >= 8, (
        "the sd_* column no longer sits inside the spine; if that column moved, the container "
        "overlaps in the baseline are no longer the ones this test explains")
