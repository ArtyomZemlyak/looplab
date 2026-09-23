"""The fold's SELECTION family: the trust gate, the champion selector, and the evidence they read.

Split out of `events/replay.py` (review 2026-09-22, EVT-12). This is the part of the fold that DECIDES
— which node is the run's best, and which nodes may not be — and it changes for selection's reasons
(the >1-SE and CI-tie rules, the confirm certificate, holdout promotion, the trust tiers), beside a
lifecycle fold that changes for others. What lives here:

* the trust classifiers every reader shares — `is_hard_signal`, `hard_flagged_ids`,
  `flagged_node_ids` — and the eligible population `promotion_eligible_nodes`;
* THE SELECTOR, `select_best_node`, and the post-pass pair `replay.py::_finalize_fold` calls in order,
  `_apply_trust_gate` then `_select_best`; `verifier_tie_groups`, the one tie-set that can move it;
* the handlers whose rows those read — `trust_gate_changed`, `reward_hack_suspected`, `node_verified`,
  `verifier_group_scored`, and `best_confirmed` with the generation-map check that binds a
  confirmation certificate to the candidate set it was computed over.

Moved VERBATIM, comments included. Every public name here is still imported from `replay` by its
readers (`engine/champion_caveats.py`, `engine/memory.py`, `engine/holdout.py`, `events/digest.py`,
`serve/llm_context.py`, …), and `replay.py` re-exports each one; it merges `HANDLERS` into its
dispatch table.
"""
from __future__ import annotations

from looplab.core.fitness import (VERIFIER_SELECTION_CONTRACT, SearchFitness, is_usable_metric,
                                  verifier_evidence_digest)
from looplab.core.models import (Event, Node, NodeStatus, RunState,
                                 coerce_node_id as _coerce_node_id)
from looplab.events.replay_ctx import (_MISSING, _FoldCtx, _event_generation, _generation_matches,
                                       _node_for_event)
from looplab.events.types import (EV_BEST_CONFIRMED, EV_NODE_VERIFIED, EV_REWARD_HACK_SUSPECTED,
                                  EV_TRUST_GATE_CHANGED, EV_VERIFIER_GROUP_SCORED)


def flagged_node_ids(st: RunState) -> set:
    """T2: node ids excluded from best/holdout selection under trust_gate gate/block — those with a
    HIGH-PRECISION cheating/leakage signal (see `is_hard_signal`). One `critic:` signal —
    `critic:hardcoded_metric` — is HARD and gates; every OTHER `critic:` issue and `perfect_metric`
    stay advisory in every mode (perfect_metric flags the EXACT theoretical optimum — metric==0.0 on
    min / ==1.0 on max — which a legitimately-perfect score hits, so gating on it could exclude honest
    winners). Empty under `audit`. Shared by the fold and the engine's holdout-topk so both apply the
    SAME exclusion."""
    if st.trust_gate not in ("gate", "block"):
        return set()
    return hard_flagged_ids(st)


def promotion_eligible_nodes(st: RunState, *, flagged=None) -> list[Node]:
    """Nodes allowed to publish selection-affecting or promoted cross-run measurements."""
    excluded = flagged_node_ids(st) if flagged is None else set(flagged)
    return [node for node in st.evaluated_nodes()
            if SearchFitness.eligible(node, excluded, st.aborted_nodes)]


def verifier_tie_groups(st: RunState, *, holdout_select: bool | None = None,
                        ci_tie: bool | None = None) -> list[list[Node]]:
    """Return the one complete tie-set that can affect the selector's final answer.

    Holdout promotion runs last.  Once it has a non-empty eligible pool, no mean/CI decision can reach the
    final champion, so surfacing both groups wastes calls and can leave incomparable overlapping treatments.
    Without a holdout pool, mirror the mean selector's confirmed-pool and CI/exact tie semantics.
    """
    holdout_select = st.holdout_select if holdout_select is None else bool(holdout_select)
    ci_tie = st.verifier_ci_tie if ci_tie is None else bool(ci_tie)
    eligible = promotion_eligible_nodes(st)
    confirmed = [n for n in eligible if n.confirmed_mean is not None]
    pool = confirmed if confirmed else eligible
    def _champion_tie(nodes, metric_of):
        candidates = [n for n in nodes if metric_of(n) is not None]
        if not candidates:
            return []
        chooser = min if st.direction == "min" else max
        leader = chooser(candidates, key=lambda n: (metric_of(n), n.id))
        return [n for n in candidates if metric_of(n) == metric_of(leader)]

    holdout_pool = [n for n in eligible if is_usable_metric(n.holdout_metric)]
    if holdout_select and holdout_pool:
        tied = _champion_tie(holdout_pool, lambda n: n.holdout_metric)
    elif ci_tie:
        tied = SearchFitness(st.direction, verifier_tiebreak=True, ci_tie=True).ci_tie_set(pool)
    else:
        tied = _champion_tie(pool, lambda n: n.robust_metric)
    return [sorted(tied, key=lambda n: n.id)] if (
        len(tied) >= 2 and any(node.verifier_score is None for node in tied)) else []


def is_hard_signal(sig: str) -> bool:
    """Is this reward-hack/leakage signal HIGH-PRECISION (gating + agent-facing), vs advisory noise?

    The single classifier shared by `hard_flagged_ids` (gate/block selection exclusion) AND
    `digest.trust_reflection._sigs` (which signals to NAME in the agent hint) — kept here so the two
    can't drift: before, `_sigs` stripped EVERY `critic:` signal while `hard_flagged_ids` promoted
    `critic:hardcoded_metric`, so a node hard-flagged ONLY for that rendered as "node N ()" (a
    contentless warning). `critic:hardcoded_metric` is HIGH-PRECISION (the critic requires a LITERAL
    metric value with no computed assignment anywhere), so it gates — closing the "hardcode a
    near-optimal metric and win under every built-in gate" bypass on self-report tasks. Other
    `critic:` issues and `perfect_metric` (which a legitimately-perfect score hits) stay advisory."""
    sig = str(sig)
    if sig == "critic:hardcoded_metric":
        return True
    # `protected_audit_unavailable` (the whole workdir-tamper audit threw) is fail-closed evidence
    # that the node is NOT verified-clean, but it is not itself proof of tampering — a transient FS
    # error should SURFACE to the operator/agent, not gate-exclude an honest node. So it stays
    # advisory alongside critic:*/perfect_metric. `protected_missing`/`protected_unreadable` (a
    # protected file we placed is gone/corrupt) ARE real tamper evidence and remain HARD (P1-6).
    # `suspicious_output` is a broad SHAPE heuristic (the `looplab harden` constant-prediction rule,
    # pattern `[x]*NNN`) that also matches ordinary buffer pre-allocation (`weights = [0]*1000`); a
    # constant predictor already loses on ground truth, so hard-gating it only risks silently excluding
    # an HONEST winner. Advisory (surface, never gate), exactly like perfect_metric.
    # An unknown FUTURE signal name stays hard on purpose (fail closed toward catching cheating).
    # A BLANK one is different: it is not a signal at all, only the `s.get("signal", "")` default for
    # an entry that never carried the key. Counting it as high-precision cheating evidence let a
    # single malformed/hand-edited record gate-exclude an honest winner under "gate"/"block" — and
    # the digest then rendered it as `node 1 ()`, the contentless warning this function's own
    # contract says can never happen. Reject the malformed shape; keep every named signal hard.
    sig = sig.strip() if isinstance(sig, str) else ""
    if not sig:
        return False
    return not sig.startswith(("critic:", "perfect_metric", "protected_audit_unavailable",
                               "suspicious_output"))


def hard_flagged_ids(st: RunState) -> set:
    """Node ids carrying a HIGH-PRECISION cheating/leakage signal, including the narrow
    ``critic:hardcoded_metric`` exception but excluding other ``critic:*`` and ``perfect_metric``
    heuristics, INDEPENDENT of `trust_gate` mode. `flagged_node_ids` uses it for gate/block selection
    exclusion; the agent-facing trust-reflection hint (signal-delivery §1) uses it to warn the
    Researcher about a flagged lineage even under `audit`, where nothing is gate-excluded."""
    def _has_current_hard_signal(rh: dict) -> bool:
        nid = _coerce_node_id(rh)
        n = st.nodes.get(nid) if nid is not None else None
        if n is None or rh.get("generation", n.attempt) != n.attempt:
            return False
        return any(is_hard_signal(s.get("signal", "")) for s in (rh.get("signals") or []))
    return {nid for r in st.reward_hacks
            if _has_current_hard_signal(r) and (nid := _coerce_node_id(r)) is not None}


def _on_trust_gate_changed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Operator edited the run's trust gate after launch (server config edit). Last write
    # wins so the change engages in every fold — live view, resume, reset — immediately.
    _tg = str(d.get("trust_gate", "")).strip().lower()
    if _tg in ("audit", "gate", "block"):
        st.trust_gate = _tg


def _generation_map_matches(st: RunState, d: dict) -> bool:
    """Validate the whole candidate-generation snapshot carried by a best_confirmed event.
    A confirmation pass spans several nodes; checking only the chosen node would still accept a
    winner computed using a reset competitor's stale seeds. Old events have no map and remain valid."""
    raw = d.get("generations", _MISSING)
    if raw is _MISSING:
        # Legacy best_confirmed (pre-generation-map). Modern producers ALWAYS stamp `generations`
        # (confirm_phase), so this branch is reached only by OLD persisted logs. Validate just the
        # CHOSEN winner: rejecting whenever ANY unrelated node was later aborted/tombstoned would
        # retroactively drop a legitimately-completed confirmation that the pre-batch fold accepted
        # (invariant 5b — an old log must fold as it did before). A winner that is itself
        # aborted/tombstoned is still correctly rejected.
        n = _node_for_event(st, d)
        return n is None or (not n.tombstoned and n.id not in st.aborted_nodes
                             and _generation_matches(n, d))
    if not isinstance(raw, dict):
        return False
    chosen = _coerce_node_id(d)
    seen: set[int] = set()
    for raw_nid, raw_generation in raw.items():
        nid = _coerce_node_id({"node_id": raw_nid})
        generation = _event_generation({"generation": raw_generation})
        if (nid is None or generation in (_MISSING, None)
                or nid not in st.nodes or nid in st.aborted_nodes
                or st.nodes[nid].tombstoned or st.nodes[nid].attempt != generation):
            return False
        seen.add(nid)
    if d.get("node_id") is not None and (chosen is None or chosen not in seen):
        return False
    # A candidate created while confirmation was running was absent from the snapshot and therefore
    # never compared. Do not mark confirmation complete until the snapshot exactly covers the current
    # candidate set (a reset is already caught by the per-entry generation checks above).
    active = {nid for nid, n in st.nodes.items()
              if nid not in st.aborted_nodes and not n.tombstoned}
    return seen == active


def _on_reward_hack_suspected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if n is not None and n.id in st.aborted_nodes:
        return
    generation = _event_generation(d)
    if generation is not _MISSING and (n is None or not _generation_matches(n, d)):
        return
    # `signals` MUST be a list of dicts. `hard_flagged_ids._has_current_hard_signal` later runs
    # `s.get("signal", "")` over `rh.get("signals") or []`, and the fold loop has no per-event
    # try/except — so a forged/hand-edited truthy SCALAR (`"leak"`, `5` -> TypeError on iteration)
    # or a list of non-dicts (`["leak"]`, `[None]` -> AttributeError on `.get`) bricks EVERY
    # fold/replay/resume/view of the run under trust_gate gate/block, and the digest's trust
    # reflection even under `audit`. Same class as the `node_ids` scalar guard above; fold must stay
    # total. Bounded like every other list this fold admits, so a forged event cannot park an
    # unbounded array in RunState either.
    raw_signals = d.get("signals")
    record = {"node_id": nid,
              "signals": [s for s in raw_signals if isinstance(s, dict)][:64]
                         if isinstance(raw_signals, list) else [],
              "evidence_version": d.get("evidence_version", 0),
              "code_digest": d.get("code_digest")}
    if n is not None:
        record["generation"] = n.attempt
    st.reward_hacks.append(record)


def _on_node_verified(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # R1-c: freeze a node's calibrated §12-verifier soundness score (the LLM output can't be recomputed
    # in the deterministic fold). Generation-scoped exactly like proxy_scored: a score computed against a
    # reset-abandoned attempt (stale generation) is dropped, so a stale-attempt verification can't bias
    # selection. Audit sidecar — read ONLY as a metric-tie-break in _select_best; never a raw override.
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is None or n.id in st.aborted_nodes or n.tombstoned
            or n.status is not NodeStatus.evaluated):
        return
    # node_verified is a BRAND-NEW selection-affecting event — no legacy log carries it, and the engine
    # always stamps `generation` (n.attempt) at emit — so REQUIRE the stamp (reject a missing OR mismatched
    # generation) rather than accept-a-missing-one as current. A forged/hand-edited unscoped score can't
    # then bias selection; this is strictly tighter than the additive-legacy pattern the older per-node
    # events must keep for their pre-generation logs.
    if _event_generation(d) is _MISSING or not _generation_matches(n, d):
        return
    evidence_digest = d.get("evidence_digest")
    if evidence_digest is None:
        # Digestless rows are a legacy raw-metric format.  Once confirmation or holdout data exists, the
        # evidence has a revision identity and an in-flight legacy row cannot be allowed to restore a score
        # invalidated by that newer evidence.
        if n.confirmed_mean is not None or n.holdout_metric is not None:
            return
    elif (not isinstance(evidence_digest, str)
          or evidence_digest != verifier_evidence_digest(st.direction, n)):
        return
    score = d.get("score")
    if is_usable_metric(score) and 0.0 <= float(score) <= 1.0:
        n.verifier_score = float(score)


def _on_verifier_group_scored(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Publish a complete verifier tie treatment only after every member validates."""
    # This event is atomic and selection-affecting; reject the entire record unless
    # version, contract,
    # membership, generation and evidence revision all match the current selector-visible tie group.
    if (not st.select_verifier_tiebreak or isinstance(d.get("v"), bool) or d.get("v") != 1
            or d.get("contract") != VERIFIER_SELECTION_CONTRACT
            or d.get("contract") != st.select_verifier_contract):
        return
    requested = d.get("requested_samples")
    if (isinstance(requested, bool) or not isinstance(requested, int)
            or requested != st.select_verifier_samples):
        return
    members = d.get("members")
    if not isinstance(members, list) or not 2 <= len(members) <= 8:
        return
    seen: set[int] = set()
    staged: list[tuple[Node, float]] = []
    for row in members:
        if not isinstance(row, dict):
            return
        nid = _coerce_node_id(row)
        node = st.nodes.get(nid) if nid is not None else None
        if (node is None or nid in seen or node.id in st.aborted_nodes or node.tombstoned
                or node.status is not NodeStatus.evaluated):
            return
        if _event_generation(row) is _MISSING or not _generation_matches(node, row):
            return
        digest = row.get("evidence_digest")
        if not isinstance(digest, str) or digest != verifier_evidence_digest(st.direction, node):
            return
        score, n_samples, agreement = row.get("score"), row.get("n_samples"), row.get("agreement")
        if not is_usable_metric(score) or not 0.0 <= float(score) <= 1.0:
            return
        if (isinstance(n_samples, bool) or not isinstance(n_samples, int)
                or not 1 <= n_samples <= requested or n_samples * 2 <= requested):
            return
        if not is_usable_metric(agreement) or not 0.5 < float(agreement) <= 1.0:
            return
        method = row.get("method")
        if not isinstance(method, str) or len(method) > 80:
            return
        seen.add(nid)
        staged.append((node, float(score)))
    expected = {frozenset(node.id for node in group) for group in verifier_tie_groups(st)}
    # Member validity is insufficient; this must be the complete selector-reachable tie-set.
    # Reject a well-formed subset, a losing tie, or a mean group shadowed by a non-empty holdout pool before
    # publishing any score, so a forged/torn record cannot steer a different comparison.
    if frozenset(seen) not in expected:
        return
    for node, score in staged:
        node.verifier_score = score

def _on_best_confirmed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # R1 epoch identity: a confirmation certificate authorizes selection state (confirmed_done + the
    # confirm-override in _select_best), so it must be bound to the candidate-set epoch it was computed
    # against. A best_confirmed STAMPED with a stale epoch — e.g. an in-flight confirm pass that appends
    # AFTER a cross-writer reopen bumped search_epoch — is rejected, so an epoch-(N-1) confirmation can't
    # authorize state a fresh epoch N must re-decide. Additive/reader-defaulted: a missing stamp (legacy
    # logs / manual events) is treated as legacy-current, so old logs fold byte-identically. The
    # requeuing-reopen case is already caught by _generation_map_matches; this closes the NON-requeuing
    # reopen (no disclosed holdout), which leaves generations unchanged but still bumps the epoch.
    # This boolean changes whether confirmation can override the verifier's CI-tie winner.  Do not coerce
    # strings/numbers by truthiness: a malformed certificate is rejected as a whole and cannot even close
    # the confirmation gate.  Absence remains the legacy `True` default.
    if "significant" in d and not isinstance(d.get("significant"), bool):
        return
    if "search_epoch" in d and d.get("search_epoch") != st.search_epoch:
        return
    if not _generation_map_matches(st, d):
        return
    nid = _coerce_node_id(d)
    if "node_id" in d:
        ctx.best_confirmed = nid
        # R1-d: record whether this certificate is a SIGNIFICANT winner (default True: legacy events with no
        # `significant` field keep the unconditional override). A non-significant certificate is a STATISTICAL
        # tie the verifier CI-tie may resolve instead — see _select_best.
        ctx.best_confirmed_significant = d.get("significant", True)
    st.confirmed_done = True   # the confirmation phase ran to completion


def _apply_trust_gate(st: RunState) -> set:
    """T2 trust enforcement post-pass: under "gate"/"block", a node flagged for a reward-hack or
    data-leakage signal must not be selectable as best (closes "a hacked/leaky node can win").
    Order-independent: computed from the folded `reward_hacks` after the full pass (see
    `flagged_node_ids`). Returns the flagged node-id set for `_select_best`."""
    flagged = flagged_node_ids(st)
    # Bar the flagged set from BREEDING/confirm targets (doc 14 §2.2): under `gate` the node stays
    # feasible (kept in the tree for diversity/audit, barred only from winning) but
    # `breedable_nodes()` skips it so the search doesn't sink budget improving a cheating lineage.
    # `block` ALSO makes it infeasible (feasible=False removes it from feasible_nodes() entirely),
    # the stricter mode.
    st.breed_excluded = set(flagged)
    if st.trust_gate == "block":
        for nid in flagged:
            nb = st.nodes.get(nid)
            if nb is not None:
                nb.feasible = False
    return flagged


def select_best_node(st: RunState, pool, *, best_confirmed: int | None = None,
                     best_confirmed_significant: bool = True) -> Node | None:
    """THE SELECTOR: the node `_select_best` crowns out of `pool` — the mean-based pick, then the
    variance-gated confirm certificate, then the holdout-gated promotion, then an explicit human
    approval — or None when `pool` is empty. Pure: it reads `st` and decides, and writes nothing.

    `pool` is the ELIGIBLE population and every override is honoured only for a node inside it —
    `_select_best` passes `promotion_eligible_nodes(st, flagged=...)`, which is exactly the
    "evaluated, not tombstoned, `SearchFitness.eligible`" test each override used to spell out for
    its own candidate. A caller may pass a narrower population and ask the SAME question of it:
    `engine/champion_caveats.py::mislead_gap` asks which node the selector would crown among the
    nodes the record says nothing against, which until review 2026-09-22 (ENG2-09) it answered with
    a raw-metric maximum of its own — so a confirm demotion, a holdout pick or a tombstone read as a
    NEGATIVE inflation on ordinary runs. One selector, so the champion and that pair cannot drift.

    `best_confirmed` / `best_confirmed_significant` are the confirm phase's certificate as the fold
    threaded it (`_FoldCtx`); the post-pass also leaves them on the state
    (`RunState.confirm_certificate_node` / `_significant`) for exactly that second caller.
    """
    # Multi-objective (#5): a constraint-violating node is excluded from selection — it keeps
    # its metric for the audit trail but can never be chosen best. If NOTHING is feasible,
    # there is no valid best (best_node_id stays None).
    # Exclude nodes with no usable metric: a hand-edited / BYO-script node_evaluated event can carry
    # metric=null yet fold to status=evaluated, and comparing None vs a float in the chooser below
    # would raise TypeError and brick every re-fold/resume. Such a node simply can't be "best".
    # R1/SearchFitness: the eligibility predicate, the ranked-scalar keys and the direction chooser are
    # OWNED by core.fitness.SearchFitness — one spelling shared with rank_by_metric / holdout_topk, so a
    # later scored tie-break (R1-c) composes in exactly one place. Byte-identical to the inlined logic.
    fit = SearchFitness(st.direction, verifier_tiebreak=st.select_verifier_tiebreak,
                        ci_tie=st.verifier_ci_tie)
    evaluated = list(pool)
    members = {n.id for n in evaluated}
    chosen: Node | None = None
    if evaluated:
        # If any node has been confirmed (multi-seed), the final answer must be the
        # robust winner: rank confirmed nodes by confirmed_mean. With no confirmations
        # this is identical to ranking all evaluated nodes by their single metric.
        # R1-c: promotion_key adds a calibrated-verifier tie-break slot when select_verifier_tiebreak is
        # on — it resolves metric-EQUAL contests only, never overriding a strictly-better robust_metric.
        confirmed = [n for n in evaluated if n.confirmed_mean is not None]
        candidates = confirmed if confirmed else evaluated
        # R1-d: `best_ci` widens the verifier tie-break to a STATISTICAL tie when `verifier_ci_tie` is on
        # (grounded in confirmed_std/seeds); it is IDENTICAL to the exact-tie `best(promotion_key)` when the
        # flag is off (or nodes lack confirm-noise data). §21.7: never picks over a significantly-better mean.
        chosen = fit.best_ci(candidates)

    # The variance-gated confirmation decision (I10) overrides the mean-based pick — but never
    # past the feasibility gate (#5): a constraint-violating node must not become best even if
    # the confirm phase ran on it (the mean-based pick above already excluded infeasibles).
    # The confirm certificate is the confirm phase's OWN authoritative winner (robust_selection over the
    # multi-seed means + a significance test), so it overrides the mean pick. R1-d COMPOSITION:
    # the certificate overrides only when it found a SIGNIFICANT winner — OR when verifier_ci_tie is off
    # (then it overrides unconditionally, byte-identical to before). When the confirm found NO significant
    # winner (a statistical tie) AND ci_tie is on, the `best_ci` soundness pick above STANDS, because that
    # tie is EXACTLY what the CI-tie exists to resolve — an unconditional override would erase it and make
    # R1-d a no-op. Scope boundary (unchanged): among nodes the confirm DID significantly separate, the
    # winner is the confirm phase's, not the verifier's.
    # (Membership of `pool` IS the old "evaluated, not tombstoned, `fit.eligible`" clause: `pool` is
    # `promotion_eligible_nodes(st, flagged=flagged)`, or a subset of it.)
    if (best_confirmed is not None and best_confirmed in members
            and (best_confirmed_significant or not st.verifier_ci_tie)):
        chosen = st.nodes[best_confirmed]

    # D1 holdout-gated promotion: when the run recorded holdout_select, the champion is the best
    # node ON THE HOLDOUT PARTITION among those that were holdout-scored (the val-top-k — so the
    # search metric still decides WHO gets a holdout eval, but the unseen signal decides who WINS).
    # Applied LAST: the holdout is a stronger discipline than the confirm mean (it is data/splits
    # the search never optimized against — AIRA: picking on the search signal overfits 9-13 pp).
    # Same guards as every other pick: feasibility + trust flags.
    if st.holdout_select and evaluated:
        hpool = [n for n in evaluated if is_usable_metric(n.holdout_metric)]
        if hpool:
            # holdout_key carries the SAME verifier tie-break slot (when select_verifier is on): a tie on
            # the unseen-signal holdout metric is broken by soundness too, so the stronger holdout signal
            # decides first and the verifier only resolves a holdout tie (never overrides it). R1-d SCOPE:
            # the holdout pick uses the EXACT-tie holdout_key, NOT the CI widening — the holdout metric is a
            # single unseen-partition score with no multi-seed std, so there is no confirm-noise CI to widen
            # with, and the unseen signal is deliberately stronger than a search-metric soundness tie-break.
            # So `verifier_ci_tie` refines only the confirmed-MEAN pick (above); when holdout_select is on
            # (default) the holdout exact-tie pick is the final word — R1-d's CI widening is effective on the
            # champion only when holdout_select is OFF.
            chosen = fit.best_holdout(hpool)

    # An explicit human approval of a real non-best node is a selection decision, not a global latch
    # that authorizes publication of some OTHER algorithmic best. Honor it last. (An approval of a
    # node outside `pool` is not honoured here; `_select_best` invalidates it for the whole run.)
    if st.approved and st.approved_node_id is not None and st.approved_node_id in members:
        chosen = st.nodes[st.approved_node_id]
    return chosen


def _select_best(st: RunState, flagged: set, best_confirmed: int | None,
                 best_confirmed_significant: bool = True) -> None:
    """Best-selection post-pass: derive `best_node_id` (mean-based pick -> variance-gated confirm
    override -> holdout-gated promotion) plus the audit-only generalization gap. Pure and
    deterministic over the folded state — the tail of `fold`, extracted verbatim.

    The DECISION is `select_best_node` over `promotion_eligible_nodes` (review 2026-09-22, ENG2-09,
    split out so a second caller asks the selector instead of re-spelling it); what stays here is
    what only the whole run's answer may do — publish it, void an approval that no longer names an
    eligible node, stamp the certificate it was decided with, and derive the audit gaps."""
    evaluated = promotion_eligible_nodes(st, flagged=flagged)
    chosen = select_best_node(st, evaluated, best_confirmed=best_confirmed,
                              best_confirmed_significant=best_confirmed_significant)
    if chosen is not None:
        st.best_node_id = chosen.id
    # If the approved node is no longer eligible, invalidate the grant so the engine asks again
    # instead of finalizing another.
    if (st.approved and st.approved_node_id is not None
            and st.approved_node_id not in {n.id for n in evaluated}):
        st.approved = False
        st.approved_node_id = None
    # The certificate this answer was decided with, for a caller that must ask the SAME selector
    # about a narrower population (`select_best_node`'s docstring). Fold-internal, never a decision
    # input of the fold itself.
    st.confirm_certificate_node = best_confirmed
    st.confirm_certificate_significant = bool(best_confirmed_significant)

    # Derived generalization gap (audit-only, Trust panel): how much better the search metric
    # looked than the unseen-signal metric — holdout when present, else the confirmed mean.
    # Direction-aware so positive always means "overperformed on the signal the search saw".
    for n in st.nodes.values():
        robust = n.holdout_metric if n.holdout_metric is not None else n.confirmed_mean
        if not is_usable_metric(robust) or not is_usable_metric(n.metric):
            continue
        n.generalization_gap = (n.metric - robust) if st.direction == "max" else (robust - n.metric)
    # Derived self-report gap (audit-only, doc 52 row 10a): how much better the candidate SAID it
    # did than the host scorer measured. Direction-aware so positive always means "over-reported".
    # Deliberately NOT `generalization_gap`: that one compares the signal the search optimised with
    # an unseen one, and on a host-scored node the search optimised the HOST's number.
    for n in st.nodes.values():
        if not is_usable_metric(n.self_metric) or not is_usable_metric(n.metric):
            continue
        n.self_report_gap = ((n.self_metric - n.metric) if st.direction == "max"
                             else (n.metric - n.self_metric))


# This family's rows of the fold's dispatch table. `replay.py::_HANDLERS` is assembled from every
# family's table and refuses a type two of them claim, so a selection handler is registered HERE,
# beside its body, and nowhere else.
HANDLERS = {
    EV_TRUST_GATE_CHANGED: _on_trust_gate_changed,
    EV_REWARD_HACK_SUSPECTED: _on_reward_hack_suspected,
    EV_NODE_VERIFIED: _on_node_verified,
    EV_VERIFIER_GROUP_SCORED: _on_verifier_group_scored,
    EV_BEST_CONFIRMED: _on_best_confirmed,
}
