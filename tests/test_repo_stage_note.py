"""The three-way pipeline note is PROMPT TEXT, so its bytes are the contract (doc 25 RA-07).

It was built inline in the middle of a 185-line `_run`. Extracting it is safe only because the text
moved verbatim — CLAUDE.md forbids rewording a prompt as part of a refactor, and this wording is
load-bearing for a recorded reason: the old prompt asserted a train stage unconditionally, and after
an empty STAGES phase the model wrote a score-only entrypoint that scored a stale checkpoint.

So these pin the exact strings. A "tidy-up" of the wording is a red test, not a silently different
agent.
"""
from __future__ import annotations

import pytest

from looplab.adapters.repo_developer import LLMRepoDeveloper


def _note(operator_stages, declared, carried_over=False, manifest_protected=False) -> str:
    return LLMRepoDeveloper._stage_note(
        object(), operator_stages, declared, carried_over, manifest_protected)


def test_operator_declared_pipeline_runs_verbatim():
    got = _note(True, [{"name": "train"}, {"name": "score"}])
    assert got == ("\nPIPELINE for this node (OPERATOR-declared, runs verbatim): "
                   "train → score. Implement the code those stages run.")


def test_a_pipeline_the_stages_phase_declared():
    got = _note(False, [{"name": "train"}])
    assert got == ("\nPIPELINE for this node (declared by your STAGES phase): train "
                   "→ score (operator cmd). Implement the code those stages run; the "
                   "eval entrypoint only SCORES the artifacts the earlier stages produce.")


def test_a_pipeline_carried_over_from_the_parent_says_so():
    """The distinction that exists because the model must not think it declared these itself."""
    got = _note(False, [{"name": "train"}], carried_over=True)
    assert "carried over from the parent solution — your STAGES phase declared nothing new this node" in got
    assert "declared by your STAGES phase" not in got.split("(")[1].split(")")[0]


def test_no_declared_stages_tells_the_model_the_cmd_runs_alone():
    """The variant the bug was about: with no stages, the entrypoint must train a FRESH model rather
    than score a checkpoint that an earlier stage never produced."""
    got = _note(False, [])
    assert got == ("\nNO pipeline stages are declared for this node"
                   ": the operator's cmd runs ALONE as a single command. The code it "
                   "runs must do ALL the work itself when invoked — train a FRESH model, "
                   "then score it and print the metric (never read a pre-existing "
                   "checkpoint or a static results file).")


def test_a_protected_manifest_is_named_in_the_no_stages_note():
    got = _note(False, [], manifest_protected=True)
    assert " (the operator protected looplab_stages.json)" in got


def test_operator_stages_win_over_declared():
    """Precedence: an operator-declared pipeline runs verbatim even when the STAGES phase produced
    its own list, so the model must not be told to implement the wrong chain."""
    got = _note(True, [{"name": "a"}])
    assert "OPERATOR-declared" in got and "score (operator cmd)" not in got


def test_the_note_is_not_rebuilt_inline_in_run():
    """The regression: a second spelling drifting from this one inside `_run`."""
    import inspect

    src = inspect.getsource(LLMRepoDeveloper._run)
    assert "PIPELINE for this node" not in src, (
        "`_run` builds the pipeline note inline again; call `_stage_note` so one wording ships")
