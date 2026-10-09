"""The pages a newcomer reads first stay short enough to be read (doc 74 EB-01/EB-02/EB-11).

Measured 2026-10-09 before the rewrite: README 3,110 words, Quickstart 1,764 (its step 5 alone
810), Installation 698 — and the user guide behind them 246,000. Every change to the product used
to add a paragraph to whichever page it touched, and the entry pages are where that cost a reader
most. The budgets below are the post-rewrite sizes with room for a few sentences; a page that needs
more should link to the reference page that holds the detail (`ui.md` holds the Quickstart's former
step 5, `cli-reference.md` every flag), the same rule `CLAUDE.md`'s own byte budget applies to it.

Words, not bytes: the pages mix code blocks and prose, and a reader's cost is the words.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

BUDGETS = {
    "README.md": 1000,
    "docs/index.md": 1000,
    "docs/guide/index.md": 550,
    "docs/guide/installation.md": 500,
    "docs/guide/quickstart.md": 750,
    "docs/guide/cli-walkthrough.md": 900,
    "examples/README.md": 600,
    "tests/README.md": 400,
}


def _words(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").split())


@pytest.mark.parametrize("rel, budget", sorted(BUDGETS.items()))
def test_entry_page_stays_within_its_word_budget(rel, budget):
    words = _words(ROOT / rel)
    assert words <= budget, (
        f"{rel} is {words} words, over its {budget}-word budget: move the detail to the reference "
        "page that owns it and link there, rather than raising the budget")


def test_agents_md_opens_with_a_short_first_run_sequence():
    """`AGENTS.md` is what a coding agent reads first; its first section is the five-step run."""
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    first = re.split(r"^## ", text, flags=re.M)[1]
    assert first.startswith("First run in five steps")
    assert len(first.split()) <= 200
    for step in ("looplab ui", "external_harness=true", "harness-mcp", "connection_check",
                 "result_notices"):
        assert step in first, step


def test_entry_pages_point_at_the_offline_demo_rather_than_a_flag_footnote():
    """doc 74 EB-03: the demo file sets `backend: toy` itself, so the entry pages need neither the
    `--backend toy` footnote nor the date the default changed."""
    for rel in ("README.md", "docs/guide/installation.md", "docs/guide/cli-walkthrough.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "examples/demo.yaml" in text, rel
        assert "2026-08-04" not in text, rel


def test_every_stated_node_requirement_is_the_ui_packages_own_range():
    """doc 71 found "Node ≥ 20" in the JupyterHub guide while `ui/package.json` accepts only
    `^20.19.0 || ^22.13.0 || >=24.0.0` — so 21.x and 22.0-22.12 were promised and refused. A page that
    states the requirement must state all three lines of the real range, and no page may say a bare
    "Node ≥ 20"."""
    import json
    engines = json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))["engines"]["node"]
    assert engines == "^20.19.0 || ^22.13.0 || >=24.0.0", "update the pages below with the new range"
    pages = [ROOT / "README.md", *sorted((ROOT / "docs" / "guide").glob("*.md"))]
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert not re.search(r"Node\s*(?:≥|>=)\s*20(?![.\d])", text), f"{page.name}: bare Node >= 20"
        if re.search(r"\bNode\b[^\n]{0,40}\b20\.19", text):
            assert "22.13" in text and "24" in text, f"{page.name}: states only part of the Node range"


def test_every_large_guide_page_opens_with_where_to_start():
    """doc 74 EB-11: the reference pages stay references, but a reader landing on one is told which
    one or two sections they need. Every guide page over 10,000 words carries a "Start here" lead
    (or the tasks/concepts/configuration variants) near its top, and it links into the page."""
    leads = ("**Start here:**", "**New here?**", "**Which kind do I need?**",
             "## Start here: the settings most runs touch")
    for page in sorted((ROOT / "docs" / "guide").glob("*.md")):
        text = page.read_text(encoding="utf-8")
        if len(text.split()) <= 10_000:
            continue
        head = "\n".join(text.splitlines()[:40])
        lead = next((marker for marker in leads if marker in head), None)
        assert lead, f"{page.name}: no where-to-start lead in its first 40 lines"
        block = head[head.index(lead) + len(lead):].split("\n## ", 1)[0]   # the lead up to the next section
        assert "](#" in block or "`" in block, f"{page.name}: the lead names no section or setting"



def test_installation_names_the_read_fence_warning_a_windows_or_root_run_prints():
    """doc 74 §12.6 (EB-05): every Windows run, and every run as root, opens with the read fence's
    WARNING. It stays — it names a real residual — so Installation says it is expected, quoting its
    opening words; this keeps that quote the message's own text rather than a paraphrase that drifts."""
    from looplab.runtime import read_fence
    page = (ROOT / "docs/guide/installation.md").read_text(encoding="utf-8")
    quoted = re.search(r'"(the read fence\'s KERNEL self-protection rung is ADVISORY here)"', page)
    assert quoted, "Installation no longer names the read fence warning"
    target = ROOT / "docs/guide/installation.md"         # a file with write bits: always reduced
    message = read_fence.harden_guarantee(target)
    assert message and message.startswith(quoted.group(1)), message
