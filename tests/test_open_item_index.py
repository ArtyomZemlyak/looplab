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
    KINDS as _KNOWN_KINDS,
    PROOF as _PROOF_GRAMMAR,
    predicate_holds,
    proof_predicate,
    read_text as _read,
    satisfied_only_by_prose,
    text_without_markers as _text_without_markers,
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
# THE GRAMMAR IS THE SIBLING'S, IMPORTED RATHER THAN RE-SPELLED (2026-08-21). This was a local
# regex accepting `absent:`/`present:`/`missing:` and a whitespace-free literal, while
# `tests/test_claim_pins.py` accepted `line:` and a backtick-quoted literal too — so the two indexes
# disagreed about which predicates EXIST while sharing the one function that evaluates them, which
# is precisely the split `looplab/core/claimpin.py` was created to end.
#
# WHAT THE DRIFT COST: an item of the shape "the DEFAULT is wrong" had no expressible falsifier at
# all. §0.1 #7's live half is `Settings.landlock` shipping `"off"`, and the only predicate that
# discriminates it is `line:landlock&&"off"@looplab/core/config.py` — True today, False the moment
# the default flips. `line:` was exactly what this scanner did not admit, and the one whitespace-free
# spelling in the tree sits in a COMMENT, which `satisfied_only_by_prose` exists to reject. So that
# entry stayed outside the guard — as nine other ranked entries are, three of which were checked
# against the tree that day and none of which still described it.
_PROOF = _PROOF_GRAMMAR
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
    # BOTH halves, and they are independent: `_tracked_text_files(ROOT)` is the shared evaluator's
    # rooted walk (one implementation for OPEN and CLAIM), while `_marker_windows` is what stops a
    # window at the NEXT marker. Dropping either one restores a distinct false green — the first a
    # walk that skips by absolute path inside a worktree, the second a marker checked against its
    # neighbour's falsifier.
    for path in _tracked_text_files(ROOT):
        for kind, slug, window in _marker_windows(_read(path)):
            yield path, kind, slug, window


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
            for pred in proof_predicate(proof).split("+"):
                if not pred.startswith(_KNOWN_KINDS):
                    bad.append(f"{rel}: OPEN[{slug}] predicate {pred!r} is not one of "
                               + "/".join(_KNOWN_KINDS))
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
        for pred in proof_predicate(proof).split("+"):
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


# The three proofs whose literal today occurs ONLY in prose. Each needs re-pointing at the line that
# DECIDES its item, not at the sentence describing it; until then they can never go green, so they
# are noise rather than evidence. Listed by name and bounded so the set can only shrink — the same
# shape `test_file_identity_tiers` uses for its unconverted signatures.
PROSE_ONLY_PROOFS = {
    "prompt-bundle-unpinned-across-hot-reload",
    "no-shared-reserve-commit-run-budget",
    "claim-legacy-prompt-branches",
}


def test_no_proof_is_satisfiable_only_by_prose():
    """A falsifier a COMMENT can satisfy is not a falsifier.

    The rule existed before this test and existed only as a comment beside one marker — the guard
    against comment-satisfiable proofs was itself a comment, which is the exact shape it warns about
    one level up. Both directions cost something, and differently: an `absent:` literal prose can
    produce goes GREEN the day someone writes the word (a false shipped), while a `present:` literal
    only prose carries can never go green at all (a marker stuck open, which teaches readers to skip
    the index).

    A literal that spans real code and a string constant — `startswith("setup` — is ABOUT the call
    and is not flagged. That distinction is not cosmetic: the naive question ("does it survive with
    every string blanked?") reports 5 offenders here, and only 3 of them are real.
    """
    offenders = {}
    for path, kind, slug, window in _iter_markers():
        proof = _PROOF.search(window)
        if not proof:
            continue
        for pred in proof_predicate(proof).split("+"):
            for form in ("absent:", "present:"):
                if not pred.startswith(form):
                    continue
                body = pred[len(form):]
                if "@" not in body:
                    continue
                literal, rel = body.rsplit("@", 1)
                target = ROOT / rel
                if not target.is_file() or target.suffix != ".py":
                    continue
                source = _text_without_markers(target)
                if satisfied_only_by_prose(target, source, literal):
                    offenders[slug] = f"{form}{literal}@{rel}"

    new = {s: p for s, p in offenders.items() if s not in PROSE_ONLY_PROOFS}
    assert not new, (
        "these proofs are satisfied only by a comment or string — re-point each at the line that "
        "DECIDES the item:\n  " + "\n  ".join(f"OPEN[{s}] {p}" for s, p in sorted(new.items())))
    gone = PROSE_ONLY_PROOFS - set(offenders)
    assert not gone, (
        "these were re-pointed or deleted — remove them from PROSE_ONLY_PROOFS so the bound keeps "
        f"shrinking: {sorted(gone)}")


def test_the_prose_check_can_actually_fail():
    """NON-VACUITY, both directions, driven on the real tree rather than on a fixture.

    Without this the test above passes on a broken `satisfied_only_by_prose` that always answers
    False — which is precisely how the rule spent its life as a comment.
    """
    llm = ROOT / "looplab/core/llm.py"
    assert satisfied_only_by_prose(llm, _text_without_markers(llm), "dollar-cap"), (
        "a literal whose only occurrences are prose must be caught")

    triage = ROOT / "looplab/engine/triage.py"
    assert not satisfied_only_by_prose(triage, _text_without_markers(triage), 'startswith("setup'), (
        "a literal anchored in real code must NOT be flagged, even where it reaches into a string")

    assert not satisfied_only_by_prose(triage, _text_without_markers(triage), "no-such-text-anywhere"), (
        "a literal that does not occur at all is a DEAD citation, reported by the proof check — "
        "this one must not also claim it is prose")


def test_the_two_indexes_agree_on_which_predicates_EXIST():
    """One grammar, two indexes — asserted over the compiled objects, not over two spellings.

    `looplab/core/claimpin.py` was created because "two evaluators would eventually disagree about
    what `present:` means". The SCANNERS were left behind and had drifted by 2026-08-21: this file's
    local regex accepted `absent:`/`present:`/`missing:` with a whitespace-free literal, while
    `tests/test_claim_pins.py` also accepted `line:` and a backtick-quoted literal. Two indexes
    disagreeing about which predicates exist, over one shared evaluator.

    THE COST WAS A WHOLE SHAPE OF ITEM. "The DEFAULT is wrong" has no whitespace-free falsifier —
    §0.1 #7's live half is `Settings.landlock` shipping `"off"`, discriminated only by
    `line:landlock&&"off"@…`, and the sole whitespace-free spelling in the tree sits in a COMMENT
    that `satisfied_only_by_prose` rejects. So that entry could not come under the guard at all.
    """
    from looplab.core import claimpin

    assert _PROOF is claimpin.PROOF
    for kind in ("absent:", "present:", "missing:", "line:"):
        bare = _PROOF.search(f"proof:{kind}Symbol@a.py")
        assert bare, f"the open-item scanner must admit {kind!r}"
        assert proof_predicate(bare).startswith(kind)
    # …and the quoted form, which is the only way a literal may carry a space.
    quoted = _PROOF.search('proof:`line:landlock&&"off"@looplab/core/config.py`')
    assert quoted and proof_predicate(quoted) == 'line:landlock&&"off"@looplab/core/config.py'


def test_a_wrong_default_is_now_expressible_and_discriminating():
    """The predicate the grammar gap made impossible, driven against the real tree: TRUE while the
    default is what the item says it is, FALSE the moment it flips. A falsifier that cannot go
    false is the vacuous guard this repo found nine times in one day."""
    open_now, _ = predicate_holds('line:landlock&&"off"@looplab/core/config.py', root=ROOT)
    closed_then, why = predicate_holds('line:landlock&&"on"@looplab/core/config.py', root=ROOT)
    assert open_now is True, "the item this proves is open must actually be open"
    assert closed_then is False and "belong together" in why
