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
# Engine cadence: _maybe_verify_ties / _metric_tie_groups
# --------------------------------------------------------------------------- #

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


def test_settings_default_off():
    assert Settings().select_verifier is False and Settings().select_verifier_samples == 3
