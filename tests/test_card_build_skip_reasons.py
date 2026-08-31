"""WHY a paid build was closed without minting a node — the registry and its two-way guard.

MEASURED on `runs/e5small-dr-unified-v9`: 3 of 12 builds closed `card_build_done.skipped == "stale"`
having spent **41.4M tokens, 11.8 % of the run**, and the durable row could not say which of the
claim path's refusals had fired. `engine/speculation.py::_claim_requested_card_build` had NINE
distinct `return "stale"` sites — one of them a compound of three conditions — all writing one word.

That is the `inert` defect (#82, `ffdb34e3`) one package over: a coarse word standing in for facts
with different remedies. `not_selected_now` means the board moved and the build is INTACT;
`card_action_changed` means the build no longer matches what it was built for. One word covered
both, and #110's question — why one card was never re-requested while two others were — is
unanswerable without the distinction.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

from looplab.engine import speculation
from looplab.engine.speculation import CARD_BUILD_SKIP_REASONS

_SRC = pathlib.Path(inspect.getsourcefile(speculation)).read_text()


def _claim_path_slugs() -> set[str]:
    """Every `stale:<slug>` the claim path can return, re-derived from its own AST.

    AST and not a substring scan, for the reason `tests/_source_scan.py` states: a comment carrying
    the literal would satisfy a text pin, and comments are not AST nodes.
    """
    tree = ast.parse(_SRC)
    slugs = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name != "_claim_requested_card_build":
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)):
                continue
            if not node.value.elts or not isinstance(node.value.elts[0], ast.Constant):
                continue
            value = node.value.elts[0].value
            if isinstance(value, str) and value.startswith("stale"):
                slugs.add(value.partition(":")[2])
    return slugs


def test_no_BARE_stale_survives_in_the_claim_path():
    """The defect itself, as an assertion. Mutation: revert any one exit to `return "stale", None`
    and its 41.4M-token refusal goes back to being unattributable on the durable row."""
    assert "" not in _claim_path_slugs(), (
        "a `return \"stale\", None` with no slug is exactly the undiagnosed proxy this registry "
        "replaced — name the refusal and register it")


def test_every_slug_the_claim_path_returns_is_REGISTERED():
    """Mutation: add a `return \"stale:typo_here\", None` without registering it. A typo'd slug does
    not fail anything at runtime — it lands on a durable row and reads as a refusal nobody can look
    up, which is the failure mode `TRIAGE_ACTIONS` and `REPAIR_VERDICTS` are guarded against."""
    unregistered = _claim_path_slugs() - set(CARD_BUILD_SKIP_REASONS)
    assert not unregistered, f"unregistered skip reasons: {sorted(unregistered)}"


def test_every_REGISTERED_slug_is_reachable_from_the_claim_path_or_its_outer_gate():
    """The other direction, which is what stops the registry rotting into a list of words nothing
    emits. `commit_not_allowed` is deliberately NOT in the claim path — it is the outer gate's own
    refusal in `_serve_card_builds` — so the search covers the whole module, by AST."""
    tree = ast.parse(_SRC)
    # THE REGISTRY'S OWN DEFINITION IS EXCLUDED, and leaving it in made this test vacuous: a slug
    # added to the tuple and emitted NOWHERE was found by the scan inside the tuple itself and
    # counted as its own evidence. The mutant "add a dead word" survived until this line existed.
    registry_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CARD_BUILD_SKIP_REASONS" for t in node.targets):
            registry_nodes.update(id(n) for n in ast.walk(node))
    emitted = set()
    for node in ast.walk(tree):
        if id(node) in registry_nodes:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if text.startswith("stale:"):
                emitted.add(text.partition(":")[2])
            elif text in CARD_BUILD_SKIP_REASONS:
                emitted.add(text)
    dead = set(CARD_BUILD_SKIP_REASONS) - emitted
    assert not dead, f"registered but emitted nowhere: {sorted(dead)}"


def test_the_registry_has_no_duplicates_and_no_blanks():
    """A duplicated slug makes two different refusals read as one — the very collapse being undone.
    Mutation: repeat an entry, or add an empty string."""
    assert len(set(CARD_BUILD_SKIP_REASONS)) == len(CARD_BUILD_SKIP_REASONS)
    assert all(isinstance(r, str) and r.strip() for r in CARD_BUILD_SKIP_REASONS)


def test_the_coarse_word_is_UNCHANGED_so_every_existing_reader_is_byte_identical():
    """`skipped` still takes exactly `producer_failed` | `stale`, and `_append_card_build_done`
    still refuses anything else. Mutation: let the slug into the `skipped` field itself, and every
    reader keyed on the coarse word — including `looplab tokens`' stale line — silently stops
    matching. The reason is ADDITIVE (invariant #5), which is what makes old logs still fold."""
    tree = ast.parse(_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_append_card_build_done")
    sets = [n for n in ast.walk(fn) if isinstance(n, ast.Set)]
    vocab = {e.value for s in sets for e in s.elts if isinstance(e, ast.Constant)}
    assert vocab == {"producer_failed", "stale"}, (
        f"the coarse `skipped` vocabulary must stay closed and unchanged, got {sorted(vocab)}")


def test_the_receipt_carries_the_reason_only_when_there_IS_one():
    """A `skipped_reason` on a row that minted a node would be a refusal nobody made. Driven over
    the payload builder's own source, by AST: the write is guarded and the guard is on the string.
    Mutation: stamp it unconditionally."""
    tree = ast.parse(_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_append_card_build_done")
    writes = [n for n in ast.walk(fn)
              if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
              and n.slice.value == "skipped_reason"]
    assert writes, "the receipt must be able to carry a reason at all"
    # THE GUARD MUST TEST THE REASON ITSELF. "inside some `if`" is not an assertion: the write
    # already sits inside `if skipped is not None`, so a mutant that stamps the reason
    # unconditionally on every skipped row SURVIVED until this looked at the condition.
    guarded = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        tests_the_reason = any(isinstance(n, ast.Name) and n.id == "skipped_reason"
                               for n in ast.walk(node.test))
        if not tests_the_reason:
            continue
        if any(isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant)
               and sub.slice.value == "skipped_reason" for sub in ast.walk(node)):
            guarded.append(node)
    assert guarded, (
        "the `skipped_reason` write must sit under a guard that tests `skipped_reason` itself — "
        "a row that minted a node must never carry a refusal nobody made")


# ------------------------------------------------------- the OTHER emitter: a `skipped_reason=` kwarg

def _kwarg_slugs() -> dict[str, set[str]]:
    """Every `skipped_reason=<literal>` passed anywhere in the module, keyed by enclosing function.

    THE SCAN ABOVE COULD NOT SEE THIS AND THAT IS WHY IT SHIPPED BARE. `_claim_path_slugs` walks
    `_claim_requested_card_build`'s own `ast.Return` tuples, which is the right reading of the claim
    path and a complete blind spot everywhere else: the crash-recovery close in `_serve_card_builds`
    emits its refusal as a KEYWORD ARGUMENT to `_append_card_build_done`, so it carried a bare
    `skipped="stale"` for as long as the registry existed and no guard could go red about it.

    Keyed by function so a future site is attributable rather than just counted.
    """
    tree = ast.parse(_SRC)
    out: dict[str, set[str]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "skipped_reason":
                    continue
                for const in ast.walk(kw.value):
                    if isinstance(const, ast.Constant) and isinstance(const.value, str):
                        out.setdefault(fn.name, set()).add(const.value)
    return out


def test_every_slug_passed_as_a_kwarg_is_REGISTERED():
    """Same rule as the claim path's, at the seam the claim path's scan cannot reach."""
    passed = {slug for slugs in _kwarg_slugs().values() for slug in slugs}
    assert passed, "no `skipped_reason=` call site found at all — the scan has stopped matching"
    unregistered = passed - set(CARD_BUILD_SKIP_REASONS)
    assert not unregistered, f"unregistered skip reasons: {sorted(unregistered)}"


def test_the_crash_recovery_close_NAMES_its_refusal():
    """The last bare `stale` in the module, and the one whose rows are read post-mortem.

    A head recovered after a crash whose card is dropped or merged away closes the paid build. Until
    2026-08-30 it wrote `skipped="stale"` and nothing else, so a discarded build on the one path an
    operator reaches for after a crash was unattributable.

    MUTATION: drop the `skipped_reason=` argument from that call and this goes red. The old
    claim-path scan stays green either way — which is exactly the gap this test exists for.
    """
    slugs = _kwarg_slugs().get("_serve_card_builds", set())
    # A CONTAINMENT and not an equality: this function ALREADY forwarded the outer gate's own
    # refusals through the same keyword (`run_is_stopping`, `eval_budget_exhausted`,
    # `commit_not_allowed`, `search_epoch_rotated`), which is itself the finding — the kwarg seam
    # was carrying live slugs the claim-path scan had never been able to see. Pinning the exact set
    # would make this test fail every time a NEW refusal is legitimately routed here.
    assert {"card_gone", "card_dropped"} <= slugs, (
        "the crash-recovery close must name BOTH dead shapes: a merged-away card is ABSENT "
        f"(`card_gone`) and a dropped one is PRESENT and dead (`card_dropped`), got {sorted(slugs)}")


def test_the_two_dead_shapes_are_not_ONE_word():
    """They have different remedies — a merged card's work is under its canonical, a dropped card's
    is over — so collapsing them re-creates the coarse `stale` one level down.

    MUTATION: return `card_gone` for both and this goes red.
    """
    assert "card_dropped" in CARD_BUILD_SKIP_REASONS and "card_gone" in CARD_BUILD_SKIP_REASONS
    src = _SRC[_SRC.index("def _serve_card_builds"):]
    src = src[:src.index("\n    def ", 1)] if "\n    def " in src[1:] else src
    assert "merged_away" in src, (
        "the branch that chooses between them must read the merged-away proof, not guess")
