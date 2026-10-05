"""Transport fixtures with explicit with/without observations, not run-median rank.

Each technique's test comparison has a fixed control score 0.5, independent of
the other techniques' scores. Algorithm behaviour is covered by test_concept_effects.
"""
from looplab.engine.concept_capsules import build_concept_capsule
from looplab.search.concept_effects import empty_effect


def capsule_with_contrasts(**kwargs):
    effects = {}
    for cid, value in (kwargs.get("concept_outcomes") or {}).items():
        if type(value) not in (int, float):
            continue
        delta = (value - 0.5) * (-1 if kwargs.get("direction") == "min" else 1)
        e = empty_effect("observational_not_causal")
        e.update(status="matched", estimate=delta, mean=delta, low=delta, high=delta,
                 n_pairs=1, n_contexts=1, n_with=1, n_without=1,
                 positive=int(delta > 0), negative=int(delta < 0), neutral=int(delta == 0),
                 pairs=[{"with_node": 1, "without_node": 0, "with_attempt": 0,
                         "without_attempt": 0, "delta": delta, "kind": "parent", "phase": "search"}])
        effects[cid] = e
    return build_concept_capsule(**kwargs, concept_effects=effects)
