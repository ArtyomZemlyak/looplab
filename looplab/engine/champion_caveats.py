"""WHAT KIND OF NUMBER IS A RUN'S `best_metric` — the one fact the run-summary row could not carry.

THE LEAK, and it is a portfolio-wide one because the row it rides on is the portfolio.
`serve/run_projections.py::run_summaries` publishes `best_metric` (and `best_confirmed`) for every
run directory on the box, and it publishes NOTHING ELSE about that number: no `violations`, no
`metric_provenance`, no node id. `RunState.best()` reads `best_node_id`, which `events/replay.py::
_select_best` derives from `promotion_eligible_nodes` — so the row's number is whatever the run's own
selector was willing to crown UNDER THE RUNGS THAT RUN WAS CONFIGURED WITH. Two of those rungs let a
number the run itself recorded a caveat about become the champion:

  * `metric_salvage="select"` over OPERATOR-produced output mints NO `metric_salvaged` violation row
    (`metric_salvage.py::SalvagedMetric.violation_rows`), so a metric RECOVERED from a failed eval is
    `feasible`, competes, and can be `best_node_id`. The only record is the folded
    `metric_provenance`, which the summary row does not carry.
  * `trust_gate="audit"` — THE SHIPPED DEFAULT — makes `flagged_node_ids` empty, so a node carrying a
    HIGH-PRECISION reward-hack/leakage signal (`hard_flagged_ids`, which is mode-INDEPENDENT) is
    excluded from nothing and can be `best_node_id`. The only record is `st.reward_hacks`, which the
    summary row does not carry either.

In both cases the run made a decision and RECORDED it; what is missing is that the decision travels
with the number. Thirteen browser surfaces read that row (`RunList`, `runIndex.js::sortRuns`,
`portfolioModel.js`, `RunCompare`, `MapView`, `conceptForest.js`, `crossRunRank.js`/`CrossRunPanel`,
ConceptFrame refs, …) and not one of them can derive the fact, because it is not in the payload. So
this is one server-side field and not thirteen client fixes.

--------------------------------------------------------------------------------------------------
WHY THIS IS NOT `engine/memory.py::unreliable_metric_ids`, WHICH IS THE OBVIOUS ANSWER AND IS EMPTY
HERE BY CONSTRUCTION.

That function joins the same two families — `metric_salvage.metric_unmeasured` and
`replay.flagged_node_ids` — and it is the right join for the question it answers ("may this number
ground a CROSS-RUN claim"). It is the wrong one here, and not by a margin: **its intersection with
`{best_node_id}` is empty under every rung, as a theorem rather than as a corpus fact.**

  * `metric_unmeasured` is true only of a node carrying a `metric_salvaged` VIOLATION row. The fold's
    rule is `feasible = not violations` (`replay.py::_on_node_evaluated`) and
    `core/fitness.py::SearchFitness.eligible` requires `feasible`, so such a node is never selected.
  * `flagged_node_ids` is non-empty only under `gate`/`block`, and `_select_best` passes exactly that
    set into `SearchFitness.eligible` as `flagged`, so such a node is never selected either.

A field stating that join would therefore be a constant `false` on the one row it decorates.
`tests/test_champion_metric_caveats.py` drives that theorem rather than trusting this paragraph.

What the caveats below state is the COMPLEMENTARY HALF of each of those two members — the half that
SURVIVES selection, which is exactly the half a selection-blind exclusion predicate cannot see:

    unreliable_metric_ids            champion_metric_caveats
    ------------------------------   ------------------------------------------------
    salvaged AND excluded            salvaged AND admitted     (`select`, no row minted)
    flagged AND enforced             flagged AND not enforced  (`audit`, nothing excluded)

So it IS the same join, taken on the other side of the selection boundary — which is why both halves
are spelled as CALLS to that join's own two primitives (`metric_unmeasured`, `hard_flagged_ids` /
`flagged_node_ids`) and not as a fresh reading of `violations` or of `reward_hacks`. A projection
that re-derived either rule would drift from the rung that decides it the first time the rung moved,
and the rung here is `SalvagedMetric.violation_rows` — a function whose whole job is to decide
whether a row exists at all.

--------------------------------------------------------------------------------------------------
WHAT THIS DELIBERATELY DOES NOT SAY, both derived from the record rather than from taste.

  * A REPAIRED DECLARATION (`metric_provenance.declaration_repaired`, the F1e re-check) is a MEASURED
    metric: the pipeline produced the artifact and only the sentence describing where it lived was
    wrong. `ui/src/trustSemantics.js::objectiveMetricSource` answers `measured` for it and carries the
    one thing it uniquely proves in a tooltip. Marking it here would put the portfolio row in
    disagreement with the node tab about the same node, which is the defect this vocabulary exists to
    close, one direction over.
  * AN UNBOUND METRIC SUBJECT under the `audit`/`off` rungs. Those rungs RECORD without enforcing and
    mint no violation row, so such a node is feasible and routinely IS the champion — measured
    2026-08-15, the live `runs/rubertlite-dr-unified-v8`'s champion (node 1, 0.738425) carries
    `metric_provenance = {"subject_bound": false, "unbound_reason": "not_declared", "subjects": []}`.
    That is the RULE and not a finding: 82 of 83 preserved corpus metrics are in that state because
    the task declares no `eval.metric.subject` at all, so a caveat there would fire on every run on
    the box and mean nothing. `trustSemantics.js` states the same refusal for the same reason, and
    that run is the negative control the guard test drives. Under `require` the rung DOES mint a row
    — and then the node is infeasible and cannot be champion, which is the first theorem again.
  * A CONSTRAINT violation. `latency_ms > 500` is a fact about a bound and the metric beside it was
    measured; such a node is also infeasible and cannot be champion.

MEASURED OVER THE PRESERVED CORPUS, 2026-08-15 (46 run directories under `runs/`, folded with the
same `fold` `/api/runs` serves): 37 runs carry a `best_metric`. **The two METRIC-PROVENANCE members
caveat ZERO of them**, and that is the honest number for those two — they close a reachable hole,
they do not clean up a dirty corpus. The corpus holds exactly ONE salvaged node
(`rubertlite-dr-unified-v6` node 3, 0.728113, condition `artifact_contract`) and its producer is
`agent_stage`, which `violation_rows` keeps excluded under EVERY rung but `off` — so re-running the
selection over all 46 logs with `metric_salvage="select"` moves ZERO champions, even though that
node's number beats its run's champion (0.727991). Two runs carry any `reward_hack` row and one
(`task-g7`, node 1) carries a HARD signal; neither run's champion is the flagged node.

**`params_overridden` IS NOT EMPTY, and it lands on the live run's champion.** RE-DERIVED
2026-08-15 23:41 UTC over the same 46 logs by calling this module's own predicate on every folded
node: **FOUR of the 218 folded nodes** have code contradicting their declared parameters, all four on
`rubertlite-dr-unified-v8` — node 3 (`batch_size` declared 8192 / code 4096, `gradient_accumulation_
steps` 2 / 4), node 8 (`batch_size` 8192 / 4096 AND `n_epochs` 15 / 8), and nodes 10 and 11 (the same
batch/accum pair, at 2048 / 8 and 4096 / 4). Node 3 is the run's champion at 0.762048, +0.0236 clear
of the field, so exactly ONE of the 46 RUNS is caveated — which is the number this paragraph is about,
the member being per-CHAMPION.

**The earlier reading of "exactly ONE of 297 nodes" was wrong and is retracted here, and how it was
wrong is worth more than the number.** 297 was the `node_created` COUNT (300 by 23:41) and not the
folded population; and it missed nodes 8 and 9, which `adapters/repo_developer.py::_time_budget_note`
had ALREADY named from the `node_repaired` side in the same change — two derivations of one
population, published disagreeing, in one merge. **QUOTE THE INSTANT WITH THE NUMBER here, because
this population MOVES BOTH WAYS while v8 runs**: node 9 carried an `n_epochs` 10 / 6 override at
23:34 and does not at 23:41, its second repair having deleted that very assignment. A caveat derived
live from folded state is not a corpus statistic and must not be written as one.

So the member is a fence around a state that is not merely reachable but OCCUPIED, repeatedly, by the
run that produced the best result on this box. (The denominator is small and should be quoted too: 28
of those 218 nodes declare a dotted numeric parameter at all, all on the four
`rubertlite-dr-unified-v{2,6,7,8}` runs; the toy and benchmark spaces declare bare names, which
`PARAM_OVERRIDE_MIN_PARTS` excludes by design.) TWO shapes it deliberately does not see, both
measured: a declaration contradicted by the node's YAML CONFIG rather than by its code (five nodes,
re-derived under this rung's OWN contiguous-suffix rule against every YAML in each working set —
`rubertlite-dr-unified-v2` node 3 and v7 nodes 1 and 2 and v8 nodes 0 and 9, ALL FIVE already
diverging at node_created, so this route is a Developer-authored defect and not a repair-authored one
— left open because the rung would have to know which config file the pipeline reads and by what key
path), and a BARE-name declaration overridden in a conditional branch
(`rubertlite-dense-retrieval` node 36, `distill_alpha` declared 0.5, `train.py:117` assigning 0.0
inside a missing-teacher fallback) — the one corpus instance of exactly the two shapes
`PARAM_OVERRIDE_MIN_PARTS` and the "not a claim about what ran" wording exist to refuse.

AN EMPTY LIST IS NOT A CERTIFICATE, which is `ui/src/trustSemantics.js`'s first rule and applies
verbatim here: it says the run recorded no such caveat, never that a detector ran. `reward_hack_detect`
is OFF by default, so the trust member is silent on almost every run on this box — and the third
member is silent on every space that declares its parameters by bare name.
"""
from __future__ import annotations

# WHAT A RUN'S CHAMPION NUMBER CAN BE CAVEATED FOR, as a closed vocabulary — one slug per surviving
# half of `unreliable_metric_ids`' two families, plus one that is NOT of that join at all and says so
# in its own entry: the first two qualify HOW the number was measured, the third qualifies WHAT it is
# a number for. A fourth belongs here only if it is likewise derivable from the fold and from bytes
# the engine authenticated, and only if it survives selection — anything the selector already refuses
# is `unreliable_metric_ids`' side of the boundary and cannot be true of a champion.
#
#   salvaged      — the number was RECOVERED from a failed eval by the operator's own declared reader
#                   and `metric_salvage="select"` admitted it into selection. The word is the SAME one
#                   `ui/src/trustSemantics.js::OBJECTIVE_SALVAGED` prints for the node itself, so the
#                   portfolio row and the node's Metrics tab say one word about one number.
#   trust_flagged — the node carries a high-precision reward-hack/leakage signal that this run's own
#                   `trust_gate` did not enforce. NOT the same claim as "this run has flags": it is
#                   the claim that the flagged node is the one whose number this row publishes.
#   params_overridden — the champion's own committed `.py` code assigns a DIFFERENT number to a
#                   parameter its `Idea` declares. Unlike the two above this is not about how the
#                   number was measured, it is about what the number is a number FOR: `idea.params`
#                   are the coordinates every reader of `core/numeric.py::numeric_params` places
#                   this result at (the surrogate, the panel, the proxy, the archive's niches, the
#                   novelty distance, `search/operators.py::merge_idea`'s arithmetic, the exported
#                   champion notebook, and the "Best so far: node N params={…}" line the Researcher
#                   is shown), so a champion whose code contradicts them is published at coordinates
#                   it never occupied. Derived from the DECLARATION and the committed BYTES, never
#                   from any text an agent wrote — `engine/repair_verify.py::declared_param_
#                   overrides` owns the rule and its bounds; this is a call to it, not a second copy.
CHAMPION_CAVEAT_SALVAGED = "salvaged"
CHAMPION_CAVEAT_TRUST_FLAGGED = "trust_flagged"
CHAMPION_CAVEAT_PARAMS_OVERRIDDEN = "params_overridden"
CHAMPION_CAVEATS = (CHAMPION_CAVEAT_SALVAGED, CHAMPION_CAVEAT_TRUST_FLAGGED,
                    CHAMPION_CAVEAT_PARAMS_OVERRIDDEN)


def champion_metric_caveats(state) -> list[str]:
    """The `CHAMPION_CAVEATS` slugs that apply to `state.best()`'s metric — `[]` for most runs.

    Ordered by the vocabulary (not by discovery) so the same state always produces the same list; a
    reader may render it as a set, but a projection that changed order between two polls would make
    an unchanged run look changed to every client diffing it.

    Total over junk in the same strict sense as `metric_unmeasured` one module over: a state with no
    champion, a hand-assembled `RunState`, a node whose `metric_provenance` is a string — none of
    them raises, and all of them answer `[]`. Deliberately NOT wrapped in a containment `except`:
    both halves are total by construction (`metric_unmeasured` reads one list; `hard_flagged_ids` and
    `flagged_node_ids` are the helpers the fold itself calls on every replay), and swallowing an
    error would answer "nothing to say about this number", which is the one wrong answer.
    """
    # Function-local, mirroring `memory.unreliable_metric_ids`' own imports of these same two: this
    # module is imported by a `serve` PROJECTION, and neither predicate is needed to build a run row
    # that has no champion. It is a cost decision, not a cycle one — `engine` may import `events`.
    from looplab.engine.metric_salvage import metric_unmeasured
    from looplab.engine.repair_verify import declared_param_overrides
    from looplab.events.replay import flagged_node_ids, hard_flagged_ids

    best = state.best() if hasattr(state, "best") else None
    if best is None:
        return []
    out: list[str] = []

    # SALVAGE. `metric_unmeasured` is the EXCLUDED half and cannot be true of a champion (see the
    # module docstring's theorem) — it is asked anyway, and asked FIRST, because it is the rung that
    # decides: this branch means precisely "the record says salvaged AND the rung minted no row", and
    # spelling that as `not feasible` or as a scan of `violations` would be a second copy of a rule
    # `SalvagedMetric.violation_rows` owns. `metric_provenance` is the salvage's own account and the
    # ONLY record `select` leaves behind; `.get` on a non-dict is why it is normalized here.
    record = getattr(best, "metric_provenance", None)
    provenance = record if isinstance(record, dict) else {}
    if provenance.get("salvaged") and not metric_unmeasured(best):
        out.append(CHAMPION_CAVEAT_SALVAGED)

    # TRUST. The subtraction IS the statement: `hard_flagged_ids` is mode-independent ("a
    # high-precision signal was recorded about this node") and `flagged_node_ids` is what the run's
    # own `trust_gate` ENFORCED, so the difference is exactly "recorded and not enforced". Under
    # `gate`/`block` the difference is empty and the node was never selectable in the first place, so
    # this branch cannot second-guess a rung the operator did turn on.
    if best.id in hard_flagged_ids(state) and best.id not in flagged_node_ids(state):
        out.append(CHAMPION_CAVEAT_TRUST_FLAGGED)

    # DECLARED COORDINATES. Asked of the FOLD and of nothing else — `Idea.params` comes from
    # `node_created` and `Node.files`/`Node.code` are the bytes the engine committed, so this is
    # recomputed identically by any replay and needs no event of its own. It deliberately does NOT
    # ask whether a REPAIR introduced it: the operator's question here is "may I reuse this
    # configuration", and a declaration the node's own code contradicts fails that question whoever
    # wrote the line. (`node_repaired.param_overrides` carries the attribution, for the reader who
    # is asking about the attempt rather than about the number.)
    if declared_param_overrides(getattr(getattr(best, "idea", None), "params", None) or {},
                                getattr(best, "files", None) or {},
                                code=getattr(best, "code", "") or ""):
        out.append(CHAMPION_CAVEAT_PARAMS_OVERRIDDEN)
    return out
