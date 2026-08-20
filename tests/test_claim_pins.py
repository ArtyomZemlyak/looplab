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
    CLAIM_MARKER,
    DECIDED,
    decided_predicates,
    check_text,
    check_tree,
    citation_defects,
    iter_claims,
    predicate_holds,
    read_text,
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
    how §0.3's eight went dead without a single commit mentioning them. CLAUDE.md already tells you
    to locate by SYMBOL; this is that instruction with a guard behind it.
    """
    defects = citation_defects(ROOT)
    assert not defects, (
        "source citations that no longer resolve — re-point each at where its subject lives (or "
        "drop the citation):\n  " + "\n  ".join(defects))


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
                if not pred.startswith(("absent:", "present:", "missing:", "line:")):
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
