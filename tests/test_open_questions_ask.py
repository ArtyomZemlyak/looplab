"""The Researcher IS asked for `open_questions`, and the carrier survives to the durable log.

THIS FILE EXISTS BECAUSE A MARKER GOT ITS OWN PREMISE WRONG, in a way that is easy to repeat. The
note on `Idea.open_questions` said "the Researcher is never ASKED for one", inferred from the string
`open_questions` occurring ZERO times in `agents/roles.py`, `agents/unified_agent.py` and
`search/panel.py`. Grepping those files is the wrong instrument: the producer emits
`IdeaEmission.model_json_schema()`, and `IdeaEmission` derives from `Idea`, so this field's own
description has reached the model on every proposal since the day it landed.

WHAT IS TRUE, re-measured 2026-08-30: the schema ask has always been there, and across every
`node_created` row on this box — 155 of them — ZERO carry a filled `open_questions`. So the step the
marker prescribed ("ask in the emit schema first, look at what comes back") was already done and the
answer was zero. The only untested lever was PROSE, which the user turn now carries.

The three properties pinned here are the ones whose silent loss would make the next measurement a
lie: the field must be IN the emitted schema (or the model is not asked at all), the user turn must
ASK (or the new measurement is of the old condition), and the value must SURVIVE the writer boundary
(or a filled answer is recorded as empty and reads as "the Researcher volunteered nothing").

Every assertion has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import inspect

from looplab.core.models import Idea, IdeaEmission, durable_idea_payload


def test_the_emitted_schema_really_does_ask():
    """Mutation: drop `open_questions` from `Idea`, and the model is never asked at all — which is
    what the marker believed was already the case."""
    schema = IdeaEmission.model_json_schema()
    field = schema.get("properties", {}).get("open_questions")
    assert field is not None, (
        "the producer emits IdeaEmission's JSON schema; without this property the Researcher has no "
        "way to know the field exists, and any 'it never fills it' measurement is vacuous")
    assert "not pursuing" in (field.get("description") or "").lower(), (
        "and the description must say what a question IS — mutation: blank the description and the "
        "field becomes an unexplained key the model fills with restatements of its own proposal")


def test_the_user_turn_asks_in_PROSE_too():
    """The lever the 2026-08-30 change actually added.

    OVER THE AST, AND THE FIRST CUT OF THIS TEST WAS THE VERY DEFECT CLAUDE.md WARNS ABOUT. It read
    `"open_questions" in inspect.getsource(propose)`, and the explanatory COMMENT above the new
    clause contains that string — so deleting the prose left the pin green. The mutation run is what
    said so. String literals are AST nodes; comments are not, so the ask is read out of the function's
    own constants.

    Mutation: delete the clause and the next run measures the OLD condition — schema-only — while
    reporting it as a new result."""
    import ast

    from looplab.agents import roles

    tree = ast.parse(inspect.getsource(roles.LLMResearcher.propose).lstrip())
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert any("open_questions" in text for text in literals), (
        "schema presence is not an ask: this repo measured that prose outranks a computed cue, and "
        "0 of 155 proposals filled this field under the schema-only condition. A COMMENT naming the "
        "field is not an ask — only a string the prompt actually carries is")
    assert any("worth its own investigation" in text for text in literals), (
        "and it must say what a question IS, or the model fills it with restatements of the "
        "experiment it is already proposing")


def test_a_filled_value_SURVIVES_the_writer_boundary_to_the_durable_payload():
    """The silent-loss path. Mutation: make `to_idea` drop it, or drop it from
    `durable_idea_payload`, and a Researcher that DID answer is recorded as having said nothing —
    which would read exactly like the zero this field is being measured for."""
    questions = ["does a stronger teacher help at this batch size?",
                 "is recall@100 saturated on this corpus?"]
    emission = IdeaEmission(operator="draft", params={"x": 1.0}, rationale="r",
                            concept_mode="full", open_questions=questions)

    idea = emission.to_idea()
    assert idea.open_questions == questions, "the strict->durable crossing must keep it"

    payload = durable_idea_payload(idea)
    assert payload.get("open_questions") == questions, (
        "it must ride `durable_idea_payload` -> `node_created`; dropped here it never reaches the "
        "log and no later reader can tell 'not asked' from 'asked and answered nothing'")

    assert Idea(**payload).open_questions == questions, (
        "and the replay read must return it — `Idea(**d['idea'])` is how every fold rebuilds this")


def test_an_ABSENT_value_is_still_an_empty_list_for_an_old_log():
    """Invariant #5: additive, reader-side default. Mutation: make the field required and every log
    written before it existed fails to replay."""
    assert Idea(operator="draft", params={}, rationale="r").open_questions == []
