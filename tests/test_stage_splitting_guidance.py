"""The stages phase must ask for BOUNDARIES, and must not ask for theatre.

Two failures on the same run motivated this, and they pull in opposite directions — which is why the
guidance has to state both or it will over-correct.

WHAT WENT WRONG. `rubertlite-dr-unified-v5` node 0 declared ONE stage, because the testbed's
`train.py` mines negatives, trains AND evaluates in a single process. Its `expect.files` named a
checkpoint path read off a config field, while the repo composes its output directory as
`<run_name>_<model>`. So a 76-minute two-H200 training that had already computed recall@100 = 0.743
failed its artifact contract, and with one stage there was nothing for a repair to reuse: every
retry would have re-trained from scratch. The repo DOES ship a separate `vectorsearch/test.py` whose
`test_model(..., model=None)` loads the checkpoint from disk — a viable second stage that was never
declared.

WHAT MUST NOT GO WRONG INSTEAD. The operator's own caveat: "if there is no mining stage because the
mined data is already on disk, that is good." A stage exists to make a step restartable, not to make
a manifest look thorough, and a declared stage that re-does settled work costs the run real time.
"""
from __future__ import annotations

import re

from looplab.adapters.repo_developer import LLMRepoDeveloper


def _guidance() -> str:
    """The pipeline guidance as the model receives it — found by content, not by private name, so a
    rename of the constant does not silently empty this file."""
    import looplab.adapters.repo_developer as mod

    blobs = [v for v in vars(mod).values()
             if isinstance(v, str) and "TRAIN-THEN-SCORE PIPELINE" in v]
    assert len(blobs) == 1, f"expected exactly one pipeline guidance blob, found {len(blobs)}"
    return blobs[0]


def test_it_asks_for_the_boundaries_a_repair_can_restart_from():
    text = _guidance()
    assert "SPLIT AS FAR AS THE REPO ALLOWS" in text
    # The REASON, not just the instruction — a rule with no cost attached is one a model trades away.
    assert "every boundary you declare is a boundary a repair can restart from" in text
    assert "re-trains from scratch" in text


def test_it_names_the_inherited_monolith_specifically():
    """The pre-existing rule only forbade HAND-ROLLING a monolith. The repo's own entrypoint being
    one is a different situation and the guidance said nothing about it — which is the case that
    actually occurred."""
    text = _guidance()
    assert "MONOLITH YOU INHERITED" in text
    assert "separate eval entrypoint that loads a saved checkpoint" in text
    assert "even though training already evaluated" in text, (
        "the whole point: an eval inside train is not a reason to skip the eval stage")


def test_it_refuses_stage_theatre_in_the_same_breath():
    """Without this, 'split as far as possible' reads as 'declare a mining stage regardless', and a
    node would re-mine negatives that are already on disk. Both halves or neither."""
    text = _guidance()
    assert "do NOT invent a stage for work that is already done" in text
    assert "ALREADY ON DISK" in text
    assert "never to look thorough" in text


def test_it_tells_the_model_to_trace_the_path_rather_than_read_it_off_a_field():
    text = _guidance()
    assert "COMPOSE the output directory" in text
    assert "the path in the config is NOT the path on disk" in text
    # State the cost. This failure is uniquely expensive because it is detected only at the END.
    assert "AFTER it has spent its full runtime" in text


def test_the_two_rules_are_adjacent_so_neither_can_be_read_alone():
    """A model reading a 2,000-character paragraph acts on what is near what it just read. 'Split
    more' and 'do not invent stages' contradict each other when separated and resolve each other
    when adjacent."""
    text = _guidance()
    split_at = text.index("SPLIT AS FAR AS THE REPO ALLOWS")
    theatre_at = text.index("do NOT invent a stage for work that is already done")
    assert 0 < theatre_at - split_at < 1200, (
        "the caveat drifted away from the rule it qualifies")


def test_the_guidance_still_reaches_the_developer_that_writes_the_code():
    """Content pins above are worth nothing if the blob stops being sent. `_implement_system` is what
    the model is actually handed."""
    import inspect

    import looplab.adapters.repo_developer as mod

    # The blob is a PromptStore default, so the assembly renders it under a key. Both halves matter:
    # the key must still resolve to THIS text (an override replaces it, which is intended), and the
    # call must still be in the assembly at all.
    name = next(k for k, v in vars(mod).items()
                if isinstance(v, str) and "TRAIN-THEN-SCORE PIPELINE" in v)
    source = inspect.getsource(mod)
    assert re.search(rf'render\(self\.prompts, "[a-z_]+", {name}\)', source), (
        "the pipeline guidance is no longer rendered into the developer's system prompt")
    text = _guidance()
    assert len(text) > 500 and "declare_stages" in text
