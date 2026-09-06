"""What the run tells itself about money, and which phases hear it.

MEASURED 2026-09-06 over eight capped probes, counting `generation` spans whose prompt carries the
`BUDGET:` line:

    deep_research   395/475  83 %      propose            0/538   0 %
    plan_step       279/897  31 %      repropose          0/158   0 %
    plan             44/216  20 %      foresight_rank      0/64   0 %
                                       hyp_prioritize      0/60   0 %

This corrects §278, which concluded from `RESEARCHER_PROMPT_CUES` that no money hint existed at all.
It does exist; it is just built twice, by hand, in two adapters, and reaches neither the phases the
sweep list names nor the two that spend the most.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from looplab.core.costs_text import budget_line  # noqa: E402


def test_it_reports_spend_share_and_remainder():
    got = budget_line(0.25, 1.0)
    assert got.startswith("BUDGET: $0.2500 of $1.0000 spent, $0.7500 left (25 % gone).")


def test_a_run_with_no_ceiling_gets_no_line():
    """A run with no limit has no share to report, and "0 % gone" would be a number it cannot act
    on."""
    assert budget_line(0.25, None) == ""
    assert budget_line(0.25, 0.0) == ""


def test_a_caller_sentence_is_appended_not_replaced():
    got = budget_line(0.5, 1.0, "Size this memo to what is left.")
    assert "50 % gone" in got and got.rstrip().endswith("Size this memo to what is left.")


def test_overspend_reads_as_nothing_left_not_as_negative_money():
    got = budget_line(1.4, 1.0)
    assert "$0.0000 left" in got and "140 % gone" in got, got


def test_junk_is_not_a_line():
    assert budget_line("x", 1.0) == ""          # type: ignore[arg-type]
    assert budget_line(0.5, "y") == ""          # type: ignore[arg-type]


def test_the_two_existing_copies_say_the_same_head():
    """The line already existed twice, copy-pasted with different second sentences. This pins that
    the shared head matches what both of them emit, so the extraction is not a reword."""
    root = Path(__file__).resolve().parents[1] / "looplab"
    for rel in ("agents/deep_research.py", "adapters/repo_developer.py"):
        src = (root / rel).read_text(encoding="utf-8", errors="replace")
        assert "BUDGET: ${spent:.4f} of ${limit:.4f} spent, ${remaining:.4f} left ({pct:.0f} % gone)" \
            in src, rel
