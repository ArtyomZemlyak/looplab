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

from looplab.adapters.repo_developer import LLMRepoDeveloper


def _guidance() -> str:
    """The pipeline guidance as the model receives it — found by content, not by private name, so a
    rename of the constant does not silently empty this file."""
    import looplab.adapters.repo_developer as mod

    blobs = [v for v in vars(mod).values()
             if isinstance(v, str) and "TRAIN-THEN-SCORE PIPELINE" in v]
    assert blobs, "the pipeline guidance is gone from the developer module"
    # The system body is now COMPOSED (head + an execution clause chosen by `Settings.developer_probe`
    # + tail), so the guidance legitimately appears in three module globals: the tail that owns it and
    # the two assembled bodies that contain the tail. A flat "exactly one" count would fail on that
    # while a SECOND, independently-worded copy — the drift this guard was actually written against —
    # would still slip past a count of two. So the rule is stated properly instead: one blob owns the
    # text, and every other carrier must literally CONTAIN it.
    owner = min(blobs, key=len)
    derived = [b for b in blobs if owner not in b]
    assert not derived, f"{len(derived)} independent copies of the pipeline guidance"
    return owner


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
    """Content pins above are worth nothing if the blob stops being sent.

    DRIVEN, not matched against the assembly's source text (CLAUDE.md tier 1). The body is composed
    now, and which execution clause it carries depends on `Settings.developer_probe` — so a regex
    over `_run` could only ever see one of the two prompts a Developer can actually be handed, and
    would go green while the other lost the guidance. Build BOTH and look in each; that is the
    property, and it survives the next refactor of how the body is assembled.
    """
    from _source_scan import called_names
    from looplab.core.prompts import render

    text = _guidance()
    for probe in (True, False):
        dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
        dev._probe, dev.prompts = probe, None
        assert text in dev._system_body(render), (
            f"the pipeline guidance is missing from the system prompt with developer_probe={probe}")
    # The PromptStore key is still an override point in both configurations — replacing the body is
    # the operator's business, dropping it silently is not.
    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    dev._probe, dev.prompts = True, {"repo_developer_system_body": "OPERATOR BODY"}
    body = dev._system_body(render)
    assert body.startswith("OPERATOR BODY"), (
        "an operator override must replace the persona body, not be merged into it")
    # What may follow it is exactly the CODE-OWNED suffixes, appended AFTER `render()` on purpose
    # (see `_system_body`): an override replaces the PERSONA and must not be able to drop a rule the
    # code is responsible for. The Developer currently owns NO such suffix — `_system_body` states
    # why at length: `developer_probe=False` must reproduce the historical prompt byte for byte, and
    # the context-before-tools rule is the one that A/B'd to nothing while the same knowledge as
    # DATA moved cold-start tool calls 41.3 -> 17.7. So the closed set is empty, and the assertion
    # is that NOTHING follows the override.
    #
    # This assertion said `== "OPERATOR BODY" + _CONTEXT_BEFORE_TOOLS_RULE` and was RED on this
    # branch before any rebase: the commit that stopped appending the rule left the expectation
    # behind. Keeping the shape (prefix + a named, closed suffix set) rather than loosening it to
    # `startswith` is deliberate — an empty set still forbids anything leaking in, which is the
    # property, and the next code-owned suffix has one obvious place to be named.
    _CODE_OWNED_SUFFIXES = ""
    assert body == "OPERATOR BODY" + _CODE_OWNED_SUFFIXES, (
        "the override carried something other than the code-owned suffixes")
    # ...and the assembly must still ASK for the body. AST, not a substring: a commented-out call
    # would satisfy a regex while the model received no guidance at all.
    assert "self._system_body" in called_names(LLMRepoDeveloper._run)
    assert len(text) > 500 and "declare_stages" in text
