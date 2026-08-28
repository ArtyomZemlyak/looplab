"""What each EXPERIMENT's build cost, and whether that experiment was ever evaluated.

MEASURED on `runs/e5small-dr-unified-v9` and the reason this shipped: cards 3 and 4 cost 68.9M
tokens — 21.0 % of the run at 17.6 h — and every node either card ever owned was thrown away by the
Card freshness gate before dispatch. Those four nodes are TWO cards each built TWICE, and the
rebuilds cost MORE than the originals (40.9M of the 68.9M). Nothing could report it: the per-phase
fold stops at the phase and the durable ledger carries no phase, no role and no node.

The identity was already in the record and only the roll-up was missing — a generation does not
carry `card_id`, the `card_build` span above it does, and 4,478 of that run's 4,511 build
generations resolve by walking the parent chain (97 % of all generation tokens).

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

from looplab.events.token_spend import (CARD_UNATTRIBUTED, _CARD_ANCESTRY_HOPS,
                                        token_spend_by_card)


def _span(span_id, parent_id=None, kind="operation", card=None, total=None, name=None):
    attributes = {}
    if card is not None:
        attributes["card_id"] = card
    if total is not None:
        attributes["usage"] = {"total": total}
    return {"span_id": span_id, "parent_id": parent_id, "kind": kind,
            "name": name, "attributes": attributes}


def _build(card, base, gen_totals, hops=2):
    """One card_build span with `hops` intermediate spans and a generation under each total.

    Mirrors the real ancestry `generation <- stages <- card_build`, which is why the walk exists at
    all rather than reading `card_id` off the generation.
    """
    rows = [_span(f"{base}-build", None, "operation", card=card, name="card_build")]
    parent = f"{base}-build"
    for h in range(hops):
        rows.append(_span(f"{base}-mid{h}", parent, "operation", name="stages"))
        parent = f"{base}-mid{h}"
    for i, total in enumerate(gen_totals):
        rows.append(_span(f"{base}-gen{i}", parent, "generation", total=total))
    return rows


def test_a_generation_is_charged_to_the_card_its_ANCESTRY_names():
    """Mutation: resolve `card_id` off the generation's own attributes only, and every build
    generation falls into `(no card)` — which is the state this whole fold exists to leave."""
    out = token_spend_by_card(_build("card-1", "a", [100, 50]))
    assert [r["card"] for r in out["rows"]] == ["card-1"]
    assert out["rows"][0]["tokens"] == 150 and out["rows"][0]["calls"] == 2


def test_a_generation_no_card_owns_is_BUCKETED_and_never_dropped():
    """Every `propose` generation is this row by construction — a proposal is made before the card
    it may become exists. Mutation: `continue` instead of bucketing, and the fold reports a run as
    cheaper than the ledger says it was, blaming the difference on the ledger."""
    rows = _build("card-1", "a", [100]) + [_span("loose", None, "generation", total=7)]
    out = token_spend_by_card(rows)
    assert {r["card"]: r["tokens"] for r in out["rows"]} == {"card-1": 100, CARD_UNATTRIBUTED: 7}
    assert out["attributed"] == 107 and out["calls"] == 2


def test_a_card_is_WHOLLY_DISCARDED_only_when_it_owns_nodes_and_all_of_them_were_discarded():
    """The load-bearing rule, and both halves have a failing input.

    Mutation A (drop the `bool(nodes)` conjunct): a card with NO nodes — a build still in flight, or
    one refused before minting — is reported as a loss, i.e. the engine's normal forward motion
    reads as waste. Mutation B (`set(discarded) & set(nodes)`): a card with one discarded node and
    one that RAN is reported as a loss, when the idea was evaluated and the discarded sibling is the
    prefetch machinery working.
    """
    rows = _build("all-dead", "a", [100]) + _build("mixed", "b", [100]) + _build("inflight", "c", [100])
    lineage = {
        "all-dead": {"nodes": [3, 6], "discarded": [3, 6]},
        "mixed": {"nodes": [4, 7], "discarded": [7]},
        "inflight": {"nodes": [], "discarded": []},
    }
    flags = {r["card"]: r["wholly_discarded"] for r in token_spend_by_card(rows, card_nodes=lineage)["rows"]}
    assert flags == {"all-dead": True, "mixed": False, "inflight": False}


def test_the_nodes_a_card_owns_ride_with_its_row():
    """Mutation: return the flag alone. `card-3 DISCARDED` names no experiment an operator can go
    read; `3,6 DISCARDED` is the pair of nodes whose logs and workdirs are on disk."""
    lineage = {"card-3": {"nodes": [3, 6], "discarded": [3, 6]}}
    row = token_spend_by_card(_build("card-3", "a", [100]), card_nodes=lineage)["rows"][0]
    assert row["nodes"] == [3, 6] and row["discarded"] == [3, 6]


def test_rows_sort_by_tokens_DESC_then_card_ASC():
    """The per-phase fold's rule, restated so the two sections of one command cannot disagree.
    Mutation: sort by card alone, and the run's largest spend stops being the first line read."""
    rows = _build("b-card", "a", [50]) + _build("a-card", "b", [50]) + _build("big", "c", [900])
    assert [r["card"] for r in token_spend_by_card(rows)["rows"]] == ["big", "a-card", "b-card"]


def test_a_parent_chain_that_CYCLES_terminates():
    """A torn sidecar can present a chain that loops, and this fold only REPORTS on such a file — it
    must not hang on one. Mutation: drop the hop bound and this test never returns.

    (A test that hangs is a bad failure mode, so the bound is also asserted below where it can fail
    loudly instead.)
    """
    rows = [_span("x", "y", "operation"), _span("y", "x", "operation"),
            _span("g", "x", "generation", total=5)]
    out = token_spend_by_card(rows)
    assert [r["card"] for r in out["rows"]] == [CARD_UNATTRIBUTED]


def test_the_ancestry_walk_is_BOUNDED_and_the_bound_is_the_stated_one():
    """Real builds sit 2-3 hops under their card, so the bound is slack for TERMINATION and never a
    limit a live run reaches — which is what makes it safe to assert on exactly.

    THE CHAIN LENGTH IS A LITERAL AND THAT IS THE POINT. Written as `_CARD_ANCESTRY_HOPS + 5` this
    test scaled with the constant and a mutant that raised the bound to 600 SURVIVED it — the
    fixture moved with the thing it was meant to pin. Both assertions here are hard numbers, so
    raising the bound has to be argued for rather than absorbed.
    """
    assert _CARD_ANCESTRY_HOPS == 60, (
        "the termination bound is load-bearing: raise it only with a reason, and update the reason "
        "beside the constant, not just the number")
    deep = _build("far", "a", [10], hops=65)
    assert [r["card"] for r in token_spend_by_card(deep)["rows"]] == [CARD_UNATTRIBUTED]
    near = _build("near", "b", [10], hops=2)
    assert [r["card"] for r in token_spend_by_card(near)["rows"]] == ["near"]


def test_a_provider_stated_ZERO_is_not_re_derived_here_either():
    """The card fold reads usage through the same `_tokens_of` as the phase fold. Mutation: sum the
    parts unconditionally, and a fully cache-served turn inflates `attributed`, drives `residual`
    negative and makes the command print "spans over-attribute" about a number it invented."""
    rows = [_span("b", None, "operation", card="c1"),
            {"span_id": "g", "parent_id": "b", "kind": "generation",
             "attributes": {"usage": {"total": 0, "prompt": 12000, "completion": 0}}}]
    assert token_spend_by_card(rows)["attributed"] == 0


def test_junk_rows_are_counted_and_never_raise():
    """Mutation: let a non-dict row through to `.get` and the command dies on a file it is reporting
    the damage of. `damaged` and `torn_attributes` stay SEPARATE populations, as in the phase fold:
    one counts rows that were not spans, the other a real billed call whose attribution was lost."""
    rows = ["nonsense", None, 42] + _build("card-1", "a", [10])
    rows.append({"span_id": "torn", "parent_id": "a-build", "kind": "generation", "attributes": None})
    out = token_spend_by_card(rows)
    assert out["damaged"] == 3 and out["torn_attributes"] == 1
    assert out["calls"] == 2 and out["attributed"] == 10


def test_the_residual_is_signed_and_absent_means_absent():
    """Mutation: clamp at zero, or default the ledger to 0. Two spans opened against one retried
    billed row is a real state; "nothing to compare against" is not "zero difference"."""
    rows = _build("card-1", "a", [100])
    assert token_spend_by_card(rows, ledger_total=60)["residual"] == -40
    assert token_spend_by_card(rows)["residual"] is None
