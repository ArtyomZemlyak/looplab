"""The feature-engineering CV rule as a GATE, not a sentence (docs/BACKLOG.md §0.1 row 13).

`Settings.feature_engineering` put "KEEP a feature only if it improves CV; drop any that don't" in
the proposal prompt, `search/operators.py` had no FE operator, and nothing anywhere read any CV
evidence — the row that asked for it called the CV gate MANDATORY, and an instruction to a model is
not a gate. What ships is three pieces and this file drives each of them:

* the OPERATOR's rule — `search/operators.py::parse_feature_cv` + `feature_engineering_verdicts`,
  the keep/drop decision over the candidate's own declared per-feature ledger, using this repo's own
  >1-SE acceptance test when the ledger declares a spread;
* the deterministic RUNG — `trust/cv.py::feature_cv_findings`, a `findings.py`-shaped, already
  namespaced finding for a feature the ledger fails and the code still builds;
* and the WIRING, end to end: a real run with the flag on records the finding in the durable ledger,
  a run with the flag off records nothing, and under `trust_gate="gate"` the flagged node loses the
  championship it would otherwise have won. Whether a flag CHANGES selection is `trust_gate`'s
  decision and no part of `looplab/trust/` — so both halves are asserted separately.
"""
from __future__ import annotations

import anyio

from factories import make_engine
from looplab.agents.roles import ToyObjectiveDeveloper
from looplab.core.models import Event, developer_artifact_footprint
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold, is_hard_signal
from looplab.search.operators import feature_engineering_verdicts, parse_feature_cv
from looplab.trust.cv import feature_cv_findings, feature_cv_verdicts, feature_is_kept


# ------------------------------------------------------------------- the ledger the operator reads

def test_the_ledger_is_read_off_the_marker_and_junk_rows_are_dropped():
    rows = parse_feature_cv("\n".join([
        "training…",
        'FEATURE_CV {"feature": "ratio_ab", "with": 0.30, "without": 0.40, "std": 0.02, "n": 5}',
        'FEATURE_CV {"feature": "ratio_ab", "with": 9.9, "without": 0.0}',   # duplicate name
        'FEATURE_CV not json at all',
        'FEATURE_CV {"feature": "", "with": 1.0, "without": 2.0}',           # no name
        'FEATURE_CV {"feature": "no_pair", "with": 1.0}',                    # half a comparison
        'FEATURE_CV {"feature": "nan", "with": null, "without": 2.0}',
        '{"feature": "unmarked", "with": 0.1, "without": 0.2}',              # not a ledger row
        'FEATURE_CV {"feature": "noise_x", "with": 0.45, "without": 0.40}',
    ]))
    assert [r["feature"] for r in rows] == ["ratio_ab", "noise_x"]
    assert rows[0] == {"feature": "ratio_ab", "with": 0.30, "without": 0.40, "std": 0.02, "n": 5}
    assert rows[1]["std"] == 0.0 and rows[1]["n"] == 0, "an undeclared spread is 0, never guessed"


def test_a_feature_is_kept_only_when_it_improves_cv_in_the_run_s_direction():
    rows = [{"feature": "helps", "with": 0.30, "without": 0.40, "std": 0.0, "n": 0},
            {"feature": "hurts", "with": 0.45, "without": 0.40, "std": 0.0, "n": 0}]
    verdicts = {v["feature"]: v for v in feature_engineering_verdicts(rows, "min")}
    assert verdicts["helps"]["keep"] and verdicts["helps"]["rule"] == "strict"
    assert not verdicts["hurts"]["keep"] and verdicts["hurts"]["delta"] < 0
    # The SAME numbers under a maximizing run swap places — the rule is direction-aware, and a
    # gate that read "with > without" would flag every honest feature of a loss-minimizing task.
    flipped = {v["feature"]: v["keep"] for v in feature_engineering_verdicts(rows, "max")}
    assert flipped == {"helps": False, "hurts": True}


def test_a_declared_spread_moves_the_decision_to_the_one_se_rule():
    """An improvement smaller than the noise of the folds it was measured on is seed luck, which is
    exactly what `trust/gate.py::one_se_better` exists to refuse — and why the FE directive calls
    feature engineering non-universal."""
    noisy = [{"feature": "marginal", "with": 0.39, "without": 0.40, "std": 0.05, "n": 5}]
    verdict = feature_engineering_verdicts(noisy, "min")[0]
    assert verdict["delta"] > 0, "it did improve the mean…"
    assert verdict["rule"] == "one_se" and not verdict["keep"], "…by less than one SE"
    # …and the same feature with a tight spread is kept, so the rule is not simply refusing noise.
    tight = [{"feature": "marginal", "with": 0.39, "without": 0.40, "std": 0.001, "n": 5}]
    assert feature_engineering_verdicts(tight, "min")[0]["keep"]


# ---------------------------------------------------------------------------- kept-in-code

def test_a_feature_the_code_still_builds_counts_as_kept():
    code = 'df["ratio_ab"] = df.a / df.b\n'
    assert feature_is_kept(code, "ratio_ab")
    assert not feature_is_kept(code, "never_mentioned")
    assert not feature_is_kept(code, "")


def test_a_feature_only_mentioned_on_a_dropping_line_counts_as_dropped():
    """The gate is asking candidates to DROP failing features; reading the drop itself as the
    violation would punish the behaviour it wants."""
    assert not feature_is_kept('X = X.drop(columns=["noise_x"])\n', "noise_x")
    assert not feature_is_kept('del feats["noise_x"]\n', "noise_x")
    # …but a feature that is built AND mentioned on a dropping line is still built.
    assert feature_is_kept('feats["noise_x"] = z\nother = feats.drop(columns=["noise_x"])\n',
                           "noise_x")


# ------------------------------------------------------------------------------ the rung

_LEDGER = ('FEATURE_CV {"feature": "ratio_ab", "with": 0.30, "without": 0.40}\n'
           'FEATURE_CV {"feature": "noise_x", "with": 0.45, "without": 0.40}\n')
_CODE = 'df["ratio_ab"] = df.a / df.b\ndf["noise_x"] = rng.normal(size=len(df))\n'


def test_only_the_failing_feature_that_is_still_built_is_flagged():
    verdicts = {v["feature"]: v for v in feature_cv_verdicts(_CODE, _LEDGER, "min")}
    assert verdicts["ratio_ab"]["keep"] and verdicts["noise_x"]["kept_in_code"]
    findings = feature_cv_findings(_CODE, _LEDGER, "min")
    assert [f["signal"] for f in findings] == ["feature_cv:kept_feature_failed_cv"]
    assert "noise_x" in findings[0]["detail"] and "without=0.4" in findings[0]["detail"], (
        "a verdict has to quote the numbers it was reached from")
    assert set(findings[0]) == {"signal", "detail", "method", "confidence"}


def test_dropping_the_failing_feature_clears_the_gate():
    dropped = _CODE.replace('df["noise_x"] = rng.normal(size=len(df))',
                            'df = df.drop(columns=["noise_x"])')
    assert feature_cv_findings(dropped, _LEDGER, "min") == []


def test_a_node_that_declared_no_ledger_is_not_flagged():
    """The stated recall gap, in the safe direction: nothing was claimed, so nothing is contradicted.
    Inferring feature construction from an AST and hard-gating on the inference would flag every
    honest solution that assigns a column."""
    assert feature_cv_findings(_CODE, "no ledger here\n", "min") == []
    assert feature_cv_findings("", "", "min") == []


def test_the_namespace_is_a_hard_signal():
    """`is_hard_signal` is what decides whether a flag can gate at all, and an unknown namespace is
    hard by design (fail closed toward catching cheating). Asserted rather than assumed, because the
    whole point of this rung is that it can change selection under `trust_gate`."""
    assert is_hard_signal("feature_cv:kept_feature_failed_cv")


# --------------------------------------------------------- and the gate reaches selection

def _mk(evs):
    return [Event(type=t, data=d) for t, d in evs]


def _run_with_a_flagged_leader(gate_events):
    return _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "min",
                         "trust_gate": "audit"}),
        ("node_created", {"node_id": 1, "operator": "draft", "idea": {"operator": "draft"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.1}),
        ("reward_hack_suspected", {"node_id": 1, "signals": feature_cv_findings(
            _CODE, _LEDGER, "min")}),
        ("node_created", {"node_id": 2, "operator": "draft", "idea": {"operator": "draft"}}),
        ("node_evaluated", {"node_id": 2, "metric": 0.5}),
        *gate_events,
    ])


def test_under_gate_the_flagged_node_loses_the_championship_it_would_have_won():
    """The enforcement, and its boundary: the default `audit` still elects the better number and
    only SURFACES the finding. Whether a flag changes selection is `Settings.trust_gate`, never
    `looplab/trust/`."""
    assert fold(_run_with_a_flagged_leader([])).best_node_id == 1
    gated = fold(_run_with_a_flagged_leader([("trust_gate_changed", {"trust_gate": "gate"})]))
    assert gated.best_node_id == 2, "the CV gate is advisory even under trust_gate=gate"


# ------------------------------------------------------------------- end to end, through a run

_FE_SOLUTION = '''import json
# The candidate's own per-feature CV ledger, exactly as the FE directive asks it to report.
print("FEATURE_CV " + json.dumps({"feature": "ratio_ab", "with": 0.30, "without": 0.40}))
print("FEATURE_CV " + json.dumps({"feature": "noise_x", "with": 0.45, "without": 0.40}))
ratio_ab = 1.0     # …and both features are still built, which is the violation
noise_x = 2.0
print(json.dumps({"metric": 0.5}))
'''


class _FeatureEngineeringDeveloper(ToyObjectiveDeveloper):
    """The toy Developer, but every node it writes keeps a feature its own ledger fails."""

    def implement(self, idea):
        self.last_footprint = developer_artifact_footprint(idea.footprint, _FE_SOLUTION)
        return _FE_SOLUTION


def _signals(run_dir) -> list[dict]:
    return [signal
            for event in EventStore(run_dir / "events.jsonl").read_all()
            if event.type == "reward_hack_suspected"
            for signal in (event.data.get("signals") or [])]


def test_a_real_run_records_the_finding_in_the_durable_ledger(tmp_path):
    """THE wiring assertion: computed findings that reach no event are a run that reports clean
    because nothing looked. Driven through a real sandboxed evaluation."""
    run_dir = tmp_path / "fe-on"
    engine = make_engine(run_dir, developer=_FeatureEngineeringDeveloper(), n_seeds=1, max_nodes=1,
                         feature_engineering=True)
    state = anyio.run(engine.run)
    assert state.finished
    flagged = [s for s in _signals(run_dir)
               if str(s.get("signal", "")).startswith("feature_cv:")]
    assert flagged, "the FE CV gate ran and its findings went nowhere"
    assert flagged[0]["signal"] == "feature_cv:kept_feature_failed_cv"
    assert "noise_x" in flagged[0]["detail"] and "ratio_ab" not in flagged[0]["detail"]
    # …and the receipt says this detector looked, so a clean scan cannot be confused with no scan.
    receipts = [e.data for e in EventStore(run_dir / "events.jsonl").read_all()
                if e.type == "trust_scan"]
    assert receipts and "feature_cv" in receipts[0]["detectors"]


def test_the_same_run_with_the_flag_off_says_nothing_at_all(tmp_path):
    """The flag that puts the directive in the prompt is the flag that enforces it: a run that never
    asked for engineered features has no ledger to read, and must not report that it looked."""
    run_dir = tmp_path / "fe-off"
    engine = make_engine(run_dir, developer=_FeatureEngineeringDeveloper(), n_seeds=1, max_nodes=1)
    state = anyio.run(engine.run)
    assert state.finished
    assert [s for s in _signals(run_dir)
            if str(s.get("signal", "")).startswith("feature_cv:")] == []
    receipts = [e.data for e in EventStore(run_dir / "events.jsonl").read_all()
                if e.type == "trust_scan"]
    assert receipts and "feature_cv" not in receipts[0]["detectors"]
