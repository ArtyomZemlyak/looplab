"""The Developer-crash sentinel has exactly one spelling (doc 25 ES-11).

A Developer that cannot finish returns its error IN BAND as the node's code. Six consumers key a
terminal/no-terminal decision on recognising it, and a consumer that fails to recognise it does not
error — it records a node as EVALUATED whose code is an error message. That is a false success in
the search, and the metric attached to it is meaningless.

While the prefix was a bare literal at seven sites, a reworded producer (a different Developer
backend, an i18n pass, a stray space) would have flipped all six consumers silently, with no failing
test anywhere. This file is the source-scanning guard in the spirit of the other seam registries in
`CLAUDE.md`: the literal may appear only where the constant is DEFINED.
"""
from __future__ import annotations

import re
from pathlib import Path

from _source_scan import iter_sources

import pytest

from looplab.core.models import DEVELOPER_ERROR_PREFIX, is_developer_error

_PKG = Path(__file__).resolve().parents[1] / "looplab"


def test_the_sentinel_text_itself_is_unchanged():
    """Pinned as a literal HERE and nowhere else: the value is a wire contract with old run logs.

    Nodes already on disk carry codes starting with this exact prefix, and `is_developer_error` is
    what a replay uses to classify them. Changing the text silently reclassifies history."""
    assert DEVELOPER_ERROR_PREFIX == "(developer error:"
    assert is_developer_error("(developer error: LLM unreachable)")
    assert not is_developer_error("(developer note: fine)")
    assert not is_developer_error("print('(developer error:')")   # must be a PREFIX, not a substring
    assert not is_developer_error(None) and not is_developer_error(b"(developer error:")


def test_no_module_respells_the_sentinel_literally():
    offenders = []
    for path, source in iter_sources(_PKG):
        if path.name == "models.py" and path.parent.name == "core":
            continue                      # the definition site
        for number, line in enumerate(source.splitlines(), 1):
            code = line.split("#", 1)[0]  # a why-comment may quote the sentinel; only code counts
            if "(developer error:" in code:
                offenders.append(f"{path.relative_to(_PKG.parent)}:{number}: {line.strip()}")
    assert not offenders, (
        "the Developer-crash sentinel must come from core.models.DEVELOPER_ERROR_PREFIX / "
        "is_developer_error, never a re-spelled literal:\n" + "\n".join(offenders))


@pytest.mark.parametrize("relative,symbol", [
    ("engine/orchestrator.py", "is_developer_error"),
    ("engine/node_build.py", "is_developer_error"),
    ("engine/speculation.py", "is_developer_error"),
    ("adapters/repo_developer.py", "DEVELOPER_ERROR_PREFIX"),
])
def test_every_producer_and_consumer_still_goes_through_the_shared_name(relative, symbol):
    """Guards the other direction: a module that drops the import has stopped participating."""
    source = (_PKG / relative).read_text(encoding="utf-8")
    assert re.search(rf"\b{symbol}\b", source), (
        f"{relative} no longer references {symbol} — it either lost its crash handling or "
        "re-spelled the sentinel some other way")



# --------------------------------------------------------------------------- #
# The Researcher's twin. A Developer that cannot finish returns its error in band as the node's CODE;
# a Researcher that cannot finish returns it in band as the Idea's RATIONALE. The failure mode is the
# same shape and worse in one respect: nothing downstream errors either, and the run reports a
# CHAMPION over experiments that were never proposed (`/tmp/ll-s4b/run`). Same guard, same reason.
# --------------------------------------------------------------------------- #

from looplab.agents.roles import (  # noqa: E402
    RESEARCHER_FALLBACK_PREFIX, is_researcher_fallback, researcher_fallback_cause)


def test_the_researcher_fallback_sentinel_text_is_unchanged():
    """Pinned here and nowhere else: it is a wire contract with logs already on disk. Nodes written
    before the constant existed carry rationales starting with this exact prefix."""
    assert RESEARCHER_FALLBACK_PREFIX == "fallback ("

    class _Idea:
        rationale = "fallback (agent parse failed: Connection error.)"

    assert is_researcher_fallback(_Idea())
    assert researcher_fallback_cause(_Idea()) == "agent parse failed: Connection error."

    class _Real:
        rationale = "Expand the features with a degree-2 polynomial basis."

    assert not is_researcher_fallback(_Real())
    # Must be a PREFIX, not a substring, and total over anything without a string rationale.
    class _Mentions:
        rationale = "the previous node was a fallback (agent parse failed)"

    assert not is_researcher_fallback(_Mentions())
    assert not is_researcher_fallback(None) and not is_researcher_fallback(object())


def test_no_module_respells_the_researcher_fallback_literally():
    """AST-based, unlike its Developer-side twin above, and it has to be: `agents/preflight.py`'s
    docstring QUOTES the sentinel while telling the story of the run that produced it, and a
    why-comment/docstring that quotes a contract is exactly what this codebase asks for. Only string
    constants a module could actually BUILD a rationale from count — docstrings are excluded by
    position, the way Python itself distinguishes them."""
    import ast

    offenders = []
    for path, source in iter_sources(_PKG):
        if path.name == "roles.py" and path.parent.name == "agents":
            continue                      # the definition site
        tree = ast.parse(source)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None) or []
                first = body[0] if body else None
                if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docstrings.add(id(first.value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and node.value.startswith(RESEARCHER_FALLBACK_PREFIX)):
                offenders.append(
                    f"{path.relative_to(_PKG.parent)}:{node.lineno}: {node.value[:60]!r}")
    assert not offenders, (
        "a degraded proposal's rationale must come from roles.researcher_fallback_rationale / "
        "is_researcher_fallback, never a re-spelled literal:\n" + "\n".join(offenders))


@pytest.mark.parametrize("relative,symbol", [
    ("agents/roles.py", "researcher_fallback_rationale"),
    ("agents/agent.py", "researcher_fallback_rationale"),
    ("engine/orchestrator.py", "is_researcher_fallback"),
])
def test_every_researcher_fallback_site_goes_through_the_shared_name(relative, symbol):
    source = (_PKG / relative).read_text(encoding="utf-8-sig")
    assert re.search(rf"\b{symbol}\b", source), (
        f"{relative} no longer references {symbol} — it either lost the proposal-path circuit "
        "breaker or re-spelled the sentinel some other way")
