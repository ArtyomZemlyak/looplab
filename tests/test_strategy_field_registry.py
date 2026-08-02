"""The Strategy field set is synchronized across five encodings — guarded here (doc 25 AG-03).

`strategist.py`'s own comment lists the chain: adding one Strategy field means touching the
`Strategy` TypedDict, `_StrategyOut` (the LLM output schema), `_assemble_strategy` (a field-by-field
copy), `validate_strategy` (a paranoid whitelist), and `Engine._apply_strategy`. Five hand-kept
encodings, and until now no registry test — contrary to the codebase's own convention for exactly
this failure class (DEVELOPER_OUTPUT_ATTRS, RESEARCHER_HINT_ATTRS, PROMPT_KEYS, …).

The failure is silent in the direction that matters. A field added to `_StrategyOut` but forgotten
in `_assemble_strategy` is DROPPED: the model proposes it, the schema accepts it, nothing raises,
and the knob simply never moves. A field that reaches `_assemble_strategy` but not
`validate_strategy` is dropped one step later, by the whitelist that exists to be paranoid.

So these tests walk the real encodings rather than restating them, and every direction that can
silently drop a field is asserted separately.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from looplab.agents import strategist

_PKG = Path(__file__).resolve().parents[1] / "looplab"
_SOURCE = (_PKG / "agents" / "strategist.py").read_text(encoding="utf-8-sig")
_TREE = ast.parse(_SOURCE)


def _function(name: str) -> str:
    node = next(n for n in ast.walk(_TREE)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return ast.unparse(node)


def _strategy_keys() -> set[str]:
    """The functional-syntax TypedDict is the source of truth for what a Strategy may carry."""
    call = next(n for n in ast.walk(_TREE)
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "TypedDict")
    return set(re.findall(r"'(\w+)':", ast.unparse(call)))


def _strategy_out_fields() -> set[str]:
    return set(strategist._StrategyOut.model_fields)


# Keys a Strategy carries that the MODEL is deliberately not allowed to propose. Each is here for a
# stated reason, and adding to this list is the explicit way to say "the LLM must not set this".
NOT_LLM_PROPOSABLE = {
    "source",          # provenance the code stamps ("rule"|"llm"|"operator"|"config"), not a knob
    "policy_params",   # free-form dict; the schema exposes the named knobs instead
    "card_scoring",    # atomic {stance, weights} block assembled from validated pieces
    "developer",       # the backend factory key — an operator/config decision, not a model one
    "operators",       # assembled from the flat ablate_every / merge_mode / complexity_cue fields
    "max_parallel",    # legacy alias of eval_parallel, accepted on input but never proposed
    "parallel_build",  # legacy alias of llm_parallel, same
}


def test_the_llm_schema_covers_every_proposable_strategy_field():
    """A knob the model cannot express is a knob the Strategist cannot steer."""
    proposable = _strategy_keys() - NOT_LLM_PROPOSABLE
    missing = sorted(proposable - _strategy_out_fields())
    assert not missing, (
        f"these Strategy fields have no `_StrategyOut` field, so the model can never propose them: "
        f"{missing}. Add them to the schema, or declare them in NOT_LLM_PROPOSABLE with the reason.")


def test_every_schema_field_is_copied_by_assemble():
    """The silent-drop direction: proposed, accepted by the schema, then quietly discarded."""
    assemble = _function("_assemble_strategy")
    missing = sorted(name for name in _strategy_out_fields()
                     if not re.search(rf"\b{re.escape(name)}\b", assemble))
    assert not missing, (
        f"`_assemble_strategy` never mentions {missing} — the model can propose them and nothing "
        "raises, but the knob never moves")


def test_every_strategy_field_passes_the_validator_whitelist():
    """`validate_strategy` is a whitelist, so an unlisted field is dropped one step after assembly."""
    validate = _function("validate_strategy")
    missing = sorted(name for name in _strategy_keys()
                     if not re.search(rf"'{re.escape(name)}'", validate))
    assert not missing, (
        f"`validate_strategy` does not whitelist {missing}; the paranoid filter that exists to keep "
        "a forged strategy out will also drop these legitimate ones")


def test_the_engine_applies_every_strategy_field():
    """A field that survives validation but that `_apply_strategy` ignores changes nothing at all."""
    applied = (_PKG / "engine" / "strategy.py").read_text(encoding="utf-8-sig")
    missing = sorted(name for name in _strategy_keys()
                     if f'"{name}"' not in applied and f"'{name}'" not in applied)
    assert not missing, (
        f"`Engine._apply_strategy`'s module never names {missing} — they validate cleanly and then "
        "do nothing")


def test_the_declared_non_proposable_set_is_real():
    """Guards the exemption list itself: an entry that is no longer a Strategy field is stale, and a
    stale exemption is how a genuinely-missing schema field hides."""
    unknown = sorted(NOT_LLM_PROPOSABLE - _strategy_keys())
    assert not unknown, (
        f"{unknown} are exempted from the LLM schema but are not Strategy fields at all — drop them")
    overlap = sorted(NOT_LLM_PROPOSABLE & _strategy_out_fields())
    assert not overlap, (
        f"{overlap} are declared non-proposable yet ARE in `_StrategyOut`; one of the two is wrong")


def test_the_scan_can_actually_see_the_encodings():
    """A source scan that matches nothing is indistinguishable from one whose parse is broken."""
    keys = _strategy_keys()
    assert len(keys) >= 15, f"only {len(keys)} Strategy keys parsed — the TypedDict walk regressed"
    assert "policy" in keys and "novelty_stance" in keys
    assert len(_strategy_out_fields()) >= 10
    assert "novelty_stance" in _function("_assemble_strategy")


@pytest.mark.parametrize("first,second", [("NOVELTY_STANCES", "CARD_SCORING_STANCES")])
def test_the_two_stance_tuples_are_still_deliberately_identical(first, second):
    """AG-03 also flags these as two identical tuples. They are NOT collapsed here — a card-scoring
    stance and a novelty stance are separate dials that happen to share a vocabulary today, and
    merging them would couple two knobs the Strategist sets independently. This pins the equality so
    that if they ever diverge it is a decision, and pins that both still exist so a future collapse
    is a deliberate edit rather than a silent one."""
    assert getattr(strategist, first) == getattr(strategist, second), (
        f"{first} and {second} diverged. That may be correct — a card-scoring stance need not track "
        "a novelty stance — but it is a behaviour change for anything reading one and applying the "
        "other, so make it explicit here.")
