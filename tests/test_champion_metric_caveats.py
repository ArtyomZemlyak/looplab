"""`/api/runs`'s `best_metric` could BE a caveated number, portfolio-wide, and say nothing.

THE FINDING. `serve/run_projections.py::run_summaries` publishes `best_metric`/`best_confirmed` for
every run directory on the box and nothing else about the champion — no `violations`, no
`metric_provenance`, no node id — while `RunState.best()` reads `best_node_id`, which the fold
derives from whatever that run's own rungs were willing to crown. Two rungs crown a number the run
recorded a caveat about: `metric_salvage="select"` (which mints NO violation row, so a metric
RECOVERED from a failed eval is feasible and competes) and `trust_gate="audit"`, the SHIPPED DEFAULT
(which enforces nothing, so a hard reward-hack/leakage signal excludes nothing). Thirteen browser
surfaces read that row and not one can derive either fact, because neither is in the payload.

EVERY TEST BELOW DRIVES THE PROPERTY (guard-test tier 1): a real `events.jsonl` written with
`EventStore`, folded by the real `fold`, projected by the real `run_summaries` off a real
`make_app` AppState. In particular the salvage rows are asked of the SHIPPED RUNG
(`SalvagedMetric.violation_rows`) instead of being hand-written here — the whole finding is about
which rung mints a row, so a test that hand-copies the rows would go on passing after the rung moved.

MEASURED over the 46 preserved runs in `runs/` on 2026-08-15: 37 carry a `best_metric` and ZERO of
them are caveated today. The corpus holds one salvaged node (`rubertlite-dr-unified-v6` node 3,
producer `agent_stage`, which `violation_rows` keeps excluded under every rung but `off`) and one
hard trust flag (`task-g7` node 1, not its champion) — so this closes a reachable hole rather than
cleaning up a dirty corpus, and `test_a_measured_run_gains_no_label` is the negative control that
keeps it that way.
"""
from __future__ import annotations

import pytest

from looplab.engine.champion_caveats import (CHAMPION_CAVEAT_MIXED_COMPARABILITY,
                                             CHAMPION_CAVEAT_PARAMS_OVERRIDDEN,
                                             CHAMPION_CAVEAT_SALVAGED, CHAMPION_CAVEAT_TRUST_FLAGGED,
                                             CHAMPION_CAVEATS, champion_metric_caveats)
from looplab.engine.memory import unreliable_metric_ids
from looplab.engine.metric_salvage import OPERATOR_PRODUCED, SalvagedMetric
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

pytest.importorskip("fastapi")


# The record a salvage really writes, minted by the shipped rung rather than transcribed.
# `producer=OPERATOR_PRODUCED` is the half `select` is conditional on: an agent-produced salvage keeps
# its row under every rung but `off`, which is why the whole corpus flips nothing under `select`.
SALVAGE = SalvagedMetric(metric=0.81, condition="artifact_contract", source="declared_reader",
                         reader="stdout_regex", stage="train", producer=OPERATOR_PRODUCED)

# Two comparability records that are PROVABLY different at a certifying authority — the shape
# `engine/comparability.py::comparability_record` writes when a task declares `eval.inputs` and the
# corpus underneath it changed. Written as literals rather than bound from disk because this file is
# about the CAVEAT VOCABULARY; the binding itself is driven end-to-end in
# `tests/test_metric_comparability.py`.
_KEY_A = {"version": 1, "authority": "measured", "keys": {"measured": "aaaaaaaaaaaaaaaa"}}
_KEY_B = {"version": 1, "authority": "measured", "keys": {"measured": "bbbbbbbbbbbbbbbb"}}


def _run(tmp_path, name, *, nodes, trust_gate="audit", hacks=()):
    """A real run directory whose log folds to the state under test. Returns its `AppState`."""
    from looplab.serve.server import make_app
    rd = tmp_path / name
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "t", "goal": "g", "direction": "max",
                                 "trust_gate": trust_gate})
    for node in nodes:
        store.append("node_created", {
            "node_id": node["id"], "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": node.get("params") or {},
                     "rationale": "seed"},
            "code": "pass\n", "files": node.get("files") or {}})
        payload = {"node_id": node["id"], "generation": 0, "metric": node["metric"],
                   "violations": node.get("violations", [])}
        if node.get("provenance") is not None:
            payload["metric_provenance"] = node["provenance"]
        store.append("node_evaluated", payload)
    for node_id, signal in hacks:
        store.append("reward_hack_suspected", {"node_id": node_id, "generation": 0,
                                               "signals": [{"signal": signal}]})
    return make_app(tmp_path).state.looplab


def _row(srv, name):
    from looplab.serve import run_projections
    rows = {row["run_id"]: row for row in run_projections.run_summaries(srv)}
    assert name in rows, "precondition: the run is projected at all"
    return rows[name]


def _state(srv, name):
    return fold(EventStore(srv.root / name / "events.jsonl").read_all())


def test_a_select_admitted_salvage_reaches_the_portfolio_row(tmp_path):
    """THE LEAK. Under `metric_salvage="select"` over operator-produced bytes the rung mints no
    violation row at all, so the node is feasible, wins champion selection on a number nobody
    measured, and every one of the thirteen surfaces reading `/api/runs` printed it bare."""
    rows = SALVAGE.violation_rows("select")
    assert rows == [], ("precondition, from the rung itself: `select` over operator-produced output "
                        "mints no row — which is exactly why nothing on the selection path notices")

    srv = _run(tmp_path, "admitted", nodes=[
        {"id": 0, "metric": 0.51},
        {"id": 1, "metric": 0.81, "violations": rows, "provenance": SALVAGE.as_event()},
    ])
    state = _state(srv, "admitted")
    assert state.best_node_id == 1 and state.best().metric == 0.81, (
        "precondition: the salvaged node really is this run's champion")

    row = _row(srv, "admitted")
    assert row["best_metric"] == 0.81
    assert row["best_metric_caveats"] == [CHAMPION_CAVEAT_SALVAGED]

    # …and WHY it had to be a server field: nothing else on the row can produce that answer.
    for key in ("violations", "metric_provenance", "best_node_id", "reward_hacks", "nodes_detail"):
        assert key not in row, f"the summary row carries no {key}; a client cannot derive the caveat"


def test_the_audit_rung_leaves_the_row_uncaveated_because_it_leaves_the_node_unselected(tmp_path):
    """NEGATIVE CONTROL, and the one that proves the field is not simply reading
    `metric_provenance.salvaged`. Under the DEFAULT rung the same salvage mints a row, the fold's
    `feasible = not violations` excludes the node, and the champion is the measured 0.51 — so the
    honest answer about THIS row's number is no caveat at all."""
    rows = SALVAGE.violation_rows("audit")
    assert len(rows) == 1 and rows[0]["name"] == "metric_salvaged", "precondition: the rung minted it"

    srv = _run(tmp_path, "audited", nodes=[
        {"id": 0, "metric": 0.51},
        {"id": 1, "metric": 0.81, "violations": rows, "provenance": SALVAGE.as_event()},
    ])
    state = _state(srv, "audited")
    assert state.best_node_id == 0, "precondition: the salvaged node is excluded from selection"
    row = _row(srv, "audited")
    assert row["best_metric"] == 0.51 and row["best_metric_caveats"] == []


def test_a_measured_run_gains_no_label(tmp_path):
    """THE NEGATIVE CONTROL THE CHANGE IS MEASURED AGAINST, and its champion is shaped like the LIVE
    run's: `runs/rubertlite-dr-unified-v8` node 1 (0.738425) carries
    `metric_provenance = {"subject_bound": false, "unbound_reason": "not_declared", …}` because
    `metric_subject` RECORDS under its default rung without enforcing. 82 of 83 preserved corpus
    metrics are in that state, so a caveat there would fire on the rule instead of on a finding —
    and `ui/src/trustSemantics.js` already refuses to label it for the same reason. A naive
    implementation ("this node has a metric_provenance, so qualify it") turns every run on the box
    orange; this is the case that catches it."""
    v8_shaped = {"subject_bound": False, "unbound_reason": "not_declared", "subjects": []}
    srv = _run(tmp_path, "measured", nodes=[
        {"id": 0, "metric": 0.51, "provenance": v8_shaped},
        {"id": 1, "metric": 0.81, "provenance": v8_shaped},
    ])
    row = _row(srv, "measured")
    assert row["best_metric"] == 0.81
    assert row["best_metric_caveats"] == [], "a measured champion gains no label"

    # A DECLARATION-REPAIRED metric is measured too (the F1e re-check passed the artifact contract),
    # and the browser calls it `measured` with the detail in a tooltip. The portfolio row must not
    # disagree with the node tab about the same node.
    srv2 = _run(tmp_path, "repaired", nodes=[
        {"id": 0, "metric": 0.9,
         "provenance": {"salvaged": False, "declaration_repaired": True, "stage": "train"}}])
    assert _row(srv2, "repaired")["best_metric_caveats"] == []


def test_a_reward_hacked_champion_is_labelled_under_the_default_gate(tmp_path):
    """THE SAME LEAK ONE VIOLATION FAMILY OVER, and the one reachable WITHOUT changing a setting.
    `trust_gate="audit"` is the shipped default: `flagged_node_ids` is empty there, so a node with a
    high-precision cheating/leakage signal is excluded from nothing and can be the run's champion —
    with `hard_flagged_ids` (which is mode-INDEPENDENT) recording the signal all along."""
    srv = _run(tmp_path, "hacked", nodes=[{"id": 0, "metric": 0.51}, {"id": 1, "metric": 0.99}],
               trust_gate="audit", hacks=[(1, "protected_missing")])
    state = _state(srv, "hacked")
    assert state.best_node_id == 1, "precondition: audit enforces nothing, so the flagged node wins"
    row = _row(srv, "hacked")
    assert row["best_metric"] == 0.99
    assert row["best_metric_caveats"] == [CHAMPION_CAVEAT_TRUST_FLAGGED]


def test_an_enforcing_gate_needs_no_caveat_because_it_moved_the_champion(tmp_path):
    """NEGATIVE CONTROL for the trust half, and the reason the rule is a SUBTRACTION
    (`hard_flagged_ids` minus `flagged_node_ids`) rather than a membership test. Under `gate` the
    operator's own rung excluded the flagged node, the champion is somebody else, and a caveat here
    would be a warning about a number this row does not publish."""
    srv = _run(tmp_path, "gated", nodes=[{"id": 0, "metric": 0.51}, {"id": 1, "metric": 0.99}],
               trust_gate="gate", hacks=[(1, "protected_missing")])
    state = _state(srv, "gated")
    assert state.best_node_id == 0, "precondition: gate excluded the flagged node from selection"
    row = _row(srv, "gated")
    assert row["best_metric"] == 0.51 and row["best_metric_caveats"] == []


def test_an_advisory_signal_is_not_a_caveat(tmp_path):
    """`is_hard_signal` is the classifier, and it is deliberately not "anything recorded":
    `perfect_metric` flags the exact theoretical optimum, which a legitimately-perfect score hits.
    The caveat is spelled through `hard_flagged_ids` precisely so this stays out of it."""
    srv = _run(tmp_path, "advisory", nodes=[{"id": 0, "metric": 1.0}],
               trust_gate="audit", hacks=[(0, "perfect_metric")])
    assert _row(srv, "advisory")["best_metric_caveats"] == []


def test_every_caveat_rides_together_in_vocabulary_order(tmp_path):
    """They are independent facts about one number and none hides another. The ORDER is the
    vocabulary's, not discovery's, so an unchanged run cannot look changed to a client diffing the
    row between two polls — which is why this drives the WHOLE registry rather than a pair, and goes
    red the day a member is appended without a rule for where it sorts. (The fixture bytes are
    defined below with the third member; a node can be salvaged, flagged AND diverging at once.)"""
    srv = _run(tmp_path, "both", nodes=[
        {"id": 0, "metric": 0.81, "violations": SALVAGE.violation_rows("select"),
         "provenance": {**SALVAGE.as_event(), "comparability": _KEY_A}, "params": _V8_PARAMS,
         "files": {"vectorsearch/train.py": _V8_OVERRIDING_TRAIN_PY}},
        # The FOURTH member needs a second node, because it is the one caveat that is a claim about
        # the run's POPULATION rather than about the champion's own record: node 1 loses on metric
        # and was measured against different data, so the champion won a mixed field.
        {"id": 1, "metric": 0.50, "provenance": {"comparability": _KEY_B}}],
        trust_gate="audit", hacks=[(0, "protected_missing")])
    row = _row(srv, "both")
    assert row["best_metric_caveats"] == [CHAMPION_CAVEAT_SALVAGED, CHAMPION_CAVEAT_TRUST_FLAGGED,
                                          CHAMPION_CAVEAT_PARAMS_OVERRIDDEN,
                                          CHAMPION_CAVEAT_MIXED_COMPARABILITY]
    # THE REGISTRY IS THE ORDER, AND SINCE 2026-08-29 THAT IS A SUBSEQUENCE AND NOT AN EQUALITY.
    # This line read `list(CHAMPION_CAVEATS) == row[...]`, i.e. "every member fires at once", which
    # was true while all four were independent facts about one number. `merged_coordinates` is the
    # first member that CANNOT co-occur with another: it REPLACES `params_overridden` for a
    # merge-operator champion, because that slug's premise — a declaration the node's own code
    # contradicts — is absent when `merge_idea` derived the params and the node trained nothing.
    # Weakening the equality to a subsequence keeps both properties the test was written for (the
    # order is the vocabulary's, so an unchanged run cannot look changed to a client diffing the row
    # between two polls; and no caveat hides another), and the exclusivity it can no longer express
    # is driven directly by `tests/test_merged_champion_coordinates.py`.
    emitted = row["best_metric_caveats"]
    registry = list(CHAMPION_CAVEATS)
    assert [c for c in registry if c in emitted] == emitted, "the registry IS the order"
    assert set(emitted) < set(registry), "and every emitted slug is a registered one"


def test_the_champion_is_never_in_unreliable_metric_ids(tmp_path):
    """WHY THIS IS NOT `memory.unreliable_metric_ids`, driven rather than asserted in a docstring.

    That join is the obvious candidate — it already unites the salvage and trust families for the
    cross-run writers — and its intersection with `{best_node_id}` is EMPTY under every rung, as a
    theorem: `metric_unmeasured` needs a violation row, a violation row makes the node infeasible,
    and `SearchFitness.eligible` requires feasibility; `flagged_node_ids` is non-empty only under
    gate/block and `_select_best` passes exactly that set in as its exclusion. So a field stating
    that join would be a constant `false` on the one row it decorates. This drives all four rungs,
    INCLUDING the two where a caveat fires — that is the case which shows the two predicates are
    complementary halves of the same families rather than the same question asked twice."""
    cases = [
        ("t-admitted", dict(nodes=[{"id": 0, "metric": 0.51},
                                   {"id": 1, "metric": 0.81,
                                    "violations": SALVAGE.violation_rows("select"),
                                    "provenance": SALVAGE.as_event()}])),
        ("t-audited", dict(nodes=[{"id": 0, "metric": 0.51},
                                  {"id": 1, "metric": 0.81,
                                   "violations": SALVAGE.violation_rows("audit"),
                                   "provenance": SALVAGE.as_event()}])),
        ("t-hacked", dict(nodes=[{"id": 0, "metric": 0.51}, {"id": 1, "metric": 0.99}],
                          trust_gate="audit", hacks=[(1, "protected_missing")])),
        ("t-gated", dict(nodes=[{"id": 0, "metric": 0.51}, {"id": 1, "metric": 0.99}],
                         trust_gate="gate", hacks=[(1, "protected_missing")])),
    ]
    caveated = 0
    for name, spec in cases:
        srv = _run(tmp_path, name, **spec)
        state = _state(srv, name)
        best = state.best()
        assert best is not None, f"{name}: precondition, this run has a champion"
        assert best.id not in unreliable_metric_ids(state), (
            f"{name}: the knowledge-boundary join can never answer about a champion")
        caveated += bool(champion_metric_caveats(state))
    assert caveated == 2, ("two of the four rungs put a caveated number on the row and the join is "
                           "silent about both — which is the whole reason this is a second rule")


def test_the_rule_is_total_over_a_state_with_no_champion():
    """A run that has evaluated nothing, and a hand-assembled state whose provenance is junk. The
    projection runs on every directory in the workspace, so an exception here is a dead run list."""
    from looplab.core.models import Idea, Node, NodeStatus, RunState
    assert champion_metric_caveats(RunState(goal="g", direction="max")) == []
    st = RunState(goal="g", direction="max")
    st.nodes[0] = Node(id=0, operator="draft", status=NodeStatus.evaluated, metric=0.5,
                       idea=Idea(operator="draft"), metric_provenance=None)
    st.best_node_id = 0
    assert champion_metric_caveats(st) == []
    st.nodes[0].metric_provenance = {"salvaged": "yes-ish"}    # truthy junk, still a salvage claim
    assert champion_metric_caveats(st) == [CHAMPION_CAVEAT_SALVAGED]
    st.best_node_id = 99                                        # a champion id no node answers to
    assert champion_metric_caveats(st) == []


def test_the_caveat_vocabulary_is_the_one_the_browser_prints():
    """A cross-language registry check, the same shape `tests/test_metric_formatting.py` uses for
    `ui/src/format.js`. The browser cannot import Python, so the two spellings of this closed
    vocabulary are held together by this assertion and by nothing else — and a slug the server emits
    that the browser has no word for renders as the raw string rather than disappearing, which is the
    behaviour the second half pins."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "ui" / "src" / "runIndex.js").read_text("utf-8")
    for slug in CHAMPION_CAVEATS:
        assert f"= '{slug}'" in source, f"the browser has no constant for the server's {slug!r}"
    assert "best_metric_caveats" in source, "the browser must read the field by its shipped name"


# ================================================== the third member: what the number is a number FOR
# The two members above qualify HOW the champion's metric was measured. This one qualifies WHAT it is
# a measurement of, and unlike them it is NOT empty on this box: `runs/rubertlite-dr-unified-v8`
# node 3 is the run's champion at 0.762048 and its own `vectorsearch/train.py` sets
# `batch_size = 4096` / `gradient_accumulation_steps = 4` over an `idea.params` (and a `config.yaml`)
# declaring 8192 / 2. The real bytes are the fixture, reduced to what a fold needs.

_V8_PARAMS = {"loss.rdrop_alpha": 0.5, "train.training.batch_size": 8192.0,
              "train.training.gradient_accumulation_steps": 2.0,
              "train.training.n_epochs": 15.0, "train.training.learning_rate": 0.001}

# Verbatim from that node's repaired `train.py`, comment included — the comment is the SYSTEM half of
# the finding (the repair states it avoided `config.yaml` to keep the completed `mine` stage
# reusable, i.e. `_safe_reuse_start`'s non-`.py` clause naming itself as the reason).
_V8_OVERRIDING_TRAIN_PY = (
    "from vectorsearch.config import Config\n\n\n"
    "def main():\n"
    "    config = Config()\n"
    "    # Config is pydantic-mutable, so this is a train.py-only change (no config.yaml edit)\n"
    "    # that leaves the completed `mine` stage reusable.\n"
    "    config.train.training.batch_size = 4096\n"
    "    config.train.training.gradient_accumulation_steps = 4\n")

# The same file as node 3's SIBLINGS ship it: the identical declaration, read and never written.
_V8_AGREEING_TRAIN_PY = (
    "from vectorsearch.config import Config\n\n\n"
    "def main():\n"
    "    config = Config()\n"
    "    return config.train.training.batch_size\n")


def test_a_champion_whose_code_contradicts_its_declaration_reaches_the_portfolio_row(tmp_path):
    """THE FINDING, through the real projection. The number was measured normally — no salvage, no
    flag, nothing the other two members can see — and it is published at coordinates the experiment
    never occupied. `idea.params` is not decoration: it is what `core/numeric.py::numeric_params`
    hands the surrogate, the panel, the proxy, the archive's niches and the novelty distance, and
    what `search/operators.py::merge_idea` does ARITHMETIC on. On the live run that is not
    hypothetical — node 8 is a mean-merge of nodes 3 and 1 and inherited `8192`/`2` from a parent
    that ran `4096`/`4`, so it was minted at 8192/2 where the true parents' mean is 6144/3."""
    srv = _run(tmp_path, "overridden", nodes=[
        {"id": 1, "metric": 0.738425, "params": _V8_PARAMS,
         "files": {"vectorsearch/train.py": _V8_AGREEING_TRAIN_PY}},
        {"id": 3, "metric": 0.762048, "params": _V8_PARAMS,
         "files": {"vectorsearch/train.py": _V8_OVERRIDING_TRAIN_PY}},
    ])
    state = _state(srv, "overridden")
    assert state.best_node_id == 3, "precondition: the diverging node really is this run's champion"
    assert state.best().feasible and not state.best().violations, (
        "precondition: nothing else caveats this number — it was measured, unsalvaged and unflagged")

    row = _row(srv, "overridden")
    assert row["best_metric"] == 0.762048, "the METRIC does not move; only what is said about it"
    assert row["best_metric_caveats"] == [CHAMPION_CAVEAT_PARAMS_OVERRIDDEN]


def test_a_champion_whose_code_matches_its_declaration_gains_nothing(tmp_path):
    """THE NEGATIVE CONTROL, and a real one: node 3's siblings declare the IDENTICAL parameters and
    ship a `train.py` that only reads them. Measured over all 46 preserved logs, exactly ONE of 297
    nodes answers here — if the rule fired on the siblings it would fire on every node in the run
    and mean nothing, which is the failure mode `test_a_measured_run_gains_no_label` guards for the
    other two members."""
    srv = _run(tmp_path, "agreeing", nodes=[
        {"id": 1, "metric": 0.738425, "params": _V8_PARAMS,
         "files": {"vectorsearch/train.py": _V8_AGREEING_TRAIN_PY}},
        {"id": 3, "metric": 0.762048, "params": _V8_PARAMS,
         "files": {"vectorsearch/train.py": _V8_AGREEING_TRAIN_PY}},
    ])
    assert _row(srv, "agreeing")["best_metric_caveats"] == []
    # …and neither does a run whose space declares BARE names, which is every toy/benchmark run on
    # the box: `PARAM_OVERRIDE_MIN_PARTS` refuses to read a local variable as a declaration.
    srv2 = _run(tmp_path, "bare", nodes=[
        {"id": 0, "metric": 0.9, "params": {"lr": 0.1},
         "files": {"t.py": "def f():\n    lr = 0.5\n"}}])
    assert _row(srv2, "bare")["best_metric_caveats"] == []


def test_the_caveat_moves_no_champion_no_selection_and_no_violation(tmp_path):
    """THE BOUND, and it is the whole licence for this member existing at all: the diverging node
    stays feasible, stays selectable, keeps its metric and keeps its empty violation list. Nothing
    an agent wrote — the code IS agent-written — may move any of those four, and the only thing this
    rung does is add a sentence beside the number (docs/36). Replayed over all 46 preserved logs on
    2026-08-15, exactly one thing moved: v8's `best_metric_caveats` `[]` -> `['params_overridden']`,
    with every champion, every metric, every feasible set and every violation row unchanged."""
    srv = _run(tmp_path, "bounded", nodes=[
        {"id": 0, "metric": 0.70, "params": _V8_PARAMS,
         "files": {"vectorsearch/train.py": _V8_AGREEING_TRAIN_PY}},
        {"id": 1, "metric": 0.99, "params": _V8_PARAMS,
         "files": {"vectorsearch/train.py": _V8_OVERRIDING_TRAIN_PY}},
    ])
    state = _state(srv, "bounded")
    best = state.best()
    assert state.best_node_id == 1 and best.metric == 0.99
    assert best.feasible and best.violations == []
    assert 1 in {n.id for n in state.feasible_nodes()}
    assert champion_metric_caveats(state) == [CHAMPION_CAVEAT_PARAMS_OVERRIDDEN]
    # The caveat is a member of the closed vocabulary, so `bestMetricCaveatLabel` has a word for it
    # and no client renders it as an unknown slug.
    assert CHAMPION_CAVEAT_PARAMS_OVERRIDDEN in CHAMPION_CAVEATS
