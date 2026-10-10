"""doc 75 section 8: the findings registry's structure is a test, not a table.

Doc 74's own critique (its section 12.2, point 11) found two drifts in a plan document: items were
recorded "done" by the substance of a change rather than by the acceptance the document set, and
the counts written by hand were the first thing to go stale. Doc 75 is a plan whose items are all
still open, so the acceptance of each cannot be checked yet; what CAN be held is the registry's
shape while it waits: every `UX-NN` heading states an observation, a proposal and an acceptance
criterion; every item is assigned to exactly one delivery package; the summary counts in its first
section are the parser's; and the index, the MkDocs nav and the screenshots it links exist. When
an item ships, its package row's status cell changes (doc 75 section 11) — nothing here moves.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "75-functionality-usability-review-2026-10-10.md"
HEADING = re.compile(r"^#### (UX-\d\d) · (P[0-2]) · ([SML]) — .+$", re.M)
PARTS = ("**Наблюдение.**", "**Предложение.**", "**Приёмка.**")


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _items(text: str) -> dict[str, tuple[str, str, str]]:
    """`UX-NN -> (priority, size, body)`; a body runs to the next heading or to the end of the
    registry (the `## 6.` scenarios section)."""
    heads = list(HEADING.finditer(text))
    registry_end = text.index("\n## 6.")
    items = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else registry_end
        items[m.group(1)] = (m.group(2), m.group(3), text[m.end():end])
    return items


def test_every_finding_states_an_observation_a_proposal_and_an_acceptance():
    items = _items(_text())
    assert items, "doc 75 has no UX-NN headings"
    incomplete = {uid: [part for part in PARTS if part not in body]
                  for uid, (_, _, body) in items.items()}
    assert {uid: gaps for uid, gaps in incomplete.items() if gaps} == {}


def test_the_summary_counts_are_the_parsers_and_the_ids_are_dense():
    text = _text()
    items = _items(text)
    by_priority = Counter(priority for priority, _, _ in items.values())
    sentence = re.search(r"Реестр: (\d+) находк\w*, из них P0 — (\d+), P1 — (\d+), P2 — (\d+)", text)
    assert sentence, "section 1 must carry the registry count sentence the parser checks"
    assert tuple(int(n) for n in sentence.groups()) == (
        len(items), by_priority["P0"], by_priority["P1"], by_priority["P2"])
    assert sorted(items) == [f"UX-{n:02d}" for n in range(1, len(items) + 1)]


def test_every_finding_sits_in_exactly_one_delivery_package():
    text = _text()
    items = _items(text)
    table = text[text.index("\n## 7."):text.index("\n## 8.")]
    rows = [line for line in table.splitlines() if line.startswith("| ") and "UX-" in line]
    assigned = Counter(uid for row in rows for uid in re.findall(r"UX-\d\d", row.split("|")[2]))
    assert set(assigned) == set(items), {
        "unassigned": sorted(set(items) - set(assigned)),
        "unknown": sorted(set(assigned) - set(items))}
    assert {uid: n for uid, n in assigned.items() if n != 1} == {}


def test_the_index_the_nav_and_the_linked_screenshots_carry_the_document():
    assert DOC.name in (ROOT / "docs" / "00-INDEX.md").read_text(encoding="utf-8")
    assert DOC.name in (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    linked = re.findall(r"\]\((assets/75-usability-review/[^)]+)\)", _text())
    assert linked, "doc 75 links its screenshots"
    assert [rel for rel in linked if not (ROOT / "docs" / rel).is_file()] == []
