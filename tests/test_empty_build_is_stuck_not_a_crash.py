"""A fresh build that wrote no candidate ends the NODE, not the RUN.

`empty_build_refusal` convicts a session that produced no code. Until 2026-08-28 it spelled that
with `DEVELOPER_ERROR_PREFIX` -- the sentinel `engine/orchestrator.py` routes to the provider
circuit breaker -- so a model that probed 24 times and never called `write_file` paused the whole
run. `core/models.py:943` already states the rule that violates: "(developer error: ...) routes to
the provider circuit breaker and pauses the RUN, which is exactly the wrong answer for a healthy
model that has simply run out of ideas about one node."

MEASURED on the probe corpus: 2 of 106 nodes ended this way, dsNew2 node 2 and qwen38f node 0, and
BOTH ended their run. dsNew2 stopped at 2 evaluated nodes of 3 while the gateway answered every
call -- at 2-4 nodes per $1 run that is a third of the run thrown away for a model's bad turn.

The hazard this pins is the reason it was not a one-line respelling: the fresh-build path knew ONLY
the crash spelling, so a refusal respelled without a matching branch would fall through to "this is
solution code" -- the false-success path where the eval runs the PARENT's entrypoint and inherits
the PARENT's metric.
"""
from __future__ import annotations

import re
from pathlib import Path

from looplab.adapters.repo_developer import empty_build_refusal
from looplab.core.models import is_developer_error, is_developer_stuck

_ORCH = Path(__file__).resolve().parents[1] / "looplab" / "engine" / "orchestrator.py"


def _refusal(files=None, deleted=None):
    return empty_build_refusal(error=None, base=None, base_deleted=None,
                               files=files if files is not None else {}, deleted=deleted)


def test_a_build_that_wrote_nothing_is_stuck_and_is_not_a_crash():
    refusal = _refusal()
    assert refusal, "an empty build must still be refused"
    assert is_developer_stuck(refusal), refusal
    assert not is_developer_error(refusal), \
        "the crash spelling pauses the RUN through the provider circuit breaker"


def test_the_manifest_only_build_is_stuck_too():
    """Writing only `looplab_stages.json` is a plan with nothing to evaluate -- same fact."""
    from looplab.adapters.repo_write_tools import STAGES_MANIFEST
    refusal = _refusal(files={STAGES_MANIFEST: "{}"})
    assert is_developer_stuck(refusal) and not is_developer_error(refusal)
    assert "only the stage manifest" in refusal


def test_a_build_that_wrote_something_is_not_refused_at_all():
    assert _refusal(files={"solver.py": "x = 1\n"}) == ""
    assert _refusal(files={}, deleted=["solver.py"]) == ""


def test_the_fresh_build_path_handles_stuck_before_it_handles_a_crash():
    """Order matters: `is_developer_error` does not match the stuck spelling, so without this
    branch -- or with it placed after -- the sentinel reaches the code path and is evaluated."""
    body = _ORCH.read_text(encoding="utf-8")
    stuck = body.find("elif is_developer_stuck(code):")
    crash = body.find("elif is_developer_error(code):")
    assert stuck != -1, "the fresh-build path lost its stuck branch"
    assert crash != -1, "the crash branch must still exist"
    assert stuck < crash, "stuck must be tested BEFORE the crash sentinel"


def test_the_stuck_branch_fails_the_node_without_asking_for_a_pause():
    body = _ORCH.read_text(encoding="utf-8")
    start = body.find("elif is_developer_stuck(code):")
    block = body[start:body.find("elif is_developer_error(code):", start)]
    # CODE ONLY. The block's own comment explains what a pause would do and why this branch does
    # not do it, so a naive substring search convicts the explanation instead of the behaviour --
    # which is exactly what the first version of this test did.
    code_lines = [ln for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    code = "\n".join(code_lines)
    assert "EV_NODE_FAILED" in code, "the node must still terminalize -- one terminal, always"
    assert '"developer_stuck"' in code, "the reason must not be developer_crash"
    assert "developer_crash" not in code, code
    assert "pause" not in code.lower(), "ending the node must not end the run"
    assert "developer_crash_records" not in code, "that helper is the crash/pause pair"


def test_the_refusal_still_says_what_happened_in_words():
    """A sentinel nobody can read is a sentinel nobody acts on."""
    refusal = _refusal()
    assert "no candidate to evaluate" in refusal and "untouched template" in refusal
    assert re.match(r"^\(developer stuck: ", refusal), refusal
