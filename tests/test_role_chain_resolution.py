"""One walk of the researcher→inner→fallback→developer chain (doc 25 EC-13), and no second GPU API
for the same pool (doc 25 EC-14).

The active Researcher is usually a WRAPPER — a surrogate, a K-idea panel, a foresight `__getattr__`
proxy, the unified facade — and the three things the engine pulls off a role live on whichever link
actually carries them. Four sites had hand-rolled the walk, and a hand-rolled copy is exactly where a
link gets missed. The failure is silent in the worst way available: the resolution falls back to a
DEFAULT rather than raising, so a run parses structured output with `tool_call` against a provider
configured for JSON, or distils lessons with none of the operator's prompt overrides — and every
downstream artifact looks well-formed.

The two truthiness rules differ ON PURPOSE and are pinned below: an EMPTY parser name is not a
configured parser, but an EMPTY PromptStore is a wired store that overrides nothing.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.agents.roles import (resolve_role_client, resolve_role_parser, resolve_role_prompts,
                                  role_wrapper_chain)


class _Role:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Client:
    def complete_text(self, *args, **kwargs):  # the duck type the resolver actually requires
        return ""


# ------------------------------------------------------------------ the chain order

def test_the_chain_is_researcher_then_inner_then_fallback_then_developer():
    inner, fallback, developer = _Role(), _Role(), _Role()
    researcher = _Role(inner=inner, fallback=fallback)
    assert role_wrapper_chain(researcher, developer) == (researcher, inner, fallback, developer)


def test_a_role_with_no_wrapper_links_still_yields_a_four_slot_chain():
    """The `None` holes are deliberate: callers keep using `getattr(obj, ...)`, which is already
    None-tolerant, instead of needing a second filtering rule per call site."""
    chain = role_wrapper_chain(_Role(), None)
    assert len(chain) == 4 and chain[1] is None and chain[2] is None and chain[3] is None


def test_a_missing_researcher_does_not_raise():
    assert role_wrapper_chain(None, None) == (None, None, None, None)


# ------------------------------------------------------------------ the parser

def test_the_parser_comes_from_the_outermost_link_that_has_one():
    researcher = _Role(parser="json_schema", inner=_Role(parser="tool_call"))
    assert resolve_role_parser(researcher, None) == "json_schema"


def test_a_wrapper_with_no_parser_is_walked_THROUGH_to_the_wrapped_role():
    """The finding's whole point. A panel/surrogate wrapper carries no `parser` of its own; a lookup
    that stopped at the wrapper would silently use the default against a provider configured for
    something else, and nothing downstream reports a parse mode as wrong."""
    wrapped = _Role(parser="json_schema")
    assert resolve_role_parser(_Role(inner=wrapped), None) == "json_schema"


def test_the_fallback_link_is_reached_when_neither_the_wrapper_nor_inner_has_one():
    assert resolve_role_parser(_Role(inner=_Role(), fallback=_Role(parser="json_schema")),
                               None) == "json_schema"


def test_the_developer_is_the_last_resort_before_the_default():
    assert resolve_role_parser(_Role(), _Role(parser="json_schema")) == "json_schema"


def test_nothing_wired_is_the_documented_tool_call_default():
    """Toy backends wire no parser at all; `agent_merge` and the verifier both assume exactly this."""
    assert resolve_role_parser(None, None) == "tool_call"


@pytest.mark.parametrize("empty", ["", None])
def test_an_EMPTY_parser_name_is_not_a_configured_parser(empty):
    """Truthy, not `is not None`. Passing "" through to the structured-output layer is not "the
    operator chose an empty parser" — it is an unset field, and the default is the right answer."""
    assert resolve_role_parser(_Role(parser=empty), _Role(parser="json_schema")) == "json_schema"


# ------------------------------------------------------------------ the PromptStore

def test_an_EMPTY_prompt_store_still_counts_as_wired():
    """The opposite rule from the parser, and deliberately so: a PromptStore that overrides nothing
    is a configured store. Skipping past it would keep walking into a wrapper that was never
    configured, and the operator's (empty but real) store would be replaced by whatever came next."""
    empty_store = {}
    other = {"merge_system": "text"}
    assert resolve_role_prompts(_Role(prompts=empty_store, inner=_Role(prompts=other)),
                                None) is empty_store


def test_prompts_walk_the_same_chain_as_the_parser():
    store = {"merge_system": "text"}
    assert resolve_role_prompts(_Role(inner=_Role(), fallback=_Role(prompts=store)), None) is store


def test_no_prompt_store_anywhere_is_None():
    assert resolve_role_prompts(_Role(), _Role()) is None


# ------------------------------------------------------------------ the LLM client

def test_the_client_must_actually_be_an_llm_client():
    """`hasattr(complete_text)` is load-bearing: toy backends carry a `client` attribute that is not
    an LLM client. Returning one turns "no LLM wired, skip this advisory step" into an AttributeError
    inside distillation — a run-end crash on a path whose whole contract is best-effort."""
    real = _Client()
    assert resolve_role_client(_Role(client=object(), inner=_Role(client=real)), None) is real


def test_no_usable_client_is_None_not_a_raise():
    assert resolve_role_client(_Role(client=object()), _Role()) is None


def test_the_developer_client_is_used_when_the_researcher_has_none():
    real = _Client()
    assert resolve_role_client(_Role(), _Role(client=real)) is real


# ------------------------------------------------------------------ every site delegates

@pytest.mark.parametrize("module,holder,function", [
    ("looplab.engine.strategy", "StrategyCadenceMixin", "_verifier_soundness"),
    ("looplab.engine.novelty", "NoveltyGateMixin", "_verified_failed_direction_reopen"),
])
def test_no_engine_site_re_derives_the_parser_walk(module, holder, function):
    import importlib

    source = inspect.getsource(
        getattr(getattr(importlib.import_module(module), holder), function))
    assert "resolve_role_parser(" in source, f"{function} does not use the shared resolver"
    assert '"inner"' not in source and "'inner'" not in source, (
        f"{function} still walks the wrapper chain by hand")


def test_the_lessons_helpers_delegate_too():
    from looplab.engine import lessons, lessons_distill

    client_source = inspect.getsource(lessons.LessonMemory.reflect_client)
    merge_source = inspect.getsource(lessons_distill.LessonDistillMixin._merge_prompt_opts)
    assert "resolve_role_client(" in client_source
    assert "resolve_role_prompts(" in merge_source and "resolve_role_parser(" in merge_source
    for source in (client_source, merge_source):
        assert '"fallback"' not in source, "the chain is re-derived alongside the shared resolver"


def test_the_merge_opts_still_return_prompts_and_parser_in_that_order():
    """`consolidate(...)` and `agent_merge(...)` are called positionally off this pair at two sites;
    swapping them would pass a PromptStore where a parser name belongs and only fail deep inside a
    provider call."""
    from looplab.engine.lessons_distill import LessonDistillMixin

    store = {"merge_system": "text"}
    holder = LessonDistillMixin.__new__(LessonDistillMixin)
    holder._e = _Role(researcher=_Role(prompts=store, parser="json_schema"), developer=None)
    assert holder._merge_prompt_opts() == (store, "json_schema")


# ------------------------------------------------------------------ EC-14: one GPU API

@pytest.mark.parametrize("name", ["_acquire_gpu", "_release_gpu"])
def test_the_single_gpu_wrappers_are_gone(name):
    """They were production-dead — called only from one test — and their "serial mode never pins"
    branch was a SECOND copy of a rule that really lives in `_resource_request_for_node`. A second
    answer to an admission question is worse than none: the test could keep passing while the path
    the dispatcher actually takes disagreed."""
    from looplab.engine import resources

    assert not hasattr(resources.ResourceSchedulingMixin, name), (
        f"{name} came back; the dispatcher uses the multi-GPU API")


def test_the_serial_no_pin_rule_lives_in_admission():
    from looplab.engine import resources

    source = inspect.getsource(resources.ResourceSchedulingMixin._resource_request_for_node)
    assert "parallel > 1" in source, "the unspecified-footprint pinning rule moved or was dropped"
