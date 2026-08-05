"""Selection must not rank on inputs the candidate writes about itself (doc 25 SE-11).

Two terms of `card_score` fed on evidence the code itself documents as untrustworthy, on a path that
is ON by default (`Settings.card_driven_selection`):

* **coverage** was computed from `Card.concept_tags` alone — before the node is built, that is the
  taxonomy the proposing Researcher wrote about its OWN proposal while competing for selection.
  Minting a plausible new slug put every tag outside `explored` and returned 1.0, the maximal
  exploration bonus, earned by naming.
* **confidence** carried a hardcoded 65% of the foresight term. It is the foresight ranker's
  self-assessment of its own ordering, and this repository's §21.12 measurement records it as
  Pearson≈0 with realized outcome — `foresight.py::_verifier_confidence` exists to replace it, but
  only on the idea path, never on the board ranking that stamps `Card.confidence`.

Neither had a test. The change below altered live selection and all 191 existing card/strategist
tests stayed green, so this file is the coverage that was missing, not a duplicate of it.
"""
from __future__ import annotations

import pytest

from looplab.core.models import Card, CardConceptSource
from looplab.search.card_selection import (CardScoring, _coverage_signal, _foresight_signal,
                                           _independently_classified, normalize_card_scoring)

# The historical hardcoded blend, kept as a literal so the escape hatch is checked against the exact
# behaviour it claims to restore rather than against itself.
HISTORICAL_CONFIDENCE_WEIGHT = 0.65


def _receipt(**over) -> CardConceptSource:
    base = dict(kind="node", node_id=1, node_generation=0, provenance="classifier",
                membership_present=True, complete=True, receipt_valid=True)
    base.update(over)
    return CardConceptSource(**base)


def _card(tags, source=None) -> Card:
    return Card(id="c-1", statement="s", seed_statement="s", concept_tags=list(tags),
                concept_source=source)


# ------------------------------------------------------------------ coverage: a ceiling, not a value

def test_a_self_minted_slug_no_longer_earns_the_maximal_coverage_bonus():
    """The exploit. Every tag outside `explored` used to return 1.0 on the proposer's own say-so."""
    card = _card(["arch/my-brand-new-idea"])

    assert _coverage_signal(card, explored=set(), rename={}) == 0.5


def test_an_independently_classified_card_keeps_the_full_range():
    """The cap is about PROVENANCE, not about coverage being untrustworthy in general — otherwise it
    would punish the classifier pass it is meant to reward."""
    card = _card(["arch/my-brand-new-idea"], _receipt())

    assert _coverage_signal(card, explored=set(), rename={}) == 1.0


def test_the_cap_is_a_ceiling_so_an_honest_low_claim_is_unchanged():
    """The asymmetry that makes this safe: the exploit only runs UPWARD. Replacing an unverified
    value with 0.5 outright would INFLATE the honest case — a card admitting its tags are already
    explored — which is exactly the signal worth keeping."""
    covered = _card(["loss/a", "loss/b"])

    assert _coverage_signal(covered, explored={"loss/a", "loss/b"}, rename={}) == 0.0
    # ...and a partial claim below the ceiling passes through untouched.
    assert _coverage_signal(_card(["loss/a", "loss/b"]), explored={"loss/a"}, rename={}) == 0.5
    assert _coverage_signal(_card(["loss/a", "loss/b", "loss/c", "loss/d"]),
                            explored={"loss/a", "loss/b", "loss/c"}, rename={}) == 0.25


def test_a_card_with_no_usable_tags_still_scores_zero():
    assert _coverage_signal(_card([]), explored=set(), rename={}) == 0.0
    # ...including tags that exist but do not survive the normalization projection.
    assert _coverage_signal(_card(["   "]), explored=set(), rename={}) == 0.0


# ------------------------------------------------------------------ every receipt clause bites

def test_an_incomplete_receipt_does_not_lift_the_cap():
    """`complete=False` is the one under-complete state a VALIDATED receipt can actually be in."""
    card = _card(["arch/new"], _receipt(complete=False))

    assert _independently_classified(card) is False
    assert _coverage_signal(card, explored=set(), rename={}) == 0.5


@pytest.mark.parametrize("field", ["receipt_valid", "membership_present", "complete"])
def test_each_receipt_clause_is_checked_independently_for_a_duck_typed_card(field):
    """The clauses look redundant on a validated `CardConceptSource` — `_coherent_owner` already
    enforces `complete => membership_present and receipt_valid`, and the sibling test below proves
    the invalid combinations are unconstructible. They are NOT redundant on the path this gate
    actually has to survive: `card_score` is a documented public scoring hook, so an external policy
    can hand it a source object no validator ever saw. Checking `complete` alone would then trust a
    hand-built receipt that says complete while admitting it is invalid.
    """

    class DuckSource:
        provenance = "classifier"
        receipt_valid = True
        membership_present = True
        complete = True

    setattr(DuckSource, field, False)

    class DuckCard:
        concept_tags = ["arch/new"]
        concept_source = DuckSource()

    assert _independently_classified(DuckCard()) is False


@pytest.mark.parametrize("field", ["receipt_valid", "membership_present"])
def test_the_model_itself_makes_those_combinations_unconstructible(field):
    """Why the test above has to use a duck type. Stated so a future reader does not "simplify"
    `_independently_classified` down to `complete` on the strength of the model's validator, which
    does not run on the public-hook path."""
    with pytest.raises(ValueError, match="complete concept sources"):
        _receipt(**{field: False})


@pytest.mark.parametrize("provenance,independent", [
    ("classifier", True),
    ("operator-edited", True),
    ("researcher-authored", False),      # the gameable case this exists for
    ("untrusted-source", False),
    ("offline-heuristic", False),        # coarse display-only fallback, not evidence
])
def test_only_a_non_proposer_provenance_counts_as_independent(provenance, independent):
    card = _card(["arch/new"], _receipt(provenance=provenance))

    assert _independently_classified(card) is independent


def test_an_absent_receipt_fails_closed_rather_than_raising():
    """`card_score` is a documented public hook: an external policy may pass a duck-typed card."""

    class Duck:
        concept_tags = ["arch/new"]

    assert _independently_classified(Duck()) is False
    assert _independently_classified(_card(["arch/new"])) is False


# ------------------------------------------------------------------ foresight: rank, not self-report

def test_the_default_foresight_term_is_the_rank_the_ranker_chose():
    assert CardScoring().confidence_weight == 0.0
    assert _foresight_signal(confidence=1.0, priority=0.25, confidence_weight=0.0) == 0.25
    assert _foresight_signal(confidence=0.0, priority=0.25, confidence_weight=0.0) == 0.25


def test_the_escape_hatch_restores_the_historical_blend_exactly():
    """The weight exists rather than a deletion precisely so the old behaviour stays expressible for
    anyone who wants to A/B it. Compared against the literal 0.65/0.35 formula, not against itself."""
    for confidence, priority in ((1.0, 0.0), (0.0, 1.0), (0.3, 0.7), (0.9, 0.2)):
        assert _foresight_signal(confidence, priority, HISTORICAL_CONFIDENCE_WEIGHT) == pytest.approx(
            0.65 * confidence + 0.35 * priority)


def test_a_confident_ranker_no_longer_outranks_a_better_rank_by_default():
    """The behaviour that changed, stated as the comparison it decides. Card A is ranked SECOND but
    claims full confidence; card B is ranked first and claims none."""
    a = _foresight_signal(confidence=1.0, priority=1.0 / 3.0, confidence_weight=0.0)   # rank 2
    b = _foresight_signal(confidence=0.0, priority=1.0, confidence_weight=0.0)         # rank 0

    assert b > a
    # Under the historical weighting the self-report won outright, which is what §21.12 says has no
    # relationship to realized outcome.
    old_a = _foresight_signal(1.0, 1.0 / 3.0, HISTORICAL_CONFIDENCE_WEIGHT)
    old_b = _foresight_signal(0.0, 1.0, HISTORICAL_CONFIDENCE_WEIGHT)
    assert old_a > old_b


def test_the_weight_is_normalized_and_defaults_closed():
    assert normalize_card_scoring(None).confidence_weight == 0.0
    assert normalize_card_scoring({}).confidence_weight == 0.0
    assert normalize_card_scoring({"confidence_weight": 0.65}).confidence_weight == 0.65
    # bounded like every other scorer input — a malformed resume payload cannot widen the term
    assert normalize_card_scoring({"confidence_weight": 5.0}).confidence_weight == 1.0
    assert normalize_card_scoring({"confidence_weight": -1.0}).confidence_weight == 0.0
    assert normalize_card_scoring({"confidence_weight": True}).confidence_weight == 0.0
    assert normalize_card_scoring({"confidence_weight": "0.65"}).confidence_weight == 0.0
    assert normalize_card_scoring(CardScoring(confidence_weight=0.65)).confidence_weight == 0.65


# ------------------------------------------------------------------ the Strategist cannot set it

def test_the_strategist_cannot_propose_a_confidence_weight():
    """A trust decision, not a search stance. The number `confidence_weight` governs is the LLM's own
    self-assessment; letting an LLM Strategist raise it would let the model hand its self-report back
    the majority share of an active selection signal it was measured not to predict.

    Enforced by `validate_card_scoring`'s exact-set match, which rejects the whole map — the same
    fail-closed treatment any unknown field gets, so this needs no extra branch to maintain.
    """
    from looplab.agents.strategist import _CARD_SCORING_FIELDS, validate_card_scoring

    accepted = {"stance": "balanced", "novelty_weight": 0.5, "coverage_weight": 0.5}
    assert validate_card_scoring(accepted) == accepted

    assert validate_card_scoring({**accepted, "confidence_weight": 0.65}) is None
    assert "confidence_weight" not in _CARD_SCORING_FIELDS


def test_a_strategist_treatment_still_normalizes_to_the_closed_default():
    """The two field sets differing must not make the Strategist's own map unusable: the scorer
    supplies `confidence_weight` itself, at the closed default."""
    from looplab.agents.strategist import validate_card_scoring

    treatment = normalize_card_scoring(
        validate_card_scoring({"stance": "explore", "novelty_weight": 0.7, "coverage_weight": 0.2}))

    assert (treatment.stance, treatment.novelty_weight, treatment.coverage_weight) == (
        "explore", 0.7, 0.2)
    assert treatment.confidence_weight == 0.0


def test_the_weight_has_exactly_one_way_in():
    """What makes the reachability claim in `_foresight_signal` and the configuration guide TRUE.

    The Strategist is blocked by `validate_card_scoring`'s exact-set match — but that only closes the
    door if the validator is the ONLY thing that ever assigns `Engine._card_scoring`. A second
    assignment path (a config loader, a resume restore, a control handler) would reopen it silently,
    and the docs would go from accurate to actively misleading with nothing failing.

    The walk comes from `_source_scan` rather than a local `rglob`, per the meta-guard in
    `test_source_scan_helper.py`. Its reason is not tidiness: a hand-rolled walk reading plain
    `utf-8` dies on `runtime/command_eval.py`'s BOM, which is exactly what the first draft of this
    test did.
    """
    import ast

    from tests import _source_scan

    writes = []
    for path, tree in _source_scan.iter_trees():
        for node in ast.walk(tree):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
            for target in targets:
                if (isinstance(target, ast.Attribute) and target.attr == "_card_scoring"
                        and isinstance(target.value, ast.Name) and target.value.id == "self"):
                    writes.append((path, node.lineno))

    assert len(writes) == 1, (
        "_card_scoring is assigned from more than one place: "
        f"{[(p.name, line) for p, line in writes]}")
    path, lineno = writes[0]
    assert path.name == "strategy.py", path.name
    # The single assignment must read the VALIDATED map, not the raw proposal.
    source = path.read_text(encoding="utf-8-sig", errors="replace").split("\n")
    assert "validate_card_scoring(" in "\n".join(source[max(0, lineno - 6):lineno]), (
        "the single assignment no longer comes from validate_card_scoring, so a proposal naming "
        "confidence_weight could reach the scorer")
