"""Every numbered design record says its language in the index, and the newest Russian ones carry an
English summary (doc 75 UX-20).

14 of the 75 numbered documents are written in Russian and the rest in English, and the index gave a
reader no way to know which before opening one. The tag is derived from the document itself — the
share of Cyrillic letters in its first 4 KB — so it cannot drift from the file it describes; the
index page is outside the heuristic (its own prose is English, its rows are mixed).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
ROW = re.compile(r"^\| [^|]+ \| \*\*\[([0-9]{2}-[^\]]+\.md)\]\(\1\)\*\* \[(RU|EN)\]", re.M)


def _language(path: Path) -> str:
    head = path.read_text(encoding="utf-8", errors="ignore")[:4000]
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", head))
    return "RU" if cyrillic > 0.3 * len(re.findall(r"[A-Za-z]", head)) else "EN"


def test_every_numbered_document_row_carries_the_tag_its_text_implies():
    index = (DOCS / "00-INDEX.md").read_text(encoding="utf-8")
    tagged = dict(ROW.findall(index))
    numbered = {p.name for p in DOCS.glob("[0-9][0-9]-*.md") if p.name != "00-INDEX.md"}
    linked = set(re.findall(r"\]\(([0-9]{2}-[^)]+\.md)\)\*\*", index)) & numbered
    assert linked - set(tagged) == set(), "an index row links a numbered document with no [RU]/[EN] tag"
    wrong = {name: tag for name, tag in tagged.items() if _language(DOCS / name) != tag}
    assert wrong == {}


def test_the_newest_russian_records_open_with_an_english_summary():
    for number in range(71, 76):
        path = next(DOCS.glob(f"{number}-*.md"))
        assert _language(path) == "RU"
        assert "*EN summary:" in "\n".join(path.read_text(encoding="utf-8").splitlines()[:6]), path.name
