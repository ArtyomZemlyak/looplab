"""A repo task's merge recombines its parents' CODE; it is never a from-scratch "mean-merge".

Measured 2026-09-24 on a MiniOneRec inference run: `merge_mode="auto"` resolves by the Developer's
`is_code_generating`, which `LLMRepoDeveloper` never declared. So auto fell to the MEAN merge, whose
build seeds from nothing and names no parent's code: "mean-merge of nodes 0,1" took three hours of
Developer sessions and shipped neither parent's change (1.002 against parents at 1.035 and 1.019).
"""
from __future__ import annotations

import sys
from pathlib import Path

from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask


def _repo_developer():
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    return LLMRepoDeveloper(object(), task)


def test_the_repo_developer_declares_that_it_writes_code():
    assert _repo_developer().is_code_generating is True


def test_auto_resolves_to_the_ensemble_merge_for_a_repo_developer(tmp_path):
    from tests.factories import make_engine
    eng = make_engine(tmp_path / "run", developer=_repo_developer(), merge_mode="auto")
    assert eng._merge_mode == "ensemble"
