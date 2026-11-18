# One population for Card plans

RESULT 2026-11-11 — isolated implementation and CPU acceptance; no deployment claim.

Card admission uses `node_counts_toward_card_budget`, which excludes tombstones, infeasible or
trust-gated work and proven never-executed speculative discards. Its policy ceiling already uses
this population. The plan formerly combined that ceiling with all physical node IDs/counts,
including hidden operator injections. A hidden operator node could therefore be subtracted again,
and raw IDs could trigger the reserve early or make an earlier merge look like a reserve merge.

New Card plan rows declare `node_count_basis: card_budget`. The plan's `at_node`, operator-injected
count, reserve gates, Strategist switch, merge/simplification membership and plateau window all use
the same population. Node IDs remain immutable identities; window membership uses eligible rank.
Legacy non-Card rows preserve their old shape and count. A Card engine resuming an old count basis
writes an explicit `population_changed` re-cut, retaining spent champions; it does not rewrite old
plan history. A re-cut of an old stall coordinate system ends that old episode rather than silently
interpreting its raw coordinates as eligible positions.

The public projection adds an observed `current_node_count` to Card plans without changing the
event journal. UI budget text reads it rather than guessing from the largest physical node ID;
missing or unsupported counts make no claim of reserve entry or exhausted budget.

`tests/test_card_plan_population.py` covers real folded hidden/manual/infeasible nodes, repeated
plan checks, replay, all gate readers, sparse-ID merge/simplification/plateau membership, explicit
migration, spent champions and public count without writes. Existing endgame, inject re-cut,
plateau, event-contract, Card budget and engine suites remain required. The UI test covers visible
budget decisions and malformed observed counts.
