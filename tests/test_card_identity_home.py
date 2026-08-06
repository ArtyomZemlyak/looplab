"""Card identity lives in `core/cards.py`, and the move did not re-value a single digest (doc 25 CO-02).

`core/models.py` had grown to 2,320 lines around five separable subsystems; the card/idea identity
machinery — versioned digests, ownership receipts, the footprint and steering vocabularies they bind,
the Card provenance family and `Card` itself — moved out into `core/cards.py` and is re-exported
through `models` exactly as `core/concepts.py` already was.

Three different things can go wrong with a move like this, and only the first is loud:

* **the digest VALUE shifts.** Every one of these gates receipt acceptance on replay
  (`events/replay.py::_card_added_ownership` re-derives the digest from the durable snapshot and
  compares). A shifted value does not raise: the receipt simply stops verifying, the card drops to a
  non-selectable shadow, and a run that was legitimately calibrated is refused with no error that
  says why. So the values are pinned as LITERALS, together with the field sets they were derived
  from, so a shift's CAUSE is nameable — the pattern `tests/test_calibration_profile_home.py`
  established.
* **the seam stops being one object.** ~360 sites and every monkeypatch spell these on
  `looplab.core.models`. A re-export that COPIES rather than aliases makes each existing patch a
  silent no-op instead of an error.
* **the split closes the import cycle.** `models` imports `cards`, so a runtime import back the other
  way is an ImportError at startup — and the `Idea` annotation on two moved functions is exactly the
  shape that invites one.

The `Card` PROJECTION itself is not re-pinned here — `tests/test_card_selection_guard.py` and
`tests/test_events_replay.py` already drive it end to end. What is driven here is the one property
those cannot see: a receipt minted through the NEW spelling still verifies through the fold, which
reaches it through the OLD one.
"""
from __future__ import annotations

import ast
import subprocess
import sys

import pytest

from _source_scan import PKG, iter_trees
from looplab.core import cards, models
from looplab.core.models import Event
from looplab.events.replay import fold

# ------------------------------------------------------------------ the frozen identity values
#
# Measured on the tree, BOTH sides of the move (2026-08-05: identical before and after, together with
# a 5,699-row corpus covering every branch of every minter). Both halves must stay literals: writing
# `_EXPECTED = cards.card_action_digest(...)` compares the value to itself and can never fail.
#
# If one of these shifts:
#   * the FIELD SETS below are unchanged -> a refactor changed the DERIVATION. That is the bug this
#     file exists for; every already-issued receipt stops verifying. Do not re-pin, find the change.
#   * a field set changed too            -> a digest version legitimately grew, which means a NEW
#     version constant, not an edit to a shipped one. v1 and expanded-v1 are frozen history and can
#     never legitimately move at all.
_IDEA_DIGEST = "idea:v1:d210963498a73c4a1a7f835d509c3fd92e89919788d9d38e4ddf6eb52b7c67e1"
_CARD_V2_DIGEST = "card-action:v2:acce9bd9270dd120bd07ec8166c4baec7e71dfc5b87d05ff9e106e5e67476803"
_CARD_V1_DIGEST = "card-action:v1:751318e9566b425a077ef4383ef0c4d8bf1a3d7cce460bada653c2179229ffd3"
_CARD_EXPANDED_V1_DIGEST = (
    "card-action:v1:5fbfc9add828f6662efca1e82523f7afc52a528377317676c7a622dd0d6b78e9")
# The belief identity a card is hash-joined on. `hypothesis_id` is a FROZEN key — every ledger entry
# and every capsule that joined on it was written with this spelling — and the sha256 sibling is what
# `events/belief_projection.py` groups a belief's cards by.
_HYPOTHESIS_ID = "adam-helps-here-b6ab27"
_HYPOTHESIS_SHA = "6b8a5179597ada41c52368d219ad75d81171126fed9e2ce99128a1bec6b8aab1"

# The field sets the values above were derived over. `events/replay.py::_card_action_receipt_payload`
# projects a durable card snapshot through EXACTLY these tuples before handing it back to the minter,
# so dropping a member does not fail loudly — it re-derives a different digest for the same card.
_IDEA_FIELDS = (
    "operator", "params", "rationale", "eval_profile", "eval_timeout", "theme",
    "concepts", "concept_mode", "concepts_added", "concepts_removed", "space",
    "hypothesis", "card_id", "footprint",
)
_CARD_V1_FIELDS = (
    "operator", "params", "space", "eval_profile", "parent_id", "parent_ids",
    "scored_against", "footprint",
)
_CARD_V2_FIELDS = (
    "operator", "params", "space", "eval_profile", "eval_timeout", "parent_id", "parent_ids",
    "parent_generations", "scored_against", "scored_against_generation",
    "scored_against_empty", "footprint",
)

_ACTION = {
    "operator": "improve",
    "params": {"lr": 0.1, "wd": 0.0},
    "space": {"lr": [0.05, 0.2]},
    "eval_profile": "fast",
    "eval_timeout": 30.0,
    "parent_id": 4,
    "parent_ids": [4, 7],
    "parent_generations": {"4": 0, "7": 2},
    "scored_against": 4,
    "scored_against_generation": 1,
    "scored_against_empty": False,
    "footprint": {"gpus": 2, "gpu_mem_mib": 8192},
}


def _idea():
    return models.Idea(
        title="t", rationale="r", operator="mutate", params={"lr": 0.1},
        space={"lr": [0.05, 0.2]}, eval_profile="fast", eval_timeout=30.0,
        theme="regularization", concepts=["opt/adam"], concept_mode="full",
        hypothesis="adam helps", card_id="card-3",
        footprint={"gpus": 2, "gpu_mem_mib": 8192})


def _cause(field_sets_moved: bool) -> str:
    if field_sets_moved:
        return ("a digest field set ALSO changed, so this is a real schema change: a shipped version "
                "is frozen, so it needs a NEW version constant rather than an edited one.")
    return ("the field sets are UNCHANGED, so a refactor changed the DERIVATION — every "
            "already-issued receipt silently stops verifying and the cards it owns drop to "
            "non-selectable shadows. Do not re-pin; find the change.")


def test_the_field_sets_each_digest_is_taken_over_are_frozen():
    assert cards.IDEA_PROPOSAL_DIGEST_V1_FIELDS == _IDEA_FIELDS
    assert cards.CARD_ACTION_DIGEST_V1_FIELDS == _CARD_V1_FIELDS
    assert cards.CARD_ACTION_DIGEST_V2_FIELDS == _CARD_V2_FIELDS


@pytest.mark.parametrize("name,expected", [
    ("idea", _IDEA_DIGEST),
    ("v2", _CARD_V2_DIGEST),
    ("v1", _CARD_V1_DIGEST),
    ("expanded_v1", _CARD_EXPANDED_V1_DIGEST),
    ("hypothesis_id", _HYPOTHESIS_ID),
    ("hypothesis_sha", _HYPOTHESIS_SHA),
])
def test_no_identity_changed_value_when_the_module_changed(name, expected):
    """The receipt gate. A shifted value is silent at every layer that consumes it."""
    actual = {
        "idea": lambda: cards.idea_proposal_digest(_idea()),
        "v2": lambda: cards.card_action_digest("card-3", "adam helps", _ACTION),
        "v1": lambda: cards.legacy_card_action_digest_v1("card-3", "adam helps", _ACTION),
        "expanded_v1": lambda: cards.transitional_card_action_digest_v1(
            "card-3", "adam helps", _ACTION),
        "hypothesis_id": lambda: cards.hypothesis_id("Adam  Helps  Here"),
        "hypothesis_sha": lambda: cards.hypothesis_statement_digest("Adam  Helps  Here"),
    }[name]()
    moved = (cards.IDEA_PROPOSAL_DIGEST_V1_FIELDS != _IDEA_FIELDS
             or cards.CARD_ACTION_DIGEST_V1_FIELDS != _CARD_V1_FIELDS
             or cards.CARD_ACTION_DIGEST_V2_FIELDS != _CARD_V2_FIELDS)
    assert actual == expected, f"the {name} identity moved to {actual!r}. " + _cause(moved)


def test_the_three_card_action_versions_stay_distinct_identities():
    """v1, expanded-v1 and v2 exist BECAUSE they mint different values over the same action. Collapsing
    two of them would make a receipt verify under a version that never wrote it."""
    minted = {_CARD_V1_DIGEST, _CARD_EXPANDED_V1_DIGEST, _CARD_V2_DIGEST}
    assert len(minted) == 3


def test_the_ownership_receipts_carry_their_own_version_beside_the_digest():
    assert cards.card_ownership_receipt("card-3", "adam helps", _ACTION) == {
        "v": 2, "card_id": "card-3", "action_digest": _CARD_V2_DIGEST}
    assert cards.legacy_card_ownership_receipt_v1("card-3", "adam helps", _ACTION) == {
        "v": 1, "card_id": "card-3", "action_digest": _CARD_V1_DIGEST}
    assert cards.transitional_card_ownership_receipt_v1("card-3", "adam helps", _ACTION) == {
        "v": 1, "card_id": "card-3", "action_digest": _CARD_EXPANDED_V1_DIGEST}


def test_the_digest_preimage_boundary_moved_with_the_digest():
    """`durable_idea_payload` is `idea_proposal_digest`'s preimage boundary, not an ordinary Idea
    serializer: the digest starts AT it so an absent legacy concept envelope and an explicitly
    authored empty one keep separate identities. Editing it re-values every idea digest, which is why
    the two live in one module — and why the absent/empty distinction is pinned here."""
    absent = models.Idea(title="t", rationale="r", operator="mutate")
    authored = models.Idea(title="t", rationale="r", operator="mutate", concepts=[],
                           concept_mode="full")
    payload = cards.durable_idea_payload(absent)
    assert "concept_mode" not in payload and "concepts" not in payload
    assert cards.durable_idea_payload(authored)["concepts"] == []
    assert cards.idea_proposal_digest(absent) != cards.idea_proposal_digest(authored)


# ------------------------------------------------------------------ the seam is ONE object

# Public names defined in `core/cards.py` that `core/models.py` deliberately does NOT re-export.
# Empty today: every one of these was public on `models` before the move, so dropping any of them
# breaks an import site. A future cards-only helper goes here, as a deliberate decision rather than
# an omission.
NOT_RE_EXPORTED: frozenset[str] = frozenset()
# ...and the one PRIVATE name the seam still has to carry, because a guard test reaches the versioned
# minter through `models` to pin that it passes the shared preimage cap.
RE_EXPORTED_PRIVATES = frozenset({"_card_action_digest"})


def _cards_module_tree() -> ast.AST:
    """`core/cards.py`'s AST, through the shared walk (`tests/test_source_scan_helper.py` forbids a
    guard test rolling its own)."""
    for path, tree in iter_trees(PKG / "core"):
        if path.name == "cards.py":
            return tree
    raise AssertionError("looplab/core/cards.py is gone — card identity has no home")


def _top_level_names(tree) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_every_moved_name_still_resolves_on_models_to_the_SAME_object():
    """Not `==`: `is`. A re-export that rebuilds the object instead of aliasing it turns every
    existing `monkeypatch.setattr("looplab.core.models.<name>", ...)` into a silent no-op."""
    defined = _top_level_names(_cards_module_tree())
    public = {name for name in defined if not name.startswith("_")} - NOT_RE_EXPORTED
    assert len(public) >= 25, "the card-identity surface shrank unexpectedly"
    missing = sorted(name for name in public | RE_EXPORTED_PRIVATES
                     if not hasattr(models, name))
    assert not missing, (
        f"{missing} no longer resolve on core.models. Either re-export them through the seam or add "
        "them to NOT_RE_EXPORTED as a deliberate decision.")
    copied = sorted(name for name in public | RE_EXPORTED_PRIVATES
                    if getattr(models, name) is not getattr(cards, name))
    assert not copied, f"{copied} are COPIES on core.models, so patching either spelling misses"


def test_the_legacy_flat_spelling_reaches_the_same_module():
    """`looplab.cards` — the pre-split alias the compat finder mints from `_LAYOUT`. Registering the
    new stem is what keeps `tests/test_package_layout.py`'s two-way check honest."""
    import looplab

    assert looplab._LAYOUT["cards"] == "core"
    import looplab.cards as flat

    assert flat is cards


# ------------------------------------------------------------------ the cycle stays open


def test_cards_does_not_reach_models_at_runtime():
    """Driven in a FRESH interpreter, not asserted about the source: `models` imports `cards`, so a
    runtime import back the other way is an ImportError at startup. The `Idea` annotation on
    `durable_idea_payload`/`idea_proposal_digest` is exactly the shape that invites one, and
    `from __future__ import annotations` makes it work locally right up until someone drops the
    TYPE_CHECKING guard."""
    probe = ("import looplab.core.cards, sys; "
             "print('looplab.core.models' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            cwd=str(PKG.parent))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", (
        "importing core.cards now pulls in core.models — the cycle is closed at runtime")


def test_cards_imports_nothing_above_core():
    """`core` imports nothing above itself (CLAUDE.md layering). Card identity is consumed by the
    FOLD, which may import only `core`."""
    forbidden = []
    for node in ast.walk(_cards_module_tree()):
        module = None
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
        elif isinstance(node, ast.Import):
            module = node.names[0].name
        if module and module.startswith("looplab.") and not module.startswith("looplab.core."):
            forbidden.append(module)
    assert not forbidden, f"core/cards.py reaches above core: {forbidden}"


# ------------------------------------------------------------------ no second copy left behind


@pytest.mark.parametrize("literal", [
    "idea:v1:", "card-action:v", "LOOPLAB_FOOTPRINT", "_canonical_json_digest", "selection_ready",
])
def test_models_does_not_carry_a_second_copy_of_the_identity(literal):
    """A NEGATIVE pin, deliberately a substring (CLAUDE.md: what must not come back is the TEXT, and
    a commented-out re-derivation drifts as readily as a live one). The failure mode this catches is
    the ordinary one — someone needs a digest inside `models` and re-spells it there, so two
    functions mint two values for one logical identity and only one of them is the receipt's."""
    source = (PKG / "core" / "models.py").read_text(encoding="utf-8-sig", errors="replace")
    assert literal not in source, (
        f"core/models.py names {literal!r} again — card identity has a second spelling")


# ------------------------------------------------------------------ end to end through the fold


def test_a_receipt_minted_through_cards_still_verifies_through_the_fold():
    """The property the `is`-identity check cannot see. The engine mints through one spelling and the
    fold re-derives through the other (`events/replay.py` imports from `core.models`); if the move had
    left two derivations, this card would fold to a non-selectable shadow with nothing raised."""
    card_id, statement = "card-1", "a bounded improvement"
    action = {
        "operator": "improve", "params": {"lr": 0.2}, "space": None, "eval_profile": None,
        "eval_timeout": None, "parent_id": 1, "parent_ids": [1], "parent_generations": {"1": 0},
        "scored_against": 1, "scored_against_generation": 0, "scored_against_empty": False,
        "footprint": None,
    }
    receipt = cards.card_ownership_receipt(card_id, statement, action)
    assert receipt is not None
    rows = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "baseline"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.5}),
        ("card_added", {
            "id": card_id, "statement": statement, "source": "engine",
            "idea": {"operator": "improve", "params": {"lr": 0.2}, "eval_timeout": None},
            "parent_id": 1, "parent_ids": [1], "parent_generations": {"1": 0},
            "scored_against": 1, "scored_against_generation": 0, "scored_against_empty": False,
            "ownership_receipt": receipt,
        }),
    ]
    state = fold([Event(seq=index, type=kind, data=data)
                  for index, (kind, data) in enumerate(rows)])
    card = state.cards[card_id]
    assert card.identity.kind == "native", (
        f"the receipt no longer verifies through the fold: {card.identity.model_dump()}")
    assert card.identity.receipt_valid and card.identity.durable
    assert card.identity.action_digest == receipt["action_digest"]
    assert card.selection_ready and not card.selection_blockers
