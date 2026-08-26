"""THE VERDICT EXISTED, WAS GOOD, AND WAS ONE CALL AWAY FROM THE PATH THE LOOP WALKS.

`benchmarks/algotune/looplab_eval.py` never prints a non-positive `speedup` without a `no_speedup`
object beside it: the class (`invalid_results`, `no_valid_speedups`, ...), the evaluator's own
verdict, the instance counts, and the task's ranked `is_solution` rejection messages. That block
reaches the durable record — it is on the final JSON stdout line, which `Node.stdout_tail` keeps —
and `read_logs` renders it. Nothing else did.

THE MEASUREMENT (`/var/tmp/looplab-bench/runs-B`, 20 finished task-arms, 56 `node_evaluated` rows)
--------------------------------------------------------------------------------------------------
Nine of those rows are a metric of 0.0 whose row carries a `no_speedup` block; all nine parse. On
those nine nodes the agents made:

    read_experiment   61 calls   ->   0 returned the reason
    read_logs         32 calls   ->  32 returned the reason

`read_experiment` is the call that gets made — 574 times across the corpus against 293 for
`read_logs` — and it rendered `metric=0.0` and stopped. On `spectral_clustering` the split was 4
`read_experiment` calls on the zero node and **zero** `read_logs` calls on the whole task-arm, so
the verdict ("98/100 valid", plus two named hack-detector messages, the fixture below) reached NO
tool output in that run at all. The loop then read the bare zero as a verdict on its HYPOTHESIS and
spent the rest of a $1.00 budget elsewhere. It was two instances from a working solver.

So this is not a missing signal (`tests/test_scored_output_evidence.py` closed that one). It is a
signal that was only on the surface nobody called. What is asserted here is that the number and its
account arrive TOGETHER on the surfaces that show the number: `read_experiment`, the
`list_experiments` line, and the always-on working set.

THE RULE, and why it is a prefix (`events/digest.py::metric_account`)
---------------------------------------------------------------------
A nested object under a `no_<something>` key on the final metric line is the eval saying why the
number beside it is not a number. The looser rule "any nested object carrying a `reason`" was
measured first and matched all 56 rows — every healthy node prints `subset_evidence.reason` — which
is what `test_a_healthy_evals_own_bookkeeping_is_not_an_account` pins.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.events import digest
from looplab.tools.run_tools import RunTools

# The bytes of `runs-B/spectral_clustering/run/nodes/node_0/score.log`, unedited — the record the
# doc-53 item was written about. Real, because a hand-written block would drift from the producer.
_SCORE_LINE = (Path(__file__).parent / "fixtures"
               / "algotune_score_line_invalid_results.txt").read_text(encoding="utf-8")

# What a HEALTHY node on that same arm printed. Also real (`runs-B/convex_hull`, node 0), and it
# carries a nested `reason` of its own — the false positive the prefix rule exists to refuse.
_HEALTHY_LINE = ('{"speedup": 1.1013, "eval_seconds": 87.3, "subset": "train", "subset_evidence": '
                 '{"asked": "train", "verified": true, "reason": "patch_marker_present", "marker": '
                 '"# --- LOOPLAB EVAL SUBSET (benchmarks/algotune/patch_eval_subset.py) ---"}, '
                 '"baseline_source": "in-harness (record exposes no baseline_time_ms to cache)"}\n')


def _state(stdout_tail: str, metric=0.0, stderr_tail: str = "") -> RunState:
    st = RunState(direction="max", goal="make the solver fast and correct")
    st.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                       metric=metric, status=NodeStatus.evaluated,
                       stdout_tail=stdout_tail, stderr_tail=stderr_tail)
    return st


def _tools(st: RunState) -> RunTools:
    rt = RunTools()
    rt.bind_state(st)
    return rt


# ------------------------------------------------------------------ THE call the loop makes

def test_the_call_the_loop_actually_makes_returns_the_reason():
    """`read_experiment` on the node — the 61-call surface — must carry the whole account.

    Four assertions because the block answers four different questions, and the corpus shows the
    loop needed each: WHICH failure class, HOW WRONG (98 of 100 is not 0 of 100), WHAT the task
    rejected, and that `N/A` was a refusal to score rather than a measured zero."""
    out = _tools(_state(_SCORE_LINE)).execute("read_experiment", {"node_id": 0})

    assert "invalid_results" in out, "the failure CLASS did not reach read_experiment"
    assert "98" in out and "100" in out, "the validity fraction did not reach read_experiment"
    assert "Detected argmax over a k-column subset" in out, \
        "the task's own ranked rejection message did not reach read_experiment"
    assert "N/A" in out, "a refusal to score is rendered indistinguishably from a measured zero"
    # …and the number itself is untouched: the account is beside it, never instead of it.
    assert "metric=0" in out


def test_the_ranked_messages_survive_the_budget():
    """The 300-char budget `read_experiment` gives `failure=` was measured to CUT the messages off
    five of the nine real blocks, and the messages are the half that says what to fix. 600 carries
    all nine whole (the longest real render is 576)."""
    account = digest.metric_account(_state(_SCORE_LINE).nodes[0])
    assert len(account) <= digest.ACCOUNT_CHARS
    assert account.count("is_solution_errors[") == 2, \
        "a ranked rejection message was dropped by the budget"
    assert "hard fail" in account, "the LAST ranked message is the one a head-clip loses"


# ------------------------------------------------------------------ the surfaces that RANK it

def test_the_listing_line_does_not_offer_a_bare_zero():
    """`list_experiments` is how the loop chooses what to build on. A line that ranks a node has to
    say what its number means, or the ranking is read as a verdict on the idea."""
    out = _tools(_state(_SCORE_LINE)).execute("list_experiments", {})
    assert "invalid_results" in out and "98/100 valid" in out


def test_the_always_on_working_set_carries_it_too():
    """`digest._node_line` — the Researcher's every-turn view, where `Best so far: node 0
    metric=0.0` came from. Bounded to the `triage_rationale` clause size, which is what makes it
    affordable here (`engine/signal_delivery.py::scored_eval_reason`)."""
    line = digest._node_line(_state(_SCORE_LINE).nodes[0])
    assert "invalid_results" in line
    assert len(digest.metric_account(_state(_SCORE_LINE).nodes[0], brief=True)) \
        <= digest.ACCOUNT_LINE_CHARS


# ------------------------------------------------------------------ what must NOT change

def test_a_healthy_evals_own_bookkeeping_is_not_an_account():
    """THE false positive, at real bytes. Every scored node on this arm prints a nested
    `subset_evidence.reason`; if that read as "why there is no number", all 56 rows would grow a
    why-clause and the clause would mean nothing. Absence must be absence."""
    n = _state(_HEALTHY_LINE, metric=1.1013).nodes[0]
    assert digest.metric_account(n) == ""
    assert " — why:" not in digest._node_line(n)
    assert "metric_account" not in _tools(_state(_HEALTHY_LINE, metric=1.1013)).execute(
        "read_experiment", {"node_id": 0})
    assert "patch_marker_present" not in digest._node_line(n)


def test_a_front_truncated_tail_yields_no_account_rather_than_a_wrong_one():
    """`stdout_tail` is a TAIL, so a long line can arrive cut at its front. A half-parsed block must
    render nothing — an account the eval did not give is worse than none."""
    n = _state(_SCORE_LINE[900:]).nodes[0]
    assert digest.metric_account(n) == ""


def test_the_account_is_bounded_on_both_surfaces():
    """A hostile eval must not be able to spend the working set. Both bounds hold on a block whose
    every field is 50 kB."""
    junk = "z" * 50_000
    line = json.dumps({"speedup": 0.0, "no_speedup": {"reason": junk, "evaluator_verdict": junk,
                                                      "is_solution_errors": [
                                                          {"message": junk, "count": 3}] * 40}})
    n = _state(line).nodes[0]
    assert len(digest.metric_account(n)) <= digest.ACCOUNT_CHARS
    assert len(digest.metric_account(n, brief=True)) <= digest.ACCOUNT_LINE_CHARS


def test_read_logs_still_returns_the_untouched_tail():
    """The pull channel this change did NOT replace: `read_logs` keeps rendering the whole recorded
    stream (`tests/test_scored_output_evidence.py` owns that contract). The account is a second,
    cheaper door onto the same record, not a narrowing of the first."""
    out = _tools(_state(_SCORE_LINE, stderr_tail="Failed evaluations: 1")).execute(
        "read_logs", {"node_id": 0})
    assert "Failed evaluations: 1" in out and "is_solution_errors" in out


# ------------------------------------------------------------------ record, not verdict

_DECISION_PATH = ("looplab/engine/triage.py", "looplab/engine/metric_salvage.py",
                  "looplab/engine/failure_diagnosis.py", "looplab/engine/repair_judgment.py",
                  "looplab/engine/crash_repair.py", "looplab/core/fitness.py",
                  "looplab/search/policy.py", "looplab/engine/evaluate.py")


def test_nothing_that_decides_reads_the_account():
    """Same rule as `stderr_tail`'s: text the candidate's own eval wrote may NOMINATE a reason to a
    reader and may never decide one. `metric_account` is a render helper and must stay off every
    path that chooses a number, classifies a failure, or spends money."""
    root = Path(inspect.getfile(digest)).resolve().parents[2]
    offenders = [rel for rel in _DECISION_PATH
                 if "metric_account" in (root / rel).read_text(encoding="utf-8")]
    assert offenders == [], f"a decision path is keying on eval-authored text: {offenders}"


def test_the_account_does_not_move_the_number():
    """The strongest form of the same claim, driven rather than grepped: a node whose eval declined
    to score is still `evaluated` with the same metric, and its feasibility is unchanged."""
    st = _state(_SCORE_LINE)
    before = (st.nodes[0].metric, st.nodes[0].status, len(st.feasible_nodes()))
    _tools(st).execute("read_experiment", {"node_id": 0})
    digest._node_line(st.nodes[0])
    assert (st.nodes[0].metric, st.nodes[0].status, len(st.feasible_nodes())) == before
