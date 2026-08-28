"""A build that was PAID FOR and minted no node, priced from the durable log's own windows.

MEASURED on `runs/e5small-dr-unified-v9`: three of twelve builds ended `card_build_done` with
`skipped: "stale"` and no `node_id` — card-2 31.6M, card-5 8.5M, card-3 1.2M, **41.4M together,
11.8 % of the run**. They never minted a node, so the Card freshness gate never saw them and they
are NOT the 68.9M `token_spend_by_card` reports (only card-3's 1.2M overlaps).

WHY IT IS A SECOND FOLD AND NOT A WIDER RULE. `wholly_discarded` requires the card to OWN a node,
which is right for its own question and blind here in both directions: card-5's only build was
skipped, so it shows no nodes and no flag, and card-2 reads as a healthy 97.6M row while a third of
that spend bought a build the engine threw away.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

from looplab.events.token_spend import token_spend_by_build


def _gen(span_id, parent_id, start, total):
    return {"span_id": span_id, "parent_id": parent_id, "kind": "generation", "start": start,
            "attributes": {"usage": {"total": total}}}


def _build_span(span_id, card):
    return {"span_id": span_id, "parent_id": None, "kind": "operation",
            "attributes": {"card_id": card}}


def _window(card, start, end, skipped=None, node_id=None):
    return {"card": card, "start": start, "end": end, "skipped": skipped, "node_id": node_id}


def test_a_generation_inside_its_own_cards_window_is_charged_to_that_build():
    rows = [_build_span("b1", "card-5"), _gen("g1", "b1", 100.0, 42)]
    out = token_spend_by_build(rows, [_window("card-5", 90.0, 110.0, skipped="stale")])
    assert out["rows"][0]["tokens"] == 42 and out["rows"][0]["calls"] == 1
    assert out["unwindowed"] == 0


def test_a_window_only_takes_its_OWN_cards_generations():
    """Mutation: drop the `w["card"] == card` conjunct. Two builds overlap in time on a two-lane
    run as a matter of course, and a purely temporal window would charge one card's build to the
    other — inventing a loss on a card that ran fine."""
    rows = [_build_span("b1", "card-5"), _gen("g1", "b1", 100.0, 42),
            _build_span("b2", "card-9"), _gen("g2", "b2", 100.0, 7)]
    out = token_spend_by_build(rows, [_window("card-5", 90.0, 110.0, skipped="stale")])
    assert out["rows"][0]["tokens"] == 42
    assert out["unwindowed"] == 7


def test_a_generation_OUTSIDE_every_window_is_reported_not_dropped():
    """`propose`, deep research and enrichment all legitimately live outside a build window.
    Mutation: `continue` without adding to `unwindowed`, and the fold silently reports a run as
    cheaper than the ledger says — the omission the phase fold's `(no phase)` already refuses."""
    rows = [_build_span("b1", "card-5"), _gen("g1", "b1", 100.0, 42),
            _gen("loose", None, 500.0, 9)]
    out = token_spend_by_build(rows, [_window("card-5", 90.0, 110.0)])
    assert out["unwindowed"] == 9 and out["attributed"] == 51


def test_only_a_SKIPPED_window_counts_toward_the_skipped_total():
    """Mutation: sum every window. The line exists to say what minted NOTHING; counting the builds
    that produced an evaluated node would report the whole build path as a loss."""
    rows = [_build_span("b1", "card-5"), _gen("g1", "b1", 100.0, 40),
            _build_span("b2", "card-6"), _gen("g2", "b2", 200.0, 60)]
    out = token_spend_by_build(rows, [_window("card-5", 90.0, 110.0, skipped="stale"),
                                      _window("card-6", 190.0, 210.0, node_id=5)])
    assert out["skipped_tokens"] == 40 and out["skipped_builds"] == 1 and out["builds"] == 2


def test_a_window_with_no_bounds_prices_NOTHING_rather_than_guessing():
    """A `card_build_done` whose `card_build_requested` is missing from the log (a torn prefix, a
    resumed run) has no start. Mutation: default it to 0, and that window swallows every generation
    of its card from the beginning of the run — a fabricated loss, which is worse than an absent
    one."""
    rows = [_build_span("b1", "card-5"), _gen("g1", "b1", 100.0, 42)]
    out = token_spend_by_build(rows, [_window("card-5", None, 110.0, skipped="stale")])
    assert out["rows"] == [] and out["skipped_tokens"] == 0
    assert out["unwindowed"] == 42


def test_a_generation_with_no_START_is_never_placed_in_a_window():
    """Mutation: treat a missing start as 0.0. The same fabrication as above from the other side —
    an untimed span would land in whichever window begins earliest."""
    rows = [_build_span("b1", "card-5"),
            {"span_id": "g1", "parent_id": "b1", "kind": "generation",
             "attributes": {"usage": {"total": 42}}}]
    out = token_spend_by_build(rows, [_window("card-5", 0.0, 1e9, skipped="stale")])
    assert out["skipped_tokens"] == 0 and out["unwindowed"] == 42


def test_overlapping_windows_of_one_card_resolve_to_the_FIRST_deterministically():
    """Two builds of one card cannot normally overlap, but a fold must not depend on that. Mutation:
    charge every matching window, and one generation is counted twice — `attributed` then exceeds
    the ledger and the command prints "spans over-attribute" about a number it invented."""
    rows = [_build_span("b1", "card-5"), _gen("g1", "b1", 100.0, 42)]
    out = token_spend_by_build(rows, [_window("card-5", 90.0, 110.0, skipped="stale"),
                                      _window("card-5", 95.0, 115.0, skipped="stale")])
    assert [w["tokens"] for w in out["rows"]] == [42, 0]
    assert out["skipped_tokens"] == 42


def test_junk_rows_and_junk_windows_never_raise():
    """Mutation: let a non-dict through to `.get`. This fold reports ON a damaged sidecar; dying on
    one is the failure it exists to describe."""
    rows = ["nonsense", None, _build_span("b1", "card-5"), _gen("g1", "b1", 100.0, 5)]
    out = token_spend_by_build(rows, ["junk", None, _window("card-5", 90.0, 110.0, skipped="stale")])
    assert out["skipped_tokens"] == 5 and out["builds"] == 1
