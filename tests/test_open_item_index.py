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


ROOT = Path(__file__).resolve().parents[1]

# One key. `grep -rn 'OPEN\[' .` is the whole index.
_MARKER = re.compile(r"\b(OPEN|DECLINED)\[([a-z0-9][a-z0-9-]{2,60})\]")
# The kind is part of the key on purpose: `proof:` alone occurs in ordinary prose
# ("the correlation proof: …"), and an index whose scanner matches prose is the
# `still open` collision this convention was chosen to avoid.
_PROOF = re.compile(r"proof:((?:absent:|present:|missing:)\S+)")
_MEASURED = re.compile(r"measured:(.{0,400})", re.S)

# The window a marker's own clause must live in. A docstring paragraph and a markdown row both fit.
# LENGTH IS NOT WHAT KEEPS A CLAUSE FROM DESCRIBING THE NEXT ITEM, and this comment claimed it was
# until 2026-08-20: in the bullet-list docs (27, 34) markers sit 150-300 chars apart, so a marker
# written with NO clause of its own silently borrowed its neighbour's. Measured over this tree by
# deleting each OPEN marker's own `proof:` clause: 17 of 77 still found one, i.e. 17 markers were
# one edit away from being checked against a DIFFERENT item's falsifier — the wrong-proof failure
# this index is worth less than nothing with. `_marker_windows` therefore ends every window at the
# NEXT marker, and the length is only the outer bound it always was.
_WINDOW = 900

_SKIP_DIRS = {".git", ".claude", "runs", "node_modules", "dist", "site", "__pycache__",
              ".pytest_cache", ".mypy_cache", ".venv", "venv", "build"}
_TEXT_SUFFIXES = {".py", ".md", ".js", ".jsx", ".html", ".txt", ".toml", ".yml", ".yaml"}


def _tracked_text_files() -> list[Path]:
    out: list[Path] = []
    stack = [ROOT]
    while stack:
        d = stack.pop()
        for child in d.iterdir():
            if child.is_symlink():
                continue
            if child.is_dir():
                if child.name not in _SKIP_DIRS and not child.name.endswith(".egg-info"):
                    stack.append(child)
            elif child.suffix in _TEXT_SUFFIXES:
                out.append(child)
    return sorted(out)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _text_without_markers(path: Path) -> str:
    """The file as a predicate sees it: every line carrying a marker is removed.

    Both directions matter. Without this an `absent:` proof would be falsified by the very line that
    states it, and a `present:` proof could be SATISFIED by its own marker text — the "a comment can
    satisfy the pin" failure this guard exists not to have.
    """
    return "\n".join(line for line in _read(path).splitlines()
                      if not (_MARKER.search(line) or "proof:" in line
                              or "measured:" in line))


def _marker_windows(text: str):
    """`(kind, slug, window)` for every marker in `text`, each window ending at the NEXT marker.

    Pure and driven directly by `test_a_markers_proof_window_stops_at_the_next_marker`, because a
    window that runs on is a green marker with someone else's falsifier — which reads exactly like a
    verified item and is the one failure this whole file is supposed to make impossible.
    """
    starts = [m.start() for m in _MARKER.finditer(text)]
    for m in _MARKER.finditer(text):
        nxt = next((s for s in starts if s > m.start()), len(text))
        yield m.group(1), m.group(2), text[m.end():min(m.end() + _WINDOW, nxt)]


def _iter_markers():
    for path in _tracked_text_files():
        for kind, slug, window in _marker_windows(_read(path)):
            yield path, kind, slug, window


def _resolve(rel: str) -> Path:
    return (ROOT / rel).resolve()


def _predicate_holds(pred: str) -> tuple[bool, str]:
    """Evaluate one predicate against the real tree. Returns (holds, why-not)."""
    if pred.startswith("missing:"):
        rel = pred[len("missing:"):]
        target = _resolve(rel)
        if target.exists():
            return False, f"{rel} now EXISTS — the item this proof describes is no longer absent"
        return True, ""
    for kind in ("absent:", "present:"):
        if not pred.startswith(kind):
            continue
        body = pred[len(kind):]
        if "@" not in body:
            return False, f"malformed predicate {pred!r} — expected <literal>@<path>"
        literal, rel = body.rsplit("@", 1)
        target = _resolve(rel)
        if not target.exists():
            # A dead citation is this repo's most-measured form of rot (`docs/BACKLOG.md` §0.3: 8 of
            # 8 line citations dead, 13 corrected inline). An index whose proofs can point at
            # nothing is the same index the glyphs already were. This is also what makes a slug
            # SURVIVE A MOVE: the key does not change, the proof is re-pointed under a red test.
            return False, f"{rel} does not exist — re-point this proof at where the item now lives"
        if target.is_dir():
            # The skip check is RELATIVE to the cited directory, not over the absolute path —
            # fixed 2026-08-19, and the bug it had was a silent false GREEN. `p.parts` on an
            # absolute path carries every component of wherever this checkout happens to live, and
            # this repo's own agent worktrees live under `.claude/worktrees/<name>/`, which is in
            # `_SKIP_DIRS`. So in a worktree EVERY file under a cited directory was filtered out,
            # `found` was False for all of them, and a directory-form `absent:` predicate held
            # vacuously while a `present:` one reported a defect as shipped. An index whose proofs
            # depend on the checkout PATH is the unverified claim this file exists to replace.
            files = [p for p in target.rglob("*")
                     if p.is_file() and p.suffix in _TEXT_SUFFIXES
                     and not any(part in _SKIP_DIRS for part in p.relative_to(target).parts)]
        else:
            files = [target]
        found = any(literal in _text_without_markers(p) for p in files)
        if kind == "absent:" and found:
            return False, (f"{literal!r} is now PRESENT in {rel} — this item claims to be open "
                           f"BECAUSE that was absent, so either it shipped or the proof is wrong")
        if kind == "present:" and not found:
            return False, (f"{literal!r} is GONE from {rel} — this item claims to be open BECAUSE "
                           f"that defect was still there, so either it shipped or the proof moved")
        return True, ""
    return False, f"unknown predicate kind in {pred!r} (use absent:/present:/missing:)"


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


def test_a_markers_proof_window_stops_at_the_next_marker():
    """A marker must carry its OWN falsifier — never inherit the next one's.

    The failure this catches is a false GREEN, which is why it is driven rather than reasoned about:
    a marker with no clause used to scan forward `_WINDOW` characters and find the following item's
    `proof:`, so it reported as verified while nothing about it had been checked, and it would have
    gone RED on the day the OTHER item shipped. Both halves are asserted, because a truncation that
    also cut a legitimate clause short would silently empty the index instead.
    """
    # Assembled rather than written out: this file is itself scanned by the tree walk above, and a
    # literal marker here would enter the real index (and its `proof:` would be evaluated for real).
    key, clause = "OPEN", "proof:" + "absent:_beta_symbol@looplab/core/config.py"
    text = (f"- {key}[alpha-has-no-clause] the sentence about alpha, with no falsifier at all.\n"
            f"- {key}[beta-has-its-own] {clause}\n")
    windows = {slug: window for _kind, slug, window in _marker_windows(text)}
    assert "proof:" not in windows["alpha-has-no-clause"], (
        "a marker with no clause of its own must not inherit the next marker's proof")
    assert clause in windows["beta-has-its-own"], (
        "...and a marker's own clause must still be reachable inside the window")


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
