"""R1-c — persisted per-node calibrated-verifier metric-tie-break in best-selection (Part IV unblock).

Locks: `node_verified` folds into `Node.verifier_score` (generation-scoped, stale-attempt dropped, range
guarded); `select_verifier_tiebreak` breaks an EXACT metric tie by the higher soundness score (both
directions) and NEVER overrides a strictly-better metric (§21.7); the engine cadence `_maybe_verify_ties`
lazily verifies tied nodes and is a no-op when off / no tie / no client. Byte-identical selection when off
(that is separately locked by test_golden_replay)."""
from __future__ import annotations

from looplab.core.config import Settings
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _run(tmp_path, direction="max", *, select_verifier=False) -> EventStore:
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": direction,
                             "select_verifier": select_verifier})
    return s


def _add(s: EventStore, nid: int, metric: float) -> None:
    s.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": f"exp {nid}"}})
    s.append("node_evaluated", {"node_id": nid, "metric": metric})


# --------------------------------------------------------------------------- #
# Fold: node_verified -> Node.verifier_score
# --------------------------------------------------------------------------- #

def test_node_verified_folds_into_score(tmp_path):
    s = _run(tmp_path)
    _add(s, 0, 0.9)
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.73})
    assert fold(s.read_all()).nodes[0].verifier_score == 0.73


def test_node_verified_without_generation_is_dropped(tmp_path):
    # a BRAND-NEW selection-affecting event has no legacy logs, so a missing generation stamp is REJECTED
    # (not accepted-as-current) — a forged/hand-edited unscoped score can't bias selection.
    s = _run(tmp_path)
    _add(s, 0, 0.9)
    s.append("node_verified", {"node_id": 0, "score": 0.8})     # no generation
    assert fold(s.read_all()).nodes[0].verifier_score is None


def test_node_verified_stale_generation_is_dropped(tmp_path):
    s = _run(tmp_path)
    _add(s, 0, 0.9)
    s.append("node_reset", {"node_id": 0, "from_stage": "eval"})           # bumps attempt -> 1
    s.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.9})
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.9})   # stale gen 0 -> dropped
    assert fold(s.read_all()).nodes[0].verifier_score is None


def test_node_verified_out_of_range_ignored(tmp_path):
    s = _run(tmp_path)
    _add(s, 0, 0.9)
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 1.5})   # not in [0,1]
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": True})  # bool is not a score
    assert fold(s.read_all()).nodes[0].verifier_score is None


def test_verifier_score_cleared_on_reset(tmp_path):
    # verify-then-reset: a soundness score judged the OLD attempt's result, so a reset must scrub it
    # (like proxy_scores / holdout_metric) — else it survives onto a re-evaluated, different result.
    s = _run(tmp_path)
    _add(s, 0, 0.9)
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.88})
    assert fold(s.read_all()).nodes[0].verifier_score == 0.88
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})    # attempt -> 1
    assert fold(s.read_all()).nodes[0].verifier_score is None


def test_stale_verifier_score_does_not_bias_the_new_attempt(tmp_path):
    # the reviewer's scenario: an attempt-0 soundness score must NOT decide the tie for the DIFFERENT
    # attempt-1 result. After the reset-clear, node 0's re-eval is unscored (neutral) like node 1.
    s = _run(tmp_path, "max", select_verifier=True)
    _add(s, 0, 0.9)
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.88})   # attempt-0 result
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})           # discard attempt 0
    s.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.5})  # new, different result
    _add(s, 1, 0.5)                                                             # node 1 ties at 0.5
    # node 0's stale 0.88 was cleared -> both neutral 0.5 -> id tie-break -> max picks #1, NOT the
    # stale-score-boosted #0 (which is what happened before the reset-clear fix)
    assert fold(s.read_all()).best_node_id == 1


def test_old_logs_default_off(tmp_path):
    s = _run(tmp_path)                                                     # no select_verifier key
    _add(s, 0, 0.9)
    st = fold(s.read_all())
    assert st.nodes[0].verifier_score is None and st.select_verifier_tiebreak is False


# --------------------------------------------------------------------------- #
# Fold: the tie-break in best-selection
# --------------------------------------------------------------------------- #

def test_tiebreak_off_ignores_score_and_uses_id(tmp_path):
    s = _run(tmp_path, "max", select_verifier=False)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)                                                        # exact tie
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.9})
    assert fold(s.read_all()).best_node_id == 1     # flag off -> score ignored -> max picks highest id


def test_tiebreak_on_prefers_higher_soundness(tmp_path):
    s = _run(tmp_path, "max", select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)                                                        # exact tie; max would pick #1
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.9})
    s.append("node_verified", {"node_id": 1, "generation": 0, "score": 0.2})
    assert fold(s.read_all()).best_node_id == 0     # #0 more sound -> wins the tie despite the lower id


def test_tiebreak_never_overrides_a_better_metric(tmp_path):
    s = _run(tmp_path, "max", select_verifier=True)
    _add(s, 0, 0.95)                                                       # strictly better metric
    _add(s, 1, 0.90)
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.1})    # ...but low soundness
    s.append("node_verified", {"node_id": 1, "generation": 0, "score": 0.99})   # #1 high soundness
    assert fold(s.read_all()).best_node_id == 0     # the metric wins; the advisory score can't override it


def test_tiebreak_min_direction(tmp_path):
    s = _run(tmp_path, "min", select_verifier=True)
    _add(s, 0, 0.1)
    _add(s, 1, 0.1)                                                        # exact tie; min would pick #0
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.2})
    s.append("node_verified", {"node_id": 1, "generation": 0, "score": 0.9})
    assert fold(s.read_all()).best_node_id == 1     # #1 more sound -> wins despite the higher id


def test_tiebreak_unscored_node_is_neutral(tmp_path):
    # a scored node ABOVE the neutral midpoint (0.5) beats an unscored (neutral) tied node
    s = _run(tmp_path, "max", select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.8})   # #1 stays unscored (neutral)
    assert fold(s.read_all()).best_node_id == 0     # 0.8 > neutral 0.5 -> #0 wins over unscored #1


# --------------------------------------------------------------------------- #
# Fold: the tie-break on the HOLDOUT path (holdout_select on)
# --------------------------------------------------------------------------- #

def _holdout(s, nid, m):
    s.append("holdout_evaluated", {"node_id": nid, "generation": 0, "search_epoch": 0, "metric": m})


def test_holdout_override_breaks_tie_by_verifier(tmp_path):
    # holdout_select on: the final champion is the holdout pick. A tie on the UNSEEN-signal metric must
    # be broken by soundness too — so select_verifier actually influences the champion on this path.
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max",
                             "select_verifier": True, "holdout_select": True})
    _add(s, 0, 0.85)
    _add(s, 1, 0.90)                     # the mean pick alone would prefer #1 (higher search metric)
    _holdout(s, 0, 0.80)
    _holdout(s, 1, 0.80)                 # ...but they TIE on the holdout metric
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.9})   # #0 sounder
    s.append("node_verified", {"node_id": 1, "generation": 0, "score": 0.2})
    assert fold(s.read_all()).best_node_id == 0     # holdout override breaks the 0.80 tie by soundness


def test_holdout_override_tie_falls_to_id_when_verifier_off(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max", "holdout_select": True})
    _add(s, 0, 0.85)
    _add(s, 1, 0.90)
    _holdout(s, 0, 0.80)
    _holdout(s, 1, 0.80)
    assert fold(s.read_all()).best_node_id == 1     # flag off -> legacy max-id holdout tie-break


# --------------------------------------------------------------------------- #
# Engine cadence: _maybe_verify_ties / _metric_tie_groups
# --------------------------------------------------------------------------- #

def test_metric_tie_groups_mirrors_the_confirmed_pool(tmp_path):
    # when ANY eligible node is confirmed, _select_best ranks ONLY confirmed nodes; the cadence must not
    # burn verifier calls on unconfirmed ties the fold will never consult.
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)                                  # an UNCONFIRMED tie
    _add(s, 2, 0.8)
    st = fold(s.read_all())
    st.nodes[2].confirmed_mean = 0.8                 # a confirmation exists -> pool = confirmed-only
    assert Engine._metric_tie_groups(None, st) == []   # the unconfirmed 0.9 tie is NOT surfaced


def test_metric_tie_groups_surfaces_confirmed_ties(tmp_path):
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.7)
    _add(s, 1, 0.7)
    _add(s, 2, 0.9)                                  # unconfirmed, higher metric — excluded once confirmed exists
    st = fold(s.read_all())
    st.nodes[0].confirmed_mean = 0.8                 # #0 and #1 confirmed & TIED on confirmed mean
    st.nodes[1].confirmed_mean = 0.8
    groups = Engine._metric_tie_groups(None, st)
    assert len(groups) == 1 and {n.id for n in groups[0]} == {0, 1}   # only the confirmed tie

def test_metric_tie_groups_scores_the_ci_band_when_verifier_ci_tie(tmp_path):
    """CODEX #6 regression: the producer must create verifier work for CI-tied (near-equal) nodes — the
    exact set best_ci compares — else R1-d is a no-op (CI candidates sit unscored at the neutral midpoint)."""
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.90)
    _add(s, 1, 0.905)                            # DIFFERENT robust metrics -> NO exact tie
    st = fold(s.read_all())
    for nid, m in ((0, 0.90), (1, 0.905)):       # make them confirmed + CI-tied (SE=0.05/√3=0.029)
        st.nodes[nid].confirmed_mean = m
        st.nodes[nid].confirmed_std = 0.05
        st.nodes[nid].confirmed_seeds = 3

    class _S:
        _verifier_ci_tie = True
    groups = Engine._metric_tie_groups(_S(), st)
    assert any({0, 1} <= {n.id for n in g} for g in groups)   # the CI-band is produced -> both get scored
    assert Engine._metric_tie_groups(None, st) == []          # off -> distinct metrics, no group


def _cnode(nid, mean, *, std, seeds, score):
    from looplab.core.models import Idea, Node, NodeStatus
    n = Node(id=nid, operator="draft", idea=Idea(operator="draft"), metric=mean,
             status=NodeStatus.evaluated, feasible=True, confirmed_mean=mean)
    n.confirmed_std, n.confirmed_seeds, n.verifier_score = std, seeds, score
    return n


def test_non_significant_confirm_does_not_erase_best_ci(tmp_path):
    """CODEX #7 regression: a confirm certificate that found NO significant winner is a statistical tie the
    CI-tie exists to resolve — the override must NOT erase best_ci. A SIGNIFICANT certificate still wins."""
    from looplab.core.models import RunState
    from looplab.events.replay import _select_best
    st = RunState(direction="max")
    st.select_verifier_tiebreak = True
    st.verifier_ci_tie = True
    st.nodes[0] = _cnode(0, 0.905, std=0.05, seeds=3, score=0.2)   # raw leader, LOW soundness
    st.nodes[1] = _cnode(1, 0.90, std=0.05, seeds=3, score=0.95)   # CI-tied, HIGH soundness
    # confirm chose 0 but NOT significant -> the CI-tie soundness pick (id 1) must stand
    _select_best(st, set(), best_confirmed=0, best_confirmed_significant=False)
    assert st.best_node_id == 1
    # a SIGNIFICANT confirm of 0 DOES override (the confirm separated them) -> id 0
    _select_best(st, set(), best_confirmed=0, best_confirmed_significant=True)
    assert st.best_node_id == 0
    # ci_tie OFF -> unconditional override, byte-identical to before (even non-significant) -> id 0
    st.verifier_ci_tie = False
    _select_best(st, set(), best_confirmed=0, best_confirmed_significant=False)
    assert st.best_node_id == 0


def test_metric_tie_groups_includes_holdout_ties_when_holdout_select(tmp_path):
    """R1-c holdout completeness (§21.18): nodes tied on holdout_metric but NOT on robust_metric form a
    tie-COMPONENT when holdout_select is on, so their holdout_key verifier slot finally gets produced. With
    holdout_select off, the holdout tie is not surfaced (byte-identical to the old robust-only grouping)."""
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.7)
    _add(s, 1, 0.8)                              # DIFFERENT robust metrics -> no robust tie
    st = fold(s.read_all())
    st.nodes[0].holdout_metric = 0.5             # ...but TIED on the unseen-signal holdout metric
    st.nodes[1].holdout_metric = 0.5

    class _S:
        _holdout_select = True
    groups = Engine._metric_tie_groups(_S(), st)
    assert len(groups) == 1 and {n.id for n in groups[0]} == {0, 1}   # holdout tie surfaced
    # holdout_select off -> no holdout linking -> the (robust-distinct) nodes are not a tie
    assert Engine._metric_tie_groups(None, st) == []


def test_metric_tie_groups_surfaces_unconfirmed_holdout_tie_past_the_confirmed_pool(tmp_path):
    """R1-c completeness gap: the holdout pick ranks the FULL eligible holdout pool, NOT the confirmed
    subset. A confirmed node coexisting with an UNCONFIRMED node tied on the holdout metric must still
    surface the unconfirmed node — else it is never scored, stays at the neutral verifier midpoint, and can
    WIN the holdout tie unverified. Regression for the confirmed-subset vs full-holdout-pool mismatch."""
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.70)                                # UNCONFIRMED (would be dropped by the mean pool)
    _add(s, 1, 0.85)
    _add(s, 2, 0.90)                                # distinct robust metrics -> only a HOLDOUT tie
    st = fold(s.read_all())
    for nid in (0, 1, 2):
        st.nodes[nid].holdout_metric = 0.80         # all TIE on the unseen-signal holdout metric
    st.nodes[1].confirmed_mean = 0.85               # 1,2 confirmed -> mean pool = {1,2} only
    st.nodes[2].confirmed_mean = 0.90

    class _S:
        _holdout_select = True
    groups = Engine._metric_tie_groups(_S(), st)
    # the unconfirmed node 0 must join the surfaced holdout tie-component, not be dropped with the mean pool
    assert len(groups) == 1 and {n.id for n in groups[0]} == {0, 1, 2}


def test_reopen_clears_stale_confirm_override(tmp_path):
    # a best_confirmed from epoch N must NOT keep overriding after a reopen to epoch N+1 — the reopen
    # clears BOTH confirmed_done AND the threaded ctx.best_confirmed the confirm-override reads.
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})
    _add(s, 0, 0.5)
    s.append("best_confirmed", {"node_id": 0, "significant": True, "generations": {"0": 0},
                                "search_epoch": 0})
    s.append("run_finished", {})                               # finish so the reopen rotates the epoch
    assert fold(s.read_all()).best_node_id == 0               # epoch-0 confirm override
    s.append("run_reopened", {})                              # -> epoch 1, clears the completion gates
    _add(s, 1, 0.9)                                            # a better node in the new epoch
    assert fold(s.read_all()).best_node_id == 1              # stale confirm override cleared -> metric winner


class _VClient:
    """Fake reflect client: the §12 verifier's tool call returns a fixed verdict per criterion."""
    def __init__(self, verdict="yes"):
        self.verdict = verdict
        self.calls = 0

    def complete_tool(self, messages, json_schema):
        self.calls += 1
        return {"verdicts": [self.verdict], "rationales": ["r"]}

    def complete_text(self, messages):
        return "x"


class _FlakyVClient:
    """Returns an UNPARSEABLE verdict on the `fail_on`-th call (that node's verify -> None)."""
    def __init__(self, fail_on):
        self.fail_on = fail_on
        self.calls = 0

    def complete_tool(self, messages, json_schema):
        self.calls += 1
        return {"verdicts": ["" if self.calls == self.fail_on else "yes"], "rationales": ["r"]}

    def complete_text(self, messages):
        return "x"


class _NoisyVClient:
    """Cycles disagreeing verdicts across samples so cross-sample agreement is low (0.33 for 3)."""
    def __init__(self):
        self.calls = 0

    def complete_tool(self, messages, json_schema):
        self.calls += 1
        return {"verdicts": [["strong_yes", "strong_no", "unclear"][(self.calls - 1) % 3]],
                "rationales": ["r"]}

    def complete_text(self, messages):
        return "x"


class _Host:
    # bind the real engine methods so internal self-calls resolve
    _maybe_verify_ties = Engine._maybe_verify_ties
    _metric_tie_groups = Engine._metric_tie_groups
    _verifier_soundness = Engine._verifier_soundness

    def __init__(self, store, *, select_verifier=True, client=None, samples=1):
        self.store = store
        self._select_verifier = select_verifier
        self._select_verifier_samples = samples
        self.researcher = None
        self.developer = None
        self._client = client

    def _reflect_client(self):
        return self._client


def test_metric_tie_groups_finds_unresolved_ties(tmp_path):
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _add(s, 2, 0.5)
    groups = Engine._metric_tie_groups(None, fold(s.read_all()))   # self unused
    assert len(groups) == 1 and {n.id for n in groups[0]} == {0, 1}


def test_maybe_verify_ties_scores_tied_nodes(tmp_path):
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    client = _VClient("yes")                                        # "yes" -> 0.75
    st2 = _Host(s, client=client)._maybe_verify_ties(fold(s.read_all()))
    ev = [e for e in s.read_all() if e.type == "node_verified"]
    assert len(ev) == 2 and all(e.data["score"] == 0.75 for e in ev)
    assert client.calls == 2                                        # one verify per tied node (samples=1)
    # both scored 0.75 -> the verifier component is itself tied -> the id tie-break stands (max -> #1)
    assert st2.best_node_id == 1


def test_maybe_verify_ties_noop_when_off(tmp_path):
    s = _run(tmp_path, select_verifier=False)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _Host(s, select_verifier=False, client=_VClient())._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "node_verified" for e in s.read_all())


def test_maybe_verify_ties_noop_without_client(tmp_path):
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _Host(s, client=None)._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "node_verified" for e in s.read_all())


def test_maybe_verify_ties_noop_without_a_tie(tmp_path):
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.5)                                                 # no tie
    _Host(s, client=_VClient())._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "node_verified" for e in s.read_all())


def test_node_verified_carries_provenance(tmp_path):
    # a selection-affecting event must be auditable: it carries n_samples + agreement (fold reads score)
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _Host(s, client=_VClient("yes"), samples=1)._maybe_verify_ties(fold(s.read_all()))
    ev = [e for e in s.read_all() if e.type == "node_verified"]
    assert ev and all("n_samples" in e.data and "agreement" in e.data for e in ev)


def test_tie_group_abstains_atomically_on_a_failure(tmp_path):
    # ATOMIC: if ANY member of a tie group fails verification, the WHOLE group is left unscored (its tie
    # falls to the id break) — never half-scored (which would let a neutral 0.5 outrank a verified-low).
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _Host(s, client=_FlakyVClient(fail_on=2), samples=1)._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "node_verified" for e in s.read_all())   # neither member committed


def test_low_agreement_verdict_is_abstained(tmp_path):
    # a high-variance verdict (samples disagree -> agreement 0.33 < 0.5) must not decide a tie: abstain,
    # so the whole group (atomic) stays on the id tie-break rather than steering on noise.
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _Host(s, client=_NoisyVClient(), samples=3)._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "node_verified" for e in s.read_all())


def test_settings_default_off():
    assert Settings().select_verifier is False and Settings().select_verifier_samples == 3
