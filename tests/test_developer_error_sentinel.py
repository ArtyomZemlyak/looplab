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
    for path in sorted(_PKG.rglob("*.py")):
        if path.name == "models.py" and path.parent.name == "core":
            continue                      # the definition site
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
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

