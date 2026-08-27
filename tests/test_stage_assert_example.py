"""The `assert` EXAMPLE the Developer is shown must not be a bar nobody measured.

MEASURED, and the measurement is what makes this a guard rather than taste. Reading
`looplab_stages.json` out of every `node_created` row in `runs/`, 33 agent-authored `expect.assert`
strings carry a numeric threshold (v2 5, v3 2, v4 10, v8 3; rubertlite v8 9, v9 4) — and about 28 of
them are the SAME sentence:

    "hard negatives mined for at least 90% of the training queries"

Six different runs converged on one number because THE PROMPT HANDED IT TO THEM: that exact string
was the worked example in `_stages_user` AND in the `declare_stages` tool schema's `assert`
description. The model was copying, not guessing — which is why the uniformity is far tighter than
invention would produce.

THE NUMBER IS WRONG BY MORE THAN 2x ON THIS DATA. `add_negatives` inner-joins mined ids to product
names and drops the rest BY DESIGN, so the real figure is 41.8% (908,121 of 2,170,069) and the
champion (0.7934) was trained on exactly that. e5small-dr-unified-v8 node 1 mined a valid
2,732,976-row parquet, failed its own gate, and was abandoned after two repairs — with the engine's
own diagnostician calling it `check_false_positive`, correctly.

THE REPLACEMENT IS A DIFFERENT KIND OF CLAIM, not a smaller number: "every row has its n_negatives"
is a property the stage CONTROLS; "90% of queries survive a downstream join" is an OUTCOME of the
data it does not. A stage that mines 1% still fails the former loudly.

NEGATIVE pins are substrings on purpose (CLAUDE.md): what must not come back is the TEXT.
"""
from __future__ import annotations

import inspect

from looplab.adapters.repo_developer import LLMRepoDeveloper


def _prompt_sources() -> str:
    """Both channels the example rides on: the user prompt and the tool-schema description."""
    return (inspect.getsource(LLMRepoDeveloper._stages_user)
            + inspect.getsource(LLMRepoDeveloper._stages_emit_spec))


def test_the_measured_wrong_bar_is_not_offered_as_an_example():
    """MUTATION: restore "hard negatives mined for at least 90% of the training queries" in either
    channel -> red, which is the point: it is one careless revert away from returning."""
    src = _prompt_sources()

    # The comment recording WHY carries the string, so strip comment lines before pinning.
    live = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "at least 90%" not in live, (
        "the 90% coverage bar is back in a live prompt channel; it refused the recipe that produced "
        "this box's best result")
    assert "90% of the training queries" not in live


def test_both_channels_still_carry_an_example_at_all():
    """An example is load-bearing — removing it rather than replacing it is the other failure.

    MUTATION: delete the example instead of fixing it -> red. The model needs a model sentence; what
    it must not get is a number the engine knows is wrong.
    """
    for name, src in (("_stages_user", inspect.getsource(LLMRepoDeveloper._stages_user)),
                      ("_stages_emit_spec", inspect.getsource(LLMRepoDeveloper._stages_emit_spec))):
        assert "n_negatives negatives on every row" in src, f"{name} lost its assert example"
        assert "epochs completed" in src, f"{name} lost its second example"


def test_the_RULE_is_stated_and_not_only_exemplified():
    """The example alone is what got copied last time, so the rule is spelled out.

    MUTATION: drop the sentence and keep only the new example -> red. A model shown a better example
    copies the better example; a model told the rule can apply it to a stage nobody anticipated.
    """
    src = inspect.getsource(LLMRepoDeveloper._stages_user)

    assert "have NOT measured" in src
    assert "PRINT the quantity you do not control" in src
    assert "measure it first" in src, "the escape hatch must exist: a measured bar is legitimate"


def test_the_stage_CONTROLS_distinction_reaches_the_tool_schema():
    """The schema description is the channel in front of the model as it fills the argument.

    MUTATION: fix only `_stages_user` and leave the schema -> red. That asymmetry is exactly what
    100e22b6 established for `question_concepts`: the field description is the one channel present
    at the moment of the call.
    """
    src = inspect.getsource(LLMRepoDeveloper._stages_emit_spec)

    assert "CONTROLS" in src
    assert "not measured" in src
