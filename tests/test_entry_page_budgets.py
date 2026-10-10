"""The pages a newcomer reads first stay short enough to be read (doc 74 EB-01/EB-02/EB-11).

Measured 2026-10-09 before the rewrite: README 3,110 words, Quickstart 1,764 (its step 5 alone
804), Installation 698 — and the user guide behind them 246,000. Every change to the product used
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
    "docs/guide/quickstart.md": 700,
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
    # doc 75 UX-03: the WARNING is one line that still names the residual risk; the fence's own
    # sentence (`harden_guarantee`, the security record) is logged at DEBUG, unchanged.
    from looplab.engine.resources import READ_FENCE_REDUCED_WARNING
    from looplab.runtime import read_fence
    page = " ".join((ROOT / "docs/guide/installation.md").read_text(encoding="utf-8").split())
    assert f'"{READ_FENCE_REDUCED_WARNING}"' in page, "Installation no longer quotes the warning"
    assert len(READ_FENCE_REDUCED_WARNING) <= 160 and "`" not in READ_FENCE_REDUCED_WARNING
    assert "overwrite" in READ_FENCE_REDUCED_WARNING, "the short line still names the risk"
    target = ROOT / "docs/guide/installation.md"         # a file with write bits: always reduced
    message = read_fence.harden_guarantee(target)
    assert message and message.startswith("the read fence's KERNEL self-protection rung is ADVISORY")


def test_start_in_assistant_keeps_the_steps_and_names_no_code():
    """doc 74 EB-11: the UI guide's "Start in Assistant" opening is the user's path — at most 400
    words of its own (sub-sections hold the detail) and no developer identifiers (`uiText`,
    `ru.json`, `npm run …`), which once sat between the user's steps."""
    lines = (ROOT / "docs/guide/ui.md").read_text(encoding="utf-8").splitlines()
    start = lines.index("## Start in Assistant")
    end = next(i for i in range(start + 1, len(lines)) if re.match(r"^#{2,3} ", lines[i]))
    own = "\n".join(lines[start + 1:end])
    assert len(own.split()) <= 400, len(own.split())
    assert not re.search(r"uiText|ru\.json|npm run|\.jsx?\b", own), "developer detail in the user's steps"


# doc 75 UX-21: the large hand-written reference pages stop growing. Doc 74 gave each a "Start here"
# opening and refused the Use/Reference rewrite; their size still went 246,207 -> 246,629 words in a
# day. A CEILING per page, at its size when it was set, measured on PROSE: a table row that opens
# with a backticked identifier (`| \`field\` | …`) is excluded, because every new `Settings` field owes
# `configuration.md` exactly such a row in the same change (CLAUDE.md) and a ratchet that refused it
# would contradict that rule. Shrink freely; lower the number when you do. Generated pages
# (`api-reference.md`, `event-reference.md`) are written by their generators and are not held here.
PROSE_CEILINGS = {
    "cli-reference.md": 28776,
    "concepts.md": 32764,
    "configuration.md": 10447,
    "external-harness.md": 17985,
    "llm-and-agents.md": 14472,
    "memory.md": 12532,
    "tasks.md": 22381,
    "ui.md": 23007,
}
_GENERATED = {"api-reference.md", "event-reference.md"}


def _prose_words(path: Path) -> int:
    return sum(len(line.split()) for line in path.read_text(encoding="utf-8").splitlines()
               if not line.startswith("| `"))


@pytest.mark.parametrize("name, ceiling", sorted(PROSE_CEILINGS.items()))
def test_a_large_guide_page_does_not_grow(name, ceiling):
    words = _prose_words(ROOT / "docs" / "guide" / name)
    assert words <= ceiling, (
        f"docs/guide/{name} has {words} words of prose, over its ceiling {ceiling}: say it in fewer "
        "words, or move the detail to the module docstring or a numbered doc and link it")


def test_every_large_hand_written_page_has_a_ceiling():
    large = {path.name for path in (ROOT / "docs" / "guide").glob("*.md")
             if len(path.read_text(encoding="utf-8").split()) > 10_000 and path.name not in _GENERATED}
    assert large - set(PROSE_CEILINGS) == set()


def test_the_walkthrough_says_only_what_its_runs_show():
    """doc 75 UX-06, UX-22, UX-29: the walkthrough promised the regression run would find "the true
    degree is 2" (it settled on 3), offered `--crash-after` as a user step without saying it is a
    hidden test flag, listed a run directory without the `AGENTS.md` and lock files the reader sees,
    and taught `inspect --config`'s pinned-field subtlety on the second step; the README called a
    finished run's `stop` resumable."""
    walk = (ROOT / "docs/guide/cli-walkthrough.md").read_text(encoding="utf-8")
    assert "true degree" not in walk
    assert "--crash-after is a hidden test flag" in walk
    assert "AGENTS.md" in walk and "lock" in walk
    assert "run_started`-pinned" not in walk
    assert "resumable unless already finished" in (ROOT / "README.md").read_text(encoding="utf-8")
