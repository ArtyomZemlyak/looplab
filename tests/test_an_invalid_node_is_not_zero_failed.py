"""
# 2026-08-29, MERGE with master: the coordinate rendering changed from `fmt_params` to
# `core/param_carriers.py::node_params_brief`, which prints "(none recorded)" where the old
# spelling printed an empty dict. The three literals below follow it. What this file GUARDS is
# unchanged and still passes on top of the new rendering: an unscored zero carries the
# "— NOT SCORED (the eval refused to time it)" clause with the eval's own reason, a MEASURED
# zero carries nothing extra, and a healthy line is byte-identical to a plain champion line.
AN INVALID NODE IS NOT A HEALTHY NODE, AND THE HEADLINE SAID IT WAS (doc 53 §4a).

`digest.experiments_digest` is the always-on working set: it rides on every Researcher prompt and it
opens with one line of arithmetic — `Search so far — N experiment(s), M failed`. `M` counted
`NodeStatus.failed` and nothing else. A node whose solver was WRONG on some instances never reaches
that status: the arena refuses to TIME an invalid solver, so it prints `speedup: 0.0` beside a
`no_speedup` block, the engine records a perfectly ordinary evaluated node with a real metric of
0.0, and the headline folded it into the healthy total.

THE MEASUREMENT (`/var/tmp/looplab-bench/runs-B`, 20 finished task-arms, re-derived 2026-08-26)
--------------------------------------------------------------------------------------------------
56 `node_evaluated` rows; **9 carry a `no_<metric key>` block**, spread over **6 of the 20 arms**
(`max_clique_cpsat` ×2, `sparse_eigenvectors_complex` ×3, `pde_heat1d`, `rbf_interpolation`,
`rectanglepacking`, `spectral_clustering`). **`node_failed` fires ZERO times in the entire corpus**,
so the count this line printed was `0 failed` on all twenty arms and was WRONG on six of them.
Counting the spans whose prompt carried the headline AFTER the first invalid node landed on that
arm: **61 renders of a literal "0 failed" over a board that held an invalid experiment.**
`spectral_clustering` is the specimen — one experiment, 98/100 valid, two named hack-detector
messages — and it was announced as `1 experiment(s), 0 failed` eleven times.

WHAT THIS DOES AND DOES NOT CHANGE
--------------------------------------------------------------------------------------------------
It makes the COUNT honest and stops there. The node keeps its status, its metric and its
feasibility: it did not fail, nothing crashed, and calling it `failed` would be a second lie in the
opposite direction (and would silently move `strategist.failure_rate`, which is a real decision path
— `ctx.failure_rate > 0.4` narrows the search to greedy). So the headline gains a THIRD count next
to the other two, and only when it is non-zero, which is why every existing golden headline is
byte-identical.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

from looplab.agents.strategist import failure_rate
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.events import digest
from looplab.serve.report import _report_context

# The bytes of `runs-B/spectral_clustering/run/nodes/node_0/score.log`, unedited — the same fixture
# `tests/test_metric_account_on_the_default_read_path.py` reads, and the record §4a is about.
_SCORE_LINE = (Path(__file__).parent / "fixtures"
               / "algotune_score_line_invalid_results.txt").read_text(encoding="utf-8")

# What a HEALTHY node on that arm printed (`runs-B/convex_hull` node 0). It carries a nested
# `reason` of its own, which is the false positive the `no_` prefix rule refuses.
_HEALTHY_LINE = ('{"speedup": 1.1013, "eval_seconds": 87.3, "subset": "train", "subset_evidence": '
                 '{"asked": "train", "verified": true, "reason": "patch_marker_present", "marker": '
                 '"# --- LOOPLAB EVAL SUBSET (benchmarks/algotune/patch_eval_subset.py) ---"}, '
                 '"baseline_source": "in-harness (record exposes no baseline_time_ms to cache)"}\n')


def _node(nid: int, *, tail: str = "", metric=0.0, status=NodeStatus.evaluated) -> Node:
    return Node(id=nid, operator="draft", idea=Idea(operator="draft", params={}),
                metric=metric, status=status, stdout_tail=tail)


def _state(*nodes: Node) -> RunState:
    st = RunState(direction="max", goal="make the solver fast and correct")
    for n in nodes:
        st.nodes[n.id] = n
    return st


# ------------------------------------------------------------------ the specimen, end to end

def test_spectral_clusterings_only_experiment_is_not_announced_as_zero_failed():
    """The exact line the run was steered by, over the exact record it was steered from."""
    out = digest.experiments_digest(_state(_node(0, tail=_SCORE_LINE)))
    head = out.splitlines()[1]
    assert "1 experiment(s), 0 failed:" not in head, head
    assert "1 invalid" in head, head


def test_the_count_is_of_invalid_nodes_not_of_zeros():
    """A measured 0.0 with no `no_` block is a real result and stays out of the count — otherwise
    the headline would relabel every honest zero as a defect."""
    st = _state(_node(0, tail=_SCORE_LINE), _node(1, tail=_HEALTHY_LINE, metric=0.0),
                _node(2, tail="", metric=0.0))
    head = digest.experiments_digest(st).splitlines()[1]
    assert "3 experiment(s), 0 failed, 1 invalid" in head, head


def test_a_healthy_run_renders_the_historical_headline_byte_for_byte():
    """The clause appears only where it is true; nothing else in the corpus's wording moves."""
    st = _state(_node(0, tail=_HEALTHY_LINE, metric=1.1013))
    assert digest.experiments_digest(st).splitlines()[1] == "Search so far — 1 experiment(s), 0 failed:"


def test_the_two_counts_partition():
    """A node that FAILED has a taxonomy entry already; charging it to both counts would inflate the
    line past the number of experiments it claims to describe."""
    failed = _node(9, tail=_SCORE_LINE, metric=None, status=NodeStatus.failed)
    failed.error_reason = "crash"
    st = _state(_node(0, tail=_SCORE_LINE), failed)
    head = digest.experiments_digest(st).splitlines()[1]
    assert "2 experiment(s), 1 failed, 1 invalid" in head, head


def test_the_run_report_brief_carries_it_too():
    """The other surface that counts outcomes for a model — the conclusion-first report's own
    `Nodes: …` line, which fed the final write-up the same `0 failed`."""
    line = [ln for ln in _report_context(_state(_node(0, tail=_SCORE_LINE))).splitlines()
            if ln.startswith("Nodes:")][0]
    assert "0 failed," in line and "INVALID" in line, line


# ------------------------------------------------------------------ record, not verdict

def test_the_count_does_not_move_the_number_or_the_taxonomy():
    """Honest arithmetic, not a reclassification: the node is still evaluated, still feasible, still
    0.0 — and `strategist.failure_rate`, the one signal a rule ACTS on, is unchanged."""
    st = _state(_node(0, tail=_SCORE_LINE))
    before = (st.nodes[0].metric, st.nodes[0].status, len(st.feasible_nodes()), failure_rate(st))
    digest.experiments_digest(st)
    assert (st.nodes[0].metric, st.nodes[0].status,
            len(st.feasible_nodes()), failure_rate(st)) == before
    assert failure_rate(st) == 0.0


_DECISION_PATH = ("looplab/engine/triage.py", "looplab/engine/metric_salvage.py",
                  "looplab/engine/failure_diagnosis.py", "looplab/engine/repair_judgment.py",
                  "looplab/engine/crash_repair.py", "looplab/core/fitness.py",
                  "looplab/search/policy.py", "looplab/engine/evaluate.py",
                  "looplab/agents/strategist.py")


def test_nothing_that_decides_reads_the_predicate():
    """Same rule as `metric_account`'s and one file longer: `strategist.py` is on the list because
    `failure_rate` is read by `ctx.failure_rate > 0.4`, so wiring this predicate into it would let
    text the candidate's own eval wrote choose the search policy."""
    root = Path(inspect.getfile(digest)).resolve().parents[2]
    offenders = [rel for rel in _DECISION_PATH
                 if "metric_scored_invalid" in (root / rel).read_text(encoding="utf-8")]
    assert offenders == [], f"a decision path is keying on eval-authored text: {offenders}"


def test_a_hostile_eval_cannot_spend_the_headline():
    """The count is an int; the block that produced it is never rendered on this line."""
    junk = "z" * 50_000
    line = json.dumps({"speedup": 0.0, "no_speedup": {"reason": junk, "evaluator_verdict": junk}})
    head = digest.experiments_digest(_state(_node(0, tail=line))).splitlines()[1]
    assert len(head) < 200 and "zzzz" not in head, head


# ------------------------------------------------------------------ the champion line (§4a, half 2)
#
# The headline count above was the FIRST of the three contradictions §4a measured. The SECOND is the
# line directly above it in every proposal prompt: `agents/roles.py::_state_brief` opens with
# `Best so far: node N metric=<x>`, and `<x>` was a bare `0.0` whether the run measured a genuine
# zero or the arena refused to time the solver at all.
#
# MEASURED over the thirty run dirs (runs-B + model-probes + fullctx-probe, crash-atomic
# `__looplab_event_batch_v1__` packets expanded): 340 renders of that line, **16 of them naming a
# node whose own eval had refused to score it** -- 9 on `spectral_clustering` (i.e. ALL of them: the
# arm never proposed anything under a different champion), 3 on `rectanglepacking`, 2 each on the
# `gpt56luna` and `sol1` probes. The literal bytes in `spectral_clustering`'s `spans.jsonl` are
# `Best so far: node 0 metric=0.0 params={}`, over a `score.log` that says 98/100 valid.

def _brief(state, parent=None) -> str:
    from looplab.agents.roles import _state_brief
    return _state_brief(state, parent)


def _champion_state(tail: str, metric=0.0) -> RunState:
    st = _state(_node(0, tail=tail, metric=metric))
    st.best_node_id = 0
    return st


def test_the_champion_line_says_the_number_is_not_a_score():
    """`spectral_clustering`'s nine proposal prompts, over the record they were written from."""
    line = [ln for ln in _brief(_champion_state(_SCORE_LINE)).splitlines()
            if ln.startswith("Best so far:")][0]
    assert line.startswith("Best so far: node 0 metric=0.0 params=(none recorded)"), line
    assert digest.UNSCORED_LABEL in line, line
    # The eval's OWN verdict, not a label we invented for it: the arm read 0.0 as "the idea is
    # wrong" while its record said "the idea is right and two answers were rejected".
    assert "98/100 valid" in line, line


def test_the_parent_line_carries_it_too():
    """The same untruth from the parent's side. It names an invalid node ZERO times in the corpus
    (108 `Refine from node` renders, none of them invalid) and gets the clause anyway, because it is
    the same builder reading the same predicate -- a fix that covers only the line that happened to
    fire is a fix that reopens on the next corpus."""
    st = _champion_state(_SCORE_LINE)
    line = [ln for ln in _brief(st, st.nodes[0]).splitlines()
            if ln.startswith("Refine from node")][0]
    assert digest.UNSCORED_LABEL in line, line


def test_a_measured_zero_is_still_just_a_zero():
    """The falsifier for "render the clause whenever the metric is 0.0". A healthy node that really
    scored zero is a RESULT; relabelling it would be the same defect pointed the other way, and it
    would fire on 47 of the corpus's 56 evaluated rows instead of 9."""
    st = _champion_state(_HEALTHY_LINE, metric=0.0)
    line = [ln for ln in _brief(st).splitlines() if ln.startswith("Best so far:")][0]
    assert line == "Best so far: node 0 metric=0.0 params=(none recorded)", line


def test_a_healthy_champion_line_is_byte_identical():
    """Nothing moves where the clause is not true -- the prompt is a contract."""
    st = _champion_state(_HEALTHY_LINE, metric=1.1013)
    line = [ln for ln in _brief(st).splitlines() if ln.startswith("Best so far:")][0]
    assert line == "Best so far: node 0 metric=1.1013 params=(none recorded)", line


def test_the_clause_is_render_only_and_the_champion_does_not_move():
    """`state.best()` returns the same node it returned before, with the same metric and status: the
    prompt gets a truer sentence, the search gets no new rule."""
    st = _champion_state(_SCORE_LINE)
    before = (st.best().id, st.best().metric, st.best().status, st.best_node_id)
    _brief(st, st.nodes[0])
    assert (st.best().id, st.best().metric, st.best().status, st.best_node_id) == before


def test_a_hostile_eval_cannot_spend_the_prompt():
    """The clause rides a line that already carries the params dict, so it takes the BRIEF account
    (ACCOUNT_LINE_CHARS), not the 600-char block `read_experiment` renders."""
    junk = "z" * 50_000
    tail = json.dumps({"speedup": 0.0, "no_speedup": {"reason": junk, "evaluator_verdict": junk}})
    line = [ln for ln in _brief(_champion_state(tail)).splitlines()
            if ln.startswith("Best so far:")][0]
    assert len(line) < 200, len(line)


def test_both_renders_of_the_one_fact_use_the_one_vocabulary():
    """The headline and the champion line are two renders of a single fact. They are built in
    different packages (`events` and `agents`), which is exactly how two names for one thing get
    into one prompt -- so the label is a module constant and this asserts the prompt agrees with
    itself."""
    out = _brief(_champion_state(_SCORE_LINE))
    head = [ln for ln in out.splitlines() if ln.startswith("Search so far")][0]
    champ = [ln for ln in out.splitlines() if ln.startswith("Best so far:")][0]
    assert "1 invalid" in head and "refused to time it" in head, head
    assert "refused to time it" in champ, champ
