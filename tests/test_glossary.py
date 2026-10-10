"""The glossary defines the words a user meets on the first screens (doc 75 UX-19).

No page defined them: "node", "champion", "Card", "Genesis", "boss" were used on the entry pages, the
Report and in `looplab inspect` with their definitions spread over a 32 000-word concepts page. The
glossary is one line per term, reachable from "Start here" and the "Get started" nav; this holds its
size, its place, and that the words the Report and `inspect` print are in it — `inspect`'s are read
off a real run of the offline demo, not a list someone keeps by hand.
"""
from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from looplab.cli import app

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "guide" / "glossary.md"

# The terms the Report's labels and headline, the run list and the Cards board use.
UI_TERMS = ["experiment", "node", "selected result", "champion", "eligible", "first eligible experiment",
            "comparable", "confirmed", "unconfirmed", "deterministic", "repeat checks", "caveat",
            "Card", "lane", "verdict", "Deep Research", "concept", "claim", "lesson", "Assistant",
            "boss", "Genesis", "Strategist", "endgame", "rung", "sweep", "Energy", "base"]


def _defined() -> set[str]:
    terms = set()
    # A term is bold in its row — the first cell, or a related word the line defines in passing.
    for row in re.findall(r"^\| \*\*.*$", PAGE.read_text(encoding="utf-8"), re.M):
        terms.update(t.lower() for t in re.findall(r"\*\*(.+?)\*\*", row))
    return terms


def test_the_page_is_short_reachable_and_one_line_per_term():
    text = PAGE.read_text(encoding="utf-8")
    assert len(text.split()) <= 1500
    rows = re.findall(r"^\| \*\*", text, re.M)
    assert 0 < len(rows) <= 60
    assert "guide/glossary.md" in (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "(glossary.md)" in (ROOT / "docs" / "guide" / "index.md").read_text(encoding="utf-8")


def test_every_word_the_report_and_the_board_use_is_defined():
    missing = [term for term in UI_TERMS if term.lower() not in _defined()
               and not any(term.lower() in defined for defined in _defined())]
    assert missing == []


def test_the_words_inspect_prints_on_the_demo_are_defined(tmp_path):
    run_dir = tmp_path / "demo"
    assert CliRunner().invoke(app, ["run", str(ROOT / "examples" / "demo.yaml"),
                                    "--out", str(run_dir)]).exit_code == 0
    out = CliRunner().invoke(app, ["inspect", str(run_dir)]).output
    # Each line opens with its own word: `stop:`, `BEST experiment`, `trust scan:`, `comparability:`.
    heads = {"stop": "finished", "BEST experiment": "experiment", "trust scan": "trust scan",
             "comparability": "comparable", "node budget spent": "node budget"}
    defined = " ".join(sorted(_defined()))
    for printed, term in heads.items():
        assert printed in out, out
        assert term in defined, term
