"""C4 independent critic (execution-free)."""
from __future__ import annotations

from looplab.critic import critique
from looplab.models import Idea


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
