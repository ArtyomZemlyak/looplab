"""The OPEN/DECLINED index: one greppable key for every open item, and a guard that a real drift FAILS.

Why this exists, measured (CLAUDE.md, "The open-item index"): on 2026-08-14 `docs/BACKLOG.md` §0
recorded B7 as open; the fix landed 2026-08-15 and the row stayed open until 2026-08-19 because
nothing connected the row to the tree. Re-derived on 2026-08-19 the same shape is live in three more
§0.1 rows (2, 4, 8 — row 8's own test, `tests/test_append_multiprocess_race.py`, landed the SAME DAY
the row was written and the row has never been amended). In the other direction, eight `✅` rows are
annotated "★Shipped's ✅ overstates it". A status marker nobody re-derives is wrong in BOTH
directions, so this file re-derives every marker's own falsifier against the real tree on every run.

The design rule this enforces, in one sentence: **an item is open only while a predicate over the
source tree says so, and closing it is a DELETION.** There is no CLOSED marker, so there is no `✅`
to overstate. `DECLINED[...]` is the one permanent form and must carry a number and a doc citation,
because this repo declines things deliberately (doc 29 F3) and a decline without a measurement is
indistinguishable from a dodge.

What this guard is NOT: a substring pin that a comment can satisfy. `tests/test_repair_judgment.py`
carries a live `REVIEW 2026-08-18 (guard-test)` annotation about exactly that weakness, and
`tests/test_options_divergence.py:182-185` records the repo already deleting one such pin ("that is
bookkeeping about an OPEN finding … and it goes stale the moment the finding is fixed"). Every
predicate here READS THE TREE, and every marker line is stripped from the text before its own
predicates are evaluated, so a marker can neither satisfy nor falsify itself.
"""
from __future__ import annotations

from pathlib import Path
import re

from looplab.core.claimpin import (
    predicate_holds,
    read_text as _read,
    tracked_text_files as _tracked_text_files,
)

ROOT = Path(__file__).resolve().parents[1]

# The predicate evaluator, the tree walk and the marker-stripping rule MOVED to
# `looplab/core/claimpin.py` on 2026-08-20 and are imported rather than re-implemented. The reason
# is this repo's own most-repeated defect: §0.8 found FOUR implementations of one claim/verdict join
# and every drift was between the copies. Its sibling guard `tests/test_claim_pins.py` re-derives a
# different family of markers (`CLAIM[…] … decided:`) with the SAME three predicates plus `line:`,
# and two evaluators would eventually disagree about what `present:` means — including about the
# marker-stripping rule, whose absence was a silent FALSE GREEN here until 2026-08-19.
#
# The two indexes stay separate because their reds mean OPPOSITE things: red here means the item
# SHIPPED (delete the marker), red there means the SENTENCE is false (fix the sentence).

# One key. `grep -rn 'OPEN\[' .` is the whole index.
_MARKER = re.compile(r"\b(OPEN|DECLINED)\[([a-z0-9][a-z0-9-]{2,60})\]")
# The kind is part of the key on purpose: `proof:` alone occurs in ordinary prose
# ("the correlation proof: …"), and an index whose scanner matches prose is the
# `still open` collision this convention was chosen to avoid.
_PROOF = re.compile(r"proof:((?:absent:|present:|missing:)\S+)")
_MEASURED = re.compile(r"measured:(.{0,400})", re.S)

# The window a marker's own clause must live in. A docstring paragraph and a markdown row both fit;
# it is deliberately short enough that the clause cannot end up describing the NEXT item.
_WINDOW = 900


def _iter_markers():
    for path in _tracked_text_files(ROOT):
        text = _read(path)
        for m in _MARKER.finditer(text):
            yield path, m.group(1), m.group(2), text[m.end():m.end() + _WINDOW]


def _predicate_holds(pred: str) -> tuple[bool, str]:
    """This index's three predicates, evaluated by the shared implementation.

    Repo-relative only (`allow_absolute=False`): the suite must pass against a bare
    `git archive HEAD` tree, so an open item may never be proved by a path on one box.
    """
    return predicate_holds(pred, root=ROOT, allow_absolute=False)


def test_every_open_marker_is_well_formed():
    """A marker with no evaluable proof is a `⬜` with extra steps."""
    bad = []
    for path, kind, slug, window in _iter_markers():
        rel = path.relative_to(ROOT)
        if kind == "OPEN":
            proof = _PROOF.search(window)
            if not proof:
                bad.append(f"{rel}: OPEN[{slug}] carries no `proof:` clause within {_WINDOW} chars")
                continue
            for pred in proof.group(1).split("+"):
                if not pred.startswith(("absent:", "present:", "missing:")):
                    bad.append(f"{rel}: OPEN[{slug}] predicate {pred!r} is not absent:/present:/missing:")
        else:
            measured = _MEASURED.search(window)
            if not measured:
                bad.append(f"{rel}: DECLINED[{slug}] carries no `measured:` clause — a decline "
                           "without a number is a dodge (CLAUDE.md)")
                continue
            body = measured.group(1)
            if not re.search(r"\d", body):
                bad.append(f"{rel}: DECLINED[{slug}] `measured:` clause carries no number")
            if "docs/" not in body:
                bad.append(f"{rel}: DECLINED[{slug}] `measured:` clause cites no docs/ page")
    assert not bad, "malformed open-item markers:\n  " + "\n  ".join(bad)


def test_each_slug_is_declared_exactly_once():
    """The key must survive an item moving between files, so it has to name ONE item.

    `docs/BACKLOG.md`'s own caveat 2 records the alternative: three namespaces share one letter-digit
    space, `C2`/`C3`/`C5` each mean two different things, and the file warns you never to cite a bare
    ID from it. Re-derived 2026-08-19 there are at least seven disjoint ID namespaces across the docs
    (§0/§1 A-C, ★Shipped/§2 A-I, §6 D1-D5, doc 25 XX-NN, doc 29 F1-F8, doc 34 D-01..D-05, and the
    `CR0/CR1a/CR2b` pointers in `looplab/engine/` into a doc section that no longer exists).
    """
    seen: dict[str, list[str]] = {}
    for path, kind, slug, _ in _iter_markers():
        seen.setdefault(slug, []).append(f"{path.relative_to(ROOT)} ({kind})")
    dupes = {slug: where for slug, where in seen.items() if len(where) > 1}
    assert not dupes, f"a slug names exactly one item; these are declared more than once: {dupes}"


def test_no_open_marker_has_silently_shipped():
    """THE guard. Every open item re-derives its own falsifier against the tree, every run.

    A failure here is NOT a product defect. It means the named item is no longer in the state its
    marker claims — usually because it SHIPPED. The fix is to delete the marker (and say in the
    surrounding prose what landed). That is the whole convention: closing is a deletion, and this
    test is what makes an uncollected marker impossible to ignore for five days.
    """
    stale = []
    for path, kind, slug, window in _iter_markers():
        if kind != "OPEN":
            continue
        proof = _PROOF.search(window)
        if not proof:
            continue  # reported by the well-formedness test
        for pred in proof.group(1).split("+"):
            holds, why = _predicate_holds(pred)
            if not holds:
                stale.append(f"OPEN[{slug}] at {path.relative_to(ROOT)}: {why}")
    assert not stale, (
        "open-item markers whose own proof no longer holds — delete the marker (or re-point the "
        "proof if the item merely MOVED):\n  " + "\n  ".join(stale))


def test_the_index_is_not_empty_and_not_a_single_file():
    """A convention with no corpus looks authoritative while being empty, which is the failure mode
    `docs/BACKLOG.md`'s own header warns about ("the file contradicts itself by construction").

    The second half is the constraint that killed a separate `OPEN.md`: an index that lives in ONE
    file cannot hold an item whose home is a docstring paragraph, and duplicating it into a tracker
    is how this repo got four implementations of one claim/verdict join (§0.8 finding 2).
    """
    slugs = [(path, slug) for path, _kind, slug, _ in _iter_markers()]
    assert len(slugs) >= 10, f"the index has collapsed to {len(slugs)} entries"
    homes = {p.suffix for p, _ in slugs}
    assert {".py", ".md"} <= homes, (
        f"the index must span code and docs or the prose items escape again; found {homes}")
