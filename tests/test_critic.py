"""C4 independent critic (execution-free)."""
from __future__ import annotations

from looplab.trust.critic import critique
from looplab.core.models import Idea


def test_flags_stub():
    issues = critique(Idea(operator="draft"), "pass")
    assert any(i["issue"] == "stub" for i in issues)


def test_flags_hardcoded_metric():
    code = "import json\nprint(json.dumps({'metric': 0.0}))\n"
    issues = critique(Idea(operator="draft"), code)
    assert any(i["issue"] == "hardcoded_metric" for i in issues)


def test_flags_params_ignored():
    code = "import json\nx = 1\nprint(json.dumps({'metric': compute(x)}))\n"
    issues = critique(Idea(operator="improve", params={"degree": 3.0, "lam": 0.1}), code)
    assert any(i["issue"] == "params_ignored" for i in issues)


def test_clean_solution_no_issues():
    code = ("import json\n"
            "degree = 3\nlam = 0.1\n"
            "score = train_and_cv(degree, lam)\n"
            "print(json.dumps({'metric': score}))\n")
    issues = critique(Idea(operator="improve", params={"degree": 3.0, "lam": 0.1}), code)
    assert issues == []


def test_toy_template_passes_clean():
    # the real toy/regression templates reference their params and compute the metric -> clean.
    code = ("import json\nx = 3.0\ny = -1.0\nloss = (x - 3.0)**2 + (y + 1.0)**2\n"
            "print(json.dumps({'metric': loss}))\n")
    assert critique(Idea(operator="draft", params={"x": 3.0, "y": -1.0}), code) == []


# --- host-graded (MLE-bench &c.): score comes from a submission file, not an in-code metric ------

def test_host_graded_skips_metric_check():
    # A real MLE-bench solution writes submission.csv and never says "metric" — clean when the
    # critic knows the run is host-graded (was a false-positive `no_metric_output`).
    code = ("import pandas as pd\nsub = pd.read_csv('sample_submission.csv')\n"
            "sub.to_csv('./submission.csv', index=False)\n")
    issues = critique(Idea(operator="draft"), code, submission_file="submission.csv")
    assert issues == []


def test_host_graded_flags_missing_submission():
    # Host-graded but the code never writes the submission file -> the grader has nothing to score.
    code = "import pandas as pd\ndf = pd.read_csv('train.csv')\nx = df.mean()\n"
    issues = critique(Idea(operator="draft"), code, submission_file="submission.csv")
    assert any(i["issue"] == "no_submission_output" for i in issues)
    assert not any(i["issue"] == "no_metric_output" for i in issues)


def test_host_graded_sample_submission_read_is_not_a_write():
    # Reading `sample_submission.csv` (every solution does) must NOT satisfy the submission-output
    # check — it contains "submission.csv" as a substring but never writes the graded file. A bare
    # substring test would make `no_submission_output` dead; token-boundary matching keeps it alive.
    code = ("import pandas as pd\nsub = pd.read_csv('sample_submission.csv')\n"
            "print(sub.shape)  # builds nothing, writes nothing\n")
    issues = critique(Idea(operator="draft"), code, submission_file="submission.csv")
    assert any(i["issue"] == "no_submission_output" for i in issues)


def test_host_graded_sample_read_plus_real_write_is_clean():
    # The common shape — read sample_submission.csv for the format, then write submission.csv.
    code = ("import pandas as pd\nsub = pd.read_csv('sample_submission.csv')\n"
            "sub.to_csv('submission.csv', index=False)\n")
    assert critique(Idea(operator="draft"), code, submission_file="submission.csv") == []


def test_host_graded_basename_match():
    # The expected path may be './submission.csv' or absolute; matching is on the basename.
    code = "open('./submission.csv','w').write('id,t\\n1,0\\n')\n# enough body to clear stub gate\n"
    assert critique(Idea(operator="draft"), code,
                    submission_file="/data/run/submission.csv") == []


def test_debug_operator_skips_params_ignored():
    # An agent-proposed `debug` node (verify CUDA / GRU forward) whose codegen produced a normal
    # sklearn submission: its params are diagnostic toggles, not modeling knobs, so `params_ignored`
    # must NOT fire (mirrors node #114). Host-graded, so no metric check either -> clean.
    code = ("import pandas as pd\nfrom sklearn.ensemble import GradientBoostingRegressor\n"
            "sub = pd.read_csv('sample_submission.csv')\nsub.to_csv('./submission.csv', index=False)\n")
    idea = Idea(operator="debug",
                params={"verify_cuda": 1.0, "test_gru_forward": 1.0, "gru_hidden": 24.0,
                        "gru_layers": 1.0, "benchmark_iterations": 100.0})
    assert critique(idea, code, submission_file="submission.csv") == []


def test_improve_params_still_checked_when_host_graded():
    # Host-grading relaxes the metric checks but NOT params_ignored for modeling operators.
    code = ("import pandas as pd\nsub = pd.read_csv('sample_submission.csv')\n"
            "sub.to_csv('./submission.csv', index=False)\n")
    issues = critique(Idea(operator="improve", params={"n_estimators": 700.0, "max_depth": 5.0}),
                      code, submission_file="submission.csv")
    assert any(i["issue"] == "params_ignored" for i in issues)


# --- critic: a computed metric must not be flagged as hard-coded ----------------------------------

def test_critic_no_false_positive_on_computed_metric():
    idea = Idea(operator="draft", params={"k": 3.0}, rationale="x")
    computed = ("k = 3\nscore = sum(range(k)) / k\n"
                "import json; print(json.dumps({'metric': score}))")
    assert not any(i["issue"] == "hardcoded_metric" for i in critique(idea, computed))
    # A genuinely hard-coded literal with no computation is still flagged.
    hard = "import json\nprint(json.dumps({'metric': 0.99}))\nk = 3\n"
    assert any(i["issue"] == "hardcoded_metric" for i in critique(idea, hard))


def test_hardcoded_metric_not_masked_by_a_symmetric_substring():
    # Regression: the `computed` guard matched `metric` as a SUFFIX of any identifier, so a throwaway
    # line whose name ends in "metric" (`is_symmetric = True`, `asymmetric`, `parametric`, ...) made
    # `metric = T` read as a computed assignment — `hardcoded and not computed` went False and the
    # hard-coded-metric cheat slipped the HARD `critic:hardcoded_metric` gate under trust_gate=gate/block.
    idea = Idea(operator="draft", rationale="x")
    for filler in ("is_symmetric = True", "asymmetric = 1", "parametric = fn()", "barometric = p"):
        cheat = f"import json\n{filler}\nprint(json.dumps({{'metric': 0.999}}))\n"
        assert any(i["issue"] == "hardcoded_metric" for i in critique(idea, cheat)), filler
    # A genuinely computed metric alongside such a name is still NOT flagged (no false positive).
    ok = ("import json\nis_symmetric = True\nscore = compute()\n"
          "print(json.dumps({'metric': score}))\n")
    assert not any(i["issue"] == "hardcoded_metric" for i in critique(idea, ok))


def test_a_dotted_param_set_in_a_NESTED_CONFIG_is_not_reported_as_ignored():
    """MEASURED on rubertlite-dr-unified-v6: `critic:params_ignored` fired on nodes 2, 3, 4 and 6 —
    every node the check ran on — while each node's own `config.yaml` set every one of those
    parameters under its nested key. A repo task's params are FLATTENED dotted paths and the file
    that sets them is a hierarchy, so the literal string `train.training.learning_rate` appears
    nowhere and never will.

    The surface below is the real shape: the exact param names node 6 proposed, against the exact
    file it wrote them into."""
    params = {"loss.temperature": 0.05, "loss.uniformity_weight": 0.0,
              "train.training.learning_rate": 0.001, "train.training.batch_size": 8192.0}
    config = ("loss:\n"
              "  type: nll_cos\n"
              "  temperature: 0.05\n"
              "  uniformity_weight: 0.0\n"
              "train:\n"
              "  training:\n"
              "    learning_rate: 0.001\n"
              "    batch_size: 8192\n")
    issues = critique(Idea(operator="improve", params=params), config)
    assert not any(i["issue"] == "params_ignored" for i in issues), (
        "a node that set every proposed parameter was reported as ignoring all of them")


def test_a_surface_that_mentions_no_parameter_at_all_is_still_reported():
    """The control, and the reason the leaf rule is only just weaker than the dotted one: the check
    fires only when NOTHING matches, so a solution that ignored the proposal still trips it. A no-op
    does not accidentally contain `uniformity_weight`."""
    params = {"loss.temperature": 0.05, "loss.uniformity_weight": 0.0,
              "train.training.learning_rate": 0.001}
    code = ("import json\n"
            "score = train_with_defaults()\n"
            "print(json.dumps({'metric': score}))\n")
    issues = critique(Idea(operator="improve", params=params), code)
    assert any(i["issue"] == "params_ignored" for i in issues)


def test_one_matching_leaf_is_enough_because_the_rule_is_none_of_them():
    """Stated so it cannot be tightened by accident: `params_ignored` asks whether the proposal was
    implemented AT ALL, not whether every knob was threaded. A partially-applied proposal is a
    modelling judgement the search ranks, not a trust finding."""
    params = {"a.alpha": 1.0, "b.beta": 2.0, "c.gamma": 3.0}
    issues = critique(Idea(operator="improve", params=params), "gamma = 3.0\nrun(gamma)\n")
    assert not any(i["issue"] == "params_ignored" for i in issues)
