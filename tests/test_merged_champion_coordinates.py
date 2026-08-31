"""A MERGED champion declares nothing, so `params_overridden`'s premise is absent.

`search/operators.py::merge_idea` returns `Idea(operator="merge", params=<the ARITHMETIC MEAN of
the parents' params>)`, and the node trains nothing of its own: it averages two parents' weights and
scores the average. So `params_overridden` — "the champion's own committed code assigns a DIFFERENT
value to a parameter its `Idea` DECLARES" — is false in BOTH halves, while the underlying worry is
true and sharper than that slug can put it: a mean-merge is published at coordinates NO
configuration ever occupied, which is a fact about merging and not about two files disagreeing.

MEASURED (2026-08-29) by replaying `champion_metric_caveats` over every `runs/**/events.jsonl` on
this box — 9 logs, 0 unreadable, 7 with a champion, 4 caveated:

    e5small-dr-unified-v4   node 13   0.793411   merge   <- the SECOND-BEST number on this box
        pipeline `merge` + `score`; carried `params_overridden` cited to
        vectorsearch/configs/config.yaml:265's 2048 against a declared 4096, on a node that ran
        zero epochs at no batch size and no learning rate.

THE SWAP CANNOT CLEAN ANYTHING, and that is why it is safe to ship: over that corpus it re-labels
exactly ONE champion and newly caveats ZERO. No number goes from caveated to clean, none from clean
to caveated, and the two real `params_overridden` champions (v2 node 1, v8 node 3 — both `draft`
ideas that really trained) are untouched. Simply SUPPRESSING the caveat was refused for the reason
the whole family exists: it would make the box's second-best number read clean when it is the least
well-located result in the corpus.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

from looplab.core.models import Idea, Node, RunState
from looplab.engine.champion_caveats import (
    CHAMPION_CAVEAT_MERGED_COORDINATES, CHAMPION_CAVEAT_PARAMS_OVERRIDDEN, CHAMPION_CAVEATS,
    champion_metric_caveats,
)


def _state(node):
    """A REAL `RunState`, not a stub. `champion_metric_caveats` calls `unreliable_metric_ids`, which
    reads `reward_hacks` / `violations` / `findings` — a hand-rolled double grows a field every time
    that join learns to look somewhere new, and passes by not having the attribute rather than by
    the rule holding."""
    return RunState(nodes={node.id: node}, best_node_id=node.id)


def _node(operator, *, diverged=True):
    """A champion whose APPLIED-params record reports a divergence — the witness that used to raise
    `params_overridden` on the merge node."""
    idea = Idea(operator=operator, params={"train.training.batch_size": 4096.0},
                rationale="mean-merge of nodes 11,10" if operator == "merge" else "a real idea")
    prov = {"applied_params": {
        "authority": "committed", "checked": 1, "declared": 1,
        "applied": {"train.training.batch_size": 2048.0},
        "diverged": ([{"param": "train.training.batch_size", "declared": 4096.0,
                       "applied": 2048.0, "file": "configs/config.yaml", "line": 265}]
                     if diverged else []),
        "stages": ["merge", "score"] if operator == "merge" else ["mine", "train", "score"],
    }}
    # `Node.operator` is a field of its own beside `Node.idea.operator`; the caveat reads the
    # IDEA's, which is what `merge_idea` sets, so both are filled here exactly as a real fold has
    # them and the test cannot pass by accident on the wrong one.
    return Node(id=13, parent_id=None, idea=idea, operator=operator, code="", metric=0.793411,
                status="evaluated", metric_provenance=prov)


def test_a_MERGED_champion_gets_merged_coordinates_and_not_params_overridden():
    """The defect. Mutation: drop the `operator == "merge"` branch and the box's second-best number
    is republished as `params_overridden`, cited to a config file no process on it ever read."""
    caveats = champion_metric_caveats(_state(_node("merge")))
    assert CHAMPION_CAVEAT_MERGED_COORDINATES in caveats, (
        "a mean-merge champion is filed at coordinates nobody chose and must say so")
    assert CHAMPION_CAVEAT_PARAMS_OVERRIDDEN not in caveats, (
        "and it must NOT also claim its code contradicts a declaration — a merged Idea declares "
        "nothing, so that slug's premise is absent and its file citation is spurious")


def test_a_MERGED_champion_is_never_left_UNCAVEATED():
    """The safety property the whole change rests on. Mutation: make the merge branch `pass`
    instead of appending, and suppressing `params_overridden` silently cleans the second-best
    number on this box — the vacuous green this caveat family exists to abolish."""
    assert champion_metric_caveats(_state(_node("merge"))), (
        "a merge champion must carry SOME caveat; going from caveated to clean is the one outcome "
        "this change was designed to make impossible")


def test_a_merge_champion_with_NO_divergence_still_says_merged_coordinates():
    """The claim is about MERGING, not about a file disagreement. Mutation: gate the merge branch
    on the divergence witness and a merge champion whose config happens to agree reads as clean,
    though it occupies no configuration's coordinates either way."""
    caveats = champion_metric_caveats(_state(_node("merge", diverged=False)))
    assert caveats == [CHAMPION_CAVEAT_MERGED_COORDINATES]


def test_a_REAL_champion_keeps_params_overridden():
    """The two live `params_overridden` champions must not move. Mutation: key the branch on
    anything a draft idea also satisfies (`operator is not None`, a truthy `operator`) and v2 node 1
    and v8 node 3 both lose a caveat they have earned."""
    caveats = champion_metric_caveats(_state(_node("draft")))
    assert CHAMPION_CAVEAT_PARAMS_OVERRIDDEN in caveats
    assert CHAMPION_CAVEAT_MERGED_COORDINATES not in caveats


def test_the_slug_is_keyed_on_the_OPERATOR_and_never_on_the_rationale_text():
    """`merge_idea` writes BOTH a structured `operator="merge"` and a `mean-merge of nodes …`
    rationale. Mutation: read the rationale instead and any idea whose prose contains the word
    'merge' — a Researcher writing 'merge these two ideas' — is convicted of being one."""
    idea = Idea(operator="draft", params={"train.training.batch_size": 4096.0},
                rationale="mean-merge of nodes 11,10 is what this should be compared against")
    node = _node("draft").model_copy(update={"idea": idea})
    assert CHAMPION_CAVEAT_MERGED_COORDINATES not in champion_metric_caveats(_state(node))


def test_the_new_slug_is_registered():
    """Mutation: add the constant without extending `CHAMPION_CAVEATS` and the vocabulary the UI
    and every reader derive from is missing a word the engine emits."""
    assert CHAMPION_CAVEAT_MERGED_COORDINATES in CHAMPION_CAVEATS
