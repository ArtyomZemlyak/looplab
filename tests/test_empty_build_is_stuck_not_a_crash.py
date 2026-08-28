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


# ------------------------------------- the second consumer, added 2026-08-28 after dsFix1 node 2
_SPEC = Path(__file__).resolve().parents[1] / "looplab" / "engine" / "speculation.py"


def test_the_speculative_path_knows_the_stuck_spelling_too():
    """A sentinel is only as safe as its LEAST aware reader.

    c11251a1 respelled `empty_build_refusal` and taught only `engine/orchestrator.py`. Measured the
    same morning on dsFix1 node 2, built speculatively: the refusal fired, `speculation.py`'s
    `is_developer_error` returned False, the sentinel fell through as if it were solution code, and
    the engine committed a node with `files: {}` and spent 36.1 s evaluating the untouched
    `raise NotImplementedError` template for a 0.0 -- one node slot of three.
    """
    body = _SPEC.read_text(encoding="utf-8")
    # BY CALL SITE, NOT BY PRESENCE. The first version of this case asserted the string appeared in
    # the file, and the import line alone satisfied that -- so reverting the predicate to the crash
    # spelling left it GREEN. A test that does not redden under the regression it was written for
    # is decoration.
    predicate = body[body.index("def _developer_sentinel"):]
    predicate = predicate[:predicate.index("@staticmethod")]
    assert "is_developer_stuck(node.code)" in predicate, \
        "the node predicate reads only the crash spelling; a stuck build passes it as real code"
    assert "is_developer_error(node.code)" in predicate, "the crash spelling must still be read"


def test_the_speculative_stuck_branch_precedes_the_crash_branch_and_asks_for_no_pause():
    body = _SPEC.read_text(encoding="utf-8")
    stuck = body.find("if is_developer_stuck(result.code):")
    crash = body.find("if is_developer_error(result.code):")
    assert stuck != -1 and crash != -1
    assert stuck < crash, "stuck must be tested BEFORE the crash sentinel or it is unreachable"
    block = body[stuck:crash]
    code = "\n".join(ln for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#"))
    assert "EV_NODE_FAILED" in code and '"developer_stuck"' in code
    assert "pause" not in code.lower(), "one card running dry must not stop the run"


def test_every_module_that_reads_one_spelling_reads_both():
    """The invariant this whole episode exists to install, checked across the engine."""
    root = Path(__file__).resolve().parents[1] / "looplab"
    # COUNTED CALL SITES, not presence: an import of `is_developer_stuck` that nothing calls is
    # exactly the state that let dsFix1's node through, and `node_build.py` was found this way
    # after `speculation.py` had already been fixed by hand.
    offenders = []
    for path in root.rglob("*.py"):
        body = path.read_text(encoding="utf-8", errors="replace")
        calls_error = body.count("is_developer_error(")
        calls_stuck = body.count("is_developer_stuck(")
        # the definitions themselves live in core/models.py and are not consumers
        if path.as_posix().endswith("core/models.py"):
            continue
        if calls_error and calls_stuck < calls_error:
            offenders.append(f"{path.relative_to(root).as_posix()} "
                             f"({calls_error} error / {calls_stuck} stuck)")
    assert not offenders, (
        "these modules act on the crash sentinel but cannot see the stuck one, so a build that "
        f"wrote nothing falls through them as solution code: {offenders}")
