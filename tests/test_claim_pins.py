"""Every recorded claim is pinned to the site that decides it, and this re-derives all of them.

WHY, measured. Seven claims failed in the two days to 2026-08-20 and every one is the same shape —
a fact recorded in ONE place whose truth lives in ANOTHER, with nothing connecting them. The
expensive ones were aimed at AGENTS: `runs/e5small-dr-unified-v3`'s goal stated the manual e5-small
recipe as "16k overall = 8k x 2 GPUs" and labelled it VERIFIED (that row is `rubert-tiny-lite`'s),
and all three of its nodes died of `torch.OutOfMemoryError` chasing a per-device 8192 that needs
~530 GiB on a 139.8 GiB card. `core/hardware.py` told five roles to "use ALL available GPUs" while
`engine/resources.py` fences an undeclared footprint to exactly one device.

`docs/45-claim-surfaces-2026-08-20.md` argues the design; `looplab/core/claimpin.py` is the
evaluator. This file is the in-repo CARRIER, and it has two halves with very different costs:

* `test_no_source_citation_is_dead` — ZERO adoption cost. 653 `<mod>.py::<symbol>` citations already
  exist in `looplab/`; nothing resolved one until now, and `docs/BACKLOG.md` §0.3 records what that
  bought (8 of 8 line citations dead). This found four MORE dead symbol citations and six live
  line-number ones on the day it was written.
* `test_every_claim_pin_still_holds` — opt-in, for the facts a citation cannot carry: a number, a
  behaviour, a row in a file outside the repo.

**A red here is not a product defect and it is NOT the same as a red `test_open_item_index`.** There,
red means the item shipped: delete the marker. Here, red means THE SENTENCE IS NOW FALSE — fix the
sentence, or fix the code it describes. Deleting the pin and keeping the sentence is the one move
that defeats this, which is why every pin is greppable in one command (`grep -rn 'CLAIM\\['`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from looplab.core.claimpin import (
    KINDS,
    CLAIM_MARKER,
    DECIDED,
    SECTION_BACKLOG,
    decided_predicates,
    check_text,
    check_tree,
    citation_defects,
    iter_claims,
    iter_section_citations,
    predicate_holds,
    read_text,
    section_backlog,
    section_citation_defects,
    tracked_text_files,
)

ROOT = Path(__file__).resolve().parents[1]


def test_no_source_citation_is_dead():
    """The half nobody has to opt into: `<mod>.py::<symbol>` is already the house style, so guard it.

    Two defects, and both were live on master when this landed. A path that resolves to nothing
    (`cli.py::_engine` — that module became the `looplab/cli/` PACKAGE) and a symbol that is not in
    the file it is cited from: `events/replay.py::_card_debug_leaf_children` was written in TWO
    places while the function lives in `events/card_ledger.py`, which a third site spelled
    correctly — one fact, three copies, two wrong.

    A `<mod>.py:NNN` citation is refused outright rather than resolved. That is not "hard to check",
    it is UNCHECKABLE: an edit anywhere above the cited line silently re-points it, which is exactly
    how BACKLOG §0.3's eight went dead without a single commit mentioning them. CLAUDE.md already
    tells you to locate by SYMBOL; this is that instruction with a guard behind it.

    And a `§` citation must name a section of the doc it resolves against (review 2026-09-22,
    TST-07): 71 did not when that landed, among them a section 331 that doc 56 never had. The rows
    of `tests/data/section_citations_unresolved.txt` are the part not yet corrected.
    """
    defects = citation_defects(ROOT)
    assert not defects, (
        "source citations that no longer resolve — re-point each at where its subject lives (or "
        "drop the citation):\n  " + "\n  ".join(defects))


def test_a_bare_test_file_citation_must_name_a_file_that_exists(tmp_path):
    """The "pinned by `tests/test_x.py`" citation carries no `::symbol`, so the rule above never read
    it — and three of the 371 in `looplab/` named files that did not exist (doc 50 CO-07). Driven on
    a synthetic tree: the dead one is reported, a live one, the `::` form and a `(dot)py` history
    mention are not, and the `::` form is still reported ONCE, by its own rule, when it is dead."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_real.py").write_text("def test_it():\n    pass\n", encoding="utf-8")
    pkg = tmp_path / "looplab"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        "# pinned by `tests/test_real.py` and by `tests/test_real.py::test_it`\n"
        "# a file that never existed: `tests/test_gone(dot)py`, spelled as history\n"
        "X = 1\n", encoding="utf-8")
    assert citation_defects(tmp_path) == []

    (pkg / "dead.py").write_text(
        "# `tests/test_gone.py` holds this true\n"
        "# `tests/test_gone.py::test_it` too\n", encoding="utf-8")
    defects = citation_defects(tmp_path)
    bare = [d for d in defects if "no such test file" in d]
    symbol = [d for d in defects if "no file at" in d]
    assert len(bare) == 1 and "looplab/dead.py: `tests/test_gone.py`" in bare[0], defects
    assert len(symbol) == 1 and "tests/test_gone.py::test_it" in symbol[0], defects
    assert len(defects) == 2, "the `::` form must not be reported twice"


# ---------------------------------------------------------------------------------------------
# `§` section citations (review 2026-09-22, TST-07). The sign is spelled `§` in this file so
# the tree-wide guard above never reads these fixtures as citations of the REAL docs.
S = "§"


def _section_tree(tmp_path: Path) -> Path:
    """A synthetic tree with each addressable shape, and beside it one shape that is NOT a section.

    doc 56 (the notebook): `## 1.`, `## S2 —`, `### 2.1`, a `**21.1 —**` sub-label, 330 and 332 with
    no 331 between them — and a heading that OPENS WITH A CITATION (`### S114 is weaker…`) plus a
    heading inside a code fence, neither of which is a section. doc 17 (PART IV/V) shares 12 and 21.1
    with it; doc 35 numbers a LIST under its 7; BACKLOG numbers `15. **…**` items; doc 16 is the one
    the `arch-review` alias names.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "56-notebook.md").write_text(
        "# 56 — notebook\n\n"
        f"## 1. First\n\n## {S}2 — Second\n\n### 2.1 A dotted subsection\n\n"
        "## 3. Third\n\n## 4. Fourth\n\n## 12. Twelve\n\n"
        "**21.1 — a bold sub-label, and `**338.26**` below is a bold NUMBER, not a label**\n\n"
        "**338.26** seconds\n\n"
        f"### {S}114 is weaker than four points made it look\n\n"
        "```\n## 999. a heading inside a fence\n```\n\n"
        "## 330 — before the gap\n\n## 332 — after the gap\n", encoding="utf-8")
    (docs / "17-plan.md").write_text(
        "### 12. LLM-as-a-Verifier\n\n#### 21.1 D0 — the same key as the notebook's label\n\n"
        "##### 21.20.5 Retrieval and the context contract\n", encoding="utf-8")
    (docs / "35-options.md").write_text(
        "## 7. What I could not determine\n\n1. **One.**\n2. **Two.**\n", encoding="utf-8")
    (docs / "16-review.md").write_text("## 5. P2/P3 findings\n", encoding="utf-8")
    (docs / "BACKLOG.md").write_text(
        f"### {S}0.8 The memo summary\n\n15. **Drift detection is absent.**\n", encoding="utf-8")
    (tmp_path / "looplab").mkdir()
    # One rule per line, each line opening with words, so no citation inherits its neighbour's doc
    # through the chain rule unless the line is ABOUT the chain rule.
    (tmp_path / "looplab" / "mod.py").write_text("\n".join([
        f"# bare, the notebook's: {S}1, {S}2 and {S}2.1; bare, PART IV's: {S}21.20.5",
        f"# bare keys BOTH docs carry: {S}12 (an integer) and {S}21.1 (a dotted key)",
        f"# the gap: {S}331",
        f"# a heading that opens with a citation is not a section: {S}114",
        f"# a fence is not a heading: {S}999",
        f"# a bold number is not a label: {S}338.26",
        f"# named and resolving: docs/BACKLOG.md {S}15",
        f"# named and resolving: BACKLOG's {S}0.8",
        f"# named and resolving: doc 35 {S}7, and a list item: doc 35 {S}2",
        f"# named through the alias: arch-review {S}5",
        # Named and NOT resolving. Every key is a real section of the NOTEBOOK, so each of these
        # would pass if it were read bare — the defect is the proof that the name was read.
        f"# no such section: doc 35 {S}7.2",
        f"# the chain rule: doc 35 {S}7/{S}3",
        f"# the doc named after the key: {S}4 of docs/35",
        f"# past an item id: doc 35 XP-01 {S}330",
        f"# through the alias: arch-review {S}332",
        f"# no such doc: doc 99 {S}1",
        "# a doc named at the end of one comment line and cited on the next (docs/BACKLOG.md",
        f"# {S}0.8) is one citation",
        "X = 1",
    ]) + "\n", encoding="utf-8")
    return tmp_path


def test_a_section_citation_must_name_a_section_of_the_doc_it_resolves_against(tmp_path):
    """The resolver, driven on every shape it reads, with both halves of each rule asserted.

    A BARE citation resolves in doc 56 or doc 17; a NAMED one only in the doc it names — which is
    why every named defect below uses a key the notebook DOES carry: read bare, each would pass,
    so the defect proves the name was read (before the sign, through `of <doc>`, through a chain
    `doc 35 S7/S3`, past an item id, through the `arch-review` alias). The notebook's own shapes are
    held from both sides too: `S2.1` and `S21.1` resolve, while a heading that opens with a
    citation (`S114`), a heading in a code fence (`S999`) and a bold NUMBER (`S338.26`) are not
    sections. MUTATION: drop the chain rule, the `of` lookahead, the alias or the fence toggle and
    this set changes.
    """
    root = _section_tree(tmp_path)
    defects = {c.backlog_key for c in section_citation_defects(root)}
    assert defects == {
        f"looplab/mod.py::{S}331", f"looplab/mod.py::{S}114", f"looplab/mod.py::{S}999",
        f"looplab/mod.py::{S}338.26",
        f"looplab/mod.py::doc 35 {S}7.2", f"looplab/mod.py::doc 35 {S}3",
        f"looplab/mod.py::doc 35 {S}4", f"looplab/mod.py::doc 35 {S}330",
        f"looplab/mod.py::doc 16 {S}332", f"looplab/mod.py::doc 99 {S}1",
    }, defects

    messages = citation_defects(root)
    assert any(f"`{S}331` names no section of doc 56 or doc 17, the docs a BARE" in m
               for m in messages), messages
    assert any(f"`doc 35 {S}7.2` — docs/35-options.md has no section 7.2" in m
               for m in messages), messages
    assert any(f"`doc 99 {S}1` — there is no doc 99 under docs/" in m for m in messages), messages


def test_a_key_both_bare_docs_carry_is_attributed_by_its_shape(tmp_path):
    """The tie-break is statable, so it is stated: when doc 56 AND doc 17 carry a bare key, an
    INTEGER is read as the notebook's and a DOTTED key as PART IV/V's. It decides attribution only
    (the verdict is "resolves" either way), and the corpus is why: the 182 bare dotted citations
    whose key both docs carry all sit in the concept and trust code, none in benchmarks/."""
    root = _section_tree(tmp_path)
    resolved = {c.key: c.resolved for c in iter_section_citations(root) if c.doc is None}
    assert resolved["12"] == "56" and resolved["21.1"] == "17", resolved
    assert resolved["2.1"] == "56" and resolved["21.20.5"] == "17", resolved


def test_the_section_backlog_is_subtracted_and_a_tree_without_docs_is_not_condemned(tmp_path):
    """Two boundaries of `citation_defects`, driven. A backlog row silences EXACTLY its key — the
    operator pre-flight (`python -m looplab.core.claimpin`) stays at 0 on a tree whose only defects
    are recorded — and a tree with no `docs/` at all reports no `§` defect: with nothing to resolve
    against every citation would read as dangling, and "cannot tell" must not condemn."""
    root = _section_tree(tmp_path)
    before = citation_defects(root)
    backlog = root / SECTION_BACKLOG
    backlog.parent.mkdir(parents=True)
    backlog.write_text(f"# a reason line is not a row\nlooplab/mod.py::{S}331\n", encoding="utf-8")
    assert section_backlog(root) == [f"looplab/mod.py::{S}331"]
    after = citation_defects(root)
    assert len(after) == len(before) - 1, (before, after)
    assert not any(f"`{S}331`" in m for m in after), after

    bare = tmp_path / "no-docs"
    (bare / "looplab").mkdir(parents=True)
    (bare / "looplab" / "mod.py").write_text(f"# {S}331\n", encoding="utf-8")
    assert list(iter_section_citations(bare)) == []


def test_the_section_backlog_only_shrinks_and_names_live_defects():
    """`tests/data/section_citations_unresolved.txt` is the review's backlog, in the house style of
    `containment_unreviewed.txt`: correcting a citation means deleting its row. A row whose citation
    now resolves (or is gone) is STALE and red, so the file never lists a closed defect; and the
    ceiling below is the size on the day the resolver landed — lower it as rows go, never raise it.
    MUTATION: correct a listed citation without deleting its row -> red; add a row -> red."""
    rows = section_backlog(ROOT)
    assert len(rows) == len(set(rows)), "duplicate rows"
    live = {c.backlog_key for c in section_citation_defects(ROOT)}
    stale = [r for r in rows if r not in live]
    assert not stale, (f"these {SECTION_BACKLOG} rows name no unresolved citation any more — delete "
                       "them:\n  " + "\n  ".join(stale))
    assert len(rows) <= 2, len(rows)


def test_every_claim_pin_is_well_formed():
    """A `CLAIM[…]` with no `decided:` clause is the unpinned sentence this convention replaces."""
    bad: list[str] = []
    for path in tracked_text_files(ROOT):
        text = read_text(path)
        if "CLAIM[" not in text:
            continue
        rel = path.relative_to(ROOT)
        for slug, window in iter_claims(text):
            decided = DECIDED.search(window)
            if not decided:
                bad.append(f"{rel}: CLAIM[{slug}] carries no `decided:` clause")
                continue
            for pred in decided_predicates(decided).split("+"):
                if not pred.startswith(KINDS):
                    bad.append(f"{rel}: CLAIM[{slug}] predicate {pred!r} is not a known kind")
    assert not bad, "malformed claim pins:\n  " + "\n  ".join(bad)


def test_each_claim_slug_names_exactly_one_claim():
    """Same property as the open-item index's, for the same reason: the slug is the identity, so a
    pin survives its sentence moving between files — and two claims under one slug make the grep
    that answers "what does this repo pin?" ambiguous."""
    seen: dict[str, list[str]] = {}
    for path in tracked_text_files(ROOT):
        text = read_text(path)
        for m in CLAIM_MARKER.finditer(text):
            seen.setdefault(m.group(1), []).append(str(path.relative_to(ROOT)))
    dupes = {slug: where for slug, where in seen.items() if len(where) > 1}
    assert not dupes, f"a slug names exactly one claim; declared more than once: {dupes}"


def test_every_claim_pin_still_holds():
    """THE guard. Every pinned claim re-derives its own decider against the real tree, every run.

    A failure here means the SENTENCE is false. Fix the sentence or fix the code — do not delete the
    pin and leave the prose, which is the one move that defeats this convention.
    """
    stale = check_tree(ROOT)
    assert not stale, (
        "recorded claims whose deciding site no longer says what they claim — correct the SENTENCE "
        "(or the code it describes); deleting the pin and keeping the prose is the failure this "
        "guard exists to make impossible:\n  " + "\n  ".join(stale))


def test_the_index_is_not_empty():
    """A convention with no corpus looks authoritative while being empty."""
    slugs = [m.group(1) for path in tracked_text_files(ROOT)
             for m in CLAIM_MARKER.finditer(read_text(path))]
    assert len(slugs) >= 3, f"the claim index has collapsed to {len(slugs)} pins"


# ---------------------------------------------------------------------------------------------
# Negative controls. A guard nobody has driven is a claim about a guard.


def test_a_pin_cannot_satisfy_itself(tmp_path):
    """The failure `test_open_item_index` was fixed for on 2026-08-19, held here in one place.

    Without stripping marker lines, `present:<literal>@<path>` is satisfied by the very line that
    states it, and `absent:` is falsified by it — so a claim would prove itself and a guard would
    report a live defect as shipped. The open-item index's `proof:` lines are stripped by the same
    function, so neither family can satisfy the other either.
    """
    # The marker is BUILT, never written literally: a fixture spelling `CLAIM[…]` would enter the
    # very index this file guards, and a guard whose own examples are indexed entries is the
    # satisfiable-by-a-comment failure one level up.
    marker = "CLAIM" + "[self-satisfying]"
    target = tmp_path / "mod.py"
    target.write_text(f"# {marker} decided:present:NEVER_WRITTEN@mod.py\n")
    holds, why = predicate_holds("present:NEVER_WRITTEN@mod.py", root=tmp_path)
    assert not holds and "GONE" in why, (
        "a pin's own text satisfied its own predicate — the marker must be stripped before the "
        "file is read")


def test_a_line_predicate_refuses_two_literals_that_merely_co_occur(tmp_path):
    """`line:` is the predicate the e5 defect needed, and this is the defect itself, driven.

    `present:8k x 2gpu@bench.md` HOLDS on the operator's benchmark file — that string is in it, on
    `rubert-tiny-lite`'s row — so a pin using it would have gone green while the claim ("the
    e5-small recipe is 8k x 2") was false, and three nodes still die. Binding both literals to ONE
    line is what separates "this string occurs" from "this string is said about that subject".
    """
    bench = tmp_path / "bench.md"
    bench.write_text("| rubert-tiny-lite 20 epochs; 16k overall bs (8k x 2gpu) | 0.78 |\n"
                     "| e5-small-en-ru; batch_size 1750 on 4 gpus | 0.89 |\n")
    weak, _ = predicate_holds("present:8k x 2gpu@bench.md", root=tmp_path)
    assert weak, "the weak predicate is supposed to hold — that is the whole problem with it"
    strong, why = predicate_holds("line:e5-small&&8k x 2gpu@bench.md", root=tmp_path)
    assert not strong and "belong together" in why, (
        "`line:` must refuse two literals that appear in the file but never on the same line")
    right, _ = predicate_holds("line:rubert-tiny-lite&&8k x 2gpu@bench.md", root=tmp_path)
    assert right, "the TRUE reading of the same file must hold"


def test_a_repo_pin_may_not_cite_an_absolute_path(tmp_path):
    """The suite must pass against a bare `git archive HEAD` tree, so an in-repo pin cannot depend
    on this box. A task GOAL legitimately can — it is about the operator's own machine — which is
    why `allow_absolute` is a parameter and the two carriers pass it differently."""
    holds, why = predicate_holds("present:x@/etc/hostname", root=tmp_path, allow_absolute=False)
    assert not holds and "absolute path" in why
    problems = check_text("CLAIM" + "[abs-in-repo] decided:present:x@/etc/hostname", "t",
                          root=tmp_path, allow_absolute=False)
    assert problems and "absolute path" in problems[0]


def test_a_dead_citation_in_a_pin_is_itself_a_failure(tmp_path):
    """Property 2 of the open-item index, inherited: the pin survives its sentence MOVING, because
    a decider that no longer exists goes red rather than silently passing."""
    holds, why = predicate_holds("present:anything@no/such/file.py", root=tmp_path)
    assert not holds and "does not exist" in why


@pytest.mark.parametrize("pred", ["present:x", "line:onlyone@f.md", "sideways:x@f.md"])
def test_a_malformed_predicate_is_refused_rather_than_ignored(tmp_path, pred):
    """A predicate the evaluator cannot parse must FAIL, never quietly pass — a pin that is green
    because nothing understood it is worse than no pin, which is the `⬜`-with-extra-steps failure
    the open-item index's well-formedness test exists for."""
    (tmp_path / "f.md").write_text("nothing\n")
    holds, _ = predicate_holds(pred, root=tmp_path)
    assert not holds


def test_the_task_goal_carrier_catches_the_defect_that_killed_a_run(tmp_path):
    """The out-of-repo carrier, driven on a reconstruction of the goal that cost three nodes.

    A task file lives OUTSIDE the repo and cites the operator's own machine, so no pytest can ever
    check the real one — which is precisely why the surface with the measured cost gets a carrier of
    its own (`python -m looplab.core.claimpin <task.json>`, run before submitting).

    The goal below is `runs/e5small-dr-unified-v3`'s claim, verbatim in substance: "the manual
    e5-small recipe is 16k overall = 8k x 2 GPUs", labelled VERIFIED. It is `rubert-tiny-lite`'s
    row. Pinned, it goes RED before a single GPU-hour is spent — and the author writing the pin has
    to open the benchmark file to name the line, which is where they discover there isn't one.
    """
    import json

    bench = tmp_path / "bench.md"
    bench.write_text("| sergeyzh/rubert-tiny-lite 20 epochs; 16k overall bs (8k x 2gpu) | 0.78 |\n"
                     "| e5-small-en-ru BASELINE; batch_size 1750, n_gpus 4 | 0.89 |\n")
    task = tmp_path / "task.json"

    false_goal = ("The manual benchmark's best e5-small recipe is 16k overall assembled as "
                  f"8k x 2 GPUs. CLAIM" + "[e5-recipe] decided:`line:e5-small&&8k x 2gpu@"
                  f"{bench}`")
    task.write_text(json.dumps({"task": {"goal": false_goal}}))
    from looplab.core.claimpin import check_task_goal

    problems = check_task_goal(task, root=ROOT)
    assert problems and "belong together" in problems[0], (
        "the goal claim that killed run 3 must be REFUSED by its own pin")

    true_goal = ("The '8k x 2 GPUs' recipe in that file is rubert-tiny-lite's, not e5-small's. "
                 "CLAIM" + "[e5-recipe-true] decided:`line:rubert-tiny-lite&&8k x 2gpu@"
                 f"{bench}`")
    task.write_text(json.dumps({"task": {"goal": true_goal}}))
    assert not check_task_goal(task, root=ROOT), "the CORRECTED sentence must pass"


def test_a_goal_with_no_pins_is_reported_as_UNCHECKED_not_as_clean(tmp_path, capsys):
    """`0 claim defect(s)` over ZERO pins must never read like `every claim checks out`.

    DRIVEN THROUGH `_main`, not asserted about a helper, because the defect lives in what the
    OPERATOR sees. Measured 2026-08-20 the first time this checker was pointed at the live e5 task:
    it answered a bare "0 claim defect(s)" about a goal carrying no pins at all — i.e. about exactly
    the sentences whose falsity had cost three nodes days earlier. A checker that cannot say "I
    checked nothing" reproduces, in the tool built to abolish it, the vacuous green this whole
    convention is for; it is the same shape as an open-item marker inheriting its neighbour's
    falsifier. Both halves are asserted, because a denominator that also failed to appear for a goal
    that DOES carry pins would just be noise the reader learns to skip.
    """
    import json

    from looplab.core.claimpin import _main, check_task_goal

    # THE EXIT CODE IS NOT THIS TEST'S BUSINESS, and asserting on it was this test's own defect until
    # 2026-08-20. `_main` also walks the whole repo, so its return value answers "does this TREE carry
    # any claim defect at all", while the subject here is "how is a PINLESS GOAL reported". Coupled,
    # an unrelated dead citation elsewhere in the repository reddened a test about a temp file — an
    # assertion whose input is far wider than its claim, which is the shape this whole file exists to
    # prevent. The goal-scoped verdict is asserted below, on `check_task_goal`, whose input is only
    # the file.
    unpinned = tmp_path / "unpinned.json"
    unpinned.write_text(json.dumps({"task": {"goal": "Batch 8192 fits in 80.6 GiB with checkpointing."}}))
    _main([str(unpinned)])
    out = capsys.readouterr().out
    assert "NO PINS AT ALL" in out and str(unpinned) in out, (
        "a goal carrying no pins must be named as unchecked, not silently folded into a clean count")

    bench = tmp_path / "bench.md"
    bench.write_text("| e5-small-en-ru BASELINE; batch_size 1750, n_gpus 4 | 0.89 |\n")
    pinned = tmp_path / "pinned.json"
    pinned.write_text(json.dumps({"task": {"goal": (
        "The e5-small baseline is batch 1750 over 4 GPUs. CLAIM"
        + f"[e5-baseline] decided:`line:e5-small&&batch_size 1750@{bench}`")}}))
    _main([str(pinned)])
    out = capsys.readouterr().out
    assert "NO PINS AT ALL" not in out, "a goal that DOES carry a pin must not be reported unchecked"
    assert "1 pin(s) evaluated" in out and str(pinned) in out, (
        "the denominator must name the surface it counted, or it cannot be acted on")

    # ...and the goal-scoped verdicts, which ARE this test's business: one file in, one answer out.
    assert not check_task_goal(unpinned, root=ROOT), "a pinless goal has no DEFECTS to report"
    assert not check_task_goal(pinned, root=ROOT), "the pinned goal's claim holds against its file"


def test_the_unpinned_diagnostic_names_the_citation_case():
    """Five reds in this family were the same move: prose citing an existing claim in bracket form.

    The convention already decides it -- `test_each_claim_slug_names_exactly_one_claim` exists so a
    slug names ONE declaration -- but the message said only "carries no `decided:` clause", which
    reads as an instruction to add one. Adding one is the wrong repair: it creates the duplicate the
    next test then rejects. The message now names the alternative.
    """
    slug = "some-" + "unpinned-" + "example"
    text = "CLAIM" + "[" + slug + "] a sentence with no deciding clause after it."
    out = check_text(text, "x.md", root=Path("."), allow_absolute=False)
    assert out, "an unpinned claim must still be a defect"
    assert "drop the brackets" in out[0], out[0]
    assert slug in out[0]
