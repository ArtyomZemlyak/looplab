"""Paid steward failures must be explicit and must never trigger a hidden parser call."""
from __future__ import annotations

import pytest

from looplab.core.errors import LLMError
from looplab.core.parse import ParseError
from looplab.engine.claim_steward import propose_claim_curation
from looplab.engine.concept_steward import propose_concept_curation
from looplab.engine.task_facets import propose_task_facets


class _FailingProvider:
    def __init__(self):
        self.calls: list[str] = []

    def complete_tool(self, _messages, _schema):
        self.calls.append("tool")
        raise LLMError("provider unavailable")

    def complete_text(self, _messages):
        self.calls.append("text")
        return "{}"


_CASES = [
    (
        lambda client, strict: propose_concept_curation(
            {"concepts": [{"concept": "a", "n_runs": 1, "runs": []}]},
            client,
            raise_on_failure=strict,
        ),
        {"merges": [], "splits": [], "purges": []},
    ),
    (
        lambda client, strict: propose_claim_curation(
            [{
                "statement": "a helps",
                "maturity": "machine-proposed",
                "epistemic": "supported",
                "n_support": 1,
                "n_oppose": 0,
                "scopes": ["task"],
            }],
            client,
            raise_on_failure=strict,
        ),
        {"decisions": []},
    ),
    (
        lambda client, strict: propose_task_facets(
            "rank documents", "dataset", client, raise_on_failure=strict),
        {},
    ),
]


@pytest.mark.parametrize("invoke,empty", _CASES)
def test_durable_steward_failure_raises_after_exactly_one_provider_path(invoke, empty):
    strict_client = _FailingProvider()
    with pytest.raises(ParseError):
        invoke(strict_client, True)
    assert strict_client.calls == ["tool"]

    best_effort_client = _FailingProvider()
    assert invoke(best_effort_client, False) == empty
    assert best_effort_client.calls == ["tool"]
